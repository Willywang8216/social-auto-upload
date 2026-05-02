"""Tests for the rclone storage wrapper."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from myUtils import rclone_storage


class RcloneStorageTests(unittest.TestCase):
    def test_build_remote_path_uses_campaign_layout(self) -> None:
        with patch.dict(
            os.environ,
            {"SAU_DEFAULT_RCLONE_PATH": "Scripts-ssh-ssl-keys/SocialUpload"},
            clear=False,
        ):
            remote_path = rclone_storage.build_remote_path(
                "/tmp/clip.mp4",
                campaign_id=42,
                artifact_subdir="videos",
            )
        self.assertEqual(
            remote_path,
            "Scripts-ssh-ssl-keys/SocialUpload/campaigns/42/videos/clip.mp4",
        )

    def test_upload_artifact_runs_copy_and_link(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(command, **kwargs):
            calls.append(list(command))
            if command[1] == "link":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="https://public.example/link",
                    stderr="",
                )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp_dir, patch.dict(
            os.environ,
            {
                "SAU_DEFAULT_RCLONE_REMOTE": "Onedrive-Yahooforsub-Tao",
                "SAU_DEFAULT_RCLONE_PATH": "Scripts-ssh-ssl-keys/SocialUpload",
            },
            clear=False,
        ):
            source = Path(tmp_dir) / "clip.mp4"
            source.write_text("data")
            artifact = rclone_storage.upload_artifact(
                source,
                campaign_id=7,
                artifact_subdir="videos",
                runner=fake_runner,
            )

        self.assertEqual(calls[0][1], "copyto")
        self.assertEqual(calls[1][1], "link")
        self.assertEqual(artifact.public_url, "https://public.example/link")
        self.assertEqual(
            artifact.remote_spec,
            "Onedrive-Yahooforsub-Tao:Scripts-ssh-ssl-keys/SocialUpload/campaigns/7/videos/clip.mp4",
        )

    def test_public_url_template_bypasses_rclone_link(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(command, **kwargs):
            calls.append(list(command))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        url = rclone_storage.ensure_public_link(
            "folder/file.mp4",
            remote_name="drive",
            public_url_template="https://cdn.example/{remote_path}",
            campaign_id=12,
            runner=fake_runner,
        )
        self.assertEqual(url, "https://cdn.example/folder/file.mp4")
        self.assertEqual(calls, [])

    def test_invalid_request_raises_clear_error(self) -> None:
        def fake_runner(command, **kwargs):
            raise subprocess.CalledProcessError(
                1,
                command,
                output="",
                stderr="Invalid request",
            )

        with self.assertRaises(RuntimeError) as ctx:
            rclone_storage.ensure_public_link(
                "folder/file.mp4",
                remote_name="drive",
                runner=fake_runner,
            )
        self.assertIn("anonymous link creation failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
