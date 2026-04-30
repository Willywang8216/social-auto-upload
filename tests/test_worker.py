"""End-to-end tests for the async publish worker.

These tests exercise the full worker lifecycle (claim → run → mark) against a
fake executor so we never spin up real Playwright browsers.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import db.createTable as create_table
from myUtils import jobs
from myUtils.worker import (
    PublishWorker,
    RetryPolicy,
    WorkerConfig,
)


def _spec(targets):
    return jobs.JobSpec(platform="douyin", payload={"title": "t"}, targets=targets,
                        idempotency_key=f"key-{id(targets)}")


class WorkerLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "worker.db"
        create_table.bootstrap(self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _drain(self, executor, *, retry: RetryPolicy | None = None,
               max_concurrent: int = 4) -> None:
        config = WorkerConfig(
            poll_interval=0.001,
            batch_size=4,
            max_concurrent=max_concurrent,
            retry=retry or RetryPolicy(max_attempts=3, base_backoff_seconds=0.001,
                                        max_backoff_seconds=0.01),
        )
        worker = PublishWorker(executor, config=config, db_path=self.db_path)
        asyncio.run(worker.drain())

    def test_all_targets_succeed(self) -> None:
        spec = _spec([("acct-1", "f1", None), ("acct-2", "f1", None),
                      ("acct-1", "f2", None)])
        job = jobs.enqueue_job(spec, db_path=self.db_path)
        seen: list[tuple[str, str]] = []

        async def executor(platform, payload, target):
            seen.append((target.account_ref, target.file_ref))

        self._drain(executor)

        finalised = jobs.get_job(job.id, db_path=self.db_path)
        self.assertEqual(finalised.status, jobs.JOB_SUCCEEDED)
        self.assertEqual(finalised.completed_targets, 3)
        # All three (account, file) pairs were exercised exactly once.
        self.assertEqual(Counter(seen),
                         Counter([("acct-1", "f1"), ("acct-2", "f1"), ("acct-1", "f2")]))

    def test_retry_then_succeed(self) -> None:
        jobs.enqueue_job(_spec([("acct-1", "f1", None)]), db_path=self.db_path)

        attempts = {"count": 0}

        async def executor(platform, payload, target):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise RuntimeError("transient")

        self._drain(executor, retry=RetryPolicy(max_attempts=5,
                                                 base_backoff_seconds=0.001,
                                                 max_backoff_seconds=0.01))

        target = jobs.list_targets(1, db_path=self.db_path)[0]
        self.assertEqual(target.status, jobs.TARGET_SUCCEEDED)
        self.assertEqual(target.attempts, 3)
        self.assertEqual(jobs.get_job(1, db_path=self.db_path).status, jobs.JOB_SUCCEEDED)

    def test_permanent_failure_after_max_attempts(self) -> None:
        jobs.enqueue_job(_spec([("acct-1", "f1", None)]), db_path=self.db_path)

        async def executor(platform, payload, target):
            raise RuntimeError("always fails")

        self._drain(executor, retry=RetryPolicy(max_attempts=2,
                                                 base_backoff_seconds=0.001,
                                                 max_backoff_seconds=0.01))

        target = jobs.list_targets(1, db_path=self.db_path)[0]
        self.assertEqual(target.status, jobs.TARGET_FAILED)
        self.assertEqual(target.attempts, 2)
        self.assertIn("always fails", target.last_error or "")
        self.assertEqual(jobs.get_job(1, db_path=self.db_path).status, jobs.JOB_FAILED)

    def test_same_account_runs_serially(self) -> None:
        # Two targets on the same account — must not overlap.
        jobs.enqueue_job(
            _spec([("shared", "f1", None), ("shared", "f2", None)]),
            db_path=self.db_path,
        )

        running = 0
        peak = 0
        gate = asyncio.Lock()

        async def executor(platform, payload, target):
            nonlocal running, peak
            async with gate:
                running += 1
                if running > peak:
                    peak = running
            await asyncio.sleep(0.01)
            async with gate:
                running -= 1

        self._drain(executor, max_concurrent=4)
        self.assertEqual(peak, 1, "same-account targets must serialise")

    def test_different_accounts_run_in_parallel(self) -> None:
        jobs.enqueue_job(
            _spec([("acct-a", "f1", None), ("acct-b", "f1", None),
                   ("acct-c", "f1", None)]),
            db_path=self.db_path,
        )

        running = 0
        peak = 0
        gate = asyncio.Lock()

        async def executor(platform, payload, target):
            nonlocal running, peak
            async with gate:
                running += 1
                if running > peak:
                    peak = running
            await asyncio.sleep(0.05)
            async with gate:
                running -= 1

        self._drain(executor, max_concurrent=3)
        self.assertGreaterEqual(peak, 2,
            "different-account targets must overlap")

    def test_cancel_mid_run_does_not_let_executor_resurrect_target(self) -> None:
        # Simulates the QA-found cancel race: target is running, the user
        # cancels the job, then the executor finishes "successfully". The
        # final state must show both the job AND its targets as cancelled.
        job = jobs.enqueue_job(
            _spec([("acct-1", "f1", None)]),
            db_path=self.db_path,
        )

        cancel_observed = asyncio.Event()
        executor_finished = asyncio.Event()

        async def executor(platform, payload, target):
            # Wait for the test driver to cancel before "finishing".
            await cancel_observed.wait()
            executor_finished.set()
            # Returning normally would drive mark_target_success; without
            # the guard this would resurrect the cancelled target.

        async def drive() -> None:
            config = WorkerConfig(
                poll_interval=0.001,
                batch_size=1,
                max_concurrent=1,
                retry=RetryPolicy(max_attempts=1, base_backoff_seconds=0.001,
                                   max_backoff_seconds=0.001),
            )
            worker = PublishWorker(executor, config=config, db_path=self.db_path)
            drain_task = asyncio.create_task(worker.drain())

            # Wait until the worker has actually claimed the target.
            for _ in range(50):
                target = jobs.list_targets(job.id, db_path=self.db_path)[0]
                if target.status == jobs.TARGET_RUNNING:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(target.status, jobs.TARGET_RUNNING)

            jobs.cancel_job(job.id, db_path=self.db_path)
            cancel_observed.set()
            await executor_finished.wait()
            await drain_task

        asyncio.run(drive())

        finalised = jobs.get_job(job.id, db_path=self.db_path)
        target_after = jobs.list_targets(job.id, db_path=self.db_path)[0]
        self.assertEqual(finalised.status, jobs.JOB_CANCELLED)
        self.assertEqual(target_after.status, jobs.TARGET_CANCELLED,
            "executor's late success must not overwrite a cancelled target")
        self.assertEqual(finalised.completed_targets, 0)
        self.assertEqual(finalised.failed_targets, 0)


if __name__ == "__main__":
    unittest.main()
