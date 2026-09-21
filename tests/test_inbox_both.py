"""Tests for the inbox "both" (nw=1 + sw=3) publish path.

These hit the pure ``_inbox_publish_payload`` mapping helper and the Flask
route wiring (via a small isolated ``Flask.app`` that registers the helpers so
the routes' closure over the module globals resolves). They do not touch a
real DB or start jobs.
"""

from __future__ import annotations

import importlib.util
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
            with patch("myUtils.inbox_ops.list_items") as listed, \
                    patch("myUtils.inbox_ops.approve") as approved, \
                    patch("sau_backend._start_worker_drain_thread") as drained:
                listed.return_value = {"ready": [dict(BOTH_ITEM)], "pending": [], "quarantined": []}
                approved.return_value = dict(BOTH_ITEM)
                resp = client.post("/api/inbox/items/item-both-1/publish", json={})

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["code"], 200)
        self.assertEqual(payload["data"]["campaignIds"], [10, 11])
        self.assertEqual(payload["data"]["jobs"], [{"id": 1}, {"id": 2}])
        self.assertEqual(captured["profile_ids"], [1, 3])
        self.assertEqual(captured["media_file_paths"], ["/app/sau-inbox/both/video/sfw-demo.mp4"])
        self.assertIsNone(captured["schedule"])
        drained.assert_called_once()

    def test_publish_endpoint_missing_item_404(self):
        from sau_backend import app

        client = app.test_client()
        with patch("myUtils.inbox_ops.list_items") as listed:
            listed.return_value = {"ready": [], "pending": [], "quarantined": []}
            resp = client.post("/api/inbox/items/nope/publish", json={})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["code"], 404)


from types import SimpleNamespace  # noqa: E402


if __name__ == "__main__":
    unittest.main()