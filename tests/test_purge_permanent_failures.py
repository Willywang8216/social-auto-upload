"""The permanent-failure classifier must never bury a retryable failure.

It cancels targets whose retry can never succeed, so a false positive loses a
publish. The tests below pin the protective direction: anything ambiguous, and
anything that a later attempt could fix (rate limits), must be left alone.
"""

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "purge_permanent_failures",
        REPO_ROOT / "scripts" / "purge_permanent_failures.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load()


class PermanentFailureClassificationTests(unittest.TestCase):
    def test_rate_limits_are_never_permanent(self):
        # A rate limit can succeed later, so cancelling would lose a publish.
        for message in (
            "RATELIMIT: Looks like you've been doing that a lot",
            "rate limited (HTTP 429) by https://muyuan.do",
            "HTTP 429 Too Many Requests",
        ):
            with self.subTest(message=message):
                self.assertIsNone(mod.classify(message))

    def test_media_gone_is_permanent(self):
        for message in (
            "MediaRestoreError: Generated artifact is missing",
            "PreparedPublishError: Telegram media file is missing or empty: /app/x",
            "PreparedPublishError: TikTok video artifact not found: /x",
            "FileNotFoundError: [Errno 2] No such file or directory: '/app/uploads/a.png'",
        ):
            with self.subTest(message=message):
                self.assertEqual(mod.classify(message), "media-unrecoverable")

    def test_platform_refusal_is_permanent(self):
        for message in (
            "PreparedPublishError: Reddit submit failed: SUBREDDIT_NOTALLOWED_BANNED",
            "SUBMIT_VALIDATION_LINK_WHITELIST",
            "NO_SELFS: This community doesn't allow text posts",
            "TikTok app is in development mode",
            "PreparedPublishError: No subreddit on this account can accept this content: "
            "r/NudistMen: does not accept explicit content",
        ):
            with self.subTest(message=message):
                self.assertEqual(mod.classify(message), "platform-refused")

    def test_destination_incompatible_is_permanent(self):
        # An image-only campaign sent to YouTube can never become a video.
        self.assertEqual(
            mod.classify(
                "PreparedPublishError: YouTube publish requires a local video artifact"
            ),
            "destination-incompatible",
        )

    def test_content_guard_is_permanent(self):
        self.assertEqual(
            mod.classify("[content-guard] placeholder/generic copy"),
            "content-permanent",
        )

    def test_missing_account_is_permanent(self):
        self.assertEqual(mod.classify("LookupError: Account not found: id=87"), "account-gone")

    def test_an_unknown_error_is_left_alone(self):
        # Anything not confidently permanent must be reported, not cancelled.
        for message in (
            "some brand new failure",
            "",
            "TypeError: get_browser_options() takes 0 positional arguments",
            "OSError: disk full",
        ):
            with self.subTest(message=message):
                self.assertIsNone(mod.classify(message))

    def test_a_rate_limit_wins_over_a_permanent_marker_in_the_same_text(self):
        # Defensive: a message mentioning both must not be cancelled.
        self.assertIsNone(
            mod.classify("MediaRestoreError ... but retry later: rate limited 429")
        )


class PendingMediaClassificationTests(unittest.TestCase):
    """A pending target is cancelled only when no artifact can be recovered."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE file_records (id INTEGER PRIMARY KEY, file_path TEXT, "
            "storage_key TEXT, storage_backend_id INTEGER, storage_cdn_url TEXT)"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _payload(self, **artifact):
        entry = {
            "local_path": "/app/nonexistent.mp4",
            "public_url": "https://socialupload.iamwillywang.com/getFile?filename=nonexistent.mp4",
            "remote_path": None,
            "source_file_record_id": 1187,
        }
        entry.update(artifact)
        return {"artifacts": [entry]}

    def test_dead_url_with_no_route_is_unrecoverable(self):
        with patch.object(mod, "_url_is_live", return_value=False):
            self.assertEqual(
                mod.classify_pending_target(
                    self.conn, self._payload(), set(), verify_urls=True
                ),
                "media-unrecoverable",
            )

    def test_live_url_is_recoverable(self):
        with patch.object(mod, "_url_is_live", return_value=True):
            self.assertIsNone(
                mod.classify_pending_target(
                    self.conn, self._payload(), set(), verify_urls=True
                )
            )

    def test_unverifiable_url_is_left_alone(self):
        # verify_urls off means a URL cannot be proven dead - never cancel.
        self.assertIsNone(
            mod.classify_pending_target(
                self.conn, self._payload(), set(), verify_urls=False
            )
        )

    def test_network_error_probe_is_left_alone(self):
        with patch.object(mod, "_url_is_live", return_value=None):
            self.assertIsNone(
                mod.classify_pending_target(
                    self.conn, self._payload(), set(), verify_urls=True
                )
            )

    def test_local_file_is_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "here.mp4"
            path.write_bytes(b"x")
            with patch.object(mod, "_url_is_live", return_value=False):
                self.assertIsNone(
                    mod.classify_pending_target(
                        self.conn,
                        self._payload(local_path=str(path)),
                        set(),
                        verify_urls=True,
                    )
                )

    def test_drive_object_is_recoverable(self):
        index = {"videoFile/_inbox_cache/demo.mp4"}
        with patch.object(mod, "_url_is_live", return_value=False):
            self.assertIsNone(
                mod.classify_pending_target(
                    self.conn,
                    self._payload(
                        local_path="/home/will/social-auto-upload/videoFile/_inbox_cache/demo.mp4"
                    ),
                    index,
                    verify_urls=True,
                )
            )

    def test_storage_route_is_recoverable(self):
        self.conn.execute(
            "INSERT INTO file_records (id, file_path, storage_key, storage_backend_id) "
            "VALUES (1187, 'nonexistent.mp4', 'key', 1)"
        )
        self.conn.commit()
        with patch.object(mod, "_url_is_live", return_value=False):
            self.assertIsNone(
                mod.classify_pending_target(
                    self.conn, self._payload(), set(), verify_urls=True
                )
            )

    def test_remote_path_is_recoverable(self):
        with patch.object(mod, "_url_is_live", return_value=False):
            self.assertIsNone(
                mod.classify_pending_target(
                    self.conn,
                    self._payload(remote_path="sau/videoFile/kept.mp4"),
                    set(),
                    verify_urls=True,
                )
            )

    def test_no_artifacts_is_left_alone(self):
        self.assertIsNone(
            mod.classify_pending_target(self.conn, {"artifacts": []}, set(), verify_urls=True)
        )

    def test_a_partially_recoverable_payload_is_left_alone(self):
        payload = {
            "artifacts": [
                {
                    "local_path": "/app/nonexistent.mp4",
                    "public_url": "https://example/dead",
                    "remote_path": None,
                    "source_file_record_id": 1187,
                },
                {
                    "local_path": "/app/also-gone.mp4",
                    "public_url": None,
                    "remote_path": None,
                    "source_file_record_id": None,
                },
            ]
        }

        def fake_live(url, **_kwargs):
            return url == "https://example/dead"

        with patch.object(mod, "_url_is_live", side_effect=fake_live):
            self.assertIsNone(
                mod.classify_pending_target(
                    self.conn, payload, set(), verify_urls=True
                )
            )


if __name__ == "__main__":
    unittest.main()
