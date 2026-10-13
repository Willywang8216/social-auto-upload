"""Tests for media limits enforcement fixes.

Reproduces:
1. Threads 600s video reaching publisher unsplit (should be split to ≤300s parts)
2. Facebook 413 error from oversized file (should be split/compressed to ≤4096 MB)
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if "conf" not in sys.modules:
    import types as _types

    conf_module = _types.ModuleType("conf")
    conf_module.BASE_DIR = str(Path(__file__).resolve().parent.parent)
    conf_module.DEBUG_MODE = True
    conf_module.LOCAL_CHROME_HEADLESS = True
    conf_module.LOCAL_CHROME_PATH = ""
    sys.modules["conf"] = conf_module

from myUtils import media_prep
from myUtils import platform_limits


class ThreadsDurationCapTests(unittest.TestCase):
    """A 600s video must be split to ≤300s parts for Threads."""

    def test_split_count_for_600s_video_with_300s_cap(self) -> None:
        """600s video with 300s cap should split into 2 parts."""
        count = media_prep._split_count(
            duration=600.0,
            size_bytes=100 * 1024 * 1024,  # 100 MB (under size cap)
            max_seconds=300.0,
            max_bytes=None,
        )
        self.assertEqual(count, 2)

    def test_split_count_for_442s_video_with_300s_cap(self) -> None:
        """442s video with 300s cap should split into 2 parts."""
        count = media_prep._split_count(
            duration=442.0,
            size_bytes=100 * 1024 * 1024,
            max_seconds=300.0,
            max_bytes=None,
        )
        self.assertEqual(count, 2)

    def test_split_to_seconds_produces_parts_under_cap(self) -> None:
        """split_to_seconds must produce parts whose duration ≤ max_seconds."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "long_video.mp4"
            src.write_bytes(b"x")
            
            # Mock probe to return 600s duration
            meta = {
                "width": 1920,
                "height": 1080,
                "fps": 30,
                "duration": 600.0,
                "has_audio": True,
            }
            
            cmds = []

            def fake_run(cmd, **kwargs):
                cmds.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(media_prep, "probe", return_value=meta), mock.patch.object(
                media_prep, "_probe_size_bytes", return_value=100 * 1024 * 1024
            ), mock.patch.object(media_prep, "_ensure_tool", return_value="ffmpeg"), mock.patch.object(
                media_prep, "_run", side_effect=fake_run
            ):
                parts = media_prep.split_to_seconds(src, tmp, max_seconds=300.0)

            # Should produce 2 parts
            self.assertEqual(len(parts), 2)
            # Each part should be named correctly
            self.assertEqual(parts[0].name, "long_video_part1of2_pub.mp4")
            self.assertEqual(parts[1].name, "long_video_part2of2_pub.mp4")
            # Each ffmpeg command should have a -t flag for duration
            for cmd in cmds:
                self.assertIn("-t", cmd)
                # The duration should be approximately 300s (600/2)
                t_index = cmd.index("-t")
                duration_str = cmd[t_index + 1]
                duration = float(duration_str)
                self.assertLessEqual(duration, 300.0)

    def test_duration_probe_failure_does_not_skip_split(self) -> None:
        """When duration probe fails, split should still happen based on size or raise."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "video.mp4"
            src.write_bytes(b"x")
            
            # Simulate probe failure by raising an exception
            def failing_probe(path):
                raise RuntimeError("ffprobe failed")

            # When probe fails and size is over cap, split should still happen
            cmds = []

            def fake_run(cmd, **kwargs):
                cmds.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "", "")

            # Mock probe to fail on the first call (duration check) but succeed on split
            call_count = [0]
            def probe_side_effect(path):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise RuntimeError("ffprobe failed")
                return {
                    "width": 1920,
                    "height": 1080,
                    "fps": 30,
                    "duration": 600.0,  # Now it works
                    "has_audio": True,
                }

            with mock.patch.object(media_prep, "probe", side_effect=probe_side_effect), mock.patch.object(
                media_prep, "_probe_size_bytes", return_value=500 * 1024 * 1024  # 500 MB
            ), mock.patch.object(media_prep, "_ensure_tool", return_value="ffmpeg"), mock.patch.object(
                media_prep, "_run", side_effect=fake_run
            ):
                # This should either split or raise, not silently return the original
                try:
                    parts = media_prep.split_to_seconds(src, tmp, max_seconds=300.0, max_bytes=400 * 1024 * 1024)
                    # If it didn't raise, it should have split
                    self.assertGreater(len(parts), 1, "split_to_seconds should split when size is over cap")
                except RuntimeError:
                    # Raising is also acceptable - better than silently using the oversized file
                    pass


class FacebookSizeCapTests(unittest.TestCase):
    """Files over Facebook's 4096 MB cap must be split or compressed."""

    def test_split_count_for_oversized_file(self) -> None:
        """5000 MB file with 4096 MB cap should split into 2 parts."""
        count = media_prep._split_count(
            duration=600.0,  # Under duration cap
            size_bytes=5000 * 1024 * 1024,  # 5000 MB
            max_seconds=14460.0,  # Facebook duration cap
            max_bytes=4096 * 1024 * 1024,  # Facebook size cap
        )
        self.assertEqual(count, 2)

    def test_split_to_seconds_splits_on_size_overflow(self) -> None:
        """split_to_seconds must split when size exceeds max_bytes."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "large_video.mp4"
            src.write_bytes(b"x")
            
            meta = {
                "width": 1920,
                "height": 1080,
                "fps": 30,
                "duration": 600.0,  # Under duration cap
                "has_audio": True,
            }
            
            cmds = []

            def fake_run(cmd, **kwargs):
                cmds.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(media_prep, "probe", return_value=meta), mock.patch.object(
                media_prep, "_probe_size_bytes", return_value=5000 * 1024 * 1024  # 5000 MB
            ), mock.patch.object(media_prep, "_ensure_tool", return_value="ffmpeg"), mock.patch.object(
                media_prep, "_run", side_effect=fake_run
            ):
                parts = media_prep.split_to_seconds(
                    src,
                    tmp,
                    max_seconds=14460.0,  # Facebook duration cap
                    max_bytes=4096 * 1024 * 1024,  # Facebook size cap
                )

            # Should split into 2 parts based on size
            self.assertEqual(len(parts), 2)
            self.assertEqual(len(cmds), 2)

    def test_platform_limits_facebook_size_cap(self) -> None:
        """Facebook's media_max_mb should be 4096."""
        self.assertEqual(platform_limits.media_max_mb("facebook"), 4096)

    def test_platform_limits_threads_duration_cap(self) -> None:
        """Threads' video_max_seconds should be 300."""
        self.assertEqual(platform_limits.video_max_seconds("threads"), 300.0)


class PlatformCapsIntegrationTests(unittest.TestCase):
    """End-to-end: platform caps are enforced before publishing."""

    def test_threads_600s_video_is_split(self) -> None:
        """A 600s video targeting Threads must be split to ≤300s parts."""
        threads_cap = platform_limits.video_max_seconds("threads")
        self.assertEqual(threads_cap, 300.0)
        
        # Simulate the split logic from campaign_media_prep
        duration = 600.0
        count = media_prep._split_count(
            duration=duration,
            size_bytes=100 * 1024 * 1024,
            max_seconds=threads_cap,
            max_bytes=None,
        )
        
        # Should split into 2 parts
        self.assertEqual(count, 2)
        
        # Each part should be under the cap
        part_duration = duration / count
        self.assertLessEqual(part_duration, threads_cap)

    def test_facebook_5gb_video_is_split(self) -> None:
        """A 5 GB video targeting Facebook must be split to ≤4096 MB parts."""
        facebook_size_cap = platform_limits.media_max_mb("facebook")
        self.assertEqual(facebook_size_cap, 4096)
        
        # Simulate the split logic
        size_bytes = 5000 * 1024 * 1024  # 5000 MB
        count = media_prep._split_count(
            duration=600.0,
            size_bytes=size_bytes,
            max_seconds=platform_limits.video_max_seconds("facebook"),
            max_bytes=facebook_size_cap * 1_000_000,  # Convert to bytes
        )
        
        # Should split into 2 parts
        self.assertEqual(count, 2)
        
        # Each part should be under the cap
        part_size = size_bytes / count
        self.assertLessEqual(part_size, facebook_size_cap * 1_000_000)


if __name__ == "__main__":
    unittest.main()
