"""One provider account shared across profiles must not be queued twice.

The operator consolidated the SFW Meta set ("it's all SFW so I am now using one
set of these accounts for it"): Facebook page `Sexualwill`, Instagram and Threads
`itsmenakedwill` each have a row in the NW profile *and* in the SW profile, all
pointing at the same provider account. The queue guard keyed on the internal
account id, so submitting `profileIds:[1,3]` queued the same media twice — ten
media groups were in that state on 2026-10-28 and would have posted twice to one
page.
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myUtils import publish_orchestrator as po  # noqa: E402


class _Account:
    def __init__(self, account_id: int, platform: str, config: dict):
        self.id = account_id
        self.platform = platform
        self.config = config
        self.account_name = f"acct-{account_id}"
        self.nickname = ""


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, platform TEXT, config_json TEXT);
        CREATE TABLE file_records (id INTEGER PRIMARY KEY, filename TEXT);
        CREATE TABLE media_groups (id INTEGER PRIMARY KEY);
        CREATE TABLE media_group_items (media_group_id INTEGER, file_record_id INTEGER);
        CREATE TABLE campaigns (id INTEGER PRIMARY KEY, media_group_id INTEGER);
        CREATE TABLE campaign_posts (id INTEGER PRIMARY KEY, campaign_id INTEGER);
        CREATE TABLE publish_job_targets (id INTEGER PRIMARY KEY, account_ref TEXT, file_ref TEXT,
                                          status TEXT, schedule_at TEXT);
        """
    )
    return conn


class SharedIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path("/tmp") / "sau-shared-identity-test.db"
        self.tmp.unlink(missing_ok=True)
        conn = _db()
        with sqlite3.connect(self.tmp) as disk:
            conn.backup(disk)
        conn.close()
        with sqlite3.connect(self.tmp) as c:
            c.execute("INSERT INTO accounts VALUES (11, 'facebook', '{\"pageId\": \"125\"}')")
            c.execute("INSERT INTO accounts VALUES (64, 'facebook', '{\"pageId\": \"125\"}')")
            c.execute("INSERT INTO accounts VALUES (99, 'facebook', '{\"pageId\": \"777\"}')")
            c.commit()

    def tearDown(self) -> None:
        self.tmp.unlink(missing_ok=True)

    def test_identity_is_read_from_the_meta_config(self):
        self.assertEqual(
            po._external_account_identity(_Account(11, "facebook", {"pageId": "125"})),
            ("facebook", "125"),
        )
        self.assertEqual(
            po._external_account_identity(_Account(134, "instagram", {"igUserId": "1784"})),
            ("instagram", "1784"),
        )
        self.assertEqual(
            po._external_account_identity(_Account(133, "threads", {"threadUserId": "2885"})),
            ("threads", "2885"),
        )
        self.assertIsNone(po._external_account_identity(_Account(77, "twitter", {"x": "1"})))

    def test_rows_with_the_same_page_are_found(self):
        ids = po._account_ids_with_identity("facebook", "125", db_path=self.tmp)
        self.assertEqual(ids, [11, 64])

    def test_the_other_profile_row_makes_the_media_a_duplicate(self):
        # account 11 (NW) already has this media queued...
        with sqlite3.connect(self.tmp) as c:
            c.execute("INSERT INTO media_groups VALUES (5)")
            c.execute("INSERT INTO media_group_items VALUES (5, 1)")
            c.execute("INSERT INTO campaigns VALUES (1, 5)")
            c.execute("INSERT INTO campaign_posts VALUES (1, 1)")
            c.execute("INSERT INTO publish_job_targets VALUES (1, 'account:11', 'campaign_post:1', 'pending', '2026-11-01T13:00:00')")
            c.commit()
        # ...so the SW row (64) pointing at the same page must be refused.
        self.assertTrue(
            po._shared_identity_already_queued(_Account(64, "facebook", {"pageId": "125"}), 5, db_path=self.tmp)
        )

    def test_a_different_page_is_not_a_duplicate(self):
        self.assertFalse(
            po._shared_identity_already_queued(_Account(99, "facebook", {"pageId": "777"}), 5, db_path=self.tmp)
        )

    def test_a_platform_without_a_shared_identity_is_never_flagged(self):
        self.assertFalse(
            po._shared_identity_already_queued(_Account(123, "twitter", {}), 5, db_path=self.tmp)
        )


if __name__ == "__main__":
    unittest.main()
