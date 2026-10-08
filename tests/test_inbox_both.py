"""Tests for the inbox "both" (nw=1 + sw=3) publish path.

These hit the pure ``_inbox_publish_payload`` mapping helper and the Flask
route wiring (via a small isolated ``Flask.app`` that registers the helpers so
the routes' closure over the module globals resolves). They do not touch a
real DB or start jobs.
"""

from __future__ import annotations

import importlib.util
import os
import unittest
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


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class InboxPublishRouteTest(unittest.TestCase):
    def test_route_registered(self):
        from sau_backend import app

        rules = {r.rule for r in app.url_map.iter_rules()}
        self.assertIn("/api/inbox/items/<string:item_id>/publish", rules)

    def test_publish_endpoint_maps_both_profiles(self):
        from sau_backend import app

        captured = {}

        def fake_submit_publish(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(campaign_ids=[10, 11], jobs=[{"id": 1}, {"id": 2}])

        with patch("sau_backend.publish_orchestrator.submit_publish", side_effect=fake_submit_publish):
            client = app.test_client()
            from myUtils.security import SecurityPolicy
            original_policy = app.config["SECURITY_POLICY"]
            app.config["SECURITY_POLICY"] = SecurityPolicy(tokens=frozenset(), cors_origins=("http://localhost:5173",))
            with patch("myUtils.inbox_ops.list_items") as listed, \
                    patch("myUtils.inbox_ops.approve") as approved, \
                    patch("sau_backend._start_worker_drain_thread") as drained, \
                    patch("myUtils.inbox_drive.remote_path_for_item", return_value="both/video/sfw-demo.mp4"), \
                    patch("myUtils.inbox_drive.stage_remote_media", return_value="_inbox_cache/demo.mp4"):
                listed.return_value = {"ready": [dict(BOTH_ITEM)], "pending": [], "quarantined": []}
                approved.return_value = dict(BOTH_ITEM)
                resp = client.post("/api/inbox/items/item-both-1/publish", json={})

        self.assertEqual(resp.status_code, 200, resp.get_json())
        payload = resp.get_json()
        self.assertEqual(payload["code"], 200)
        self.assertEqual(payload["data"]["campaignIds"], [10, 11])
        self.assertEqual(payload["data"]["jobs"], [{"id": 1}, {"id": 2}])
        self.assertEqual(captured["profile_ids"], [1, 3])
        self.assertEqual(len(captured["media_file_paths"]), 1)
        self.assertTrue(captured["media_file_paths"][0].endswith("/_inbox_cache/demo.mp4"))
        self.assertIsNone(captured["schedule"])
        drained.assert_called_once()
        app.config["SECURITY_POLICY"] = original_policy

    def test_publish_endpoint_missing_item_404(self):
        from sau_backend import app

        client = app.test_client()
        from myUtils.security import SecurityPolicy
        original_policy = app.config["SECURITY_POLICY"]
        app.config["SECURITY_POLICY"] = SecurityPolicy(tokens=frozenset(), cors_origins=("http://localhost:5173",))
        with patch("myUtils.inbox_ops.list_items") as listed:
            listed.return_value = {"ready": [], "pending": [], "quarantined": []}
            resp = client.post("/api/inbox/items/nope/publish", json={})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["code"], 404)
        app.config["SECURITY_POLICY"] = original_policy

    def test_publish_endpoint_starts_drain_when_campaign_has_no_jobs(self):
        # Regression for async prep: the old ``if result.jobs`` guard skipped
        # the drain when a campaign was created with no jobs yet, so prep was
        # never picked up. ``campaignIds`` must also arm the drain.
        from sau_backend import app

        def fake_submit_publish(**_kwargs):
            return SimpleNamespace(campaign_ids=[10], jobs=[])

        with patch("sau_backend.publish_orchestrator.submit_publish", side_effect=fake_submit_publish):
            client = app.test_client()
            from myUtils.security import SecurityPolicy
            original_policy = app.config["SECURITY_POLICY"]
            app.config["SECURITY_POLICY"] = SecurityPolicy(tokens=frozenset(), cors_origins=("http://localhost:5173",))
            with patch("myUtils.inbox_ops.list_items") as listed, \
                    patch("sau_backend._start_worker_drain_thread") as drained, \
                    patch("myUtils.inbox_drive.remote_path_for_item", return_value="both/video/sfw-demo.mp4"), \
                    patch("myUtils.inbox_drive.stage_remote_media", return_value="_inbox_cache/demo.mp4"):
                listed.return_value = {"ready": [dict(BOTH_ITEM)], "pending": [], "quarantined": []}
                resp = client.post("/api/inbox/items/item-both-1/publish", json={})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.get_json()["data"]["jobs"], [])
        self.assertEqual(resp.get_json()["data"]["campaignIds"], [10])
        drained.assert_called_once()
        app.config["SECURITY_POLICY"] = original_policy

    def test_publish_endpoint_async_prep_returns_preparing(self):
        from sau_backend import app

        def fake_async_submit(**_kwargs):
            return SimpleNamespace(campaign_ids=[7], jobs=[], skipped=[])

        with patch.dict(os.environ, {"SAU_ASYNC_PREP": "1"}), \
                patch("sau_backend.campaign_prep.submit_publish_async", side_effect=fake_async_submit), \
                patch("sau_backend.publish_orchestrator.submit_publish") as sync_submit:
            client = app.test_client()
            from myUtils.security import SecurityPolicy
            original_policy = app.config["SECURITY_POLICY"]
            app.config["SECURITY_POLICY"] = SecurityPolicy(tokens=frozenset(), cors_origins=("http://localhost:5173",))
            with patch("myUtils.inbox_ops.list_items") as listed, \
                    patch("sau_backend._start_worker_drain_thread") as drained, \
                    patch("myUtils.inbox_drive.remote_path_for_item", return_value="both/video/sfw-demo.mp4"), \
                    patch("myUtils.inbox_drive.stage_remote_media", return_value="_inbox_cache/demo.mp4"):
                listed.return_value = {"ready": [dict(BOTH_ITEM)], "pending": [], "quarantined": []}
                resp = client.post("/api/inbox/items/item-both-1/publish", json={})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        data = resp.get_json()["data"]
        self.assertEqual(data["status"], "preparing")
        self.assertEqual(data["jobs"], [])
        self.assertEqual(data["campaignIds"], [7])
        sync_submit.assert_not_called()
        drained.assert_called_once()
        app.config["SECURITY_POLICY"] = original_policy


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class InboxPublishStagingCleanupTest(unittest.TestCase):
    """The staged Drive source must survive an async submit.

    Once prep is asynchronous ``submit_publish`` returns ``jobs=[]`` while the
    campaign is still ``preparing``. The cleanup must key on "was a campaign
    created?", never on "were jobs returned?", otherwise the only local copy
    is deleted before prep ever reads it (silent data loss).
    """

    def _post_publish(self, *, submit_result=None, submit_side_effect=None):
        import tempfile
        from pathlib import Path

        from sau_backend import app
        import sau_backend

        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        staged_rel = "_inbox_cache/demo.mp4"
        staged_abs = Path(tmpdir.name) / "videoFile" / staged_rel
        staged_abs.parent.mkdir(parents=True, exist_ok=True)
        staged_abs.write_bytes(b"staged-media")

        submit_kwargs = (
            {"side_effect": submit_side_effect}
            if submit_side_effect is not None
            else {"return_value": submit_result}
        )

        client = app.test_client()
        from myUtils.security import SecurityPolicy
        original_policy = app.config["SECURITY_POLICY"]
        app.config["SECURITY_POLICY"] = SecurityPolicy(
            tokens=frozenset(), cors_origins=("http://localhost:5173",)
        )
        try:
            with patch.object(sau_backend, "BASE_DIR", tmpdir.name), \
                    patch("sau_backend.publish_orchestrator.submit_publish", **submit_kwargs), \
                    patch("myUtils.inbox_ops.list_items") as listed, \
                    patch("sau_backend._start_worker_drain_thread"), \
                    patch("sau_backend._notify_tg_review"), \
                    patch("myUtils.inbox_drive.remote_path_for_item",
                          return_value="both/video/sfw-demo.mp4"), \
                    patch("myUtils.inbox_drive.stage_remote_media",
                          return_value=staged_rel):
                listed.return_value = {
                    "ready": [dict(BOTH_ITEM)], "pending": [], "quarantined": []
                }
                resp = client.post("/api/inbox/items/item-both-1/publish", json={})
        finally:
            app.config["SECURITY_POLICY"] = original_policy
        return resp, staged_abs

    def test_async_submit_with_campaign_but_no_jobs_keeps_staged_file(self):
        resp, staged_abs = self._post_publish(
            submit_result=SimpleNamespace(campaign_ids=[42], jobs=[])
        )
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.get_json()["data"]["campaignIds"], [42])
        self.assertEqual(resp.get_json()["data"]["jobs"], [])
        self.assertTrue(
            staged_abs.exists(),
            "async submit (campaign created, jobs still empty) must not delete "
            "the staged source",
        )

    def test_failed_submit_without_campaign_discards_staged_file(self):
        resp, staged_abs = self._post_publish(
            submit_result=SimpleNamespace(campaign_ids=[], jobs=[], skipped=[])
        )
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertFalse(
            staged_abs.exists(),
            "a submission that created no campaign genuinely failed -> discard",
        )

    def test_lookup_error_after_orchestrator_invoked_keeps_staged_file(self):
        def boom(**_kwargs):
            raise LookupError("profile 3 not found")

        resp, staged_abs = self._post_publish(submit_side_effect=boom)
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(
            staged_abs.exists(),
            "a failure mid-orchestration may leave a campaign referencing the "
            "staged source, so it must not be deleted",
        )


from types import SimpleNamespace  # noqa: E402


if __name__ == "__main__":
    unittest.main()