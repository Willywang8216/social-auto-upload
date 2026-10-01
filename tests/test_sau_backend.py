"""Tests for the legacy Flask backend's behaviour-critical helpers.

These cover the bug fixes applied in this round:

- `sse_stream` exits on a terminal status and cleans `active_queues`
- `/getFile` rejects path-traversal attempts via robust ``Path.resolve``
"""

from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from queue import Queue
from pathlib import Path
from unittest.mock import patch


flask_available = importlib.util.find_spec("flask") is not None


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class SseStreamTests(unittest.TestCase):
    def test_exits_on_terminal_success_payload(self) -> None:
        from sau_backend import active_queues, sse_stream

        queue = Queue()
        queue.put("data:image/png;base64,abc")
        queue.put("200")
        active_queues["t1"] = queue

        out = list(sse_stream(queue, login_id="t1"))
        self.assertEqual(out[-1], "data: 200\n\n")
        self.assertEqual(len(out), 2)
        self.assertNotIn("t1", active_queues)

    def test_exits_on_terminal_failure_payload(self) -> None:
        from sau_backend import active_queues, sse_stream

        queue = Queue()
        queue.put("500")
        active_queues["t2"] = queue

        out = list(sse_stream(queue, login_id="t2"))
        self.assertEqual(out, ["data: 500\n\n"])
        self.assertNotIn("t2", active_queues)

    def test_idle_timeout_emits_500(self) -> None:
        from sau_backend import sse_stream

        queue = Queue()
        # Use a tiny idle timeout so the test does not block.
        out = list(sse_stream(queue, login_id="t3", idle_timeout=0.1))
        self.assertEqual(out, ["data: 500\n\n"])


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class GetFileTraversalGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        from sau_backend import app

        self.client = app.test_client()

    def test_dotdot_traversal_rejected(self) -> None:
        response = self.client.get("/getFile?filename=../conf.py")
        self.assertEqual(response.status_code, 400)

    def test_absolute_path_rejected(self) -> None:
        response = self.client.get("/getFile?filename=/etc/passwd")
        self.assertEqual(response.status_code, 400)

    def test_missing_filename_rejected(self) -> None:
        response = self.client.get("/getFile")
        self.assertEqual(response.status_code, 400)


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class GetFileServingTests(unittest.TestCase):
    """``/getFile`` must serve media that lives in a subdirectory.

    The route resolved the requested path correctly and then handed only the
    BASENAME to send_from_directory, so anything under _library/, _photos/,
    _batch*/ or _inbox_cache/ 404'd even though it was on disk — the media
    library thumbnails in the queue and calendar were all broken because of it.
    """

    def setUp(self) -> None:
        import sau_backend

        self.sau_backend = sau_backend
        self._tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self._tmp.name)
        (self.base_dir / "videoFile").mkdir(parents=True, exist_ok=True)
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

    def _write(self, relative: str, payload: bytes) -> Path:
        target = self.base_dir / "videoFile" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return target

    def test_nested_file_is_served(self) -> None:
        # Percent/plus-encoded exactly as the browser sends it (url_for uses
        # quote_plus, so a space arrives as '+').
        from urllib.parse import urlencode

        self._write("_library/NW/clip with spaces.jpg", b"nested-bytes")
        query = urlencode({"filename": "_library/NW/clip with spaces.jpg"})
        response = self.client.get(f"/getFile?{query}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"nested-bytes")

    def test_deeply_nested_file_is_served(self) -> None:
        self._write("_batch1/nsfw/deep. mp4", b"deep-bytes")
        response = self.client.get("/getFile?filename=_batch1/nsfw/deep.%20mp4")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"deep-bytes")

    def test_root_level_file_still_served(self) -> None:
        self._write("plain.mp4", b"root-bytes")
        response = self.client.get("/getFile?filename=plain.mp4")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"root-bytes")

    def test_traversal_out_of_a_subdirectory_is_still_rejected(self) -> None:
        (self.base_dir / "conf.py").write_bytes(b"secret")
        response = self.client.get("/getFile?filename=_library/../../conf.py")
        self.assertEqual(response.status_code, 400)

    def test_missing_nested_file_still_404s(self) -> None:
        (self.base_dir / "videoFile" / "_library").mkdir(parents=True, exist_ok=True)
        response = self.client.get("/getFile?filename=_library/nope.jpg")
        self.assertEqual(response.status_code, 404)


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class LegacyDbBootstrapEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        import sau_backend

        self._tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self._tmp.name)
        (self.base_dir / "db").mkdir(parents=True, exist_ok=True)
        self._base_dir_patch = patch.object(sau_backend, "BASE_DIR", self.base_dir)
        self._base_dir_patch.start()

        from myUtils.security import SecurityPolicy
        self._orig_policy = sau_backend.app.config["SECURITY_POLICY"]
        sau_backend.app.config["SECURITY_POLICY"] = SecurityPolicy(tokens=frozenset(), cors_origins=("http://localhost:5173",))
        sau_backend.app.config["TESTING"] = True
        self.client = sau_backend.app.test_client()

    def tearDown(self) -> None:
        self._base_dir_patch.stop()
        if hasattr(self, "_orig_policy"):
            import sau_backend
            sau_backend.app.config["SECURITY_POLICY"] = self._orig_policy
        self._tmp.cleanup()

    def _legacy_db_path(self) -> Path:
        return self.base_dir / "db" / "database.db"

    def _table_names(self) -> set[str]:
        with sqlite3.connect(self._legacy_db_path()) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        return {row[0] for row in rows}

    def test_get_accounts_bootstraps_fresh_environment(self) -> None:
        response = self.client.get("/getAccounts")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"], [])
        self.assertTrue(self._legacy_db_path().exists())
        self.assertTrue({"user_info", "file_records"}.issubset(self._table_names()))

    def test_get_files_bootstraps_fresh_environment(self) -> None:
        response = self.client.get("/getFiles")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"], [])
        self.assertTrue(self._legacy_db_path().exists())
        self.assertTrue({"user_info", "file_records"}.issubset(self._table_names()))

    def test_get_accounts_recovers_from_empty_sqlite_file(self) -> None:
        db_path = self._legacy_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(db_path).close()

        response = self.client.get("/getAccounts")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"], [])
        self.assertTrue({"user_info", "file_records"}.issubset(self._table_names()))

    def test_delete_account_supports_structured_profile_accounts(self) -> None:
        self.client.get("/getAccounts")

        cookie_dir = self.base_dir / "cookies" / "twitter" / "brand"
        cookie_dir.mkdir(parents=True, exist_ok=True)
        cookie_path = cookie_dir / "main.json"
        cookie_path.write_text("{}")

        with sqlite3.connect(self._legacy_db_path()) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO profiles (name, slug, description, settings_json) VALUES (?, ?, ?, ?)",
                ("Brand", "brand", "", "{}"),
            )
            profile_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO accounts (profile_id, platform, account_name, cookie_path, auth_type, config_json, enabled, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (profile_id, "twitter", "main", str(cookie_path), "cookie", "{}", 1, 1),
            )
            account_id = cursor.lastrowid
            conn.commit()

        response = self.client.get(f"/deleteAccount?id={account_id}")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(cookie_path.exists())

    def test_download_cookie_supports_structured_account_paths(self) -> None:
        cookie_dir = self.base_dir / "cookies" / "twitter" / "brand"
        cookie_dir.mkdir(parents=True, exist_ok=True)
        cookie_path = cookie_dir / "main.json"
        cookie_path.write_text('{"cookies": [], "origins": []}')

        response = self.client.get(
            f"/downloadCookie?filePath={cookie_path.as_posix()}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"cookies"', response.data)


if __name__ == "__main__":
    unittest.main()
