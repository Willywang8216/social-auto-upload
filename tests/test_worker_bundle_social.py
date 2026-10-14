"""Tests for the bundle.social Reddit fallback hook in the publish worker.

The direct Reddit path is dead (Reddit revoked the account credentials), so a
Reddit target is handed to bundle.social instead. These tests pin the wiring and
the rule refusals:

* the fallback posts only to a subreddit whose channel accepts the media kind;
* a video-less subreddit is never submitted;
* the monthly post quota is respected and a quota stop is not a crash;
* a successful fallback transitions the target to ``succeeded``.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import db.createTable as create_table
from myUtils import bundle_social
from myUtils import jobs
from myUtils import profiles as profile_registry
from myUtils import worker
from myUtils.worker import PublishWorker, WorkerConfig


def _channels(**metadata_by_sub):
    return {
        name.lower(): {"username": f"r/{name}", "name": name, "metadata": md}
        for name, md in metadata_by_sub.items()
    }


class BundleSocialRedditFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "worker.db"
        create_table.bootstrap(self.db_path)
        self.profile_id = profile_registry.create_profile(
            "Reddit Fallback", db_path=self.db_path
        ).id
        self.account_id = profile_registry.add_account(
            self.profile_id,
            platform="reddit",
            account_name="rd-test",
            auth_type="oauth",
            config={"subreddits": ["Body"]},
            db_path=self.db_path,
        ).id
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for key in ("SAU_REDDIT_DIRECT_PUBLISH", "BUNDLE_SOCIAL_API_KEY"):
            os.environ.pop(key, None)
        # These tests use a synthetic subreddit; the registry's unknown-subreddit
        # guard is covered by its own tests, so relax it here to isolate the
        # media-kind rule under test.
        os.environ["SAU_SUBREDDIT_STRICT"] = "0"

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _enqueue(self) -> int:
        job = jobs.enqueue_job(
            jobs.JobSpec(
                platform="reddit",
                payload={
                    "campaignId": 1,
                    "campaignPostId": 1,
                    "draft": {"title": "A title", "message": "A title\nA body", "subreddits": ["Body"]},
                    "artifacts": [
                        {
                            "local_path": str(Path(self._tmp.name) / "clip.mp4"),
                            "artifact_kind": "video",
                        }
                    ],
                },
                targets=[(f"account:{self.account_id}", "campaign_post:1", None)],
                profile_id=self.profile_id,
                idempotency_key="bundle-social",
            ),
            db_path=self.db_path,
        )
        return job.id

    def _status(self, job_id: int) -> str:
        return jobs.list_targets(job_id, db_path=self.db_path)[0].status

    def _running_target(self, job_id: int):
        """Claim the target the way the worker does before it runs."""
        target = jobs.list_targets(job_id, db_path=self.db_path)[0]
        con = sqlite3.connect(self.db_path)
        con.execute(
            "UPDATE publish_job_targets SET status='running', attempts=1 WHERE id=?",
            (target.id,),
        )
        con.commit()
        con.close()
        return jobs.list_targets(job_id, db_path=self.db_path)[0]

    def test_fallback_posts_to_a_video_channel_and_succeeds(self) -> None:
        job_id = self._enqueue()
        target = self._running_target(job_id)
        instance = PublishWorker(lambda *a, **k: None, db_path=self.db_path)
        with patch.object(bundle_social, "is_enabled", return_value=True), patch.object(
            worker, "_ensure_artifact_paths_local", return_value=None
        ), patch.object(
            bundle_social, "get_reddit_account", return_value={"username": "u"}
        ), patch.object(
            bundle_social,
            "reddit_channels",
            return_value=_channels(Body={"allowVideos": True, "allowImages": True}),
        ), patch.object(
            bundle_social, "remaining_posts", return_value=20
        ), patch.object(
            bundle_social,
            "publish_reddit_post",
            return_value={"post_id": "p1", "subreddit": "Body", "upload_ids": ["u1"]},
        ) as publisher:
            delivered = asyncio.run(
                instance._try_bundle_social_fallback(target, "direct disabled", MagicMock())
            )
        self.assertTrue(delivered)
        self.assertEqual(self._status(job_id), "succeeded")
        kwargs = publisher.call_args.kwargs
        self.assertEqual(kwargs["subreddit"], "Body")
        self.assertEqual(kwargs["media_kind"], "video")
        self.assertEqual(kwargs["title"], "A title")

    def test_fallback_never_submits_video_to_a_video_less_channel(self) -> None:
        job_id = self._enqueue()
        target = jobs.list_targets(job_id, db_path=self.db_path)[0]
        instance = PublishWorker(lambda *a, **k: None, db_path=self.db_path)
        with patch.object(bundle_social, "is_enabled", return_value=True), patch.object(
            worker, "_ensure_artifact_paths_local", return_value=None
        ), patch.object(
            bundle_social, "get_reddit_account", return_value={"username": "u"}
        ), patch.object(
            bundle_social,
            "reddit_channels",
            return_value=_channels(Body={"allowVideos": False, "allowImages": True}),
        ), patch.object(
            bundle_social, "remaining_posts", return_value=20
        ), patch.object(
            bundle_social, "publish_reddit_post"
        ) as publisher:
            delivered = asyncio.run(
                instance._try_bundle_social_fallback(target, "direct disabled", MagicMock())
            )
        self.assertFalse(delivered)
        publisher.assert_not_called()

    def test_fallback_stops_when_the_monthly_quota_is_reached(self) -> None:
        job_id = self._enqueue()
        target = jobs.list_targets(job_id, db_path=self.db_path)[0]
        instance = PublishWorker(lambda *a, **k: None, db_path=self.db_path)
        with patch.object(bundle_social, "is_enabled", return_value=True), patch.object(
            worker, "_ensure_artifact_paths_local", return_value=None
        ), patch.object(
            bundle_social, "get_reddit_account", return_value={"username": "u"}
        ), patch.object(
            bundle_social,
            "reddit_channels",
            return_value=_channels(Body={"allowVideos": True}),
        ), patch.object(
            bundle_social, "remaining_posts", return_value=0
        ), patch.object(
            bundle_social, "publish_reddit_post"
        ) as publisher:
            delivered = asyncio.run(
                instance._try_bundle_social_fallback(target, "direct disabled", MagicMock())
            )
        self.assertFalse(delivered)
        publisher.assert_not_called()

    def test_fallback_is_off_without_a_key(self) -> None:
        job_id = self._enqueue()
        target = jobs.list_targets(job_id, db_path=self.db_path)[0]
        instance = PublishWorker(lambda *a, **k: None, db_path=self.db_path)
        with patch.object(bundle_social, "is_enabled", return_value=False):
            self.assertFalse(
                asyncio.run(
                    instance._try_bundle_social_fallback(target, "direct disabled", MagicMock())
                )
            )


class RedditRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("SAU_REDDIT_DIRECT_PUBLISH", None)

    def tearDown(self) -> None:
        self._env.stop()

    def test_direct_path_is_skipped_when_bundle_social_is_configured(self) -> None:
        with patch.object(bundle_social, "is_enabled", return_value=True):
            self.assertFalse(worker._reddit_direct_publish_enabled())
        with patch.object(bundle_social, "is_enabled", return_value=False):
            self.assertTrue(worker._reddit_direct_publish_enabled())

    def test_direct_path_can_be_forced_back_on(self) -> None:
        os.environ["SAU_REDDIT_DIRECT_PUBLISH"] = "1"
        with patch.object(bundle_social, "is_enabled", return_value=True):
            self.assertTrue(worker._reddit_direct_publish_enabled())
