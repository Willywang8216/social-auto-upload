"""Tests for per-platform video-artifact selection + split grouping (sau_backend)."""

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


def _part(artifact_id: int, index: int, count: int, cap: int) -> dict:
    return _video(
        artifact_id,
        metadata={
            "role": "video",
            "max_duration_seconds": cap,
            "part_index": index,
            "part_count": count,
        },
    )


class SelectVideosForPlatformTests(unittest.TestCase):
    def test_full_artifact_is_used_when_no_variant_fits(self) -> None:
        items = [_video(1), _video(2, metadata={"role": "video", "max_duration_seconds": 600})]
        selected = sau_backend._select_videos_for_platform(items, "threads")
        self.assertEqual([a["id"] for a in selected], [1])

    def test_largest_fitting_single_variant_is_chosen(self) -> None:
        items = [
            _video(1),
            _video(2, metadata={"role": "video", "max_duration_seconds": 600}),
            _video(3, metadata={"role": "video", "max_duration_seconds": 140}),
        ]
        self.assertEqual(
            [a["id"] for a in sau_backend._select_videos_for_platform(items, "instagram")],
            [2],
        )
        self.assertEqual(
            [a["id"] for a in sau_backend._select_videos_for_platform(items, "twitter")],
            [3],
        )

    def test_uncapped_platform_uses_first_artifact(self) -> None:
        items = [_video(1), _video(2, metadata={"role": "video", "max_duration_seconds": 60})]
        selected = sau_backend._select_videos_for_platform(items, "unknown")
        self.assertEqual([a["id"] for a in selected], [1])

    def test_all_split_parts_are_returned_in_order(self) -> None:
        items = [_video(1), _part(10, 1, 3, 300), _part(11, 2, 3, 300), _part(12, 3, 3, 300)]
        selected = sau_backend._select_videos_for_platform(items, "threads")
        self.assertEqual([a["id"] for a in selected], [10, 11, 12])

    def test_only_the_largest_fitting_cap_bucket_is_used(self) -> None:
        items = [
            _video(1),
            _part(10, 1, 2, 140),
            _part(11, 2, 2, 140),
            _part(20, 1, 3, 300),
            _part(21, 2, 3, 300),
            _part(22, 3, 3, 300),
        ]
        # Threads cap 300s picks the 300s bucket, never mixing in the 140s one.
        self.assertEqual(
            [a["id"] for a in sau_backend._select_videos_for_platform(items, "threads")],
            [20, 21, 22],
        )
        # X cap 140s picks only the 140s bucket.
        self.assertEqual(
            [a["id"] for a in sau_backend._select_videos_for_platform(items, "twitter")],
            [10, 11],
        )


class ArtifactPartGroupsTests(unittest.TestCase):
    def test_split_video_becomes_one_group_per_part(self) -> None:
        artifacts = [
            _video(1),
            _part(10, 1, 3, 300),
            _part(11, 2, 3, 300),
            _part(12, 3, 3, 300),
            {
                "id": 4,
                "source_file_record_id": 7,
                "artifact_kind": "raw_remote_upload",
                "local_path": "/tmp/raw.mp4",
                "public_url": "https://cdn/raw.mp4",
                "metadata": {"role": "video"},
            },
        ]
        groups = sau_backend._artifact_part_groups_for_platform(artifacts, "threads")
        self.assertEqual(len(groups), 3)
        self.assertEqual([g[0]["metadata"]["part_index"] for g in groups], [1, 2, 3])
        for group in groups:
            self.assertNotIn(4, [a["id"] for a in group])  # raw artifact dropped

    def test_no_split_yields_a_single_group(self) -> None:
        artifacts = [_video(1)]
        groups = sau_backend._artifact_part_groups_for_platform(artifacts, "threads")
        self.assertEqual(len(groups), 1)
        self.assertEqual([a["id"] for a in groups[0]], [1])

    def test_payload_drops_raw_and_picks_videos_per_source(self) -> None:
        artifacts = [
            _video(1),
            _part(2, 1, 2, 300),
            _part(3, 2, 2, 300),
            {
                "id": 9,
                "source_file_record_id": 9,
                "artifact_kind": "remote_upload",
                "local_path": "/tmp/img.jpg",
                "public_url": "https://cdn/img.jpg",
                "metadata": {"role": "image"},
            },
        ]
        selected = sau_backend._artifact_payloads_for_platform(artifacts, "threads")
        ids = {a["id"] for a in selected}
        self.assertIn(9, ids)  # image passes through
        self.assertTrue(ids >= {2, 3})


if __name__ == "__main__":
    unittest.main()
