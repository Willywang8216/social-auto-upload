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
