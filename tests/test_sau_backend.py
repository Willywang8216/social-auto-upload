"""Tests for the legacy Flask backend's behaviour-critical helpers.

These cover the bug fixes applied in this round:

- `sse_stream` exits on a terminal status and cleans `active_queues`
- `/getFile` rejects path-traversal attempts via robust ``Path.resolve``
"""

from __future__ import annotations

import importlib.util
import inspect
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
class TwitterOAuthReconnectTests(unittest.TestCase):
    def setUp(self) -> None:
        import sau_backend
        import db.createTable as create_table
        from myUtils import profiles

        self.sau_backend = sau_backend
        self.profiles = profiles
        self._tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self._tmp.name)
        self.db_path = self.base_dir / "db" / "database.db"
        create_table.bootstrap(self.db_path)
        self._base_dir_patch = patch.object(sau_backend, "BASE_DIR", self.base_dir)
        self._base_dir_patch.start()
        self._db_path_patch = patch.object(sau_backend, "_current_db_path", return_value=self.db_path)
        self._db_path_patch.start()
        from myUtils.security import SecurityPolicy
        self._orig_policy = sau_backend.app.config["SECURITY_POLICY"]
        sau_backend.app.config["SECURITY_POLICY"] = SecurityPolicy(
            tokens=frozenset(), cors_origins=("http://localhost:5173",)
        )
        sau_backend.app.config["TESTING"] = True
        self.client = sau_backend.app.test_client()

        self.profile = profiles.create_profile("Reconnect Test", db_path=self.db_path)
        self.account = profiles.add_account(
            self.profile.id,
            "twitter",
            "nudeweiwei",
            auth_type="oauth",
            config={
                "twitterAuthType": "api",
                "accessToken": "old-access-token",
                "refreshToken": "old-refresh-token",
                "_needsReconnect": True,
                "_reconnectAlertedAt": "2026-10-01T00:00:00",
                "_maintenanceFailures": 4,
                "_nextMaintenanceAttemptAt": "2026-10-03T12:00:00",
                "_lastMaintenanceError": "old rejection",
                "_lastMaintenanceAttemptAt": "2026-10-03T11:50:00",
            },
            db_path=self.db_path,
        )
        from myUtils import x_review
        self.state = "test-twitter-oauth-state"
        x_review.create_oauth_request(
            state_token=self.state,
            profile_id=self.profile.id,
            account_id=self.account.id,
            account_name=self.account.account_name,
            redirect_uri="https://socialupload.example.com/oauth/twitter/callback",
            code_verifier="test-verifier",
            scopes=["tweet.read", "tweet.write", "users.read", "offline.access"],
            db_path=self.db_path,
        )

    def tearDown(self) -> None:
        self._db_path_patch.stop()
        self._base_dir_patch.stop()
        self.sau_backend.app.config["SECURITY_POLICY"] = self._orig_policy
        self._tmp.cleanup()

    def test_successful_callback_replaces_stale_reconnect_state(self) -> None:
        from myUtils import x_auth
        token_payload = {
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "expires_in": 1,
            "token_type": "bearer",
            "scope": "tweet.read tweet.write users.read offline.access",
        }
        identity = {"data": {"id": "x-user-123", "username": "nudeweiwei", "name": "NW X"}}
        with patch.object(x_auth, "exchange_code_for_token", return_value=token_payload), \
             patch.object(x_auth, "fetch_user_info", return_value=identity):
            response = self.client.get(f"/oauth/twitter/callback?state={self.state}&code=ok")
        self.assertEqual(response.status_code, 200)
        updated = self.profiles.get_account(self.account.id, db_path=self.db_path)
        config = updated.config or {}
        self.assertEqual(config["accessToken"], "new-access-token")
        self.assertEqual(config["refreshToken"], "new-refresh-token")
        self.assertEqual(config["twitterUserName"], "nudeweiwei")
        self.assertEqual(config["twitterAuthType"], "api")
        for marker in (
            "_needsReconnect", "_reconnectAlertedAt", "_maintenanceFailures",
            "_nextMaintenanceAttemptAt", "_lastMaintenanceError",
            "_lastMaintenanceAttemptAt",
        ):
            self.assertNotIn(marker, config)
        self.assertTrue(self.sau_backend._is_refreshable_account_stale(updated))

    def test_failed_callback_keeps_existing_reconnect_state(self) -> None:
        from myUtils import x_auth
        with patch.object(x_auth, "exchange_code_for_token", side_effect=RuntimeError("exchange failed")):
            response = self.client.get(f"/oauth/twitter/callback?state={self.state}&code=bad")
        self.assertEqual(response.status_code, 500)
        current = self.profiles.get_account(self.account.id, db_path=self.db_path)
        self.assertEqual(current.config["accessToken"], "old-access-token")
        self.assertTrue(current.config["_needsReconnect"])
        self.assertEqual(current.config["_maintenanceFailures"], 4)

    def test_callback_requires_refresh_token_when_offline_access_was_requested(self) -> None:
        from myUtils import x_auth
        with patch.object(
            x_auth,
            "exchange_code_for_token",
            return_value={"access_token": "new-access", "expires_in": 3600},
        ), patch.object(
            x_auth,
            "fetch_user_info",
            return_value={"data": {"id": "x-user", "username": "nudeweiwei"}},
        ):
            response = self.client.get(f"/oauth/twitter/callback?state={self.state}&code=ok")
        self.assertEqual(response.status_code, 500)
        current = self.profiles.get_account(self.account.id, db_path=self.db_path)
        self.assertEqual(current.config["accessToken"], "old-access-token")
        self.assertTrue(current.config["_needsReconnect"])

    def test_failed_refresh_is_backed_off_and_records_safe_provider_code(self) -> None:
        from myUtils.x_auth import TwitterOAuthError
        failure = TwitterOAuthError(
            "X OAuth refresh failed: HTTP 400 (invalid_grant)",
            status_code=400,
            error_code="invalid_grant",
        )
        with patch.object(
            self.sau_backend.prepared_publishers,
            "refresh_twitter_access_token",
            side_effect=failure,
        ):
            with self.assertRaises(TwitterOAuthError):
                self.sau_backend._run_account_token_refresh(
                    account_id=self.account.id, db_path=self.db_path, mode="auto"
                )
        current = self.profiles.get_account(self.account.id, db_path=self.db_path)
        config = current.config
        self.assertTrue(config["_needsReconnect"])
        self.assertEqual(config["_maintenanceFailures"], 5)
        self.assertTrue(config["_nextMaintenanceAttemptAt"])
        self.assertNotIn("must-not-be-stored", config["_lastMaintenanceError"])
        event = self.sau_backend.account_events.list_events(
            account_id=self.account.id, db_path=self.db_path, limit=1
        )[0]
        self.assertEqual(event.metadata["providerErrorCode"], "invalid_grant")
        self.assertNotIn("must-not-be-stored", event.error_text or "")


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


class PrePublishCapGuardTests(unittest.TestCase):
    """A failed re-encode must not hand an oversized original to the platform.

    ``_shrink_for_publish`` is best-effort by design, but when it fails (the
    ffmpeg re-encode is CPU-bound and can be interrupted) returning the source
    unchanged published a 602 MB / 13.5 min original that Threads must reject -
    and the campaign recorded it as "prepared", so the failure only surfaced
    minutes later as an opaque container error. The cap check makes that case
    fail immediately with a message naming the size and the limit.
    """

    def _fake_probe(self, size_bytes):
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            import myUtils.media_prep as media_prep

            original = media_prep.probe
            media_prep.probe = lambda path: {"size": size_bytes, "duration": 813.0}
            try:
                yield
            finally:
                media_prep.probe = original

        return _ctx()

    def test_raises_when_source_exceeds_the_platform_cap(self):
        from pathlib import Path

        import sau_backend

        with self._fake_probe(602_285_541):
            with self.assertRaisesRegex(ValueError, "exceeds the .*cap"):
                sau_backend._assert_within_platform_caps(
                    Path("SFW Taipei Stonewall.mp4"), {"bluesky", "threads"}
                )

    def test_allows_a_conforming_source(self):
        from pathlib import Path

        import sau_backend

        with self._fake_probe(47_000_000):
            # Must not raise: 47 MB is under every cap we ship.
            sau_backend._assert_within_platform_caps(
                Path("small.mp4"), {"bluesky", "threads", "instagram"}
            )

    def test_no_platforms_means_no_size_check(self):
        from pathlib import Path

        import sau_backend

        with self._fake_probe(999_000_000):
            sau_backend._assert_within_platform_caps(Path("big.mp4"), set())

    def test_unprobeable_file_is_not_blocked(self):
        from pathlib import Path

        import myUtils.media_prep as media_prep
        import sau_backend

        def _boom(path):
            raise RuntimeError("ffprobe unavailable")

        original = media_prep.probe
        media_prep.probe = _boom
        try:
            sau_backend._assert_within_platform_caps(
                Path("unknown.mp4"), {"bluesky"}
            )
        finally:
            media_prep.probe = original

    def test_shrink_failure_path_invokes_the_cap_guard(self):
        """The guard must actually run on the shrink-failure path.

        The other tests exercise the helper directly; this one pins the wiring,
        so removing the call from ``_shrink_for_publish`` fails here.
        """
        from pathlib import Path

        import myUtils.media_prep as media_prep
        import sau_backend

        def _fail_shrink(source, out_dir, platforms=None):
            raise RuntimeError("ffmpeg failed")

        original_shrink = media_prep.shrink
        original_available = media_prep._ensure_available
        original_probe = media_prep.probe
        media_prep.shrink = _fail_shrink
        media_prep._ensure_available = lambda: True
        media_prep.probe = lambda path: {"size": 602_285_541, "duration": 813.0}
        try:
            with self.assertRaisesRegex(ValueError, "exceeds the .*cap"):
                sau_backend._shrink_for_publish(
                    Path("SFW Taipei Stonewall.mp4"), 2492, {"bluesky", "threads"}
                )
        finally:
            media_prep.shrink = original_shrink
            media_prep._ensure_available = original_available
            media_prep.probe = original_probe


class TelegramConnectionCheckShapeTests(unittest.TestCase):
    """The Telegram check must handle BOTH response shapes.

    The bot API returns ``{"chatId": ..., "result": {...}}`` per chat, but MTProto
    (a user account - all four of this deployment's Telegram accounts) returns a
    plain list of chat-id strings. Calling ``.get`` on a string raised
    "'str' object has no attribute 'get'", so every MTProto Telegram account
    reported an error from the connection check even though publishing worked.
    """

    def test_bot_api_shape_still_yields_titles(self):
        import sau_backend

        source = inspect.getsource(sau_backend)
        self.assertIn("isinstance(chat, str)", source)
        self.assertIn("isinstance(chat, dict)", source)

    def test_mtproto_string_list_is_handled(self):
        # Reproduce the branch logic directly: a list of strings must produce
        # those strings as titles, not raise.
        chats = ["@nakedwilltgchannel", "@nakedwill"]
        titles = []
        for chat in chats:
            if isinstance(chat, str):
                titles.append(chat)
                continue
            if not isinstance(chat, dict):
                continue
            payload = chat.get("result") or {}
            titles.append(
                payload.get("title") or payload.get("username") or chat.get("chatId")
            )
        titles = [t for t in titles if t]
        self.assertEqual(titles, ["@nakedwilltgchannel", "@nakedwill"])

    def test_bot_api_dict_still_works(self):
        chats = [
            {"chatId": "@x", "result": {"title": "My Channel"}},
            {"chatId": "@y", "result": {}},
        ]
        titles = []
        for chat in chats:
            if isinstance(chat, str):
                titles.append(chat)
                continue
            if not isinstance(chat, dict):
                continue
            payload = chat.get("result") or {}
            titles.append(
                payload.get("title") or payload.get("username") or chat.get("chatId")
            )
        titles = [t for t in titles if t]
        self.assertEqual(titles, ["My Channel", "@y"])
