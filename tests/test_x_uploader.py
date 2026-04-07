import sys
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

fake_conf = types.ModuleType("conf")
fake_conf.BASE_DIR = Path("/workspace")
fake_conf.APP_BASE_URL = "http://localhost:5409"
fake_conf.X_CLIENT_ID = ""
fake_conf.X_CLIENT_SECRET = ""
fake_conf.X_REDIRECT_URI = "http://localhost:5409/oauth/x/callback"
sys.modules.setdefault("conf", fake_conf)

fake_httpx = types.ModuleType("httpx")


class FakeHTTPError(Exception):
    pass


class FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def request(self, *args, **kwargs):
        raise AssertionError("Unexpected httpx.Client.request call in unit test")


def _fake_request(*args, **kwargs):
    raise AssertionError("Unexpected httpx.request call in unit test")


fake_httpx.HTTPError = FakeHTTPError
fake_httpx.Client = FakeClient
fake_httpx.request = _fake_request
sys.modules.setdefault("httpx", fake_httpx)

from uploader.x_uploader.main import (
    build_authorize_url,
    build_pkce_pair,
    publish_post,
    validate_x_credentials,
)


class XUploaderTests(unittest.TestCase):
    def test_build_pkce_pair_returns_verifier_and_challenge(self):
        verifier, challenge = build_pkce_pair()

        self.assertTrue(len(verifier) >= 43)
        self.assertTrue(len(challenge) >= 43)
        self.assertNotIn("=", challenge)

    def test_build_authorize_url_includes_pkce_and_state(self):
        url = build_authorize_url(
            "state-123",
            "challenge-xyz",
            redirect_uri="https://example.com/x/callback",
            client_id="x-client",
            scopes=("tweet.read", "tweet.write"),
        )

        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(query["client_id"], ["x-client"])
        self.assertEqual(query["state"], ["state-123"])
        self.assertEqual(query["code_challenge"], ["challenge-xyz"])
        self.assertEqual(query["scope"], ["tweet.read tweet.write"])

    def test_validate_x_credentials_refreshes_expired_token(self):
        credentials = {
            "access_token": "old-token",
            "refresh_token": "refresh-token",
            "expires_at": 100,
            "external_username": "old-name",
        }

        with patch("uploader.x_uploader.main._now_ts", return_value=100):
            with patch(
                "uploader.x_uploader.main.refresh_access_token",
                return_value={
                    "access_token": "new-token",
                    "refresh_token": "new-refresh-token",
                    "expires_at": 3600,
                    "scope": "tweet.read tweet.write",
                    "token_type": "bearer",
                },
            ) as mock_refresh:
                with patch(
                    "uploader.x_uploader.main.get_current_user",
                    return_value={
                        "id": "user-1",
                        "username": "demo_user",
                        "name": "Demo User",
                        "profile_image_url": "https://example.com/avatar.png",
                    },
                ) as mock_user:
                    validated = validate_x_credentials(credentials)

        mock_refresh.assert_called_once_with("refresh-token")
        mock_user.assert_called_once_with("new-token")
        self.assertEqual(validated["access_token"], "new-token")
        self.assertEqual(validated["refresh_token"], "new-refresh-token")
        self.assertEqual(validated["external_username"], "demo_user")
        self.assertEqual(validated["external_name"], "Demo User")

    def test_publish_post_uploads_media_then_creates_post(self):
        with patch(
            "uploader.x_uploader.main.validate_x_credentials",
            return_value={"access_token": "token"},
        ) as mock_validate:
            with patch(
                "uploader.x_uploader.main.upload_media",
                side_effect=["media-1", "media-2"],
            ) as mock_upload:
                with patch(
                    "uploader.x_uploader.main.create_post",
                    return_value={"id": "tweet-1"},
                ) as mock_create:
                    post_data, refreshed_credentials = publish_post(
                        {"access_token": "old"},
                        "hello world",
                        media_paths=["a.png", "b.mp4"],
                    )

        mock_validate.assert_called_once()
        self.assertEqual(mock_upload.call_count, 2)
        mock_create.assert_called_once_with("token", "hello world", media_ids=["media-1", "media-2"])
        self.assertEqual(post_data["id"], "tweet-1")
        self.assertEqual(refreshed_credentials["access_token"], "token")


if __name__ == "__main__":
    unittest.main()
