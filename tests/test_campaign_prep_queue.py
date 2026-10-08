"""Tests for the async campaign prep queue.

Covers the four behaviours the queue must guarantee:

* a ``preparing`` campaign can be claimed exactly once (no double-claim);
* a stale lease is recovered (so a dead worker cannot strand a campaign);
* a prep failure lands the campaign in ``needs_review``;
* the extracted runner prepares artifacts before generating drafts.

Spec: ``logs/async-prep-queue-notes.md``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db.createTable as create_table

if "conf" not in sys.modules:
    conf_module = types.ModuleType("conf")
    conf_module.BASE_DIR = str(Path(__file__).resolve().parent.parent)
    conf_module.DEBUG_MODE = True
    conf_module.LOCAL_CHROME_HEADLESS = True
    conf_module.LOCAL_CHROME_PATH = ""
    sys.modules["conf"] = conf_module

from myUtils import campaign_prep, campaigns, jobs, media_groups, profiles
from myUtils.worker import PublishWorker, WorkerConfig


def _iso_minutes_ago(minutes: int) -> str:
    return (
        datetime.now(tz=timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes)
    ).isoformat(timespec="seconds")


class PrepQueuePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "prep.db"
        create_table.bootstrap(self.db_path)
        self.profile = profiles.create_profile("Brand", db_path=self.db_path)
        self.media_group = media_groups.create_media_group(
            "Launch", db_path=self.db_path
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _preparing_campaign(self, *, metadata: dict | None = None) -> campaigns.Campaign:
        return campaigns.create_campaign(
            self.profile.id,
            self.media_group.id,
            status=campaigns.CAMPAIGN_PREPARING,
            metadata=metadata if metadata is not None else {"source": "test"},
            db_path=self.db_path,
        )

    def _force_lease(self, campaign_id: int, *, owner: str, minutes_ago: int, attempts: int | None = None) -> None:
        campaign = campaigns.get_campaign(campaign_id, db_path=self.db_path)
        metadata = dict(campaign.metadata or {})
        metadata[campaigns.PREP_LEASE_KEY] = {
            "owner": owner,
            "claimedAt": _iso_minutes_ago(minutes_ago),
        }
        if attempts is not None:
            metadata[campaigns.PREP_ATTEMPTS_KEY] = attempts
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE campaigns SET metadata_json = ? WHERE id = ?",
                (json.dumps(metadata, ensure_ascii=False), campaign_id),
            )
            conn.commit()

    def test_claim_records_lease_and_attempt(self) -> None:
        campaign = self._preparing_campaign()
        claimed = campaigns.claim_next_preparing_campaign(
            owner="worker-a", db_path=self.db_path
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, campaign.id)
        lease = claimed.metadata[campaigns.PREP_LEASE_KEY]
        self.assertEqual(lease["owner"], "worker-a")
        self.assertEqual(claimed.metadata[campaigns.PREP_ATTEMPTS_KEY], 1)

    def test_claim_does_not_double_claim(self) -> None:
        self._preparing_campaign()
        first = campaigns.claim_next_preparing_campaign(
            owner="worker-a", db_path=self.db_path
        )
        second = campaigns.claim_next_preparing_campaign(
            owner="worker-b", db_path=self.db_path
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second, "a live lease must not be handed to a second worker")

    def test_stale_lease_is_reclaimable_immediately(self) -> None:
        campaign = self._preparing_campaign()
        self._force_lease(campaign.id, owner="dead-worker", minutes_ago=999)
        reclaimed = campaigns.claim_next_preparing_campaign(
            owner="worker-b", db_path=self.db_path
        )
        self.assertIsNotNone(reclaimed)
        self.assertEqual(
            reclaimed.metadata[campaigns.PREP_LEASE_KEY]["owner"], "worker-b"
        )
        self.assertEqual(reclaimed.metadata[campaigns.PREP_ATTEMPTS_KEY], 1)

    def test_requeue_stale_preparing_releases_lease(self) -> None:
        campaign = self._preparing_campaign()
        self._force_lease(campaign.id, owner="dead-worker", minutes_ago=999)
        moved = campaigns.requeue_stale_preparing(
            older_than_minutes=120, max_attempts=3, db_path=self.db_path
        )
        self.assertEqual(moved, 1)
        refreshed = campaigns.get_campaign(campaign.id, db_path=self.db_path)
        self.assertNotIn(campaigns.PREP_LEASE_KEY, refreshed.metadata)
        self.assertEqual(refreshed.status, campaigns.CAMPAIGN_PREPARING)
        # Now claimable again.
        self.assertIsNotNone(
            campaigns.claim_next_preparing_campaign(owner="worker-b", db_path=self.db_path)
        )

    def test_requeue_stale_preparing_fails_after_budget(self) -> None:
        campaign = self._preparing_campaign()
        self._force_lease(
            campaign.id, owner="dead-worker", minutes_ago=999, attempts=3
        )
        moved = campaigns.requeue_stale_preparing(
            older_than_minutes=120, max_attempts=3, db_path=self.db_path
        )
        self.assertEqual(moved, 1)
        refreshed = campaigns.get_campaign(campaign.id, db_path=self.db_path)
        self.assertEqual(refreshed.status, campaigns.CAMPAIGN_NEEDS_REVIEW)
        self.assertIn("prep lease expired", refreshed.last_error or "")

    def test_finish_campaign_prep_rejects_foreign_owner(self) -> None:
        campaign = self._preparing_campaign()
        campaigns.claim_next_preparing_campaign(owner="worker-a", db_path=self.db_path)
        ok = campaigns.finish_campaign_prep(
            campaign.id,
            owner="worker-b",
            status=campaigns.CAMPAIGN_PUBLISHING,
            db_path=self.db_path,
        )
        self.assertFalse(ok)
        refreshed = campaigns.get_campaign(campaign.id, db_path=self.db_path)
        self.assertEqual(refreshed.status, campaigns.CAMPAIGN_PREPARING)
        self.assertEqual(
            refreshed.metadata[campaigns.PREP_LEASE_KEY]["owner"], "worker-a"
        )

    def test_finish_campaign_prep_clears_lease(self) -> None:
        campaign = self._preparing_campaign()
        campaigns.claim_next_preparing_campaign(owner="worker-a", db_path=self.db_path)
        ok = campaigns.finish_campaign_prep(
            campaign.id,
            owner="worker-a",
            status=campaigns.CAMPAIGN_PUBLISHING,
            prepared_at="2026-01-01T00:00:00",
            last_error=None,
            db_path=self.db_path,
        )
        self.assertTrue(ok)
        refreshed = campaigns.get_campaign(campaign.id, db_path=self.db_path)
        self.assertEqual(refreshed.status, campaigns.CAMPAIGN_PUBLISHING)
        self.assertNotIn(campaigns.PREP_LEASE_KEY, refreshed.metadata)

    def test_has_claimable_ignores_live_lease(self) -> None:
        campaign = self._preparing_campaign()
        campaigns.claim_next_preparing_campaign(owner="other", db_path=self.db_path)
        self.assertFalse(
            campaigns.has_claimable_preparing_campaigns(db_path=self.db_path)
        )
        self._force_lease(campaign.id, owner="dead", minutes_ago=999)
        self.assertTrue(
            campaigns.has_claimable_preparing_campaigns(db_path=self.db_path)
        )


class WorkerPrepExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "workerprep.db"
        create_table.bootstrap(self.db_path)
        self.profile = profiles.create_profile("Brand", db_path=self.db_path)
        self.media_group = media_groups.create_media_group(
            "Launch", db_path=self.db_path
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _preparing_campaign(self) -> campaigns.Campaign:
        return campaigns.create_campaign(
            self.profile.id,
            self.media_group.id,
            status=campaigns.CAMPAIGN_PREPARING,
            metadata={
                "source": "test",
                campaigns.PREP_REQUEST_KEY: {"brief": "hello", "options": {}},
            },
            db_path=self.db_path,
        )

    def _drive_one_tick(self, worker: PublishWorker) -> None:
        async def drive():
            await worker._tick()
            if worker._prep_tasks:
                await asyncio.gather(*tuple(worker._prep_tasks), return_exceptions=True)
            worker.stop()
            await worker._finish_shutdown()

        asyncio.run(drive())

    def test_worker_success_flips_campaign_to_publishing(self) -> None:
        campaign = self._preparing_campaign()

        def runner(_campaign, _db_path):
            return {"jobs": [{"id": 1}], "skipped": []}

        worker = PublishWorker(
            lambda *_args: None,
            config=WorkerConfig(poll_interval=0.001),
            db_path=self.db_path,
            prep_runner=runner,
        )
        self._drive_one_tick(worker)
        refreshed = campaigns.get_campaign(campaign.id, db_path=self.db_path)
        self.assertEqual(refreshed.status, campaigns.CAMPAIGN_PUBLISHING)

    def test_worker_prep_failure_lands_in_needs_review(self) -> None:
        campaign = self._preparing_campaign()

        def boom(_campaign, _db_path):
            raise RuntimeError("ffmpeg exploded")

        worker = PublishWorker(
            lambda *_args: None,
            config=WorkerConfig(poll_interval=0.001),
            db_path=self.db_path,
            prep_runner=boom,
        )
        self._drive_one_tick(worker)
        refreshed = campaigns.get_campaign(campaign.id, db_path=self.db_path)
        self.assertEqual(refreshed.status, campaigns.CAMPAIGN_NEEDS_REVIEW)
        self.assertIn("ffmpeg exploded", refreshed.last_error or "")
        self.assertNotIn(campaigns.PREP_LEASE_KEY, refreshed.metadata)

    def test_worker_empty_result_lands_in_needs_review(self) -> None:
        campaign = self._preparing_campaign()

        worker = PublishWorker(
            lambda *_args: None,
            config=WorkerConfig(poll_interval=0.001),
            db_path=self.db_path,
            prep_runner=lambda _c, _d: {"jobs": [], "skipped": []},
        )
        self._drive_one_tick(worker)
        refreshed = campaigns.get_campaign(campaign.id, db_path=self.db_path)
        self.assertEqual(refreshed.status, campaigns.CAMPAIGN_NEEDS_REVIEW)
        self.assertEqual(refreshed.last_error, "No publishable posts queued")

    def test_prep_runner_disabled_leaves_campaign_untouched(self) -> None:
        campaign = self._preparing_campaign()
        worker = PublishWorker(
            lambda *_args: None,
            config=WorkerConfig(poll_interval=0.001),
            db_path=self.db_path,
        )
        self._drive_one_tick(worker)
        refreshed = campaigns.get_campaign(campaign.id, db_path=self.db_path)
        self.assertEqual(refreshed.status, campaigns.CAMPAIGN_PREPARING)

    def test_drain_includes_preparing_campaigns(self) -> None:
        campaign = self._preparing_campaign()
        seen: list[int] = []

        def runner(c, _db_path):
            seen.append(c.id)
            return {"jobs": [{"id": 1}]}

        async def drive():
            worker = PublishWorker(
                lambda *_args: None,
                config=WorkerConfig(poll_interval=0.001),
                db_path=self.db_path,
                prep_runner=runner,
            )
            await asyncio.wait_for(worker.drain(), timeout=5)

        asyncio.run(drive())
        self.assertEqual(seen, [campaign.id])
        refreshed = campaigns.get_campaign(campaign.id, db_path=self.db_path)
        self.assertEqual(refreshed.status, campaigns.CAMPAIGN_PUBLISHING)

    def test_drain_does_not_spin_on_foreign_lease(self) -> None:
        campaign = self._preparing_campaign()
        campaigns.claim_next_preparing_campaign(owner="other", db_path=self.db_path)

        def runner(_c, _db_path):
            raise AssertionError("a foreign lease must never be run")

        async def drive():
            worker = PublishWorker(
                lambda *_args: None,
                config=WorkerConfig(poll_interval=0.001),
                db_path=self.db_path,
                prep_runner=runner,
            )
            # Must return promptly; a live foreign lease is not our pending work.
            await asyncio.wait_for(worker.drain(), timeout=5)

        asyncio.run(drive())
        refreshed = campaigns.get_campaign(campaign.id, db_path=self.db_path)
        self.assertEqual(refreshed.status, campaigns.CAMPAIGN_PREPARING)


class RunCampaignPrepTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "runcamp.db"
        create_table.bootstrap(self.db_path)
        self.profile = profiles.create_profile("Brand", db_path=self.db_path)
        self.file_id = self._file_record("clip.mp4")
        self.media_group = media_groups.create_media_group(
            "Launch",
            primary_video_file_id=self.file_id,
            db_path=self.db_path,
        )
        media_groups.add_media_group_item(
            self.media_group.id,
            self.file_id,
            role=media_groups.ROLE_VIDEO,
            db_path=self.db_path,
        )
        self.account = profiles.add_account(
            self.profile.id,
            profiles.PLATFORM_DOUYIN,
            "brand",
            db_path=self.db_path,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _file_record(self, filename: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO file_records (filename, filesize, file_path) VALUES (?, ?, ?)",
                (filename, 1.0, f"/tmp/{filename}"),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def test_run_campaign_prep_prepares_before_drafting(self) -> None:
        campaign = campaigns.create_campaign(
            self.profile.id,
            self.media_group.id,
            status=campaigns.CAMPAIGN_PREPARING,
            selected_account_ids=[self.account.id],
            metadata={
                campaigns.PREP_REQUEST_KEY: {
                    "brief": "a real brief",
                    "options": {},
                    "schedule": {"publishNow": True},
                }
            },
            db_path=self.db_path,
        )
        calls: list[str] = []

        def prepare_artifacts(*_args, **_kwargs):
            calls.append("prepare")
            return {"videoLocalPath": "/tmp/out.mp4"}

        def generate_account_draft(*_args, **_kwargs):
            calls.append("draft")
            return {"message": "hello world", "hashtags": [], "firstComment": ""}

        result = campaign_prep.run_campaign_prep(
            campaign,
            db_path=self.db_path,
            prepare_artifacts=prepare_artifacts,
            generate_account_draft=generate_account_draft,
            artifact_payloads_for_platform=lambda *_a, **_k: [{"local_path": "/tmp/out.mp4"}],
            artifact_part_groups_for_platform=None,
            job_to_payload=lambda job: {"id": job.id},
        )

        self.assertEqual(calls, ["prepare", "draft"])
        self.assertEqual(len(result["jobs"]), 1)
        posts = campaigns.list_campaign_posts(campaign.id, db_path=self.db_path)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].draft["message"], "hello world")
        queued = jobs.get_job(
            result["jobs"][0]["id"], db_path=self.db_path
        )
        self.assertEqual(queued.platform, profiles.PLATFORM_DOUYIN)
        self.assertEqual(queued.total_targets, 1)

    def test_run_campaign_prep_without_request_raises(self) -> None:
        campaign = campaigns.create_campaign(
            self.profile.id,
            self.media_group.id,
            status=campaigns.CAMPAIGN_PREPARING,
            selected_account_ids=[self.account.id],
            metadata={"source": "legacy"},
            db_path=self.db_path,
        )
        with self.assertRaises(ValueError):
            campaign_prep.run_campaign_prep(
                campaign,
                db_path=self.db_path,
                prepare_artifacts=lambda *_a, **_k: {},
                generate_account_draft=lambda *_a, **_k: {},
                artifact_payloads_for_platform=lambda *_a, **_k: [],
                job_to_payload=lambda job: {"id": job.id},
            )


if __name__ == "__main__":
    unittest.main()
