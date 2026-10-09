"""Scheduling integrity: timezone, slot serialisation, disabled accounts.

S2 - the Publish Center sends a naive local datetime (the date picker's
value-format has no offset) while the reschedule path sends a real UTC instant.
Treating the naive form as UTC shifted every scheduled post by the timezone
offset: an operator picking 07:00 got a post at 15:00.

S1 - slot allocation read the booked times and wrote the new target as two
separate steps, so concurrent submits both saw the same free minute. The live
database held nine such collisions (account:120 had three pending targets at one
minute), created by submits minutes apart rather than one burst.
"""

import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from myUtils import publish_orchestrator as po  # noqa: E402


class OperatorTimezoneTests(unittest.TestCase):
    def test_naive_input_is_the_operator_wall_clock(self):
        result = po._resolve_base_time(
            {"publishNow": False, "startAt": "2026-06-17T10:00:00"}
        )
        # 10:00 Asia/Shanghai -> 02:00 UTC
        self.assertEqual(result, datetime(2026, 6, 17, 2, 0, 0))
        self.assertIsNone(result.tzinfo)

    def test_explicit_offset_is_converted_not_reinterpreted(self):
        result = po._resolve_base_time(
            {"publishNow": False, "startAt": "2026-06-17T10:00:00+08:00"}
        )
        self.assertEqual(result, datetime(2026, 6, 17, 2, 0, 0))

    def test_explicit_utc_is_preserved(self):
        result = po._resolve_base_time(
            {"publishNow": False, "startAt": "2026-06-17T10:00:00+00:00"}
        )
        self.assertEqual(result, datetime(2026, 6, 17, 10, 0, 0))

    def test_publish_now_is_none(self):
        self.assertIsNone(po._resolve_base_time({"publishNow": True}))

    def test_unknown_timezone_falls_back_to_utc_without_raising(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"SAU_OPERATOR_TIMEZONE": "Not/AZone"}):
            result = po._resolve_base_time(
                {"publishNow": False, "startAt": "2026-06-17T10:00:00"}
            )
        self.assertEqual(result, datetime(2026, 6, 17, 10, 0, 0))


class SlotLockTests(unittest.TestCase):
    def test_the_lock_serialises_concurrent_holders(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "x.db"
        db.write_bytes(b"")
        order: list[str] = []

        def worker(name, hold):
            with po.slot_reservation_lock(db):
                order.append(f"{name}-in")
                time.sleep(hold)
                order.append(f"{name}-out")

        first = threading.Thread(target=worker, args=("A", 0.4))
        second = threading.Thread(target=worker, args=("B", 0.05))
        first.start()
        time.sleep(0.05)
        second.start()
        first.join()
        second.join()
        self.assertEqual(order, ["A-in", "A-out", "B-in", "B-out"])

    def test_the_lock_is_released_after_use(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "x.db"
        db.write_bytes(b"")
        with po.slot_reservation_lock(db):
            pass
        # Re-acquiring must not block: prove with a short timeout thread.
        done: list[bool] = []

        def reacquire():
            with po.slot_reservation_lock(db):
                done.append(True)

        thread = threading.Thread(target=reacquire)
        thread.start()
        thread.join(timeout=5)
        self.assertEqual(done, [True])

    def test_an_unwritable_lock_path_does_not_raise(self):
        # Best-effort by design: a lock problem must never fail a publish.
        # A directory where the lock file cannot be created stands in for a
        # read-only or missing parent.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        blocked = Path(tmp.name) / "blocked"
        blocked.mkdir()
        (blocked / "ro").write_bytes(b"")  # parent is a FILE, not a directory
        with po.slot_reservation_lock(blocked / "ro" / "x.db"):
            pass


class SlotCollisionTests(unittest.TestCase):
    def test_same_account_targets_are_spaced_by_the_gap(self):
        booked: dict[int, list[str]] = {}
        base = datetime(2026, 10, 14, 22, 0, 0)
        slots = [po._next_free_slot(120, base, 0, booked) for _ in range(3)]
        self.assertEqual(len(set(slots)), 3, "each target needs its own slot")
        self.assertEqual(slots[1] - slots[0], po.timedelta(minutes=po.MIN_GAP_MINUTES))

    def test_different_accounts_may_share_a_minute(self):
        booked: dict[int, list[str]] = {}
        base = datetime(2026, 10, 14, 22, 0, 0)
        self.assertEqual(
            po._next_free_slot(120, base, 0, booked),
            po._next_free_slot(121, base, 0, booked),
        )


if __name__ == "__main__":
    unittest.main()
