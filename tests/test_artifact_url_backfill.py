"""A part artifact must never inherit the full file's public URL.

`_backfill_artifact_public_urls` matched sibling artifacts only by
`source_file_record_id`. A split part and the full video share that source id,
so a part with no URL was given the full video's URL; the publisher then fetched
the wrong object (a 404 from /getFile) instead of the local part.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import db.createTable as create_table  # noqa: E402
from myUtils import campaigns, media_groups, profiles, worker  # noqa: E402
import sqlite3  # noqa: E402


class ArtifactUrlBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "backfill.db"
        create_table.bootstrap(self.db)
        profile = profiles.create_profile("P", db_path=self.db)
        group = media_groups.create_media_group("G", db_path=self.db)
        self.campaign = campaigns.create_campaign(
            profile.id, group.id, status=campaigns.CAMPAIGN_PREPARING, db_path=self.db
        )
        with sqlite3.connect(self.db) as conn:
            cursor = conn.execute(
                "INSERT INTO file_records (filename, filesize, file_path) VALUES (?, ?, ?)",
                ("source.mp4", 1.0, "/tmp/source.mp4"),
            )
            self.source_id = int(cursor.lastrowid)
            conn.commit()

    def _add(self, local_path: str, public_url=None) -> None:
        campaigns.add_campaign_artifact(
            self.campaign.id,
            artifact_kind="local",
            source_file_record_id=self.source_id,
            local_path=local_path,
            public_url=public_url,
            db_path=self.db,
        )

    def test_part_artifact_does_not_inherit_the_full_files_url(self) -> None:
        self._add(
            "/app/generated/campaigns/campaign-1/full_pub.mp4",
            "https://cdn/full_pub.mp4",
        )
        self._add("/app/generated/campaigns/campaign-1/full_pub_part1of2_pub.mp4")
        part = {
            "campaign_id": self.campaign.id,
            "artifact_kind": "local",
            "local_path": "/app/generated/campaigns/campaign-1/full_pub_part1of2_pub.mp4",
            "source_file_record_id": self.source_id,
        }
        worker._backfill_artifact_public_urls([part], self.db)
        self.assertEqual(str(part.get("public_url") or "").strip(), "")

    def test_same_file_sibling_still_backfills(self) -> None:
        campaigns.add_campaign_artifact(
            self.campaign.id,
            artifact_kind="remote_upload",
            source_file_record_id=self.source_id,
            local_path="/tmp/full_pub.mp4",
            public_url="https://cdn/full_pub.mp4",
            db_path=self.db,
        )
        artifact = {
            "campaign_id": self.campaign.id,
            "artifact_kind": "watermarked_video",
            "local_path": "/tmp/full_pub.mp4",
            "source_file_record_id": self.source_id,
        }
        worker._backfill_artifact_public_urls([artifact], self.db)
        self.assertEqual(artifact.get("public_url"), "https://cdn/full_pub.mp4")

    def test_restore_rewrites_local_path_to_the_resolved_file(self) -> None:
        """A publisher in another process must get the real path, not /app/..."""
        import tempfile as _tempfile
        from unittest.mock import patch

        with _tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "clip.mp4"
            real.write_bytes(b"x" * 20)
            artifact = {
                "local_path": "/app/generated/campaigns/c/x.mp4",
                "artifact_kind": "local",
            }
            with patch.object(worker, "_resolve_media_path", return_value=real):
                worker._ensure_artifact_paths_local(
                    {"artifacts": [artifact]}, db_path=self.db
                )
        self.assertEqual(artifact["local_path"], str(real))


if __name__ == "__main__":
    unittest.main()
