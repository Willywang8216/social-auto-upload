"""Regression tests for the offload fail-closed invariant.

A local file referenced by a live publish target (pending/running/retrying)
must NOT be deleted by the offload unless a verified restore mapping exists:
  - a file_records row with storage_key IS NOT NULL AND storage_backend_id IS NOT NULL, OR
  - a campaign_artifacts row with public_url IS NOT NULL OR remote_path IS NOT NULL.

Three scenarios:
  1. Live-target artifact WITH a mapping → purgeable (not in excludes)
  2. Live-target artifact WITHOUT a mapping → kept (in excludes)
  3. Far-future pending artifact → offloaded only WITH a mapping;
     without a mapping it stays local (in excludes)
"""

import os
import sqlite3
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
OFFLOAD_SH = REPO / "offload_to_drive.sh"


def _extract_pending_excludes_body() -> str:
    """Extract the Python heredoc body from pending_excludes() in the shell script."""
    source = OFFLOAD_SH.read_text()
    # The function is: pending_excludes() { docker exec -i ... <<'PY' ... PY }
    # We extract the Python between <<'PY' and PY
    marker = "pending_excludes() {"
    idx = source.index(marker)
    after = source[idx + len(marker):]
    # Find the heredoc start
    heredoc_start = after.index("<<'PY'") + len("<<'PY'")
    heredoc_end = after.index("\nPY\n", heredoc_start)
    return after[heredoc_start:heredoc_end]


def _build_harness(db_path: Path, local_src: Path) -> str:
    """Build a Python script that runs the pending_excludes logic against a fixture DB.

    The real pending_excludes() runs inside a Docker container against /app/db/database.db.
    For testing, we patch the DB path and the /app source root to point at fixtures.
    """
    body = _extract_pending_excludes_body()
    # Patch the DB path
    patched = body.replace(
        'conn = sqlite3.connect("/app/db/database.db")',
        f'conn = sqlite3.connect("{db_path}")',
    )
    # Patch /app/ references to the fixture source root.
    # The code uses "/app/" + root for os.walk and in path markers.
    patched = patched.replace(
        'base = "/app/" + root',
        f'base = "{local_src}/" + root',
    )
    # Also patch the marker strings used to extract relative paths from absolute ones.
    # The code checks for "/videoFile/", "/uploads/", "/generated/" in paths.
    # In the fixture, paths are like "{local_src}/generated/...", so we need to
    # ensure the markers still work. Actually, the markers are used to split paths,
    # and they look for "/videoFile/" etc. in the path string. Since our fixture
    # paths are like "{local_src}/generated/campaigns/clip.mp4", the marker
    # "/generated/" will still match. So no change needed here.
    return patched


def _bootstrap_schema(db_path: Path) -> None:
    """Create the minimal schema needed for pending_excludes()."""
    sys.path.insert(0, str(REPO))
    import db.createTable as create_table
    create_table.bootstrap(db_path)


class FailClosedTests(unittest.TestCase):
    """The offload must purge a live-target file only when a restore mapping exists."""

    def _run_pending_excludes(self, db_path: Path, src: Path) -> str:
        """Run the pending_excludes Python logic and return its stdout."""
        harness = _build_harness(db_path, src)
        result = subprocess.run(
            [sys.executable, "-c", harness],
            text=True,
            capture_output=True,
            env={**os.environ, "PYTHONPATH": str(REPO)},
        )
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}\nstdout: {result.stdout}")
        return result.stdout

    def _setup_common(self, tmp: Path):
        """Create a fixture DB and source tree with the needed tables and dirs."""
        db = tmp / "test.db"
        src = tmp / "src"
        for root in ("videoFile", "uploads", "generated"):
            (src / root).mkdir(parents=True)
        _bootstrap_schema(db)
        return db, src

    def test_live_target_with_mapping_is_purgeable(self):
        """A live-target artifact WITH a verified restore mapping is NOT excluded.

        The file has a file_records row with storage_key+storage_backend_id,
        so the worker can restore it. The offload may purge it.
        We use a pending target with a far-future schedule so it's not covered
        by the unconditional in-flight or due-soon exclusions.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db, src = self._setup_common(tmp)

            # Create a media file
            media = src / "generated" / "campaigns" / "clip_mapped.mp4"
            media.parent.mkdir(parents=True, exist_ok=True)
            media.write_bytes(b"mapped bytes")

            # Create a storage backend
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "INSERT INTO storage_backends (slug,label,provider,bucket,region,endpoint,access_key,secret_key,enabled) "
                    "VALUES ('test','T','rclone','drive','','sau/generated','','',1)"
                )
                backend_id = conn.execute(
                    "SELECT id FROM storage_backends WHERE endpoint='sau/generated'"
                ).fetchone()[0]

                # Create a file_records row WITH storage_key+storage_backend_id (verified mapping)
                conn.execute(
                    "INSERT INTO file_records (filename,filesize,file_path,storage_key,storage_backend_id) "
                    "VALUES (?,?,?,?,?)",
                    ("clip_mapped.mp4", media.stat().st_size, "generated/campaigns/clip_mapped.mp4",
                     "campaigns/clip_mapped.mp4", backend_id),
                )
                file_record_id = conn.execute("SELECT id FROM file_records").fetchone()[0]

                # Create a publish job + target referencing this file (status=pending, far-future)
                conn.execute(
                    "INSERT INTO publish_jobs (idempotency_key,platform,status,payload_json) VALUES (?,?,?,?)",
                    ("k1", "tiktok", "pending",
                     '{"artifacts":[{"local_path":"/app/generated/campaigns/clip_mapped.mp4",'
                     f'"source_file_record_id":{file_record_id}}}]}}'),
                )
                job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO publish_job_targets (job_id,account_ref,file_ref,status,schedule_at) VALUES (?,?,?,?,?)",
                    (job_id, "acct1", "campaign_post:1", "pending", "2099-06-01 00:00:00"),
                )

            output = self._run_pending_excludes(db, src)
            excludes = [line for line in output.splitlines() if line.startswith("/") and not line.startswith("#")]

            # The mapped file should NOT be in the excludes (it's purgeable)
            self.assertNotIn(
                "/campaigns/clip_mapped.mp4",
                excludes,
                "A live-target artifact WITH a verified restore mapping should be purgeable",
            )

    def test_live_target_without_mapping_is_kept(self):
        """A live-target artifact WITHOUT a verified restore mapping IS excluded.

        The file has a file_records row but NO storage_key/storage_backend_id,
        and no campaign_artifacts mapping. The worker cannot restore it, so
        the offload must keep it local.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db, src = self._setup_common(tmp)

            # Create a media file
            media = src / "generated" / "campaigns" / "clip_unmapped.mp4"
            media.parent.mkdir(parents=True, exist_ok=True)
            media.write_bytes(b"unmapped bytes")

            with sqlite3.connect(db) as conn:
                # Create a file_records row WITHOUT storage_key/storage_backend_id
                conn.execute(
                    "INSERT INTO file_records (filename,filesize,file_path) VALUES (?,?,?)",
                    ("clip_unmapped.mp4", media.stat().st_size, "generated/campaigns/clip_unmapped.mp4"),
                )
                file_record_id = conn.execute("SELECT id FROM file_records").fetchone()[0]

                # Create a publish job + target referencing this file (status=retrying)
                conn.execute(
                    "INSERT INTO publish_jobs (idempotency_key,platform,status,payload_json) VALUES (?,?,?,?)",
                    ("k2", "youtube", "retrying",
                     '{"artifacts":[{"local_path":"/app/generated/campaigns/clip_unmapped.mp4",'
                     f'"source_file_record_id":{file_record_id}}}]}}'),
                )
                job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO publish_job_targets (job_id,account_ref,file_ref,status) VALUES (?,?,?,?)",
                    (job_id, "acct2", "campaign_post:2", "retrying"),
                )

            output = self._run_pending_excludes(db, src)
            excludes = [line for line in output.splitlines() if line.startswith("/") and not line.startswith("#")]

            # The unmapped file MUST be in the excludes (kept local)
            self.assertIn(
                "/campaigns/clip_unmapped.mp4",
                excludes,
                "A live-target artifact WITHOUT a verified restore mapping must be kept local",
            )

    def test_far_future_pending_with_mapping_is_purgeable(self):
        """A far-future pending target's artifact WITH a mapping is purgeable.

        The file is not due soon, so it's not covered by the due-soon exclusion.
        But it HAS a verified restore mapping, so the worker can restore it.
        The offload may purge it.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db, src = self._setup_common(tmp)

            # Create a media file
            media = src / "generated" / "campaigns" / "clip_future_mapped.mp4"
            media.parent.mkdir(parents=True, exist_ok=True)
            media.write_bytes(b"future mapped bytes")

            with sqlite3.connect(db) as conn:
                conn.execute(
                    "INSERT INTO storage_backends (slug,label,provider,bucket,region,endpoint,access_key,secret_key,enabled) "
                    "VALUES ('test2','T','rclone','drive','','sau/generated','','',1)"
                )
                backend_id = conn.execute(
                    "SELECT id FROM storage_backends WHERE endpoint='sau/generated'"
                ).fetchone()[0]

                # Create a file_records row WITH storage_key+storage_backend_id
                conn.execute(
                    "INSERT INTO file_records (filename,filesize,file_path,storage_key,storage_backend_id) "
                    "VALUES (?,?,?,?,?)",
                    ("clip_future_mapped.mp4", media.stat().st_size,
                     "generated/campaigns/clip_future_mapped.mp4",
                     "campaigns/clip_future_mapped.mp4", backend_id),
                )
                file_record_id = conn.execute("SELECT id FROM file_records").fetchone()[0]

                # Create a publish job + target with a FAR-FUTURE schedule (year 2099)
                conn.execute(
                    "INSERT INTO publish_jobs (idempotency_key,platform,status,payload_json) VALUES (?,?,?,?)",
                    ("k3", "tiktok", "pending",
                     '{"artifacts":[{"local_path":"/app/generated/campaigns/clip_future_mapped.mp4",'
                     f'"source_file_record_id":{file_record_id}}}]}}'),
                )
                job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO publish_job_targets (job_id,account_ref,file_ref,status,schedule_at) VALUES (?,?,?,?,?)",
                    (job_id, "acct3", "campaign_post:3", "pending", "2099-12-31 23:59:59"),
                )

            output = self._run_pending_excludes(db, src)
            excludes = [line for line in output.splitlines() if line.startswith("/") and not line.startswith("#")]

            # The mapped far-future file should NOT be in the excludes (it's purgeable)
            self.assertNotIn(
                "/campaigns/clip_future_mapped.mp4",
                excludes,
                "A far-future pending artifact WITH a verified restore mapping should be purgeable",
            )

    def test_far_future_pending_without_mapping_is_kept(self):
        """A far-future pending target's artifact WITHOUT a mapping is kept local.

        The file is not due soon, and has no verified restore mapping.
        The worker cannot restore it, so the offload must keep it local.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db, src = self._setup_common(tmp)

            # Create a media file
            media = src / "generated" / "campaigns" / "clip_future_unmapped.mp4"
            media.parent.mkdir(parents=True, exist_ok=True)
            media.write_bytes(b"future unmapped bytes")

            with sqlite3.connect(db) as conn:
                # Create a file_records row WITHOUT storage_key/storage_backend_id
                conn.execute(
                    "INSERT INTO file_records (filename,filesize,file_path) VALUES (?,?,?)",
                    ("clip_future_unmapped.mp4", media.stat().st_size,
                     "generated/campaigns/clip_future_unmapped.mp4"),
                )
                file_record_id = conn.execute("SELECT id FROM file_records").fetchone()[0]

                # Create a publish job + target with a FAR-FUTURE schedule (year 2099)
                conn.execute(
                    "INSERT INTO publish_jobs (idempotency_key,platform,status,payload_json) VALUES (?,?,?,?)",
                    ("k4", "youtube", "pending",
                     '{"artifacts":[{"local_path":"/app/generated/campaigns/clip_future_unmapped.mp4",'
                     f'"source_file_record_id":{file_record_id}}}]}}'),
                )
                job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO publish_job_targets (job_id,account_ref,file_ref,status,schedule_at) VALUES (?,?,?,?,?)",
                    (job_id, "acct4", "campaign_post:4", "pending", "2099-12-31 23:59:59"),
                )

            output = self._run_pending_excludes(db, src)
            excludes = [line for line in output.splitlines() if line.startswith("/") and not line.startswith("#")]

            # The unmapped far-future file MUST be in the excludes (kept local)
            self.assertIn(
                "/campaigns/clip_future_unmapped.mp4",
                excludes,
                "A far-future pending artifact WITHOUT a verified restore mapping must be kept local",
            )

    def test_campaign_artifacts_public_url_counts_as_mapping(self):
        """A campaign_artifacts row with public_url counts as a verified mapping."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db, src = self._setup_common(tmp)

            media = src / "generated" / "campaigns" / "clip_public_url.mp4"
            media.parent.mkdir(parents=True, exist_ok=True)
            media.write_bytes(b"public url bytes")

            with sqlite3.connect(db) as conn:
                # file_records WITHOUT storage mapping
                conn.execute(
                    "INSERT INTO file_records (filename,filesize,file_path) VALUES (?,?,?)",
                    ("clip_public_url.mp4", media.stat().st_size,
                     "generated/campaigns/clip_public_url.mp4"),
                )
                file_record_id = conn.execute("SELECT id FROM file_records").fetchone()[0]

                # campaign_artifacts WITH public_url (verified mapping)
                # Need prerequisite rows: profiles, media_groups, campaigns
                conn.execute(
                    "INSERT INTO profiles (name, slug) VALUES (?, ?)",
                    ("test-profile", "test-profile"),
                )
                profile_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO media_groups (name) VALUES (?)",
                    ("test-group",),
                )
                mg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO campaigns (profile_id,media_group_id,status) VALUES (?,?,?)",
                    (profile_id, mg_id, "active"),
                )
                campaign_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO campaign_artifacts (campaign_id,source_file_record_id,artifact_kind,local_path,public_url) "
                    "VALUES (?,?,?,?,?)",
                    (campaign_id, file_record_id, "video",
                     "/app/generated/campaigns/clip_public_url.mp4",
                     "https://cdn.example.com/clip_public_url.mp4"),
                )

                # Live target referencing this file
                conn.execute(
                    "INSERT INTO publish_jobs (idempotency_key,platform,status,payload_json) VALUES (?,?,?,?)",
                    ("k5", "tiktok", "pending",
                     '{"artifacts":[{"local_path":"/app/generated/campaigns/clip_public_url.mp4",'
                     f'"source_file_record_id":{file_record_id}}}]}}'),
                )
                job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO publish_job_targets (job_id,account_ref,file_ref,status,schedule_at) VALUES (?,?,?,?,?)",
                    (job_id, "acct5", "campaign_post:5", "pending", "2099-06-15 12:00:00"),
                )

            output = self._run_pending_excludes(db, src)
            excludes = [line for line in output.splitlines() if line.startswith("/") and not line.startswith("#")]

            # The file has a campaign_artifacts public_url mapping → purgeable
            self.assertNotIn(
                "/campaigns/clip_public_url.mp4",
                excludes,
                "A campaign_artifacts public_url counts as a verified restore mapping",
            )


if __name__ == "__main__":
    unittest.main()
