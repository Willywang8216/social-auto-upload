from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from myUtils import inbox_drive


class InboxDriveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base_patch = patch.object(inbox_drive, "BASE_DIR", self.root)
        self.base_patch.start()

    def tearDown(self):
        self.base_patch.stop()
        self.tmp.cleanup()

    def test_remote_path_is_confined_to_persona_media_dirs(self):
        self.assertEqual(inbox_drive.validate_remote_path("both/video/a clip.mp4"), "both/video/a clip.mp4")
        for path in ("../secret.mp4", "/nw/video/a.mp4", "nw/other/a.mp4", "nw/video/../../x.mp4"):
            with self.subTest(path=path), self.assertRaises(inbox_drive.InboxDriveError):
                inbox_drive.validate_remote_path(path)

    def test_legacy_source_path_must_be_under_known_inbox(self):
        self.assertEqual(
            inbox_drive.remote_path_for_item({"sourcePath": "/home/will/sau-inbox/sw/video/a.mp4"}),
            "sw/video/a.mp4",
        )
        with self.assertRaises(inbox_drive.InboxDriveError):
            inbox_drive.remote_path_for_item({"sourcePath": "/tmp/untrusted.mp4"})

    def test_download_stages_to_managed_relative_path(self):
        def runner(command, **kwargs):
            Path(command[-1]).write_bytes(b"video bytes")

        staged = inbox_drive.stage_remote_media("both/video/clip.mp4", runner=runner)
        self.assertTrue(staged.startswith("_inbox_cache/"))
        self.assertEqual((self.root / "videoFile" / staged).read_bytes(), b"video bytes")

    def test_download_failure_removes_partial_file(self):
        destination = self.root / "videoFile" / "_inbox_cache"

        def runner(command, **kwargs):
            Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(command[-1]).write_bytes(b"partial")
            raise RuntimeError("failed")

        with self.assertRaises(inbox_drive.InboxDriveError):
            inbox_drive.stage_remote_media("both/video/clip.mp4", runner=runner)
        self.assertEqual(list(destination.glob("*")), [])


if __name__ == "__main__":
    unittest.main()
