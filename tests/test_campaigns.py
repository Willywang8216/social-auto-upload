"""Tests for campaign persistence."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

import db.createTable as create_table

if "conf" not in sys.modules:
    conf_module = types.ModuleType("conf")
    conf_module.BASE_DIR = str(Path(__file__).resolve().parent.parent)
    sys.modules["conf"] = conf_module

from myUtils import campaigns, media_groups, profiles


class CampaignPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        create_table.bootstrap(self.db_path)
        self.profile = profiles.create_profile("Brand", db_path=self.db_path)
        self.file_id = self._file_record("clip.mp4")
        self.media_group = media_groups.create_media_group(
            "Launch batch",
            primary_video_file_id=self.file_id,
            db_path=self.db_path,
        )
        media_groups.add_media_group_item(
            self.media_group.id,
            self.file_id,
            role=media_groups.ROLE_VIDEO,
            db_path=self.db_path,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _file_record(self, filename: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO file_records (filename, filesize, file_path)
                VALUES (?, ?, ?)
                """,
                (filename, 1.0, f"/tmp/{filename}"),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def _publish_job(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO publish_jobs (
                    idempotency_key,
                    profile_id,
                    platform,
                    payload_json,
                    status,
                    total_targets
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "test-job",
                    self.profile.id,
                    profiles.PLATFORM_TWITTER,
                    "{}",
                    "pending",
                    1,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def test_create_campaign_persists_json_fields(self) -> None:
        campaign = campaigns.create_campaign(
            self.profile.id,
            self.media_group.id,
            selected_account_ids=[3, 5],
            metadata={"source": "manual"},
            db_path=self.db_path,
        )
        fetched = campaigns.get_campaign(campaign.id, db_path=self.db_path)
        self.assertEqual(fetched.selected_account_ids, [3, 5])
        self.assertEqual(fetched.metadata, {"source": "manual"})
        self.assertEqual(fetched.status, campaigns.CAMPAIGN_DRAFT)

    def test_update_campaign_tracks_sheet_and_status(self) -> None:
        campaign = campaigns.create_campaign(
            self.profile.id,
            self.media_group.id,
            db_path=self.db_path,
        )
        updated = campaigns.update_campaign(
            campaign.id,
            status=campaigns.CAMPAIGN_PREPARED,
            sheet_spreadsheet_id="sheet-123",
            sheet_title="2026-05-01-brand",
            prepared_at="2026-05-01T10:00:00",
            last_error=None,
            db_path=self.db_path,
        )
        self.assertEqual(updated.status, campaigns.CAMPAIGN_PREPARED)
        self.assertEqual(updated.sheet_spreadsheet_id, "sheet-123")
        self.assertEqual(updated.sheet_title, "2026-05-01-brand")
        self.assertEqual(updated.prepared_at, "2026-05-01T10:00:00")

    def test_artifacts_and_posts_round_trip(self) -> None:
        campaign = campaigns.create_campaign(
            self.profile.id,
            self.media_group.id,
            db_path=self.db_path,
        )
        artifact = campaigns.add_campaign_artifact(
            campaign.id,
            artifact_kind="transcript",
            source_file_record_id=self.file_id,
            local_path="/tmp/transcript.txt",
            metadata={"language": "zh"},
            db_path=self.db_path,
        )
        post = campaigns.add_campaign_post(
            campaign.id,
            profiles.PLATFORM_TWITTER,
            account_ids=[11],
            draft={"message": "hello"},
            sheet_row={"Message": "hello"},
            status=campaigns.CAMPAIGN_POST_READY,
            db_path=self.db_path,
        )
        publish_job_id = self._publish_job()
        updated_post = campaigns.update_campaign_post(
            post.id,
            status=campaigns.CAMPAIGN_POST_QUEUED,
            last_published_job_id=publish_job_id,
            db_path=self.db_path,
        )

        artifacts = campaigns.list_campaign_artifacts(campaign.id, db_path=self.db_path)
        posts = campaigns.list_campaign_posts(
            campaign.id,
            status=campaigns.CAMPAIGN_POST_QUEUED,
            db_path=self.db_path,
        )
        self.assertEqual(artifact.metadata, {"language": "zh"})
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(updated_post.last_published_job_id, publish_job_id)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].draft, {"message": "hello"})

    def test_delete_campaign_cascades_posts_and_artifacts(self) -> None:
        campaign = campaigns.create_campaign(
            self.profile.id,
            self.media_group.id,
            db_path=self.db_path,
        )
        campaigns.add_campaign_artifact(
            campaign.id,
            artifact_kind="audio",
            db_path=self.db_path,
        )
        campaigns.add_campaign_post(
            campaign.id,
            profiles.PLATFORM_THREADS,
            db_path=self.db_path,
        )

        campaigns.delete_campaign(campaign.id, db_path=self.db_path)
        self.assertEqual(campaigns.list_campaigns(db_path=self.db_path), [])
        with sqlite3.connect(self.db_path) as conn:
            artifact_count = conn.execute(
                "SELECT COUNT(*) FROM campaign_artifacts"
            ).fetchone()[0]
            post_count = conn.execute(
                "SELECT COUNT(*) FROM campaign_posts"
            ).fetchone()[0]
        self.assertEqual(artifact_count, 0)
        self.assertEqual(post_count, 0)


if __name__ == "__main__":
    unittest.main()
