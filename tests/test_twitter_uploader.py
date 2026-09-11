from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from uploader.twitter_uploader.main import (
    PreparedTwitterSegment,
    PlannedTwitterSegment,
    TWITTER_MAX_IMAGES_PER_TWEET,
    TWITTER_MAX_VIDEO_SECONDS,
    TWITTER_SPLIT_SEGMENT_SECONDS,
    extract_created_post_id,
    materialize_thread_segments,
    plan_image_segments,
    plan_thread_segments,
    plan_video_segments,
    prepare_image_for_upload,
    publish_thread_segments,
)


class TwitterUploaderPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _video(self, name: str) -> Path:
        path = self.tmp_path / name
        path.write_bytes(b"video")
        return path

    def test_video_at_or_below_140_seconds_stays_unsplit(self) -> None:
        video = self._video("clip.mp4")

        exact_limit = plan_video_segments(
            video,
            duration_seconds=TWITTER_MAX_VIDEO_SECONDS,
            source_index=0,
        )
        below_limit = plan_video_segments(
            video,
            duration_seconds=119.5,
            source_index=0,
        )

        self.assertEqual(len(exact_limit), 1)
        self.assertEqual(len(below_limit), 1)
        self.assertFalse(exact_limit[0].requires_split)
        self.assertFalse(below_limit[0].requires_split)
        self.assertEqual(exact_limit[0].start_seconds, 0.0)

    def test_video_above_140_seconds_splits_into_139_second_max_chunks(self) -> None:
        video = self._video("long.mp4")

        planned = plan_video_segments(
            video,
            duration_seconds=280.0,
            source_index=0,
        )

        self.assertEqual(len(planned), 3)
        self.assertTrue(all(segment.requires_split for segment in planned))
        self.assertTrue(all(segment.duration_seconds <= TWITTER_SPLIT_SEGMENT_SECONDS for segment in planned))
        self.assertEqual(
            [(segment.segment_index, segment.start_seconds, segment.duration_seconds) for segment in planned],
            [(0, 0.0, 139.0), (1, 139.0, 139.0), (2, 278.0, 2.0)],
        )

    def _image(self, name: str) -> Path:
        path = self.tmp_path / name
        path.write_bytes(b"image")
        return path

    def test_image_batches_are_capped_at_four_per_tweet(self) -> None:
        images = [self._image(f"shot-{index}.jpg") for index in range(9)]

        planned = plan_image_segments(images)

        self.assertEqual([len(segment.media_paths) for segment in planned], [4, 4, 1])
        self.assertTrue(all(len(segment.upload_paths) <= TWITTER_MAX_IMAGES_PER_TWEET for segment in planned))
        self.assertTrue(all(not segment.requires_split for segment in planned))
        self.assertEqual([segment.source_index for segment in planned], [0, 4, 8])

    def test_mixed_images_and_videos_keep_upload_order(self) -> None:
        first = self._image("first.jpg")
        second = self._image("second.png")
        video = self._video("middle.mp4")
        third = self._image("third.jpg")

        planned = plan_thread_segments(
            [first, second, video, third],
            duration_reader=lambda path: 30.0,
        )

        self.assertEqual(
            [(segment.source_index, len(segment.upload_paths)) for segment in planned],
            [(0, 2), (2, 1), (3, 1)],
        )
        # The paired stills share one tweet; the video gets its own.
        self.assertEqual(planned[0].upload_paths, (first.resolve(), second.resolve()))
        self.assertEqual(planned[1].upload_paths, (video.resolve(),))

    def test_video_split_still_applies_alongside_images(self) -> None:
        video = self._video("long.mp4")
        trailing = self._image("trailing.jpg")

        planned = plan_thread_segments(
            [video, trailing],
            duration_reader=lambda path: 280.0,
        )

        self.assertEqual([segment.requires_split for segment in planned], [True, True, True, False])
        self.assertEqual(
            [segment.segment_index for segment in planned if segment.requires_split],
            [0, 1, 2],
        )

    def test_materialized_image_posts_expose_all_media_paths(self) -> None:
        planned = plan_image_segments([self._image(f"m-{index}.jpg") for index in range(4)])

        with materialize_thread_segments(planned) as prepared:
            self.assertEqual(len(prepared), 1)
            self.assertEqual(prepared[0].post_index, 0)
            self.assertTrue(prepared[0].is_image_post)
            self.assertEqual(len(prepared[0].upload_paths), 4)
            # upload_path stays the primary media for backwards compatibility.
            self.assertEqual(prepared[0].upload_path, prepared[0].upload_paths[0])

    def test_materialize_normalizes_oversized_images_via_the_injected_hook(self) -> None:
        calls: list[tuple[Path, int]] = []

        def fake_normalizer(source: Path, temp_root: Path, index: int) -> Path:
            calls.append((source, index))
            replaced = Path(temp_root) / f"shrunk-{index}.jpg"
            replaced.write_bytes(b"small")
            return replaced

        planned = plan_image_segments([self._image(f"big-{index}.jpg") for index in range(2)])

        with materialize_thread_segments(planned, image_normalizer=fake_normalizer) as prepared:
            self.assertEqual(len(calls), 2)
            self.assertTrue(all("shrunk-" in path.name for path in prepared[0].upload_paths))

    def test_prepare_image_for_upload_passes_through_small_files(self) -> None:
        source = self._image("small.jpg")

        result = prepare_image_for_upload(source, self.tmp_path, 0, max_bytes=1024)

        self.assertEqual(result, source.resolve())

    def test_prepare_image_for_upload_shrinks_oversized_files(self) -> None:
        from PIL import Image

        source = self.tmp_path / "huge.jpg"
        Image.new("RGB", (3000, 4000), (12, 34, 56)).save(source, quality=95)
        # Pad it so the source is genuinely over the injected cap.
        source.write_bytes(source.read_bytes() + b"\0" * 200_000)

        result = prepare_image_for_upload(source, self.tmp_path, 0, max_bytes=64 * 1024)

        self.assertNotEqual(result, source.resolve())
        self.assertLessEqual(result.stat().st_size, 64 * 1024)
        self.assertEqual(result.suffix, ".jpg")

    def test_multi_file_order_is_flattened_deterministically(self) -> None:
        first = self._video("first.mp4")
        second = self._video("second.mp4")
        third = self._video("third.mp4")
        durations = {
            first.resolve(): 30.0,
            second.resolve(): 280.0,
            third.resolve(): 20.0,
        }

        planned = plan_thread_segments(
            [first, second, third],
            duration_reader=lambda path: durations[path.resolve()],
        )

        self.assertEqual(
            [(segment.source_index, segment.segment_index, segment.start_seconds) for segment in planned],
            [
                (0, 0, 0.0),
                (1, 0, 0.0),
                (1, 1, 139.0),
                (1, 2, 278.0),
                (2, 0, 0.0),
            ],
        )

    def test_temporary_split_artifacts_are_cleaned_up(self) -> None:
        video = self._video("cleanup.mp4")
        planned = plan_video_segments(
            video,
            duration_seconds=150.0,
            source_index=0,
        )

        def fake_splitter(segment: PlannedTwitterSegment, output_path: str | Path) -> Path:
            output = Path(output_path)
            output.write_bytes(
                f"{segment.start_seconds}:{segment.duration_seconds}".encode("utf-8")
            )
            return output

        with materialize_thread_segments(planned, splitter=fake_splitter) as prepared:
            split_paths = [segment.upload_path for segment in prepared if segment.requires_split]
            self.assertTrue(split_paths)
            temp_dir = split_paths[0].parent
            self.assertTrue(temp_dir.exists())
            self.assertTrue(all(path.exists() for path in split_paths))

        self.assertFalse(temp_dir.exists())

    def test_sequential_reply_chaining_uses_previous_post_id(self) -> None:
        prepared = [
            PreparedTwitterSegment(
                source_path=self._video("root.mp4"),
                upload_path=self._video("root.mp4"),
                source_index=0,
                segment_index=0,
                start_seconds=0.0,
                duration_seconds=30.0,
                requires_split=False,
            ),
            PreparedTwitterSegment(
                source_path=self._video("reply-1.mp4"),
                upload_path=self._video("reply-1.mp4"),
                source_index=0,
                segment_index=1,
                start_seconds=30.0,
                duration_seconds=30.0,
                requires_split=True,
            ),
            PreparedTwitterSegment(
                source_path=self._video("reply-2.mp4"),
                upload_path=self._video("reply-2.mp4"),
                source_index=1,
                segment_index=0,
                start_seconds=0.0,
                duration_seconds=20.0,
                requires_split=False,
            ),
        ]
        seen_previous_ids: list[str | None] = []

        async def publish_step(segment: PreparedTwitterSegment, previous_post_id: str | None) -> str:
            seen_previous_ids.append(previous_post_id)
            return f"post-{segment.source_index}-{segment.segment_index}"

        result = asyncio.run(publish_thread_segments(prepared, publish_step))

        self.assertEqual(seen_previous_ids, [None, "post-0-0", "post-0-1"])
        self.assertEqual(result, ["post-0-0", "post-0-1", "post-1-0"])

    def test_extract_created_post_id_prefers_create_tweet_branch(self) -> None:
        payload = {
            "data": {
                "viewer": {
                    "user_results": {
                        "result": {
                            "rest_id": "wrong-user-id",
                        }
                    }
                },
                "create_tweet": {
                    "tweet_results": {
                        "result": {
                            "rest_id": "correct-post-id",
                            "legacy": {
                                "id_str": "legacy-post-id",
                            },
                        }
                    }
                },
            }
        }

        self.assertEqual(extract_created_post_id(payload), "correct-post-id")


if __name__ == "__main__":
    unittest.main()
