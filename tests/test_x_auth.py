"""Tests for safe X OAuth refresh error handling."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from myUtils import x_auth


class _Response:
    def __init__(self, payload, *, status_code=400):
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self):
        return self._payload


class XRefreshErrorTests(unittest.TestCase):
    def test_invalid_grant_is_reported_without_echoing_credentials(self):
        response = _Response({
            "error": "invalid_grant",
            "error_description": "refresh_token=secret-refresh-value",
        })
        with patch.dict("os.environ", {"X_CLIENT_ID": "client", "X_CLIENT_SECRET": "secret"}):
            with self.assertRaises(x_auth.TwitterOAuthError) as raised:
                x_auth.refresh_access_token(
                    refresh_token="secret-refresh-value",
                    session=type("Session", (), {"post": lambda *_a, **_kw: response})(),
                )
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.error_code, "invalid_grant")
        self.assertNotIn("secret-refresh-value", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

    def test_unknown_provider_code_is_not_persisted(self):
        response = _Response({
            "error": "provider_private_detail",
            "error_description": "client_secret=private-value",
        })
        with patch.dict("os.environ", {"X_CLIENT_ID": "client", "X_CLIENT_SECRET": "secret"}):
            with self.assertRaises(x_auth.TwitterOAuthError) as raised:
                x_auth.refresh_access_token(
                    refresh_token="refresh",
                    session=type("Session", (), {"post": lambda *_a, **_kw: response})(),
                )
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.error_code, "")
        self.assertNotIn("private-value", str(raised.exception))
        self.assertNotIn("provider_private_detail", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
