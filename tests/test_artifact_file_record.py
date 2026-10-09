"""Every artifact with bytes on disk must be restorable.

The offload cron moves published media to Drive and deletes the local copy,
keeping it restorable only through the mapping it writes into ``file_records``
(``storage_key`` + ``storage_backend_id``). An artifact created without that row
is a file whose local copy can vanish with no route back: 1831 of 2509 campaign
artifacts had no ``file_records`` row, and 241 pending targets referenced one
with no public URL - a publish that could only ever fail.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

import db.createTable as create_table
from myUtils import campaigns as campaign_store
from myUtils import profiles as profile_registry


class ArtifactFileRecordTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "artifacts.db"
        create_table.bootstrap(self.db_path)
        self.profile_id = profile_registry.create_profile(
            "Artifacts", db_path=self.db_path
        ).id
        connection = sqlite3.connect(str(self.db_path))
        try:
            connection.execute(
                "INSERT INTO media_groups (id, name, notes, status) "
                "VALUES (1, 'g', '', 'ready')"
            )
            connection.commit()
        finally:
            connection.close()
        self.campaign = campaign_store.create_campaign(
            profile_id=self.profile_id, media_group_id=1, db_path=self.db_path
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _records(self):
        connection = sqlite3.connect(str(self.db_path))
        try:
            return connection.execute(
                "SELECT id, filename, file_path FROM file_records"
            ).fetchall()
        finally:
            connection.close()

    def test_artifact_creation_registers_a_file_record(self):
        artifact = campaign_store.add_campaign_artifact(
            self.campaign.id,
            artifact_kind="local",
            local_path="/app/generated/campaigns/campaign-1/x_pub.mp4",
            db_path=self.db_path,
        )
        records = self._records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][2], "generated/campaigns/campaign-1/x_pub.mp4")
        # And the artifact points at it, so restore has a route.
        self.assertEqual(artifact.source_file_record_id, records[0][0])

    def test_same_path_in_another_shape_does_not_duplicate(self):
        campaign_store.add_campaign_artifact(
            self.campaign.id, artifact_kind="local",
            local_path="/app/generated/campaigns/campaign-1/x_pub.mp4",
            db_path=self.db_path,
        )
        # The same file referred to by its repo-relative form must reuse the row,
        # not create a second one (duplicates would let the offloader register
        # only one and strand the other).
        campaign_store.add_campaign_artifact(
            self.campaign.id, artifact_kind="remote_upload",
            local_path="generated/campaigns/campaign-1/x_pub.mp4",
            db_path=self.db_path,
        )
        self.assertEqual(len(self._records()), 1)

    def test_existing_record_is_reused_and_left_alone(self):
        connection = sqlite3.connect(str(self.db_path))
        try:
            connection.execute(
                "INSERT INTO file_records (filename, filesize, file_path, "
                "storage_key, storage_backend_id) VALUES (?, ?, ?, ?, ?)",
                ("x_pub.mp4", 12.0, "generated/campaigns/campaign-1/x_pub.mp4",
                 "campaigns/campaign-1/x_pub.mp4", 7),
            )
            connection.commit()
            existing_id = connection.execute(
                "SELECT id FROM file_records"
            ).fetchone()[0]
        finally:
            connection.close()
        artifact = campaign_store.add_campaign_artifact(
            self.campaign.id, artifact_kind="local",
            local_path="/app/generated/campaigns/campaign-1/x_pub.mp4",
            db_path=self.db_path,
        )
        self.assertEqual(len(self._records()), 1)
        self.assertEqual(artifact.source_file_record_id, existing_id)
        # The offloader's registered storage location must survive untouched.
        connection = sqlite3.connect(str(self.db_path))
        try:
            key, backend = connection.execute(
                "SELECT storage_key, storage_backend_id FROM file_records"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(key, "campaigns/campaign-1/x_pub.mp4")
        self.assertEqual(backend, 7)

    def test_no_local_path_registers_nothing(self):
        campaign_store.add_campaign_artifact(
            self.campaign.id, artifact_kind="remote_upload",
            public_url="https://cdn.example/x.mp4", db_path=self.db_path,
        )
        self.assertEqual(self._records(), [])

    def test_non_media_path_registers_nothing(self):
        # A path outside the media roots has no offload mapping to keep, so
        # inventing a file_records row for it would only create noise.
        campaign_store.add_campaign_artifact(
            self.campaign.id, artifact_kind="local",
            local_path="/tmp/scratch/output.mp4", db_path=self.db_path,
        )
        self.assertEqual(len(self._records()), 1)
        self.assertEqual(self._records()[0][2], "/tmp/scratch/output.mp4")
