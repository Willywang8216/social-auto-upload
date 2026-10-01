from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import db.createTable as create_table
from myUtils import campaigns, jobs, media_groups, profiles, scheduled_copy


class ScheduledCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "scheduled-copy.db"
        create_table.bootstrap(self.db_path)
        self.profile_id = profiles.create_profile("Test profile", db_path=self.db_path).id
        self.media_group_id = media_groups.create_media_group(
            "Test media", db_path=self.db_path
        ).id

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _post_with_target(self, message: str, *, status: str = "pending", scheduled: bool = True):
        campaign = campaigns.create_campaign(
            self.profile_id, self.media_group_id, status="scheduled", db_path=self.db_path
        )
        post = campaigns.add_campaign_post(
            campaign.id, "twitter", account_ids=[], draft={"message": message, "title": "说明"},
            status="queued", db_path=self.db_path,
        )
        job = jobs.enqueue_job(
            jobs.JobSpec(
                platform="twitter",
                payload={"campaignId": campaign.id, "campaignPostId": post.id,
                         "message": message, "draft": {"message": message, "title": "说明"}},
                targets=[("account:99", f"campaign_post:{post.id}", "2026-10-01T10:00:00" if scheduled else None)],
            ),
            db_path=self.db_path,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE publish_job_targets SET status=? WHERE job_id=?",
                (status, job.id),
            )
        return post.id, job.id

    def test_preview_converts_only_active_scheduled_simplified_copy(self) -> None:
        pending_id, _ = self._post_with_target("这是一个新的发布测试")
        self._post_with_target("这是重试文案", status="retrying")
        self._post_with_target("这是未排程文案", scheduled=False)
        self._post_with_target("這是繁體文案", status="pending")
        self._post_with_target("這是已發佈文案", status="succeeded")

        preview = scheduled_copy.preview_scheduled_copy(self.db_path)

        self.assertEqual(preview["changed"], 3)
        candidate = next(item for item in preview["items"] if item["postId"] == pending_id)
        self.assertEqual(candidate["before"]["message"], "这是一个新的发布测试")
        self.assertNotEqual(candidate["after"]["message"], candidate["before"]["message"])
        self.assertEqual(candidate["after"]["title"], "說明")

    def test_apply_updates_post_payload_and_writes_audit(self) -> None:
        post_id, job_id = self._post_with_target("这是一个新的发布测试")

        result = scheduled_copy.apply_scheduled_copy(self.db_path, confirm=True)

        self.assertEqual(result["changed"], 1)
        with sqlite3.connect(self.db_path) as conn:
            draft = json.loads(conn.execute(
                "SELECT draft_json FROM campaign_posts WHERE id=?", (post_id,)
            ).fetchone()[0])
            payload = json.loads(conn.execute(
                "SELECT payload_json FROM publish_jobs WHERE id=?", (job_id,)
            ).fetchone()[0])
            audit = conn.execute(
                "SELECT original_draft_json, converted_draft_json FROM scheduled_copy_conversion_audit WHERE post_id=?",
                (post_id,),
            ).fetchone()
        self.assertEqual(draft["message"], payload["message"])
        self.assertEqual(draft, payload["draft"])
        self.assertEqual(json.loads(audit[0])["message"], "这是一个新的发布测试")
        self.assertEqual(json.loads(audit[1]), draft)

    def test_apply_requires_explicit_confirmation(self) -> None:
        with self.assertRaisesRegex(ValueError, "confirm=True"):
            scheduled_copy.apply_scheduled_copy(self.db_path)


if __name__ == "__main__":
    unittest.main()
