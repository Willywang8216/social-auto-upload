"""HTTP tests for the Publish Center content-entity read view.

Covers ``GET /publish-entities`` (grouping by shared media group + legacy job
fallback, filters, pagination, safe media previews) and
``GET /publish-entities/<entity_id>``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import db.createTable as create_table


flask_available = importlib.util.find_spec("flask") is not None

if "conf" not in sys.modules:
    conf_module = types.ModuleType("conf")
    conf_module.BASE_DIR = Path(".").resolve()
    conf_module.XHS_SERVER = "http://127.0.0.1:11901"
    conf_module.LOCAL_CHROME_PATH = ""
    conf_module.LOCAL_CHROME_HEADLESS = True
    conf_module.DEBUG_MODE = False
    sys.modules["conf"] = conf_module


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class PublishEntitiesApiTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import sau_backend
        except ModuleNotFoundError as exc:  # pragma: no cover - environment-specific
            self.skipTest(f"backend dependencies unavailable: {exc}")

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

    def tearDown(self) -> None:
        if hasattr(self, "_base_dir_patch"):
            self._base_dir_patch.stop()
        if hasattr(self, "_orig_policy"):
            self.sau_backend.app.config["SECURITY_POLICY"] = self._orig_policy
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    def _insert_file_record(self, filename: str, file_path: str) -> int:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            cursor = conn.execute(
                "INSERT INTO file_records (filename, filesize, file_path) VALUES (?, ?, ?)",
                (filename, 1.0, file_path),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def _profile(self, name: str = "Brand A") -> int:
        response = self.client.post("/profiles", json={"name": name})
        self.assertEqual(response.status_code, 200)
        return response.get_json()["data"]["id"]

    def _account(self, profile_id: int, name: str = "brand-main") -> int:
        response = self.client.post(
            f"/profiles/{profile_id}/accounts",
            json={
                "platform": "twitter",
                "accountName": name,
                "authType": "oauth",
                "enabled": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()["data"]["id"]

    def _seed_campaign_entity(self, *, account_id: int, profile_id: int) -> dict:
        """A legacy media group/post/job history used by read/edit projections."""
        from tests.legacy_queue import enqueue_legacy_job
        campaign_store = self.sau_backend.campaign_store
        media_group_store = self.sau_backend.media_group_store
        job_runtime = self.sau_backend.job_runtime

        in_root = self._insert_file_record("clip.mp4", "clip.mp4")
        outside = self._insert_file_record("outside.mp4", str(self.base_dir / "outside.mp4"))

        group = media_group_store.create_media_group("Launch", db_path=self.db_path)
        media_group_store.add_media_group_item(
            group.id, in_root, role="video", sort_order=0, db_path=self.db_path
        )
        media_group_store.add_media_group_item(
            group.id, outside, role="attachment", sort_order=1, db_path=self.db_path
        )

        campaign = campaign_store.create_campaign(
            profile_id,
            group.id,
            status=campaign_store.CAMPAIGN_PUBLISHING,
            metadata={"title": "Launch day"},
            db_path=self.db_path,
        )
        post = campaign_store.add_campaign_post(
            campaign.id,
            "twitter",
            account_ids=[account_id],
            draft={"message": "hello world", "hashtags": ["launch"]},
            status="queued",
            db_path=self.db_path,
        )
        campaign_store.add_campaign_artifact(
            campaign.id,
            artifact_kind="remote_upload",
            public_url="https://cdn.example/clip.mp4",
            local_path="/private/local/clip.mp4",
            remote_path="private-bucket/clip.mp4",
            db_path=self.db_path,
        )
        campaign_store.add_campaign_artifact(
            campaign.id,
            artifact_kind="local",
            local_path="/private/local/raw.mp4",
            remote_path="private-bucket/raw.mp4",
            db_path=self.db_path,
        )

        job = enqueue_legacy_job(
            job_runtime.JobSpec(
                platform="twitter",
                payload={
                    "campaignId": campaign.id,
                    "campaignPostId": post.id,
                    "message": "hello world",
                },
                targets=[("account:%d" % account_id, "campaign_post:%d" % post.id, None)],
                profile_id=profile_id,
            ),
            db_path=self.db_path,
        )
        return {"media_group_id": group.id, "campaign_id": campaign.id, "post_id": post.id, "job_id": job.id}

    def test_media_group_entity_groups_campaign_post_and_job(self) -> None:
        profile_id = self._profile()
        account_id = self._account(profile_id)
        seeded = self._seed_campaign_entity(account_id=account_id, profile_id=profile_id)

        response = self.client.get("/publish-entities")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()["data"]
        self.assertEqual(body["total"], 1)
        entity = body["items"][0]

        self.assertEqual(entity["entityId"], f"mg-{seeded['media_group_id']}")
        self.assertEqual(entity["entityType"], "media_group")
        self.assertEqual(entity["mediaGroup"]["id"], seeded["media_group_id"])
        self.assertEqual(entity["profileId"], profile_id)
        self.assertEqual(entity["profile"]["name"], "Brand A")
        self.assertEqual(entity["campaignIds"], [seeded["campaign_id"]])
        self.assertEqual(entity["status"], "queued")

        # Media items come from file_records; the in-root file gets a preview,
        # the outside-root file must not.
        items = {item["filename"]: item for item in entity["mediaItems"]}
        self.assertIn("clip.mp4", items)
        self.assertEqual(items["clip.mp4"]["previewUrl"], "/getFile?filename=clip.mp4")
        self.assertNotIn("previewUrl", items["outside.mp4"])

        # Per-post draft/platform/account/status, linked to its job.
        self.assertEqual(len(entity["posts"]), 1)
        post = entity["posts"][0]
        self.assertEqual(post["platform"], "twitter")
        self.assertEqual(post["status"], "queued")
        self.assertEqual(post["draft"]["message"], "hello world")
        self.assertEqual(post["accounts"][0]["id"], account_id)
        self.assertEqual(post["jobId"], seeded["job_id"])

        # Job/target status + schedule.
        self.assertEqual(len(entity["jobs"]), 1)
        self.assertEqual(entity["jobs"][0]["id"], seeded["job_id"])
        self.assertEqual(entity["jobs"][0]["status"], "pending")
        self.assertEqual(entity["jobs"][0]["targets"][0]["status"], "pending")
        self.assertIsNone(entity["jobs"][0]["targets"][0]["scheduleAt"])

        # Only the HTTPS artifact is linkable; private paths never leak.
        urls = [artifact.get("url") for artifact in entity["artifacts"]]
        self.assertIn("https://cdn.example/clip.mp4", urls)
        self.assertEqual(sum(1 for url in urls if url), 1)
        rendered = json.dumps(response.get_json())
        self.assertNotIn("/private/local", rendered)
        self.assertNotIn("private-bucket", rendered)
        self.assertNotIn(str(self.base_dir), rendered)

    def test_legacy_job_without_campaign_is_one_entity_per_job(self) -> None:
        job_runtime = self.sau_backend.job_runtime
        self._insert_file_record("legacy.mp4", "legacy.mp4")
        job = job_runtime.enqueue_job(
            job_runtime.JobSpec(
                platform="twitter",
                payload={"title": "legacy publish"},
                targets=[("account:7", "legacy.mp4", None)],
            ),
            db_path=self.db_path,
        )

        response = self.client.get("/publish-entities")
        self.assertEqual(response.status_code, 200)
        items = response.get_json()["data"]["items"]
        self.assertEqual(len(items), 1)
        entity = items[0]
        self.assertEqual(entity["entityId"], f"job-{job.id}")
        self.assertEqual(entity["entityType"], "job")
        self.assertIsNone(entity["mediaGroupId"])
        self.assertEqual(entity["campaignIds"], [])
        self.assertEqual(entity["mediaItems"][0]["filename"], "legacy.mp4")
        self.assertEqual(entity["mediaItems"][0]["previewUrl"], "/getFile?filename=legacy.mp4")
        self.assertEqual(entity["posts"], [])
        self.assertEqual(entity["jobs"][0]["id"], job.id)
        self.assertEqual(entity["jobs"][0]["targets"][0]["status"], "pending")
        self.assertIn("retryEligibility", entity["jobs"][0]["targets"][0])
        detail = self.client.get(f"/publish-entities/job-{job.id}").get_json()["data"]
        self.assertEqual(detail["jobs"], entity["jobs"])

    def test_status_month_filters_and_pagination(self) -> None:
        profile_id = self._profile()
        account_id = self._account(profile_id)
        self._seed_campaign_entity(account_id=account_id, profile_id=profile_id)

        from datetime import datetime, timezone

        current_month = datetime.now(tz=timezone.utc).strftime("%Y-%m")

        matching = self.client.get(f"/publish-entities?status=queued&month={current_month}")
        self.assertEqual(matching.get_json()["data"]["total"], 1)

        wrong_status = self.client.get("/publish-entities?status=failed")
        self.assertEqual(wrong_status.get_json()["data"]["total"], 0)

        wrong_month = self.client.get("/publish-entities?month=1999-01")
        self.assertEqual(wrong_month.get_json()["data"]["total"], 0)

        bad_month = self.client.get("/publish-entities?month=2026/01")
        self.assertEqual(bad_month.status_code, 400)

        page = self.client.get("/publish-entities?limit=1").get_json()["data"]
        self.assertEqual(page["limit"], 1)
        self.assertEqual(len(page["items"]), 1)
        self.assertFalse(page["hasMore"])

    def test_calendar_month_includes_entity_when_later_target_is_in_month(self) -> None:
        profile_id = self._profile()
        account_id = self._account(profile_id)
        seeded = self._seed_campaign_entity(account_id=account_id, profile_id=profile_id)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE publish_job_targets SET schedule_at=? WHERE job_id=?",
                ("2026-10-12T14:00:00", seeded["job_id"]),
            )

        response = self.client.get("/publish-entities?month=2026-10")

        self.assertEqual(response.status_code, 200)
        items = response.get_json()["data"]["items"]
        self.assertEqual(len(items), 1)
        target = items[0]["jobs"][0]["targets"][0]
        self.assertEqual(target["scheduleAt"], "2026-10-12T14:00:00")

    def test_entity_contains_distinct_destination_schedules(self) -> None:
        profile_id = self._profile()
        account_id = self._account(profile_id)
        seeded = self._seed_campaign_entity(account_id=account_id, profile_id=profile_id)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO publish_jobs (idempotency_key, profile_id, platform, payload_json, status, total_targets) VALUES (?, ?, ?, ?, ?, ?)",
                ("later-target-job", profile_id, "twitter", json.dumps({"campaignId": seeded["campaign_id"], "campaignPostId": seeded["post_id"], "message": "hello world"}), "pending", 1),
            )
            later_job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO publish_job_targets (job_id, account_ref, file_ref, schedule_at, status) VALUES (?, ?, ?, ?, ?)",
                (later_job_id, f"account:{account_id}", f"campaign_post:{seeded['post_id']}", "2026-10-15T16:30:00", "pending"),
            )
            conn.execute(
                "UPDATE publish_job_targets SET schedule_at=? WHERE job_id=?",
                ("2026-10-12T14:00:00", seeded["job_id"]),
            )

        detail = self.client.get(f"/publish-entities/mg-{seeded['media_group_id']}")

        self.assertEqual(detail.status_code, 200)
        targets = [target for job in detail.get_json()["data"]["jobs"] for target in job["targets"]]
        self.assertEqual({target["scheduleAt"] for target in targets}, {
            "2026-10-12T14:00:00", "2026-10-15T16:30:00"
        })

    def test_public_media_url_filter_rejects_private_hosts(self) -> None:
        self.assertFalse(self.sau_backend._is_public_https_url("https://127.0.0.1/media.mp4"))
        self.assertFalse(self.sau_backend._is_public_https_url("https://localhost/media.mp4"))
        self.assertTrue(self.sau_backend._is_public_https_url("https://cdn.example/media.mp4"))

    def test_entity_detail_route_and_unknown_id(self) -> None:
        profile_id = self._profile()
        account_id = self._account(profile_id)
        seeded = self._seed_campaign_entity(account_id=account_id, profile_id=profile_id)

        detail = self.client.get(f"/publish-entities/mg-{seeded['media_group_id']}")
        self.assertEqual(detail.status_code, 200)
        entity = detail.get_json()["data"]
        self.assertEqual(entity["entityId"], f"mg-{seeded['media_group_id']}")
        self.assertNotIn("_timestamps", entity)
        self.assertNotIn("_sortAt", entity)

        job_detail = self.client.get(f"/publish-entities/job-{seeded['job_id']}")
        self.assertEqual(job_detail.status_code, 200)

        missing = self.client.get("/publish-entities/mg-999999")
        self.assertEqual(missing.status_code, 404)

        malformed = self.client.get("/publish-entities/not-a-key")
        self.assertEqual(malformed.status_code, 404)

    def test_workspace_scoping_excludes_foreign_entities(self) -> None:
        campaign_store = self.sau_backend.campaign_store
        media_group_store = self.sau_backend.media_group_store
        job_runtime = self.sau_backend.job_runtime

        profile_id = self._profile()
        group_a = media_group_store.create_media_group(
            "ws-a", workspace_id="ws-A", db_path=self.db_path
        )
        group_b = media_group_store.create_media_group(
            "ws-b", workspace_id="ws-B", db_path=self.db_path
        )
        campaign_store.create_campaign(
            profile_id, group_a.id, workspace_id="ws-A", db_path=self.db_path
        )
        campaign_store.create_campaign(
            profile_id, group_b.id, workspace_id="ws-B", db_path=self.db_path
        )
        foreign_job = job_runtime.enqueue_job(
            job_runtime.JobSpec(
                platform="twitter",
                payload={"title": "foreign"},
                targets=[("account:1", "legacy.mp4", None)],
            ),
            workspace_id="ws-B",
            db_path=self.db_path,
        )

        with patch.object(self.sau_backend, "_workspace_scope", return_value="ws-A"):
            listing = self.client.get("/publish-entities")
            self.assertEqual(listing.status_code, 200)
            items = listing.get_json()["data"]["items"]
            self.assertEqual([item["entityId"] for item in items], [f"mg-{group_a.id}"])

            foreign_group = self.client.get(f"/publish-entities/mg-{group_b.id}")
            self.assertEqual(foreign_group.status_code, 404)

            foreign_job_response = self.client.get(f"/publish-entities/job-{foreign_job.id}")
            self.assertEqual(foreign_job_response.status_code, 404)

    def test_patch_post_copy_syncs_draft_and_job_payload(self) -> None:
        profile_id = self._profile()
        account_id = self._account(profile_id)
        seeded = self._seed_campaign_entity(account_id=account_id, profile_id=profile_id)

        response = self.client.patch(
            f"/publish-entities/mg-{seeded['media_group_id']}/posts/{seeded['post_id']}",
            json={"message": "edited copy", "hashtags": ["new"]},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()["data"]
        self.assertEqual(payload["postId"], seeded["post_id"])
        self.assertEqual(payload["jobId"], seeded["job_id"])
        self.assertTrue(payload["payloadSynced"])
        self.assertEqual(payload["post"]["draft"]["message"], "edited copy")
        self.assertEqual(payload["post"]["draft"]["hashtags"], ["new"])
        self.assertEqual(
            payload["entity"]["posts"][0]["draft"]["message"], "edited copy"
        )

        # The stored post draft and the queued job payload agree.
        campaign_store = self.sau_backend.campaign_store
        job_runtime = self.sau_backend.job_runtime
        stored_post = campaign_store.get_campaign_post(seeded["post_id"], db_path=self.db_path)
        self.assertEqual(stored_post.draft["message"], "edited copy")
        stored_job = job_runtime.get_job(seeded["job_id"], db_path=self.db_path)
        self.assertEqual(stored_job.payload["draft"]["message"], "edited copy")
        self.assertEqual(stored_job.payload["message"], "edited copy")

    def test_patch_refused_once_target_no_longer_editable(self) -> None:
        profile_id = self._profile()
        account_id = self._account(profile_id)
        seeded = self._seed_campaign_entity(account_id=account_id, profile_id=profile_id)

        job_runtime = self.sau_backend.job_runtime
        target = job_runtime.list_targets(seeded["job_id"], db_path=self.db_path)[0]
        job_runtime.cancel_target(target.id, db_path=self.db_path)

        response = self.client.patch(
            f"/publish-entities/mg-{seeded['media_group_id']}/posts/{seeded['post_id']}",
            json={"message": "too late"},
        )
        self.assertEqual(response.status_code, 409)
        # The draft is left untouched when the edit is refused.
        stored_post = self.sau_backend.campaign_store.get_campaign_post(
            seeded["post_id"], db_path=self.db_path
        )
        self.assertEqual(stored_post.draft["message"], "hello world")

    def test_patch_requires_a_body_and_scopes_to_workspace(self) -> None:
        profile_id = self._profile()
        account_id = self._account(profile_id)
        seeded = self._seed_campaign_entity(account_id=account_id, profile_id=profile_id)

        empty = self.client.patch(
            f"/publish-entities/mg-{seeded['media_group_id']}/posts/{seeded['post_id']}",
            json={},
        )
        self.assertEqual(empty.status_code, 400)

        with patch.object(self.sau_backend, "_workspace_scope", return_value="ws-other"):
            foreign = self.client.patch(
                f"/publish-entities/mg-{seeded['media_group_id']}/posts/{seeded['post_id']}",
                json={"message": "nope"},
            )
        self.assertEqual(foreign.status_code, 404)

    def test_rollup_action_delivery_and_queue_precede_historical_success(self) -> None:
        rollup = self.sau_backend._rollup_entity_status
        for status in ("blocked", "awaiting_delivery", "needs_review"):
            for source in ("record", "job", "target"):
                for scheduled in (False, True):
                    with self.subTest(status=status, source=source, scheduled=scheduled):
                        records = ["published"] + ([status] if source == "record" else [])
                        jobs = ["succeeded"] + ([status] if source == "job" else [])
                        targets = [{"status": "succeeded"}] + ([{"status": status}] if source == "target" else [])
                        self.assertEqual(rollup(records, jobs, targets, has_schedule=scheduled), status)
        for terminal in ("failed", "cancelled"):
            self.assertEqual(rollup(["published"], [terminal], [{"status": "pending"}], has_schedule=True), terminal)
        for source_status in ("pending", "queued", "retrying"):
            self.assertEqual(rollup(["published"], [source_status], [], has_schedule=False), "queued")
            self.assertEqual(rollup(["published"], [], [{"status": source_status}], has_schedule=True), "scheduled")
        self.assertEqual(rollup(["published", "preparing"], ["succeeded"], [], has_schedule=False), "preparing")
        self.assertEqual(rollup(["published", "draft"], ["succeeded"], [], has_schedule=False), "draft")
        self.assertEqual(rollup(["published"], ["succeeded"], [{"status": "succeeded"}], has_schedule=False), "published")

    def test_reliability_status_filters_work_at_job_and_target_level(self) -> None:
        profile_id = self._profile()
        seeded = self._seed_campaign_entity(account_id=self._account(profile_id), profile_id=profile_id)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE campaigns SET status='published' WHERE id=?", (seeded["campaign_id"],))
            conn.execute("UPDATE campaign_posts SET status='published' WHERE id=?", (seeded["post_id"],))
        for status in ("blocked", "awaiting_delivery", "needs_review"):
            for source in ("job", "target", "both"):
                with self.subTest(status=status, source=source):
                    with closing(sqlite3.connect(self.db_path)) as conn, conn:
                        conn.execute("UPDATE publish_jobs SET status=? WHERE id=?", (status if source != "target" else "succeeded", seeded["job_id"]))
                        conn.execute("UPDATE publish_job_targets SET status=? WHERE job_id=?", (status if source != "job" else "succeeded", seeded["job_id"]))
                    listing = self.client.get(f"/publish-entities?status={status}").get_json()["data"]
                    self.assertEqual(listing["total"], 1)
                    self.assertEqual(listing["items"][0]["status"], status)
                    self.assertEqual(self.client.get("/publish-entities?status=published").get_json()["data"]["total"], 0)
                    detail = self.client.get(f"/publish-entities/mg-{seeded['media_group_id']}").get_json()["data"]
                    self.assertEqual(detail["status"], status)

    def test_reused_media_group_keeps_new_receipt_wait_distinct_from_published_history(self) -> None:
        from tests.legacy_queue import enqueue_legacy_job

        profile_id = self._profile()
        account_id = self._account(profile_id)
        seeded = self._seed_campaign_entity(account_id=account_id, profile_id=profile_id)
        store = self.sau_backend.campaign_store
        runtime = self.sau_backend.job_runtime
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE campaigns SET status='published' WHERE id=?", (seeded["campaign_id"],))
            conn.execute("UPDATE campaign_posts SET status='published' WHERE id=?", (seeded["post_id"],))
            conn.execute("UPDATE publish_jobs SET status='succeeded' WHERE id=?", (seeded["job_id"],))
            conn.execute("UPDATE publish_job_targets SET status='succeeded' WHERE job_id=?", (seeded["job_id"],))
        campaign = store.create_campaign(profile_id, seeded["media_group_id"], status=store.CAMPAIGN_PUBLISHING, db_path=self.db_path)
        post = store.add_campaign_post(campaign.id, "twitter", account_ids=[account_id], draft={"message": "New publication"}, status="awaiting_delivery", db_path=self.db_path)
        job = enqueue_legacy_job(runtime.JobSpec(platform="twitter", profile_id=profile_id,
            payload={"campaignId": campaign.id, "campaignPostId": post.id, "message": "New publication"},
            targets=[(f"account:{account_id}", f"campaign_post:{post.id}", None)]), db_path=self.db_path)
        receipt = {"provider": "twitter", "id": "new-receipt", "status": "processing", "submitted_at": "2099-12-12T09:00:00"}
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE publish_jobs SET status='awaiting_delivery' WHERE id=?", (job.id,))
            conn.execute("UPDATE publish_job_targets SET status='awaiting_delivery', delivery_state='awaiting_delivery', schedule_at=?, checkpoint_json=? WHERE job_id=?",
                ("2099-12-12T09:00:00", json.dumps({"provider_receipt": receipt}), job.id))
        body = self.client.get("/publish-entities?status=awaiting_delivery").get_json()["data"]
        self.assertEqual(body["total"], 1)
        entity = body["items"][0]
        self.assertEqual(entity["status"], "awaiting_delivery")
        self.assertEqual(set(entity["campaignIds"]), {seeded["campaign_id"], campaign.id})
        target = next(item for item in entity["jobs"] if item["id"] == job.id)["targets"][0]
        self.assertEqual(target["providerReceipt"], receipt)
        self.assertFalse(target["retryEligibility"]["allowed"])
        self.assertEqual(self.client.get("/publish-entities?status=published").get_json()["data"]["total"], 0)

    def test_job_entity_and_calendar_share_only_safe_reliability_fields(self) -> None:
        profile_id = self._profile()
        seeded = self._seed_campaign_entity(account_id=self._account(profile_id), profile_id=profile_id)
        runtime = self.sau_backend.job_runtime
        checkpoint = {
            "submission_started": True,
            "provider_receipt": {"provider": "twitter", "id": "receipt-123", "status": "processing", "submitted_at": "2099-12-12T09:00:00",
                "request": {"body": "private-request"}, "response": {"body": "private-response"}, "config": {"apiKey": "private-config"}, "url": "https://cdn.example/private?sig=private-signature"},
            "readiness": {"phase": "waiting_configuration", "provider": "twitter", "reasonCode": "reconnect", "reason": "Reconnect account; /private/local/media.mp4 token=private-error-token",
                "checkedAt": "2099-12-12T09:00:00", "partProgress": {"completeParts": 1, "totalParts": 2, "debug": "private-progress"}, "request": "private-readiness"},
            "payload": {"path": "/private/local/checkpoint.mp4"}, "request": "private-checkpoint-request", "response": "private-checkpoint-response", "lease_token": "private-lease",
        }
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE campaigns SET status='needs_review', last_error=? WHERE id=?", ("Account needs reconnect; api_key=private-campaign-key", seeded["campaign_id"]))
            conn.execute("UPDATE publish_jobs SET status='needs_review' WHERE id=?", (seeded["job_id"],))
            conn.execute("UPDATE publish_job_targets SET status='needs_review', schedule_at=?, next_attempt_at=?, error_category='ambiguous', delivery_state='unknown', last_error=?, checkpoint_json=?, lease_token=? WHERE job_id=?",
                ("2099-12-12T09:00:00", "2099-12-12T09:05:00", "Verify existing receipt; token=private-target-token", json.dumps(checkpoint), "private-lease", seeded["job_id"]))
        with patch.object(runtime, "DB_PATH", self.db_path):
            job = self.client.get(f"/jobs/{seeded['job_id']}").get_json()["data"]
            calendar = self.client.get("/jobs/calendar?month=2099-12&status=needs_review").get_json()["data"]["items"][0]
        listing = self.client.get("/publish-entities?status=needs_review").get_json()["data"]["items"][0]
        detail = self.client.get(f"/publish-entities/mg-{seeded['media_group_id']}").get_json()["data"]
        targets = [job["targets"][0], listing["jobs"][0]["targets"][0], detail["jobs"][0]["targets"][0], calendar]
        fields = ("nextAttemptAt", "errorCategory", "deliveryState", "lastError", "readiness", "retryEligibility", "checkpoint", "providerReceipt")
        for target in targets:
            with self.subTest(route_target=target.get("id", target.get("targetId"))):
                self.assertEqual({key: target[key] for key in fields}, {key: targets[0][key] for key in fields})
                self.assertFalse(target["retryEligibility"]["allowed"])
                self.assertEqual(target["readiness"]["partProgress"], {"completeParts": 1, "totalParts": 2})
                self.assertEqual(set(target["providerReceipt"]), {"provider", "id", "status", "submitted_at"})
                rendered = json.dumps(target)
                for private in ("private-request", "private-response", "private-config", "private-signature", "private-progress", "private-readiness", "private-checkpoint", "private-lease", "private-error-token", "private-target-token", "/private/local", "checkpoint_json", "lease_token"):
                    self.assertNotIn(private, rendered)
        self.assertNotIn("private-campaign-key", json.dumps(listing))
        campaign = self.client.get(f"/campaigns/{seeded['campaign_id']}").get_json()["data"]
        self.assertEqual(campaign["last_error"], campaign["lastError"])
        self.assertNotIn("private-campaign-key", campaign["lastError"])
        self.assertIn("Account needs reconnect", campaign["lastError"])

    def test_cancel_response_contains_authoritative_targets_and_retained_receipts(self) -> None:
        profile_id = self._profile()
        account_id = self._account(profile_id)
        seeded = self._seed_campaign_entity(account_id=account_id, profile_id=profile_id)
        receipt = {"provider": "twitter", "id": "accepted-receipt", "status": "processing"}
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            for index, (status, delivery_state) in enumerate((("awaiting_delivery", "awaiting_delivery"), ("succeeded", "delivered"), ("failed", "not_sent")), start=1):
                conn.execute("INSERT INTO publish_job_targets (job_id, account_ref, file_ref, status, delivery_state, checkpoint_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (seeded["job_id"], f"account:{account_id + index}", f"campaign_post:{seeded['post_id']}", status, delivery_state, json.dumps({"provider_receipt": receipt}) if status == "awaiting_delivery" else "{}"))
            conn.execute("UPDATE publish_jobs SET status='running', total_targets=4, completed_targets=1, failed_targets=1 WHERE id=?", (seeded["job_id"],))
        with patch.object(self.sau_backend.job_runtime, "DB_PATH", self.db_path):
            response = self.client.post(f"/jobs/{seeded['job_id']}/cancel")
            self.assertEqual(response.status_code, 200)
            body = response.get_json()["data"]
            authoritative = self.client.get(f"/jobs/{seeded['job_id']}").get_json()["data"]
        self.assertEqual(body["status"], "cancelled")
        self.assertEqual(body["targets"], authoritative["targets"])
        self.assertEqual([target["status"] for target in body["targets"]], ["cancelled", "cancelled", "succeeded", "failed"])
        self.assertEqual(body["targets"][1]["deliveryState"], "awaiting_delivery")
        self.assertEqual(body["targets"][1]["providerReceipt"], receipt)
        self.assertFalse(body["targets"][1]["retryEligibility"]["allowed"])

    def test_201_entities_paginate_at_existing_200_cap(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            for index in range(201):
                cursor = conn.execute("INSERT INTO publish_jobs (idempotency_key, platform, payload_json, status, total_targets) VALUES (?, 'twitter', ?, 'pending', 1)",
                    (f"entity-page-{index}", json.dumps({"title": f"Page {index}"})))
                conn.execute("INSERT INTO publish_job_targets (job_id, account_ref, file_ref, status) VALUES (?, 'account:7', 'legacy.mp4', 'pending')", (cursor.lastrowid,))
        first = self.client.get("/publish-entities?limit=999").get_json()["data"]
        second = self.client.get("/publish-entities?limit=200&offset=200").get_json()["data"]
        self.assertEqual(first["limit"], 200)
        self.assertEqual(first["total"], 201)
        self.assertEqual(len(first["items"]), 200)
        self.assertTrue(first["hasMore"])
        self.assertEqual(second["offset"], 200)
        self.assertEqual(len(second["items"]), 1)
        self.assertFalse(second["hasMore"])
        self.assertEqual(len({item["entityId"] for item in first["items"] + second["items"]}), 201)


if __name__ == "__main__":
    unittest.main()
