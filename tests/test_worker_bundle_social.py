"""Rules-first bundle.social worker hooks with real admission and SQLite leases.

Only the provider transport and codec probe are mocked. Registered, checksum-
backed campaign media must pass the worker's real prerequisites before the
fallback is allowed to submit to the exact permitted subreddit.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import db.createTable as create_table
from myUtils import bundle_social, jobs, media_compliance, profiles as profile_registry, worker
from myUtils.worker import PublishWorker
from tests.test_bundle_social import FakeResponse, FakeSession
from tests.worker_publish_fixtures import WORKSPACE_ID, campaign_media_payload, create_test_profile, native_config, record_native_proof, video_probe

SUBREDDIT = "BareMenPositivity"  # Known permitted registry entry; strict policy stays ON.


class BundleSocialRedditFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "worker.db"
        create_table.bootstrap(self.db_path)
        profile = create_test_profile(self.db_path, "Reddit Fallback")
        self.profile_id, self.workspace_id = profile.id, WORKSPACE_ID
        account = profile_registry.add_account(self.profile_id, platform="reddit", account_name="rd-test",
            auth_type="oauth", config={**native_config("reddit"), "subreddits": [SUBREDDIT], "flairId": "fixture-flair"},
            db_path=self.db_path)
        self.account_id = account.id
        self.assertTrue(record_native_proof(self.db_path, account)["ready"])
        self.media = Path(self._tmp.name) / "clip.mp4"
        self.media.write_bytes(b"exact-worker-video-fixture" * 100)
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        for key in ("SAU_REDDIT_DIRECT_PUBLISH", bundle_social.API_KEY_ENV, bundle_social.API_URL_ENV,
                    bundle_social.CAP_ENV, bundle_social.SUBS_PER_TARGET_ENV):
            os.environ.pop(key, None)
        os.environ[bundle_social.TEAM_ID_ENV] = "fixture-team"
        os.environ["SAU_SUBREDDIT_STRICT"] = "1"
        codec = patch.object(media_compliance.media_prep, "probe", side_effect=video_probe)
        codec.start()
        self.addCleanup(codec.stop)
        self.payload = campaign_media_payload(self.db_path, account, self.media,
            {"title": "A title", "message": "A title\nA body", "subreddits": [SUBREDDIT]})
        self.allow_videos, self.remaining = True, 20
        self.uploaded_bytes = None
        self.session = FakeSession(self._transport)
        transport = patch.object(bundle_social, "_session", side_effect=lambda supplied=None: supplied or self.session)
        transport.start()
        self.addCleanup(transport.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _transport(self, method, url, kwargs):
        path = urlparse(url).path
        if method == "GET" and path == "/api/v1/social-account/by-type":
            self.assertEqual(kwargs["params"], {"type": "REDDIT", "teamId": "fixture-team"})
            return FakeResponse(payload={"username": "offline-reddit-fixture", "channels": [
                {"username": f"r/{SUBREDDIT}", "name": SUBREDDIT,
                 "metadata": {"allowVideos": self.allow_videos, "allowImages": True}}]})
        if method == "GET" and path == "/api/v1/organization/usage/posts":
            return FakeResponse(payload={"used": 20 - self.remaining, "remaining": self.remaining, "limit": 20})
        if method == "GET" and path.startswith("/api/v1/post/reference-key/"):
            return FakeResponse(status_code=404, payload={})
        if method == "GET" and path == "/api/v1/misc/reddit/post-requirements":
            return FakeResponse(payload={"isFlairRequired": True, "isFlairEnabled": True})
        if method == "POST" and path == "/api/v1/upload/":
            name, handle, mime = kwargs["files"]["file"]
            self.assertEqual((name, mime), (self.media.name, "video/mp4"))
            self.uploaded_bytes = handle.read()
            self.assertEqual(hashlib.sha256(self.uploaded_bytes).hexdigest(), self.payload["artifacts"][0]["sha256"])
            self.assertEqual(len(self.uploaded_bytes), self.payload["artifacts"][0]["size_bytes"])
            return FakeResponse(payload={"id": "u1"})
        if method == "POST" and path == "/api/v1/post/":
            return FakeResponse(payload={"id": "p1", "status": "SCHEDULED"})
        if method == "GET" and path == "/api/v1/post/p1":
            return FakeResponse(payload={"id": "p1", "status": "POSTED"})
        raise AssertionError(f"Unexpected fixture {method} {path}")

    def _enqueue(self) -> int:
        job = jobs.enqueue_job(jobs.JobSpec(platform="reddit", payload=self.payload,
            targets=[(f"account:{self.account_id}", f"campaign_post:{self.payload['campaignPostId']}", None)],
            profile_id=self.profile_id, idempotency_key="bundle-social"),
            workspace_id=self.workspace_id, db_path=self.db_path)
        self.assertTrue(job.payload["publishReadiness"]["ready"])
        return job.id

    def _attempt(self, job_id):
        target = jobs.claim_next_targets(db_path=self.db_path, owner="bundle-fixture")[0]
        self.assertEqual(target.job_id, job_id)
        job = jobs.get_job(job_id, db_path=self.db_path)
        instance = PublishWorker(lambda *a: None, db_path=self.db_path)
        payload = instance._target_payload(target, job)
        instance._payloads[target.id] = payload

        async def attempt():
            self.assertIsNone(await instance._prepare_before_dispatch(target, job, payload))
            return await instance._try_bundle_social_fallback(target, "direct disabled", worker._logger)

        return asyncio.run(attempt()), target

    def _creates(self):
        return [call for call in self.session.calls if call[0] == "POST" and urlparse(call[1]).path == "/api/v1/post/"]

    def test_fallback_posts_to_a_video_channel_and_succeeds(self) -> None:
        os.environ[bundle_social.API_KEY_ENV] = "fixture-only"
        job_id = self._enqueue()
        delivered, target = self._attempt(job_id)
        self.assertTrue(delivered)
        final = jobs.get_target(target.id, db_path=self.db_path)
        self.assertEqual(final.status, jobs.TARGET_SUCCEEDED)
        self.assertTrue(final.checkpoint["readiness"]["ready"])
        self.assertEqual(len(self._creates()), 1)
        body = self._creates()[0][2]["json"]
        self.assertEqual(body["data"]["REDDIT"]["sr"], f"r/{SUBREDDIT}")
        self.assertEqual(body["data"]["REDDIT"]["text"], "A title")
        self.assertEqual(body["data"]["REDDIT"]["uploadIds"], ["u1"])
        self.assertEqual(body["referenceKey"], bundle_social.reference_key(target.id, SUBREDDIT))
        self.assertEqual(self.uploaded_bytes, self.media.read_bytes())

    def test_fallback_never_submits_video_to_a_video_less_channel(self) -> None:
        os.environ[bundle_social.API_KEY_ENV] = "fixture-only"
        self.allow_videos = False
        delivered, _target = self._attempt(self._enqueue())
        self.assertFalse(delivered)
        self.assertEqual(self._creates(), [])
        self.assertIsNone(self.uploaded_bytes)

    def test_fallback_stops_when_the_monthly_quota_is_reached(self) -> None:
        os.environ[bundle_social.API_KEY_ENV] = "fixture-only"
        self.remaining = 0
        delivered, _target = self._attempt(self._enqueue())
        self.assertFalse(delivered)
        self.assertEqual(self._creates(), [])
        self.assertIsNone(self.uploaded_bytes)

    def test_fallback_is_off_without_a_key(self) -> None:
        delivered, _target = self._attempt(self._enqueue())
        self.assertFalse(delivered)
        self.assertEqual(self.session.calls, [])


class RedditRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("SAU_REDDIT_DIRECT_PUBLISH", None)
        os.environ.pop(bundle_social.API_KEY_ENV, None)

    def tearDown(self) -> None:
        self._env.stop()

    def test_direct_path_is_skipped_when_bundle_social_is_configured(self) -> None:
        os.environ[bundle_social.API_KEY_ENV] = "fixture-only"
        self.assertFalse(worker._reddit_direct_publish_enabled())
        os.environ.pop(bundle_social.API_KEY_ENV)
        self.assertTrue(worker._reddit_direct_publish_enabled())

    def test_direct_path_can_be_forced_back_on(self) -> None:
        os.environ["SAU_REDDIT_DIRECT_PUBLISH"] = "1"
        os.environ[bundle_social.API_KEY_ENV] = "fixture-only"
        self.assertTrue(worker._reddit_direct_publish_enabled())
