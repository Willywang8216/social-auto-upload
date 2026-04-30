"""Tests for the job-runtime persistence layer (myUtils.jobs)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import db.createTable as create_table
from myUtils import jobs


def _spec(*, key: str | None = None, targets=None) -> jobs.JobSpec:
    return jobs.JobSpec(
        platform="douyin",
        payload={"title": "hello", "tags": ["a"]},
        targets=targets or [("acct-1", "file-1", None)],
        idempotency_key=key,
    )


class JobLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "jobs.db"
        create_table.bootstrap(self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_enqueue_creates_job_and_targets(self) -> None:
        spec = _spec(targets=[("acct-1", "file-a", None), ("acct-2", "file-a", None)])
        job = jobs.enqueue_job(spec, db_path=self.db_path)
        self.assertEqual(job.status, jobs.JOB_PENDING)
        self.assertEqual(job.total_targets, 2)
        targets = jobs.list_targets(job.id, db_path=self.db_path)
        self.assertEqual(len(targets), 2)
        self.assertTrue(all(t.status == jobs.TARGET_PENDING for t in targets))

    def test_idempotency_returns_existing_job(self) -> None:
        spec = _spec(key="manual-key")
        first = jobs.enqueue_job(spec, db_path=self.db_path)
        second = jobs.enqueue_job(spec, db_path=self.db_path)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(jobs.list_jobs(db_path=self.db_path)), 1)

    def test_auto_idempotency_key_collapses_duplicate_payloads(self) -> None:
        first = jobs.enqueue_job(_spec(), db_path=self.db_path)
        second = jobs.enqueue_job(_spec(), db_path=self.db_path)
        self.assertEqual(first.id, second.id)

    def test_claim_excludes_in_flight_accounts(self) -> None:
        spec = _spec(targets=[("acct-1", "f1", None), ("acct-1", "f2", None),
                              ("acct-2", "f1", None)])
        jobs.enqueue_job(spec, db_path=self.db_path)

        claimed = jobs.claim_next_targets(limit=5, excluded_accounts=["acct-1"],
                                          db_path=self.db_path)
        self.assertEqual([t.account_ref for t in claimed], ["acct-2"])
        # The two acct-1 targets remain pending.
        remaining = [t for t in jobs.list_targets(claimed[0].job_id, db_path=self.db_path)
                     if t.status == jobs.TARGET_PENDING]
        self.assertEqual(len(remaining), 2)

    def test_target_success_advances_job_counters(self) -> None:
        job = jobs.enqueue_job(
            _spec(targets=[("acct-1", "f1", None), ("acct-2", "f1", None)]),
            db_path=self.db_path,
        )
        claimed = jobs.claim_next_targets(limit=2, db_path=self.db_path)
        self.assertEqual(len(claimed), 2)

        for target in claimed:
            jobs.mark_target_success(target.id, db_path=self.db_path)

        finalised = jobs.get_job(job.id, db_path=self.db_path)
        self.assertEqual(finalised.status, jobs.JOB_SUCCEEDED)
        self.assertEqual(finalised.completed_targets, 2)
        self.assertIsNotNone(finalised.finished_at)

    def test_failure_finalises_job_as_failed(self) -> None:
        job = jobs.enqueue_job(_spec(), db_path=self.db_path)
        target = jobs.claim_next_targets(limit=1, db_path=self.db_path)[0]
        jobs.mark_target_failed(target.id, "boom", db_path=self.db_path)

        finalised = jobs.get_job(job.id, db_path=self.db_path)
        self.assertEqual(finalised.status, jobs.JOB_FAILED)
        self.assertEqual(finalised.failed_targets, 1)

    def test_retry_makes_target_claimable_again(self) -> None:
        jobs.enqueue_job(_spec(), db_path=self.db_path)
        first = jobs.claim_next_targets(limit=1, db_path=self.db_path)[0]
        jobs.mark_target_retry(first.id, "transient", db_path=self.db_path)

        second_round = jobs.claim_next_targets(limit=1, db_path=self.db_path)
        self.assertEqual(len(second_round), 1)
        self.assertEqual(second_round[0].id, first.id)
        # Attempts increment on each claim.
        self.assertEqual(second_round[0].attempts, 2)

    def test_cancel_job_stops_pending_targets(self) -> None:
        job = jobs.enqueue_job(
            _spec(targets=[("acct-1", "f1", None), ("acct-2", "f1", None)]),
            db_path=self.db_path,
        )
        # Claim one to running; the other stays pending.
        jobs.claim_next_targets(limit=1, db_path=self.db_path)
        jobs.cancel_job(job.id, db_path=self.db_path)

        finalised = jobs.get_job(job.id, db_path=self.db_path)
        self.assertEqual(finalised.status, jobs.JOB_CANCELLED)
        statuses = {t.status for t in jobs.list_targets(job.id, db_path=self.db_path)}
        # Both running and pending targets become cancelled.
        self.assertIn(jobs.TARGET_CANCELLED, statuses)
        self.assertNotIn(jobs.TARGET_PENDING, statuses)

    def test_targets_are_deduplicated_within_a_spec(self) -> None:
        # Same (account, file) listed twice → only one row created.
        spec = _spec(targets=[("acct-1", "f1", None), ("acct-1", "f1", None)])
        job = jobs.enqueue_job(spec, db_path=self.db_path)
        targets = jobs.list_targets(job.id, db_path=self.db_path)
        self.assertEqual(len(targets), 1)

    def test_enqueue_rejects_empty_targets(self) -> None:
        with self.assertRaises(ValueError):
            jobs.enqueue_job(jobs.JobSpec(platform="x", payload={}, targets=[]),
                             db_path=self.db_path)

    def test_list_jobs_can_filter_by_status(self) -> None:
        # claim_next_targets walks targets by id, so the first-enqueued job is
        # the one that ends up running.
        running_job = jobs.enqueue_job(
            _spec(key="a", targets=[("acct-1", "f1", None)]),
            db_path=self.db_path,
        )
        jobs.enqueue_job(
            _spec(key="b", targets=[("acct-2", "f1", None)]),
            db_path=self.db_path,
        )
        jobs.claim_next_targets(limit=1, db_path=self.db_path)

        pending = jobs.list_jobs(status=jobs.JOB_PENDING, db_path=self.db_path)
        running = jobs.list_jobs(status=jobs.JOB_RUNNING, db_path=self.db_path)
        self.assertEqual({job.idempotency_key for job in pending}, {"b"})
        self.assertEqual({job.idempotency_key for job in running}, {"a"})
        self.assertEqual(running[0].id, running_job.id)


if __name__ == "__main__":
    unittest.main()
