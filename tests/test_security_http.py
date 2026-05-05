"""HTTP-level tests for the bearer-token gate and /uploadCookie validation."""

from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db.createTable as create_table


flask_available = importlib.util.find_spec("flask") is not None


def _seed_account(db_path: Path, *, file_path: str = "alice.json") -> int:
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO user_info (type, filePath, userName, status) VALUES (?, ?, ?, ?)",
            (3, file_path, "alice", 1),
        )
        conn.commit()
        return cur.lastrowid


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class AuthGateTests(unittest.TestCase):
    def setUp(self) -> None:
        from conf import BASE_DIR
        from myUtils import jobs as job_runtime
        from myUtils.security import SecurityPolicy
        from sau_backend import app

        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "auth.db"
        create_table.bootstrap(self.db_path)
        self._patch = patch.object(job_runtime, "DB_PATH", self.db_path)
        self._patch.start()

        # /getAccounts and friends still read from the legacy DB at
        # BASE_DIR/db/database.db. Make sure that file exists with the
        # expected schema so post-auth requests don't 500.
        self._legacy_db = Path(BASE_DIR) / "db" / "database.db"
        self._legacy_db.parent.mkdir(parents=True, exist_ok=True)
        create_table.bootstrap(self._legacy_db)

        self._previous_policy = app.config["SECURITY_POLICY"]
        app.config["SECURITY_POLICY"] = SecurityPolicy(
            tokens=frozenset({"top-secret"}),
            cors_origins=("http://localhost:5173",),
        )
        app.config["TESTING"] = True
        self.app = app
        self.client = app.test_client()

    def tearDown(self) -> None:
        self._patch.stop()
        self.app.config["SECURITY_POLICY"] = self._previous_policy
        self._tmp.cleanup()

    def test_protected_endpoint_rejects_missing_token(self) -> None:
        response = self.client.get("/getAccounts")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["msg"], "unauthorized")

    def test_protected_endpoint_rejects_wrong_token(self) -> None:
        response = self.client.get(
            "/getAccounts",
            headers={"Authorization": "Bearer not-the-real-one"},
        )
        self.assertEqual(response.status_code, 401)

    def test_protected_endpoint_accepts_correct_token(self) -> None:
        response = self.client.get(
            "/getAccounts",
            headers={"Authorization": "Bearer top-secret"},
        )
        self.assertEqual(response.status_code, 200)

    def test_public_paths_skip_the_gate(self) -> None:
        # The static SPA shell paths and TikTok callback/webhook endpoints must remain reachable.
        for path in ("/favicon.ico", "/vite.svg", "/oauth/tiktok/callback", "/oauth/reddit/callback", "/webhooks/tiktok"):
            response = self.client.get(path)
            self.assertNotEqual(
                response.status_code,
                401,
                f"{path} should not require auth",
            )

    def test_login_sse_rejects_missing_query_token(self) -> None:
        # /login without a token must 401 before the SSE worker spins up.
        # We deliberately do NOT exercise the success path here because the
        # SSE worker would try to launch a real browser; that's covered by
        # the unit-level test_query_only_consulted_for_sse case.
        response = self.client.get("/login?type=99&id=alice")
        self.assertEqual(response.status_code, 401)

    def test_login_sse_rejects_wrong_query_token(self) -> None:
        response = self.client.get("/login?type=99&id=alice&auth=wrong")
        self.assertEqual(response.status_code, 401)

    def test_whoami_in_protected_mode(self) -> None:
        response = self.client.get(
            "/whoami",
            headers={"Authorization": "Bearer top-secret"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()["data"]
        self.assertFalse(body["openMode"])
        self.assertTrue(body["authenticated"])


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class UploadCookieValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        from myUtils import jobs as job_runtime
        from sau_backend import app

        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "validate.db"
        create_table.bootstrap(self.db_path)
        self._patch = patch.object(job_runtime, "DB_PATH", self.db_path)
        self._patch.start()
        # The /uploadCookie endpoint reads the legacy DB at BASE_DIR/db,
        # which is shared across tests. We seed an account row so the
        # endpoint can locate the destination path. A test-only base dir
        # would be cleaner; this is the smallest patch.
        from conf import BASE_DIR
        self.base_dir = Path(BASE_DIR)
        self.legacy_db = self.base_dir / "db" / "database.db"
        self.legacy_db.parent.mkdir(parents=True, exist_ok=True)
        create_table.bootstrap(self.legacy_db)
        self.account_id = _seed_account(self.legacy_db, file_path="alice.json")
        # Make sure the cookie destination doesn't survive between tests.
        self.cookie_target = self.base_dir / "cookiesFile" / "alice.json"
        self.cookie_target.parent.mkdir(parents=True, exist_ok=True)
        if self.cookie_target.exists():
            self.cookie_target.unlink()

        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self) -> None:
        self._patch.stop()
        if self.cookie_target.exists():
            self.cookie_target.unlink()
        # Clean up the seeded row.
        with sqlite3.connect(self.legacy_db) as conn:
            conn.execute("DELETE FROM user_info WHERE id = ?", (self.account_id,))
            conn.commit()
        self._tmp.cleanup()

    def _post_cookie(self, body: bytes):
        return self.client.post(
            "/uploadCookie",
            data={
                "id": str(self.account_id),
                "platform": "douyin",
                "file": (io.BytesIO(body), "alice.json"),
            },
            content_type="multipart/form-data",
        )

    def test_valid_cookie_is_accepted_and_persisted(self) -> None:
        body = json.dumps({"cookies": [{"name": "a", "value": "b"}]}).encode()
        response = self._post_cookie(body)
        self.assertEqual(response.status_code, 200)
        # File now on disk.
        self.assertTrue(self.cookie_target.exists())
        self.assertEqual(self.cookie_target.read_bytes(), body)

    def test_garbage_cookie_is_rejected_and_not_written(self) -> None:
        response = self._post_cookie(b"definitely not json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            self.cookie_target.exists(),
            "an invalid upload must not overwrite or create the cookie file",
        )

    def test_cookie_missing_required_keys_rejected(self) -> None:
        response = self._post_cookie(json.dumps({"hello": "world"}).encode())
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.cookie_target.exists())


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class TikTokWebhookEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        import sau_backend

        self._tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self._tmp.name)
        self._base_dir_patch = patch.object(sau_backend, "BASE_DIR", self.base_dir)
        self._base_dir_patch.start()
        sau_backend.app.config["TESTING"] = True
        self.client = sau_backend.app.test_client()

    def tearDown(self) -> None:
        self._base_dir_patch.stop()
        self._tmp.cleanup()

    def test_webhook_get_returns_ready(self) -> None:
        response = self.client.get('/webhooks/tiktok')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['data']['service'], 'tiktok-webhook')

    def test_webhook_post_logs_event_without_secret(self) -> None:
        with patch.dict('os.environ', {}, clear=False):
            response = self.client.post(
                '/webhooks/tiktok',
                data=b'{"event":"post.publish.publicly_available"}',
                headers={'Content-Type': 'application/json'},
            )
        self.assertEqual(response.status_code, 200)
        log_path = self.base_dir / 'logs' / 'webhooks' / 'tiktok-events.ndjson'
        self.assertTrue(log_path.exists())
        self.assertIn('post.publish.publicly_available', log_path.read_text(encoding='utf-8'))


if __name__ == "__main__":
    unittest.main()
