"""Tests for myUtils.ops_alerts.public_app_origin.

The resolver decides where operator-facing links point. It exists because the
digest and the worker both used to read only SAU_PUBLIC_APP_URL while the app
stores its origin in SAU_PUBLIC_BASE_URL, so every link in both channels was
silently omitted on a box that had the second variable set and not the first.
"""

from __future__ import annotations

import sys
import os
import unittest
from unittest.mock import patch

from myUtils import ops_alerts


class PublicAppOriginTests(unittest.TestCase):
    def setUp(self) -> None:
        # Never let the host's own configuration decide the outcome.
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("SAU_PUBLIC_APP_URL", None)
        os.environ.pop("SAU_PUBLIC_BASE_URL", None)
        self.addCleanup(self._env.stop)

    def test_unconfigured_yields_empty_so_callers_omit_the_link(self):
        self.assertEqual(ops_alerts.public_app_origin(), "")

    def test_falls_back_to_the_apps_own_public_base_url(self):
        with patch.dict(os.environ, {"SAU_PUBLIC_BASE_URL": "https://socialupload.example.com"}):
            self.assertEqual(
                ops_alerts.public_app_origin(), "https://socialupload.example.com"
            )

    def test_app_url_wins_when_both_are_set(self):
        with patch.dict(
            os.environ,
            {
                "SAU_PUBLIC_APP_URL": "https://app.example.net",
                "SAU_PUBLIC_BASE_URL": "https://socialupload.example.com",
            },
        ):
            self.assertEqual(ops_alerts.public_app_origin(), "https://app.example.net")

    def test_explicit_argument_wins_over_the_environment(self):
        with patch.dict(os.environ, {"SAU_PUBLIC_BASE_URL": "https://socialupload.example.com"}):
            self.assertEqual(
                ops_alerts.public_app_origin("https://explicit.example.org/"),
                "https://explicit.example.org",
            )

    def test_explicit_empty_string_forces_no_link(self):
        # The digest CLI passes --app-url through, and an explicit "" must mean
        # "no link" rather than falling back to a configured default.
        with patch.dict(os.environ, {"SAU_PUBLIC_BASE_URL": "https://socialupload.example.com"}):
            self.assertEqual(ops_alerts.public_app_origin(""), "")

    def test_trailing_slashes_are_stripped(self):
        with patch.dict(os.environ, {"SAU_PUBLIC_BASE_URL": "https://socialupload.example.com///"}):
            self.assertEqual(
                ops_alerts.public_app_origin(), "https://socialupload.example.com"
            )

    def test_whitespace_only_value_is_treated_as_unset(self):
        with patch.dict(os.environ, {"SAU_PUBLIC_APP_URL": "   ", "SAU_PUBLIC_BASE_URL": ""}):
            self.assertEqual(ops_alerts.public_app_origin(), "")


class _FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class TelegramChunkingTests(unittest.TestCase):
    """Telegram caps sendMessage at 4096 characters.

    A longer payload is rejected outright, and since alerting is best-effort the
    failure is silent — which is how the daily digest (5-6k characters, one line
    per scheduled target) would have failed to arrive every single morning.
    """

    def setUp(self) -> None:
        self._env = patch.dict(
            os.environ,
            {"SAU_ALERT_TELEGRAM_BOT_TOKEN": "t", "SAU_ALERT_TELEGRAM_CHAT_ID": "1"},
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def _capture(self, body: str):
        calls: list[dict] = []

        class FakeRequests:
            @staticmethod
            def post(url, json=None, timeout=None):
                calls.append(json)
                return _FakeResponse(200)

        # Deliberately exercise the real transport with a mocked HTTP layer.
        # The sender otherwise refuses to alert from a test process.
        with patch.dict(os.environ, {"SAU_ALERTS_ALLOW_UNDER_TEST": "1"}):
            with patch.object(ops_alerts, "requests", FakeRequests):
                ok = ops_alerts._send_telegram("SUBJ", body)
        return ok, calls

    def test_short_alert_is_one_message(self):
        ok, calls = self._capture("a short body")
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertIn("SUBJ", calls[0]["text"])

    def test_long_alert_is_split_across_messages(self):
        body = "\n".join(f"media-group:{i} something scheduled at 21:0{i % 10}" for i in range(200))
        ok, calls = self._capture(body)
        self.assertTrue(ok)
        self.assertGreater(len(calls), 1, "a 5k+ body must not be sent as one message")
        self.assertTrue(all(len(call["text"]) <= 4096 for call in calls))
        # Every line is still delivered, in order.
        delivered = "\n".join(call["text"] for call in calls)
        for probe in ("media-group:0 ", "media-group:199 "):
            self.assertIn(probe, delivered)

    def test_split_parts_are_numbered_so_none_looks_like_the_whole_message(self):
        body = "\n".join(f"line {i}" + "x" * 90 for i in range(100))
        _ok, calls = self._capture(body)
        self.assertIn("[1/", calls[0]["text"])
        self.assertIn("(cont.)", calls[1]["text"])

    def test_an_failed_chunk_does_not_silently_pass(self):
        class FailingRequests:
            @staticmethod
            def post(url, json=None, timeout=None):
                return _FakeResponse(400)

        with patch.dict(os.environ, {"SAU_ALERTS_ALLOW_UNDER_TEST": "1"}):
            with patch.object(ops_alerts, "requests", FailingRequests):
                with self.assertRaises(RuntimeError):
                    ops_alerts._send_telegram("SUBJ", "body")


if __name__ == "__main__":
    unittest.main()


class TestRunNeverAlertsProduction(unittest.TestCase):
    """A test run must never deliver to the operator's real Telegram.

    The suite loads the repo .env, so a test that exercises the failure path can
    pick up the live bot token. That is how fixtures like

        [SAU] Publish failed: bluesky target #1 (job #1) ... bluesky said no

    reached the operator's phone - job #1 and account fb-bluesky do not exist in
    the database, they are literals in the test files. tests/conftest.py blanks
    the credentials, but a conftest only loads when pytest collects that
    directory, so the sender carries its own guard as well.
    """

    def test_pytest_is_detected(self):
        self.assertIn("pytest", sys.modules)
        self.assertTrue(ops_alerts._is_under_test())

    def test_explicit_disable_switch(self):
        with patch.dict(os.environ, {"SAU_ALERTS_DISABLED": "1"}):
            self.assertTrue(ops_alerts._is_under_test())

    def test_telegram_send_refuses_under_test_even_with_real_credentials(self):
        # Simulate the exact pre-fix condition: a real-looking token is present.
        with patch.dict(
            os.environ,
            {
                "SAU_ALERT_TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN-NOT-REAL",
                "SAU_ALERT_TELEGRAM_CHAT_ID": "9999999999",
            },
        ):
            sent = []
            with patch.object(
                ops_alerts, "requests", create=True
            ) as fake_requests:
                fake_requests.post.side_effect = lambda *a, **k: sent.append(a)
                self.assertFalse(
                    ops_alerts._send_telegram("subject", "body")
                )
            self.assertEqual(sent, [], "must not attempt any HTTP call")

    def test_send_ops_alert_is_false_under_test(self):
        with patch.dict(
            os.environ,
            {
                "SAU_ALERT_TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN-NOT-REAL",
                "SAU_ALERT_TELEGRAM_CHAT_ID": "9999999999",
            },
        ):
            self.assertFalse(ops_alerts.send_ops_alert(subject="s", body="b"))
