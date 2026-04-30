"""Tests for the worker runtime: per-job log files and the CLI entry point."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db.createTable as create_table
from myUtils import job_logging, jobs
from myUtils.worker import (
    PublishWorker,
    RetryPolicy,
    WorkerConfig,
    _build_arg_parser,
    _cli,
)


def _spec(targets):
    return jobs.JobSpec(
        platform="douyin",
        payload={"title": "t"},
        targets=targets,
        idempotency_key=f"test-{id(targets)}",
    )


class StructuredLoggingTests(unittest.TestCase):
    """Worker emits correlated log records, one file per job, closed on terminal."""

    def setUp(self) -> None:
        self._db_tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._db_tmp.name) / "worker.db"
        create_table.bootstrap(self.db_path)

        self._log_tmp = tempfile.TemporaryDirectory()
        self._log_patch = patch.object(
            job_logging, "JOB_LOG_DIR", Path(self._log_tmp.name) / "jobs"
        )
        self._log_patch.start()

    def tearDown(self) -> None:
        for job_id in list(job_logging._JOB_SINKS.keys()):
            job_logging.close_job_sink(job_id)
        self._log_patch.stop()
        self._log_tmp.cleanup()
        self._db_tmp.cleanup()

    def _drain(self, executor) -> None:
        config = WorkerConfig(
            poll_interval=0.001,
            batch_size=2,
            max_concurrent=2,
            retry=RetryPolicy(max_attempts=2,
                              base_backoff_seconds=0.001,
                              max_backoff_seconds=0.01),
        )
        worker = PublishWorker(executor, config=config, db_path=self.db_path)
        asyncio.run(worker.drain())

    def test_per_job_log_file_is_written_and_closed(self) -> None:
        job = jobs.enqueue_job(
            _spec([("acct-1", "f1", None)]),
            db_path=self.db_path,
        )

        async def executor(platform, payload, target):
            return None

        self._drain(executor)

        # The job reached terminal status, so the sink must have been closed.
        self.assertNotIn(job.id, job_logging._JOB_SINKS)

        path = job_logging.job_log_path(job.id)
        self.assertTrue(path.exists(), "per-job log file must exist")
        body = path.read_text()
        self.assertIn(f"job_id={job.id}", body)
        self.assertIn("target claimed", body)
        self.assertIn("target succeeded", body)

    def test_failed_target_gets_correlated_error_record(self) -> None:
        jobs.enqueue_job(
            _spec([("acct-1", "f1", None)]),
            db_path=self.db_path,
        )

        async def executor(platform, payload, target):
            raise RuntimeError("boom")

        self._drain(executor)

        path = job_logging.job_log_path(1)
        body = path.read_text()
        self.assertIn("target_id=1", body)
        self.assertIn("failed permanently", body)
        self.assertIn("RuntimeError: boom", body)


class CliEntryPointTests(unittest.TestCase):
    def test_arg_parser_accepts_flags(self) -> None:
        parser = _build_arg_parser()
        args = parser.parse_args([
            "--once",
            "--poll-interval", "0.5",
            "--batch-size", "8",
            "--max-concurrent", "5",
            "--max-attempts", "4",
        ])
        self.assertTrue(args.once)
        self.assertEqual(args.poll_interval, 0.5)
        self.assertEqual(args.batch_size, 8)
        self.assertEqual(args.max_concurrent, 5)
        self.assertEqual(args.max_attempts, 4)

    def test_cli_drains_pending_jobs_in_once_mode(self) -> None:
        # End-to-end: stage one pending job, run --once, expect it to finish.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "cli.db"
            create_table.bootstrap(db_path)

            jobs.enqueue_job(
                _spec([("acct-1", "f1", None)]),
                db_path=db_path,
            )

            # Override the default executor so we don't try to spin up a
            # browser. We patch the symbol the CLI looks up directly.
            calls = []

            async def fake_executor(platform, payload, target):
                calls.append((platform, target.id))

            with patch("myUtils.worker.default_executor", fake_executor):
                exit_code = _cli([
                    "--once",
                    "--poll-interval", "0.001",
                    "--db-path", str(db_path),
                    "--max-attempts", "1",
                ])

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
