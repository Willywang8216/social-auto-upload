"""Unit tests for myUtils.security policy helpers."""

from __future__ import annotations

import json
import unittest

from myUtils.security import (
    CookieValidationError,
    extract_bearer_token,
    load_policy,
    SecurityPolicy,
    validate_storage_state,
)


class LoadPolicyTests(unittest.TestCase):
    def test_open_mode_when_token_env_absent(self) -> None:
        policy = load_policy(env={})
        self.assertTrue(policy.open_mode)
        # Default CORS origins cover the Vite dev + preview servers.
        self.assertIn("http://localhost:5173", policy.cors_origins)

    def test_tokens_split_on_comma_and_whitespace(self) -> None:
        policy = load_policy(env={"SAU_API_TOKENS": "alpha, beta\tgamma"})
        self.assertFalse(policy.open_mode)
        self.assertEqual(policy.tokens, frozenset({"alpha", "beta", "gamma"}))

    def test_cors_origins_can_be_overridden(self) -> None:
        policy = load_policy(env={"SAU_CORS_ORIGINS": "https://example.com"})
        self.assertEqual(policy.cors_origins, ("https://example.com",))


class TokenValidationTests(unittest.TestCase):
    def test_valid_token_accepted(self) -> None:
        policy = SecurityPolicy(
            tokens=frozenset({"secret"}),
            cors_origins=("http://localhost:5173",),
        )
        self.assertTrue(policy.token_is_valid("secret"))

    def test_invalid_token_rejected(self) -> None:
        policy = SecurityPolicy(
            tokens=frozenset({"secret"}),
            cors_origins=("http://localhost:5173",),
        )
        self.assertFalse(policy.token_is_valid("nope"))
        self.assertFalse(policy.token_is_valid(""))
        self.assertFalse(policy.token_is_valid(None))


class ExtractBearerTokenTests(unittest.TestCase):
    def test_extracts_from_authorization_header(self) -> None:
        token = extract_bearer_token({"Authorization": "Bearer abc"})
        self.assertEqual(token, "abc")

    def test_case_insensitive_header(self) -> None:
        token = extract_bearer_token({"authorization": "bearer abc"})
        self.assertEqual(token, "abc")

    def test_missing_returns_none(self) -> None:
        self.assertIsNone(extract_bearer_token({}))
        self.assertIsNone(extract_bearer_token(None))

    def test_query_only_consulted_for_sse(self) -> None:
        # Regular requests must not pick up tokens from the URL.
        self.assertIsNone(extract_bearer_token({}, {"auth": "abc"}))
        # SSE path opts in.
        self.assertEqual(
            extract_bearer_token({}, {"auth": "abc"}, is_sse=True),
            "abc",
        )

    def test_header_wins_over_query(self) -> None:
        token = extract_bearer_token(
            {"Authorization": "Bearer header"},
            {"auth": "query"},
            is_sse=True,
        )
        self.assertEqual(token, "header")


class ValidateStorageStateTests(unittest.TestCase):
    def test_minimal_cookies_only(self) -> None:
        body = json.dumps({"cookies": [{"name": "a", "value": "b"}]})
        out = validate_storage_state(body)
        self.assertEqual(out["cookies"][0]["name"], "a")

    def test_minimal_origins_only(self) -> None:
        body = json.dumps({"origins": [{"origin": "https://example.com"}]})
        out = validate_storage_state(body)
        self.assertEqual(out["origins"][0]["origin"], "https://example.com")

    def test_rejects_non_json(self) -> None:
        with self.assertRaises(CookieValidationError):
            validate_storage_state("not json")

    def test_rejects_array_top_level(self) -> None:
        with self.assertRaises(CookieValidationError):
            validate_storage_state("[]")

    def test_rejects_missing_both_keys(self) -> None:
        with self.assertRaises(CookieValidationError):
            validate_storage_state(json.dumps({"hello": "world"}))

    def test_rejects_cookies_not_a_list(self) -> None:
        with self.assertRaises(CookieValidationError):
            validate_storage_state(json.dumps({"cookies": "oops"}))

    def test_rejects_cookie_missing_name(self) -> None:
        with self.assertRaises(CookieValidationError):
            validate_storage_state(json.dumps({"cookies": [{"value": "b"}]}))

    def test_rejects_origin_missing_origin_field(self) -> None:
        with self.assertRaises(CookieValidationError):
            validate_storage_state(json.dumps({"origins": [{"foo": "bar"}]}))

    def test_size_cap_enforced(self) -> None:
        big = json.dumps({"cookies": [
            {"name": "a", "value": "x" * (2 * 1024 * 1024)}
        ]})
        with self.assertRaises(CookieValidationError):
            validate_storage_state(big)


if __name__ == "__main__":
    unittest.main()
