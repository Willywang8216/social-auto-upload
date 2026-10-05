from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


class ReconcileGeneratedDriveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "db.sqlite"
        self.source = self.root / "source"
        (self.source / "generated").mkdir(parents=True)
        self.remote = self.root / "bin" / "rclone"
        self.remote.parent.mkdir()
        self.remote.write_text('#!/usr/bin/env sh\nprintf "%s\\n" "$REMOTE_LIST"\n')
        self.remote.chmod(0o755)
        self.script = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_generated_drive.py"
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE storage_backends (id INTEGER PRIMARY KEY, slug TEXT UNIQUE, label TEXT, provider TEXT, bucket TEXT, region TEXT, endpoint TEXT, access_key TEXT, secret_key TEXT, cdn_url TEXT, is_default INTEGER, enabled INTEGER)")
            conn.execute("CREATE TABLE file_records (id INTEGER PRIMARY KEY, filename TEXT, file_path TEXT, filesize INTEGER, storage_key TEXT, storage_backend_id INTEGER)")
            conn.execute("INSERT INTO file_records VALUES (1,'clip.mp4','generated/campaigns/campaign-1/clip.mp4',4,NULL,NULL)")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, remote_list: str, *, apply: bool = False):
        env = {
            **__import__("os").environ,
            "PATH": f"{self.remote.parent}:{__import__('os').environ['PATH']}",
            "REMOTE_LIST": remote_list,
        }
        command = [
            "python3", str(self.script), "--db", str(self.db),
            "--rclone-config", str(self.root / "rclone.conf"),
            "--source-root", str(self.source),
        ]
        if apply:
            command.append("--apply")
        return subprocess.run(command, env=env, text=True, capture_output=True)

    def test_dry_run_reports_verified_match_without_writing(self):
        result = self._run("campaigns/campaign-1/clip.mp4;4")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"exact_path_and_size_matches": 1', result.stdout)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM storage_backends").fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT storage_key FROM file_records").fetchone()[0])

    def test_size_mismatch_is_unresolved_and_refuses_apply(self):
        result = self._run("campaigns/campaign-1/clip.mp4;5", apply=True)
        self.assertEqual(result.returncode, 3)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM storage_backends").fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT storage_key FROM file_records").fetchone()[0])

    def test_apply_registers_exact_verified_key_without_credentials(self):
        result = self._run("campaigns/campaign-1/clip.mp4;4", apply=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT storage_key,storage_backend_id FROM file_records").fetchone(), ("campaigns/campaign-1/clip.mp4", 1))
            backend = conn.execute("SELECT provider,bucket,endpoint,access_key,secret_key FROM storage_backends").fetchone()
        self.assertEqual(backend, ("rclone", "GDrive-willywang8216", "sau/generated", "", ""))


if __name__ == "__main__":
    unittest.main()
