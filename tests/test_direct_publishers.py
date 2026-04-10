import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils.direct_publishers import (
    get_direct_publisher_target,
    get_direct_publishers_config,
    publish_job_to_direct_target,
    save_direct_publishers_config,
)


class DirectPublishersTests(unittest.TestCase):
    def test_save_direct_publishers_config_normalizes_targets(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            saved = save_direct_publishers_config(
                base_dir,
                {
                    "targets": [
                        {
                            "platform": "telegram",
                            "name": "",
                            "config": {
                                "botToken": "bot-token",
                                "chatId": "@channel",
                            },
                        },
                        {
                            "platform": "twitter",
                            "id": "x-main",
                            "name": "X Main",
                            "config": {
                                "apiKey": "key",
                                "apiKeySecret": "secret",
                                "accessToken": "token",
                                "accessTokenSecret": "token-secret",
                            },
                        },
                    ]
                },
            )

            self.assertEqual(len(saved["targets"]), 2)
            self.assertTrue(saved["targets"][0]["id"])
            self.assertEqual(saved["targets"][0]["name"], "@channel")
            loaded = get_direct_publishers_config(base_dir)
            self.assertEqual(loaded["targets"][1]["id"], "x-main")

    @patch("utils.direct_publishers._get_requests_module")
    def test_publish_job_to_telegram_uses_send_video_for_video_jobs(self, requests_module_mock):
        response = Mock()
        response.ok = True
        response.json.return_value = {"ok": True, "result": {"message_id": 321}}
        requests_module_mock.return_value.post.return_value = response

        result = publish_job_to_direct_target(
            Path("."),
            {
                "platformKey": "telegram",
                "message": "Hello channel",
                "mediaPublicUrl": "https://cdn.example.com/video.mp4",
                "metadata": {"mediaKind": "video"},
            },
            {
                "platform": "telegram",
                "enabled": True,
                "config": {
                    "botToken": "bot-token",
                    "chatId": "@demo",
                    "parseMode": "HTML",
                    "disableWebPagePreview": False,
                },
            },
        )

        self.assertEqual(result["messageId"], 321)
        self.assertIn("/sendVideo", requests_module_mock.return_value.post.call_args.args[0])

    @patch("utils.direct_publishers._get_requests_module")
    def test_publish_job_to_discord_uses_webhook(self, requests_module_mock):
        response = Mock()
        response.status_code = 204
        response.ok = True
        requests_module_mock.return_value.post.return_value = response

        publish_job_to_direct_target(
            Path("."),
            {
                "platformKey": "discord",
                "title": "Update",
                "message": "Discord copy",
                "mediaPublicUrl": "https://cdn.example.com/image.png",
                "metadata": {"mediaKind": "image"},
            },
            {
                "platform": "discord",
                "enabled": True,
                "config": {"webhookUrl": "https://discord.example/webhook", "username": "Uploader"},
            },
        )

        self.assertEqual(requests_module_mock.return_value.post.call_args.args[0], "https://discord.example/webhook")
        self.assertIn("embeds", requests_module_mock.return_value.post.call_args.kwargs["json"])

    @patch("utils.direct_publishers._get_requests_module")
    def test_publish_job_to_reddit_uses_refresh_token_flow(self, requests_module_mock):
        token_response = Mock()
        token_response.ok = True
        token_response.json.return_value = {"access_token": "access-token"}
        submit_response = Mock()
        submit_response.ok = True
        submit_response.json.return_value = {"json": {"errors": []}}
        requests_module_mock.return_value.post.side_effect = [token_response, submit_response]

        result = publish_job_to_direct_target(
            Path("."),
            {
                "platformKey": "reddit",
                "title": "Reddit Title",
                "message": "Reddit body",
                "mediaPublicUrl": "",
                "metadata": {},
            },
            {
                "platform": "reddit",
                "enabled": True,
                "config": {
                    "clientId": "cid",
                    "clientSecret": "secret",
                    "refreshToken": "refresh",
                    "subreddit": "socialauto",
                },
            },
        )

        self.assertEqual(result["subreddit"], "socialauto")
        self.assertIn("access_token", token_response.json.return_value)
        self.assertEqual(requests_module_mock.return_value.post.call_args_list[1].kwargs["data"]["kind"], "self")

    @patch("utils.direct_publishers._get_requests_module")
    def test_publish_job_to_x_posts_to_v2_tweets(self, requests_module_mock):
        response = Mock()
        response.ok = True
        response.json.return_value = {"data": {"id": "12345"}}
        requests_module_mock.return_value.post.return_value = response

        result = publish_job_to_direct_target(
            Path("."),
            {
                "platformKey": "twitter",
                "message": "Hello X",
                "mediaPublicUrl": "https://cdn.example.com/video.mp4",
                "metadata": {"mediaKind": "video"},
            },
            {
                "platform": "twitter",
                "enabled": True,
                "config": {
                    "apiKey": "key",
                    "apiKeySecret": "secret",
                    "accessToken": "token",
                    "accessTokenSecret": "token-secret",
                },
            },
        )

        self.assertEqual(result["tweetId"], "12345")
        self.assertEqual(requests_module_mock.return_value.post.call_args.args[0], "https://api.x.com/2/tweets")
        self.assertIn("Authorization", requests_module_mock.return_value.post.call_args.kwargs["headers"])

    def test_get_direct_publisher_target_raises_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaises(ValueError):
                get_direct_publisher_target(Path(tmp_dir), "missing")


if __name__ == "__main__":
    unittest.main()
