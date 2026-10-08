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
    conf_module.DEBUG_MODE = True
    conf_module.LOCAL_CHROME_HEADLESS = True
    conf_module.LOCAL_CHROME_PATH = ""
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


class NextFreeSlotTests(unittest.TestCase):
    """Tests for the anti-jam schedule allocator."""

    def setUp(self):
        self._gap = publish_orchestrator.MIN_GAP_MINUTES
        self._cap = publish_orchestrator.MAX_POSTS_PER_ACCOUNT_PER_DAY
        publish_orchestrator.MIN_GAP_MINUTES = 30
        publish_orchestrator.MAX_POSTS_PER_ACCOUNT_PER_DAY = 3
        self.base = datetime(2026, 10, 6, 13, 0, 0)

    def tearDown(self):
        publish_orchestrator.MIN_GAP_MINUTES = self._gap
        publish_orchestrator.MAX_POSTS_PER_ACCOUNT_PER_DAY = self._cap

    def test_publish_now_is_unchanged(self):
        self.assertIsNone(publish_orchestrator._next_free_slot(1, None, 0, {}))
        self.assertIsNotNone(publish_orchestrator._next_free_slot(1, None, 1, {}))

    def test_exact_collision_is_pushed_past_the_gap(self):
        booked = {1: ["2026-10-06T13:00:00"]}
        slot = publish_orchestrator._next_free_slot(1, self.base, 0, booked)
        self.assertEqual(slot, datetime(2026, 10, 6, 13, 30, 0))

    def test_daily_cap_rolls_to_the_next_day_same_slot(self):
        booked = {1: ["2026-10-06T13:00:00", "2026-10-06T13:30:00", "2026-10-06T14:00:00"]}
        slot = publish_orchestrator._next_free_slot(1, self.base, 0, booked)
        self.assertEqual(slot, datetime(2026, 10, 7, 13, 0, 0))

    def test_allocations_in_one_submit_do_not_collide(self):
        booked = {}
        slots = [
            publish_orchestrator._next_free_slot(1, self.base, i, booked)
            for i in range(3)
        ]
        self.assertEqual(len(set(slots)), 3)
        for earlier, later in zip(slots, slots[1:]):
            self.assertGreaterEqual((later - earlier).total_seconds(), 30 * 60)

    def test_different_accounts_do_not_share_a_cap(self):
        booked = {1: ["2026-10-06T13:00:00", "2026-10-06T13:30:00", "2026-10-06T14:00:00"]}
        slot = publish_orchestrator._next_free_slot(2, self.base, 0, booked)
        self.assertEqual(slot, datetime(2026, 10, 6, 13, 0, 0))

    def test_load_booked_slots_is_best_effort(self):
        self.assertEqual(
            publish_orchestrator._load_booked_slots("/nonexistent/db.sqlite"), {}
        )


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

    def test_preview_generates_account_specific_language_drafts(self):
        profile = profile_registry.create_profile("Language Brand", db_path=self.db_path)
        en = profile_registry.add_account(
            profile.id, "twitter", "english", auth_type="oauth",
            config={"audience_language": "en"}, db_path=self.db_path,
        )
        zh = profile_registry.add_account(
            profile.id, "twitter", "mandarin", auth_type="oauth",
            config={"audience_language": "zh"}, db_path=self.db_path,
        )
        seen = []

        def draft(account, *_args, **_kwargs):
            seen.append((account.id, account.config.get("audience_language")))
            return {"message": account.config["audience_language"]}

        with patch.object(self.sau_backend, "_generate_account_draft", side_effect=draft):
            resp = self.client.post("/publish-center/preview", json={
                "profileIds": [profile.id],
                "selectedAccountIds": [en.id, zh.id],
                "brief": "Language routing test",
            })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(seen, [(en.id, "en"), (zh.id, "zh")])

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

    def test_submit_overrides_are_normalized_before_queuing(self):
        """A stringified / label-blob accountDraft must not reach the platform.

        The operator approves copy that the preview already normalised, but the
        override in the submit body is the client's copy of the draft. If it
        arrives stringified (the model returned a JSON object as text) or as a
        "Title: ... / Description: ..." blob, the orchestrator must normalise it
        at the point of consumption rather than post a dict literal.
        """
        profile, account = self._create_profile_and_account()
        file_record_id = self._insert_file_record()
        with patch.object(self.sau_backend, "_prepare_campaign_media_artifacts", return_value={}), \
             patch.object(self.sau_backend, "_ensure_file_record_for_path", return_value=file_record_id), \
             patch.object(self.sau_backend, "_artifact_payloads_for_platform", return_value=[]), \
             patch.object(self.sau_backend, "_job_to_payload", side_effect=lambda j: {"id": j.id, "platform": j.platform, "totalTargets": 1}):
            resp = self.client.post("/publish-center/submit", json={
                "profileIds": [profile.id],
                "selectedAccountIds": [account.id],
                "mediaFilePaths": ["SFW test-video.mp4"],
                "brief": "Test post",
                "options": {"watermark": False, "intro": False, "outro": False},
                "schedule": {"publishNow": True},
                "accountDrafts": {
                    str(account.id): {
                        "message": "{'title': 'A title', 'description': 'Real body text'}",
                        "hashtags": [],
                        "firstComment": "",
                    },
                },
            })
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()["data"]
        self.assertGreaterEqual(len(data["jobs"]), 1)
        job_id = data["jobs"][0]["id"]

        from myUtils import jobs as job_runtime
        job = job_runtime.get_job(job_id, db_path=self.db_path)
        draft = job.payload.get("draft") or {}
        message = str(draft.get("message") or "")
        self.assertNotIn("{'title'", message, "stringified draft leaked to the platform")
        self.assertNotIn('"title"', message)
        # The body copy survives; only the field labels are removed.
        self.assertIn("Real body text", message)

    def test_youtube_is_skipped_for_image_only_media_with_account_name(self):
        profile, account = self._create_profile_and_account(platform="youtube")
        resp = self._submit(profile, account, ["SFW still.jpg"])
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()["data"]
        self.assertEqual(data["jobs"], [])
        self.assertEqual(data["skipped"][0]["accountName"], account.account_name)
        self.assertIn("requires video media", data["skipped"][0]["reason"])

    def test_tiktok_photo_draft_is_skipped_before_queueing(self):
        profile, account = self._create_profile_and_account(platform="tiktok")
        resp = self._submit(profile, account, ["SFW still.jpg"], options={
            "watermark": False, "intro": False, "outro": False, "tiktokDirectPost": False,
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()["data"]
        self.assertEqual(data["jobs"], [])
        self.assertEqual(data["skipped"][0]["accountName"], account.account_name)
        self.assertIn("requires Direct Post", data["skipped"][0]["reason"])

    def test_single_media_platform_splits_into_multiple_jobs(self):
        """When a single-media platform gets multiple files, it should split into staggered jobs.

        TikTok bans nudity, so the fixtures carry the ``SFW`` marker: an
        unlabelled filename is rated NSFW and would (correctly) be dropped by
        the content gate before it ever reaches the split logic under test.
        """
        profile, account = self._create_profile_and_account(platform="tiktok")
        # TikTok is single-media
        self.assertFalse(platform_capabilities.platform_supports_multi_media("tiktok"))

        file_record_ids = []
        for fname in ["SFW video1.mp4", "SFW video2.mp4", "SFW video3.mp4"]:
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
                "mediaFilePaths": ["SFW video1.mp4", "SFW video2.mp4", "SFW video3.mp4"],
                "brief": "TikTok batch",
                "options": {"watermark": False, "intro": False, "outro": False},
                "schedule": {"publishNow": True},
                "accountDrafts": {},
            })
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()["data"]
        # Should create 3 jobs (one per video), not 1
        self.assertEqual(len(data["jobs"]), 3)

    # --- NSFW containment on the publish-center path (the one the UI uses) ---

    def _submit(self, profile, account, media, **extra):
        file_record_id = self._insert_file_record(media[0])
        with patch.object(self.sau_backend, "_prepare_campaign_media_artifacts", return_value={}), \
             patch.object(self.sau_backend, "_generate_account_draft", return_value={
                 "message": "post", "hashtags": [], "firstComment": "",
             }), \
             patch.object(self.sau_backend, "_ensure_file_record_for_path", return_value=file_record_id), \
             patch.object(self.sau_backend, "_artifact_payloads_for_platform", return_value=[]), \
             patch.object(self.sau_backend, "_job_to_payload", side_effect=lambda j: {"id": j.id, "platform": j.platform, "totalTargets": 1}):
            body = {
                "profileIds": [profile.id],
                "selectedAccountIds": [account.id],
                "mediaFilePaths": media,
                "brief": "post",
                "options": {"watermark": False, "intro": False, "outro": False},
                "schedule": {"publishNow": True},
                "accountDrafts": {},
            }
            body.update(extra)
            return self.client.post("/publish-center/submit", json=body)

    def test_unlabelled_media_is_never_sent_to_instagram_even_when_selected(self):
        # Explicitly selecting the Instagram account must not be enough: the
        # filename carries no "sfw" marker, so the file is NSFW and Instagram
        # is dropped. This is the publish-center route the web UI calls.
        profile, ig = self._create_profile_and_account(platform="instagram")
        resp = self._submit(profile, ig, ["20260722155038425.mp4"])
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()["data"]
        self.assertEqual(data["jobs"], [])
        self.assertEqual(
            [s["reason"] for s in data["skipped"]], ["nsfw_no_adult_safe_account"],
        )

    def test_sfw_labelled_media_reaches_instagram(self):
        profile, ig = self._create_profile_and_account(platform="instagram")
        resp = self._submit(profile, ig, ["SFW 20260813074703243.mp4"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()["data"]["jobs"]), 1)

    def test_unlabelled_media_still_reaches_adult_safe_platforms(self):
        profile, tg = self._create_profile_and_account(platform="telegram")
        resp = self._submit(profile, tg, ["20260722155038425.mp4"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()["data"]["jobs"]), 1)

    def test_nsfw_batch_to_mixed_profile_keeps_only_safe_accounts(self):
        profile, tg = self._create_profile_and_account(platform="telegram")
        ig = profile_registry.add_account(
            profile_id=profile.id, platform="instagram", account_name="ig",
            auth_type="manual", config={}, db_path=self.db_path,
        )
        resp = self._submit(
            profile, tg, ["20260722155038425.mp4"],
            selectedAccountIds=[tg.id, ig.id],
        )
        self.assertEqual(resp.status_code, 200)
        jobs = resp.get_json()["data"]["jobs"]
        self.assertEqual([j["platform"] for j in jobs], ["telegram"])

    def test_sfw_flag_in_options_cannot_launder_unlabelled_media(self):
        # A request body saying "this is SFW" is not trusted over the filename.
        profile, ig = self._create_profile_and_account(platform="instagram")
        resp = self._submit(
            profile, ig, ["20260722155038425.mp4"],
            options={"watermark": False, "intro": False, "outro": False, "sfwFlag": "sfw"},
        )
        self.assertEqual(resp.get_json()["data"]["jobs"], [])


if __name__ == "__main__":
    unittest.main()


class RequestDataContactDefaultsTests(unittest.TestCase):
    """contactDetails / cta fall back to the profile so Threads drafts do not raise."""

    def _profile(self, **overrides):
        profile = MagicMock()
        profile.settings = overrides.pop("settings", {})
        profile.contact_details = overrides.pop("contact_details", "")
        profile.default_cta = overrides.pop("default_cta", "")
        return profile

    def test_option_overrides_profile(self):
        result = publish_orchestrator._request_data_for_options(
            brief="b", options={"contactDetails": "TG @opt", "cta": "Opt CTA"},
            profile=self._profile(contact_details="TG @col", default_cta="Col CTA"),
        )
        self.assertEqual(result["contactDetails"], "TG @opt")
        self.assertEqual(result["cta"], "Opt CTA")

    def test_profile_columns_used_when_no_option(self):
        result = publish_orchestrator._request_data_for_options(
            brief="b", options={},
            profile=self._profile(contact_details=" TG @col ", default_cta="Col CTA"),
        )
        self.assertEqual(result["contactDetails"], "TG @col")
        self.assertEqual(result["cta"], "Col CTA")

    def test_legacy_settings_keys_used_last(self):
        result = publish_orchestrator._request_data_for_options(
            brief="b", options={},
            profile=self._profile(settings={"contactDetails": "IG @legacy", "ctaText": "Legacy CTA"}),
        )
        self.assertEqual(result["contactDetails"], "IG @legacy")
        self.assertEqual(result["cta"], "Legacy CTA")

    def test_non_string_values_become_empty(self):
        profile = MagicMock()  # attributes are MagicMocks, not strings
        profile.settings = {}
        result = publish_orchestrator._request_data_for_options(brief="b", options={}, profile=profile)
        self.assertEqual(result["contactDetails"], "")
        self.assertEqual(result["cta"], "")


class DuplicateQueueGuardTests(unittest.TestCase):
    """The same media must not be queued twice for one account.

    publish_job_targets is UNIQUE on (job_id, account_ref, file_ref), which stops
    a duplicate within a job but not the same media arriving again under a new
    job_id. That gap let one Telegram account accumulate 662 targets for 356
    distinct files (the same clips queued 4-5 times), pushing its horizon to
    2027-04. Identity is the media GROUP, and only live states count, so a
    genuine re-publish after a terminal failure still works.
    """

    def setUp(self) -> None:
        import db.createTable as create_table

        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "guard.db"
        create_table.bootstrap(self.db_path)
        self.profile = profile_registry.create_profile("Guard", db_path=self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed(self, account_ref: str, status: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO media_groups (id, name, notes, status)"
                " VALUES (900, 'g', '', 'ready')"
            )
            conn.execute(
                "INSERT INTO campaigns (id, profile_id, media_group_id, status)"
                " VALUES (900, ?, 900, 'ready')",
                (self.profile.id,),
            )
            conn.execute(
                "INSERT INTO campaign_posts (id, campaign_id, platform, account_ids_json)"
                " VALUES (900, 900, 'telegram', '[]')"
            )
            conn.execute(
                "INSERT INTO publish_jobs (id, idempotency_key, platform, payload_json)"
                " VALUES (900, 'k900', 'telegram', '{}')"
            )
            conn.execute(
                "INSERT INTO publish_job_targets (job_id, account_ref, file_ref, status)"
                " VALUES (900, ?, 'campaign_post:900', ?)",
                (account_ref, status),
            )
            conn.commit()

    def test_live_target_blocks_a_second_queue(self):
        self._seed("account:127", "pending")
        self.assertTrue(
            publish_orchestrator._already_queued_for_media(
                127, 900, db_path=self.db_path
            )
        )

    def test_terminal_states_do_not_block_a_republish(self):
        # Fresh DB per status: the seed reuses fixed ids, so reuse would trip
        # the media_groups primary key rather than test the guard.
        import db.createTable as create_table

        for status in ("succeeded", "failed", "cancelled"):
            with self.subTest(status=status):
                tmp = tempfile.TemporaryDirectory()
                try:
                    db_path = Path(tmp.name) / "guard.db"
                    create_table.bootstrap(db_path)
                    self.db_path = db_path
                    self._seed("account:127", status)
                    self.assertFalse(
                        publish_orchestrator._already_queued_for_media(
                            127, 900, db_path=db_path
                        )
                    )
                finally:
                    tmp.cleanup()

    def test_a_different_account_is_not_blocked(self):
        self._seed("account:127", "pending")
        self.assertFalse(
            publish_orchestrator._already_queued_for_media(
                999, 900, db_path=self.db_path
            )
        )

    def test_a_lookup_failure_does_not_block_publishing(self):
        # A missing DB/table must degrade to "not a duplicate", never raise.
        self.assertFalse(
            publish_orchestrator._already_queued_for_media(
                127, 900, db_path=Path(self._tmp.name) / "missing.db"
            )
        )


class DuplicateQueueGuardWiringTests(unittest.TestCase):
    """The guard must actually run in submit_publish, not just exist.

    The other tests exercise the helper directly; this pins the call site so
    removing it from the account loop fails here.
    """

    def test_account_loop_consults_the_duplicate_guard(self):
        import inspect

        source = inspect.getsource(publish_orchestrator.submit_publish)
        self.assertIn("_already_queued_for_media(", source)
        # And it must be a real guard, not commented out or short-circuited.
        self.assertNotIn("if False and _already_queued_for_media(", source)
        self.assertIn("already queued for this account", source)
