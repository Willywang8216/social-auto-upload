"""Tests for the schedule optimizer (preferred UTC windows + anti-spam)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path

import db.createTable as create_table
from myUtils import jobs, profiles

from scripts import optimize_schedule as opt


class OptimizeScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "sched.db"
        create_table.bootstrap(self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _target(self, *, account="account:1", platform="twitter", when=None, key="k1"):
        jobs.enqueue_job(
            jobs.JobSpec(
                platform=platform,
                payload={"draft": {"message": "A real caption for the queue."}},
                targets=[(account, "campaign_post:1", when)],
                idempotency_key=key,
            ),
            db_path=self.db_path,
        )

    def test_moves_a_midnight_post_into_a_preferred_window(self) -> None:
        # 03:00 UTC is outside every preferred window.
        self._target(platform="twitter", when="2026-12-01T03:00:00", key="mid")
        changes, _ = opt.plan_optimize(
            db_path=self.db_path, now=datetime(2026, 11, 1, 0, 0, 0)
        )
        self.assertEqual(len(changes), 1)
        new_hour = int(changes[0]["new"][11:13])
        self.assertIn(new_hour, opt.PREFERRED_UTC_HOURS["twitter"])

    def test_never_moves_a_post_earlier(self) -> None:
        self._target(platform="twitter", when="2026-12-01T23:15:00", key="late")
        changes, _ = opt.plan_optimize(
            db_path=self.db_path, now=datetime(2026, 11, 1, 0, 0, 0)
        )
        for item in changes:
            self.assertGreaterEqual(item["new"], item["old"])

    def test_past_due_targets_are_left_alone(self) -> None:
        self._target(platform="twitter", when="2026-01-01T03:00:00", key="past")
        changes, past = opt.plan_optimize(
            db_path=self.db_path, now=datetime(2026, 2, 1, 0, 0, 0)
        )
        self.assertEqual(changes, [])
        self.assertEqual(past, 1)

    def test_caps_posts_per_account_per_day(self) -> None:
        for index in range(5):
            self._target(
                account="account:7",
                platform="twitter",
                when=f"2026-12-01T0{index + 1}:00:00",
                key=f"c{index}",
            )
        changes, _ = opt.plan_optimize(
            db_path=self.db_path, now=datetime(2026, 11, 1, 0, 0, 0)
        )
        from collections import Counter

        per_day = Counter(item["new"][:10] for item in changes)
        # account 7 gets at most MAX_PER_DAY slots on the first day; the rest
        # roll to later days.
        self.assertLessEqual(per_day["2026-12-01"], max(1, opt.MAX_PER_DAY) + 1)



class ScheduleOptimizerIdentityTests(unittest.TestCase):
    """The operator deliberately points two account rows at one provider account.

    Facebook page / Instagram user / Threads user are shared across the NW and SW
    profiles, so "one account" for the anti-spam rules has to mean the provider
    identity. Keying on the row let both rows book the same feed minutes apart.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "sched.db"
        create_table.bootstrap(self.db_path)
        nakedwill = profiles.create_profile("NW", db_path=self.db_path)
        sexualwill = profiles.create_profile("SW", db_path=self.db_path)
        self.page_a = profiles.add_account(
            nakedwill.id, "facebook", "NW-FB", auth_type="oauth",
            config={"pageId": "PAGE-1", "accessToken": "token-a"}, db_path=self.db_path,
        )
        self.page_b = profiles.add_account(
            sexualwill.id, "facebook", "SW-FB", auth_type="oauth",
            config={"pageId": "PAGE-1", "accessToken": "token-b"}, db_path=self.db_path,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _queue(self, account_id: int, when: str, key: str) -> None:
        jobs.enqueue_job(
            jobs.JobSpec(
                platform="facebook",
                payload={"draft": {"message": "A real caption for the queue."}},
                targets=[(f"account:{account_id}", "campaign_post:1", when)],
                idempotency_key=key,
            ),
            db_path=self.db_path,
        )

    def _plan(self):
        changes, _ = opt.plan_optimize(
            db_path=self.db_path, now=datetime(2026, 11, 1, 0, 0, 0)
        )
        return changes

    def test_two_rows_for_one_page_never_stack_on_the_same_feed(self) -> None:
        self._queue(self.page_a.id, "2026-12-01T13:00:00", "a")
        self._queue(self.page_b.id, "2026-12-01T13:00:00", "b")
        changes = self._plan()
        self.assertEqual(len(changes), 2)
        times = sorted(datetime.fromisoformat(item["new"]) for item in changes)
        self.assertGreaterEqual(
            (times[1] - times[0]).total_seconds(), opt.MIN_GAP_MINUTES * 60
        )

    def test_daily_cap_counts_the_page_not_the_row(self) -> None:
        for index in range(2):
            self._queue(self.page_a.id, "2026-12-01T13:00:00", f"a{index}")
            self._queue(self.page_b.id, "2026-12-01T13:00:00", f"b{index}")
        changes = self._plan()
        self.assertEqual(len(changes), 4)
        per_day = Counter(item["new"][:10] for item in changes)
        self.assertLessEqual(per_day["2026-12-01"], opt.MAX_PER_DAY)
        self.assertEqual(per_day["2026-12-02"], 4 - per_day["2026-12-01"])

    def test_rows_without_a_shared_identity_keep_their_own_group(self) -> None:
        telegram = profiles.add_account(
            self.page_a.profile_id, "telegram", "NW TG", auth_type="api",
            config={"channelId": "-100"}, db_path=self.db_path,
        )
        unidentified = profiles.add_account(
            self.page_b.profile_id, "facebook", "no-page", auth_type="oauth",
            config={"accessToken": "token-c"}, db_path=self.db_path,
        )
        conn = sqlite3.connect(self.db_path)
        try:
            groups = opt._identity_groups(conn)
        finally:
            conn.close()
        shared = opt._identity_group(f"account:{self.page_a.id}", groups)
        self.assertEqual(shared, "meta:facebook:PAGE-1")
        self.assertEqual(opt._identity_group(f"account:{self.page_b.id}", groups), shared)
        for account in (telegram, unidentified):
            self.assertEqual(
                opt._identity_group(f"account:{account.id}", groups),
                f"account:{account.id}",
            )


if __name__ == "__main__":
    unittest.main()
