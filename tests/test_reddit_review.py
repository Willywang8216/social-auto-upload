"""Tests for Reddit OAuth request persistence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import db.createTable as create_table
from myUtils import profiles, reddit_review


class RedditOAuthRequestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        create_table.bootstrap(self.db_path)
        self.profile = profiles.create_profile("TestProfile", db_path=self.db_path)
        self.account = profiles.add_account(
            self.profile.id,
            profiles.PLATFORM_REDDIT,
            "test-reddit-account",
            db_path=self.db_path,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_create_and_get_oauth_request(self):
        req = reddit_review.create_oauth_request(
            state_token="state-123",
            profile_id=self.profile.id,
            account_id=self.account.id,
            account_name="test-account",
            redirect_uri="https://example.com/callback",
            scopes=["identity", "submit"],
            db_path=self.db_path,
        )
        self.assertEqual(req.state_token, "state-123")
        self.assertEqual(req.profile_id, self.profile.id)
        self.assertEqual(req.account_id, self.account.id)
        self.assertEqual(req.account_name, "test-account")
        self.assertEqual(req.status, "started")
        self.assertEqual(req.scopes, ["identity", "submit"])

        fetched = reddit_review.get_oauth_request("state-123", db_path=self.db_path)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.state_token, "state-123")
        self.assertEqual(fetched.account_name, "test-account")

    def test_get_nonexistent_request_returns_none(self):
        result = reddit_review.get_oauth_request("nonexistent", db_path=self.db_path)
        self.assertIsNone(result)

    def test_complete_oauth_request_updates_status(self):
        reddit_review.create_oauth_request(
            state_token="state-456",
            profile_id=self.profile.id,
            account_id=self.account.id,
            account_name="test",
            redirect_uri="https://example.com/cb",
            scopes=["identity"],
            db_path=self.db_path,
        )
        completed = reddit_review.complete_oauth_request(
            "state-456",
            status="completed",
            result={"access_token": "token", "refresh_token": "refresh"},
            db_path=self.db_path,
        )
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.result["access_token"], "token")
        self.assertIsNotNone(completed.completed_at)

    def test_complete_oauth_request_with_error(self):
        reddit_review.create_oauth_request(
            state_token="state-789",
            profile_id=self.profile.id,
            account_id=self.account.id,
            account_name="test",
            redirect_uri="https://example.com/cb",
            scopes=["identity"],
            db_path=self.db_path,
        )
        completed = reddit_review.complete_oauth_request(
            "state-789",
            status="error",
            error_text="User denied access",
            db_path=self.db_path,
        )
        self.assertEqual(completed.status, "error")
        self.assertEqual(completed.error_text, "User denied access")

    def test_complete_nonexistent_request_raises(self):
        with self.assertRaises(LookupError):
            reddit_review.complete_oauth_request(
                "nonexistent",
                status="completed",
                db_path=self.db_path,
            )

    def test_latest_oauth_request_returns_most_recent(self):
        reddit_review.create_oauth_request(
            state_token="state-1",
            profile_id=self.profile.id,
            account_id=self.account.id,
            account_name="test",
            redirect_uri="https://example.com/cb",
            scopes=["identity"],
            db_path=self.db_path,
        )
        reddit_review.create_oauth_request(
            state_token="state-2",
            profile_id=self.profile.id,
            account_id=self.account.id,
            account_name="test",
            redirect_uri="https://example.com/cb",
            scopes=["identity"],
            db_path=self.db_path,
        )
        latest = reddit_review.latest_oauth_request(account_id=self.account.id, db_path=self.db_path)
        self.assertIsNotNone(latest)
        self.assertEqual(latest.state_token, "state-2")

    def test_latest_oauth_request_with_no_results(self):
        result = reddit_review.latest_oauth_request(account_id=999, db_path=self.db_path)
        self.assertIsNone(result)

    def test_oauth_request_to_dict(self):
        req = reddit_review.create_oauth_request(
            state_token="state-dict",
            profile_id=self.profile.id,
            account_id=self.account.id,
            account_name="test",
            redirect_uri="https://example.com/cb",
            scopes=["identity"],
            db_path=self.db_path,
        )
        d = req.to_dict()
        self.assertEqual(d["state_token"], "state-dict")
        self.assertEqual(d["account_id"], self.account.id)
        self.assertIn("scopes", d)


if __name__ == "__main__":
    unittest.main()
