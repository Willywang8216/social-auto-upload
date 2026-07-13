"""Unit tests for myUtils.secret_redaction."""

from __future__ import annotations

import unittest

from myUtils.secret_redaction import (
    REDACTION_SENTINEL,
    is_secret_key,
    redact_config_secrets,
    strip_redaction_sentinels,
)


class IsSecretKeyTests(unittest.TestCase):
    def test_secret_value_keys(self) -> None:
        for key in (
            "accessToken", "refreshToken", "metaUserAccessToken",
            "pageAccessToken", "botToken", "apiToken", "githubToken",
            "clientSecret", "accessTokenSecret", "apiKeySecret", "apiKey",
            "password", "cookie", "cookies",
        ):
            self.assertTrue(is_secret_key(key), key)

    def test_metadata_keys_are_not_secret(self) -> None:
        for key in (
            "accessTokenExpiresAt", "accessTokenUpdatedAt",
            "refreshTokenExpiresAt", "metaUserAccessTokenExpiresAt",
            "tokenType", "clientSecretEnv", "accessTokenEnv", "botTokenEnv",
            "webhookUrlEnv", "consumerKey", "channelTitle", "displayName",
            "openId", "avatarUrl", "pageId",
        ):
            self.assertFalse(is_secret_key(key), key)

    def test_non_string_key(self) -> None:
        self.assertFalse(is_secret_key(123))


class RedactTests(unittest.TestCase):
    def test_non_empty_secret_is_masked(self) -> None:
        out = redact_config_secrets({"accessToken": "abc123", "channelTitle": "My Chan"})
        self.assertEqual(out["accessToken"], REDACTION_SENTINEL)
        self.assertEqual(out["channelTitle"], "My Chan")

    def test_empty_secret_stays_empty(self) -> None:
        out = redact_config_secrets({"accessToken": "", "refreshToken": None})
        self.assertEqual(out["accessToken"], "")
        self.assertIsNone(out["refreshToken"])

    def test_metadata_preserved(self) -> None:
        cfg = {"accessToken": "x", "accessTokenExpiresAt": "2026-01-01", "tokenType": "bearer"}
        out = redact_config_secrets(cfg)
        self.assertEqual(out["accessToken"], REDACTION_SENTINEL)
        self.assertEqual(out["accessTokenExpiresAt"], "2026-01-01")
        self.assertEqual(out["tokenType"], "bearer")

    def test_nested_secrets_masked(self) -> None:
        cfg = {"pages": [{"pageId": "1", "pageAccessToken": "secret"}]}
        out = redact_config_secrets(cfg)
        self.assertEqual(out["pages"][0]["pageId"], "1")
        self.assertEqual(out["pages"][0]["pageAccessToken"], REDACTION_SENTINEL)

    def test_input_not_mutated(self) -> None:
        cfg = {"accessToken": "abc"}
        redact_config_secrets(cfg)
        self.assertEqual(cfg["accessToken"], "abc")


class StripSentinelTests(unittest.TestCase):
    def test_sentinel_keys_removed(self) -> None:
        out = strip_redaction_sentinels(
            {"accessToken": REDACTION_SENTINEL, "channelTitle": "keep"}
        )
        self.assertNotIn("accessToken", out)
        self.assertEqual(out["channelTitle"], "keep")

    def test_real_values_kept(self) -> None:
        out = strip_redaction_sentinels({"accessToken": "real-token"})
        self.assertEqual(out["accessToken"], "real-token")

    def test_nested_sentinels_removed(self) -> None:
        out = strip_redaction_sentinels(
            {"pages": [{"pageId": "1", "pageAccessToken": REDACTION_SENTINEL}]}
        )
        self.assertEqual(out["pages"][0], {"pageId": "1"})

    def test_redact_then_strip_roundtrip_preserves_via_merge(self) -> None:
        # Simulate the account-update merge: redact on read, client resubmits,
        # strip on write, merge over stored config -> real secret survives.
        stored = {"accessToken": "real", "channelTitle": "old"}
        sent_to_client = redact_config_secrets(stored)
        # client edits a non-secret field, resubmits the whole (redacted) config
        resubmitted = dict(sent_to_client)
        resubmitted["channelTitle"] = "new"
        incoming = strip_redaction_sentinels(resubmitted)
        merged = dict(stored)
        merged.update(incoming)
        self.assertEqual(merged["accessToken"], "real")
        self.assertEqual(merged["channelTitle"], "new")


if __name__ == "__main__":
    unittest.main()
