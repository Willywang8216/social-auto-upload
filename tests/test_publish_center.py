"""Tests for the Publish Center feature.

Covers:
- publish_services.submit_publish (durable admission, off-request prep and splitting)
- publish_orchestrator._request_data_for_options (option translation)
- publish_orchestrator._resolve_base_time (schedule resolution)
- Backend HTTP endpoints: /publish-center/preview, /regenerate, /submit
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack, closing
from datetime import datetime, timedelta, timezone
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
from myUtils import campaign_media_prep, campaign_prep, campaigns as campaign_store
from myUtils import jobs as job_runtime, publish_services
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

    def test_naive_start_at_is_the_operator_wall_clock_converted_to_utc(self):
        """A naive startAt is local time, not UTC.

        The Publish Center date picker sends ``value-format="YYYY-MM-DDTHH:mm:00"``
        with no offset, so the string is the wall clock the operator saw. This
        test previously asserted ``hour == 10`` for an input of 10:00 - i.e. it
        encoded the bug, under which an operator picking 07:00 got 15:00. With
        Asia/Shanghai as the operator timezone, 10:00 local is 02:00 UTC.
        """
        # Exercise localization, independent of Windows' optional tzdata package.
        with patch.dict(os.environ, {"SAU_OPERATOR_TIMEZONE": "Asia/Shanghai"}), \
                patch.object(publish_orchestrator, "ZoneInfo", return_value=timezone(timedelta(hours=8))) as zone:
            result = publish_orchestrator._resolve_base_time({
                "publishNow": False,
                "startAt": "2026-06-17T10:00:00",
            })
        zone.assert_called_once_with("Asia/Shanghai")
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2026)
        self.assertEqual(result.month, 6)
        self.assertEqual(result.hour, 2, "10:00 Asia/Shanghai is 02:00 UTC")
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
            brief="test", options={"watermarkDisabled": True}, profile=profile,
        )
        self.assertEqual(result["watermark"], "")

    def test_watermark_defaults_to_the_profile_config(self):
        profile = MagicMock()
        profile.settings = {"watermark": "Brand WM"}
        # No toggle: the profile watermark still applies.
        result = publish_orchestrator._request_data_for_options(
            brief="test", options={}, profile=profile,
        )
        self.assertEqual(result["watermark"], "Brand WM")

    def test_watermark_override_wins(self):
        profile = MagicMock()
        profile.settings = {"watermark": "Brand WM"}
        result = publish_orchestrator._request_data_for_options(
            brief="test", options={"watermarkOverride": "Custom WM"}, profile=profile,
        )
        self.assertEqual(result["watermark"], "Custom WM")

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
        db_env = patch.dict(os.environ, {"SAU_DB_PATH": str(self.db_path)})
        db_env.start()
        self.addCleanup(db_env.stop)
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
        self.addCleanup(self._tmp.cleanup)
        self.base_dir = Path(self._tmp.name)
        self.db_path = self.base_dir / "db" / "database.db"
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.dict(os.environ, {"SAU_DB_PATH": str(self.db_path)}))
        self.patches.enter_context(patch.object(sau_backend, "BASE_DIR", self.base_dir))
        self.patches.enter_context(patch.object(campaign_media_prep, "BASE_DIR", self.base_dir))
        create_table.bootstrap(self.db_path)
        (self.base_dir / "videoFile").mkdir()

        from myUtils.security import SecurityPolicy
        self.patches.enter_context(patch.dict(sau_backend.app.config, {
            "SECURITY_POLICY": SecurityPolicy(
                tokens=frozenset(), cors_origins=("http://localhost:5173",),
            ),
            "TESTING": True,
        }))
        self.client = sau_backend.app.test_client()
        # Admission must not execute media prep, generation, or notifications.
        self.prep = self.patches.enter_context(patch.object(
            publish_services, "_prepare_campaign_media_artifacts",
            side_effect=AssertionError("media prep ran during HTTP admission"),
        ))
        self.draft = self.patches.enter_context(patch.object(
            publish_services, "_generate_account_draft",
            side_effect=AssertionError("draft generation ran during HTTP admission"),
        ))
        self.notify = self.patches.enter_context(patch.object(publish_services, "_notify_tg_review"))
        self.drain = self.patches.enter_context(patch.object(sau_backend, "_start_worker_drain_thread"))

    def _create_profile_and_account(self, platform="telegram"):
        profile = profile_registry.create_profile("Test Brand", db_path=self.db_path)
        account = profile_registry.add_account(
            profile_id=profile.id, platform=platform, account_name=f"test-{platform}",
            auth_type="manual", config={"chatId": "@test", "botToken": "fake-token"},
            db_path=self.db_path,
        )
        return profile, account

    def _write_media(self, filename):
        path = self.base_dir / "videoFile" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic media; the prep callback is mocked")
        return path

    def _snapshot(self):
        tables = ("file_records", "media_groups", "media_group_items", "campaigns",
                  "campaign_posts", "publish_jobs", "publish_job_targets", "publish_submissions")
        with closing(sqlite3.connect(self.db_path)) as conn:
            return {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                    for table in tables}

    def _assert_admitted(self, response, *, campaign_count=1):
        self.assertEqual(response.status_code, 202, response.get_json())
        body = response.get_json()
        self.assertEqual(body["code"], 200)
        data = body["data"]
        self.assertEqual(data["status"], "preparing" if campaign_count else "needs_review")
        self.assertEqual(len(data["campaignIds"]), campaign_count)
        self.assertEqual(data["jobs"], [])
        self.prep.assert_not_called()
        self.draft.assert_not_called()
        self.notify.assert_not_called()
        snapshot = self._snapshot()
        for table in ("campaign_posts", "publish_jobs", "publish_job_targets"):
            self.assertEqual(snapshot[table], [], table)
        for campaign_id in data["campaignIds"]:
            campaign = campaign_store.get_campaign(campaign_id, db_path=self.db_path)
            self.assertEqual(campaign.status, campaign_store.CAMPAIGN_PREPARING)
            self.assertIsNone(campaign.prepared_at)
        return data

    def _prepare_admitted(self, data):
        """Run the real Flask-free prep/finalize path, mocking only heavy work."""
        from flask import has_app_context, has_request_context

        self.assertFalse(has_app_context())
        self.assertFalse(has_request_context())

        def prepare(campaign_id, profile, media_files, request_data, **kwargs):
            self.assertEqual(kwargs["db_path"], self.db_path)
            for media in media_files:
                path = campaign_media_prep._resolve_video_file_path_safely(media["file_path"])
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 0)
                campaign_store.add_campaign_artifact(
                    campaign_id, artifact_kind="local",
                    source_file_record_id=media["file_record_id"], local_path=str(path),
                    metadata={"role": media["role"]}, db_path=self.db_path,
                )
            return {"transcriptText": "A quiet walk beside the river."}

        result = {"jobs": [], "skipped": []}
        with patch.object(publish_services, "_prepare_campaign_media_artifacts", side_effect=prepare), \
                patch.object(publish_services, "_generate_account_draft", return_value={
                    "message": "A quiet walk beside the river.", "hashtags": [], "firstComment": "",
                }), patch.object(publish_services, "_notify_tg_review"):
            runner = campaign_prep.make_default_prep_runner()
            for campaign_id in data["campaignIds"]:
                campaign = campaign_store.claim_next_preparing_campaign(
                    owner="test-publish-center", db_path=self.db_path,
                )
                self.assertIsNotNone(campaign)
                self.assertEqual(campaign.id, campaign_id)
                prepared = runner(campaign, self.db_path)
                result["jobs"].extend(prepared["jobs"])
                result["skipped"].extend(prepared["skipped"])
        return result

    def _submit(self, profile, account, media, **extra):
        for filename in media:
            self._write_media(filename)
        body = {
            "profileIds": [profile.id], "selectedAccountIds": [account.id],
            "mediaFilePaths": media, "brief": "post",
            "options": {"watermark": False, "intro": False, "outro": False},
            "schedule": {"publishNow": True}, "accountDrafts": {},
        }
        body.update(extra)
        return self.client.post("/publish-center/submit", json=body)

    def test_submit_requires_profile_ids(self):
        before = self._snapshot()
        resp = self.client.post("/publish-center/submit", json={"mediaFilePaths": ["video.mp4"]})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("profile", resp.get_json()["msg"])
        self.assertEqual(self._snapshot(), before)

    def test_submit_requires_media_files(self):
        profile, _ = self._create_profile_and_account()
        before = self._snapshot()
        resp = self.client.post("/publish-center/submit", json={
            "profileIds": [profile.id], "mediaFilePaths": [],
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("media file", resp.get_json()["msg"])
        self.assertEqual(self._snapshot(), before)

    def test_submit_creates_jobs_for_valid_request(self):
        profile, account = self._create_profile_and_account()
        resp = self._submit(profile, account, ["test-video.mp4"], brief="Test post")
        data = self._assert_admitted(resp)
        prepared = self._prepare_admitted(data)
        self.assertEqual(len(prepared["jobs"]), 1)
        job = job_runtime.get_job(prepared["jobs"][0]["id"], db_path=self.db_path)
        self.assertEqual(job.platform, "telegram")
        self.assertEqual(job.payload["draft"]["message"], "A quiet walk beside the river.")
        self.assertTrue(job.payload["artifacts"])
        posts = campaign_store.list_campaign_posts(data["campaignIds"][0], db_path=self.db_path)
        self.assertEqual(posts[0].draft, job.payload["draft"])
        self.assertEqual(posts[0].status, campaign_store.CAMPAIGN_POST_QUEUED)

    def test_submit_overrides_are_normalized_before_queuing(self):
        """Stringified client overrides must be normalized at prep consumption."""
        profile, account = self._create_profile_and_account()
        resp = self._submit(profile, account, ["SFW test-video.mp4"], accountDrafts={
            str(account.id): {
                "message": "{'title': 'A title', 'description': 'Real body text'}",
                "hashtags": [], "firstComment": "",
            },
        })
        data = self._assert_admitted(resp)
        prepared = self._prepare_admitted(data)
        self.assertEqual(len(prepared["jobs"]), 1)
        job = job_runtime.get_job(prepared["jobs"][0]["id"], db_path=self.db_path)
        message = str(job.payload["draft"].get("message") or "")
        self.assertNotIn("{'title'", message, "stringified draft leaked to the platform")
        self.assertNotIn('"title"', message)
        self.assertIn("Real body text", message)

    def test_youtube_is_skipped_for_image_only_media_with_account_name(self):
        profile, account = self._create_profile_and_account(platform="youtube")
        data = self._assert_admitted(self._submit(profile, account, ["SFW still.jpg"]), campaign_count=0)
        self.assertEqual(data["skipped"][0]["accountName"], account.account_name)
        self.assertIn("requires video media", data["skipped"][0]["reason"])
        self.drain.assert_not_called()

    def test_tiktok_photo_draft_is_skipped_before_queueing(self):
        profile, account = self._create_profile_and_account(platform="tiktok")
        resp = self._submit(profile, account, ["SFW still.jpg"], options={
            "watermark": False, "intro": False, "outro": False, "tiktokDirectPost": False,
        })
        data = self._assert_admitted(resp, campaign_count=0)
        self.assertEqual(data["skipped"][0]["accountName"], account.account_name)
        self.assertIn("requires Direct Post", data["skipped"][0]["reason"])
        self.drain.assert_not_called()

    def test_single_media_platform_splits_into_multiple_jobs(self):
        # TikTok forbids nudity: keep the source filename's SFW marker so this
        # exercises splitting, not a (correct) content-policy rejection.
        profile, account = self._create_profile_and_account(platform="tiktok")
        self.assertFalse(platform_capabilities.platform_supports_multi_media("tiktok"))
        media = ["SFW video1.mp4", "SFW video2.mp4", "SFW video3.mp4"]
        data = self._assert_admitted(self._submit(profile, account, media))
        prepared = self._prepare_admitted(data)
        self.assertEqual(len(prepared["jobs"]), 3)
        jobs = [job_runtime.get_job(j["id"], db_path=self.db_path) for j in prepared["jobs"]]
        self.assertEqual(len({tuple(j.payload["fileRecordIds"]) for j in jobs}), 3)
        self.assertTrue(all(len(j.payload["fileRecordIds"]) == 1 for j in jobs))

    def test_unlabelled_media_is_never_sent_to_instagram_even_when_selected(self):
        profile, ig = self._create_profile_and_account(platform="instagram")
        data = self._assert_admitted(
            self._submit(profile, ig, ["20260722155038425.mp4"]), campaign_count=0,
        )
        self.assertEqual([s["reason"] for s in data["skipped"]], ["nsfw_no_adult_safe_account"])
        self.drain.assert_not_called()

    def test_sfw_labelled_media_reaches_instagram(self):
        profile, ig = self._create_profile_and_account(platform="instagram")
        data = self._assert_admitted(self._submit(profile, ig, ["SFW 20260813074703243.mp4"]))
        prepared = self._prepare_admitted(data)
        self.assertEqual([j["platform"] for j in prepared["jobs"]], ["instagram"])

    def test_unlabelled_media_still_reaches_adult_safe_platforms(self):
        profile, tg = self._create_profile_and_account(platform="telegram")
        data = self._assert_admitted(self._submit(profile, tg, ["20260722155038425.mp4"]))
        prepared = self._prepare_admitted(data)
        self.assertEqual([j["platform"] for j in prepared["jobs"]], ["telegram"])

    def test_nsfw_batch_to_mixed_profile_keeps_only_safe_accounts(self):
        profile, tg = self._create_profile_and_account(platform="telegram")
        ig = profile_registry.add_account(
            profile.id, "instagram", "ig", auth_type="manual", db_path=self.db_path,
        )
        data = self._assert_admitted(self._submit(
            profile, tg, ["20260722155038425.mp4"], selectedAccountIds=[tg.id, ig.id],
        ))
        campaign = campaign_store.get_campaign(data["campaignIds"][0], db_path=self.db_path)
        self.assertEqual(campaign.selected_account_ids, [tg.id])
        self.assertEqual(campaign_store.get_prep_request(campaign)["selectedAccountIds"], [tg.id])
        prepared = self._prepare_admitted(data)
        self.assertEqual([j["platform"] for j in prepared["jobs"]], ["telegram"])

    def test_sfw_flag_in_options_cannot_launder_unlabelled_media(self):
        profile, ig = self._create_profile_and_account(platform="instagram")
        resp = self._submit(profile, ig, ["20260722155038425.mp4"], options={
            "watermark": False, "intro": False, "outro": False, "sfwFlag": "sfw",
        })
        data = self._assert_admitted(resp, campaign_count=0)
        self.assertEqual([s["reason"] for s in data["skipped"]], ["nsfw_no_adult_safe_account"])
        self.drain.assert_not_called()

    def _submit_async_payload(self, profile, account, filename="SFW async.mp4"):
        return {
            "profileIds": [profile.id], "selectedAccountIds": [account.id],
            "mediaFilePaths": [filename], "brief": "async post",
            "options": {"watermark": False, "intro": False, "outro": False},
            "schedule": {"publishNow": True}, "accountDrafts": {},
        }

    def test_async_prep_off_still_uses_durable_admission(self):
        profile, account = self._create_profile_and_account()
        with patch.dict(os.environ, {"SAU_ASYNC_PREP": "0"}):
            resp = self._submit(profile, account, ["test-video.mp4"])
        self._assert_admitted(resp)
        self.drain.assert_called_once()

    def test_async_prep_on_returns_preparing_without_running_prep(self):
        profile, account = self._create_profile_and_account()
        self._write_media("SFW async.mp4")
        with patch.dict(os.environ, {"SAU_ASYNC_PREP": "1"}):
            resp = self.client.post("/publish-center/submit", json=self._submit_async_payload(profile, account))
        data = self._assert_admitted(resp)
        self.drain.assert_called_once()
        campaign = campaign_store.get_campaign(data["campaignIds"][0], db_path=self.db_path)
        request = campaign_store.get_prep_request(campaign)
        self.assertIsNotNone(request)
        self.assertEqual(request["brief"], "async post")
        self.assertEqual(request["schedule"], {"publishNow": True})
        self.assertEqual(request["mediaFilePaths"], ["SFW async.mp4"])
        self.assertEqual(request["selectedAccountIds"], [account.id])

    def test_async_prep_on_starts_drain_with_empty_jobs(self):
        profile, account = self._create_profile_and_account()
        with patch.dict(os.environ, {"SAU_ASYNC_PREP": "1"}):
            resp = self._submit(profile, account, ["SFW async.mp4"])
        self._assert_admitted(resp)
        self.drain.assert_called_once()

    def test_stable_submission_id_replays_identical_admission_after_prep(self):
        profile, account = self._create_profile_and_account()
        self._write_media("SFW async.mp4")
        body = dict(self._submit_async_payload(profile, account), submissionId="ui:stable")
        first = self.client.post("/publish-center/submit", json=body)
        data = self._assert_admitted(first)
        self.assertEqual(data["submissionId"], "ui:stable")
        self.assertEqual(len(self._prepare_admitted(data)["jobs"]), 1)
        before_retry = self._snapshot()
        second = self.client.post("/publish-center/submit", json=body)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.get_json(), first.get_json())
        self.assertEqual(self._snapshot(), before_retry)

    def test_stable_submission_id_rejects_changed_intent_without_mutation(self):
        profile, account = self._create_profile_and_account()
        self._write_media("SFW async.mp4")
        body = dict(self._submit_async_payload(profile, account), submissionId="ui:stable")
        self._assert_admitted(self.client.post("/publish-center/submit", json=body))
        before = self._snapshot()
        response = self.client.post("/publish-center/submit", json=dict(body, brief="Different intent"))
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()["code"], 409)
        self.assertIsNone(response.get_json()["data"])
        self.assertEqual(self._snapshot(), before)

    def test_all_selected_accounts_are_validated_before_admission(self):
        profile, account = self._create_profile_and_account()
        other = profile_registry.create_profile("Other", db_path=self.db_path)
        foreign = profile_registry.add_account(other.id, "telegram", "other", auth_type="manual", db_path=self.db_path)
        self._write_media("SFW async.mp4")
        body = self._submit_async_payload(profile, account)
        before = self._snapshot()
        for invalid_id in (99999, foreign.id):
            with self.subTest(account=invalid_id):
                response = self.client.post("/publish-center/submit", json=dict(
                    body, selectedAccountIds=[account.id, invalid_id],
                ))
                self.assertEqual(response.status_code, 400, response.get_json())
                self.assertIn("Account", response.get_json()["msg"])
                self.assertEqual(self._snapshot(), before)
        self.drain.assert_not_called()

    def test_invalid_media_paths_are_rejected_without_admission(self):
        profile, account = self._create_profile_and_account()
        outside = self.base_dir / "SFW outside.mp4"
        outside.write_bytes(b"not inside videoFile")
        (self.base_dir / "videoFile" / "SFW directory.mp4").mkdir()
        before = self._snapshot()
        for path in ("../SFW outside.mp4", str(outside), "SFW missing.mp4", "SFW directory.mp4"):
            with self.subTest(path=path):
                response = self.client.post("/publish-center/submit", json=self._submit_async_payload(profile, account, path))
                self.assertEqual(response.status_code, 400, response.get_json())
                self.assertEqual(self._snapshot(), before)
        self.drain.assert_not_called()

    def test_empty_local_media_is_rejected_without_admission(self):
        profile, account = self._create_profile_and_account()
        (self.base_dir / "videoFile" / "SFW empty.mp4").touch()
        before = self._snapshot()
        response = self.client.post("/publish-center/submit", json=self._submit_async_payload(profile, account, "SFW empty.mp4"))
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(self._snapshot(), before)
        self.drain.assert_not_called()

    def test_registered_local_media_without_bytes_is_not_a_remote_source(self):
        profile, account = self._create_profile_and_account()
        filename = "SFW stale.mp4"
        # A legacy row without an exact remote backend/key cannot justify an
        # absent local source. The inbox tests cover genuine remote admission.
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO file_records (filename, file_path, filesize) VALUES (?, ?, ?)",
                (filename, filename, 1),
            )
        before = self._snapshot()
        response = self.client.post(
            "/publish-center/submit", json=self._submit_async_payload(profile, account, filename),
        )
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(self._snapshot(), before)
        self.drain.assert_not_called()


class AsyncPrepSwitchTests(unittest.TestCase):
    def test_switch_parses_truthy_values_only(self):
        import sau_backend

        for value in ("1", "true", "TRUE", "yes", "YES", "on", "On"):
            with self.subTest(value=value), patch.dict(os.environ, {"SAU_ASYNC_PREP": value}):
                self.assertTrue(sau_backend._async_prep_enabled())
        for value in ("", "0", "false", "no", "off", "maybe"):
            with self.subTest(value=value), patch.dict(os.environ, {"SAU_ASYNC_PREP": value}):
                self.assertFalse(sau_backend._async_prep_enabled())

    def test_switch_defaults_off_when_unset(self):
        import sau_backend

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SAU_ASYNC_PREP", None)
            self.assertFalse(sau_backend._async_prep_enabled())


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
        db_env = patch.dict(os.environ, {"SAU_DB_PATH": str(self.db_path)})
        db_env.start()
        self.addCleanup(db_env.stop)
        create_table.bootstrap(self.db_path)
        self.profile = profile_registry.create_profile("Guard", db_path=self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed(self, account_ref: str, status: str) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
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


class DuplicateGuardSameFileDifferentGroupTests(unittest.TestCase):
    """A retried submit builds a NEW media group around the SAME files.

    The guard originally keyed only on media_group_id, so a resubmit never
    matched and queued again. That is how a retrying client produced 103
    duplicate pending jobs, and how file_record 1142 ended up in 6 media groups.
    Identity must also consider the underlying file records.
    """

    def setUp(self) -> None:
        import db.createTable as create_table

        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "dup.db"
        db_env = patch.dict(os.environ, {"SAU_DB_PATH": str(self.db_path)})
        db_env.start()
        self.addCleanup(db_env.stop)
        create_table.bootstrap(self.db_path)
        self.profile = profile_registry.create_profile("Dup", db_path=self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed_group(self, gid: int, post_id: int, job_id: int, account_ref: str, status: str) -> None:
        """One media group -> campaign -> post -> job -> target chain."""
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO file_records (id, filename, file_path) VALUES (701, 'clip.mp4', 'clip.mp4')"
            )
            conn.execute(
                "INSERT INTO media_groups (id, name, notes, status) VALUES (?, 'g', '', 'ready')",
                (gid,),
            )
            conn.execute(
                "INSERT INTO media_group_items (media_group_id, file_record_id, role, sort_order)"
                " VALUES (?, 701, 'video', 0)",
                (gid,),
            )
            conn.execute(
                "INSERT INTO campaigns (id, profile_id, media_group_id, status) VALUES (?, ?, ?, 'publishing')",
                (gid, self.profile.id, gid),
            )
            conn.execute(
                "INSERT INTO campaign_posts (id, campaign_id, platform, account_ids_json)"
                " VALUES (?, ?, 'telegram', '[]')",
                (post_id, gid),
            )
            conn.execute(
                "INSERT INTO publish_jobs (id, idempotency_key, platform, payload_json)"
                " VALUES (?, ?, 'telegram', '{}')",
                (job_id, f"k{job_id}"),
            )
            conn.execute(
                "INSERT INTO publish_job_targets (job_id, account_ref, file_ref, status)"
                " VALUES (?, ?, ?, ?)",
                (job_id, account_ref, f"campaign_post:{post_id}", status),
            )
            conn.commit()

    def test_same_file_in_a_new_media_group_is_still_a_duplicate(self):
        # Group 810 queued for account 127.
        self._seed_group(810, 810, 810, "account:127", "pending")
        # A RETRY: same file_record 701, but a brand-new group 811 and campaign.
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO media_groups (id, name, notes, status) VALUES (811, 'g2', '', 'ready')"
            )
            conn.execute(
                "INSERT INTO media_group_items (media_group_id, file_record_id, role, sort_order)"
                " VALUES (811, 701, 'video', 0)"
            )
            conn.commit()
        self.assertTrue(
            publish_orchestrator._already_queued_for_media(
                127, 811, db_path=self.db_path
            ),
            "the SAME file re-submitted under a new media group must be caught",
        )

    def test_a_genuinely_different_file_is_not_blocked(self):
        self._seed_group(820, 820, 820, "account:127", "pending")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO file_records (id, filename, file_path) VALUES (702, 'other.mp4', 'other.mp4')"
            )
            conn.execute(
                "INSERT INTO media_groups (id, name, notes, status) VALUES (821, 'g3', '', 'ready')"
            )
            conn.execute(
                "INSERT INTO media_group_items (media_group_id, file_record_id, role, sort_order)"
                " VALUES (821, 702, 'video', 0)"
            )
            conn.commit()
        self.assertFalse(
            publish_orchestrator._already_queued_for_media(
                127, 821, db_path=self.db_path
            )
        )

    def test_same_file_on_a_different_account_is_allowed(self):
        self._seed_group(830, 830, 830, "account:127", "pending")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO media_groups (id, name, notes, status) VALUES (831, 'g4', '', 'ready')"
            )
            conn.execute(
                "INSERT INTO media_group_items (media_group_id, file_record_id, role, sort_order)"
                " VALUES (831, 701, 'video', 0)"
            )
            conn.commit()
        self.assertFalse(
            publish_orchestrator._already_queued_for_media(
                999, 831, db_path=self.db_path
            )
        )

    def test_sharing_one_file_with_a_different_set_is_not_blocked(self):
        # Queued group 840 holds only file 701.
        self._seed_group(840, 840, 840, "account:127", "pending")
        # A genuinely new submission reuses 701 but also adds 702. The file SET
        # differs, so "shares any one file" must not treat it as a retry.
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO file_records (id, filename, file_path) VALUES (702, 'b.mp4', 'b.mp4')"
            )
            conn.execute(
                "INSERT INTO media_groups (id, name, notes, status) VALUES (841, 'g5', '', 'ready')"
            )
            conn.executemany(
                "INSERT INTO media_group_items (media_group_id, file_record_id, role, sort_order)"
                " VALUES (841, ?, 'video', ?)",
                [(701, 0), (702, 1)],
            )
            conn.commit()
        self.assertFalse(
            publish_orchestrator._already_queued_for_media(
                127, 841, db_path=self.db_path
            ),
            "a different file set must not be blocked just for sharing one file",
        )

    def test_same_set_in_a_different_order_is_still_a_duplicate(self):
        # Queued group 850 holds {701, 702}.
        self._seed_group(850, 850, 850, "account:127", "pending")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO file_records (id, filename, file_path) VALUES (702, 'b.mp4', 'b.mp4')"
            )
            conn.execute(
                "INSERT INTO media_group_items (media_group_id, file_record_id, role, sort_order)"
                " VALUES (850, 702, 'video', 1)"
            )
            conn.commit()
        # A retry group carries the same files in the opposite order.
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO media_groups (id, name, notes, status) VALUES (851, 'g6', '', 'ready')"
            )
            conn.executemany(
                "INSERT INTO media_group_items (media_group_id, file_record_id, role, sort_order)"
                " VALUES (851, ?, 'video', ?)",
                [(702, 0), (701, 1)],
            )
            conn.commit()
        self.assertTrue(
            publish_orchestrator._already_queued_for_media(
                127, 851, db_path=self.db_path
            ),
            "the same file set in any order must be treated as a duplicate",
        )


class PrepDoesNotRegressQueuedCampaignTests(unittest.TestCase):
    """A re-prep that queues nothing must not un-schedule a live campaign.

    Campaign 2599 (the SFW Taipei Pride invitation) was flipped to needs_review
    with "No publishable posts queued" by a re-prep, while all 13 of its posts sat
    queued and correctly scheduled for 2026-10-24. To an operator that reads as
    "this did not schedule" and invites a re-submit of a campaign that was fine.
    """

    def test_has_queued_posts_detects_a_queued_post(self):
        import sqlite3
        import tempfile
        from pathlib import Path

        import db.createTable as create_table
        from myUtils import campaigns as campaign_store
        from myUtils import profiles as profile_registry

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "c.db"
            create_table.bootstrap(db)
            profile_id = profile_registry.create_profile("P", db_path=db).id
            connection = sqlite3.connect(str(db))
            try:
                connection.execute(
                    "INSERT INTO media_groups (id, name, notes, status) "
                    "VALUES (1, 'g', '', 'ready')"
                )
                connection.commit()
            finally:
                connection.close()
            campaign = campaign_store.create_campaign(
                profile_id=profile_id, media_group_id=1, db_path=db
            )
            self.assertFalse(
                campaign_store.campaign_has_queued_posts(campaign.id, db_path=db)
            )
            post = campaign_store.add_campaign_post(
                campaign.id, "bluesky", account_ids=[1], db_path=db
            )
            campaign_store.update_campaign_post(
                post.id, status=campaign_store.CAMPAIGN_POST_QUEUED, db_path=db
            )
            self.assertTrue(
                campaign_store.campaign_has_queued_posts(campaign.id, db_path=db)
            )

    def test_terminal_posts_do_not_count_as_queued(self):
        import sqlite3
        import tempfile
        from pathlib import Path

        import db.createTable as create_table
        from myUtils import campaigns as campaign_store
        from myUtils import profiles as profile_registry

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "c.db"
            create_table.bootstrap(db)
            profile_id = profile_registry.create_profile("P", db_path=db).id
            connection = sqlite3.connect(str(db))
            try:
                connection.execute(
                    "INSERT INTO media_groups (id, name, notes, status) "
                    "VALUES (1, 'g', '', 'ready')"
                )
                connection.commit()
            finally:
                connection.close()
            campaign = campaign_store.create_campaign(
                profile_id=profile_id, media_group_id=1, db_path=db
            )
            post = campaign_store.add_campaign_post(
                campaign.id, "bluesky", account_ids=[1], db_path=db
            )
            campaign_store.update_campaign_post(
                post.id, status=campaign_store.CAMPAIGN_POST_FAILED, db_path=db
            )
            self.assertFalse(
                campaign_store.campaign_has_queued_posts(campaign.id, db_path=db)
            )


class SchedulerWakesForPrepTests(unittest.TestCase):
    """The scheduler must wake for a campaign awaiting prep, not only for a due post.

    drain() is what runs the prep tick (_prep_tick), and the scheduler only called
    drain() when has_claimable_targets() was true. So a campaign sitting in
    'preparing' was never prepped until some unrelated target happened to become
    due: a re-prep could idle for hours and a fresh submit whose accounts were all
    booked for a later slot looked stuck. Observed live on campaign 2503, which sat
    in 'preparing' with the worker completely silent.
    """

    def test_the_scheduler_checks_for_claimable_prep(self):
        import inspect

        import sau_backend

        source = inspect.getsource(sau_backend._publish_scheduler_loop)
        self.assertIn("has_claimable_preparing_campaigns", source)

    def test_a_preparing_campaign_with_no_due_target_still_wakes_the_worker(self):
        import inspect

        import sau_backend

        source = inspect.getsource(sau_backend._publish_scheduler_loop)
        # The prep check must be reachable even when no target is claimable.
        claimable = source.index("has_claimable_targets")
        prep = source.index("has_claimable_preparing_campaigns")
        self.assertGreater(prep, claimable)
