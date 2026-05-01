"""HTTP-level tests for the /jobs endpoints in sau_backend."""

from __future__ import annotations

import importlib.util
import json
import tempfile
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
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self) -> None:
        self._patcher.stop()
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
        mocked.assert_called_once_with(
            "thread",
            ["v1.mp4", "v2.mp4"],
            ["topic"],
            ["acct.json"],
        )

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
        mocked.assert_called_once_with(
            "thread",
            ["v1.mp4", "v2.mp4"],
            ["topic"],
            ["acct.json"],
        )

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


if __name__ == "__main__":
    unittest.main()
