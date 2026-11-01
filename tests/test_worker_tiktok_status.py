"""Regression tests for TikTok asynchronous publish-status handling.

A TikTok Direct Post is accepted asynchronously: the init call returns a
``publish_id`` and the real outcome arrives later from the status endpoint.
The worker used to ``break`` on a terminal ``failed`` status and return
normally, so a rejected post was recorded as *succeeded*. These tests pin the
correct behaviour: terminal failure raises, and an unresolved poll window
never reports success.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from myUtils import jobs
from myUtils import prepared_publishers
from myUtils import delivery
from myUtils.worker import _publish_prepared_tiktok


def _target(**overrides):
    base = dict(
        id=7,
        job_id=3,
        account_ref="account:42",
        file_ref="videoFile/clip.mp4",
        schedule_at=None,
        status=jobs.TARGET_RUNNING,
        attempts=1,
    )
    base.update(overrides)
    return jobs.Target(**base)


_PUBLISH_RESULT = {
    "creator_info": {"data": {}},
    "publish": {"data": {"publish_id": "pub-1"}},
    "access_token": "tt-token",
    "updated_config": None,
}




class TikTokStatusTests(unittest.TestCase):
    def _run(self, statuses, *, publish_result=None, resume_pending=False):
        """Drive separate status-poll attempts without recreating the upload."""
        if isinstance(statuses, dict):
            statuses = [statuses]
        responses = list(statuses)
        rounds = max(1, len(responses)) if resume_pending else 1

        def _fetch(_token, _publish_id, **_kwargs):
            if responses:
                result = responses.pop(0)
                if isinstance(result, BaseException):
                    raise result
                return result
            return {"data": {"status": "processing"}}

        account = SimpleNamespace(id=42, config={}, nickname="", account_name="tt")
        recorded = []
        payload = {"title": "clip"}
        with patch.object(prepared_publishers, "publish_tiktok_sync",
                          return_value=publish_result or _PUBLISH_RESULT) as submit, \
             patch.object(prepared_publishers, "fetch_tiktok_publish_status", side_effect=_fetch), \
             patch.object(prepared_publishers, "_ensure_tiktok_access_token", return_value=("tt-token", None)), \
             patch.object(jobs, "upsert_tiktok_publish_status",
                          side_effect=lambda *a, **k: recorded.append(k.get("status"))):
            for _ in range(rounds):
                try:
                    asyncio.run(_publish_prepared_tiktok("tiktok", payload, _target(),
                                                        account=account, account_file=None))
                except delivery.DeliveryPending:
                    if not resume_pending:
                        raise
                else:
                    submit.assert_called_once()
                    self.assertEqual(payload["_delivery"]["provider_receipt"]["status"], "delivered")
                    return recorded
        self.fail("script ended without a terminal delivery outcome")

    def test_terminal_failed_status_raises_non_retryable(self):
        with self.assertRaises(prepared_publishers.PreparedPublishError) as raised:
            self._run([{"data": {"status": "failed", "fail_reason": "duration_check_failed"}}])
        self.assertFalse(raised.exception.retryable)
        self.assertIn("duration_check_failed", str(raised.exception))

    def test_publish_complete_returns_normally(self):
        recorded = self._run([{"data": {"status": "publish_complete"}}])
        self.assertIn("publish_complete", recorded)

    def test_poll_window_exhausted_does_not_report_success(self):
        # An in-progress receipt releases the worker slot for a later poll.
        with self.assertRaises(delivery.DeliveryPending):
            self._run([{"data": {"status": "processing"}}])

    def test_transient_fetch_errors_are_tolerated_then_complete(self):
        recorded = self._run([
            RuntimeError("network hiccup"),
            {"data": {"status": "processing"}},
            {"data": {"status": "publish_complete"}},
        ], resume_pending=True)
        self.assertIn("publish_complete", recorded)

    def test_rate_limited_poll_preserves_retry_hint(self):
        error = prepared_publishers.PreparedPublishError("HTTP 429", retryable=True,
                                                        details={"retryAfter": "300"})
        with self.assertRaises(delivery.DeliveryPending) as raised:
            self._run([error])
        self.assertEqual(raised.exception.details["retryAfter"], 300)


if __name__ == "__main__":
    unittest.main()