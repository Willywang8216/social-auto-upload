"""Tests for prepared-campaign HTTP publishers."""

from __future__ import annotations

import json
import sys
import types
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if "conf" not in sys.modules:
    conf_module = types.ModuleType("conf")
    conf_module.BASE_DIR = str(Path(__file__).resolve().parent.parent)
    sys.modules["conf"] = conf_module

from myUtils import prepared_publishers


class _FakeResponse:
    def __init__(self, payload=None, *, headers=None, status_code=200):
        self._payload = payload or {}
        self.headers = headers or {}
        self.status_code = status_code

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
        account = SimpleNamespace(config={"threadUserId": "42", "accessToken": "threads-token", "accessTokenExpiresAt": "2099-01-01T00:00:00"})
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
        with patch.dict(os.environ, {'TIKTOK_CLIENT_KEY': 'client-key', 'TIKTOK_CLIENT_SECRET': 'client-secret', 'SAU_TIKTOK_VERIFIED_URL_PREFIXES': 'https://cdn.example/'}, clear=False):
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
        with patch.dict(os.environ, {'SAU_TIKTOK_VERIFIED_URL_PREFIXES': 'https://cdn.example/'}, clear=False):
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
        with patch.dict(os.environ, {'SAU_TIKTOK_VERIFIED_URL_PREFIXES': 'https://cdn.example/'}, clear=False):
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

    def test_refresh_reddit_access_token_returns_identity(self):
        session = _RecordingSession([
            _FakeResponse({'access_token': 'reddit-token', 'expires_in': 3600, 'scope': 'submit identity read'}),
            _FakeResponse({'name': 'brand-main'}),
        ])
        account = {
            'clientId': 'cid',
            'clientSecret': 'secret',
            'refreshToken': 'refresh',
            'userAgent': 'ua',
        }
        result = prepared_publishers.refresh_reddit_access_token(account, session=session)
        self.assertEqual(result['access_token'], 'reddit-token')
        self.assertEqual(result['me']['name'], 'brand-main')

    def test_refresh_youtube_access_token_returns_channel_metadata(self):
        session = _RecordingSession([
            _FakeResponse({'access_token': 'yt-token', 'expires_in': 3600}),
            _FakeResponse({'items': [{'snippet': {'title': 'Demo Channel'}}]}),
        ])
        account = {
            'channelId': 'UC123',
            'clientId': 'cid',
            'clientSecret': 'secret',
            'refreshToken': 'refresh',
        }
        result = prepared_publishers.refresh_youtube_access_token(account, session=session)
        self.assertEqual(result['access_token'], 'yt-token')
        self.assertEqual(result['channel']['items'][0]['snippet']['title'], 'Demo Channel')

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

    def test_tiktok_video_rejects_pull_from_url_file_over_one_gb(self):
        session = _RecordingSession([
            _FakeResponse({'data': {'creator_avatar_url': 'x'}}),
        ])
        account = SimpleNamespace(config={'accessToken': 'tt-token'})
        with tempfile.TemporaryDirectory() as tmp,              patch.object(prepared_publishers.media_pipeline, 'probe_video_duration', return_value=120.0),              patch.object(prepared_publishers, 'TIKTOK_MAX_PULL_FROM_URL_BYTES', 1):
            video = Path(tmp) / 'clip.mp4'
            video.write_bytes(b'video')
            with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
                prepared_publishers.publish_tiktok_sync(
                    account,
                    {
                        'message': 'TikTok launch',
                        'artifacts': [{'public_url': 'https://cdn.example/video.mp4', 'local_path': str(video), 'artifact_kind': 'remote_upload'}],
                    },
                    session=session,
                )
        self.assertIn('1 GB', str(ctx.exception))

    def test_tiktok_video_rejects_duration_over_sixty_minutes(self):
        session = _RecordingSession([
            _FakeResponse({'data': {'creator_avatar_url': 'x'}}),
        ])
        account = SimpleNamespace(config={'accessToken': 'tt-token'})
        with tempfile.TemporaryDirectory() as tmp, patch.object(prepared_publishers.media_pipeline, 'probe_video_duration', return_value=3601.0):
            video = Path(tmp) / 'clip.mp4'
            video.write_bytes(b'video')
            with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
                prepared_publishers.publish_tiktok_sync(
                    account,
                    {
                        'message': 'TikTok launch',
                        'artifacts': [{'public_url': 'https://cdn.example/video.mp4', 'local_path': str(video), 'artifact_kind': 'remote_upload'}],
                    },
                    session=session,
                )
        self.assertIn('exceeds the limit', str(ctx.exception))

    def test_tiktok_video_rejects_caption_over_2200_chars(self):
        session = _RecordingSession([
            _FakeResponse({'data': {'creator_avatar_url': 'x'}}),
        ])
        account = SimpleNamespace(config={'accessToken': 'tt-token'})
        with tempfile.TemporaryDirectory() as tmp, patch.object(prepared_publishers.media_pipeline, 'probe_video_duration', return_value=120.0):
            video = Path(tmp) / 'clip.mp4'
            video.write_bytes(b'video')
            with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
                prepared_publishers.publish_tiktok_sync(
                    account,
                    {
                        'message': 'x' * 2201,
                        'artifacts': [{'public_url': 'https://cdn.example/video.mp4', 'local_path': str(video), 'artifact_kind': 'remote_upload'}],
                    },
                    session=session,
                )
        self.assertIn('2200', str(ctx.exception))


class RaiseForStatusTests(unittest.TestCase):
    """_raise_for_status surfaces the platform error and redacts tokens."""

    def test_surfaces_platform_error_message(self):
        class _Resp:
            status_code = 400

            def raise_for_status(self):
                raise Exception("400 Bad Request")

            def json(self):
                return {"error": {"message": "API access blocked.", "code": 200, "type": "OAuthException"}}

        with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
            prepared_publishers._raise_for_status(_Resp())
        message = str(ctx.exception)
        self.assertIn("API access blocked.", message)
        self.assertIn("code=200", message)

    def test_redact_tokens_helper(self):
        self.assertEqual(
            prepared_publishers._redact_tokens("https://g/v1?access_token=ABC123&x=1"),
            "https://g/v1?access_token=<redacted>&x=1",
        )

    def test_token_never_leaks_when_no_json_body(self):
        class _Resp:
            status_code = 400

            def raise_for_status(self):
                raise Exception("400 for url https://graph/v1?access_token=SUPERSECRET")

            def json(self):
                raise ValueError("no json")

        with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
            prepared_publishers._raise_for_status(_Resp())
        message = str(ctx.exception)
        self.assertNotIn("SUPERSECRET", message)
        self.assertIn("<redacted>", message)


class RedditPublisherTests(unittest.TestCase):
    """Comprehensive tests for Reddit publishing functions."""

    # --- validate_reddit_config_live ---

    def test_validate_reddit_config_live_returns_token_and_me(self):
        session = _RecordingSession([
            _FakeResponse({"access_token": "reddit-token"}),
            _FakeResponse({"name": "brand-user"}),
        ])
        result = prepared_publishers.validate_reddit_config_live(
            {"clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh"},
            session=session,
        )
        self.assertEqual(result["access_token"], "reddit-token")
        self.assertEqual(result["me"]["name"], "brand-user")
        self.assertEqual(session.calls[0][1], prepared_publishers.REDDIT_TOKEN_URL)
        self.assertEqual(session.calls[1][1], prepared_publishers.REDDIT_ME_URL)

    def test_validate_reddit_config_live_uses_custom_user_agent(self):
        session = _RecordingSession([
            _FakeResponse({"access_token": "token"}),
            _FakeResponse({"name": "user"}),
        ])
        prepared_publishers.validate_reddit_config_live(
            {"clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh", "userAgent": "custom-agent/1.0"},
            session=session,
        )
        self.assertEqual(session.calls[1][2]["headers"]["User-Agent"], "custom-agent/1.0")

    # --- refresh_reddit_access_token error paths ---

    def test_refresh_reddit_requires_credentials(self):
        session = _RecordingSession()
        with patch.dict(os.environ, {"REDDIT_CLIENT_ID": "", "REDDIT_CLIENT_SECRET": "", "REDDIT_REFRESH_TOKEN": ""}, clear=False):
            with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
                prepared_publishers.refresh_reddit_access_token({}, session=session)
            self.assertIn("requires clientId", str(ctx.exception))

    def test_refresh_reddit_requires_client_secret(self):
        session = _RecordingSession()
        with patch.dict(os.environ, {"REDDIT_CLIENT_ID": "", "REDDIT_CLIENT_SECRET": "", "REDDIT_REFRESH_TOKEN": ""}, clear=False):
            with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
                prepared_publishers.refresh_reddit_access_token(
                    {"clientId": "cid", "refreshToken": "refresh"}, session=session
                )
            self.assertIn("requires clientId", str(ctx.exception))

    def test_refresh_reddit_requires_refresh_token(self):
        session = _RecordingSession()
        with patch.dict(os.environ, {"REDDIT_CLIENT_ID": "", "REDDIT_CLIENT_SECRET": "", "REDDIT_REFRESH_TOKEN": ""}, clear=False):
            with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
                prepared_publishers.refresh_reddit_access_token(
                    {"clientId": "cid", "clientSecret": "secret"}, session=session
                )
            self.assertIn("requires clientId", str(ctx.exception))

    def test_refresh_reddit_raises_when_no_access_token_in_response(self):
        session = _RecordingSession([
            _FakeResponse({"expires_in": 3600}),  # no access_token
        ])
        with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
            prepared_publishers.refresh_reddit_access_token(
                {"clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh"},
                session=session,
            )
        self.assertIn("access_token", str(ctx.exception))

    # --- _reddit_access_token error paths ---

    def test_reddit_access_token_requires_all_credentials(self):
        session = _RecordingSession()
        with self.assertRaises(prepared_publishers.PreparedPublishError):
            prepared_publishers._reddit_access_token({}, session=session)

    def test_reddit_access_token_raises_when_no_token_in_response(self):
        session = _RecordingSession([
            _FakeResponse({"token_type": "bearer"}),  # no access_token
        ])
        with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
            prepared_publishers._reddit_access_token(
                {"clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh"},
                session=session,
            )
        self.assertIn("access_token", str(ctx.exception))

    def test_reddit_access_token_resolves_env_references(self):
        session = _RecordingSession([
            _FakeResponse({"access_token": "env-token"}),
        ])
        with patch.dict(os.environ, {"MY_CID": "cid", "MY_SECRET": "secret", "MY_REFRESH": "refresh"}, clear=False):
            token = prepared_publishers._reddit_access_token(
                {"clientIdEnv": "MY_CID", "clientSecretEnv": "MY_SECRET", "refreshTokenEnv": "MY_REFRESH"},
                session=session,
            )
        self.assertEqual(token, "env-token")

    # --- publish_reddit_sync error paths ---

    def test_publish_reddit_requires_non_empty_subreddits(self):
        session = _RecordingSession()
        account = SimpleNamespace(account_name="test", config={"subreddits": []})
        with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
            prepared_publishers.publish_reddit_sync(account, {"message": "test"}, session=session)
        self.assertIn("subreddits", str(ctx.exception))

    def test_publish_reddit_splits_comma_separated_subreddits(self):
        session = _RecordingSession([
            _FakeResponse({"access_token": "token"}),
            _FakeResponse({"json": {"errors": []}}),
        ])
        account = SimpleNamespace(
            account_name="test",
            config={"clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh", "subreddits": "suba,subb"},
        )
        prepared_publishers.publish_reddit_sync(account, {"message": "test"}, session=session)
        self.assertEqual(session.calls[1][2]["data"]["sr"], "suba")

    def test_publish_reddit_raises_on_api_errors(self):
        session = _RecordingSession([
            _FakeResponse({"access_token": "token"}),
            _FakeResponse({"json": {"errors": [["NO_TEXT", "you need to enter text"]]}}),
        ])
        account = SimpleNamespace(
            account_name="test",
            config={"clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh", "subreddits": ["test"]},
        )
        with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
            prepared_publishers.publish_reddit_sync(account, {"message": "test"}, session=session)
        self.assertIn("r/test", str(ctx.exception))
        self.assertIn("NO_TEXT", str(ctx.exception))

    def test_publish_reddit_uses_self_post_when_no_media(self):
        session = _RecordingSession([
            _FakeResponse({"access_token": "token"}),
            _FakeResponse({"json": {"errors": []}}),
        ])
        account = SimpleNamespace(
            account_name="test",
            config={"clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh", "subreddits": ["test"]},
        )
        prepared_publishers.publish_reddit_sync(
            account,
            {"message": "Hello Reddit"},
            session=session,
        )
        data = session.calls[1][2]["data"]
        self.assertEqual(data["kind"], "self")
        self.assertEqual(data["text"], "Hello Reddit")

    def test_publish_reddit_uses_link_post_when_media_url_present(self):
        session = _RecordingSession([
            _FakeResponse({"access_token": "token"}),
            _FakeResponse({"json": {"errors": []}}),
        ])
        account = SimpleNamespace(
            account_name="test",
            config={"clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh", "subreddits": ["test"]},
        )
        prepared_publishers.publish_reddit_sync(
            account,
            {
                "message": "Video post",
                "artifacts": [{"public_url": "https://cdn.example/video.mp4", "artifact_kind": "remote_upload"}],
            },
            session=session,
        )
        data = session.calls[1][2]["data"]
        self.assertEqual(data["kind"], "link")
        self.assertEqual(data["url"], "https://cdn.example/video.mp4")

    def test_publish_reddit_truncates_title_to_300_chars(self):
        session = _RecordingSession([
            _FakeResponse({"access_token": "token"}),
            _FakeResponse({"json": {"errors": []}}),
        ])
        account = SimpleNamespace(
            account_name="test",
            config={"clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh", "subreddits": ["test"]},
        )
        long_title = "A" * 500
        prepared_publishers.publish_reddit_sync(
            account,
            {"message": long_title},
            session=session,
        )
        data = session.calls[1][2]["data"]
        # _message_title truncates to 100 chars first, then publish_reddit_sync truncates to 300
        self.assertLessEqual(len(data["title"]), 300)

    def test_publish_reddit_uses_draft_subreddits_over_config(self):
        session = _RecordingSession([
            _FakeResponse({"access_token": "token"}),
            _FakeResponse({"json": {"errors": []}}),
        ])
        account = SimpleNamespace(
            account_name="test",
            config={"clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh", "subreddits": ["config_sub"]},
        )
        prepared_publishers.publish_reddit_sync(
            account,
            {"message": "test", "draft": {"subreddits": ["draft_sub"]}},
            session=session,
        )
        data = session.calls[1][2]["data"]
        self.assertEqual(data["sr"], "draft_sub")

    def test_publish_reddit_extracts_video_over_image(self):
        session = _RecordingSession([
            _FakeResponse({"access_token": "token"}),
            _FakeResponse({"json": {"errors": []}}),
        ])
        account = SimpleNamespace(
            account_name="test",
            config={"clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh", "subreddits": ["test"]},
        )
        prepared_publishers.publish_reddit_sync(
            account,
            {
                "message": "Post with media",
                "artifacts": [
                    {"public_url": "https://cdn.example/image.jpg", "artifact_kind": "watermarked_image"},
                    {"public_url": "https://cdn.example/video.mp4", "artifact_kind": "remote_upload"},
                ],
            },
            session=session,
        )
        data = session.calls[1][2]["data"]
        self.assertEqual(data["kind"], "link")
        self.assertEqual(data["url"], "https://cdn.example/video.mp4")


if __name__ == "__main__":
    unittest.main()
