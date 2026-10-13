"""Tests for artifact registration and the repair script.

Covers:
- ``add_campaign_artifact`` unconditionally creates a ``file_records`` row when
  ``local_path`` is set (idempotent).
- ``storage_backend_id`` is persisted on ``campaign_artifacts`` when supplied.
- The repair script classifies artifacts as mapped / registered_local /
  registered_drive / unrecoverable correctly.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db.createTable as create_table
from myUtils import campaigns as campaign_store
from myUtils import profiles as profile_registry


class _DbTestBase(unittest.TestCase):
    """Shared bootstrap: temp DB with schema, one profile, one media group."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        create_table.bootstrap(self.db_path)
        self.profile_id = profile_registry.create_profile(
            "TestProfile", db_path=self.db_path
        ).id
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "INSERT INTO media_groups (id, name, notes, status) "
                "VALUES (1, 'g', '', 'ready')"
            )
            conn.commit()
        finally:
            conn.close()
        self.campaign = campaign_store.create_campaign(
            profile_id=self.profile_id, media_group_id=1, db_path=self.db_path
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn


class RegistrationTests(_DbTestBase):
    """add_campaign_artifact must register a file_records row unconditionally."""

    def test_local_path_always_creates_file_record(self):
        """Even when the file does not exist on disk, a row is created."""
        artifact = campaign_store.add_campaign_artifact(
            self.campaign.id,
            artifact_kind="local",
            local_path="/app/generated/campaigns/campaign-1/video_pub.mp4",
            db_path=self.db_path,
        )
        self.assertIsNotNone(artifact.source_file_record_id)
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT id, file_path, filename FROM file_records WHERE id = ?",
                (artifact.source_file_record_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(
                row["file_path"],
                "generated/campaigns/campaign-1/video_pub.mp4",
            )
        finally:
            conn.close()

    def test_registration_is_idempotent(self):
        """Two artifacts for the same path reuse the same file_records row."""
        a1 = campaign_store.add_campaign_artifact(
            self.campaign.id,
            artifact_kind="local",
            local_path="/app/generated/campaigns/campaign-1/video_pub.mp4",
            db_path=self.db_path,
        )
        a2 = campaign_store.add_campaign_artifact(
            self.campaign.id,
            artifact_kind="watermarked_video",
            local_path="/app/generated/campaigns/campaign-1/video_pub.mp4",
            db_path=self.db_path,
        )
        self.assertEqual(a1.source_file_record_id, a2.source_file_record_id)
        conn = self._conn()
        try:
            count = conn.execute("SELECT COUNT(*) FROM file_records").fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            conn.close()

    def test_existing_file_record_with_storage_mapping_is_preserved(self):
        """If the offloader already wrote storage info, registration must not clobber it."""
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO file_records (filename, filesize, file_path, "
                "storage_key, storage_backend_id) VALUES (?, ?, ?, ?, ?)",
                ("video_pub.mp4", 100.0,
                 "generated/campaigns/campaign-1/video_pub.mp4",
                 "campaigns/campaign-1/video_pub.mp4", 42),
            )
            conn.commit()
            existing_id = conn.execute("SELECT id FROM file_records").fetchone()[0]
        finally:
            conn.close()

        artifact = campaign_store.add_campaign_artifact(
            self.campaign.id,
            artifact_kind="local",
            local_path="/app/generated/campaigns/campaign-1/video_pub.mp4",
            db_path=self.db_path,
        )
        self.assertEqual(artifact.source_file_record_id, existing_id)
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT storage_key, storage_backend_id FROM file_records WHERE id = ?",
                (existing_id,),
            ).fetchone()
            self.assertEqual(row["storage_key"], "campaigns/campaign-1/video_pub.mp4")
            self.assertEqual(row["storage_backend_id"], 42)
        finally:
            conn.close()

    def test_no_local_path_creates_no_file_record(self):
        """Remote-only artifacts (public_url only) need no file_records row."""
        campaign_store.add_campaign_artifact(
            self.campaign.id,
            artifact_kind="remote_upload",
            public_url="https://cdn.example/video.mp4",
            remote_path="campaigns/campaign-1/video.mp4",
            db_path=self.db_path,
        )
        conn = self._conn()
        try:
            count = conn.execute("SELECT COUNT(*) FROM file_records").fetchone()[0]
            self.assertEqual(count, 0)
        finally:
            conn.close()


class StorageBackendIdTests(_DbTestBase):
    """storage_backend_id is persisted on campaign_artifacts when supplied."""

    def _insert_backend(self, backend_id: int = 7):
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO storage_backends (id, slug, label, provider, bucket, "
                "region, endpoint, access_key, secret_key) "
                "VALUES (?, 'gdrive', 'GDrive', 'rclone', 'GDrive-test', '', '', '', '')",
                (backend_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def test_storage_backend_id_is_written(self):
        self._insert_backend(7)
        artifact = campaign_store.add_campaign_artifact(
            self.campaign.id,
            artifact_kind="remote_upload",
            local_path="/app/generated/campaigns/campaign-1/video_pub.mp4",
            public_url="https://cdn.example/video.mp4",
            remote_path="campaigns/campaign-1/video_pub.mp4",
            storage_backend_id=7,
            db_path=self.db_path,
        )
        self.assertEqual(artifact.storage_backend_id, 7)
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT storage_backend_id FROM campaign_artifacts WHERE id = ?",
                (artifact.id,),
            ).fetchone()
            self.assertEqual(row["storage_backend_id"], 7)
        finally:
            conn.close()

    def test_storage_backend_id_defaults_to_none(self):
        artifact = campaign_store.add_campaign_artifact(
            self.campaign.id,
            artifact_kind="local",
            local_path="/app/generated/campaigns/campaign-1/video_pub.mp4",
            db_path=self.db_path,
        )
        self.assertIsNone(artifact.storage_backend_id)


class UpdateMappingTests(_DbTestBase):
    """update_campaign_artifact_mapping backfills restore fields."""

    def _insert_backend(self, backend_id: int = 5):
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO storage_backends (id, slug, label, provider, bucket, "
                "region, endpoint, access_key, secret_key) "
                "VALUES (?, 'gdrive', 'GDrive', 'rclone', 'GDrive-test', '', '', '', '')",
                (backend_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def test_sets_remote_path_and_backend(self):
        self._insert_backend(5)
        artifact = campaign_store.add_campaign_artifact(
            self.campaign.id,
            artifact_kind="local",
            local_path="/app/generated/campaigns/campaign-1/video_pub.mp4",
            db_path=self.db_path,
        )
        self.assertIsNone(artifact.remote_path)
        updated = campaign_store.update_campaign_artifact_mapping(
            artifact.id,
            remote_path="campaigns/campaign-1/video_pub.mp4",
            storage_backend_id=5,
            db_path=self.db_path,
        )
        self.assertEqual(updated.remote_path, "campaigns/campaign-1/video_pub.mp4")
        self.assertEqual(updated.storage_backend_id, 5)


class ListUnmappedTests(_DbTestBase):
    """list_artifacts_without_mapping finds the right rows."""

    def test_finds_unmapped_artifact(self):
        """An artifact with local_path but no mapping is listed."""
        campaign_store.add_campaign_artifact(
            self.campaign.id,
            artifact_kind="local",
            local_path="/app/generated/campaigns/campaign-1/video_pub.mp4",
            db_path=self.db_path,
        )
        unmapped = campaign_store.list_artifacts_without_mapping(db_path=self.db_path)
        self.assertEqual(len(unmapped), 1)
        self.assertEqual(
            unmapped[0].local_path,
            "/app/generated/campaigns/campaign-1/video_pub.mp4",
        )

    def test_excludes_mapped_artifact(self):
        """An artifact with public_url is not listed as unmapped."""
        campaign_store.add_campaign_artifact(
            self.campaign.id,
            artifact_kind="remote_upload",
            local_path="/app/generated/campaigns/campaign-1/video_pub.mp4",
            public_url="https://cdn.example/video.mp4",
            db_path=self.db_path,
        )
        unmapped = campaign_store.list_artifacts_without_mapping(db_path=self.db_path)
        self.assertEqual(len(unmapped), 0)

    def test_excludes_artifact_with_file_record_mapping(self):
        """An artifact whose source_file_record has storage mapping is not unmapped."""
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO file_records (filename, filesize, file_path, "
                "storage_key, storage_backend_id) VALUES (?, ?, ?, ?, ?)",
                ("video_pub.mp4", 100.0,
                 "generated/campaigns/campaign-1/video_pub.mp4",
                 "campaigns/campaign-1/video_pub.mp4", 42),
            )
            conn.commit()
            fr_id = conn.execute("SELECT id FROM file_records").fetchone()[0]
        finally:
            conn.close()
        campaign_store.add_campaign_artifact(
            self.campaign.id,
            artifact_kind="local",
            source_file_record_id=fr_id,
            local_path="/app/generated/campaigns/campaign-1/video_pub.mp4",
            db_path=self.db_path,
        )
        unmapped = campaign_store.list_artifacts_without_mapping(db_path=self.db_path)
        self.assertEqual(len(unmapped), 0)


class RepairScriptClassificationTests(_DbTestBase):
    """The repair script correctly classifies artifacts."""

    def _insert_backend(self, backend_id: int = 10):
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO storage_backends (id, slug, label, provider, bucket, "
                "region, endpoint, access_key, secret_key) "
                "VALUES (?, 'gdrive', 'GDrive', 'rclone', 'GDrive-test', '', '', '', '')",
                (backend_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def _insert_job_and_target(self, payload: dict, status: str = "pending"):
        conn = self._conn()
        try:
            payload_json = json.dumps(payload, sort_keys=True)
            conn.execute(
                "INSERT INTO publish_jobs (idempotency_key, platform, payload_json, status, total_targets) "
                "VALUES (?, 'test', ?, 'active', 1)",
                (f"test-key-{id(payload)}", payload_json),
            )
            job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO publish_job_targets (job_id, account_ref, file_ref, status) "
                "VALUES (?, 'account:1', 'campaign_post:1', ?)",
                (job_id, status),
            )
            target_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
            return job_id, target_id
        finally:
            conn.close()

    def test_mapped_artifact_is_not_repaired(self):
        """An artifact with public_url is classified as 'mapped'."""
        artifact = campaign_store.add_campaign_artifact(
            self.campaign.id,
            artifact_kind="local",
            local_path="/app/generated/campaigns/campaign-1/video_pub.mp4",
            public_url="https://cdn.example/video.mp4",
            db_path=self.db_path,
        )
        payload = {"artifacts": [artifact.to_dict()]}
        self._insert_job_and_target(payload)

        from scripts.repair_missing_artifacts import diagnose_targets

        conn = self._conn()
        try:
            results = diagnose_targets(conn)
            # The target has only mapped artifacts, so it should not appear.
            self.assertEqual(len(results), 0)
        finally:
            conn.close()

    def test_unrecoverable_when_no_bytes_anywhere(self):
        """An artifact with no mapping and no bytes is 'unrecoverable'."""
        artifact = campaign_store.add_campaign_artifact(
            self.campaign.id,
            artifact_kind="local",
            local_path="/nonexistent/path/video_pub.mp4",
            db_path=self.db_path,
        )
        payload = {"artifacts": [artifact.to_dict()]}
        self._insert_job_and_target(payload)

        from scripts.repair_missing_artifacts import diagnose_targets

        conn = self._conn()
        try:
            results = diagnose_targets(conn)
            self.assertEqual(len(results), 1)
            diags = results[0].diagnoses
            unmapped = [d for d in diags if d.classification != "mapped"]
            self.assertEqual(len(unmapped), 1)
            self.assertEqual(unmapped[0].classification, "unrecoverable")
        finally:
            conn.close()

    def test_local_bytes_are_classified_as_registered(self):
        """An artifact whose bytes exist locally is 'registered_local'."""
        # Create a real file in the temp directory
        local_file = Path(self._tmp.name) / "video_pub.mp4"
        local_file.write_bytes(b"\x00" * 100)

        artifact = campaign_store.add_campaign_artifact(
            self.campaign.id,
            artifact_kind="local",
            local_path=str(local_file),
            db_path=self.db_path,
        )
        payload = {"artifacts": [artifact.to_dict()]}
        self._insert_job_and_target(payload)

        from scripts.repair_missing_artifacts import diagnose_targets

        conn = self._conn()
        try:
            results = diagnose_targets(conn)
            self.assertEqual(len(results), 1)
            diags = results[0].diagnoses
            unmapped = [d for d in diags if d.classification != "mapped"]
            self.assertEqual(len(unmapped), 1)
            self.assertEqual(unmapped[0].classification, "registered_local")
        finally:
            conn.close()

    def test_drive_bytes_are_classified_as_registered(self):
        """An artifact with a file_records storage_key is 'registered_drive'."""
        self._insert_backend()
        # Insert a file_records row with storage mapping
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO file_records (filename, filesize, file_path, "
                "storage_key, storage_backend_id) VALUES (?, ?, ?, ?, ?)",
                ("video_pub.mp4", 100.0,
                 "generated/campaigns/campaign-1/video_pub.mp4",
                 "campaigns/campaign-1/video_pub.mp4", 10),
            )
            conn.commit()
            fr_id = conn.execute("SELECT id FROM file_records").fetchone()[0]
        finally:
            conn.close()

        artifact = campaign_store.add_campaign_artifact(
            self.campaign.id,
            artifact_kind="local",
            source_file_record_id=fr_id,
            local_path="/nonexistent/container/path/video_pub.mp4",
            db_path=self.db_path,
        )
        # The artifact has source_file_record_id pointing to a row WITH storage
        # mapping, so it IS mapped. Let's test the case where the file_records
        # row has storage_key but NO backend_id (partial mapping).
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE file_records SET storage_backend_id = NULL WHERE id = ?",
                (fr_id,),
            )
            conn.commit()
        finally:
            conn.close()

        payload = {"artifacts": [artifact.to_dict()]}
        self._insert_job_and_target(payload)

        from scripts.repair_missing_artifacts import diagnose_targets

        conn = self._conn()
        try:
            results = diagnose_targets(conn)
            self.assertEqual(len(results), 1)
            diags = results[0].diagnoses
            unmapped = [d for d in diags if d.classification != "mapped"]
            self.assertEqual(len(unmapped), 1)
            self.assertEqual(unmapped[0].classification, "registered_drive")
        finally:
            conn.close()

    def test_only_live_targets_are_examined(self):
        """Failed/succeeded targets are not included."""
        artifact = campaign_store.add_campaign_artifact(
            self.campaign.id,
            artifact_kind="local",
            local_path="/nonexistent/path/video_pub.mp4",
            db_path=self.db_path,
        )
        payload = {"artifacts": [artifact.to_dict()]}
        # Insert as 'failed' — should not appear.
        self._insert_job_and_target(payload, status="failed")

        from scripts.repair_missing_artifacts import diagnose_targets

        conn = self._conn()
        try:
            results = diagnose_targets(conn)
            self.assertEqual(len(results), 0)
        finally:
            conn.close()


class RepairScriptApplyTests(_DbTestBase):
    """The repair script's --apply mode correctly writes mappings."""

    def _insert_backend(self, backend_id: int = 10):
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO storage_backends (id, slug, label, provider, bucket, "
                "region, endpoint, access_key, secret_key, is_default) "
                "VALUES (?, 'gdrive', 'GDrive', 'rclone', 'GDrive-test', '', '', '', '', 1)",
                (backend_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def _insert_job_and_target(self, payload: dict, status: str = "pending"):
        conn = self._conn()
        try:
            payload_json = json.dumps(payload, sort_keys=True)
            conn.execute(
                "INSERT INTO publish_jobs (idempotency_key, platform, payload_json, status, total_targets) "
                "VALUES (?, 'test', ?, 'active', 1)",
                (f"test-key-{id(payload)}", payload_json),
            )
            job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO publish_job_targets (job_id, account_ref, file_ref, status) "
                "VALUES (?, 'account:1', 'campaign_post:1', ?)",
                (job_id, status),
            )
            target_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
            return job_id, target_id
        finally:
            conn.close()

    def test_apply_registers_local_file(self):
        """--apply creates a file_records row for a local artifact."""
        local_file = Path(self._tmp.name) / "video_pub.mp4"
        local_file.write_bytes(b"\x00" * 100)

        artifact = campaign_store.add_campaign_artifact(
            self.campaign.id,
            artifact_kind="local",
            local_path=str(local_file),
            db_path=self.db_path,
        )
        payload = {"artifacts": [artifact.to_dict()]}
        self._insert_job_and_target(payload)

        from scripts.repair_missing_artifacts import diagnose_targets, apply_repairs

        conn = self._conn()
        try:
            diagnoses = diagnose_targets(conn)
            counts = apply_repairs(conn, diagnoses)
            self.assertGreaterEqual(counts.get("registered_local", 0), 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
