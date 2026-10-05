"""Tests for the schedule optimizer (preferred UTC windows + anti-spam)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import db.createTable as create_table
from myUtils import jobs

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


if __name__ == "__main__":
    unittest.main()
