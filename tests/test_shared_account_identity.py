"""One real destination shared by profile-owned rows must not queue media twice."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from myUtils import campaign_prep, campaigns, jobs, profiles, publish_orchestrator as po


class _Account:
    def __init__(self, account_id: int, platform: str, config: dict):
        self.id = account_id
        self.platform = platform
        self.config = config
        self.account_name = f"acct-{account_id}"
        self.nickname = ""


class SharedIdentityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name) / "shared.db"
        with closing(sqlite3.connect(self.tmp)) as conn, conn:
            conn.executescript("""
                CREATE TABLE accounts(id INTEGER PRIMARY KEY, platform TEXT, config_json TEXT);
                CREATE TABLE media_groups(id INTEGER PRIMARY KEY);
                CREATE TABLE media_group_items(media_group_id INTEGER, file_record_id INTEGER);
                CREATE TABLE campaigns(id INTEGER PRIMARY KEY, media_group_id INTEGER);
                CREATE TABLE campaign_posts(id INTEGER PRIMARY KEY, campaign_id INTEGER);
                CREATE TABLE publish_job_targets(id INTEGER PRIMARY KEY, account_ref TEXT, file_ref TEXT,
                    status TEXT, schedule_at TEXT, checkpoint_json TEXT DEFAULT '{}', delivery_state TEXT);
                INSERT INTO accounts VALUES(11,'facebook','{"pageId":"125"}');
                INSERT INTO accounts VALUES(64,'facebook','{"pageId":"125"}');
                INSERT INTO accounts VALUES(99,'facebook','{"pageId":"777"}');
            """)

    def _seed(self, *, status="pending", checkpoint=None, delivery=None):
        with closing(sqlite3.connect(self.tmp)) as conn, conn:
            conn.executescript("""
                INSERT INTO media_groups VALUES(5);
                INSERT INTO media_group_items VALUES(5,1);
                INSERT INTO campaigns VALUES(1,5);
                INSERT INTO campaign_posts VALUES(1,1);
            """)
            conn.execute("INSERT INTO publish_job_targets(account_ref,file_ref,status,checkpoint_json,delivery_state) "
                         "VALUES('account:11','campaign_post:1',?,?,?)",
                         (status, json.dumps(checkpoint or {}), delivery))

    def test_identity_is_read_from_stable_meta_and_tiktok_config(self):
        for platform, config, expected in (
            ("facebook", {"pageId": "125"}, ("facebook", "125")),
            ("instagram", {"igUserId": "1784"}, ("instagram", "1784")),
            ("threads", {"threadUserId": "2885"}, ("threads", "2885")),
            ("tiktok", {"openId": "native"}, ("tiktok", "native")),
            ("tiktok", {"open_id": "native"}, ("tiktok", "native")),
            ("twitter", {"x": "1"}, None),
        ):
            self.assertEqual(po._external_account_identity(_Account(999, platform, config)), expected)

    def test_rows_with_the_same_page_are_found(self):
        self.assertEqual(po._account_ids_with_identity("facebook", "125", db_path=self.tmp), [11, 64])

    def test_the_other_profile_row_makes_the_media_a_duplicate(self):
        self._seed()
        self.assertTrue(po._shared_identity_already_queued(_Account(64, "facebook", {"pageId": "125"}),
                                                         5, db_path=self.tmp))
        self.assertTrue(po._already_queued_for_media(64, 5, db_path=self.tmp))

    def test_a_different_page_is_not_a_duplicate(self):
        self._seed()
        self.assertFalse(po._shared_identity_already_queued(_Account(99, "facebook", {"pageId": "777"}),
                                                          5, db_path=self.tmp))

    def test_an_account_without_shared_identity_is_not_flagged(self):
        self._seed()
        self.assertFalse(po._shared_identity_already_queued(_Account(999, "twitter", {}), 5, db_path=self.tmp))

    def test_review_and_delivery_evidence_blocks_duplicate_even_in_terminal_rows(self):
        self._seed()
        for status, checkpoint, delivery in (
            ("awaiting_delivery", {}, None), ("needs_review", {}, None),
            ("succeeded", {"provider_receipt": {"provider": "facebook", "id": "confirmed", "status": "delivered"}}, "delivered"),
            ("failed", {"submission_started": True}, "failed"),
            ("cancelled", {"provider_receipt": {"provider": "facebook", "id": "unknown"}}, None),
        ):
            with self.subTest(status=status):
                with closing(sqlite3.connect(self.tmp)) as conn, conn:
                    conn.execute("UPDATE publish_job_targets SET status=?,checkpoint_json=?,delivery_state=?",
                                 (status, json.dumps(checkpoint), delivery))
                self.assertTrue(po._already_queued_for_media(64, 5, db_path=self.tmp))

    def test_same_whole_file_set_matches_but_partial_overlap_does_not(self):
        self._seed()
        with closing(sqlite3.connect(self.tmp)) as conn, conn:
            conn.executescript("""
                INSERT INTO media_group_items VALUES(5,2);
                INSERT INTO media_groups VALUES(6),(7);
                INSERT INTO media_group_items VALUES(6,2),(6,1),(7,1),(7,3);
            """)
        self.assertTrue(po._already_queued_for_media(64, 6, db_path=self.tmp))
        self.assertFalse(po._already_queued_for_media(64, 7, db_path=self.tmp))


NOW = datetime(2035, 1, 2, 12, tzinfo=timezone.utc)


def _managed_config(*, workspace="26985", profile_ref="3917"):
    return {"publishingProvider": "sociamonials", "sociamonials": {
        "workspace_id": workspace, "network": "tiktok", "profile_refs": [profile_ref],
    }}


class SharedTikTokFinalizerTests(unittest.TestCase):
    """Real temporary queue/media persistence; only heavy preparation is mocked."""
    def setUp(self):
        from db import createTable
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.db_path = self.base / "queue.db"
        createTable.bootstrap(self.db_path)
        self.first = profiles.create_profile("Synthetic first", db_path=self.db_path)
        self.second = profiles.create_profile("Synthetic second", db_path=self.db_path)
        self.teaching = profiles.create_profile("Synthetic teaching", db_path=self.db_path)
        self.nw = profiles.add_account(self.first.id, "tiktok", "same display name", auth_type="manual",
                                       config=_managed_config(), db_path=self.db_path)
        self.sw = profiles.add_account(self.second.id, "tiktok", "same display name", auth_type="manual",
                                       config=_managed_config(), db_path=self.db_path)
        self.teacher = profiles.add_account(self.teaching.id, "tiktok", "same display name", auth_type="manual",
                                            config=_managed_config(workspace="34293", profile_ref="5800"), db_path=self.db_path)
        self.other = profiles.add_account(self.teaching.id, "tiktok", "different ID", auth_type="manual",
                                          config=_managed_config(profile_ref="other"), db_path=self.db_path)
        self.parts_per_file = 1
        self.content_language = "en"
        self.enterContext(patch.object(po, "MIN_GAP_MINUTES", 30))
        self.enterContext(patch.object(po, "MAX_POSTS_PER_ACCOUNT_PER_DAY", 3))

    def _record(self, path, *, db_path):
        with closing(sqlite3.connect(db_path)) as conn, conn:
            found = conn.execute("SELECT id FROM file_records WHERE file_path=?", (path,)).fetchone()
            if found:
                return found[0]
            return conn.execute("INSERT INTO file_records(filename,file_path,filesize) VALUES(?,?,?)",
                                (Path(path).name, path, Path(path).stat().st_size)).lastrowid

    def _prepare(self, campaign_id, profile, media_files, request_data, **kwargs):
        for media in media_files:
            for index in range(1, self.parts_per_file + 1):
                campaigns.add_campaign_artifact(
                    campaign_id, artifact_kind="prepared_video", source_file_record_id=media["file_record_id"],
                    local_path=media["file_path"], metadata={"role": "video", "part_index": index,
                                                          "part_count": self.parts_per_file}, db_path=self.db_path,
                )
        return {"language": self.content_language, "languageSource": "supplied",
                "transcriptText": "An offline walk beside the river."}

    @staticmethod
    def _parts(artifacts, platform):
        return [[artifact] for artifact in artifacts]

    @staticmethod
    def _groups(artifacts, platform, media_files, **kwargs):
        return [([artifact], [artifact["source_file_record_id"]]) for artifact in artifacts]

    @staticmethod
    def _configure(accounts, platforms, request_data, **kwargs):
        return {**request_data, "_preparationAccountConfigs": {platform: {} for platform in platforms},
                "_backupCompatiblePlatforms": [], "_imagePreparationLimits": {}, "_preparationLimits": {}}, {}

    def _submit(self, selected, *, asynchronous=False, filenames=("SFW clip.mp4",), start_at=None):
        from myUtils import preparation_context
        paths = []
        for filename in filenames:
            path = self.base / filename
            path.write_bytes(b"synthetic media; expensive preparation is mocked")
            paths.append(str(path))
        profile_ids = list(dict.fromkeys(account.profile_id for account in selected))
        common = dict(profile_ids=profile_ids, selected_account_ids=[account.id for account in selected],
                      media_file_paths=paths, brief="An offline walk beside the river.", options={},
                      schedule={"startAt": (start_at or NOW).isoformat()}, account_drafts={}, db_path=self.db_path,
                      ensure_file_record_for_path=self._record, now_fn=lambda: NOW)
        callbacks = dict(prepare_artifacts=self._prepare, artifact_payloads_for_platform=lambda artifacts, _p: artifacts,
                         artifact_part_groups_for_platform=self._parts,
                         generate_account_draft=lambda *_args: {"message": "An offline walk beside the river."},
                         job_to_payload=lambda job: job.to_dict())
        with patch.object(preparation_context, "configure_request", side_effect=self._configure), \
                patch.object(po, "_prepared_post_groups", side_effect=self._groups):
            if not asynchronous:
                return po.submit_publish(**common, **callbacks)
            admitted = campaign_prep.submit_publish_async(**common)
            queued, skipped = [], list(admitted.skipped)
            for campaign_id in admitted.campaign_ids:
                prepared = campaign_prep.run_campaign_prep(campaigns.get_campaign(campaign_id, db_path=self.db_path),
                                                           db_path=self.db_path, **callbacks)
                queued.extend(prepared["jobs"])
                skipped.extend(prepared["skipped"])
            return po.SubmitResult(admitted.campaign_ids, queued, skipped)

    def _queued(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute("SELECT account_ref FROM publish_job_targets ORDER BY id").fetchall()

    def test_first_profile_only_preserves_ownership(self):
        result = self._submit([self.nw])
        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(jobs.get_job(result.jobs[0]["id"], db_path=self.db_path).profile_id, self.first.id)
        self.assertEqual(self._queued(), [(f"account:{self.nw.id}",)])

    def test_second_profile_only_preserves_ownership(self):
        result = self._submit([self.sw])
        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(jobs.get_job(result.jobs[0]["id"], db_path=self.db_path).profile_id, self.second.id)
        self.assertEqual(self._queued(), [(f"account:{self.sw.id}",)])

    def test_combined_synchronous_selection_queues_one_publication(self):
        result = self._submit([self.nw, self.sw])
        self.assertEqual(len(result.jobs), 1)
        self.assertTrue(any("already queued" in item["reason"] for item in result.skipped))
        self.assertEqual(self._queued(), [(f"account:{self.nw.id}",)])

    def test_combined_asynchronous_selection_queues_one_publication(self):
        result = self._submit([self.nw, self.sw], asynchronous=True)
        self.assertEqual(len(result.jobs), 1)
        self.assertTrue(any("already queued" in item["reason"] for item in result.skipped))
        self.assertEqual(self._queued(), [(f"account:{self.nw.id}",)])

    def test_separate_teaching_and_different_destination_still_queue(self):
        result = self._submit([self.nw, self.sw, self.teacher, self.other])
        self.assertEqual(len(result.jobs), 3)
        self.assertCountEqual(self._queued(), [(f"account:{account.id}",) for account in (self.nw, self.teacher, self.other)])

    def test_confirmed_delivery_is_not_requeued_through_another_alias(self):
        self._submit([self.nw])
        receipt = {"provider_receipt": {"provider": "sociamonials", "id": "fixture-delivery", "status": "delivered",
                                        "delivered_at": NOW.isoformat(), "workspace_id": "26985",
                                        "network": "tiktok", "profile_refs": ["3917"]}}
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE publish_job_targets SET status='succeeded',delivery_state='delivered',checkpoint_json=?",
                         (json.dumps(receipt),))
        self.assertEqual(self._submit([self.sw], asynchronous=True).jobs, [])
        self.assertEqual(len(self._queued()), 1)

    def test_distinct_assets_and_partial_overlap_remain_eligible(self):
        self._submit([self.nw], filenames=("SFW one.mp4", "SFW common.mp4"))
        result = self._submit([self.sw], filenames=("SFW other.mp4", "SFW common.mp4"))
        self.assertEqual(len(result.jobs), 2)
        self.assertEqual(len(self._queued()), 4)

    def _assert_complete_parts(self, asynchronous):
        self.parts_per_file = 2
        result = self._submit([self.nw, self.sw], asynchronous=asynchronous,
                              filenames=("SFW source-one.mp4", "SFW source-two.mp4"))
        self.assertEqual(len(result.jobs), 4)
        queued = [jobs.get_job(item["id"], db_path=self.db_path) for item in result.jobs]
        self.assertEqual([job.payload["partIndex"] for job in queued], [1, 2, 3, 4])
        self.assertTrue(all(job.payload["partCount"] == 4 for job in queued))
        for job in queued:
            self.assertEqual(job.payload["fileRecordIds"], [job.payload["artifacts"][0]["source_file_record_id"]])
        self.assertEqual(self._queued(), [(f"account:{self.nw.id}",)] * 4)
        slots = [datetime.fromisoformat(jobs.list_targets(job.id, db_path=self.db_path)[0].schedule_at) for job in queued]
        self.assertTrue(all(later - earlier >= timedelta(minutes=30) for earlier, later in zip(slots, slots[1:])))
        self.assertEqual(slots[3].date(), slots[0].date() + timedelta(days=1))

    def test_complete_source_parts_are_not_dropped_as_synchronous_duplicates(self):
        self._assert_complete_parts(False)

    def test_complete_source_parts_are_not_dropped_as_asynchronous_duplicates(self):
        self._assert_complete_parts(True)

    def test_shared_identity_does_not_loosen_nsfw_restriction(self):
        for asynchronous in (False, True):
            result = self._submit([self.nw, self.sw], asynchronous=asynchronous, filenames=("private.mp4",))
            self.assertEqual(result.jobs, [])
            self.assertTrue(all(item["reason"] == "nsfw_no_adult_safe_account" for item in result.skipped))
        self.assertEqual(self._queued(), [])

    def test_shared_identity_does_not_bypass_account_language_guard(self):
        self.content_language = "zh"
        profiles.update_account(self.sw.id, config={**_managed_config(), "audience_language": "en"}, db_path=self.db_path)
        result = self._submit([profiles.get_account(self.sw.id, db_path=self.db_path)], asynchronous=True)
        self.assertEqual(result.jobs, [])
        self.assertTrue(any("English-only" in item["reason"] for item in result.skipped))




    def _assert_midnight_parts_stay_ordered(self, asynchronous):
        self.parts_per_file = 2
        for index in range(3):
            jobs.enqueue_job(jobs.JobSpec(platform="tiktok", profile_id=self.first.id,
                payload={"message": "A different previously booked publication."},
                targets=[(f"account:{self.nw.id}", f"/unrelated-{index}.mp4", (NOW + timedelta(minutes=30 * index)).isoformat())]),
                db_path=self.db_path)
        base = NOW.replace(hour=23, minute=55, tzinfo=None)
        with patch.dict(os.environ, {"SAU_OPERATOR_TIMEZONE": "UTC"}):
            result = self._submit([self.nw, self.sw], asynchronous=asynchronous, start_at=base)
        self.assertEqual(len(result.jobs), 2)
        slots = [jobs._utc_datetime(jobs.list_targets(item["id"], db_path=self.db_path)[0].schedule_at) for item in result.jobs]
        self.assertEqual(slots[0], base + timedelta(days=1))
        self.assertEqual(slots[1], base + timedelta(days=1, minutes=30))
        self.assertGreater(slots[1], slots[0])

    def test_synchronous_midnight_rollover_does_not_publish_the_tail_first(self):
        self._assert_midnight_parts_stay_ordered(False)

    def test_asynchronous_midnight_rollover_does_not_publish_the_tail_first(self):
        self._assert_midnight_parts_stay_ordered(True)


if __name__ == "__main__":
    unittest.main()
