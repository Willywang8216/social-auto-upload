"""Part selection must not drop the last part of an equal split.

An equal ffmpeg split gives its final part a slightly different duration (frame
boundary rounding): a 3-part Threads split produced 200.083s, 200.083s and
200.048s. The selector grouped parts by an *exact* duration match, so the 35ms
outlier was excluded and only 2 of 3 parts were ever published - a third of the
video silently never reached Threads.
"""

import importlib.util
import unittest
from pathlib import Path

from myUtils import platform_limits

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_selector():
    source = (REPO_ROOT / "sau_backend.py").read_text(encoding="utf-8")
    namespace = {"platform_limits": platform_limits}
    start = source.index("def _artifact_payloads_for_platform")
    end = source.index("def _read_json_body")
    exec(compile(source[start:end], "sau_backend.py", "exec"), namespace)
    return namespace


_SELECT = _load_selector()


def _video(artifact_id, *, part_index=None, part_count=None, seconds=None, split_for=None):
    metadata = {"role": "video"}
    if part_index is not None:
        metadata["part_index"] = part_index
    if part_count is not None:
        metadata["part_count"] = part_count
    if seconds is not None:
        metadata["max_duration_seconds"] = seconds
    if split_for is not None:
        metadata["split_for"] = split_for
    return {
        "id": artifact_id,
        "artifact_kind": "remote_upload",
        "public_url": "https://cdn.example/x.mp4",
        "metadata": metadata,
        "source_file_record_id": 1085,
    }


class SplitPartSelectionTests(unittest.TestCase):
    def test_last_part_with_a_slightly_different_duration_is_kept(self):
        items = [
            _video(1, part_index=1, part_count=3, seconds=200.083, split_for=["threads"]),
            _video(2, part_index=2, part_count=3, seconds=200.083, split_for=["threads"]),
            # 35ms shorter: still the same split, and must not be dropped.
            _video(3, part_index=3, part_count=3, seconds=200.048, split_for=["threads"]),
        ]
        selected = _SELECT["_select_videos_for_platform"](items, "threads")
        self.assertEqual([a["id"] for a in selected], [1, 2, 3])

    def test_returns_parts_in_order(self):
        items = [
            _video(3, part_index=3, part_count=3, seconds=200.0, split_for=["threads"]),
            _video(1, part_index=1, part_count=3, seconds=200.0, split_for=["threads"]),
            _video(2, part_index=2, part_count=3, seconds=199.9, split_for=["threads"]),
        ]
        selected = _SELECT["_select_videos_for_platform"](items, "threads")
        self.assertEqual([a["id"] for a in selected], [1, 2, 3])

    def test_the_larger_part_plan_wins(self):
        # A 140s X split and a 300s Threads split both exist; Threads must take
        # its own larger parts, not X's finer split.
        items = [
            _video(10, part_index=1, part_count=5, seconds=140.0, split_for=["twitter", "threads"]),
            _video(20, part_index=1, part_count=3, seconds=200.0, split_for=["threads"]),
            _video(21, part_index=2, part_count=3, seconds=200.0, split_for=["threads"]),
            _video(22, part_index=3, part_count=3, seconds=199.95, split_for=["threads"]),
        ]
        selected = _SELECT["_select_videos_for_platform"](items, "threads")
        self.assertEqual({a["id"] for a in selected}, {20, 21, 22})

    def test_untagged_parts_are_not_used_when_a_tagged_plan_fits(self):
        items = [
            _video(1, part_index=1, part_count=2, seconds=200.0, split_for=["threads"]),
            _video(2, part_index=2, part_count=2, seconds=200.0, split_for=["threads"]),
            _video(9, part_index=1, part_count=9, seconds=60.0, split_for=["twitter"]),
        ]
        selected = _SELECT["_select_videos_for_platform"](items, "threads")
        self.assertEqual({a["id"] for a in selected}, {1, 2})


if __name__ == "__main__":
    unittest.main()
