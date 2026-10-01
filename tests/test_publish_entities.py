"""HTTP tests for the Publish Center content-entity read view.

Covers ``GET /publish-entities`` (grouping by shared media group + legacy job
fallback, filters, pagination, safe media previews) and
``GET /publish-entities/<entity_id>``.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import types
import unittest
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
        with sqlite3.connect(self.db_path) as conn:
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
        """A media group + campaign + post + job, as Publish Center produces."""
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

        job = job_runtime.enqueue_job(
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
        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
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


if __name__ == "__main__":
    unittest.main()
