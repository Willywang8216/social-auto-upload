import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils.direct_publishers import (
    get_direct_publisher_target,
    get_direct_publishers_config,
    publish_job_to_direct_target,
    refresh_direct_publish_job_status,
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

    @patch("utils.direct_publishers._get_requests_module")
    def test_publish_job_to_facebook_posts_video_to_page_videos(self, requests_module_mock):
        response = Mock()
        response.ok = True
        response.json.return_value = {"id": "fb-video-1"}
        requests_module_mock.return_value.post.return_value = response

        result = publish_job_to_direct_target(
            Path("."),
            {
                "platformKey": "facebook",
                "title": "Video title",
                "message": "Facebook body",
                "mediaPublicUrl": "https://cdn.example.com/video.mp4",
                "metadata": {"mediaKind": "video"},
            },
            {
                "platform": "facebook",
                "enabled": True,
                "config": {
                    "accessToken": "fb-token",
                    "pageId": "page-123",
                },
            },
        )

        self.assertEqual(result["postId"], "fb-video-1")
        self.assertEqual(
            requests_module_mock.return_value.post.call_args.args[0],
            "https://graph.facebook.com/v25.0/page-123/videos",
        )
        self.assertEqual(requests_module_mock.return_value.post.call_args.kwargs["data"]["file_url"], "https://cdn.example.com/video.mp4")

    @patch("utils.direct_publishers._get_requests_module")
    def test_publish_job_to_threads_creates_container_then_publishes(self, requests_module_mock):
        container_response = Mock()
        container_response.ok = True
        container_response.json.return_value = {"id": "creation-1"}
        publish_response = Mock()
        publish_response.ok = True
        publish_response.json.return_value = {"id": "thread-9"}
        requests_module_mock.return_value.post.side_effect = [container_response, publish_response]

        result = publish_job_to_direct_target(
            Path("."),
            {
                "platformKey": "threads",
                "message": "Threads post",
                "mediaPublicUrl": "https://cdn.example.com/image.png",
                "metadata": {"mediaKind": "image"},
            },
            {
                "platform": "threads",
                "enabled": True,
                "config": {
                    "accessToken": "threads-token",
                    "userId": "user-123",
                },
            },
        )

        self.assertEqual(result["threadId"], "thread-9")
        first_call = requests_module_mock.return_value.post.call_args_list[0]
        second_call = requests_module_mock.return_value.post.call_args_list[1]
        self.assertEqual(first_call.args[0], "https://graph.threads.net/v1.0/user-123/threads")
        self.assertEqual(first_call.kwargs["data"]["media_type"], "IMAGE")
        self.assertEqual(second_call.args[0], "https://graph.threads.net/v1.0/user-123/threads_publish")
        self.assertEqual(second_call.kwargs["data"]["creation_id"], "creation-1")

    @patch("utils.direct_publishers._get_requests_module")
    def test_publish_job_to_youtube_uses_resumable_upload(self, requests_module_mock):
        init_response = Mock()
        init_response.ok = True
        init_response.headers = {"Location": "https://upload.example/session"}
        upload_response = Mock()
        upload_response.ok = True
        upload_response.json.return_value = {"id": "yt-video-7"}
        requests_module_mock.return_value.post.return_value = init_response
        requests_module_mock.return_value.put.return_value = upload_response

        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            media_dir = base_dir / "videoFile"
            media_dir.mkdir(parents=True, exist_ok=True)
            media_path = media_dir / "clip.mp4"
            media_path.write_bytes(b"fake-mp4-bytes")

            result = publish_job_to_direct_target(
                base_dir,
                {
                    "platformKey": "youtube",
                    "title": "YouTube title",
                    "message": "YouTube description",
                    "mediaPath": "videoFile/clip.mp4",
                    "metadata": {"mediaKind": "video"},
                },
                {
                    "platform": "youtube",
                    "enabled": True,
                    "config": {
                        "accessToken": "yt-token",
                        "privacyStatus": "public",
                        "categoryId": "22",
                    },
                },
            )

        self.assertEqual(result["videoId"], "yt-video-7")
        self.assertEqual(
            requests_module_mock.return_value.post.call_args.args[0],
            "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        )
        self.assertEqual(requests_module_mock.return_value.put.call_args.args[0], "https://upload.example/session")

    @patch("utils.direct_publishers._get_requests_module")
    def test_publish_job_to_tiktok_initializes_pull_from_url_publish(self, requests_module_mock):
        response = Mock()
        response.ok = True
        response.json.return_value = {"data": {"publish_id": "tt-publish-1"}}
        requests_module_mock.return_value.post.return_value = response

        result = publish_job_to_direct_target(
            Path("."),
            {
                "platformKey": "tiktok",
                "title": "TikTok title",
                "message": "TikTok caption",
                "mediaPublicUrl": "https://cdn.example.com/clip.mp4",
                "metadata": {"mediaKind": "video"},
            },
            {
                "platform": "tiktok",
                "enabled": True,
                "config": {
                    "accessToken": "tt-token",
                    "privacyLevel": "PUBLIC_TO_EVERYONE",
                    "disableComment": True,
                    "disableDuet": False,
                    "disableStitch": True,
                },
            },
        )

        self.assertEqual(result["publishId"], "tt-publish-1")
        payload = requests_module_mock.return_value.post.call_args.kwargs["data"]
        self.assertIn('"source": "PULL_FROM_URL"', payload)
        self.assertIn('"privacy_level": "PUBLIC_TO_EVERYONE"', payload)

    @patch("utils.direct_publishers._get_requests_module")
    def test_refresh_threads_status_returns_permalink(self, requests_module_mock):
        response = Mock()
        response.ok = True
        response.json.return_value = {
            "id": "thread-123",
            "permalink": "https://www.threads.net/@demo/post/abc",
            "username": "demo",
        }
        requests_module_mock.return_value.get.return_value = response

        result = refresh_direct_publish_job_status(
            Path("."),
            {
                "platformKey": "threads",
                "metadata": {"threadId": "thread-123"},
            },
            {
                "platform": "threads",
                "enabled": True,
                "config": {"accessToken": "threads-token", "userId": "user-123"},
            },
        )

        self.assertEqual(result["status"], "published")
        self.assertEqual(result["url"], "https://www.threads.net/@demo/post/abc")

    @patch("utils.direct_publishers._get_requests_module")
    def test_refresh_youtube_status_returns_processing_until_processed(self, requests_module_mock):
        response = Mock()
        response.ok = True
        response.json.return_value = {
            "items": [
                {
                    "id": "yt-1",
                    "status": {"uploadStatus": "uploaded", "privacyStatus": "private"},
                    "processingDetails": {"processingStatus": "processing"},
                }
            ]
        }
        requests_module_mock.return_value.get.return_value = response

        result = refresh_direct_publish_job_status(
            Path("."),
            {
                "platformKey": "youtube",
                "metadata": {"videoId": "yt-1"},
            },
            {
                "platform": "youtube",
                "enabled": True,
                "config": {"accessToken": "yt-token"},
            },
        )

        self.assertEqual(result["status"], "processing")
        self.assertEqual(result["remoteId"], "yt-1")

    @patch("utils.direct_publishers._get_requests_module")
    def test_refresh_tiktok_status_maps_complete_to_published(self, requests_module_mock):
        response = Mock()
        response.ok = True
        response.json.return_value = {
            "data": {
                "status": "PUBLISH_COMPLETE",
                "video_id": "tt-video-1",
            }
        }
        requests_module_mock.return_value.post.return_value = response

        result = refresh_direct_publish_job_status(
            Path("."),
            {
                "platformKey": "tiktok",
                "metadata": {"publishId": "publish-1"},
            },
            {
                "platform": "tiktok",
                "enabled": True,
                "config": {"accessToken": "tt-token"},
            },
        )

        self.assertEqual(result["status"], "published")
        self.assertEqual(result["remoteId"], "tt-video-1")

    def test_get_direct_publisher_target_raises_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaises(ValueError):
                get_direct_publisher_target(Path(tmp_dir), "missing")


if __name__ == "__main__":
    unittest.main()
