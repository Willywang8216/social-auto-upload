"""An over-cap video must fail once, not burn the whole retry budget.

A file's size and duration do not change between attempts, so a refusal is
deterministic. Recognising an over-cap file and then retrying it three times is
the worst of both: the operator sees a slow failure, and the alert arrives only
after every attempt is spent. Target #5626 (Threads, 600s against a 300s cap)
did exactly that with attempts=3.

``retryable=False`` still lets the Sociamonials fallback decide, which is where
a genuine re-route belongs.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from myUtils import prepared_publishers as pp  # noqa: E402


class OverCapIsNotRetryableTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.video = Path(self._tmp.name) / "clip.mp4"
        self.video.write_bytes(b"x" * 64)

    def tearDown(self):
        self._tmp.cleanup()

    def _sparse_video(self, size_bytes: int) -> Path:
        """A file that reports a large size without occupying disk."""
        big = Path(self._tmp.name) / "big.mp4"
        with open(big, "wb") as handle:
            handle.truncate(size_bytes)
        return big

    def test_size_cap_is_not_retryable(self):
        big = self._sparse_video(4_000_000_000)
        with self.assertRaises(pp.PreparedPublishError) as ctx:
            pp._enforce_video_limits(str(big), "twitter")
        self.assertIs(ctx.exception.retryable, False)
        self.assertIn("MB", str(ctx.exception))

    def test_duration_cap_is_not_retryable(self):
        with patch.object(pp.media_pipeline, "probe_video_duration", return_value=9999.0):
            with self.assertRaises(pp.PreparedPublishError) as ctx:
                pp._enforce_video_limits(str(self.video), "twitter")
        self.assertIs(ctx.exception.retryable, False)
        self.assertIn("duration", str(ctx.exception))

    def test_threads_duration_cap_is_not_retryable(self):
        with patch.object(pp.media_pipeline, "probe_video_duration", return_value=600.0):
            with self.assertRaises(pp.PreparedPublishError) as ctx:
                pp._validate_threads_video_artifact(
                    {"local_path": str(self.video), "public_url": ""}
                )
        self.assertIs(ctx.exception.retryable, False)
        self.assertIn("Threads", str(ctx.exception))

    def test_threads_size_cap_is_not_retryable(self):
        big = self._sparse_video(4_000_000_000)
        with self.assertRaises(pp.PreparedPublishError) as ctx:
            pp._validate_threads_video_artifact(
                {"local_path": str(big), "public_url": ""}
            )
        self.assertIs(ctx.exception.retryable, False)

    def test_a_file_within_caps_does_not_raise(self):
        # The guard must not become a blanket refusal.
        with patch.object(pp.media_pipeline, "probe_video_duration", return_value=10.0):
            pp._enforce_video_limits(str(self.video), "twitter")

    def test_a_missing_file_is_not_a_rejection(self):
        # Best-effort by design: an absent local file (offloaded, remote-only)
        # must not raise, because the publisher may still fetch it.
        pp._enforce_video_limits("/nonexistent/clip.mp4", "twitter")


if __name__ == "__main__":
    unittest.main()
