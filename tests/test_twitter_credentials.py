from __future__ import annotations

import os
import unittest
from unittest import mock

from myUtils.prepared_publishers import (
    _twitter_auth_headers,
    _twitter_oauth1_credentials,
)

ENV_KEYS = (
    "X_API_KEY",
    "X_API_KEY_SECRET",
    "X_ACCESS_TOKEN",
    "X_ACCESS_TOKEN_SECRET",
)

ENV_VALUES = {
    "X_API_KEY": "env-consumer",
    "X_API_KEY_SECRET": "env-consumer-secret",
    "X_ACCESS_TOKEN": "env-user",
    "X_ACCESS_TOKEN_SECRET": "env-user-secret",
}


class TwitterOAuth1CredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, ENV_VALUES, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_env_is_the_single_account_fallback(self) -> None:
        self.assertEqual(
            _twitter_oauth1_credentials({}),
            ("env-consumer", "env-consumer-secret", "env-user", "env-user-secret"),
        )

    def test_per_account_keys_win_over_env(self) -> None:
        config = {
            "oauth1ApiKey": "acct-consumer",
            "oauth1ApiKeySecret": "acct-consumer-secret",
            "oauth1AccessToken": "acct-user",
            "oauth1AccessTokenSecret": "acct-user-secret",
        }

        self.assertEqual(
            _twitter_oauth1_credentials(config),
            ("acct-consumer", "acct-consumer-secret", "acct-user", "acct-user-secret"),
        )

    def test_oauth2_access_token_is_never_used_as_an_oauth1_token(self) -> None:
        # ``accessToken`` holds the OAuth 2.0 bearer token; reading it as the
        # OAuth 1.0a user token would silently authenticate media uploads as
        # whoever the env fallback belongs to.
        config = {
            "accessToken": "oauth2-bearer-token",
            "accessTokenSecret": "not-a-real-oauth1-secret",
        }

        self.assertEqual(_twitter_oauth1_credentials(config)[2], "env-user")

    def test_env_name_indirection_is_honoured(self) -> None:
        with mock.patch.dict(
            os.environ, {"SAU_TW_ACCOUNT_TOKEN": "indirect-user"}, clear=False
        ):
            config = {"oauth1AccessTokenEnv": "SAU_TW_ACCOUNT_TOKEN"}

            self.assertEqual(_twitter_oauth1_credentials(config)[2], "indirect-user")

    def test_auth_headers_prefer_the_oauth2_bearer_token(self) -> None:
        headers = _twitter_auth_headers(
            {"accessToken": "oauth2-bearer-token"}, method="POST", url="https://api.x.com/2/tweets"
        )

        self.assertEqual(headers, {"Authorization": "Bearer oauth2-bearer-token"})

    def test_auth_headers_sign_with_oauth1_when_no_bearer_token(self) -> None:
        headers = _twitter_auth_headers({}, method="GET", url="https://api.x.com/2/users/me")

        self.assertIn("oauth_consumer_key=\"env-consumer\"", headers["Authorization"])
        self.assertIn("oauth_token=\"env-user\"", headers["Authorization"])


if __name__ == "__main__":
    unittest.main()
