"""Tests for the inbox "both" (nw=1 + sw=3) publish path.

The payload helper and real Flask admission route use a fresh synthetic DB.
Drive registration is local-only; downloads, preparation, and publishing never
run in the request. Prep failure coverage drives the Flask-free runner explicitly.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

flask_available = importlib.util.find_spec("flask") is not None

import myUtils.inbox_ops  # noqa: E402,F401
from sau_backend import _inbox_publish_payload  # noqa: E402


BOTH_ITEM = {
    "id": "item-both-1",
    "persona": "both",
    "profileIds": [1, 3],
    "topic": "math from two angles",
    "sfwFlag": True,
    "kind": "video",
    "sourcePath": "/app/sau-inbox/both/video/sfw-demo.mp4",
    "thumbPath": None,
    "brief": "Publish to both feed profiles.",
    "contentNote": None,
    "status": "ready",
    "createdAt": "2026-09-17T09:00:00+00:00",
}

FULL_ITEM = {
    "id": "item-full-2",
    "persona": "sw",
    "profileIds": [3],
    "topic": "sw short",
    "sfwFlag": True,
    "kind": "video",
    "sourcePath": "/app/sau-inbox/sw/video/short.mp4",
    "sourcePath": "/app/sau-inbox/sw/video/sfw-short.mp4",
    "brief": "",
    "contentNote": None,
    "status": "ready",
    "createdAt": "2026-09-17T09:01:00+00:00",
}


class InboxPublishPayloadTest(unittest.TestCase):
    def test_both_item_maps_profile_ids_1_and_3(self):
        profile_ids, media_paths, brief, schedule = _inbox_publish_payload(BOTH_ITEM)
        self.assertEqual(profile_ids, [1, 3])
        self.assertIsInstance(profile_ids[0], int)

    def test_body_schedule_and_brief_override_flow_through(self):
        body = {
            "brief": "Operator override brief.",
            "schedule": {"publishNow": False, "startAt": "2026-09-18T10:00:00"},
        }
        profile_ids, media_paths, brief, schedule = _inbox_publish_payload(BOTH_ITEM, body)
        self.assertEqual(profile_ids, [1, 3])
        self.assertEqual(brief, "Operator override brief.")
        self.assertIs(schedule["publishNow"], False)
        self.assertEqual(schedule["startAt"], "2026-09-18T10:00:00")
        self.assertEqual(len(media_paths), 1)

    def test_media_path_passthrough_and_resolver_precedence(self):
        # Absolute mounted inbox path passes straight through (no resolver).
        profile_ids, media_paths, brief, schedule = _inbox_publish_payload(BOTH_ITEM)
        self.assertEqual(media_paths, ["/app/sau-inbox/both/video/sfw-demo.mp4"])

        # An explicit resolver that succeeds wins over the raw path.
        profile_ids, media_paths, brief, schedule = _inbox_publish_payload(
            FULL_ITEM, resolve_video_file_path=lambda p: "/srv/media/sfw-short.mp4"
        )
        self.assertEqual(profile_ids, [3])
        self.assertEqual(media_paths, ["/srv/media/sfw-short.mp4"])

        # A resolver that fails (None) falls back to the raw absolute path.
        profile_ids, media_paths, brief, schedule = _inbox_publish_payload(
            FULL_ITEM, resolve_video_file_path=lambda p: None
        )
        self.assertEqual(media_paths, ["/app/sau-inbox/sw/video/sfw-short.mp4"])

    def test_default_schedule_is_none_immediate(self):
        profile_ids, media_paths, brief, schedule = _inbox_publish_payload(BOTH_ITEM)
        self.assertIsNone(schedule)


class _InboxPublishTestCase(unittest.TestCase):
    def setUp(self):
        import db.createTable as create_table
        import sau_backend
        from myUtils import profiles
        from myUtils.security import SecurityPolicy

        self.sau_backend = sau_backend
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base_dir = Path(self._tmp.name)
        self.db_path = self.base_dir / "db" / "database.db"
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.dict(os.environ, {
            "SAU_DB_PATH": str(self.db_path),
            "SAU_INBOX_DRIVE_REMOTE": "test-drive", "SAU_INBOX_DRIVE_ROOT": "Test-Inbox",
        }))
        self.patches.enter_context(patch.object(sau_backend, "BASE_DIR", self.base_dir))
        self.patches.enter_context(patch("myUtils.campaign_media_prep.BASE_DIR", self.base_dir))
        create_table.bootstrap(self.db_path)
        nw = profiles.create_profile("NW", db_path=self.db_path)
        profiles.create_profile("Unused", db_path=self.db_path)
        sw = profiles.create_profile("SW", db_path=self.db_path)
        self.accounts = [
            profiles.add_account(nw.id, "telegram", "nw", auth_type="manual", db_path=self.db_path),
            profiles.add_account(sw.id, "instagram", "sw", auth_type="manual", db_path=self.db_path),
        ]
        self.item = dict(BOTH_ITEM, profileIds=[nw.id, sw.id])
        self.patches.enter_context(patch.dict(sau_backend.app.config, {
            "SECURITY_POLICY": SecurityPolicy(
                tokens=frozenset(), cors_origins=("http://localhost:5173",),
            ),
            "TESTING": True,
        }))
        self.client = sau_backend.app.test_client()
        self.listed = self.patches.enter_context(patch("myUtils.inbox_ops.list_items", return_value={
            "ready": [self.item], "pending": [], "quarantined": [],
        }))
        self.staged = self.patches.enter_context(patch(
            "myUtils.inbox_drive.stage_remote_media",
            side_effect=AssertionError("HTTP must not stage Drive media"),
        ))
        self.downloaded = self.patches.enter_context(patch(
            "myUtils.campaign_media_prep._download_file_from_storage",
            side_effect=AssertionError("HTTP must not download media"),
        ))
        self.prepared = self.patches.enter_context(patch(
            "myUtils.publish_services._prepare_campaign_media_artifacts",
            side_effect=AssertionError("HTTP must not prepare media"),
        ))
        self.drafted = self.patches.enter_context(patch("myUtils.publish_services._generate_account_draft"))
        self.notified = self.patches.enter_context(patch("myUtils.publish_services._notify_tg_review"))
        self.drained = self.patches.enter_context(patch("sau_backend._start_worker_drain_thread"))

    def _post(self, **body):
        return self.client.post(f"/api/inbox/items/{self.item['id']}/publish", json=body)

    def _rows(self, table):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def _assert_no_request_work(self):
        self.staged.assert_not_called()
        self.downloaded.assert_not_called()
        self.prepared.assert_not_called()
        self.drafted.assert_not_called()
        self.notified.assert_not_called()
        for table in ("publish_jobs", "publish_job_targets", "campaign_posts"):
            self.assertEqual(self._rows(table), [], table)

    def _assert_accepted(self, response):
        from myUtils import campaigns

        self.assertEqual(response.status_code, 202, response.get_json())
        self.assertEqual(response.get_json()["code"], 200)
        data = response.get_json()["data"]
        self.assertEqual(data["status"], "preparing")
        self.assertEqual(data["jobs"], [])
        self.assertEqual(data["skipped"], [])
        self.assertEqual(data["submissionId"], f"inbox:{self.item['id']}")
        self.assertEqual(len(data["campaignIds"]), 2)
        self._assert_no_request_work()
        for campaign_id, account in zip(data["campaignIds"], self.accounts):
            campaign = campaigns.get_campaign(campaign_id, db_path=self.db_path)
            self.assertEqual(campaign.status, campaigns.CAMPAIGN_PREPARING)
            self.assertEqual(campaign.profile_id, account.profile_id)
            self.assertEqual(campaign.selected_account_ids, [account.id])
            request = campaigns.get_prep_request(campaign)
            self.assertEqual(request["contentRating"], "sfw")
            self.assertEqual(request["source"], "inbox")
            self.assertEqual(request["submissionId"], data["submissionId"])
        return data

    def _cached_source(self):
        from myUtils import inbox_drive

        local_ref = inbox_drive.register_remote_source(
            "both/video/sfw-demo.mp4", db_path=self.db_path,
        )
        source = self.base_dir / "videoFile" / local_ref
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"previously cached synthetic media")
        return source


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class InboxPublishRouteTest(_InboxPublishTestCase):
    def test_route_registered(self):
        rules = {r.rule for r in self.sau_backend.app.url_map.iter_rules()}
        self.assertIn("/api/inbox/items/<string:item_id>/publish", rules)

    def test_publish_endpoint_maps_both_profiles(self):
        from myUtils import campaigns, publish_services

        with patch.object(publish_services, "submit_publish", wraps=publish_services.submit_publish) as submit:
            resp = self._post()
        data = self._assert_accepted(resp)
        self.assertEqual(submit.call_args.kwargs["profile_ids"], [1, 3])
        self.assertEqual(submit.call_args.kwargs["db_path"], self.db_path)
        self.assertEqual(submit.call_args.kwargs["source"], "inbox")
        self.assertIsNone(submit.call_args.kwargs["schedule"])
        files = self._rows("file_records")
        self.assertEqual(len(files), 1)
        identity = hashlib.sha256(b"test-drive:Test-Inbox/both/video/sfw-demo.mp4\0").hexdigest()
        local_ref = f"_inbox_cache/{identity}/sfw-demo.mp4"
        self.assertEqual(submit.call_args.kwargs["media_file_paths"], [local_ref])
        self.assertEqual(files[0]["file_path"], local_ref)
        self.assertEqual(files[0]["filename"], "sfw-demo.mp4")
        self.assertEqual(files[0]["storage_key"], "both/video/sfw-demo.mp4")
        self.assertIsNotNone(files[0]["storage_backend_id"])
        self.assertFalse((self.base_dir / "videoFile" / local_ref).exists())
        for campaign_id in data["campaignIds"]:
            campaign = campaigns.get_campaign(campaign_id, db_path=self.db_path)
            self.assertEqual(campaigns.get_prep_request(campaign)["mediaFilePaths"], [local_ref])
        self.drained.assert_called_once()

    def test_publish_endpoint_missing_item_404(self):
        self.listed.return_value = {"ready": [], "pending": [], "quarantined": []}
        resp = self.client.post("/api/inbox/items/nope/publish", json={})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["code"], 404)
        self.assertEqual(self._rows("file_records"), [])
        self.assertEqual(self._rows("campaigns"), [])
        self._assert_no_request_work()
        self.drained.assert_not_called()

    def test_publish_endpoint_starts_drain_when_campaign_has_no_jobs(self):
        self._assert_accepted(self._post())
        self.drained.assert_called_once()

    def test_publish_endpoint_async_prep_returns_preparing(self):
        with patch.dict(os.environ, {"SAU_ASYNC_PREP": "1"}):
            self._assert_accepted(self._post())
        self.drained.assert_called_once()

    def test_retry_reuses_inbox_submission_and_registered_source(self):
        first = self._post()
        self._assert_accepted(first)
        tables = ("file_records", "storage_backends", "media_groups", "media_group_items",
                  "campaigns", "publish_submissions")
        before = {table: self._rows(table) for table in tables}
        second = self._post()
        self._assert_accepted(second)
        self.assertEqual(second.get_json(), first.get_json())
        self.assertEqual({table: self._rows(table) for table in tables}, before)

    def test_retry_with_changed_intent_returns_conflict(self):
        self._assert_accepted(self._post())
        tables = ("file_records", "media_groups", "campaigns", "publish_submissions")
        before = {table: self._rows(table) for table in tables}
        response = self._post(brief="Different operator intent")
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()["code"], 409)
        self.assertEqual({table: self._rows(table) for table in tables}, before)
        self._assert_no_request_work()


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class InboxPublishStagingCleanupTest(_InboxPublishTestCase):
    """Admission preserves remote references/cached bytes; staging is worker work."""

    def test_async_submit_with_campaign_but_no_jobs_keeps_staged_file(self):
        staged_abs = self._cached_source()
        self._assert_accepted(self._post())
        self.assertEqual(staged_abs.read_bytes(), b"previously cached synthetic media")
        self.assertEqual(len(self._rows("file_records")), 1)

    def test_remote_download_failure_is_deferred_until_preparation(self):
        from flask import has_app_context, has_request_context
        from myUtils import campaign_prep, campaigns, publish_services
        from myUtils.inbox_drive import InboxDriveError

        response = self._post()
        data = self._assert_accepted(response)
        registered = self._rows("file_records")
        self.assertEqual(len(registered), 1)
        self.assertFalse((self.base_dir / "videoFile" / registered[0]["file_path"]).exists())
        campaign = campaigns.claim_next_preparing_campaign(owner="test-inbox", db_path=self.db_path)
        self.assertEqual(campaign.id, data["campaignIds"][0])
        self.assertFalse(has_app_context())
        self.assertFalse(has_request_context())
        failure = InboxDriveError("synthetic remote source is temporarily unavailable")
        with patch.object(publish_services, "_prepare_campaign_media_artifacts", side_effect=failure) as prep:
            with self.assertRaisesRegex(InboxDriveError, "temporarily unavailable"):
                campaign_prep.make_default_prep_runner()(campaign, self.db_path)
        prep.assert_called_once()
        self.assertEqual(prep.call_args.args[2][0]["file_path"], registered[0]["file_path"])
        # Apply the runner caller's failure transition, without starting a worker.
        self.assertTrue(campaigns.finish_campaign_prep(
            campaign.id, owner="test-inbox", status=campaigns.CAMPAIGN_NEEDS_REVIEW,
            last_error=str(failure), db_path=self.db_path,
        ))
        failed = campaigns.get_campaign(campaign.id, db_path=self.db_path)
        self.assertEqual(failed.status, campaigns.CAMPAIGN_NEEDS_REVIEW)
        self.assertIn("temporarily unavailable", failed.last_error)
        self.assertEqual(self._rows("file_records"), registered)
        self._assert_no_request_work()

    def test_invalid_profile_creates_no_campaign_and_keeps_cached_source(self):
        staged_abs = self._cached_source()
        registered = self._rows("file_records")
        response = self._post(profileIds=[1, 99999])
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertIn("Profile not found", response.get_json()["msg"])
        self.assertEqual(self._rows("campaigns"), [])
        self.assertEqual(self._rows("media_groups"), [])
        self.assertEqual(self._rows("publish_submissions"), [])
        self.assertEqual(self._rows("file_records"), registered)
        self.assertTrue(staged_abs.exists())
        self._assert_no_request_work()
        self.drained.assert_not_called()


if __name__ == "__main__":
    unittest.main()