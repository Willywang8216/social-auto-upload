"""Tests for durable TikTok review persistence."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

import db.createTable as create_table

if "conf" not in sys.modules:
    conf_module = types.ModuleType("conf")
    conf_module.BASE_DIR = str(Path(__file__).resolve().parent.parent)
    sys.modules["conf"] = conf_module

from myUtils import tiktok_review


class TikTokReviewPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        create_table.bootstrap(self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_create_and_complete_oauth_request(self) -> None:
        request = tiktok_review.create_oauth_request(
            state_token="state-1",
            profile_id=None,
            account_id=None,
            account_name="brand-main",
            redirect_uri="https://up.iamwillywang.com/oauth/tiktok/callback",
            scopes=["user.info.basic", "video.publish"],
            db_path=self.db_path,
        )
        self.assertEqual(request.status, "started")

        completed = tiktok_review.complete_oauth_request(
            "state-1",
            status="completed",
            result={"displayName": "Demo"},
            db_path=self.db_path,
        )
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.result, {"displayName": "Demo"})

    def test_review_events_return_latest_and_recent(self) -> None:
        tiktok_review.add_review_event(
            event_type="callback",
            status="ok",
            account_name="brand-main",
            payload={"displayName": "Demo"},
            db_path=self.db_path,
        )
        tiktok_review.add_review_event(
            event_type="webhook",
            status="received",
            signature_verified=True,
            signature_status="verified",
            payload={"event": "post.publish.publicly_available"},
            db_path=self.db_path,
        )

        latest_webhook = tiktok_review.latest_review_event("webhook", db_path=self.db_path)
        self.assertTrue(latest_webhook.signature_verified)
        recent = tiktok_review.list_recent_review_events(db_path=self.db_path)
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0].event_type, "webhook")


if __name__ == "__main__":
    unittest.main()
