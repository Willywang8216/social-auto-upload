"""Media type must come from the bytes when the name does not say.

The operator uploaded `The Anatomy of Connection – Episode 2: The Overlap
Myth__sfw` to the Drive inbox with **no extension**. The suffix-only classifier
called it neither video nor image, so prep produced no publishable artifact —
but the campaign was still marked `publishing` and all seven queued targets
failed with "media artifacts were supplied but none is a supported
image/video". A 420 MB video was discarded because of a missing ".mp4".
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from myUtils import campaign_media_prep


def _ffmpeg(*args: str) -> bool:
    if shutil.which("ffmpeg") is None:
        return False
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *args], capture_output=True
    ).returncode == 0


@unittest.skipUnless(shutil.which("ffprobe"), "ffprobe is required")
class MediaProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_known_suffix_never_probes(self):
        self.assertTrue(campaign_media_prep._is_video_file("clip.mp4"))
        self.assertTrue(campaign_media_prep._is_image_file("clip.jpg"))
        self.assertFalse(campaign_media_prep._is_video_file("clip.jpg"))

    def test_a_missing_file_is_unknown(self):
        self.assertIsNone(campaign_media_prep.probe_media_kind(self.root / "nope.mp4"))
        self.assertFalse(campaign_media_prep._is_video_file(self.root / "nope"))

    def test_an_extensionless_video_is_a_video(self):
        target = self.root / "The Anatomy of Connection – Episode 2__sfw"
        # -f mp4 is required: with no suffix ffmpeg cannot guess the muxer.
        if not _ffmpeg("-f", "lavfi", "-i", "testsrc=size=64x64:rate=5", "-t", "0.5",
                       "-pix_fmt", "yuv420p", "-f", "mp4", str(target)):
            self.skipTest("could not build a test video")
        self.assertEqual(campaign_media_prep.probe_media_kind(target), "video")
        self.assertTrue(campaign_media_prep._is_video_file(target))
        self.assertFalse(campaign_media_prep._is_image_file(target))

    def test_an_extensionless_image_is_an_image(self):
        target = self.root / "pride parade photo"
        if not _ffmpeg("-f", "lavfi", "-i", "color=c=red:size=32x32", "-frames:v", "1",
                       "-f", "image2", str(target)):
            self.skipTest("could not build a test image")
        self.assertEqual(campaign_media_prep.probe_media_kind(target), "image")
        self.assertTrue(campaign_media_prep._is_image_file(target))
        self.assertFalse(campaign_media_prep._is_video_file(target))

if __name__ == "__main__":
    unittest.main()
