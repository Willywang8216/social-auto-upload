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
    click_post_button,
    extract_created_post_id,
    is_topmost_at_center,
    materialize_thread_segments,
    plan_image_segments,
    plan_thread_segments,
    plan_video_segments,
    prepare_image_for_upload,
    publish_thread_segments,
    wait_for_ready_post_button,
)


class _FakeButton:
    """Minimal stand-in for a Playwright locator over one post button."""

    def __init__(self, *, visible: bool = True, enabled: bool = True, topmost: bool = True) -> None:
        self.visible = visible
        self.enabled = enabled
        self.topmost = topmost
        self.evaluations = 0

    async def is_visible(self) -> bool:
        return self.visible

    async def is_enabled(self) -> bool:
        return self.enabled

    async def evaluate(self, _script: str) -> bool:
        self.evaluations += 1
        return self.topmost


class _FakeCollection:
    def __init__(self, buttons: list[_FakeButton]) -> None:
        self.buttons = buttons

    async def count(self) -> int:
        return len(self.buttons)

    def nth(self, index: int) -> _FakeButton:
        return self.buttons[index]


class _FakePage:
    """Maps a selector fragment (``tweetButton``) to the buttons it matches."""

    def __init__(self, by_selector: dict[str, list[_FakeButton]]) -> None:
        self.by_selector = by_selector

    def locator(self, selector: str) -> _FakeCollection:
        for fragment, buttons in self.by_selector.items():
            if fragment in selector:
                return _FakeCollection(buttons)
        return _FakeCollection([])


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


class TwitterPostButtonSelectionTests(unittest.TestCase):
    """The composer sits in a modal whose mask animates over the dock, so a
    button that is merely visible+enabled can still be unclickable."""

    def _run(self, page: _FakePage):
        return asyncio.run(wait_for_ready_post_button(page, timeout_ms=2000, poll_interval_seconds=0.01))

    def test_returns_a_button_that_receives_the_click(self) -> None:
        button = _FakeButton(topmost=True)

        self.assertIs(self._run(_FakePage({"tweetButton": [button]})), button)

    def test_skips_a_covered_button_in_favour_of_the_clickable_one(self) -> None:
        covered = _FakeButton(topmost=False)
        clickable = _FakeButton(topmost=True)

        chosen = self._run(_FakePage({"tweetButton": [covered, clickable]}))

        self.assertIs(chosen, clickable)
        self.assertGreaterEqual(covered.evaluations, 1)

    def test_ignores_disabled_and_hidden_buttons(self) -> None:
        disabled = _FakeButton(enabled=False, topmost=True)
        hidden = _FakeButton(visible=False, topmost=True)
        ready = _FakeButton(topmost=True)

        chosen = self._run(_FakePage({"tweetButton": [disabled, hidden, ready]}))

        self.assertIs(chosen, ready)

    def test_falls_back_to_the_best_candidate_when_nothing_is_topmost(self) -> None:
        # Better to attempt the click than to fail the publish outright.
        covered = _FakeButton(topmost=False)

        chosen = self._run(_FakePage({"tweetButton": [covered]}))

        self.assertIs(chosen, covered)

    def test_raises_when_no_button_matches_at_all(self) -> None:
        with self.assertRaises(RuntimeError):
            self._run(_FakePage({}))


class TwitterTopmostHelperTests(unittest.TestCase):
    def test_false_when_the_hit_test_throws(self) -> None:
        class _Broken:
            async def evaluate(self, _script):
                raise RuntimeError("detached")

        self.assertFalse(asyncio.run(is_topmost_at_center(_Broken())))


class _ClickRecordingButton:
    def __init__(self, *, click_fails: bool = False, dispatch_fails: bool = False) -> None:
        self.click_fails = click_fails
        self.dispatch_fails = dispatch_fails
        self.clicked = 0
        self.dispatched = 0

    async def click(self, **_kwargs) -> None:
        self.clicked += 1
        if self.click_fails:
            raise RuntimeError("subtree intercepts pointer events")

    async def dispatch_event(self, _event: str) -> None:
        self.dispatched += 1
        if self.dispatch_fails:
            raise RuntimeError("detached")


class _KeyRecordingPage:
    def __init__(self) -> None:
        self.keys: list[str] = []

    class _Keyboard:
        def __init__(self, outer) -> None:
            self._outer = outer

        async def press(self, key: str) -> None:
            self._outer.keys.append(key)

    @property
    def keyboard(self):
        return _KeyRecordingPage._Keyboard(self)


class TwitterSubmitComposerTests(unittest.TestCase):
    """A long draft leaves the Post button under a fixed layer, so the plain
    click can fail hit-testing even though the button is enabled."""

    def _click(self, button, page):
        asyncio.run(click_post_button(page, button))

    def test_plain_click_is_used_when_it_works(self) -> None:
        button, page = _ClickRecordingButton(), _KeyRecordingPage()

        self._click(button, page)

        self.assertEqual((button.clicked, button.dispatched, page.keys), (1, 0, []))

    def test_falls_back_to_dispatching_when_the_click_is_blocked(self) -> None:
        button, page = _ClickRecordingButton(click_fails=True), _KeyRecordingPage()

        self._click(button, page)

        self.assertEqual((button.clicked, button.dispatched, page.keys), (1, 1, []))

    def test_falls_back_to_the_keyboard_shortcut_when_both_fail(self) -> None:
        button = _ClickRecordingButton(click_fails=True, dispatch_fails=True)
        page = _KeyRecordingPage()

        self._click(button, page)

        self.assertEqual(page.keys, ["Control+Enter"])


if __name__ == "__main__":
    unittest.main()
