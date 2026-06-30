"""Tests for prepared-campaign worker dispatch."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import db.createTable as create_table

if "conf" not in sys.modules:
    conf_module = types.ModuleType("conf")
    conf_module.BASE_DIR = str(Path(__file__).resolve().parent.parent)
    conf_module.XHS_SERVER = "http://127.0.0.1:11901"
    conf_module.LOCAL_CHROME_PATH = ""
    conf_module.LOCAL_CHROME_HEADLESS = True
    conf_module.DEBUG_MODE = False
    sys.modules["conf"] = conf_module

try:
    from myUtils import jobs, profiles, worker
    WORKER_IMPORT_ERROR = None
except ModuleNotFoundError as exc:  # pragma: no cover - environment-specific
    jobs = profiles = worker = None
    WORKER_IMPORT_ERROR = exc


@unittest.skipUnless(WORKER_IMPORT_ERROR is None, f"worker dependencies unavailable: {WORKER_IMPORT_ERROR}")
class PreparedWorkerDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "worker.db"
        create_table.bootstrap(self.db_path)
        self.profile = profiles.create_profile("Brand", db_path=self.db_path)
        self.account = profiles.add_account(
            self.profile.id,
            profiles.PLATFORM_TWITTER,
            "brand-main",
            cookie_path="/tmp/twitter-cookie.json",
            db_path=self.db_path,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_account_ref_resolves_to_registered_cookie_path(self) -> None:
        with patch.object(worker.profile_registry, "get_account", return_value=self.account):
            resolved = worker._resolve_account_path(f"account:{self.account.id}")
        self.assertEqual(resolved, Path("/tmp/twitter-cookie.json"))

    def test_prepared_twitter_dispatch_uses_artifact_paths(self) -> None:
        target = jobs.Target(
            id=1,
            job_id=1,
            account_ref=f"account:{self.account.id}",
            file_ref="campaign_post:1",
            schedule_at=None,
            status=jobs.TARGET_RUNNING,
            attempts=1,
        )
        payload = {
            "campaignId": 10,
            "campaignPostId": 22,
            "message": "hello world",
            "draft": {"message": "hello world", "hashtags": ["#a", "#b", "#c"]},
            "artifacts": [
                {"local_path": "/tmp/a.jpg"},
                {"local_path": "/tmp/a.jpg"},
                {"local_path": "/tmp/b.jpg"},
            ],
        }
        captured = {}

        async def fake_run_platform_upload(platform, payload, target, **kwargs):
            captured["platform"] = platform
            captured["payload"] = payload
            captured["thread_file_paths"] = kwargs["thread_file_paths"]

        with patch.object(worker, "_run_platform_upload", fake_run_platform_upload):
            asyncio.run(
                worker._publish_prepared_twitter(
                    "twitter",
                    payload,
                    target,
                    account_file=Path("/tmp/twitter-cookie.json"),
                )
            )

        self.assertEqual(captured["platform"], "twitter")
        self.assertEqual(
            captured["payload"]["threadFileRefs"],
            ["/tmp/a.jpg", "/tmp/b.jpg"],
        )
        self.assertEqual(
            [str(path) for path in captured["thread_file_paths"]],
            ["/tmp/a.jpg", "/tmp/b.jpg"],
        )

    def test_unimplemented_prepared_platform_raises_clear_error(self) -> None:
        target = jobs.Target(
            id=1,
            job_id=1,
            account_ref=f"account:{self.account.id}",
            file_ref="campaign_post:1",
            schedule_at=None,
            status=jobs.TARGET_RUNNING,
            attempts=1,
        )
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(
                worker._run_prepared_campaign_upload(
                    "nonexistent_platform",
                    {"campaignId": 1, "campaignPostId": 2},
                    target,
                    account=None,
                    account_file=None,
                )
            )
        self.assertIn("Unsupported prepared publish platform", str(ctx.exception))

    def test_reddit_api_dispatch_calls_publish_reddit_sync(self) -> None:
        target = jobs.Target(
            id=1,
            job_id=1,
            account_ref=f"account:{self.account.id}",
            file_ref="campaign_post:1",
            schedule_at=None,
            status=jobs.TARGET_RUNNING,
            attempts=1,
        )
        payload = {
            "campaignId": 10,
            "campaignPostId": 22,
            "message": "Reddit post",
            "draft": {"subreddits": ["python"]},
        }
        captured = {}

        def fake_publish_reddit(account, payload):
            captured["account"] = account
            captured["payload"] = payload

        mock_account = type("Account", (), {"config": {"redditAuthType": "api"}, "account_name": "test"})()
        with patch.object(worker.prepared_publishers, "publish_reddit_sync", fake_publish_reddit):
            asyncio.run(
                worker._publish_prepared_reddit(
                    "reddit",
                    payload,
                    target,
                    account=mock_account,
                    account_file=Path("/tmp/reddit-cookie.json"),
                )
            )

        self.assertEqual(captured["account"], mock_account)
        self.assertEqual(captured["payload"]["message"], "Reddit post")

    def test_reddit_cookie_dispatch_raises_without_account_file(self) -> None:
        target = jobs.Target(
            id=1,
            job_id=1,
            account_ref=f"account:{self.account.id}",
            file_ref="campaign_post:1",
            schedule_at=None,
            status=jobs.TARGET_RUNNING,
            attempts=1,
        )
        mock_account = type("Account", (), {"config": {"redditAuthType": "cookie"}, "account_name": "test"})()
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(
                worker._publish_prepared_reddit(
                    "reddit",
                    {"message": "test", "draft": {"subreddits": ["python"]}},
                    target,
                    account=mock_account,
                    account_file=None,
                )
            )
        self.assertIn("account_file", str(ctx.exception))

    def test_reddit_cookie_dispatch_raises_without_subreddits(self) -> None:
        target = jobs.Target(
            id=1,
            job_id=1,
            account_ref=f"account:{self.account.id}",
            file_ref="campaign_post:1",
            schedule_at=None,
            status=jobs.TARGET_RUNNING,
            attempts=1,
        )
        mock_account = type("Account", (), {"config": {"redditAuthType": "cookie"}, "account_name": "test"})()
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(
                worker._publish_prepared_reddit(
                    "reddit",
                    {"message": "test"},
                    target,
                    account=mock_account,
                    account_file=Path("/tmp/cookie.json"),
                )
            )
        self.assertIn("subreddit", str(ctx.exception))

    def test_reddit_dispatch_raises_without_account(self) -> None:
        target = jobs.Target(
            id=1,
            job_id=1,
            account_ref=f"account:{self.account.id}",
            file_ref="campaign_post:1",
            schedule_at=None,
            status=jobs.TARGET_RUNNING,
            attempts=1,
        )
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(
                worker._publish_prepared_reddit(
                    "reddit",
                    {"message": "test"},
                    target,
                    account=None,
                    account_file=None,
                )
            )
        self.assertIn("structured account", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
