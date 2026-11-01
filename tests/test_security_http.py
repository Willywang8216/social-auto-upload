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
from contextlib import closing

import db.createTable as create_table


flask_available = importlib.util.find_spec("flask") is not None


def _seed_account(db_path: Path, *, file_path: str = "alice.json") -> int:
    with closing(sqlite3.connect(db_path)) as conn, conn:
        cur = conn.execute(
            "INSERT INTO user_info (type, filePath, userName, status) VALUES (?, ?, ?, ?)",
            (3, file_path, "alice", 1),
        )
        conn.commit()
        return cur.lastrowid


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class AuthGateTests(unittest.TestCase):
    def setUp(self) -> None:
        from myUtils import jobs as job_runtime
        from myUtils.security import SecurityPolicy
        from sau_backend import app

        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "auth.db"
        create_table.bootstrap(self.db_path)
        self._patch = patch.object(job_runtime, "DB_PATH", self.db_path)
        self._patch.start()

        self._env = patch.dict("os.environ", {"SAU_DB_PATH": str(self.db_path)})
        self._env.start()

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
        self._env.stop()
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
        # The static SPA shell paths, public policy pages, and OAuth/webhook endpoints must remain reachable.
        for path in (
            "/favicon.ico",
            "/vite.svg",
            "/privacy",
            "/terms",
            # Platform review teams fetch these while signed out; a 401 here
            # reads as "no reachable policy" and fails app verification.
            "/data-deletion",
            "/robots.txt",
            "/sitemap.xml",
            "/oauth/tiktok/callback",
            "/oauth/reddit/callback",
            "/oauth/youtube/callback",
            "/oauth/meta/callback",
            "/oauth/threads/callback",
            "/webhooks/tiktok",
        ):
            response = self.client.get(path)
            self.assertNotEqual(
                response.status_code,
                401,
                f"{path} should not require auth",
            )
            response.close()

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
        from myUtils.security import SecurityPolicy
        from sau_backend import app

        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "validate.db"
        create_table.bootstrap(self.db_path)
        self._patch = patch.object(job_runtime, "DB_PATH", self.db_path)
        self._patch.start()
        import sau_backend
        self.base_dir = Path(self._tmp.name)
        self.legacy_db = self.db_path
        self._env = patch.dict("os.environ", {"SAU_DB_PATH": str(self.db_path)})
        self._env.start()
        self._base = patch.object(sau_backend, "BASE_DIR", self.base_dir)
        self._base.start()
        self.account_id = _seed_account(self.legacy_db, file_path="alice.json")
        self.cookie_target = self.base_dir / "cookiesFile" / "alice.json"
        self.cookie_target.parent.mkdir(parents=True, exist_ok=True)

        self._previous_policy = app.config["SECURITY_POLICY"]
        app.config["SECURITY_POLICY"] = SecurityPolicy(tokens=frozenset(), cors_origins=("http://localhost:5173",))
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self) -> None:
        self._patch.stop()
        self._env.stop()
        self._base.stop()
        if hasattr(self, "_previous_policy"):
            from sau_backend import app
            app.config["SECURITY_POLICY"] = self._previous_policy
        if self.cookie_target.exists():
            self.cookie_target.unlink()
        # Clean up the seeded row.
        with closing(sqlite3.connect(self.legacy_db)) as conn, conn:
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
