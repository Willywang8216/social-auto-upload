"""Tests for the media-processing helpers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

if "conf" not in sys.modules:
    conf_module = types.ModuleType("conf")
    conf_module.BASE_DIR = str(Path(__file__).resolve().parent.parent)
    conf_module.DEBUG_MODE = True
    conf_module.LOCAL_CHROME_HEADLESS = True
    conf_module.LOCAL_CHROME_PATH = ""
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


class WatermarkFontTests(unittest.TestCase):
    """CJK-capable font resolution and threading into the renderers.

    The slim runtime image only ships Latin fonts, so Chinese watermark text
    (e.g. Teaching's "威威教育") rendered as tofu. resolve_watermark_font()
    picks a CJK font and both renderers must actually use it.
    """

    def test_resolve_honours_env_when_file_exists(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".ttf") as fake_font:
            with mock.patch.dict(os.environ, {"SAU_WATERMARK_FONT": fake_font.name}):
                self.assertEqual(
                    media_pipeline.resolve_watermark_font(), fake_font.name
                )

    def test_resolve_ignores_env_when_missing_and_falls_back(self) -> None:
        with mock.patch.dict(os.environ, {"SAU_WATERMARK_FONT": "/no/such/font.ttf"}):
            with mock.patch("os.path.exists") as exists:
                # env path missing; first CJK candidate present.
                exists.side_effect = lambda p: p != "/no/such/font.ttf"
                resolved = media_pipeline.resolve_watermark_font()
                self.assertEqual(resolved, media_pipeline._CJK_FONT_CANDIDATES[0])

    def test_resolve_returns_none_when_nothing_exists(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "SAU_WATERMARK_FONT"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("os.path.exists", return_value=False):
                self.assertIsNone(media_pipeline.resolve_watermark_font())

    def test_drawtext_builders_embed_fontfile_when_given(self) -> None:
        timeline = media_pipeline.build_video_overlay_timeline(6, seed=1)
        static = media_pipeline._build_text_filter(
            "威威教育", timeline, opacity=0.2, fontsize=48, font_path="/f/noto.ttc"
        )
        moving = media_pipeline._build_moving_text_filter(
            "威威教育", 6.0, opacity=0.2, fontsize=48, font_path="/f/noto.ttc"
        )
        repeated = media_pipeline._build_repeated_text_filter(
            "威威教育", opacity=0.2, fontsize=48, font_path="/f/noto.ttc"
        )
        for chain in (static, moving, repeated):
            self.assertIn("fontfile='/f/noto.ttc'", chain)

    def test_drawtext_builders_omit_fontfile_when_absent(self) -> None:
        timeline = media_pipeline.build_video_overlay_timeline(6, seed=1)
        chain = media_pipeline._build_text_filter(
            "Brand", timeline, opacity=0.2, fontsize=48
        )
        self.assertNotIn("fontfile=", chain)

    def test_apply_video_watermark_injects_resolved_font(self) -> None:
        recorded: list[list[str]] = []

        def fake_runner(command, **kwargs):
            recorded.append(list(command))
            if command[0] == media_pipeline.FFPROBE_COMMAND:
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps({"format": {"duration": "9"}}), stderr=""
                )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with mock.patch.object(
            media_pipeline, "resolve_watermark_font", return_value="/f/noto.ttc"
        ):
            media_pipeline.apply_video_watermark(
                "/tmp/source.mp4",
                "/tmp/watermarked.mp4",
                watermark_text="威威教育",
                seed=11,
                runner=fake_runner,
            )
        vf = recorded[1][recorded[1].index("-vf") + 1]
        self.assertIn("fontfile='/f/noto.ttc'", vf)

    @unittest.skipUnless(Image is not None, "Pillow is not installed")
    def test_apply_image_watermark_accepts_cjk_with_font(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            source = Path(tmp_dir) / "source.png"
            output = Path(tmp_dir) / "out.png"
            Image.new("RGB", (320, 200), color="black").save(source)
            # No CJK font on the test host: fall back path must still succeed.
            result = media_pipeline.apply_image_watermark(
                source, output, watermark_text="威威教育", seed=5
            )
            self.assertEqual(result, output)
            self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
