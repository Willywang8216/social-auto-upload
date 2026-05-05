"""Tests for prepared-campaign HTTP publishers."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from myUtils import prepared_publishers


class _FakeResponse:
    def __init__(self, payload=None, *, headers=None):
        self._payload = payload or {}
        self.headers = headers or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _RecordingSession:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def _next(self):
        if self.responses:
            return self.responses.pop(0)
        return _FakeResponse({})

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._next()

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._next()

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return self._next()


class PreparedPublisherTests(unittest.TestCase):
    def test_telegram_single_video_uses_send_video(self):
        session = _RecordingSession([_FakeResponse({"ok": True})])
        account = SimpleNamespace(config={"botToken": "token", "chatId": "@brand"})
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "clip.mp4"
            video.write_bytes(b"video")
            prepared_publishers.publish_telegram_sync(
                account,
                {
                    "message": "hello telegram",
                    "artifacts": [{"local_path": str(video), "artifact_kind": "watermarked_video"}],
                },
                session=session,
            )
        self.assertEqual(session.calls[0][1], "https://api.telegram.org/bottoken/sendVideo")
        self.assertIn("files", session.calls[0][2])

    def test_reddit_refresh_and_submit_to_each_subreddit(self):
        session = _RecordingSession([
            _FakeResponse({"access_token": "reddit-token"}),
            _FakeResponse({"json": {"errors": []}}),
            _FakeResponse({"json": {"errors": []}}),
        ])
        account = SimpleNamespace(
            account_name="brand-main",
            config={
                "clientId": "cid",
                "clientSecret": "secret",
                "refreshToken": "refresh",
                "subreddits": ["suba", "subb"],
            },
        )
        prepared_publishers.publish_reddit_sync(
            account,
            {
                "message": "Reddit launch post",
                "artifacts": [{"public_url": "https://cdn.example/video.mp4", "artifact_kind": "remote_upload"}],
            },
            session=session,
        )
        self.assertEqual(session.calls[0][1], prepared_publishers.REDDIT_TOKEN_URL)
        self.assertEqual(session.calls[1][1], prepared_publishers.REDDIT_SUBMIT_URL)
        self.assertEqual(session.calls[2][2]["data"]["sr"], "subb")

    def test_telegram_live_validation_calls_getme_and_getchat(self):
        session = _RecordingSession([_FakeResponse({"ok": True}), _FakeResponse({"ok": True})])
        result = prepared_publishers.validate_telegram_config_live(
            {"botToken": "token", "chatId": "@brand"},
            session=session,
        )
        self.assertEqual(session.calls[0][1], "https://api.telegram.org/bottoken/getMe")
        self.assertEqual(session.calls[1][1], "https://api.telegram.org/bottoken/getChat")
        self.assertIn("chat", result)

    def test_facebook_live_validation_fetches_page(self):
        session = _RecordingSession([_FakeResponse({"id": "123", "name": "Brand Page"})])
        result = prepared_publishers.validate_facebook_config_live(
            {"pageId": "123", "accessToken": "fb-token"},
            session=session,
        )
        self.assertEqual(session.calls[0][0], "GET")
        self.assertEqual(session.calls[0][1], f"{prepared_publishers.FACEBOOK_GRAPH_ROOT}/123")
        self.assertEqual(result["id"], "123")

    def test_discord_uses_webhook_with_local_files(self):
        session = _RecordingSession([_FakeResponse({})])
        account = SimpleNamespace(config={"webhookUrl": "https://discord.example/webhook"})
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "cover.jpg"
            image.write_bytes(b"image")
            prepared_publishers.publish_discord_sync(
                account,
                {
                    "message": "Discord launch",
                    "artifacts": [{"local_path": str(image), "artifact_kind": "watermarked_image"}],
                },
                session=session,
            )
        self.assertEqual(session.calls[0][1], "https://discord.example/webhook")
        self.assertIn("files", session.calls[0][2])

    def test_facebook_multiple_images_create_unpublished_photos_then_feed_post(self):
        session = _RecordingSession([
            _FakeResponse({"id": "photo-1"}),
            _FakeResponse({"id": "photo-2"}),
            _FakeResponse({"id": "post-1"}),
        ])
        account = SimpleNamespace(config={"pageId": "123", "accessToken": "fb-token"})
        prepared_publishers.publish_facebook_sync(
            account,
            {
                "message": "Facebook launch",
                "artifacts": [
                    {"public_url": "https://cdn.example/a.jpg", "artifact_kind": "watermarked_image"},
                    {"public_url": "https://cdn.example/b.jpg", "artifact_kind": "watermarked_image"},
                ],
            },
            session=session,
        )
        self.assertEqual(session.calls[0][1], f"{prepared_publishers.FACEBOOK_GRAPH_ROOT}/123/photos")
        self.assertEqual(session.calls[2][1], f"{prepared_publishers.FACEBOOK_GRAPH_ROOT}/123/feed")
        attached = json.loads(session.calls[2][2]["data"]["attached_media"])
        self.assertEqual(attached[0]["media_fbid"], "photo-1")

    def test_instagram_single_image_creates_container_then_publishes(self):
        session = _RecordingSession([
            _FakeResponse({"id": "ig-container"}),
            _FakeResponse({"id": "ig-media"}),
        ])
        account = SimpleNamespace(config={"igUserId": "1789", "accessToken": "ig-token"})
        result = prepared_publishers.publish_instagram_sync(
            account,
            {
                "message": "Instagram launch",
                "artifacts": [{"public_url": "https://cdn.example/image.jpg", "artifact_kind": "watermarked_image"}],
            },
            session=session,
        )
        self.assertEqual(result["container_id"], "ig-container")
        self.assertEqual(session.calls[0][1], f"{prepared_publishers.FACEBOOK_GRAPH_ROOT}/1789/media")
        self.assertEqual(session.calls[1][1], f"{prepared_publishers.FACEBOOK_GRAPH_ROOT}/1789/media_publish")

    def test_threads_text_only_creates_container_then_publishes(self):
        session = _RecordingSession([
            _FakeResponse({"id": "threads-container"}),
            _FakeResponse({"id": "threads-post"}),
        ])
        account = SimpleNamespace(config={"threadUserId": "42", "accessToken": "threads-token"})
        result = prepared_publishers.publish_threads_sync(
            account,
            {"message": "Threads launch"},
            session=session,
        )
        self.assertEqual(result["container_id"], "threads-container")
        self.assertEqual(session.calls[0][1], f"{prepared_publishers.THREADS_GRAPH_ROOT}/42/threads")
        self.assertEqual(session.calls[1][1], f"{prepared_publishers.THREADS_GRAPH_ROOT}/42/threads_publish")

    def test_tiktok_publish_auto_refreshes_stale_token(self):
        session = _RecordingSession([
            _FakeResponse({'access_token': 'fresh-token', 'refresh_token': 'fresh-refresh', 'expires_in': 3600}),
            _FakeResponse({'data': {'user': {'display_name': 'Demo', 'avatar_url': 'https://example.com/a.jpg'}}}),
            _FakeResponse({'data': {'creator_avatar_url': 'x'}}),
            _FakeResponse({'data': {'publish_id': 'tt-video-2'}}),
        ])
        account = SimpleNamespace(config={
            'accessToken': 'stale-token',
            'refreshToken': 'refresh-token',
            'accessTokenExpiresAt': '2000-01-01T00:00:00+00:00',
        })
        with patch.dict(os.environ, {'TIKTOK_CLIENT_KEY': 'client-key', 'TIKTOK_CLIENT_SECRET': 'client-secret'}, clear=False):
            result = prepared_publishers.publish_tiktok_sync(
                account,
                {
                    'message': 'TikTok refreshed publish',
                    'artifacts': [{'public_url': 'https://cdn.example/video.mp4', 'artifact_kind': 'remote_upload'}],
                },
                session=session,
            )
        self.assertEqual(session.calls[0][1], prepared_publishers.tiktok_auth.TIKTOK_OAUTH_TOKEN_URL)
        self.assertEqual(result['updated_config']['accessToken'], 'fresh-token')
        self.assertEqual(result['request']['source_info']['video_url'], 'https://cdn.example/video.mp4')

    def test_tiktok_video_direct_post_uses_video_init(self):
        session = _RecordingSession([
            _FakeResponse({'data': {'creator_avatar_url': 'x'}}),
            _FakeResponse({'data': {'publish_id': 'tt-video-1'}}),
        ])
        account = SimpleNamespace(config={'accessToken': 'tt-token', 'privacyLevel': 'SELF_ONLY'})
        result = prepared_publishers.publish_tiktok_sync(
            account,
            {
                'message': 'TikTok launch',
                'artifacts': [{'public_url': 'https://cdn.example/video.mp4', 'artifact_kind': 'remote_upload'}],
            },
            session=session,
        )
        self.assertEqual(session.calls[0][1], prepared_publishers.TIKTOK_CREATOR_INFO_URL)
        self.assertEqual(session.calls[1][1], prepared_publishers.TIKTOK_VIDEO_INIT_URL)
        self.assertEqual(result['request']['post_info']['privacy_level'], 'SELF_ONLY')

    def test_tiktok_photo_direct_post_uses_content_init(self):
        session = _RecordingSession([
            _FakeResponse({'data': {'creator_avatar_url': 'x'}}),
            _FakeResponse({'data': {'publish_id': 'tt-photo-1'}}),
        ])
        account = SimpleNamespace(config={'accessToken': 'tt-token', 'autoAddMusic': True})
        result = prepared_publishers.publish_tiktok_sync(
            account,
            {
                'message': 'TikTok photos',
                'artifacts': [
                    {'public_url': 'https://cdn.example/a.jpg', 'artifact_kind': 'remote_upload'},
                    {'public_url': 'https://cdn.example/b.jpg', 'artifact_kind': 'remote_upload'},
                ],
            },
            session=session,
        )
        self.assertEqual(session.calls[1][1], prepared_publishers.TIKTOK_CONTENT_INIT_URL)
        self.assertEqual(result['request']['media_type'], 'PHOTO')
        self.assertEqual(result['request']['source_info']['photo_cover_index'], 0)

    def test_youtube_refresh_and_resumable_upload(self):
        session = _RecordingSession([
            _FakeResponse({"access_token": "google-token"}),
            _FakeResponse({}, headers={"Location": "https://upload.example/resumable"}),
            _FakeResponse({"id": "video123"}),
            _FakeResponse({}),
        ])
        account = SimpleNamespace(
            config={
                "channelId": "UC123",
                "clientId": "cid",
                "clientSecret": "secret",
                "refreshToken": "refresh",
                "privacyStatus": "public",
                "playlistId": "PL123",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "clip.mp4"
            video.write_bytes(b"video")
            result = prepared_publishers.publish_youtube_sync(
                account,
                {
                    "message": "YouTube launch\nLong description",
                    "artifacts": [{"local_path": str(video), "artifact_kind": "watermarked_video"}],
                },
                session=session,
            )
        self.assertEqual(result["id"], "video123")
        self.assertEqual(session.calls[0][1], prepared_publishers.GOOGLE_TOKEN_URL)
        self.assertEqual(session.calls[1][1], prepared_publishers.YOUTUBE_RESUMABLE_UPLOAD_URL)
        self.assertEqual(session.calls[2][1], "https://upload.example/resumable")
        self.assertEqual(session.calls[3][1], prepared_publishers.YOUTUBE_PLAYLIST_INSERT_URL)

    def test_config_value_can_resolve_env_reference(self):
        with patch.dict(os.environ, {"TOKEN_ENV": "abc"}, clear=False):
            value = prepared_publishers._config_value({"botTokenEnv": "TOKEN_ENV"}, "botToken")
        self.assertEqual(value, "abc")


if __name__ == "__main__":
    unittest.main()
