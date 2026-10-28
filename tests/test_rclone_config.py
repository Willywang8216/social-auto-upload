"""rclone must be given a config file it can rewrite.

rclone saves its config when it refreshes an OAuth token, writing the temp file
next to the config (``<config><pid>``). The container mounts a single file at
``/app/rclone-cache.conf`` in the root-owned ``/app``, so the file was writable
but the directory was not:

    Failed to save config after 10 tries: failed to create temp file for new
    config: open /app/rclone-cache.conf409909120: permission denied

On 2026-10-29 that made the oldest inbox item fail with "Could not download
inbox media from Google Drive: ... permission denied" and burn a publish retry.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from myUtils import rclone_config


class WritableConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # A config inside a directory we cannot write to, like /app in the image.
        self.readonly_dir = self.root / "app"
        self.readonly_dir.mkdir()
        self.config = self.readonly_dir / "rclone-cache.conf"
        self.config.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")
        self.readonly_dir.chmod(0o555)
        self.addCleanup(self.readonly_dir.chmod, 0o755)
        self.home = self.root / "home"
        self.home.mkdir()

    def test_a_read_only_directory_gets_a_writable_copy(self):
        copied = rclone_config.writable_rclone_config(self.config, home=self.home)
        self.assertIsNotNone(copied)
        self.assertEqual(Path(copied).read_text(encoding="utf-8"),
                         self.config.read_text(encoding="utf-8"))
        self.assertTrue(os.access(Path(copied).parent, os.W_OK))
        self.assertEqual(oct(Path(copied).stat().st_mode & 0o777), "0o600")

    def test_an_already_writable_config_is_left_alone(self):
        writable = self.root / "rclone.conf"
        writable.write_text("[gdrive]\n", encoding="utf-8")
        self.assertIsNone(rclone_config.writable_rclone_config(writable, home=self.home))

    def test_no_config_configured_is_a_no_op(self):
        self.assertIsNone(rclone_config.writable_rclone_config("", home=self.home))
        self.assertIsNone(rclone_config.writable_rclone_config(self.root / "missing.conf", home=self.home))

    def test_the_copy_follows_the_source_when_it_changes(self):
        first = rclone_config.writable_rclone_config(self.config, home=self.home)
        os.utime(self.config, (1, 1))  # the mounted file is refreshed daily
        self.config.write_text("[gdrive]\ntype = drive\nnew = 1\n", encoding="utf-8")
        os.utime(self.config, None)
        second = rclone_config.writable_rclone_config(self.config, home=self.home)
        self.assertEqual(first, second)
        self.assertIn("new = 1", Path(second).read_text(encoding="utf-8"))

    def test_argv_gets_the_flag_only_when_it_is_needed(self):
        with patch.dict(os.environ, {"RCLONE_CONFIG": str(self.config), "HOME": str(self.home)}):
            argv = rclone_config.rclone_argv("copyto", "remote:a", "/tmp/a")
        self.assertEqual(argv[0], "rclone")
        self.assertEqual(argv[1], "--config")
        self.assertEqual(argv[3:], ["copyto", "remote:a", "/tmp/a"])

        plain = self.root / "rclone.conf"
        plain.write_text("[gdrive]\n", encoding="utf-8")
        with patch.dict(os.environ, {"RCLONE_CONFIG": str(plain), "HOME": str(self.home)}):
            self.assertEqual(rclone_config.rclone_argv("lsf", "remote:"), ["rclone", "lsf", "remote:"])

    def test_an_existing_argv_is_rewritten_in_place(self):
        with patch.dict(os.environ, {"RCLONE_CONFIG": str(self.config), "HOME": str(self.home)}):
            rewritten = rclone_config.with_writable_config(["rclone", "copyto", "a", "b"])
            already = rclone_config.with_writable_config(["rclone", "--config", "/x", "copyto", "a", "b"])
            not_rclone = rclone_config.with_writable_config(["ffmpeg", "-i", "a"])
        self.assertEqual(rewritten[0], "rclone")
        self.assertEqual(rewritten[1], "--config")
        self.assertEqual(rewritten[3:], ["copyto", "a", "b"])
        self.assertEqual(already, ["rclone", "--config", "/x", "copyto", "a", "b"])
        self.assertEqual(not_rclone, ["ffmpeg", "-i", "a"])


if __name__ == "__main__":
    unittest.main()
