"""Tests for the smart schedule-shift feature (myUtils/schedule_shift.py).

Covers the operator-facing contract:

* a plain N-day shift preserves the operator wall-clock time;
* the shifted queue still honours the per-account min-gap and per-day cap by
  reusing the orchestrator allocator;
* dry-run plans never write, ``apply_shift`` does and backs up;
* running/succeeded/cancelled targets are not touched (unless opted in);
* the timezone-preservation arithmetic is DST-correct, not ``+86400*N``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import db.createTable as create_table
from myUtils import jobs
from myUtils import schedule_shift as ss

REPO_ROOT = Path(__file__).resolve().parent.parent
flask_available = importlib.util.find_spec("flask") is not None


class ScheduleShiftTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "shift.db"
        create_table.bootstrap(self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def _target(
        self,
        *,
        account: int = 1,
        platform: str = "twitter",
        when: str | None = None,
        key: str = "k1",
        file_ref: str = "campaign_post:1",
        profile_id: int | None = None,
        status: str | None = None,
    ) -> int:
        job = jobs.enqueue_job(
            jobs.JobSpec(
                platform=platform,
                payload={"draft": {"message": "A genuine caption for the queue."}},
                targets=[(f"account:{account}", file_ref, when)],
                idempotency_key=key,
                profile_id=profile_id,
            ),
            db_path=self.db_path,
        )
        target_id = jobs.list_targets(job.id, db_path=self.db_path)[0].id
        if status is not None:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute(
                    "UPDATE publish_job_targets SET status = ? WHERE id = ?",
                    (status, target_id),
                )
        return target_id

    def _schedules(self) -> dict[int, str | None]:
        with sqlite3.connect(str(self.db_path)) as conn:
            return {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT id, schedule_at FROM publish_job_targets"
                )
            }

    def _zone(self) -> ZoneInfo:
        return ZoneInfo("Asia/Shanghai")


class WallClockTests(unittest.TestCase):
    def test_shift_preserves_local_wall_clock(self):
        tz = ZoneInfo("Asia/Shanghai")
        # 2026-06-17 08:00 +08:00 == 2026-06-17 00:00 UTC.
        shifted = ss.shift_wall_clock(datetime(2026, 6, 17, 0, 0, 0), 3, tz)
        local = shifted.replace(tzinfo=timezone.utc).astimezone(tz)
        self.assertEqual(local.date(), datetime(2026, 6, 20).date())
        self.assertEqual((local.hour, local.minute), (8, 0))

    def test_dst_free_zone_equals_naive_utc_addition(self):
        # The operator's own zone has no DST, so the two forms agree. We assert
        # that rather than assume it: it is why the common case looks simple.
        tz = ZoneInfo("Asia/Shanghai")
        for base in (
            datetime(2026, 1, 15, 0, 0, 0),
            datetime(2026, 6, 17, 23, 30, 0),
            datetime(2026, 10, 1, 13, 0, 0),
        ):
            self.assertEqual(
                ss.shift_wall_clock(base, 3, tz),
                base + timedelta(days=3),
            )

    def test_dst_zone_preserves_wall_clock_not_utc_offset(self):
        # US DST begins 2026-03-08. 08:00 EST is 13:00 UTC; three days later
        # 08:00 is EDT, i.e. 12:00 UTC. A naive +86400*3 would yield 13:00 UTC
        # (09:00 local) — the exact bug this guards against.
        tz = ZoneInfo("America/New_York")
        base = datetime(2026, 3, 7, 13, 0, 0)  # 08:00 EST
        shifted = ss.shift_wall_clock(base, 3, tz)
        self.assertEqual(shifted, datetime(2026, 3, 10, 12, 0, 0))  # 08:00 EDT
        self.assertNotEqual(shifted, base + timedelta(days=3))

    def test_local_date_range_is_converted_from_operator_zone(self):
        tz = ZoneInfo("Asia/Shanghai")
        start, end = ss.local_date_range_to_utc(
            datetime(2026, 10, 20).date(), datetime(2026, 10, 21).date(), tz
        )
        self.assertEqual(start, datetime(2026, 10, 19, 16, 0, 0))
        self.assertEqual(end, datetime(2026, 10, 21, 16, 0, 0))


class PlanShiftTests(ScheduleShiftTestCase):
    def test_basic_n_day_shift(self):
        self._target(when="2026-11-01T13:00:00", key="basic")
        plan = ss.plan_shift(db_path=self.db_path, days=3)
        self.assertEqual(len(plan.changes), 1)
        change = plan.changes[0]
        self.assertEqual(change.original, "2026-11-01T13:00:00")
        self.assertEqual(change.shifted, "2026-11-04T13:00:00")
        self.assertEqual(change.new, "2026-11-04T13:00:00")
        self.assertFalse(change.moved_further)

    def test_gap_enforcement(self):
        self._target(account=1, when="2026-11-01T13:00:00", key="g1")
        self._target(account=1, when="2026-11-01T13:10:00", key="g2")
        plan = ss.plan_shift(
            db_path=self.db_path, days=3, min_gap_minutes=30, max_per_day=50
        )
        times = [datetime.fromisoformat(c.new) for c in plan.changes]
        self.assertEqual(len(times), 2)
        self.assertGreaterEqual(
            abs((times[1] - times[0]).total_seconds()), 30 * 60
        )
        moved = [c for c in plan.changes if c.moved_further]
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0].reason, "min gap")

    def test_per_day_cap_enforcement(self):
        # Four targets on one day for one account; cap is two.
        for index in range(4):
            self._target(
                account=7,
                when=f"2026-11-01T0{index + 1}:00:00",
                key=f"cap{index}",
            )
        plan = ss.plan_shift(
            db_path=self.db_path, days=3, min_gap_minutes=1, max_per_day=2
        )
        per_day: dict[str, int] = {}
        for change in plan.changes:
            day = change.new[:10]
            per_day[day] = per_day.get(day, 0) + 1
        self.assertTrue(per_day)
        self.assertLessEqual(max(per_day.values()), 2)
        caps = [c for c in plan.changes if c.reason == "daily cap"]
        self.assertEqual(len(caps), 2)

    def test_plan_shift_is_read_only(self):
        target_id = self._target(when="2026-11-01T13:00:00", key="ro")
        before = self._schedules()
        ss.plan_shift(db_path=self.db_path, days=3)
        self.assertEqual(self._schedules(), before)

    def test_running_target_is_never_selected(self):
        self._target(when="2026-11-01T13:00:00", key="run", status="running")
        plan = ss.plan_shift(db_path=self.db_path, days=3)
        self.assertEqual(plan.changes, [])

    def test_succeeded_and_cancelled_need_explicit_opt_in(self):
        self._target(when="2026-11-01T13:00:00", key="ok", status="succeeded")
        self._target(when="2026-11-01T14:00:00", key="cx", status="cancelled")
        self._target(when="2026-11-01T15:00:00", key="pd", status="pending")
        default_plan = ss.plan_shift(db_path=self.db_path, days=3)
        self.assertEqual(len(default_plan.changes), 1)
        self.assertEqual(default_plan.changes[0].status, "pending")

        terminal_plan = ss.plan_shift(
            db_path=self.db_path,
            days=3,
            scope=ss.ShiftScope(
                statuses=("pending", "succeeded", "cancelled"),
                include_terminal=True,
            ),
        )
        self.assertEqual(len(terminal_plan.changes), 3)

    def test_non_positive_days_rejected(self):
        with self.assertRaises(ValueError):
            ss.plan_shift(db_path=self.db_path, days=0)

    def test_platform_and_account_scope(self):
        self._target(account=1, platform="twitter", when="2026-11-01T13:00:00", key="s1")
        self._target(account=2, platform="twitter", when="2026-11-01T13:00:00", key="s2")
        self._target(account=1, platform="youtube", when="2026-11-01T13:00:00", key="s3")

        by_platform = ss.plan_shift(
            db_path=self.db_path,
            days=3,
            scope=ss.ShiftScope(platform="youtube"),
        )
        self.assertEqual([c.platform for c in by_platform.changes], ["youtube"])

        by_account = ss.plan_shift(
            db_path=self.db_path,
            days=3,
            scope=ss.ShiftScope(account_ids=(2,)),
        )
        self.assertEqual([c.account_id for c in by_account.changes], [2])

    def test_profile_and_date_range_scope(self):
        from myUtils import profiles as profile_registry

        first_profile = profile_registry.create_profile("Shift A", db_path=self.db_path)
        second_profile = profile_registry.create_profile("Shift B", db_path=self.db_path)
        self._target(
            account=1, platform="twitter", when="2026-11-01T13:00:00",
            key="p1", profile_id=first_profile.id,
        )
        self._target(
            account=1, platform="twitter", when="2026-12-01T13:00:00",
            key="p2", profile_id=second_profile.id,
        )
        by_profile = ss.plan_shift(
            db_path=self.db_path, days=3,
            scope=ss.ShiftScope(profile_id=first_profile.id),
        )
        self.assertEqual(len(by_profile.changes), 1)

        tz = self._zone()
        start, end = ss.local_date_range_to_utc(
            datetime(2026, 12, 1).date(), datetime(2026, 12, 1).date(), tz
        )
        by_date = ss.plan_shift(
            db_path=self.db_path,
            days=3,
            scope=ss.ShiftScope(start_at=start, end_at=end),
        )
        self.assertEqual(len(by_date.changes), 1)
        self.assertEqual(by_date.changes[0].profile_id, second_profile.id)


class ApplyShiftTests(ScheduleShiftTestCase):
    def test_apply_writes_and_backs_up(self):
        target_id = self._target(when="2026-11-01T13:00:00", key="apply")
        result = ss.apply_shift(db_path=self.db_path, days=3, backup=True)
        self.assertEqual(result.applied, 1)
        self.assertIsNotNone(result.backup_path)
        self.assertTrue(result.backup_path.exists())
        self.assertEqual(
            self._schedules()[target_id], "2026-11-04T13:00:00"
        )

    def test_dry_run_cli_writes_nothing(self):
        from sau_cli import main

        target_id = self._target(when="2026-11-01T13:00:00", key="cli")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = main(["schedule", "shift", "--db-path", str(self.db_path), "--days", "3"])
        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", buffer.getvalue())
        self.assertEqual(self._schedules()[target_id], "2026-11-01T13:00:00")

    def test_cli_apply_writes_and_reports(self):
        from sau_cli import main

        target_id = self._target(when="2026-11-01T13:00:00", key="cliapply")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = main([
                "schedule", "shift", "--db-path", str(self.db_path),
                "--days", "3", "--apply",
            ])
        self.assertEqual(rc, 0)
        self.assertIn("APPLY", buffer.getvalue())
        self.assertEqual(self._schedules()[target_id], "2026-11-04T13:00:00")


class ShiftApiTests(unittest.TestCase):
    """The ``POST /jobs/schedule/shift`` surface (dry-run by default)."""

    @unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "shift_api.db"
        create_table.bootstrap(self.db_path)

        import sau_backend
        from myUtils.security import SecurityPolicy

        self._sau_backend = sau_backend
        self._orig_policy = sau_backend.app.config["SECURITY_POLICY"]
        sau_backend.app.config["SECURITY_POLICY"] = SecurityPolicy(
            tokens=frozenset(), cors_origins=("http://localhost:5173",)
        )
        sau_backend.app.config.update(TESTING=True)
        self.client = sau_backend.app.test_client()
        self._patch = patch.object(
            sau_backend, "_current_db_path", return_value=self.db_path
        )
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._sau_backend.app.config["SECURITY_POLICY"] = self._orig_policy
        self._tmp.cleanup()

    def _seed(self) -> int:
        job = jobs.enqueue_job(
            jobs.JobSpec(
                platform="twitter",
                payload={"draft": {"message": "A genuine caption for the queue."}},
                targets=[("account:1", "campaign_post:1", "2026-11-01T13:00:00")],
                idempotency_key="api",
            ),
            db_path=self.db_path,
        )
        return jobs.list_targets(job.id, db_path=self.db_path)[0].id

    def _schedule(self, target_id: int) -> str | None:
        with sqlite3.connect(str(self.db_path)) as conn:
            return conn.execute(
                "SELECT schedule_at FROM publish_job_targets WHERE id = ?",
                (target_id,),
            ).fetchone()[0]

    def test_shift_endpoint_is_dry_run_by_default(self) -> None:
        import json

        target_id = self._seed()
        response = self.client.post(
            "/jobs/schedule/shift",
            data=json.dumps({"days": 3, "scope": {"platform": "twitter"}}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["data"]["mode"], "dry-run")
        self.assertEqual(body["data"]["summary"]["total"], 1)
        self.assertEqual(self._schedule(target_id), "2026-11-01T13:00:00")

    def test_shift_endpoint_applies_when_asked(self) -> None:
        import json

        target_id = self._seed()
        response = self.client.post(
            "/jobs/schedule/shift",
            data=json.dumps(
                {"days": 3, "apply": True, "scope": {"platform": "twitter"}}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["data"]["mode"], "apply")
        self.assertEqual(body["data"]["summary"]["applied"], 1)
        self.assertEqual(self._schedule(target_id), "2026-11-04T13:00:00")

    def test_shift_endpoint_rejects_bad_days(self) -> None:
        import json

        response = self.client.post(
            "/jobs/schedule/shift",
            data=json.dumps({"days": 0}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
