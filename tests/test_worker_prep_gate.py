"""A target must not publish while its campaign is still preparing.

The auto-publisher creates a campaign and queues its targets immediately, while
media prep runs in the background — and it schedules the first slot in the past
(the item was already overdue). The worker then claimed the target, found no
media artifacts, and failed it permanently with "media artifacts were supplied
but none is a supported image". Eleven targets died that way on 2026-10-29,
across Facebook, Instagram, Threads, Telegram, Bluesky and TikTok.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from myUtils import jobs


def _db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE campaigns (id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE campaign_posts (id INTEGER PRIMARY KEY, campaign_id INTEGER);
        CREATE TABLE publish_jobs (id INTEGER PRIMARY KEY, status TEXT, started_at TEXT,
                                   total_targets INTEGER, completed_targets INTEGER,
                                   failed_targets INTEGER, idempotency_key TEXT,
                                   profile_id INTEGER, platform TEXT, payload_json TEXT,
                                   created_at TEXT, finished_at TEXT);
        CREATE TABLE publish_job_targets (id INTEGER PRIMARY KEY, job_id INTEGER,
                                          account_ref TEXT, file_ref TEXT, status TEXT,
                                          schedule_at TEXT, attempts INTEGER DEFAULT 0,
                                          started_at TEXT, finished_at TEXT, last_error TEXT);
        """
    )
    return conn


class PrepGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "db.sqlite"
        conn = _db(self.db)
        conn.execute("INSERT INTO publish_jobs VALUES (1,'pending',NULL,1,0,0,'k',1,'facebook','{}','2026-10-01',NULL)")
        for campaign_id, status in ((10, "preparing"), (11, "publishing"), (12, "needs_review")):
            conn.execute("INSERT INTO campaigns VALUES (?,?)", (campaign_id, status))
            conn.execute("INSERT INTO campaign_posts VALUES (?,?)", (campaign_id, campaign_id))
            conn.execute(
                "INSERT INTO publish_job_targets (id, job_id, account_ref, file_ref, status, schedule_at)"
                " VALUES (?,1,'account:1',?,'pending','2020-01-01T00:00:00')",
                (campaign_id, f"campaign_post:{campaign_id}"),
            )
        # a legacy target: file_ref is a path, not a campaign post
        conn.execute(
            "INSERT INTO publish_job_targets (id, job_id, account_ref, file_ref, status, schedule_at)"
            " VALUES (99,1,'account:1','videoFile/old.mp4','pending','2020-01-01T00:00:00')"
        )
        conn.commit()
        conn.close()

    def _claimed(self) -> set[int]:
        claimed = jobs.claim_next_targets(limit=10, db_path=self.db)
        return {target.id for target in claimed}

    def test_a_preparing_campaign_is_not_claimable(self):
        self.assertNotIn(10, self._claimed())

    def test_a_needs_review_campaign_is_not_claimable(self):
        self.assertNotIn(12, self._claimed())

    def test_a_publishing_campaign_is_claimable(self):
        self.assertIn(11, self._claimed())

    def test_a_legacy_target_is_still_claimable(self):
        self.assertIn(99, self._claimed())

    def test_has_claimable_targets_ignores_unprepared_campaigns(self):
        conn = sqlite3.connect(self.db)
        # Drop the legacy path-ref target: it has no campaign and stays
        # claimable by design, so it would mask what this test checks.
        conn.execute("DELETE FROM publish_job_targets WHERE id=99")
        conn.execute("UPDATE campaigns SET status='preparing'")
        conn.commit()
        conn.close()
        self.assertFalse(jobs.has_claimable_targets(db_path=self.db))
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE campaigns SET status='publishing' WHERE id=11")
        conn.commit()
        conn.close()
        self.assertTrue(jobs.has_claimable_targets(db_path=self.db))


if __name__ == "__main__":
    unittest.main()
