"""Tests for the worker's per-target ``publish_date`` resolution.

Regression guard for the scheduled-X outage: the worker only claims targets
whose ``schedule_at`` has already fallen due (``jobs._claimable_clause``), so
forwarding that now-past datetime to an uploader asked the platform to
*schedule* a post in the past. X rejected every one of them with
"定时发布时间必须晚于当前时间", which silently failed all staggered X targets
from 2026-09-12 onwards.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from myUtils import jobs, worker


def _target(schedule_at: str | None) -> jobs.Target:
    return jobs.Target(
        id=1,
        job_id=1,
        account_ref="account:77",
        file_ref="file-ref",
        schedule_at=schedule_at,
        status=jobs.TARGET_RUNNING,
        attempts=1,
    )


def _iso(moment: datetime) -> str:
    """The storage shape: tz-naive UTC ISO seconds (see jobs._now_iso)."""
    return moment.astimezone(timezone.utc).replace(tzinfo=None).isoformat(
        timespec="seconds"
    )


class PublishDateForTargetTests(unittest.TestCase):
    def test_due_target_publishes_now(self) -> None:
        """A target the worker could actually claim is due — never scheduled."""
        past = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
        self.assertEqual(worker._publish_date_for_target(_target(_iso(past))), 0)

    def test_long_past_target_publishes_now(self) -> None:
        """The staggered case that broke X: 5 minutes past is still 'past'."""
        past = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
        self.assertEqual(worker._publish_date_for_target(_target(_iso(past))), 0)

    def test_exactly_now_publishes_now(self) -> None:
        """Boundary: due means ``<= now``, matching the claim clause."""
        self.assertEqual(
            worker._publish_date_for_target(_target(_iso(datetime.now(tz=timezone.utc)))),
            0,
        )

    def test_future_target_is_passed_through(self) -> None:
        """A target that is not due keeps its time (defensive: the claim clause
        never yields one today, but the conversion must not silently retime it)."""
        future = datetime.now(tz=timezone.utc) + timedelta(hours=6)
        resolved = worker._publish_date_for_target(_target(_iso(future)))
        self.assertIsInstance(resolved, datetime)
        # ``_iso`` stores whole seconds, so compare at that resolution.
        self.assertEqual(resolved, future.replace(tzinfo=None, microsecond=0))

    def test_missing_schedule_publishes_now(self) -> None:
        self.assertEqual(worker._publish_date_for_target(_target(None)), 0)
        self.assertEqual(worker._publish_date_for_target(_target("")), 0)

    def test_unparseable_schedule_publishes_now(self) -> None:
        """A malformed stamp must not crash the drain thread."""
        self.assertEqual(worker._publish_date_for_target(_target("not-a-date")), 0)

    def test_resolved_value_is_accepted_by_uploaders(self) -> None:
        """The value handed over must clear the uploaders' own validation.

        This is the assertion that would have caught the outage: a due target
        resolves to the immediate sentinel, which ``validate_publish_date``
        returns unchanged instead of raising.
        """
        try:
            from uploader.base_video import BaseVideoUploader
        except ImportError:  # pragma: no cover - uploader extras not installed
            self.skipTest("uploader.base_video unavailable")

        past = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
        resolved = worker._publish_date_for_target(_target(_iso(past)))
        self.assertEqual(BaseVideoUploader.validate_publish_date(resolved), 0)

    def test_past_schedule_no_longer_raises_in_uploader(self) -> None:
        """Document the underlying uploader contract the worker now avoids."""
        try:
            from uploader.base_video import BaseVideoUploader
        except ImportError:  # pragma: no cover - uploader extras not installed
            self.skipTest("uploader.base_video unavailable")

        past = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
        with self.assertRaises(ValueError):
            BaseVideoUploader.validate_publish_date(past.replace(tzinfo=None))


if __name__ == "__main__":
    unittest.main()
