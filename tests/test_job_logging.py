"""Unit tests for myUtils.job_logging."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from myUtils import job_logging
from utils.log import loguru_logger


class JobLoggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = patch.object(
            job_logging, "JOB_LOG_DIR", Path(self._tmp.name) / "jobs"
        )
        self._patch.start()

    def tearDown(self) -> None:
        # Make sure we don't leak loguru handlers between tests.
        for job_id in list(job_logging._JOB_SINKS.keys()):
            job_logging.close_job_sink(job_id)
        self._patch.stop()
        self._tmp.cleanup()

    def test_bind_returns_logger_with_correlation_extras(self) -> None:
        log = job_logging.bind_job_logger(
            job_id=42, target_id=7, platform="douyin",
            account_ref="acct-1", attempt=2,
        )
        captured: list = []

        def _sink(message):
            captured.append(message.record["extra"])

        handler_id = loguru_logger.add(_sink, level="DEBUG")
        try:
            log.info("hello")
        finally:
            loguru_logger.remove(handler_id)

        self.assertEqual(len(captured), 1)
        extra = captured[0]
        self.assertEqual(extra["job_id"], 42)
        self.assertEqual(extra["target_id"], 7)
        self.assertEqual(extra["platform"], "douyin")
        self.assertEqual(extra["account_ref"], "acct-1")
        self.assertEqual(extra["attempt"], 2)
        # Worker records always carry business_name='worker' so they reach
        # the worker.log sink configured by utils.log.
        self.assertEqual(extra["business_name"], "worker")

    def test_per_job_sink_is_created_lazily_and_idempotently(self) -> None:
        log = job_logging.bind_job_logger(job_id=11)
        log.info("first line")
        # Calling bind again must not open a second sink.
        log_again = job_logging.bind_job_logger(job_id=11)
        log_again.info("second line")

        self.assertIn(11, job_logging._JOB_SINKS)
        path = job_logging.job_log_path(11)
        # Loguru flushes asynchronously when enqueue=True, so we close the
        # sink to force a flush before reading.
        job_logging.close_job_sink(11)
        body = path.read_text()
        self.assertIn("first line", body)
        self.assertIn("second line", body)

    def test_per_job_sink_only_captures_records_for_that_job(self) -> None:
        log_a = job_logging.bind_job_logger(job_id=1)
        log_b = job_logging.bind_job_logger(job_id=2)
        log_a.info("for job 1")
        log_b.info("for job 2")

        job_logging.close_job_sink(1)
        job_logging.close_job_sink(2)

        body1 = job_logging.job_log_path(1).read_text()
        body2 = job_logging.job_log_path(2).read_text()
        self.assertIn("for job 1", body1)
        self.assertNotIn("for job 2", body1)
        self.assertIn("for job 2", body2)
        self.assertNotIn("for job 1", body2)

    def test_close_job_sink_is_safe_to_call_twice(self) -> None:
        job_logging.bind_job_logger(job_id=99).info("x")
        job_logging.close_job_sink(99)
        job_logging.close_job_sink(99)  # must not raise

    def test_json_format_serialises_extras(self) -> None:
        # Loguru passes a dict-like record into format callables, so a plain
        # dict is the closest match for our fake.
        from datetime import datetime

        record = {
            "time": datetime(2026, 1, 2, 3, 4, 5),
            "level": type("L", (), {"name": "INFO"})(),
            "message": "hello",
            "extra": {
                "business_name": "worker",
                "job_id": 7,
                "target_id": 3,
                "platform": "douyin",
                "account_ref": "acct-1",
                "attempt": 2,
            },
        }
        line = job_logging._format_json(record)
        payload = json.loads(line)
        self.assertEqual(payload["job_id"], 7)
        self.assertEqual(payload["target_id"], 3)
        self.assertEqual(payload["platform"], "douyin")
        self.assertEqual(payload["msg"], "hello")
        self.assertEqual(payload["level"], "INFO")


if __name__ == "__main__":
    unittest.main()
