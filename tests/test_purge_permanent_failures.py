"""The permanent-failure classifier must never bury a retryable failure.

It cancels targets whose retry can never succeed, so a false positive loses a
publish. The tests below pin the protective direction: anything ambiguous, and
anything that a later attempt could fix (rate limits), must be left alone.
"""

import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "purge_permanent_failures",
        REPO_ROOT / "scripts" / "purge_permanent_failures.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load()


class PermanentFailureClassificationTests(unittest.TestCase):
    def test_rate_limits_are_never_permanent(self):
        # A rate limit can succeed later, so cancelling would lose a publish.
        for message in (
            "RATELIMIT: Looks like you've been doing that a lot",
            "rate limited (HTTP 429) by https://muyuan.do",
            "HTTP 429 Too Many Requests",
        ):
            with self.subTest(message=message):
                self.assertIsNone(mod.classify(message))

    def test_media_gone_is_permanent(self):
        for message in (
            "MediaRestoreError: Generated artifact is missing",
            "PreparedPublishError: Telegram media file is missing or empty: /app/x",
            "PreparedPublishError: TikTok video artifact not found: /x",
        ):
            with self.subTest(message=message):
                self.assertEqual(mod.classify(message), "media-unrecoverable")

    def test_platform_refusal_is_permanent(self):
        for message in (
            "PreparedPublishError: Reddit submit failed: SUBREDDIT_NOTALLOWED_BANNED",
            "SUBMIT_VALIDATION_LINK_WHITELIST",
            "NO_SELFS: This community doesn't allow text posts",
            "TikTok app is in development mode",
        ):
            with self.subTest(message=message):
                self.assertEqual(mod.classify(message), "platform-refused")

    def test_content_guard_is_permanent(self):
        self.assertEqual(
            mod.classify("[content-guard] placeholder/generic copy"),
            "content-permanent",
        )

    def test_missing_account_is_permanent(self):
        self.assertEqual(mod.classify("LookupError: Account not found: id=87"), "account-gone")

    def test_an_unknown_error_is_left_alone(self):
        # Anything not confidently permanent must be reported, not cancelled.
        for message in (
            "some brand new failure",
            "",
            "TypeError: get_browser_options() takes 0 positional arguments",
            "OSError: disk full",
        ):
            with self.subTest(message=message):
                self.assertIsNone(mod.classify(message))

    def test_a_rate_limit_wins_over_a_permanent_marker_in_the_same_text(self):
        # Defensive: a message mentioning both must not be cancelled.
        self.assertIsNone(
            mod.classify("MediaRestoreError ... but retry later: rate limited 429")
        )


if __name__ == "__main__":
    unittest.main()
