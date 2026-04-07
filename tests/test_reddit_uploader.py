import sys
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

fake_conf = types.ModuleType("conf")
fake_conf.BASE_DIR = Path("/workspace")
fake_conf.APP_BASE_URL = "http://localhost:5409"
fake_conf.REDDIT_CLIENT_ID = ""
fake_conf.REDDIT_CLIENT_SECRET = ""
fake_conf.REDDIT_REDIRECT_URI = "http://localhost:5409/oauth/reddit/callback"
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

from uploader.reddit_uploader.main import (
    RedditAPIError,
    build_authorize_url,
    submit_post,
    validate_reddit_credentials,
)


class RedditUploaderTests(unittest.TestCase):
    def test_build_authorize_url_includes_state_scope_and_redirect(self):
        url = build_authorize_url(
            "state-123",
            redirect_uri="https://example.com/reddit/callback",
            client_id="reddit-client",
            scopes=("identity", "submit"),
        )

        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(query["client_id"], ["reddit-client"])
        self.assertEqual(query["state"], ["state-123"])
        self.assertEqual(query["redirect_uri"], ["https://example.com/reddit/callback"])
        self.assertEqual(query["scope"], ["identity submit"])

    def test_validate_reddit_credentials_refreshes_expired_token(self):
        credentials = {
            "access_token": "old-token",
            "refresh_token": "refresh-token",
            "expires_at": 100,
            "external_username": "old-name",
        }

        with patch("uploader.reddit_uploader.main._now_ts", return_value=100):
            with patch(
                "uploader.reddit_uploader.main.refresh_access_token",
                return_value={
                    "access_token": "new-token",
                    "refresh_token": "new-refresh-token",
                    "expires_at": 3600,
                    "scope": "identity submit",
                    "token_type": "bearer",
                },
            ) as mock_refresh:
                with patch(
                    "uploader.reddit_uploader.main.get_current_user",
                    return_value={
                        "id": "user-1",
                        "name": "demo_user",
                        "icon_img": "https://example.com/avatar.png",
                        "subreddit": {"title": "Demo Profile"},
                    },
                ) as mock_user:
                    validated = validate_reddit_credentials(credentials)

        mock_refresh.assert_called_once_with("refresh-token")
        mock_user.assert_called_once_with("new-token")
        self.assertEqual(validated["access_token"], "new-token")
        self.assertEqual(validated["refresh_token"], "new-refresh-token")
        self.assertEqual(validated["external_username"], "demo_user")
        self.assertEqual(validated["external_name"], "Demo Profile")

    def test_submit_post_rejects_media_in_phase_one(self):
        with self.assertRaises(RedditAPIError):
            submit_post(
                {"access_token": "token"},
                subreddit="python",
                title="demo",
                post_kind="self",
                body="body",
                media_paths=["demo.mp4"],
            )


if __name__ == "__main__":
    unittest.main()
