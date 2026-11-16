"""Tests for the rclone storage wrapper."""

from __future__ import annotations

import os
import json
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

    def test_google_drive_link_omits_onedrive_flags(self) -> None:
        calls = []

        def fake_runner(command, **kwargs):
            calls.append(list(command))
            return subprocess.CompletedProcess(command, 0, stdout='https://drive.example/link', stderr='')

        url = rclone_storage.ensure_public_link('socialupload/file.mp4', remote_name='GDrive-willywang8216', runner=fake_runner)
        self.assertEqual(url, 'https://drive.example/link')
        self.assertEqual(calls[0], ['rclone', 'link', 'GDrive-willywang8216:socialupload/file.mp4'])


class RcloneDownloadTests(unittest.TestCase):
    """The offload cache needs a read side: media moved to Drive must be
    recoverable, not merely gone."""

    def test_download_artifact_copies_from_the_remote_root(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(command, **kwargs):
            Path(command[3]).write_bytes(b"video")
            calls.append(list(command))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "nested" / "clip.mp4"
            result = rclone_storage.download_artifact(
                "_batch/clip.mp4",
                destination,
                remote_name="GDrive-willywang8216",
                remote_root="sau/videoFile",
                runner=fake_runner,
            )

            self.assertEqual(result, destination)
            # The parent has to exist before rclone writes into it.
            self.assertTrue(destination.parent.is_dir())

        self.assertEqual(calls[0][:3], ["rclone", "copyto", "GDrive-willywang8216:sau/videoFile/_batch/clip.mp4"])
        temporary = Path(calls[0][3])
        self.assertEqual(temporary.parent, destination.parent)
        self.assertTrue(temporary.name.startswith(".clip.mp4."))
        self.assertTrue(temporary.name.endswith(".part"))
        self.assertIn("--contimeout", calls[0])
        self.assertIn("--timeout", calls[0])
        self.assertEqual(calls[0][calls[0].index("--retries") + 1], "1")

    def test_download_artifact_checks_nonempty_destination(self) -> None:
        def fake_runner(command, **kwargs):
            Path(command[3]).write_bytes(b"")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "clip.mp4"
            with self.assertRaisesRegex(RuntimeError, "empty"):
                rclone_storage.download_artifact(
                    "clip.mp4", destination, remote_name="drive", remote_root="",
                    runner=fake_runner,
                )
            self.assertFalse(destination.exists())

    def test_download_artifact_without_a_root_uses_the_key_as_given(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(command, **kwargs):
            Path(command[3]).write_bytes(b"video")
            calls.append(list(command))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            rclone_storage.download_artifact(
                "clip.mp4",
                Path(tmp) / "clip.mp4",
                remote_name="drive",
                remote_root="",
                runner=fake_runner,
            )

        self.assertEqual(calls[0][2], "drive:clip.mp4")

class RclonePacedReadTests(unittest.TestCase):
    def test_exact_stat_is_paced_once_with_unchanged_hard_default_deadline(self):
        calls = []
        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"Size": 123,
                "Hashes": {"SHA-256": "a" * 64, "MD5": "b" * 32}}))
        result = rclone_storage.stat_artifact("exact/part2.mp4", remote_name="selected-drive",
            remote_root="registered/root", runner=runner)
        self.assertEqual(len(calls), 1)
        command, options = calls[0]
        self.assertEqual(command[:5], ["rclone", "lsjson", "selected-drive:registered/root/exact/part2.mp4", "--stat", "--hash"])
        self.assertEqual(options["timeout"], 20)
        for flag, value in (("--retries", "1"), ("--low-level-retries", "1"), ("--tpslimit", "1"),
                            ("--tpslimit-burst", "1"), ("--drive-pacer-min-sleep", "2s")):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertNotIn("--drive-client-id", command)
        self.assertEqual(command[command.index("--drive-pacer-burst") + 1], "0")
        self.assertEqual(command[command.index("--timeout") + 1], "20s")
        self.assertEqual(result["remote_checksum"], {"algorithm": "sha256", "value": "a" * 64})

    def test_timeout_never_retries_or_exposes_partial_download(self):
        calls = []
        def runner(command, **kwargs):
            calls.append((command, kwargs))
            Path(command[3]).write_bytes(b"partial")
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "selected.mp4"
            destination.write_bytes(b"original")
            with self.assertRaises(subprocess.TimeoutExpired):
                rclone_storage.download_artifact("exact.mp4", destination, remote_name="same-client",
                    remote_root="", runner=runner, total_deadline=7)
            self.assertEqual(destination.read_bytes(), b"original")
            self.assertEqual(list(Path(directory).glob("*.part")), [])
            self.assertEqual(len(calls), 1)
            command, options = calls[0]
            self.assertLessEqual(options["timeout"], 7)
            self.assertEqual(command[command.index("--contimeout") + 1], f"{options['timeout']:g}s")
            self.assertEqual(command[command.index("--timeout") + 1], f"{options['timeout']:g}s")

    def test_invalid_or_exhausted_budgets_do_not_start_a_read(self):
        for budget in (0, -1, float("nan"), float("inf")):
            with self.subTest(budget=budget), patch.object(rclone_storage, "run_subprocess") as runner:
                with self.assertRaises(TimeoutError):
                    rclone_storage.stat_artifact("exact.mp4", remote_name="same-client", remote_root="",
                        runner=runner, total_deadline=budget)
                runner.assert_not_called()



if __name__ == "__main__":
    unittest.main()


def test_explicit_paced_stat_budget_is_still_hard_bounded():
    calls = []
    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"Size": 1, "Hashes": {}}))
    rclone_storage.stat_artifact("exact.mp4", remote_name="same-client", remote_root="",
                                 total_deadline=45, runner=runner)
    assert len(calls) == 1
    command, options = calls[0]
    assert options["timeout"] == 45
    assert command[command.index("--timeout") + 1] == "40s"
    assert command[command.index("--drive-pacer-burst") + 1] == "0"
