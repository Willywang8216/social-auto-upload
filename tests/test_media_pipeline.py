"""Tests for the media-processing helpers."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

if "conf" not in sys.modules:
    conf_module = types.ModuleType("conf")
    conf_module.BASE_DIR = str(Path(__file__).resolve().parent.parent)
    sys.modules["conf"] = conf_module

from myUtils import media_pipeline

try:
    from PIL import Image
except ImportError:  # pragma: no cover - environment-specific
    Image = None


class MediaPipelineTests(unittest.TestCase):
    def test_build_video_overlay_timeline_is_deterministic(self) -> None:
        first = media_pipeline.build_video_overlay_timeline(12, seed=7)
        second = media_pipeline.build_video_overlay_timeline(12, seed=7)
        self.assertEqual(first, second)
        self.assertEqual(first[0].start_seconds, 0)
        self.assertEqual(first[-1].end_seconds, 12)
        self.assertTrue(all(1 <= (slot.end_seconds - slot.start_seconds) <= 5 for slot in first))

    def test_extract_video_audio_builds_expected_ffmpeg_command(self) -> None:
        recorded: list[list[str]] = []

        def fake_runner(command, **kwargs):
            recorded.append(list(command))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        output = media_pipeline.extract_video_audio(
            "/tmp/source.mp4",
            "/tmp/output.wav",
            runner=fake_runner,
        )
        self.assertEqual(output, Path("/tmp/output.wav"))
        self.assertEqual(recorded[0][:4], ["ffmpeg", "-y", "-i", str(Path("/tmp/source.mp4").resolve())])
        self.assertIn("-vn", recorded[0])
        self.assertIn("16000", recorded[0])

    def test_apply_video_watermark_returns_command_and_timeline(self) -> None:
        recorded: list[list[str]] = []

        def fake_runner(command, **kwargs):
            recorded.append(list(command))
            if command[0] == media_pipeline.FFPROBE_COMMAND:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps({"format": {"duration": "9"}}),
                    stderr="",
                )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        plan = media_pipeline.apply_video_watermark(
            "/tmp/source.mp4",
            "/tmp/watermarked.mp4",
            watermark_text="Brand",
            seed=11,
            runner=fake_runner,
        )
        self.assertEqual(plan.output_path, Path("/tmp/watermarked.mp4"))
        self.assertTrue(plan.timeline)
        self.assertEqual(recorded[0][0], media_pipeline.FFPROBE_COMMAND)
        self.assertEqual(recorded[1][0], media_pipeline.FFMPEG_COMMAND)
        self.assertIn("-vf", recorded[1])
        self.assertTrue(any("drawtext=" in part for part in recorded[1]))

    @unittest.skipUnless(Image is not None, "Pillow is not installed")
    def test_apply_image_watermark_creates_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            source = Path(tmp_dir) / "source.png"
            output = Path(tmp_dir) / "out.png"
            Image.new("RGB", (320, 200), color="black").save(source)

            result = media_pipeline.apply_image_watermark(
                source,
                output,
                watermark_text="Brand",
                seed=5,
            )
            self.assertEqual(result, output)
            self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
