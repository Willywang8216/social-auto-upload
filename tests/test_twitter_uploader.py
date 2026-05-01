from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from uploader.twitter_uploader.main import (
    PreparedTwitterSegment,
    PlannedTwitterSegment,
    TWITTER_MAX_VIDEO_SECONDS,
    TWITTER_SPLIT_SEGMENT_SECONDS,
    extract_created_post_id,
    materialize_thread_segments,
    plan_thread_segments,
    plan_video_segments,
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
