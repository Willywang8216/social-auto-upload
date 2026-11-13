"""HTTP-level tests for the /jobs endpoints in sau_backend."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import sqlite3
from contextlib import closing
import unittest
from pathlib import Path
from unittest.mock import patch

import db.createTable as create_table


flask_available = importlib.util.find_spec("flask") is not None


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class JobsHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "jobs_http.db"
        create_table.bootstrap(self.db_path)

        # Point the job runtime at our scratch DB for the duration of the test.
        from myUtils import jobs as job_runtime
        self._job_runtime = job_runtime
        self._patcher = patch.object(job_runtime, "DB_PATH", self.db_path)
        self._patcher.start()

        from sau_backend import app
        db_getter = patch("sau_backend._current_db_path", return_value=self.db_path)
        db_getter.start()
        self.addCleanup(db_getter.stop)
        from myUtils.security import SecurityPolicy
        self._orig_policy = app.config["SECURITY_POLICY"]
        app.config["SECURITY_POLICY"] = SecurityPolicy(tokens=frozenset(), cors_origins=("http://localhost:5173",))
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self) -> None:
        self._patcher.stop()
        if hasattr(self, "_orig_policy"):
            from sau_backend import app
            app.config["SECURITY_POLICY"] = self._orig_policy
        self._tmp.cleanup()

    def _post_job(self, body: dict):
        return self.client.post("/jobs", data=json.dumps(body),
                                content_type="application/json")

    def test_create_job_returns_queued(self) -> None:
        response = self._post_job({
            "type": 3,  # douyin
            "title": "hello",
            "fileList": ["v1.mp4", "v2.mp4"],
            "accountList": ["a1.json", "a2.json"],
            "tags": ["x"],
        })
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["code"], 200)
        self.assertEqual(body["data"]["platform"], "douyin")
        self.assertEqual(body["data"]["totalTargets"], 4)  # 2 files × 2 accounts
        self.assertEqual(body["data"]["status"], "pending")

    def test_create_job_is_idempotent_with_same_payload(self) -> None:
        body = {
            "type": 3,
            "title": "hello",
            "fileList": ["v1.mp4"],
            "accountList": ["a1.json"],
        }
        first = self._post_job(body).get_json()["data"]
        second = self._post_job(body).get_json()["data"]
        self.assertEqual(first["id"], second["id"])

    def test_create_job_rejects_empty_lists(self) -> None:
        response = self._post_job({"type": 3, "title": "x", "fileList": [], "accountList": []})
        self.assertEqual(response.status_code, 400)

    def test_create_job_rejects_unknown_platform(self) -> None:
        response = self._post_job({"type": 99, "title": "x",
                                   "fileList": ["v.mp4"], "accountList": ["a.json"]})
        self.assertEqual(response.status_code, 400)

    def test_create_twitter_job_normalises_alias_and_uses_one_target_per_account(self) -> None:
        response = self._post_job({
            "platform": "x",
            "title": "thread",
            "fileList": ["v1.mp4", "v2.mp4", "v3.mp4"],
            "accountList": ["a1.json", "a2.json"],
            "tags": ["news"],
        })
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["data"]["platform"], "twitter")
        self.assertEqual(body["data"]["totalTargets"], 2)
        self.assertEqual(
            body["data"]["payload"]["threadFileRefs"],
            ["v1.mp4", "v2.mp4", "v3.mp4"],
        )

        detail = self.client.get(f"/jobs/{body['data']['id']}").get_json()["data"]
        self.assertEqual([target["accountRef"] for target in detail["targets"]], ["a1.json", "a2.json"])
        self.assertTrue(all(target["fileRef"] == "v1.mp4" for target in detail["targets"]))

    def test_get_job_returns_targets(self) -> None:
        created = self._post_job({
            "type": 3,
            "title": "hello",
            "fileList": ["v1.mp4"],
            "accountList": ["a1.json", "a2.json"],
        }).get_json()["data"]
        response = self.client.get(f"/jobs/{created['id']}")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()["data"]
        self.assertEqual(len(body["targets"]), 2)
        self.assertEqual(body["targets"][0]["status"], "pending")

    def test_cancel_job(self) -> None:
        created = self._post_job({
            "type": 3,
            "title": "hello",
            "fileList": ["v1.mp4"],
            "accountList": ["a1.json"],
        }).get_json()["data"]
        response = self.client.post(f"/jobs/{created['id']}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["status"], "cancelled")

    def test_post_video_accepts_x_alias(self) -> None:
        with patch("sau_backend.post_video_twitter") as mocked:
            response = self.client.post(
                "/postVideo",
                data=json.dumps({
                    "type": "x",
                    "title": "thread",
                    "fileList": ["v1.mp4", "v2.mp4"],
                    "accountList": ["acct.json"],
                    "tags": ["topic"],
                }),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        mocked.assert_not_called()
        queued = response.get_json()["data"]
        self.assertEqual(queued["status"], "pending")
        self.assertFalse(queued["payload"]["publishReadiness"]["ready"])
        self.assertIn("not confirmed", response.get_json()["msg"])

    def test_post_video_batch_accepts_twitter_numeric_code(self) -> None:
        with patch("sau_backend.post_video_twitter") as mocked:
            response = self.client.post(
                "/postVideoBatch",
                data=json.dumps([
                    {
                        "type": 7,
                        "title": "thread",
                        "fileList": ["v1.mp4", "v2.mp4"],
                        "accountList": ["acct.json"],
                        "tags": ["topic"],
                    }
                ]),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        mocked.assert_not_called()
        queued = response.get_json()["data"]["jobs"]
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["status"], "pending")
        self.assertEqual(response.get_json()["data"]["failures"], [])

    # --- /jobs?limit edge cases (QA regression) ---

    def test_list_negative_limit_rejected(self) -> None:
        response = self.client.get("/jobs?limit=-5")
        self.assertEqual(response.status_code, 400)
        self.assertIn("limit", response.get_json()["msg"])

    def test_list_zero_limit_rejected(self) -> None:
        response = self.client.get("/jobs?limit=0")
        self.assertEqual(response.status_code, 400)

    def test_list_non_numeric_limit_rejected(self) -> None:
        response = self.client.get("/jobs?limit=five")
        self.assertEqual(response.status_code, 400)

    def test_list_oversized_limit_does_not_500(self) -> None:
        response = self.client.get("/jobs?limit=999999")
        self.assertEqual(response.status_code, 200)


    def _target(self):
        created = self._post_job({"type": 3, "title": "exact approved caption",
            "fileList": ["fixture.mp4"], "accountList": ["fixture.json"]}).get_json()["data"]
        return self._job_runtime.list_targets(created["id"], db_path=self.db_path)[0]

    def test_x_timer_is_preserved_in_durable_target(self):
        from datetime import datetime
        slot = datetime(2100, 1, 1, 4, 0)
        with patch("sau_backend.generate_schedule_time_next_day", return_value=[slot]):
            response = self._post_job({"platform": "x", "title": "approved",
                "fileList": ["fixture.mp4"], "accountList": ["fixture.json"], "enableTimer": True})
        self.assertEqual(response.status_code, 200)
        target = self._job_runtime.list_targets(response.get_json()["data"]["id"], db_path=self.db_path)[0]
        self.assertEqual(target.schedule_at, slot.isoformat())

    def test_x_weighted_legacy_title_is_rejected_before_admission(self):
        response = self._post_job({"platform": "twitter", "title": "😀" + "漢" * 140,
            "fileList": ["fixture.mp4"], "accountList": ["fixture.json"]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("282", response.get_json()["msg"])
        self.assertEqual(self._job_runtime.list_jobs(db_path=self.db_path), [])

    def test_cancel_accepted_receipt_then_retry_cannot_resubmit(self):
        target = self._target()
        receipt = {"provider": "fixture", "id": "accepted-original", "status": "accepted"}
        checkpoint = {"provider_receipt": receipt, "submission_started": True}
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE publish_job_targets SET status='awaiting_delivery',delivery_state='awaiting_delivery',"
                         "checkpoint_json=?,attempts=2 WHERE id=?", (json.dumps(checkpoint), target.id))
        response = self.client.post(f"/jobs/targets/{target.id}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["providerReceipt"], receipt)
        self.assertFalse(response.get_json()["data"]["retryEligibility"]["allowed"])
        before = self._job_runtime.get_target(target.id, db_path=self.db_path)
        retry = self.client.post(f"/jobs/targets/{target.id}/resubmit", json={})
        self.assertEqual(retry.status_code, 400)
        self.assertEqual(self._job_runtime.get_target(target.id, db_path=self.db_path), before)

    def test_calendar_preserves_prepared_payload_and_attempt_history(self):
        target = self._target()
        checkpoint = {"recovery_generation": "immutable-generation", "payload": {"message": "exact approved caption"}}
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE publish_job_targets SET checkpoint_json=?,attempts=2,last_error='preflight' WHERE id=?",
                         (json.dumps(checkpoint), target.id))
        response = self.client.post(f"/jobs/targets/{target.id}/reschedule", json={"scheduleAt": "2100-01-01T00:00:00Z"})
        self.assertEqual(response.status_code, 200)
        current = self._job_runtime.get_target(target.id, db_path=self.db_path)
        self.assertEqual(current.attempts, 2)
        self.assertEqual(current.last_error, "preflight")
        self.assertEqual(current.checkpoint, checkpoint)
        self.assertEqual(current.status, "pending")

    def test_failed_plain_retry_requires_plan_and_preserves_row(self):
        target = self._target()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE publish_job_targets SET status='failed',attempts=3,last_error='original cause' WHERE id=?", (target.id,))
        before = self._job_runtime.get_target(target.id, db_path=self.db_path)
        response = self.client.post(f"/jobs/targets/{target.id}/resubmit", json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("recovery plan", response.get_json()["msg"])
        self.assertEqual(self._job_runtime.get_target(target.id, db_path=self.db_path), before)

    def test_retry_plan_is_forwarded_to_authoritative_recovery(self):
        target = self._target()
        plan = {"target_id": target.id, "original_status": "failed", "expected_fingerprint": "fixture-only"}
        with patch("sau_backend.job_runtime.resubmit_target", return_value=target) as apply:
            response = self.client.post(f"/jobs/targets/{target.id}/resubmit", json={"recoveryPlan": plan})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(apply.call_args.kwargs["recovery_plan"], plan)
        self.assertIn("not yet confirmed", response.get_json()["msg"])

    def test_target_projection_carries_backend_eligibility_and_stage(self):
        target = self._target()
        response = self.client.get(f"/jobs/{target.job_id}").get_json()["data"]["targets"][0]
        self.assertFalse(response["retryEligibility"]["allowed"])
        self.assertIn("readiness", response)
        self.assertIn("checkpoint", response)

    def test_batch_invalid_item_never_triggers_provider_upload(self):
        with patch("sau_backend.post_video_DouYin") as upload:
            response = self.client.post("/postVideoBatch", json=[
                {"type": 3, "title": "approved", "fileList": ["fixture.mp4"], "accountList": ["fixture.json"]},
                {"type": 99, "title": "invalid", "fileList": ["fixture.mp4"], "accountList": ["fixture.json"]}])
        upload.assert_not_called()
        self.assertEqual(response.status_code, 207)
        self.assertEqual(len(response.get_json()["data"]["jobs"]), 1)
        self.assertEqual(len(response.get_json()["data"]["failures"]), 1)


if __name__ == "__main__":
    unittest.main()
