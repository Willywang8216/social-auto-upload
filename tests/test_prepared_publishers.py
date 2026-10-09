"""Tests for prepared-campaign HTTP publishers."""

from __future__ import annotations

import base64
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
    conf_module.DEBUG_MODE = True
    conf_module.LOCAL_CHROME_HEADLESS = True
    conf_module.LOCAL_CHROME_PATH = ""
    sys.modules["conf"] = conf_module

from myUtils import prepared_publishers


class _FakeResponse:
    def __init__(self, payload=None, *, headers=None, status_code=200, text=""):
        self._payload = payload or {}
        self.headers = headers or {}
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            from requests import HTTPError
            raise HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


class _RecordingSession:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def _next(self):
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
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

    def test_reddit_flair_and_no_images_errors_are_non_retryable(self):
        for error in (
            ["SUBMIT_VALIDATION_FLAIR_REQUIRED", "Your post must contain post flair.", "flair"],
            ["NO_IMAGES", "This community doesn't allow images", "sr"],
        ):
            session = _RecordingSession([
                _FakeResponse({"access_token": "reddit-token"}),
                _FakeResponse({"json": {"errors": [error]}}),
            ])
            account = SimpleNamespace(
                account_name="brand-main",
                config={"clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh", "subreddits": ["suba"]},
            )
            with self.assertRaises(prepared_publishers.PreparedPublishError) as raised:
                prepared_publishers.publish_reddit_sync(account, {"message": "post"}, session=session)
            self.assertFalse(raised.exception.retryable)

    def test_telegram_live_validation_calls_getme_and_getchat(self):
        session = _RecordingSession([_FakeResponse({"ok": True}), _FakeResponse({"ok": True})])
        result = prepared_publishers.validate_telegram_config_live(
            {"botToken": "token", "chatId": "@brand"},
            session=session,
        )
        self.assertEqual(session.calls[0][1], "https://api.telegram.org/bottoken/getMe")
        self.assertEqual(session.calls[1][1], "https://api.telegram.org/bottoken/getChat")
        self.assertIn("chat", result)

    def test_telegram_live_validation_fans_out_to_each_chat_id(self):
        session = _RecordingSession([
            _FakeResponse({"ok": True}),
            _FakeResponse({"ok": True}),
            _FakeResponse({"ok": True}),
        ])
        result = prepared_publishers.validate_telegram_config_live(
            {"botToken": "token", "chatIds": ["@a", "@b"]},
            session=session,
        )
        urls = [call[1] for call in session.calls]
        self.assertEqual(urls[0], "https://api.telegram.org/bottoken/getMe")
        self.assertEqual(urls[1], "https://api.telegram.org/bottoken/getChat")
        self.assertEqual(urls[2], "https://api.telegram.org/bottoken/getChat")
        self.assertEqual(len(result["chats"]), 2)
        self.assertEqual(result["chats"][0]["chatId"], "@a")
        self.assertEqual(result["chats"][1]["chatId"], "@b")
        # Back-compat: the legacy singular key still points at the first chat.
        self.assertEqual(result["chat"], result["chats"][0]["result"])

    def test_telegram_publish_fans_out_to_each_chat_id(self):
        session = _RecordingSession([
            _FakeResponse({"ok": True}),
            _FakeResponse({"ok": True}),
            _FakeResponse({"ok": True}),
        ])
        account = SimpleNamespace(config={"botToken": "token", "chatIds": ["@a", "@b"]})
        prepared_publishers.publish_telegram_sync(
            account,
            {"message": "hello both"},
            session=session,
        )
        urls = [call[1] for call in session.calls]
        self.assertEqual(urls.count("https://api.telegram.org/bottoken/sendMessage"), 2)

    def test_telegram_publish_draft_chat_ids_override_config(self):
        # Config provides two chats, draft overrides with a single chat. Only
        # the draft's chat should receive the message.
        session = _RecordingSession([
            _FakeResponse({"ok": True}),
            _FakeResponse({"ok": True}),
        ])
        account = SimpleNamespace(config={"botToken": "token", "chatIds": ["@config-a", "@config-b"]})
        prepared_publishers.publish_telegram_sync(
            account,
            {"message": "hi", "draft": {"chatIds": ["@override"]}},
            session=session,
        )
        sent_chat_ids = []
        for call in session.calls:
            if call[1].endswith("sendMessage"):
                sent_chat_ids.append(call[2]["data"]["chat_id"])
        self.assertEqual(sent_chat_ids, ["@override"])

    def test_telegram_publish_partial_failure_returns_per_chat_status(self):
        # Partial delivery raises so the worker can retry only chats without a
        # confirmed completed operation, while retaining the status summary.
        from requests import HTTPError

        bad = _FakeResponse({}, status_code=400)
        bad.raise_for_status = lambda: (_ for _ in ()).throw(HTTPError("400 chat not found"))
        session = _RecordingSession([_FakeResponse({"ok": True}), bad])
        account = SimpleNamespace(config={"botToken": "token", "chatIds": ["@good", "@bad"]})
        payload = {"message": "mixed"}
        with self.assertRaises(prepared_publishers.PreparedPublishError) as raised:
            prepared_publishers.publish_telegram_sync(account, payload, session=session)

        self.assertEqual(raised.exception.details["platform"], "telegram")
        self.assertTrue(raised.exception.details["partial"])
        self.assertEqual(raised.exception.details["chats"][0]["chatId"], "@good")
        self.assertTrue(raised.exception.details["chats"][0]["ok"])
        self.assertEqual(raised.exception.details["chats"][1]["chatId"], "@bad")
        self.assertFalse(raised.exception.details["chats"][1]["ok"])
        self.assertEqual(payload["telegramCompletedByDelivery"]["default"]["@good"], ["message"])

    def test_telegram_partial_failure_preserves_results_and_completed_operations(self):
        bad = _FakeResponse({"ok": False, "description": "chat not found"}, status_code=400)
        account = SimpleNamespace(config={"botToken": "token", "chatIds": ["@good", "@bad"]})
        payload = {"message": "mixed"}
        first_session = _RecordingSession([_FakeResponse({"ok": True}), bad])
        with self.assertRaises(prepared_publishers.PreparedPublishError) as raised:
            prepared_publishers.publish_telegram_sync(account, payload, session=first_session)
        self.assertTrue(raised.exception.retryable)
        self.assertTrue(raised.exception.details["partial"])
        self.assertEqual(
            payload["telegramCompletedByDelivery"]["default"]["@good"], ["message"]
        )
        self.assertEqual(payload["telegramCompletedByDelivery"]["default"]["@bad"], [])

        second_session = _RecordingSession([_FakeResponse({"ok": True})])
        prepared_publishers.publish_telegram_sync(account, payload, session=second_session)
        self.assertEqual(len(second_session.calls), 1)
        self.assertEqual(second_session.calls[0][2]["data"]["chat_id"], "@bad")

    def test_telegram_send_timeout_is_not_blindly_retried(self):
        session = _RecordingSession([TimeoutError("connection timed out after sending")])
        account = SimpleNamespace(config={"botToken": "token", "chatId": "@chat"})
        payload = {"message": "may have been accepted"}
        with self.assertRaises(prepared_publishers.PreparedPublishError) as raised:
            prepared_publishers.publish_telegram_sync(account, payload, session=session)
        self.assertFalse(raised.exception.retryable)
        self.assertIn(
            "ambiguous:message",
            payload["telegramCompletedByDelivery"]["default"]["@chat"],
        )

    def test_telegram_completion_state_is_scoped_per_worker_target(self):
        account = SimpleNamespace(config={"botToken": "token", "chatId": "@chat"})
        first_target_payload = {"message": "first", "_telegramDeliveryKey": "target-1"}
        prepared_publishers.publish_telegram_sync(
            account, first_target_payload, session=_RecordingSession([_FakeResponse({"ok": True})])
        )
        second_target_payload = {"message": "second", "_telegramDeliveryKey": "target-2"}
        second_session = _RecordingSession([_FakeResponse({"ok": True})])
        prepared_publishers.publish_telegram_sync(account, second_target_payload, session=second_session)
        self.assertEqual(len(second_session.calls), 1)
        self.assertEqual(second_session.calls[0][2]["data"]["text"], "second")

    def test_telegram_publish_all_chats_fail_raises(self):
        # Every chat fails -> aggregate error so the worker marks the job failed.
        from requests import HTTPError

        bad = _FakeResponse({}, status_code=400)
        bad.raise_for_status = lambda: (_ for _ in ()).throw(HTTPError("400 chat not found"))
        session = _RecordingSession([bad, bad])
        account = SimpleNamespace(config={"botToken": "token", "chatIds": ["@a", "@b"]})
        with self.assertRaises(prepared_publishers.PreparedPublishError):
            prepared_publishers.publish_telegram_sync(
                account,
                {"message": "all bad"},
                session=session,
            )

    def test_telegram_publish_legacy_chat_id_still_works(self):
        # Single-string ``chatId`` keeps working for backwards compatibility.
        session = _RecordingSession([_FakeResponse({"ok": True})])
        account = SimpleNamespace(config={"botToken": "token", "chatId": "@legacy"})
        prepared_publishers.publish_telegram_sync(
            account,
            {"message": "legacy"},
            session=session,
        )
        self.assertEqual(session.calls[0][1], "https://api.telegram.org/bottoken/sendMessage")
        self.assertEqual(session.calls[0][2]["data"]["chat_id"], "@legacy")

    def test_facebook_live_validation_fetches_page(self):
        session = _RecordingSession([_FakeResponse({"id": "123", "name": "Brand Page"})])
        result = prepared_publishers.validate_facebook_config_live(
            {"pageId": "123", "accessToken": "fb-token"},
            session=session,
        )
        self.assertEqual(session.calls[0][0], "GET")
        self.assertEqual(session.calls[0][1], f"{prepared_publishers.FACEBOOK_GRAPH_ROOT}/123")
        self.assertEqual(result["id"], "123")

    def test_discord_attaches_local_media_with_multipart(self):
        session = _RecordingSession([_FakeResponse({})])
        account = SimpleNamespace(config={"webhookUrl": "https://discord.example/webhook"})
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "cover.jpg"
            image.write_bytes(b"image")
            prepared_publishers.publish_discord_sync(
                account,
                {"message": "Discord launch", "artifacts": [{
                    "local_path": str(image), "artifact_kind": "watermarked_image",
                }]},
                session=session,
            )
        self.assertEqual(session.calls[0][1], "https://discord.example/webhook")
        self.assertIn("files[0]", session.calls[0][2]["files"])
        payload_json = json.loads(session.calls[0][2]["data"]["payload_json"])
        self.assertEqual(payload_json["content"], "Discord launch")

    def test_discord_rejects_missing_media_and_embeds_public_image_url(self):
        account = SimpleNamespace(config={"webhookUrl": "https://discord.example/webhook"})
        session = _RecordingSession()
        with self.assertRaises(prepared_publishers.PreparedPublishError):
            prepared_publishers.publish_discord_sync(
                account, {"message": "Discord attachment", "artifacts": [{
                    "local_path": "/tmp/missing-discord.jpg", "artifact_kind": "watermarked_image",
                }]}, session=session,
            )
        self.assertEqual(session.calls, [])

        session = _RecordingSession([_FakeResponse({})])
        prepared_publishers.publish_discord_sync(
            account, {"message": "Discord attachment", "artifacts": [{
                "public_url": "https://cdn.example/image.jpg", "artifact_kind": "watermarked_image",
            }]}, session=session,
        )
        self.assertEqual(session.calls[0][2]["json"]["embeds"][0]["image"]["url"], "https://cdn.example/image.jpg")

    def test_discord_text_only_without_artifacts_remains_allowed(self):
        session = _RecordingSession([_FakeResponse({})])
        prepared_publishers.publish_discord_sync(
            SimpleNamespace(config={"webhookUrl": "https://discord.example/webhook"}),
            {"message": "intentional text-only"}, session=session,
        )
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][2]["json"]["content"], "intentional text-only")

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

    def test_instagram_single_image_waits_for_container_then_publishes(self):
        session = _RecordingSession([
            _FakeResponse({"id": "ig-container"}),
            _FakeResponse({"status_code": "FINISHED"}),
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
        # container status is polled before media_publish
        self.assertEqual(session.calls[1][0], "GET")
        self.assertEqual(session.calls[1][1], f"{prepared_publishers.FACEBOOK_GRAPH_ROOT}/ig-container")
        self.assertEqual(session.calls[2][1], f"{prepared_publishers.FACEBOOK_GRAPH_ROOT}/1789/media_publish")

    def test_instagram_video_container_not_finished_raises(self):
        session = _RecordingSession([
            _FakeResponse({"id": "ig-container"}),
            _FakeResponse({"status_code": "ERROR", "error_message": "media timed out"}),
        ])
        account = SimpleNamespace(config={"igUserId": "1789", "accessToken": "ig-token"})
        with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
            prepared_publishers.publish_instagram_sync(
                account,
                {
                    "message": "Instagram reel",
                    "artifacts": [{"public_url": "https://cdn.example/video.mp4", "artifact_kind": "watermarked_video"}],
                },
                session=session,
            )
        self.assertIn("media timed out", str(ctx.exception))

    def test_twitter_refresh_failure_marks_account_reconnect_before_raising(self):
        config = {"twitterAuthType": "api", "refreshToken": "refresh", "accessToken": "expired"}
        from requests import HTTPError
        from unittest.mock import Mock

        config["accessTokenExpiresAt"] = "2000-01-01T00:00:00"
        response = Mock()
        response.json.return_value = {"error": "invalid_grant"}
        failure = HTTPError("refresh token rejected", response=response)
        persisted = []
        with patch("myUtils.prepared_publishers.refresh_twitter_access_token", side_effect=failure):
            with self.assertRaisesRegex(prepared_publishers.PreparedPublishError, "reconnect this account"):
                prepared_publishers._maybe_refresh_twitter_token(config, on_refresh=persisted.append)
        self.assertTrue(persisted[0]["_needsReconnect"])
        self.assertEqual(persisted[0]["_lastMaintenanceError"], "X OAuth 2.0 refresh token was rejected; reconnect required")
        with patch("myUtils.prepared_publishers.refresh_twitter_access_token", side_effect=RuntimeError("network timeout")):
            with self.assertRaises(prepared_publishers.PreparedPublishError) as raised:
                prepared_publishers._maybe_refresh_twitter_token(config)
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(len(persisted), 1)

    def test_successful_twitter_refresh_clears_reconnect_markers(self):
        config = {
            "twitterAuthType": "api",
            "refreshToken": "old-refresh",
            "accessTokenExpiresAt": "2000-01-01T00:00:00",
            "_maintenanceFailures": 3,
            "_nextMaintenanceAttemptAt": "old-backoff",
            "_lastMaintenanceError": "old-error",
            "_lastMaintenanceAttemptAt": "old-attempt",
        }
        persisted = []
        result = {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}
        with patch("myUtils.prepared_publishers.refresh_twitter_access_token", return_value=result):
            updated = prepared_publishers._maybe_refresh_twitter_token(config, on_refresh=persisted.append)
        self.assertEqual(updated["refreshToken"], "new-refresh")
        self.assertEqual(persisted[0]["refreshToken"], "new-refresh")
        for marker in (
            "_needsReconnect", "_reconnectAlertedAt", "_maintenanceFailures",
            "_nextMaintenanceAttemptAt", "_lastMaintenanceError",
            "_lastMaintenanceAttemptAt",
        ):
            self.assertNotIn(marker, updated)

    def test_x_401_is_reported_with_request_stage_and_stops_retries(self):
        from requests import HTTPError
        from unittest.mock import Mock
        response = Mock()
        response.status_code = 401
        response.json.return_value = {"errors": [{"message": "Invalid or expired token", "code": 89}]}
        error = HTTPError("opaque provider exception", response=response)
        wrapped = prepared_publishers._raise_x_publish_error(error, stage="media upload (OAuth 1.0a)")
        self.assertFalse(wrapped.retryable)
        self.assertEqual(wrapped.details, {"stage": "media upload (OAuth 1.0a)", "status": 401, "code": "89"})
        self.assertNotIn("access_token", str(wrapped).lower())

    def test_x_stringified_401_is_classified_as_permanent(self):
        error = RuntimeError("HTTP 401: {'errors': [{'message': 'Invalid or expired token', 'code': 89}]}")
        wrapped = prepared_publishers._raise_x_publish_error(error, stage="media upload (OAuth 1.0a)")
        self.assertFalse(wrapped.retryable)
        self.assertEqual(wrapped.details["status"], 401)
        self.assertEqual(wrapped.details["code"], "89")
        self.assertIn("media upload", str(wrapped))

    def test_twitter_media_401_names_oauth2_upload_stage(self):
        from requests import HTTPError
        from unittest.mock import Mock
        response = Mock()
        response.status_code = 401
        response.json.return_value = {"errors": [{"message": "Invalid or expired token", "code": 89}]}
        session = _RecordingSession([HTTPError("opaque", response=response)])
        account = SimpleNamespace(config={"accessToken": "oauth2-user", "scope": "tweet.write media.write offline.access"})
        with tempfile.TemporaryDirectory() as tmp:
            media_path = Path(tmp) / "photo.jpg"
            media_path.write_bytes(b"image")
            with self.assertRaises(prepared_publishers.PreparedPublishError) as raised:
                prepared_publishers.publish_twitter_sync(
                    account,
                    {"message": "photo", "artifacts": [{"local_path": str(media_path), "artifact_kind": "image"}]},
                    session=session,
                )
        self.assertEqual(raised.exception.details["stage"], "media upload (OAuth 2.0)")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(session.calls[0][1], prepared_publishers.X_MEDIA_INITIALIZE_URL)

    def test_twitter_media_upload_requires_media_write_scope_before_request(self):
        account = SimpleNamespace(config={"accessToken": "oauth2", "scope": "tweet.write offline.access"})
        with tempfile.TemporaryDirectory() as tmp:
            media_path = Path(tmp) / "clip.mp4"
            media_path.write_bytes(b"video")
            session = _RecordingSession([])
            with self.assertRaisesRegex(prepared_publishers.PreparedPublishError, "media.write") as raised:
                prepared_publishers.publish_twitter_sync(
                    account,
                    {"message": "needs scope", "artifacts": [{"local_path": str(media_path), "artifact_kind": "video"}]},
                    session=session,
                )
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(session.calls, [])

    def test_twitter_api_uploads_media_with_oauth2_and_attaches_media_id(self):
        session = _RecordingSession([
            _FakeResponse({"data": {"id": "mid-1"}}),
            _FakeResponse({}),
            _FakeResponse({}),
            _FakeResponse({"data": {"id": "tweet-1", "text": "post"}}),
        ])
        account = SimpleNamespace(config={
            "accessToken": "oauth2",
            "scope": "tweet.write media.write offline.access",
        })
        with tempfile.TemporaryDirectory() as tmp:
            media_path = Path(tmp) / "clip.mp4"
            media_path.write_bytes(b"video")
            result = prepared_publishers.publish_twitter_sync(
                account,
                {"message": "scheduled X", "artifacts": [{
                    "local_path": str(media_path), "artifact_kind": "remote_upload",
                }]},
                session=session,
            )
        self.assertEqual(result["results"][0]["data"]["id"], "tweet-1")
        self.assertEqual(session.calls[0][1], prepared_publishers.X_MEDIA_INITIALIZE_URL)
        self.assertEqual(session.calls[0][2]["headers"]["Authorization"], "Bearer oauth2")
        self.assertTrue(all(call[2]["headers"]["Authorization"] == "Bearer oauth2" for call in session.calls[:4]))
        tweet_request = session.calls[-1][2]
        tweet_body = json.loads(tweet_request["data"])
        self.assertEqual(tweet_body["media"]["media_ids"], ["mid-1"])

    def test_twitter_api_video_upload_chunks_and_waits_for_processing(self):
        session = _RecordingSession([
            _FakeResponse({"data": {"id": "mid-large"}}),
            _FakeResponse({}),
            _FakeResponse({}),
            _FakeResponse({"data": {"processing_info": {"state": "pending", "check_after_secs": 1}}}),
            _FakeResponse({"data": {"processing_info": {"state": "succeeded"}}}),
            _FakeResponse({"data": {"id": "tweet-large", "text": "post"}}),
        ])
        account = SimpleNamespace(config={
            "accessToken": "oauth2",
            "scope": "tweet.write media.write offline.access",
        })
        with tempfile.TemporaryDirectory() as tmp:
            media_path = Path(tmp) / "clip.mp4"
            media_path.write_bytes(b"x" * (5 * 1024 * 1024))
            with patch("myUtils.prepared_publishers.time.sleep"):
                prepared_publishers.publish_twitter_sync(
                    account,
                    {"message": "scheduled X", "artifacts": [{
                        "local_path": str(media_path), "artifact_kind": "remote_upload",
                    }]},
                    session=session,
                )
        append_calls = [c for c in session.calls if c[1].endswith("/append")]
        self.assertEqual(len(append_calls), 2)
        self.assertEqual(append_calls[0][2]["data"]["segment_index"], "0")
        self.assertEqual(append_calls[1][2]["data"]["segment_index"], "1")
        status_calls = [c for c in session.calls if c[0] == "GET" and c[1] == prepared_publishers.X_MEDIA_UPLOAD_URL]
        self.assertEqual(len(status_calls), 1)
        create_tweet_call = next(c for c in session.calls if c[1] == prepared_publishers.X_TWEET_URL)
        self.assertIn("media", json.loads(create_tweet_call[2]["data"]))

    def test_twitter_api_rejects_mixed_media(self):
        account = SimpleNamespace(config={"accessToken": "oauth2"})
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "photo.jpg"
            video = Path(tmp) / "clip.mp4"
            image.write_bytes(b"image")
            video.write_bytes(b"video")
            with self.assertRaisesRegex(prepared_publishers.PreparedPublishError, "cannot mix"):
                prepared_publishers.publish_twitter_sync(
                    account,
                    {"message": "post", "artifacts": [
                        {"local_path": str(image), "artifact_kind": "image"},
                        {"local_path": str(video), "artifact_kind": "video"},
                    ]},
                    session=_RecordingSession(),
                )

    def test_bluesky_media_artifact_without_source_fails_before_record_create(self):
        account = SimpleNamespace(config={"handle": "h", "appPassword": "p"})
        with self.assertRaisesRegex(prepared_publishers.PreparedPublishError, "no readable local file or public URL"):
            prepared_publishers.publish_bluesky_sync(
                account,
                {"message": "post", "artifacts": [{
                    "local_path": "/tmp/missing.jpg", "artifact_kind": "watermarked_image",
                }]},
                session=_RecordingSession(),
            )

    def test_twitter_api_rejects_public_url_only_media(self):
        account = SimpleNamespace(config={"accessToken": "oauth2"})
        with self.assertRaisesRegex(prepared_publishers.PreparedPublishError, "local media path"):
            prepared_publishers.publish_twitter_sync(
                account,
                {"message": "scheduled X", "artifacts": [{
                    "public_url": "https://cdn.example/clip.mp4",
                    "artifact_kind": "remote_upload",
                }]},
                session=_RecordingSession(),
            )

    def test_twitter_api_text_only_remains_allowed(self):
        session = _RecordingSession([_FakeResponse({"data": {"id": "tweet-2"}})])
        account = SimpleNamespace(config={"accessToken": "oauth2"})
        prepared_publishers.publish_twitter_sync(
            account, {"message": "text-only"}, session=session,
        )
        self.assertNotIn("media", json.loads(session.calls[-1][2]["data"]))

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
        # text-only: no container-status poll, straight to publish
        self.assertEqual(session.calls[1][1], f"{prepared_publishers.THREADS_GRAPH_ROOT}/42/threads_publish")

    def test_threads_long_text_is_truncated_before_container_creation(self):
        session = _RecordingSession([
            _FakeResponse({"id": "threads-container"}),
            _FakeResponse({"id": "threads-post"}),
        ])
        account = SimpleNamespace(config={"threadUserId": "42", "accessToken": "threads-token", "accessTokenExpiresAt": "2099-01-01T00:00:00"})
        long_message = "x" * 600
        prepared_publishers.publish_threads_sync(
            account,
            {"message": long_message},
            session=session,
        )
        container_data = session.calls[0][2]["data"]
        self.assertEqual(len(container_data["text"]), prepared_publishers.THREADS_MAX_TEXT_CHARS)
        self.assertEqual(container_data["text"], long_message[:prepared_publishers.THREADS_MAX_TEXT_CHARS])

    def test_threads_video_waits_for_container_then_publishes(self):
        session = _RecordingSession([
            _FakeResponse({"id": "tv-container"}),
            _FakeResponse({"status": "FINISHED"}),
            _FakeResponse({"id": "tv-post"}),
        ])
        account = SimpleNamespace(config={"threadUserId": "42", "accessToken": "threads-token", "accessTokenExpiresAt": "2099-01-01T00:00:00"})
        result = prepared_publishers.publish_threads_sync(
            account,
            {
                "message": "Threads video",
                "artifacts": [{"public_url": "https://cdn.example/video.mp4", "artifact_kind": "watermarked_video"}],
            },
            session=session,
        )
        self.assertEqual(result["container_id"], "tv-container")
        self.assertEqual(session.calls[0][1], f"{prepared_publishers.THREADS_GRAPH_ROOT}/42/threads")
        self.assertEqual(session.calls[1][0], "GET")
        self.assertEqual(session.calls[1][1], f"{prepared_publishers.THREADS_GRAPH_ROOT}/tv-container")
        self.assertEqual(session.calls[2][1], f"{prepared_publishers.THREADS_GRAPH_ROOT}/42/threads_publish")

    def test_threads_container_error_message_is_surfaced(self):
        session = _RecordingSession([
            _FakeResponse({"id": "tv-container"}),
            _FakeResponse({"status": "ERROR", "error_message": "The video could not be processed."}),
        ])
        account = SimpleNamespace(config={"threadUserId": "42", "accessToken": "threads-token", "accessTokenExpiresAt": "2099-01-01T00:00:00"})
        with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
            prepared_publishers.publish_threads_sync(
                account,
                {
                    "message": "Threads video",
                    "artifacts": [{"public_url": "https://cdn.example/video.mp4", "artifact_kind": "watermarked_video"}],
                },
                session=session,
            )
        self.assertIn("The video could not be processed.", str(ctx.exception))
        # Meta only returns error_message when it is explicitly requested.
        self.assertIn("error_message", session.calls[1][2]["params"]["fields"])

    def test_threads_container_unknown_error_is_actionable(self):
        session = _RecordingSession([
            _FakeResponse({"id": "tv-container"}),
            _FakeResponse({"status": "ERROR", "error_message": "UNKNOWN"}),
        ])
        account = SimpleNamespace(config={"threadUserId": "42", "accessToken": "threads-token", "accessTokenExpiresAt": "2099-01-01T00:00:00"})
        with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
            prepared_publishers.publish_threads_sync(
                account,
                {
                    "message": "Threads video",
                    "artifacts": [{"public_url": "https://cdn.example/video.mp4", "artifact_kind": "watermarked_video"}],
                },
                session=session,
            )
        message = str(ctx.exception)
        self.assertIn("no diagnostic detail", message)
        self.assertIn("duration/size limits", message)

    def test_threads_video_over_duration_limit_fails_before_container(self):
        session = _RecordingSession()
        account = SimpleNamespace(config={"threadUserId": "42", "accessToken": "threads-token", "accessTokenExpiresAt": "2099-01-01T00:00:00"})
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "long.mp4"
            video.write_bytes(b"video")
            with patch.object(prepared_publishers.media_pipeline, "probe_video_duration", return_value=813.0):
                with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
                    prepared_publishers.publish_threads_sync(
                        account,
                        {
                            "message": "Threads video",
                            "artifacts": [{
                                "local_path": str(video),
                                "public_url": "https://cdn.example/video.mp4",
                                "artifact_kind": "watermarked_video",
                            }],
                        },
                        session=session,
                    )
        message = str(ctx.exception)
        self.assertIn("813", message)
        self.assertIn("300", message)
        # Fail fast: no container-create or status API call is made.
        self.assertEqual(session.calls, [])

    def test_threads_video_over_size_limit_fails_before_container(self):
        session = _RecordingSession()
        account = SimpleNamespace(config={"threadUserId": "42", "accessToken": "threads-token", "accessTokenExpiresAt": "2099-01-01T00:00:00"})
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "huge.mp4"
            video.write_bytes(b"x")
            os.truncate(video, prepared_publishers.THREADS_MAX_VIDEO_BYTES + 1)
            with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
                prepared_publishers.publish_threads_sync(
                    account,
                    {
                        "message": "Threads video",
                        "artifacts": [{
                            "local_path": str(video),
                            "public_url": "https://cdn.example/video.mp4",
                            "artifact_kind": "watermarked_video",
                        }],
                    },
                    session=session,
                )
        message = str(ctx.exception)
        self.assertIn("MB", message)
        self.assertIn("1000", message)
        self.assertEqual(session.calls, [])

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

    def test_tiktok_invalid_params_is_non_retryable(self):
        response = _FakeResponse(
            {"error": {"code": "invalid_params", "message": "The request post info is empty or incorrect"}},
            status_code=400,
        )
        with self.assertRaises(prepared_publishers.PreparedPublishError) as raised:
            prepared_publishers._raise_tiktok_error(response)
        self.assertFalse(raised.exception.retryable)
        self.assertIn("invalid_params", str(raised.exception))

    def test_tiktok_http_200_business_error_is_rejected(self):
        response = _FakeResponse(
            {"error": {"code": "invalid_params", "message": "bad post"}},
            status_code=200,
        )
        with self.assertRaises(prepared_publishers.PreparedPublishError) as raised:
            prepared_publishers._raise_tiktok_error(response)
        self.assertFalse(raised.exception.retryable)

    def test_ordinary_http_400_is_non_retryable(self):
        response = _FakeResponse({"error": {"message": "invalid input"}}, status_code=400)
        with self.assertRaises(prepared_publishers.PreparedPublishError) as raised:
            prepared_publishers._raise_for_status(response)
        self.assertFalse(raised.exception.retryable)

    def test_tiktok_photo_draft_mode_fails_before_content_init(self):
        account = SimpleNamespace(config={"accessToken": "access", "publishMode": "draft"})
        session = _RecordingSession([])
        with patch.object(prepared_publishers, "_ensure_tiktok_access_token", return_value=("access", None)), \
             patch.object(prepared_publishers, "query_tiktok_creator_info", return_value={"data": {}}):
            with self.assertRaisesRegex(prepared_publishers.PreparedPublishError, "photo posts require Direct Post") as raised:
                prepared_publishers.publish_tiktok_sync(
                    account,
                    {
                        "message": "Photo caption",
                        "artifacts": [{"public_url": "https://cdn.example/image.jpg", "artifact_kind": "watermarked_image"}],
                        "tiktokDirectPost": False,
                    },
                    session=session,
                )
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(session.calls, [])

    def test_tiktok_chunk_plan_floors_the_chunk_count(self):
        # Regression for job 4518: a 602 MB (602,285,541 byte) source declares
        # chunk_size=64 MiB. TikTok expects floor(video_size / chunk_size) = 8
        # chunks; the old ceil() sent 9 and init was rejected with
        # "invalid_params: The total chunk count is invalid".
        file_size = 602_285_541
        chunk_size, total_chunks = prepared_publishers._tiktok_chunk_plan(file_size)
        self.assertEqual(chunk_size, prepared_publishers.TIKTOK_FILE_UPLOAD_MAX_CHUNK_SIZE)
        self.assertEqual(total_chunks, file_size // chunk_size)
        self.assertEqual(total_chunks, 8)
        # Declared chunks must never overshoot the file (that is what floor means).
        self.assertLessEqual(chunk_size * total_chunks, file_size)
        # The trailing remainder rides along in the final chunk, which TikTok
        # accepts up to 128 MB.
        final_chunk = file_size - chunk_size * (total_chunks - 1)
        self.assertGreater(final_chunk, chunk_size)
        self.assertLessEqual(final_chunk, 128 * 1024 * 1024)

    def test_tiktok_chunk_plan_forces_multiple_chunks_over_64mb(self):
        # TikTok: files larger than 64 MB must be uploaded in multiple chunks.
        max_chunk = prepared_publishers.TIKTOK_FILE_UPLOAD_MAX_CHUNK_SIZE
        min_chunk = prepared_publishers.TIKTOK_FILE_UPLOAD_CHUNK_SIZE
        for file_size in (max_chunk + 1, 100_000_000, 128 * 1024 * 1024 - 1, 4_000_000_000):
            with self.subTest(file_size=file_size):
                chunk_size, total_chunks = prepared_publishers._tiktok_chunk_plan(file_size)
                self.assertGreaterEqual(total_chunks, 2)
                self.assertEqual(total_chunks, file_size // chunk_size)
                self.assertGreaterEqual(chunk_size, min_chunk)
                self.assertLessEqual(chunk_size, max_chunk)

    def test_tiktok_chunk_plan_single_chunk_for_small_and_medium_files(self):
        for file_size in (1, 4 * 1024 * 1024, 5 * 1024 * 1024, 47 * 1024 * 1024):
            with self.subTest(file_size=file_size):
                chunk_size, total_chunks = prepared_publishers._tiktok_chunk_plan(file_size)
                self.assertEqual(chunk_size, file_size)
                self.assertEqual(total_chunks, 1)

    def test_tiktok_file_upload_merges_remainder_into_final_chunk(self):
        # The uploader must send ``total_chunks`` requests with the last one
        # carrying every remaining byte, matching the count declared to TikTok.
        session = _RecordingSession([
            _FakeResponse({}, status_code=201),
            _FakeResponse({}, status_code=201),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "clip.mp4"
            video.write_bytes(b"0123456789")  # 10 bytes
            prepared_publishers._tiktok_file_upload(session, video, "https://upload.example/x", 4, 2)
        self.assertEqual(len(session.calls), 2)
        first_headers = session.calls[0][2]["headers"]
        second_headers = session.calls[1][2]["headers"]
        self.assertEqual(session.calls[0][2]["data"], b"0123")
        self.assertEqual(first_headers["Content-Range"], "bytes 0-3/10")
        self.assertEqual(first_headers["Content-Length"], "4")
        self.assertEqual(session.calls[1][2]["data"], b"456789")
        self.assertEqual(second_headers["Content-Range"], "bytes 4-9/10")
        self.assertEqual(second_headers["Content-Length"], "6")

    def test_youtube_image_artifact_is_non_retryable_and_does_not_call_api(self):
        account = SimpleNamespace(config={"channelId": "UC123"})
        session = _RecordingSession([])
        with self.assertRaisesRegex(prepared_publishers.PreparedPublishError, "no video") as raised:
            prepared_publishers.publish_youtube_sync(
                account,
                {"artifacts": [{"public_url": "https://cdn.example/image.jpg", "artifact_kind": "watermarked_image"}]},
                session=session,
            )
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(session.calls, [])

    def test_youtube_refresh_and_resumable_upload(self):
        session = _RecordingSession([
            _FakeResponse({"access_token": "google-token"}),
            _FakeResponse({}, headers={"Location": "https://upload.example/resumable"}),
            _FakeResponse({"id": "video123"}),
            _FakeResponse({"items": [{"status": {"privacyStatus": "public"}}]}),
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
        self.assertEqual(session.calls[3][1], "https://www.googleapis.com/youtube/v3/videos")
        self.assertEqual(session.calls[4][1], prepared_publishers.YOUTUBE_PLAYLIST_INSERT_URL)

    def test_config_value_can_resolve_env_reference(self):
        with patch.dict(os.environ, {"TOKEN_ENV": "abc"}, clear=False):
            value = prepared_publishers._config_value({"botTokenEnv": "TOKEN_ENV"}, "botToken")
        self.assertEqual(value, "abc")

    def test_facebook_publish_returns_updated_config_with_re_derived_page_token(self):
        session = _RecordingSession([
            _FakeResponse({"id": "video-1"}),
        ])
        account = SimpleNamespace(config={
            "pageId": "p1",
            "accessToken": "",
            "metaUserAccessToken": "user-token",
            "metaUserAccessTokenExpiresAt": "2000-01-01T00:00:00",
            "accessTokenExpiresAt": "2000-01-01T00:00:00",
        })
        with patch("myUtils.meta_auth.fetch_managed_pages", return_value={
            "data": [{"id": "p1", "name": "Brand", "access_token": "new-page-token"}]
        }) as fetch_pages, \
             patch("myUtils.meta_auth.exchange_for_long_lived_token", return_value={"access_token": "user-token", "expires_in": 5183944}), \
             patch("myUtils.meta_auth.refresh_instagram_user_token", return_value={"access_token": "user-token", "expires_in": 5183944}), \
             patch.object(prepared_publishers, "_check_meta_token_not_expired", lambda *a, **kw: None):
            result = prepared_publishers.publish_facebook_sync(
                account,
                {
                    "message": "hi",
                    "artifacts": [{"public_url": "https://cdn.example/v.mp4", "artifact_kind": "watermarked_video"}],
                },
                session=session,
            )
        fetch_pages.assert_called_once()
        self.assertIsInstance(result, dict)
        self.assertEqual(result["updated_config"]["metaUserAccessToken"], "user-token")
        self.assertEqual(result["updated_config"]["accessToken"], "new-page-token")
        self.assertEqual(session.calls[0][2]["data"]["access_token"], "new-page-token")

    def test_instagram_publish_returns_updated_config_with_re_derived_page_token(self):
        session = _RecordingSession([
            _FakeResponse({"id": "ig-container"}),
            _FakeResponse({"status_code": "FINISHED"}),
            _FakeResponse({"id": "ig-media"}),
        ])
        account = SimpleNamespace(config={
            "igUserId": "ig-1",
            "pageId": "p1",
            "accessToken": "",
            "metaUserAccessToken": "user-token",
            "metaUserAccessTokenExpiresAt": "2000-01-01T00:00:00",
            "accessTokenExpiresAt": "2000-01-01T00:00:00",
        })
        with patch("myUtils.meta_auth.fetch_managed_pages", return_value={
            "data": [{
                "id": "p2",
                "name": "Brand Page",
                "access_token": "new-page-token",
                "instagram_business_account": {"id": "ig-1", "username": "brand_ig"},
            }]
        }), \
             patch("myUtils.meta_auth.exchange_for_long_lived_token", return_value={"access_token": "user-token", "expires_in": 5183944}), \
             patch("myUtils.meta_auth.refresh_instagram_user_token", return_value={"access_token": "user-token", "expires_in": 5183944}), \
             patch.object(prepared_publishers, "_check_meta_token_not_expired", lambda *a, **kw: None):
            result = prepared_publishers.publish_instagram_sync(
                account,
                {
                    "message": "hi",
                    "artifacts": [{"public_url": "https://cdn.example/i.jpg", "artifact_kind": "watermarked_image"}],
                },
                session=session,
            )
        self.assertIsInstance(result, dict)
        self.assertEqual(result["updated_config"]["accessToken"], "new-page-token")
        self.assertEqual(result["updated_config"]["pageId"], "p2")

    def test_is_recoverable_oauth_error_detects_subcode(self):
        from myUtils.prepared_publishers import _is_recoverable_oauth_error, PreparedPublishError
        ok = PreparedPublishError("HTTP 401: Token expired code=190 error_subcode=463 type=OAuthException")
        self.assertTrue(_is_recoverable_oauth_error(ok))
        bad = PreparedPublishError("HTTP 400: invalid params")
        self.assertFalse(_is_recoverable_oauth_error(bad))
        grant = PreparedPublishError("HTTP 401: invalid_grant Token has been expired or revoked")
        self.assertTrue(_is_recoverable_oauth_error(grant))

    def test_is_youtube_refresh_token_expired(self):
        from myUtils.prepared_publishers import _is_youtube_refresh_token_expired
        self.assertFalse(_is_youtube_refresh_token_expired({}))
        self.assertFalse(_is_youtube_refresh_token_expired({"refreshTokenExpiresAt": ""}))
        self.assertTrue(_is_youtube_refresh_token_expired({"refreshTokenExpiresAt": "2000-01-01T00:00:00"}))
        self.assertFalse(_is_youtube_refresh_token_expired({"refreshTokenExpiresAt": "2099-01-01T00:00:00"}))

    def test_facebook_publish_retries_on_recoverable_401(self):
        class _AuthError(_FakeResponse):
            status_code = 401
            def __init__(self):
                super().__init__({"error": {"code": 190, "error_subcode": 463, "message": "Token expired"}})
            def raise_for_status(self):
                from myUtils.prepared_publishers import PreparedPublishError
                raise PreparedPublishError("HTTP 401: Token expired code=190 error_subcode=463 type=OAuthException")
        # First response is the 401 that triggers the retry; second response is success.
        session = _RecordingSession([
            _AuthError(),
            _FakeResponse({"id": "video-1"}),
        ])
        account = SimpleNamespace(config={
            "pageId": "p1",
            "accessToken": "page-token",
            "metaUserAccessToken": "user-token",
            "metaUserAccessTokenExpiresAt": "2099-01-01T00:00:00",
            "accessTokenExpiresAt": "2099-01-01T00:00:00",
        })
        with patch.object(prepared_publishers, "_maybe_refresh_facebook_token", lambda config, **kw: config), \
             patch("myUtils.meta_auth.fetch_managed_pages", return_value={"data": [{"id": "p1", "name": "P", "access_token": "page-token"}]}), \
             patch.object(prepared_publishers, "_check_meta_token_not_expired", lambda *a, **kw: None), \
             patch.object(prepared_publishers, "_rederive_meta_page_token", side_effect=lambda config, platform, **kw: {**config, "accessToken": "page-token"}):
            result = prepared_publishers.publish_facebook_sync(
                account,
                {
                    "message": "hi",
                    "artifacts": [{"public_url": "https://cdn.example/v.mp4", "artifact_kind": "watermarked_video"}],
                },
                session=session,
            )
        self.assertEqual(result["results"][-1]["id"], "video-1")
        self.assertEqual(len(session.calls), 2)  # 1 failure + 1 success after retry

    def test_facebook_publish_does_not_retry_twice_on_persistent_401(self):
        class _AuthError(_FakeResponse):
            status_code = 401
            def __init__(self):
                super().__init__({"error": {"code": 190, "error_subcode": 463, "message": "Token expired"}})
            def raise_for_status(self):
                from myUtils.prepared_publishers import PreparedPublishError
                raise PreparedPublishError("HTTP 401: Token expired code=190 error_subcode=463 type=OAuthException")
        session = _RecordingSession([
            _AuthError(),
            _AuthError(),
        ])
        account = SimpleNamespace(config={
            "pageId": "p1",
            "accessToken": "page-token",
            "metaUserAccessToken": "user-token",
            "metaUserAccessTokenExpiresAt": "2099-01-01T00:00:00",
            "accessTokenExpiresAt": "2099-01-01T00:00:00",
        })
        with patch.object(prepared_publishers, "_maybe_refresh_facebook_token", lambda config, **kw: config), \
             patch("myUtils.meta_auth.fetch_managed_pages", return_value={"data": [{"id": "p1", "name": "P", "access_token": "page-token"}]}), \
             patch.object(prepared_publishers, "_check_meta_token_not_expired", lambda *a, **kw: None), \
             patch.object(prepared_publishers, "_rederive_meta_page_token", side_effect=lambda config, platform, **kw: {**config, "accessToken": "page-token"}):
            with self.assertRaises(prepared_publishers.PreparedPublishError):
                prepared_publishers.publish_facebook_sync(
                    account,
                    {
                        "message": "hi",
                        "artifacts": [{"public_url": "https://cdn.example/v.mp4", "artifact_kind": "watermarked_video"}],
                    },
                    session=session,
                )
        self.assertEqual(len(session.calls), 2)  # original + 1 retry, then propagated

    def test_facebook_publish_does_not_retry_on_400(self):
        class _BadRequest(_FakeResponse):
            status_code = 400
            def raise_for_status(self):
                from myUtils.prepared_publishers import PreparedPublishError
                raise PreparedPublishError("HTTP 400: bad request")
        session = _RecordingSession([_BadRequest()])
        account = SimpleNamespace(config={
            "pageId": "p1",
            "accessToken": "page-token",
            "metaUserAccessToken": "user-token",
            "metaUserAccessTokenExpiresAt": "2099-01-01T00:00:00",
            "accessTokenExpiresAt": "2099-01-01T00:00:00",
        })
        with self.assertRaises(prepared_publishers.PreparedPublishError):
            prepared_publishers.publish_facebook_sync(
                account,
                {
                    "message": "hi",
                    "artifacts": [{"public_url": "https://cdn.example/v.mp4", "artifact_kind": "watermarked_video"}],
                },
                session=session,
            )
        self.assertEqual(len(session.calls), 1)

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
        self.assertIn('4096 MB', str(ctx.exception))

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

    def test_publish_reddit_video_without_public_url_falls_back_to_self_post(self):
        session = _RecordingSession([
            _FakeResponse({"access_token": "token"}),
            _FakeResponse({"json": {"errors": []}}),
        ])
        account = SimpleNamespace(
            account_name="test",
            config={"clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh", "subreddits": ["test"]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "clip.mp4"
            video.write_bytes(b"video")
            prepared_publishers.publish_reddit_sync(
                account,
                {"message": "Video post", "artifacts": [{"local_path": str(video), "artifact_kind": "video"}]},
                session=session,
            )
        # No URL -> a self post carrying the message, not a refusal.
        data = session.calls[1][2]["data"]
        self.assertEqual(data["kind"], "self")
        self.assertEqual(data["text"], "Video post")
        self.assertNotIn("url", data)

    def test_publish_reddit_video_without_public_url_self_post_keeps_no_link(self):
        session = _RecordingSession([
            _FakeResponse({"access_token": "token"}),
            _FakeResponse({"json": {"errors": []}}),
            _FakeResponse({"json": {"errors": []}}),
        ])
        account = SimpleNamespace(
            account_name="test",
            config={
                "clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh",
                "subreddits": ["NudistMen", "somewhere"],
                "selfPostSubreddits": ["NudistMen"],
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "clip.mp4"
            video.write_bytes(b"video")
            prepared_publishers.publish_reddit_sync(
                account,
                {"message": "Video post", "artifacts": [{"local_path": str(video), "artifact_kind": "video"}]},
                session=session,
            )
        nudist = session.calls[1][2]["data"]
        other = session.calls[2][2]["data"]
        # Both subreddits fall through to a self post when there is no URL; a
        # missing URL never swallows the post.
        self.assertEqual(nudist["kind"], "self")
        self.assertEqual(nudist["text"], "Video post")
        self.assertEqual(other["kind"], "self")
        self.assertEqual(other["text"], "Video post")

    def test_publish_reddit_image_without_local_file_falls_back_to_self_post(self):
        session = _RecordingSession([
            _FakeResponse({"access_token": "token"}),
            _FakeResponse({"json": {"errors": []}}),
        ])
        account = SimpleNamespace(
            account_name="test",
            config={
                "clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh",
                "subreddits": ["test"],
            },
        )
        prepared_publishers.publish_reddit_sync(
            account,
            {
                "message": "Image post",
                "artifacts": [{"public_url": "https://cdn.example/photo.jpg", "artifact_kind": "remote_upload"}],
            },
            session=session,
        )
        data = session.calls[1][2]["data"]
        # No local copy to ingest, so it is a self post with the URL in the
        # body instead of the old hard refusal - and explicitly NOT a link
        # post pointing at our own storage.
        self.assertEqual(data["kind"], "self")
        self.assertIn("https://cdn.example/photo.jpg", data["text"])
        self.assertNotIn("url", data)

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

    def test_account_subreddits_win_over_a_stale_draft(self):
        """A queued draft must not override the account's current subreddits.

        The order was reversed, so a list baked into the payload when the job was
        queued beat the live account config. Job 5445 failed naming six
        subreddits that had been removed from the account hours earlier: the
        payload still carried them and the publisher preferred the payload, so
        correcting the account could not fix an already-queued job.
        """
        session = _RecordingSession([
            _FakeResponse({"access_token": "token"}),
            _FakeResponse({"json": {"errors": []}}),
        ])
        account = SimpleNamespace(
            account_name="test",
            config={"clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh", "subreddits": ["current_sub"]},
        )
        prepared_publishers.publish_reddit_sync(
            account,
            {"message": "test", "draft": {"subreddits": ["removed_sub"]}},
            session=session,
        )
        data = session.calls[1][2]["data"]
        self.assertEqual(data["sr"], "current_sub")

    def test_a_draft_may_narrow_the_account_subreddits(self):
        # A per-post choice is legitimate, as long as it stays within the account.
        session = _RecordingSession([
            _FakeResponse({"access_token": "token"}),
            _FakeResponse({"json": {"errors": []}}),
        ])
        account = SimpleNamespace(
            account_name="test",
            config={"clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh", "subreddits": ["a_sub", "b_sub"]},
        )
        prepared_publishers.publish_reddit_sync(
            account,
            {"message": "test", "draft": {"subreddits": ["b_sub"]}},
            session=session,
        )
        self.assertEqual(session.calls[1][2]["data"]["sr"], "b_sub")

    def test_a_draft_cannot_widen_beyond_the_account(self):
        session = _RecordingSession([
            _FakeResponse({"access_token": "token"}),
            _FakeResponse({"json": {"errors": []}}),
            _FakeResponse({"json": {"errors": []}}),
        ])
        account = SimpleNamespace(
            account_name="test",
            config={"clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh", "subreddits": ["a_sub", "b_sub"]},
        )
        prepared_publishers.publish_reddit_sync(
            account,
            {"message": "test", "draft": {"subreddits": ["b_sub", "foreign_sub"]}},
            session=session,
        )
        self.assertEqual(session.calls[1][2]["data"]["sr"], "b_sub")
        # Only one submit: the foreign subreddit was dropped, not published.
        self.assertEqual(len(session.calls), 2)

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

    # --- post flair (SUBMIT_VALIDATION_FLAIR_REQUIRED) ---

    def test_publish_reddit_sends_the_subreddit_flair_id(self):
        session = _RecordingSession([
            _FakeResponse({"access_token": "token"}),
            _FakeResponse({"json": {"errors": []}}),
        ])
        account = SimpleNamespace(
            account_name="test",
            config={
                "clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh",
                "subreddits": ["NudistMen"],
                "flairIds": {"NudistMen": "flair-guid-1"},
            },
        )
        prepared_publishers.publish_reddit_sync(
            account, {"message": "test"}, session=session,
        )
        data = session.calls[1][2]["data"]
        self.assertEqual(data["sr"], "NudistMen")
        self.assertEqual(data["flair_id"], "flair-guid-1")

    def test_publish_reddit_omits_flair_when_subreddit_is_unmapped(self):
        session = _RecordingSession([
            _FakeResponse({"access_token": "token"}),
            _FakeResponse({"json": {"errors": []}}),
        ])
        account = SimpleNamespace(
            account_name="test",
            config={
                "clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh",
                "subreddits": ["other"],
                "flairIds": {"NudistMen": "flair-guid-1"},
            },
        )
        prepared_publishers.publish_reddit_sync(
            account, {"message": "test"}, session=session,
        )
        data = session.calls[1][2]["data"]
        # Never guess a flair for a community that did not ask for one.
        self.assertNotIn("flair_id", data)

    def test_reddit_flair_id_precedence_and_shapes(self):
        # draft mapping wins over config; r/ prefix and a flat id both resolve.
        self.assertEqual(
            prepared_publishers._reddit_flair_id(
                {"draft": {"flairIds": {"NudistMen": "d1"}}},
                {"flairIds": {"NudistMen": "c1"}, "flairId": "flat"},
                "NudistMen",
            ),
            "d1",
        )
        self.assertEqual(
            prepared_publishers._reddit_flair_id({}, {"flairIds": {"r/other": "p1"}}, "other"),
            "p1",
        )
        self.assertEqual(
            prepared_publishers._reddit_flair_id({}, {"flairId": "flat"}, "anything"),
            "flat",
        )
        self.assertEqual(
            prepared_publishers._reddit_flair_id({}, {}, "anything"), ""
        )

    # --- content-category flairs ---

    def test_content_flair_label_classifies_media_text(self):
        cases = {
            "Naked Outdoor in the woods 12": "In Nature",
            "Bare in Autumn Woods 47": "In Nature",
            "Wandering the beach at sunset": "In Nature",
            "Bare Scholar's Upward Gaze 115": "Selfie",
            "Fox Masked Vulnerability 16": "Selfie",
            "Pensive Nude Reflection 117": "Selfie",
            "Morning coffee on the balcony": "Food & Drink",
            "kitchen dinner prep": "Food & Drink",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(
                    prepared_publishers._reddit_content_flair_label(text), expected
                )

    def test_content_flair_label_falls_back_to_the_neutral_bucket(self):
        # An unrecognised image must not be guessed into a specific category.
        self.assertEqual(
            prepared_publishers._reddit_content_flair_label("Serene Sensual Surrender 18"),
            prepared_publishers.REDDIT_DEFAULT_FLAIR,
        )
        self.assertEqual(
            prepared_publishers._reddit_content_flair_label("random unlabelled thing"),
            prepared_publishers.REDDIT_DEFAULT_FLAIR,
        )

    def test_content_flair_label_is_empty_for_no_text(self):
        # Empty input means "cannot decide" -> no flair, not the default.
        self.assertEqual(prepared_publishers._reddit_content_flair_label(""), "")
        self.assertEqual(prepared_publishers._reddit_content_flair_label(None), "")

    def test_flair_id_picks_the_category_from_the_image(self):
        cfg = {
            "flairIds": {
                "NudistMen": {
                    "In Nature": "nature-id",
                    "Selfie": "selfie-id",
                    "At Home": "home-id",
                    "Food & Drink": "food-id",
                }
            }
        }
        for topic, expected in (
            ("Naked Outdoor in the woods 12", "nature-id"),
            ("Bare Scholar's Upward Gaze 115", "selfie-id"),
            ("morning coffee", "food-id"),
            ("Serene Sensual Surrender 18", "home-id"),
        ):
            with self.subTest(topic=topic):
                self.assertEqual(
                    prepared_publishers._reddit_flair_id(
                        {"draft": {"topic": topic}}, cfg, "NudistMen"
                    ),
                    expected,
                )

    def test_flair_id_category_map_unmapped_subreddit_sends_nothing(self):
        # NudistMen's GUIDs are per-subreddit; sending them to another sub would
        # be an unknown flair, so an unmapped sub must send none.
        cfg = {"flairIds": {"NudistMen": {"In Nature": "nature-id"}}}
        self.assertEqual(
            prepared_publishers._reddit_flair_id(
                {"draft": {"topic": "Naked Outdoor"}}, cfg, "GayBody"
            ),
            "",
        )

    def test_flair_id_single_entry_map_still_publishes(self):
        # A subreddit with one flair configured keeps working without a topic.
        cfg = {"flairIds": {"NudistMen": {"In Nature": "only-id"}}}
        self.assertEqual(
            prepared_publishers._reddit_flair_id({}, cfg, "NudistMen"), "only-id"
        )

    def test_flair_id_category_map_derives_from_artifacts_when_no_topic(self):
        cfg = {"flairIds": {"NudistMen": {"In Nature": "nature-id", "At Home": "home-id"}}}
        payload = {
            "draft": {},
            "artifacts": [{"local_path": "/x/Naked Outdoor in the woods 12.jpg"}],
        }
        self.assertEqual(
            prepared_publishers._reddit_flair_id(payload, cfg, "NudistMen"),
            "nature-id",
        )


class RedditNativeImageTests(unittest.TestCase):
    """An image payload must become a native image post, not a self-hosted link.

    Submitting the storage URL as a link post made every Reddit submission the
    one self-hosted link in a feed of i.redd.it images, which is the promotion
    signal the target subs' rules go after.
    """

    def _account(self):
        return SimpleNamespace(
            account_name="test",
            config={
                "clientId": "cid",
                "clientSecret": "secret",
                "refreshToken": "refresh",
                "subreddits": ["test"],
            },
        )

    def _image(self, directory: str) -> str:
        path = Path(directory) / "shot.jpg"
        path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-bytes")
        return str(path)

    def _lease(self):
        return _FakeResponse(
            {
                "args": {
                    # Reddit hands this back protocol-relative.
                    "action": "//reddit-uploaded-media.s3-accelerate.amazonaws.com",
                    "fields": [
                        {"name": "key", "value": "abc123"},
                        {"name": "policy", "value": "p"},
                    ],
                },
                "asset": {"asset_id": "a1", "websocket_url": "wss://ws.example/x"},
            }
        )

    def test_image_is_uploaded_then_submitted_as_image_kind(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = self._image(directory)
            session = _RecordingSession(
                [
                    _FakeResponse({"access_token": "token"}),
                    self._lease(),
                    _FakeResponse(
                        text=(
                            "<PostResponse><Location>"
                            "https://reddit-uploaded-media.s3-accelerate.amazonaws.com/bucket/abc123"
                            "</Location></PostResponse>"
                        )
                    ),
                    _FakeResponse({"json": {"errors": [], "data": {}}}),
                ]
            )
            prepared_publishers.publish_reddit_sync(
                self._account(),
                {
                    "message": "hello",
                    "artifacts": [
                        {"local_path": image_path, "artifact_kind": "watermarked_image"}
                    ],
                },
                session=session,
            )

        lease = session.calls[1]
        self.assertEqual(lease[1], prepared_publishers.REDDIT_MEDIA_LEASE_URL)
        self.assertEqual(lease[2]["data"]["filepath"], "shot.jpg")
        self.assertEqual(lease[2]["data"]["mimetype"], "image/jpeg")

        upload = session.calls[2]
        # The lease action arrives protocol-relative; it must gain a scheme.
        self.assertEqual(
            upload[1], "https://reddit-uploaded-media.s3-accelerate.amazonaws.com"
        )
        self.assertEqual(upload[2]["data"]["key"], "abc123")

        submit = session.calls[3][2]["data"]
        self.assertEqual(submit["kind"], "image")
        self.assertEqual(
            submit["url"],
            "https://reddit-uploaded-media.s3-accelerate.amazonaws.com/bucket/abc123",
        )
        self.assertNotIn("text", submit)

    def test_url_encoded_location_is_decoded_before_submit(self):
        with tempfile.TemporaryDirectory() as directory:
            session = _RecordingSession(
                [
                    _FakeResponse({"access_token": "token"}),
                    self._lease(),
                    _FakeResponse(
                        text=(
                            "<PostResponse><Location>"
                            "https://s3.example/rte_images/a%2Bb%20c"
                            "</Location></PostResponse>"
                        )
                    ),
                    _FakeResponse({"json": {"errors": [], "data": {}}}),
                ]
            )
            prepared_publishers.publish_reddit_sync(
                self._account(),
                {
                    "message": "hello",
                    "artifacts": [
                        {
                            "local_path": self._image(directory),
                            "artifact_kind": "watermarked_image",
                        }
                    ],
                },
                session=session,
            )
        self.assertEqual(session.calls[3][2]["data"]["url"], "https://s3.example/rte_images/a+b c")

    def test_image_without_a_local_file_falls_back_to_self_post(self):
        session = _RecordingSession([
            _FakeResponse({"access_token": "token"}),
            _FakeResponse({"json": {"errors": []}}),
        ])
        prepared_publishers.publish_reddit_sync(
            self._account(),
            {
                "message": "hello",
                "artifacts": [
                    {
                        "public_url": "https://cdn.example/x.jpg",
                        "artifact_kind": "watermarked_image",
                    }
                ],
            },
            session=session,
        )
        submit = session.calls[1][2]["data"]
        # No native upload possible -> a self post, never a link post to our
        # own storage (which the native path exists to avoid).
        self.assertEqual(submit["kind"], "self")
        self.assertIn("https://cdn.example/x.jpg", submit["text"])
        self.assertNotIn("url", submit)

    def test_missing_upload_location_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            session = _RecordingSession(
                [
                    _FakeResponse({"access_token": "token"}),
                    self._lease(),
                    _FakeResponse(text="<PostResponse></PostResponse>"),
                ]
            )
            with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
                prepared_publishers.publish_reddit_sync(
                    self._account(),
                    {
                        "message": "hello",
                        "artifacts": [
                            {
                                "local_path": self._image(directory),
                                "artifact_kind": "watermarked_image",
                            }
                        ],
                    },
                    session=session,
                )
        self.assertIn("Location", str(ctx.exception))


class NwSwBlogGitPushTests(unittest.TestCase):
    """NW/SW Blog publishes MDX straight into the sexualwill_static GitHub repo."""

    def _nwsw_config(self, **overrides):
        cfg = {
            "repoOwner": "Willywang8216",
            "repoName": "sexualwill_static",
            "branch": "main",
            "persona": "sexualwill",
            "githubToken": "gh-token",
            "locales": "en,zh",
        }
        cfg.update(overrides)
        return cfg

    def test_locale_resolution_falls_back_to_en(self):
        self.assertEqual(prepared_publishers._nw_sw_blog_locales({}), ["en"])
        self.assertEqual(prepared_publishers._nw_sw_blog_locales({"locales": "en,zh"}), ["en", "zh"])
        self.assertEqual(prepared_publishers._nw_sw_blog_locales({"locale": "zh"}), ["zh"])
        # per-publish override wins
        self.assertEqual(
            prepared_publishers._nw_sw_blog_locales({"locales": "en"}, {"draft": {"locales": "zh"}}),
            ["zh"],
        )

    def test_lang_sections_single_and_bilingual(self):
        single = "One English post body."
        self.assertEqual(prepared_publishers._nw_sw_blog_lang_sections(single), [single])
        bilingual = "English full version.\n\n---\n\n中文完整版。"
        sections = prepared_publishers._nw_sw_blog_lang_sections(bilingual)
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0], "English full version.")
        self.assertEqual(sections[1], "中文完整版。")

    def test_frontmatter_includes_persona_and_18plus(self):
        fm = prepared_publishers._nw_sw_blog_frontmatter(
            title='He said "hi"',
            slug="he-said-hi",
            persona="sexualwill",
            description="meta desc",
            category="Health",
            tags=["a", "b"],
        )
        self.assertIn('persona: "sexualwill"', fm)
        self.assertIn('audience: "18+"', fm)
        self.assertIn('slug: "he-said-hi"', fm)
        self.assertIn('translationKey: "he-said-hi"', fm)
        self.assertIn('category: "Health"', fm)
        self.assertIn('title: "He said \\"hi\\""', fm)

    def test_publish_writes_english_file_with_github_api(self):
        session = _RecordingSession([
            _FakeResponse({}, status_code=404),   # GET existing -> not found
            _FakeResponse({"content": {"sha": "abc123"}, "commit": {"sha": "def456"}}),  # PUT
        ])
        account = SimpleNamespace(config=self._nwsw_config(persona="nakedwill", locales="en"))
        results = prepared_publishers.publish_nw_sw_blog_sync(
            account,
            {
                "draft": {"title": "Why Naturism Is Not Exhibitionism", "category": "Advocacy", "tags": ["naturism"]},
                "message": "# Naturism\n\nBody of the EN post.",
            },
            session=session,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["locale"], "en")
        self.assertEqual(results[0]["path"], "content/posts/why-naturism-is-not-exhibitionism.mdx")
        # GET then PUT to the same contents URL
        get_call = session.calls[0]
        put_call = session.calls[1]
        self.assertEqual(get_call[0], "GET")
        self.assertEqual(put_call[0], "PUT")
        expected_url = "https://api.github.com/repos/Willywang8216/sexualwill_static/contents/content/posts/why-naturism-is-not-exhibitionism.mdx"
        self.assertEqual(get_call[1], expected_url)
        self.assertEqual(put_call[1], expected_url)
        body = put_call[2]["json"]
        self.assertEqual(body["branch"], "main")
        self.assertNotIn("sha", body)  # 404 GET -> no existing sha
        decoded = base64.b64decode(body["content"]).decode("utf-8")
        self.assertIn('persona: "nakedwill"', decoded)
        self.assertIn("Body of the EN post.", decoded)

    def test_publish_writes_bilingual_en_and_zh_files(self):
        session = _RecordingSession([
            _FakeResponse({}, status_code=404),
            _FakeResponse({"content": {"sha": "e1"}}),
            _FakeResponse({}, status_code=404),
            _FakeResponse({"content": {"sha": "z1"}}),
        ])
        account = SimpleNamespace(config=self._nwsw_config())
        results = prepared_publishers.publish_nw_sw_blog_sync(
            account,
            {
                "draft": {"title": "English ｜ 中文", "locales": "en,zh"},
                "message": "# English\n\nEN body.\n\n---\n\n# 中文\n\n中文內文。",
            },
            session=session,
        )
        self.assertEqual(len(results), 2)
        self.assertEqual([r["locale"] for r in results], ["en", "zh"])
        self.assertEqual(results[0]["path"], "content/posts/english.mdx")
        self.assertEqual(results[1]["path"], "content/posts/zh/english.mdx")
        # zh PUT body carries the zh section
        zh_put = session.calls[3][2]["json"]
        zh_decoded = base64.b64decode(zh_put["content"]).decode("utf-8")
        self.assertIn("# 中文", zh_decoded)
        self.assertIn("中文內文。", zh_decoded)
        self.assertIn('persona: "sexualwill"', zh_decoded)

    def test_publish_updates_existing_file_when_present(self):
        session = _RecordingSession([
            _FakeResponse({"sha": "old-sha", "content": ""}),  # GET 200 -> existing
            _FakeResponse({"content": {"sha": "new-sha"}}),
        ])
        account = SimpleNamespace(config=self._nwsw_config(locales="en"))
        prepared_publishers.publish_nw_sw_blog_sync(
            account,
            {"draft": {"title": "Existing Post"}, "message": "# Existing\n\nBody"},
            session=session,
        )
        put_body = session.calls[1][2]["json"]
        self.assertEqual(put_body["sha"], "old-sha")

    def test_validate_checks_repo_access(self):
        session = _RecordingSession([_FakeResponse({"full_name": "Willywang8216/sexualwill_static", "private": True, "default_branch": "main"})])
        result = prepared_publishers.validate_nw_sw_blog_config_live(
            {"repoOwner": "Willywang8216", "repoName": "sexualwill_static", "persona": "sexualwill", "githubToken": "gh"},
            session=session,
        )
        self.assertTrue(result["private"])
class TelegramMtprotoTests(unittest.TestCase):
    """Routing + behaviour for publishing Telegram as the user account (MTProto)."""

    def test_mtproto_detected_from_flag(self):
        self.assertTrue(prepared_publishers._telegram_is_mtproto({"mtproto": True}))
        self.assertTrue(prepared_publishers._telegram_is_mtproto({"authMode": "user"}))
        self.assertTrue(prepared_publishers._telegram_is_mtproto({"apiId": "1", "apiHash": "h"}))

    def test_bot_detected_when_only_bot_token(self):
        self.assertFalse(prepared_publishers._telegram_is_mtproto({"botToken": "t"}))
        self.assertFalse(prepared_publishers._telegram_is_mtproto({}))

    def test_publish_routes_to_mtproto_and_requires_telethon(self):
        account = SimpleNamespace(config={"mtproto": True, "apiId": "26141001", "apiHash": "hash", "chatIds": ["@a"]})
        if prepared_publishers.TelegramClient is None:
            with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
                prepared_publishers.publish_telegram_sync(account, {"message": "hi"})
            self.assertIn("telethon", str(ctx.exception).lower())
        else:
            # telethon present: full flow needs a real session string
            with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
                prepared_publishers.publish_telegram_sync(account, {"message": "hi"})
            self.assertIn("sessionString", str(ctx.exception))

    def test_validate_routes_to_mtproto_and_requires_session(self):
        config = {"mtproto": True, "apiId": "26141001", "apiHash": "hash", "chatIds": ["@a"]}
        if prepared_publishers.TelegramClient is None:
            with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
                prepared_publishers.validate_telegram_config_live(config)
            self.assertIn("telethon", str(ctx.exception).lower())
        else:
            with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
                prepared_publishers.validate_telegram_config_live(config)
            self.assertIn("sessionString", str(ctx.exception))

    def test_session_string_from_direct_config(self):
        with patch.dict("os.environ", {}, clear=False):
            value = prepared_publishers._telegram_mtproto_session_string({"sessionString": "abc=="})
        self.assertEqual(value, "abc==")

    def test_session_string_from_env(self):
        with patch.dict("os.environ", {"SAU_TG_SESSION": "env-session"}, clear=False):
            value = prepared_publishers._telegram_mtproto_session_string({"sessionStringEnv": "SAU_TG_SESSION"})
        self.assertEqual(value, "env-session")

    def test_session_string_missing_raises(self):
        with self.assertRaises(prepared_publishers.PreparedPublishError):
            prepared_publishers._telegram_mtproto_session_string({"apiId": "1"})
class BlueskyPublisherTests(unittest.TestCase):
    """Bluesky (AT Protocol) publisher: session -> blob upload -> createRecord."""

    def _session(self):
        session = _RecordingSession([
            _FakeResponse({"accessJwt": "jwt1", "did": "did:plc:abc", "handle": "sexualwill.bsky.social"}),
            _FakeResponse({"blob": {"$type": "blob", "ref": {"$link": "baf1"}, "mimeType": "image/jpeg", "size": 3}}),
            _FakeResponse({"uri": "at://did:plc:abc/app.bsky.feed.post/1", "cid": "cid1"}),
        ])
        return session

    def test_publish_text_with_self_label(self):
        session = self._session()
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "cover.jpg"
            img.write_bytes(b"jpg")
            account = SimpleNamespace(config={
                "handle": "sexualwill.bsky.social",
                "appPassword": "app-pass",
                "label": "sexual",
            })
            results = prepared_publishers.publish_bluesky_sync(
                account,
                {
                    "draft": {"message": "Body of the post", "hashtags": ["#art"]},
                    "message": "Body of the post",
                    "artifacts": [{"local_path": str(img), "artifact_kind": "watermarked_image"}],
                },
                session=session,
            )
        # createSession, uploadBlob, createRecord
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(session.calls[0][0], "POST")
        self.assertIn("com.atproto.server.createSession", session.calls[0][1])
        create_call = session.calls[2]
        self.assertIn("com.atproto.repo.createRecord", create_call[1])
        body = create_call[2]["json"]
        self.assertEqual(body["repo"], "did:plc:abc")
        self.assertEqual(body["collection"], "app.bsky.feed.post")
        record = body["record"]
        self.assertEqual(record["labels"]["$type"], "com.atproto.label.defs#selfLabels")
        self.assertEqual(record["labels"]["values"][0]["val"], "sexual")
        self.assertEqual(results[0]["handle"], "sexualwill.bsky.social")

    def test_publish_fails_closed_for_missing_declared_media(self):
        account = SimpleNamespace(config={"handle": "h", "appPassword": "p"})
        with self.assertRaisesRegex(prepared_publishers.PreparedPublishError, "no readable local file or public URL"):
            prepared_publishers.publish_bluesky_sync(
                account,
                {"message": "text", "artifacts": [{
                    "local_path": "/tmp/missing-bs.png",
                    "artifact_kind": "watermarked_image",
                }]},
                session=_RecordingSession(),
            )

    def test_publish_requires_message(self):
        account = SimpleNamespace(config={"handle": "h", "appPassword": "p"})
        with self.assertRaises(prepared_publishers.PreparedPublishError):
            prepared_publishers.publish_bluesky_sync(account, {}, session=_RecordingSession())

    def test_config_requires_handle_and_password(self):
        with self.assertRaises(prepared_publishers.PreparedPublishError):
            prepared_publishers._bluesky_config({})
        with self.assertRaises(prepared_publishers.PreparedPublishError):
            prepared_publishers._bluesky_config({"handle": "h"})
        with self.assertRaises(prepared_publishers.PreparedPublishError):
            prepared_publishers._bluesky_config({"handle": "h", "appPassword": "p", "label": "bad-label"})

    def test_message_truncated_to_300(self):
        draft = {"message": "x" * 500}
        msg, images, videos = prepared_publishers._bluesky_message_and_media({"draft": draft})
        self.assertLessEqual(len(msg), 300)

    # ------------------------------------------------------------------
    # Oversized media is downscaled instead of failing with a 413
    # ------------------------------------------------------------------

    def test_oversized_image_is_downscaled_under_the_blob_limit(self):
        import io
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "big.png"
            # Noise defeats PNG compression, so this is comfortably over the
            # bluesky blob ceiling.
            import random
            random.seed(0)
            image = Image.new("RGB", (2400, 2400))
            image.putdata([
                (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
                for _ in range(2400 * 2400)
            ])
            image.save(src, format="PNG")
            self.assertGreater(src.stat().st_size, prepared_publishers.BLUESKY_MAX_IMAGE_BYTES)

            shrunk = prepared_publishers._bluesky_shrink_image(str(src))
            self.assertNotEqual(shrunk, str(src))
            self.assertLessEqual(
                Path(shrunk).stat().st_size,
                prepared_publishers.BLUESKY_MAX_IMAGE_BYTES,
            )
            Path(shrunk).unlink(missing_ok=True)

    def test_small_image_is_returned_untouched(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "small.jpg"
            Image.new("RGB", (64, 64), (10, 20, 30)).save(src, format="JPEG")
            before = src.stat().st_size
            self.assertEqual(
                prepared_publishers._bluesky_shrink_image(str(src)), str(src)
            )
            self.assertEqual(src.stat().st_size, before)

    def test_shrink_failure_never_raises(self):
        # A missing file must fall through to the caller, not explode - the
        # service's own error is a better signal than a local crash.
        self.assertEqual(
            prepared_publishers._bluesky_shrink_image("/tmp/does-not-exist.png"),
            "/tmp/does-not-exist.png",
        )
        self.assertEqual(
            prepared_publishers._bluesky_shrink_video("/tmp/does-not-exist.mp4"),
            "/tmp/does-not-exist.mp4",
        )

    def test_publish_video_uses_embed_video(self):
        session = _RecordingSession([
            _FakeResponse({"accessJwt": "jwt1", "did": "did:plc:abc", "handle": "sexualwill.bsky.social"}),
            _FakeResponse({"blob": {"$type": "blob", "ref": {"$link": "bafv1"}, "mimeType": "video/mp4", "size": 3}}),
            _FakeResponse({"uri": "at://did:plc:abc/app.bsky.feed.post/v1", "cid": "cidv"}),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            vid = Path(tmp) / "clip.mp4"
            vid.write_bytes(b"mp4")
            account = SimpleNamespace(config={
                "handle": "sexualwill.bsky.social",
                "appPassword": "app-pass",
                "label": "sexual",
            })
            results = prepared_publishers.publish_bluesky_sync(
                account,
                {
                    "draft": {"message": "Watch this", "alt_text": "video alt"},
                    "message": "Watch this",
                    "artifacts": [{"local_path": str(vid), "artifact_kind": "watermarked_video"}],
                },
                session=session,
            )
        # createSession, uploadBlob(video/mp4), createRecord
        self.assertEqual(len(session.calls), 3)
        blob_call = session.calls[1]
        self.assertEqual(blob_call[2]["headers"]["Content-Type"], "video/mp4")
        create_call = session.calls[2]
        record = create_call[2]["json"]["record"]
        self.assertEqual(record["embed"]["$type"], "app.bsky.embed.video")
        self.assertEqual(record["embed"]["video"]["ref"]["$link"], "bafv1")
        self.assertEqual(results[0]["videos"], 1)

    def test_publish_video_over_duration_limit_reports_the_real_overage(self):
        session = _RecordingSession([
            _FakeResponse({"accessJwt": "jwt1", "did": "did:plc:abc", "handle": "h"}),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            vid = Path(tmp) / "clip.mp4"
            vid.write_bytes(b"mp4")
            account = SimpleNamespace(config={"handle": "h", "appPassword": "p"})
            with patch.object(
                prepared_publishers.media_pipeline,
                "probe_video_duration",
                return_value=601.2,
            ):
                with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
                    prepared_publishers.publish_bluesky_sync(
                        account,
                        {"message": "long clip", "artifacts": [{"local_path": str(vid), "artifact_kind": "watermarked_video"}]},
                        session=session,
                    )
        message = str(ctx.exception)
        # The real duration and the size of the overrun, not a rounded "600s".
        self.assertIn("601.2s", message)
        self.assertIn("600s limit", message)
        self.assertIn("by 1.2s", message)
        self.assertIn("re-encode or split", message)

    def test_publish_video_of_exactly_the_limit_is_accepted(self):
        # The guard uses ``>``, and Bluesky allows a video of exactly 600 s.
        session = _RecordingSession([
            _FakeResponse({"accessJwt": "jwt1", "did": "did:plc:abc", "handle": "h"}),
            _FakeResponse({"blob": {"$type": "blob", "ref": {"$link": "bafv1"}, "mimeType": "video/mp4", "size": 3}}),
            _FakeResponse({"uri": "at://did:plc:abc/app.bsky.feed.post/v1", "cid": "cidv"}),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            vid = Path(tmp) / "clip.mp4"
            vid.write_bytes(b"mp4")
            account = SimpleNamespace(config={"handle": "h", "appPassword": "p"})
            with patch.object(
                prepared_publishers.media_pipeline,
                "probe_video_duration",
                return_value=600.0,
            ):
                results = prepared_publishers.publish_bluesky_sync(
                    account,
                    {"message": "exactly ten minutes", "artifacts": [{"local_path": str(vid), "artifact_kind": "watermarked_video"}]},
                    session=session,
                )
        # createSession, uploadBlob(video/mp4), createRecord
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(results[0]["videos"], 1)

    def test_validate_creates_session(self):
        session = _RecordingSession([
            _FakeResponse({"accessJwt": "j", "did": "did:plc:x", "handle": "nw.bsky.social"}),
        ])
        result = prepared_publishers.validate_bluesky_config_live(
            {"handle": "nw.bsky.social", "appPassword": "p", "label": "nudity"},
            session=session,
        )
        self.assertEqual(result["did"], "did:plc:x")
        self.assertEqual(result["label"], "nudity")


if __name__ == "__main__":
    unittest.main()


class RedditSelfPostForWhitelistedSubsTests(unittest.TestCase):
    """Link-whitelisted subreddits must get a self post, not a foreign link.

    r/NudistMen rejects a link post whose domain is not on its allow-list with
    SUBMIT_VALIDATION_LINK_WHITELIST. The same video is accepted when submitted
    as a self post carrying the URL in the body, so such subreddits are marked
    self-post in config.
    """

    def _session(self):
        return _RecordingSession([
            _FakeResponse({"access_token": "token"}),
            _FakeResponse({"json": {"errors": []}}),
        ])

    def test_whitelisted_subreddit_posts_self_with_url_in_body(self):
        session = self._session()
        account = SimpleNamespace(
            account_name="test",
            config={
                "clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh",
                "subreddits": ["NudistMen"],
                "selfPostSubreddits": ["NudistMen"],
            },
        )
        prepared_publishers.publish_reddit_sync(
            account,
            {
                "message": "body",
                "artifacts": [{
                    "public_url": "https://cdn.example/video.mp4",
                    "artifact_kind": "remote_upload",
                }],
            },
            session=session,
        )
        data = session.calls[1][2]["data"]
        self.assertEqual(data["kind"], "self")
        self.assertNotIn("url", data)
        self.assertIn("https://cdn.example/video.mp4", data["text"])

    def test_unmarked_subreddit_still_posts_a_link(self):
        session = self._session()
        account = SimpleNamespace(
            account_name="test",
            config={
                "clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh",
                "subreddits": ["somewhere"],
            },
        )
        prepared_publishers.publish_reddit_sync(
            account,
            {
                "message": "body",
                "artifacts": [{
                    "public_url": "https://cdn.example/video.mp4",
                    "artifact_kind": "remote_upload",
                }],
            },
            session=session,
        )
        data = session.calls[1][2]["data"]
        self.assertEqual(data["kind"], "link")
        self.assertEqual(data["url"], "https://cdn.example/video.mp4")

    def test_self_post_flag_applies_to_every_subreddit(self):
        self.assertTrue(
            prepared_publishers._reddit_prefers_self_post({}, {"selfPost": True}, "any")
        )
        self.assertFalse(
            prepared_publishers._reddit_prefers_self_post({}, {}, "any")
        )
        # A draft list wins over config and tolerates the r/ prefix / case.
        self.assertTrue(
            prepared_publishers._reddit_prefers_self_post(
                {"draft": {"selfPostSubreddits": ["r/nudistmen"]}},
                {},
                "NudistMen",
            )
        )


class RedditLinkArtifactSelectionTests(unittest.TestCase):
    """The served public URL must survive per-platform video-artifact selection.

    A watermarked video is stored twice: a ``watermarked_video`` row and a
    ``local`` row that carries the served ``/getFile`` URL, both pointing at the
    same file. The publisher can only link-post when the selected artifact has
    the URL, so selection must prefer the URL-carrying sibling.
    """

    def _artifacts(self):
        shared = "/tmp/generated/campaign/video_pub.mp4"
        return [
            {
                "id": 1,
                "source_file_record_id": 7,
                "artifact_kind": "watermarked_video",
                "local_path": shared,
                "public_url": None,
                "metadata": {"role": "video"},
            },
            {
                "id": 2,
                "source_file_record_id": 7,
                "artifact_kind": "local",
                "local_path": shared,
                "public_url": "https://cdn.example/video.mp4",
                "metadata": {"role": "video"},
            },
        ]

    def test_select_videos_prefers_the_url_carrying_full_artifact(self):
        import sau_backend

        selected = sau_backend._select_videos_for_platform(self._artifacts(), "reddit")
        self.assertEqual([a["id"] for a in selected], [2])

    def test_reddit_payload_keeps_the_served_url(self):
        import sau_backend

        selected = sau_backend._artifact_payloads_for_platform(self._artifacts(), "reddit")
        self.assertEqual([a["id"] for a in selected], [2])
        self.assertEqual(selected[0]["public_url"], "https://cdn.example/video.mp4")

    def test_reddit_link_post_uses_the_selected_url(self):
        import sau_backend

        selected = sau_backend._artifact_payloads_for_platform(self._artifacts(), "reddit")
        session = _RecordingSession([
            _FakeResponse({"access_token": "token"}),
            _FakeResponse({"json": {"errors": []}}),
        ])
        account = SimpleNamespace(
            account_name="test",
            config={
                "clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh",
                # A synthetic subreddit: r/GayBody is marked banned in the
                # registry, so it would be refused before the link logic ran.
                "subreddits": ["test"],
            },
        )
        prepared_publishers.publish_reddit_sync(
            account,
            {"message": "body", "artifacts": selected},
            session=session,
        )
        data = session.calls[1][2]["data"]
        self.assertEqual(data["kind"], "link")
        self.assertEqual(data["url"], "https://cdn.example/video.mp4")


class UntaggedSplitFallbackTests(unittest.TestCase):
    """A platform can use another platform's split when it is over the cap too.

    Campaign 2517's source was ~600.003 s and split into 5x120 s parts tagged
    ``split_for: ["twitter"]``. Bluesky was not tagged, so it was handed the
    full 600.003 s artifact and the publisher's 600 s guard rejected it. The
    split is usable by Bluesky because the source exceeds Bluesky's cap and the
    120 s parts fit; a large-cap platform that fits the source must still get
    the full video, not the finer split.
    """

    def _part(self, artifact_id: int, index: int, count: int, part_seconds: float, split_for):
        return {
            "id": artifact_id,
            "source_file_record_id": 7,
            "artifact_kind": "local",
            "local_path": f"/tmp/part{artifact_id}.mp4",
            "public_url": None,
            "metadata": {
                "role": "video",
                "max_duration_seconds": part_seconds,
                "part_index": index,
                "part_count": count,
                "split_for": split_for,
            },
        }

    def _full(self, artifact_id: int = 1):
        return {
            "id": artifact_id,
            "source_file_record_id": 7,
            "artifact_kind": "watermarked_video",
            "local_path": "/tmp/full.mp4",
            "public_url": None,
            "metadata": {"role": "video"},
        }

    def test_untagged_parts_used_when_source_is_over_the_platform_cap(self):
        import sau_backend

        artifacts = [self._full(), *[self._part(10 + i, i, 5, 120.0006, ["twitter"]) for i in range(1, 6)]]
        # Bluesky cap 600 s, inferred source 120.0006 * 5 = 600.003 s.
        selected = sau_backend._select_videos_for_platform(artifacts, "bluesky")
        self.assertEqual([a["id"] for a in selected], [11, 12, 13, 14, 15])
        # And the grouping makes one post per part.
        groups = sau_backend._artifact_part_groups_for_platform(artifacts, "bluesky")
        self.assertEqual(len(groups), 5)

    def test_large_cap_platform_still_gets_the_full_video(self):
        import sau_backend

        artifacts = [self._full(), *[self._part(10 + i, i, 5, 120.0006, ["twitter"]) for i in range(1, 6)]]
        # YouTube cap 43200 s: the ~600 s source fits, so keep the full video.
        selected = sau_backend._select_videos_for_platform(artifacts, "youtube")
        self.assertEqual([a["id"] for a in selected], [1])

    def test_untagged_parts_not_used_when_source_fits_the_platform(self):
        import sau_backend

        artifacts = [self._full(), *[self._part(20 + i, i, 3, 100.0, ["threads"]) for i in range(1, 4)]]
        # Instagram cap 900 s: the ~300 s source fits, so keep the full video.
        selected = sau_backend._select_videos_for_platform(artifacts, "instagram")
        self.assertEqual([a["id"] for a in selected], [1])


class BlueskyDurationToleranceTests(unittest.TestCase):
    """A clip authored as exactly the cap must not be rejected.

    Container muxing adds sub-millisecond noise, so a 600 s clip probes as
    600.0000004. A bare ``>`` rejected it with the absurd message "600.0s exceeds
    the 600s limit by 0.0s". The guard now allows a 0.5 s tolerance - far below
    any real platform tolerance, far above the noise.
    """

    def test_tolerance_boundary(self):
        from myUtils import platform_limits as pl

        cap = pl.video_max_seconds("bluesky")
        self.assertEqual(cap, 600.0)
        # Accepted: at, marginally over, and within tolerance.
        for value in (599.9, 600.0, 600.0000004, 600.4, 600.5):
            with self.subTest(seconds=value):
                self.assertFalse(value > cap + 0.5)
        # Rejected: genuinely over.
        for value in (600.6, 601.2, 900.0):
            with self.subTest(seconds=value):
                self.assertTrue(value > cap + 0.5)

    def test_guard_message_reports_the_real_overage(self):
        import inspect

        from myUtils import prepared_publishers

        source = inspect.getsource(prepared_publishers.publish_bluesky_sync)
        self.assertIn("limit by {over_by:.1f}s", source)
        # And the comparison must carry the tolerance.
        self.assertIn("BLUESKY_MAX_VIDEO_SECONDS + 0.5", source)


class RedditNoSelfPostsTests(unittest.TestCase):
    """A subreddit that bans text posts must fail with a clear reason.

    r/gaybrosgonemild answers a self post with NO_SELFS ("This community doesn't
    allow text posts"). Previously the publisher emitted the self post anyway and
    surfaced Reddit's raw error; now it names the subreddit and the remedy.
    """

    def test_bans_self_posts_matches_with_and_without_prefix(self):
        for key in ("gaybrosgonemild", "r/gaybrosgonemild", "GayBrosGoneMild"):
            with self.subTest(key=key):
                self.assertTrue(
                    prepared_publishers._reddit_bans_self_posts(
                        {}, {"noSelfPostSubreddits": [key]}, "gaybrosgonemild"
                    )
                )
        self.assertFalse(
            prepared_publishers._reddit_bans_self_posts({}, {}, "anything")
        )

    def test_self_post_to_a_no_selfs_sub_raises_with_the_subreddit_named(self):
        # Use a synthetic subreddit so ONLY the no-self-posts rule is exercised.
        # r/gaybrosgonemild trips the monetised-brand rule first (it bans both),
        # which is correct but does not isolate the behaviour under test.
        account = SimpleNamespace(
            account_name="test",
            config={
                "clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh",
                "subreddits": ["test"],
                "noSelfPostSubreddits": ["test"],
            },
        )
        with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
            prepared_publishers.publish_reddit_sync(
                account,
                {"message": "body", "artifacts": [
                    # A video artifact with a local file but no public_url: the
                    # exact shape that fell through to a self post.
                    {"artifact_kind": "remote_upload",
                     "local_path": "/tmp/does-not-matter.mp4", "public_url": ""},
                ]},
                session=_RecordingSession([
                    _FakeResponse({"access_token": "token"}),
                ]),
            )
        self.assertIn("test", str(ctx.exception))
        self.assertIn("self posts", str(ctx.exception))


class SubredditStrictGuardTests(unittest.TestCase):
    """A subreddit the platform has banned us from must not be attempted.

    r/GayBros and r/GayBody answered every submit with
    SUBREDDIT_NOTALLOWED_BANNED. Each attempt spent a retry and risked a further
    strike, so the publisher now refuses such a subreddit locally, before any
    network call. SAU_SUBREDDIT_STRICT=0 relaxes only the *unknown-name* check.
    """

    def test_known_banned_subreddit_is_refused_locally(self):
        account = SimpleNamespace(
            account_name="test",
            config={
                "clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh",
                "subreddits": ["GayBody"],
            },
        )
        session = _RecordingSession([_FakeResponse({"access_token": "token"})])
        with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
            prepared_publishers.publish_reddit_sync(
                account,
                {"message": "body", "artifacts": [
                    {"artifact_kind": "remote_upload",
                     "local_path": "/tmp/x.mp4", "public_url": "https://cdn/x.mp4"},
                ]},
                session=session,
            )
        self.assertIn("banned", str(ctx.exception).lower())
        # Crucially: no submit was attempted.
        self.assertEqual(len(session.calls), 1)  # only the token exchange

    def test_unknown_subreddit_refused_when_strict(self):
        account = SimpleNamespace(
            account_name="test",
            config={
                "clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh",
                "subreddits": ["aSubNobodyChecked"],
            },
        )
        with patch.dict(os.environ, {"SAU_SUBREDDIT_STRICT": "1"}):
            with self.assertRaises(prepared_publishers.PreparedPublishError) as ctx:
                prepared_publishers.publish_reddit_sync(
                    account,
                    {"message": "body", "artifacts": [
                        {"artifact_kind": "remote_upload",
                         "local_path": "/tmp/x.mp4", "public_url": "https://cdn/x.mp4"},
                    ]},
                    session=_RecordingSession([_FakeResponse({"access_token": "token"})]),
                )
        self.assertIn("no verified requirement profile", str(ctx.exception))

    def test_unknown_subreddit_allowed_when_not_strict(self):
        # The escape hatch must let an unverified name through to the submit.
        account = SimpleNamespace(
            account_name="test",
            config={
                "clientId": "cid", "clientSecret": "secret", "refreshToken": "refresh",
                "subreddits": ["aSubNobodyChecked"],
            },
        )
        session = _RecordingSession([
            _FakeResponse({"access_token": "token"}),
            _FakeResponse({"json": {"errors": []}}),
        ])
        with patch.dict(os.environ, {"SAU_SUBREDDIT_STRICT": "0"}):
            prepared_publishers.publish_reddit_sync(
                account,
                {"message": "body", "artifacts": [
                    {"artifact_kind": "remote_upload",
                     "local_path": "/tmp/x.mp4", "public_url": "https://cdn/x.mp4"},
                ]},
                session=session,
            )
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[1][2]["data"]["sr"], "aSubNobodyChecked")
