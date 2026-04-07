import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

fake_conf = types.ModuleType("conf")
fake_conf.BASE_DIR = Path("/workspace")
fake_conf.APP_BASE_URL = "http://localhost:5409"
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

from myUtils.social_publish import (
    SocialPublishValidationError,
    _publish_reddit_destination,
    _publish_x_destination,
    publish_social_destinations,
)
from uploader.x_uploader.main import XAPIError


class SocialPublishBackendTests(unittest.TestCase):
    def test_publish_social_destinations_rejects_empty_destinations(self):
        with self.assertRaises(SocialPublishValidationError):
            publish_social_destinations({})

    def test_publish_x_destination_returns_per_account_results(self):
        payload = {"title": "Title", "body": "Shared body"}
        destination = {"platform": "x", "accountIds": [11, 12], "text": ""}

        with patch(
            "myUtils.social_publish._load_accounts",
            return_value=[
                {"id": 11, "filePath": "x/account-1.json", "userName": "x-account-1"},
                {"id": 12, "filePath": "x/account-2.json", "userName": "x-account-2"},
            ],
        ):
            with patch(
                "myUtils.social_publish.load_x_credentials",
                return_value={"access_token": "token"},
            ):
                with patch(
                    "myUtils.social_publish.publish_x_post",
                    side_effect=[
                        ({"id": "tweet-1"}, {"access_token": "new-token"}),
                        XAPIError("rate limited"),
                    ],
                ) as mock_publish:
                    with patch("myUtils.social_publish.save_x_credentials") as mock_save:
                        results = _publish_x_destination(payload, destination, [Path("video.mp4")])

        self.assertEqual(len(results), 2)
        self.assertTrue(results[0]["success"])
        self.assertFalse(results[1]["success"])
        self.assertEqual(results[0]["postId"], "tweet-1")
        self.assertEqual(results[1]["message"], "rate limited")
        self.assertEqual(mock_publish.call_count, 2)
        mock_save.assert_called_once()

    def test_publish_reddit_destination_returns_phase_two_message_for_media(self):
        payload = {"title": "Reddit title", "body": "Reddit body"}
        destination = {
            "platform": "reddit",
            "accountIds": [21],
            "subreddits": ["python", "SideProject"],
            "postKind": "self",
        }

        with patch(
            "myUtils.social_publish._load_accounts",
            return_value=[
                {"id": 21, "filePath": "reddit/account.json", "userName": "reddit-account"},
            ],
        ):
            results = _publish_reddit_destination(payload, destination, [Path("clip.mp4")])

        self.assertEqual(len(results), 2)
        self.assertTrue(all(not result["success"] for result in results))
        self.assertEqual(results[0]["subreddit"], "python")
        self.assertIn("第二阶段", results[0]["message"])


if __name__ == "__main__":
    unittest.main()
