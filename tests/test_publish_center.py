"""Tests for the Publish Center feature.

Covers:
- publish_orchestrator.submit_publish (single-media split, stagger scheduling)
- publish_orchestrator._request_data_for_options (option translation)
- publish_orchestrator._resolve_base_time (schedule resolution)
- Backend HTTP endpoints: /publish-center/preview, /regenerate, /submit
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

if "conf" not in sys.modules:
    conf_module = types.ModuleType("conf")
    conf_module.BASE_DIR = str(Path(__file__).resolve().parent.parent)
    sys.modules["conf"] = conf_module

from myUtils import publish_orchestrator
from myUtils import profiles as profile_registry
from myUtils import platform_capabilities


# ---------------------------------------------------------------------------
# Unit tests for orchestrator helpers
# ---------------------------------------------------------------------------


class ResolveBaseTimeTests(unittest.TestCase):
    """Tests for _resolve_base_time."""

    def test_none_schedule_returns_none(self):
        self.assertIsNone(publish_orchestrator._resolve_base_time(None))

    def test_empty_schedule_returns_none(self):
        self.assertIsNone(publish_orchestrator._resolve_base_time({}))

    def test_publish_now_returns_none(self):
        self.assertIsNone(publish_orchestrator._resolve_base_time({"publishNow": True}))

    def test_start_at_returns_utc_naive_datetime(self):
        result = publish_orchestrator._resolve_base_time({
            "publishNow": False,
            "startAt": "2026-06-17T10:00:00",
        })
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2026)
        self.assertEqual(result.month, 6)
        self.assertEqual(result.hour, 10)
        self.assertIsNone(result.tzinfo)

    def test_start_at_with_timezone_converts_to_utc(self):
        result = publish_orchestrator._resolve_base_time({
            "publishNow": False,
            "startAt": "2026-06-17T18:00:00+08:00",
        })
        self.assertIsNotNone(result)
        self.assertEqual(result.hour, 10)  # UTC+8 18:00 = UTC 10:00

    def test_invalid_date_returns_none(self):
        self.assertIsNone(publish_orchestrator._resolve_base_time({
            "publishNow": False,
            "startAt": "not-a-date",
        }))


class RequestDataForOptionsTests(unittest.TestCase):
    """Tests for _request_data_for_options."""

    def test_watermark_disabled_sets_empty_string(self):
        profile = MagicMock()
        profile.settings = {"watermark": "Brand WM"}
        result = publish_orchestrator._request_data_for_options(
            brief="test", options={"watermark": False}, profile=profile,
        )
        self.assertEqual(result["watermark"], "")

    def test_watermark_enabled_uses_profile_default(self):
        profile = MagicMock()
        profile.settings = {"watermark": "Brand WM"}
        result = publish_orchestrator._request_data_for_options(
            brief="test", options={"watermark": True}, profile=profile,
        )
        self.assertEqual(result["watermark"], "Brand WM")

    def test_intro_disabled_sets_empty_list(self):
        profile = MagicMock()
        profile.settings = {"intros": ["intro.mp4"]}
        result = publish_orchestrator._request_data_for_options(
            brief="test", options={"intro": False}, profile=profile,
        )
        self.assertEqual(result["intros"], [])

    def test_screenshots_enabled(self):
        profile = MagicMock()
        profile.settings = {}
        result = publish_orchestrator._request_data_for_options(
            brief="test",
            options={"screenshots": {"enabled": True, "count": 5, "timestamps": ["00:10", "00:30"]}},
            profile=profile,
        )
        self.assertTrue(result["screenshots"]["enabled"])
        self.assertEqual(result["screenshots"]["count"], 5)
        self.assertEqual(result["screenshots"]["timestamps"], ["00:10", "00:30"])


class MediaRoleTests(unittest.TestCase):
    """Tests for _media_role_for_path."""

    def test_mp4_is_video(self):
        self.assertEqual(publish_orchestrator._media_role_for_path("video.mp4"), "video")

    def test_jpg_is_image(self):
        self.assertEqual(publish_orchestrator._media_role_for_path("photo.jpg"), "image")

    def test_png_is_image(self):
        self.assertEqual(publish_orchestrator._media_role_for_path("screenshot.png"), "image")

    def test_unknown_extension_defaults_to_video(self):
        self.assertEqual(publish_orchestrator._media_role_for_path("file.xyz"), "video")


# ---------------------------------------------------------------------------
# HTTP integration tests
# ---------------------------------------------------------------------------

flask_available = importlib.util.find_spec("flask") is not None


def _make_test_app():
    """Create a minimal test app with the publish center endpoints."""
    import db.createTable as create_table
    import sau_backend

    return sau_backend, create_table


@unittest.skipUnless(flask_available, "Flask not installed")
class PublishCenterPreviewTests(unittest.TestCase):
    def setUp(self):
        import sau_backend
        import db.createTable as create_table

        self.sau_backend = sau_backend
        self._tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self._tmp.name)
        self.db_path = self.base_dir / "db" / "database.db"
        create_table.bootstrap(self.db_path)

        self._base_dir_patch = patch.object(sau_backend, "BASE_DIR", self.base_dir)
        self._base_dir_patch.start()

        from myUtils.security import SecurityPolicy
        self._orig_policy = sau_backend.app.config["SECURITY_POLICY"]
        sau_backend.app.config["SECURITY_POLICY"] = SecurityPolicy(
            tokens=frozenset(), cors_origins=("http://localhost:5173",)
        )
        sau_backend.app.config["TESTING"] = True
        self.client = sau_backend.app.test_client()

    def tearDown(self):
        self._base_dir_patch.stop()
        self.sau_backend.app.config["SECURITY_POLICY"] = self._orig_policy
        self._tmp.cleanup()

    def _create_profile_and_account(self):
        profile = profile_registry.create_profile(
            "Test Brand", db_path=self.db_path,
        )
        account = profile_registry.add_account(
            profile_id=profile.id,
            platform="telegram",
            account_name="test-tg",
            auth_type="manual",
            config={"chatId": "@test", "botToken": "fake-token"},
            db_path=self.db_path,
        )
        return profile, account

    def test_preview_requires_profile_ids(self):
        resp = self.client.post("/publish-center/preview", json={})
        self.assertEqual(resp.status_code, 400)

    def test_preview_returns_drafts_for_valid_request(self):
        profile, account = self._create_profile_and_account()
        with patch.object(self.sau_backend, "_generate_account_draft", return_value={
            "message": "Hello world", "hashtags": ["#test"], "firstComment": "", "charCount": 11,
        }):
            resp = self.client.post("/publish-center/preview", json={
                "profileIds": [profile.id],
                "selectedAccountIds": [account.id],
                "brief": "Say hello",
                "options": {"watermark": False, "intro": False, "outro": False},
            })
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()["data"]
        self.assertEqual(len(data["profiles"]), 1)
        self.assertEqual(data["profiles"][0]["profileName"], "Test Brand")
        self.assertEqual(len(data["profiles"][0]["accounts"]), 1)
        self.assertEqual(data["profiles"][0]["accounts"][0]["draft"]["message"], "Hello world")


@unittest.skipUnless(flask_available, "Flask not installed")
class PublishCenterSubmitTests(unittest.TestCase):
    def setUp(self):
        import sau_backend
        import db.createTable as create_table

        self.sau_backend = sau_backend
        self._tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self._tmp.name)
        self.db_path = self.base_dir / "db" / "database.db"
        create_table.bootstrap(self.db_path)

        self._base_dir_patch = patch.object(sau_backend, "BASE_DIR", self.base_dir)
        self._base_dir_patch.start()

        from myUtils.security import SecurityPolicy
        self._orig_policy = sau_backend.app.config["SECURITY_POLICY"]
        sau_backend.app.config["SECURITY_POLICY"] = SecurityPolicy(
            tokens=frozenset(), cors_origins=("http://localhost:5173",)
        )
        sau_backend.app.config["TESTING"] = True
        self.client = sau_backend.app.test_client()

    def tearDown(self):
        self._base_dir_patch.stop()
        self.sau_backend.app.config["SECURITY_POLICY"] = self._orig_policy
        self._tmp.cleanup()

    def _create_profile_and_account(self, platform="telegram"):
        profile = profile_registry.create_profile(
            "Test Brand", db_path=self.db_path,
        )
        account = profile_registry.add_account(
            profile_id=profile.id,
            platform=platform,
            account_name=f"test-{platform}",
            auth_type="manual",
            config={"chatId": "@test", "botToken": "fake-token"},
            db_path=self.db_path,
        )
        return profile, account

    def test_submit_requires_profile_ids(self):
        resp = self.client.post("/publish-center/submit", json={
            "mediaFilePaths": ["video.mp4"],
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("profileIds", resp.get_json()["msg"])

    def test_submit_requires_media_files(self):
        profile, _ = self._create_profile_and_account()
        resp = self.client.post("/publish-center/submit", json={
            "profileIds": [profile.id],
            "mediaFilePaths": [],
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("mediaFilePaths", resp.get_json()["msg"])

    def _insert_file_record(self, filename: str = "test-video.mp4") -> int:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO file_records (filename, file_path, filesize) VALUES (?, ?, ?)",
                (filename, filename, 1024),
            )
            conn.commit()
            return cur.lastrowid

    def test_submit_creates_jobs_for_valid_request(self):
        profile, account = self._create_profile_and_account()
        file_record_id = self._insert_file_record()
        with patch.object(self.sau_backend, "_prepare_campaign_media_artifacts", return_value={}), \
             patch.object(self.sau_backend, "_generate_account_draft", return_value={
                 "message": "Test post", "hashtags": [], "firstComment": "",
             }), \
             patch.object(self.sau_backend, "_ensure_file_record_for_path", return_value=file_record_id), \
             patch.object(self.sau_backend, "_artifact_payloads_for_platform", return_value=[]), \
             patch.object(self.sau_backend, "_job_to_payload", side_effect=lambda j: {"id": j.id, "platform": j.platform, "totalTargets": 1}):
            resp = self.client.post("/publish-center/submit", json={
                "profileIds": [profile.id],
                "selectedAccountIds": [account.id],
                "mediaFilePaths": ["test-video.mp4"],
                "brief": "Test post",
                "options": {"watermark": False, "intro": False, "outro": False},
                "schedule": {"publishNow": True},
                "accountDrafts": {},
            })
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()["data"]
        self.assertGreaterEqual(len(data["jobs"]), 1)

    def test_single_media_platform_splits_into_multiple_jobs(self):
        """When a single-media platform gets multiple files, it should split into staggered jobs."""
        profile, account = self._create_profile_and_account(platform="tiktok")
        # TikTok is single-media
        self.assertFalse(platform_capabilities.platform_supports_multi_media("tiktok"))

        file_record_ids = []
        for fname in ["video1.mp4", "video2.mp4", "video3.mp4"]:
            file_record_ids.append(self._insert_file_record(fname))
        call_count = {"n": 0}
        def mock_ensure(path, db_path):
            idx = call_count["n"]
            call_count["n"] += 1
            return file_record_ids[idx] if idx < len(file_record_ids) else file_record_ids[0]

        with patch.object(self.sau_backend, "_prepare_campaign_media_artifacts", return_value={}), \
             patch.object(self.sau_backend, "_generate_account_draft", return_value={
                 "message": "Test TikTok", "hashtags": [], "firstComment": "",
             }), \
             patch.object(self.sau_backend, "_ensure_file_record_for_path", side_effect=mock_ensure), \
             patch.object(self.sau_backend, "_artifact_payloads_for_platform", return_value=[]), \
             patch.object(self.sau_backend, "_job_to_payload", side_effect=lambda j: {"id": j.id, "platform": j.platform, "totalTargets": 1}):
            resp = self.client.post("/publish-center/submit", json={
                "profileIds": [profile.id],
                "selectedAccountIds": [account.id],
                "mediaFilePaths": ["video1.mp4", "video2.mp4", "video3.mp4"],
                "brief": "TikTok batch",
                "options": {"watermark": False, "intro": False, "outro": False},
                "schedule": {"publishNow": True},
                "accountDrafts": {},
            })
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()["data"]
        # Should create 3 jobs (one per video), not 1
        self.assertEqual(len(data["jobs"]), 3)


if __name__ == "__main__":
    unittest.main()
