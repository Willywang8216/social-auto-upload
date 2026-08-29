"""Regression tests for the hardened login dispatch in `sau_cli.dispatch`.

These tests cover the failure modes that motivated the isinstance +
.get() hardening in every platform login branch:

* Wrapper returns None (e.g. a future refactor) — old code raised
  TypeError from `None['success']`; new code raises a clean RuntimeError.
* Wrapper returns a dict missing the expected keys — old code raised
  KeyError; new code falls back to defaults and prints a friendly message.
* Wrapper returns a dict with success=False — old code surfaced the
  message; new code does too.
* Wrapper returns a dict with success=True — happy path still prints the
  account_file path.
* main() records a structured crash file under logs/fixes/ for every
  uncaught exception and exits with rc=1.
"""

import asyncio
import glob
import io
import json
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import AsyncMock, patch

import sau_cli


PLATFORMS_WITH_LOGIN = [
    "douyin",
    "tencent",
    "kuaishou",
    "xiaohongshu",
    "bilibili",
    "medium",
    "substack",
]


class LoginDispatchHardeningTests(unittest.TestCase):
    """Each platform's login dispatch must tolerate non-dict and partial-dict returns."""

    def _login_args(self, platform: str, account: str = "tester") -> Namespace:
        kwargs = {"platform": platform, "action": "login", "account": account}
        if platform in {"douyin", "tencent", "kuaishou", "xiaohongshu", "medium", "substack"}:
            kwargs["headless"] = True
        if platform in {"medium", "substack"}:
            kwargs["profile"] = None
        return Namespace(**kwargs)

    def _login_wrapper(self, platform: str):
        return {
            "douyin": "sau_cli.login_douyin_account",
            "tencent": "sau_cli.login_tencent_account",
            "kuaishou": "sau_cli.login_kuaishou_account",
            "xiaohongshu": "sau_cli.login_xiaohongshu_account",
            "bilibili": "sau_cli.login_bilibili_account",
            "medium": "sau_cli.login_medium_account",
            "substack": "sau_cli.login_substack_account",
        }[platform]

    def test_login_dispatch_handles_none_return(self):
        """If a wrapper returns None the dispatcher must raise a friendly RuntimeError, not TypeError."""
        for platform in PLATFORMS_WITH_LOGIN:
            with self.subTest(platform=platform):
                target = self._login_wrapper(platform)
                with patch(target, new=AsyncMock(return_value=None)):
                    with self.assertRaises(RuntimeError) as ctx:
                        asyncio.run(sau_cli.dispatch(self._login_args(platform)))
                    self.assertIn(f"{platform.capitalize()} login failed", str(ctx.exception))

    def test_login_dispatch_handles_partial_dict(self):
        """A dict missing 'success' / 'message' / 'account_file' must not raise KeyError."""
        for platform in PLATFORMS_WITH_LOGIN:
            with self.subTest(platform=platform):
                target = self._login_wrapper(platform)
                with patch(target, new=AsyncMock(return_value={})):
                    with self.assertRaises(RuntimeError) as ctx:
                        asyncio.run(sau_cli.dispatch(self._login_args(platform)))
                    self.assertIn(f"{platform.capitalize()} login failed", str(ctx.exception))

    def test_login_dispatch_happy_path_prints_account_file(self):
        """A well-formed success dict must still print the account_file and return 0."""
        captured = io.StringIO()
        for platform in PLATFORMS_WITH_LOGIN:
            with self.subTest(platform=platform):
                target = self._login_wrapper(platform)
                success_result = {
                    "success": True,
                    "status": "success",
                    "message": "ok",
                    "account_file": f"/tmp/{platform}_account.json",
                }
                with patch(target, new=AsyncMock(return_value=success_result)):
                    with redirect_stdout(captured):
                        code = asyncio.run(sau_cli.dispatch(self._login_args(platform)))
                self.assertEqual(code, 0)
                self.assertIn(success_result["account_file"], captured.getvalue())

    def test_login_dispatch_propagates_message_on_success_false(self):
        """A success=False dict must surface its message in the RuntimeError."""
        for platform in PLATFORMS_WITH_LOGIN:
            with self.subTest(platform=platform):
                target = self._login_wrapper(platform)
                failure = {
                    "success": False,
                    "status": "timeout",
                    "message": "qr-code expired",
                    "account_file": "/tmp/x.json",
                }
                with patch(target, new=AsyncMock(return_value=failure)):
                    with self.assertRaises(RuntimeError) as ctx:
                        asyncio.run(sau_cli.dispatch(self._login_args(platform)))
                    self.assertIn("qr-code expired", str(ctx.exception))


class CrashRecordingTests(unittest.TestCase):
    """main() must persist every uncaught crash under logs/fixes/."""

    def setUp(self) -> None:
        self._old_fix_files = set(glob.glob("logs/fixes/crash-*.json"))

    def tearDown(self) -> None:
        # Remove any new crash files this test produced so we don't litter the repo.
        for path in set(glob.glob("logs/fixes/crash-*.json")) - self._old_fix_files:
            os.remove(path)

    def test_main_records_crash_and_returns_one(self):
        """Force a dispatch failure via monkey-patch and confirm a crash file is written."""
        boom = RuntimeError("boom for test")
        captured_err = io.StringIO()

        with patch(
            "sau_cli.login_douyin_account",
            new=AsyncMock(side_effect=boom),
        ):
            with redirect_stderr(captured_err):
                rc = sau_cli.main(["douyin", "login", "--account", "tester"])

        self.assertEqual(rc, 1)
        new_files = sorted(set(glob.glob("logs/fixes/crash-*.json")) - self._old_fix_files)
        self.assertEqual(len(new_files), 1, captured_err.getvalue())
        payload = json.loads(open(new_files[0], encoding="utf-8").read())
        self.assertEqual(payload["exception_type"], "RuntimeError")
        self.assertEqual(payload["exception_message"], "boom for test")
        self.assertEqual(payload["argv"], ["douyin", "login", "--account", "tester"])
        self.assertEqual(payload["context"]["platform"], "douyin")
        self.assertEqual(payload["context"]["action"], "login")
        self.assertEqual(payload["context"]["account"], "tester")
        self.assertIn("crash recorded under logs/fixes/", captured_err.getvalue())


if __name__ == "__main__":
    unittest.main()
