"""An extensionless upload must still produce a muxable output path.

The operator put `The Anatomy of Connection – Episode 2: The Overlap Myth__sfw`
on the Drive inbox with no extension. `prepare_campaign_artifact_path` copied
that (empty) suffix onto the output, and ffmpeg then refused to start:

    Unable to choose an output format for '...-watermarked_video';
    use a standard extension for the filename or specify the format manually.

exit 234 — which is how campaign 3005's prep died on 2026-10-29, after the
content probe had already correctly identified the file as a video.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from myUtils import media_pipeline


class ArtifactPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def _path(self, name: str, kind: str) -> Path:
        return media_pipeline.prepare_campaign_artifact_path(
            3005, f"/tmp/{name}", artifact_kind=kind, base_dir=self.base
        )

    def test_an_extensionless_video_output_gets_mp4(self):
        path = self._path("The Anatomy of Connection – Episode 2: The Overlap Myth__sfw",
                          "watermarked_video")
        self.assertEqual(path.suffix, ".mp4")
        self.assertIn("Episode 2", path.name)

    def test_an_extensionless_image_output_gets_jpg(self):
        self.assertEqual(self._path("pride parade photo", "watermarked_image").suffix, ".jpg")

    def test_a_real_suffix_is_preserved(self):
        self.assertEqual(self._path("clip.mov", "watermarked_video").suffix, ".mov")
        self.assertEqual(self._path("pic.png", "watermarked_image").suffix, ".png")

    def test_an_explicit_suffix_still_wins(self):
        path = media_pipeline.prepare_campaign_artifact_path(
            3005, "/tmp/whatever", artifact_kind="transcript", suffix=".txt", base_dir=self.base
        )
        self.assertEqual(path.suffix, ".txt")


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
class MuxerTests(unittest.TestCase):
    def test_ffmpeg_can_write_the_produced_path(self):
        """The point of the extension: ffmpeg actually accepts the output."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        source = base / "in.mp4"
        if subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=5",
             "-t", "0.4", "-pix_fmt", "yuv420p", str(source)], capture_output=True
        ).returncode != 0:
            self.skipTest("could not build a source clip")
        # stage it under an extensionless name, exactly like the Drive upload
        staged = base / "Episode 2__sfw"
        shutil.copyfile(source, staged)
        output = media_pipeline.prepare_campaign_artifact_path(
            3005, staged, artifact_kind="watermarked_video", base_dir=base
        )
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(staged), "-c", "copy", str(output)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-300:])
        self.assertGreater(output.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
