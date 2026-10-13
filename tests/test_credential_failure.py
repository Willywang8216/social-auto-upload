"""Revoked/expired credentials are permanent until a human re-authorises.

Reddit returned ``HTTP 401 {'message': 'Unauthorized'}`` on both the app-only and
refresh token exchange (the app credentials themselves are rejected), and the
cookie uploader raised ``Please log in to do that``. Those are not retryable, so
the worker must stop after one attempt and say why, rather than burn three
attempts and re-route to the fallback.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from myUtils.worker import _is_credential_failure  # noqa: E402


class CredentialFailureTests(unittest.TestCase):
    def test_reddit_401_is_credential(self):
        self.assertTrue(
            _is_credential_failure(
                "PreparedPublishError: HTTP 401: {'message': 'Unauthorized', 'error': 401}"
            )
        )

    def test_please_log_in_is_credential(self):
        self.assertTrue(
            _is_credential_failure("RuntimeError: Reddit API error: Please log in to do that.")
        )

    def test_invalid_grant_is_credential(self):
        self.assertTrue(_is_credential_failure("oauth error: invalid_grant"))
        self.assertTrue(_is_credential_failure("refresh_token_revoked"))

    def test_content_and_transient_errors_are_not_credential(self):
        self.assertFalse(
            _is_credential_failure(
                "PreparedPublishError: Threads video duration 600s exceeds the 300s limit"
            )
        )
        self.assertFalse(
            _is_credential_failure(
                "PreparedPublishError: Reddit submit failed: RATELIMIT take a break"
            )
        )
        self.assertFalse(_is_credential_failure(""))


if __name__ == "__main__":
    unittest.main()
