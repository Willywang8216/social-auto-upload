"""A long prep must keep its lease, or its finished work is thrown away.

The stale-prep sweep reclaims a campaign whose lease is older than
``SAU_PREP_LEASE_MINUTES``. Nothing refreshed the lease while a prep ran, so any
prep slower than the window looked exactly like a dead worker:

    campaign 3004 claimed for prep (owner=7-...)          17:49
    campaign 3004 prep finished but its lease was lost     18:34
    campaign 3004 claimed for prep (owner=7-...)          18:34

— an hour of transcoding discarded, then repeated, for hours. Its nine platforms'
worth of targets sat overdue the whole time. The heartbeat renews at a third of
the lease window, and the sweep now reads ``renewedAt`` rather than only the
original claim.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from myUtils import campaigns


def _db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE campaigns (id INTEGER PRIMARY KEY, profile_id INTEGER, media_group_id INTEGER,
                                status TEXT, metadata_json TEXT, created_at TEXT, prepared_at TEXT,
                                published_at TEXT, last_error TEXT, name TEXT, campaign_date TEXT,
                                scheduling_mode TEXT, default_schedule_start TEXT,
                                default_schedule_interval_minutes INTEGER, randomize_minutes INTEGER,
                                notes TEXT, workspace_id TEXT, selected_account_ids_json TEXT,
                                sheet_spreadsheet_id TEXT, sheet_title TEXT);
        CREATE TABLE campaign_posts (id INTEGER PRIMARY KEY, campaign_id INTEGER, status TEXT);
        """
    )
    conn.commit()
    conn.close()


class PrepLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "db.sqlite"
        _db(self.db)

    def _campaign(self, campaign_id: int, *, owner: str = "w-1", claimed: str, renewed: str | None = None,
                  status: str = campaigns.CAMPAIGN_PREPARING) -> None:
        lease = {"owner": owner, "claimedAt": claimed}
        if renewed:
            lease["renewedAt"] = renewed
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO campaigns (id, status, metadata_json) VALUES (?,?,?)",
            (campaign_id, status, json.dumps({campaigns.PREP_LEASE_KEY: lease})),
        )
        conn.commit()
        conn.close()

    def _lease(self, campaign_id: int) -> dict:
        conn = sqlite3.connect(self.db)
        raw = conn.execute("SELECT metadata_json FROM campaigns WHERE id=?", (campaign_id,)).fetchone()[0]
        conn.close()
        return json.loads(raw)[campaigns.PREP_LEASE_KEY]

    def test_renewal_stamps_renewedAt_and_keeps_the_owner(self):
        self._campaign(1, claimed="2026-10-29T00:00:00")
        self.assertTrue(campaigns.renew_campaign_prep_lease(1, "w-1", db_path=self.db))
        lease = self._lease(1)
        self.assertEqual(lease["owner"], "w-1")
        self.assertIn("renewedAt", lease)

    def test_a_different_owner_cannot_renew(self):
        self._campaign(2, claimed="2026-10-29T00:00:00")
        self.assertFalse(campaigns.renew_campaign_prep_lease(2, "someone-else", db_path=self.db))
        self.assertNotIn("renewedAt", self._lease(2))

    def test_a_missing_campaign_is_not_an_error(self):
        self.assertFalse(campaigns.renew_campaign_prep_lease(999, "w-1", db_path=self.db))

    def test_a_renewed_lease_survives_the_stale_sweep(self):
        # claimed long ago, but renewed just now: it must NOT be reclaimed
        from datetime import datetime, timedelta, timezone

        now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
        self._campaign(
            3,
            claimed=(now - timedelta(minutes=200)).isoformat(timespec="seconds"),
            renewed=(now - timedelta(minutes=1)).isoformat(timespec="seconds"),
        )
        self.assertEqual(
            campaigns.requeue_stale_preparing(older_than_minutes=45, db_path=self.db), 0
        )

    def test_a_stale_lease_is_still_reclaimed(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
        self._campaign(
            4,
            claimed=(now - timedelta(minutes=200)).isoformat(timespec="seconds"),
            renewed=(now - timedelta(minutes=90)).isoformat(timespec="seconds"),
        )
        self.assertEqual(
            campaigns.requeue_stale_preparing(older_than_minutes=45, db_path=self.db), 1
        )

    def test_the_heartbeat_renews_while_the_work_runs(self):
        """The worker's heartbeat context keeps a >lease prep alive."""
        from myUtils import worker

        self._campaign(5, claimed="2026-10-29T00:00:00")
        # The interval is lease/3, so a tiny lease keeps the test quick; the
        # production floor of 60 s would otherwise apply.
        with worker._prep_lease_heartbeat(5, "w-1", lease_minutes=0.01, db_path=self.db,
                                          min_interval=0.3):
            deadline = time.time() + 20
            while time.time() < deadline and "renewedAt" not in self._lease(5):
                time.sleep(0.1)
            self.assertIn("renewedAt", self._lease(5))
        # after the block the heartbeat stops
        stamp = self._lease(5).get("renewedAt")
        time.sleep(1.5)
        self.assertEqual(self._lease(5).get("renewedAt"), stamp)


if __name__ == "__main__":
    unittest.main()
