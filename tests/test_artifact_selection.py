"""Tests for per-platform video-artifact selection (sau_backend)."""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path


def _bootstrap_conf() -> None:
    if "conf" in sys.modules:
        return
    conf_module = types.ModuleType("conf")
    conf_module.BASE_DIR = str(Path(__file__).resolve().parent.parent)
    conf_module.DEBUG_MODE = True
    conf_module.LOCAL_CHROME_HEADLESS = True
    conf_module.LOCAL_CHROME_PATH = ""
    sys.modules["conf"] = conf_module


_bootstrap_conf()

import sau_backend  # noqa: E402


def _video(artifact_id: int, **overrides) -> dict:
    artifact = {
        "id": artifact_id,
        "source_file_record_id": 7,
        "artifact_kind": "remote_upload",
        "local_path": f"/tmp/v{artifact_id}.mp4",
        "public_url": f"https://cdn/v{artifact_id}.mp4",
        "metadata": {"role": "video"},
    }
    artifact.update(overrides)
    return artifact


class SelectVideoForPlatformTests(unittest.TestCase):
    def test_full_artifact_is_used_when_no_variant_fits(self) -> None:
        items = [_video(1), _video(2, metadata={"role": "video", "max_duration_seconds": 600})]
        # Threads caps at 300s: no variant fits -> fall back to the full artifact.
        self.assertEqual(sau_backend._select_video_for_platform(items, "threads")["id"], 1)

    def test_largest_fitting_variant_is_chosen(self) -> None:
        items = [
            _video(1),
            _video(2, metadata={"role": "video", "max_duration_seconds": 600}),
            _video(3, metadata={"role": "video", "max_duration_seconds": 140}),
        ]
        # Instagram caps at 900s: the 600s variant is the largest that fits.
        self.assertEqual(
            sau_backend._select_video_for_platform(items, "instagram")["id"], 2
        )
        # Twitter caps at 140s: only the 140s variant fits.
        self.assertEqual(
            sau_backend._select_video_for_platform(items, "twitter")["id"], 3
        )

    def test_uncapped_platform_uses_first_artifact(self) -> None:
        items = [_video(1), _video(2, metadata={"role": "video", "max_duration_seconds": 60})]
        self.assertEqual(sau_backend._select_video_for_platform(items, "unknown")["id"], 1)

    def test_payload_drops_raw_and_picks_one_video_per_source(self) -> None:
        artifacts = [
            _video(1),
            _video(2, metadata={"role": "video", "max_duration_seconds": 295}),
            {
                "id": 3,
                "source_file_record_id": 7,
                "artifact_kind": "raw_remote_upload",
                "local_path": "/tmp/raw.mp4",
                "public_url": "https://cdn/raw.mp4",
                "metadata": {"role": "video"},
            },
            {
                "id": 4,
                "source_file_record_id": 9,
                "artifact_kind": "remote_upload",
                "local_path": "/tmp/img.jpg",
                "public_url": "https://cdn/img.jpg",
                "metadata": {"role": "image"},
            },
        ]
        selected = sau_backend._artifact_payloads_for_platform(artifacts, "threads")
        ids = {a["id"] for a in selected}
        self.assertIn(2, ids)  # the 295s variant fits Threads
        self.assertIn(4, ids)  # images pass through
        self.assertNotIn(3, ids)  # raw artifact is dropped


if __name__ == "__main__":
    unittest.main()
