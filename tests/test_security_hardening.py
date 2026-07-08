"""Tests for security hardening: upload key sanitization, path traversal prevention, OAuth payload safety."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

if "conf" not in sys.modules:
    conf_module = types.ModuleType("conf")
    conf_module.BASE_DIR = str(Path(__file__).resolve().parent.parent)
    conf_module.DEBUG_MODE = True
    conf_module.LOCAL_CHROME_HEADLESS = True
    conf_module.LOCAL_CHROME_PATH = ""
    sys.modules["conf"] = conf_module

from sau_backend import (
    _safe_upload_filename,
    _is_valid_upload_key,
    _resolve_video_file_path_safely,
)


class SafeUploadFilenameTests(unittest.TestCase):
    """Tests for _safe_upload_filename."""

    def test_normal_filename(self):
        self.assertEqual(_safe_upload_filename("video.mp4"), "video.mp4")

    def test_spaces_replaced(self):
        result = _safe_upload_filename("my video.mp4")
        self.assertEqual(result, "my_video.mp4")

    def test_path_traversal_normalized(self):
        # secure_filename strips path components, leaving only the filename
        result = _safe_upload_filename("../../etc/passwd")
        self.assertNotIn("..", result)
        self.assertNotIn("/", result)

    def test_slash_normalized(self):
        # secure_filename strips slashes
        result = _safe_upload_filename("path/to/file.mp4")
        self.assertNotIn("/", result)

    def test_backslash_normalized(self):
        # secure_filename strips backslashes
        result = _safe_upload_filename("path\\to\\file.mp4")
        self.assertNotIn("\\", result)

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            _safe_upload_filename("")

    def test_only_special_chars_rejected(self):
        with self.assertRaises(ValueError):
            _safe_upload_filename("...")

    def test_dots_preserved(self):
        result = _safe_upload_filename("my.file.name.mp4")
        self.assertEqual(result, "my.file.name.mp4")

    def test_uuid_filename(self):
        result = _safe_upload_filename("abc-123_video.mp4")
        self.assertEqual(result, "abc-123_video.mp4")


class IsValidUploadKeyTests(unittest.TestCase):
    """Tests for _is_valid_upload_key."""

    def test_valid_uuid_key(self):
        self.assertTrue(_is_valid_upload_key("uploads/550e8400-e29b-41d4-a716-446655440000_video.mp4"))

    def test_rejects_path_traversal(self):
        self.assertFalse(_is_valid_upload_key("../../etc/passwd"))

    def test_rejects_absolute_path(self):
        self.assertFalse(_is_valid_upload_key("/etc/passwd"))

    def test_rejects_traversal_in_key(self):
        self.assertFalse(_is_valid_upload_key("uploads/../../../etc/passwd"))

    def test_rejects_backslash(self):
        self.assertFalse(_is_valid_upload_key("uploads\\abc\\file.mp4"))

    def test_rejects_empty(self):
        self.assertFalse(_is_valid_upload_key(""))

    def test_rejects_none(self):
        self.assertFalse(_is_valid_upload_key(None))

    def test_rejects_control_chars(self):
        self.assertFalse(_is_valid_upload_key("uploads/550e8400-e29b-41d4-a716-446655440000\x00file.mp4"))

    def test_rejects_no_underscore(self):
        self.assertFalse(_is_valid_upload_key("uploads/550e8400-e29b-41d4-a716-446655440000file.mp4"))

    def test_rejects_not_uuid(self):
        self.assertFalse(_is_valid_upload_key("uploads/not-a-uuid_video.mp4"))

    def test_rejects_slash_in_filename(self):
        self.assertFalse(_is_valid_upload_key("uploads/550e8400-e29b-41d4-a716-446655440000_path/file.mp4"))

    def test_valid_key_with_special_chars(self):
        self.assertTrue(_is_valid_upload_key("uploads/550e8400-e29b-41d4-a716-446655440000_my-video_2026.mp4"))


class ResolveVideoFilePathSafelyTests(unittest.TestCase):
    """Tests for _resolve_video_file_path_safely."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self._tmp.name)
        self.video_dir = self.base_dir / "videoFile"
        self.video_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_normal_path(self):
        with patch("sau_backend.BASE_DIR", self.base_dir):
            result = _resolve_video_file_path_safely("test.mp4")
        self.assertIsNotNone(result)
        self.assertEqual(result, self.video_dir / "test.mp4")

    def test_path_traversal_rejected(self):
        with patch("sau_backend.BASE_DIR", self.base_dir):
            result = _resolve_video_file_path_safely("../../etc/passwd")
        self.assertIsNone(result)

    def test_absolute_path_rejected(self):
        with patch("sau_backend.BASE_DIR", self.base_dir):
            result = _resolve_video_file_path_safely("/etc/passwd")
        self.assertIsNone(result)

    def test_dot_dot_rejected(self):
        with patch("sau_backend.BASE_DIR", self.base_dir):
            result = _resolve_video_file_path_safely("../secret.txt")
        self.assertIsNone(result)

    def test_empty_rejected(self):
        with patch("sau_backend.BASE_DIR", self.base_dir):
            result = _resolve_video_file_path_safely("")
        self.assertIsNone(result)

    def test_none_rejected(self):
        with patch("sau_backend.BASE_DIR", self.base_dir):
            result = _resolve_video_file_path_safely(None)
        self.assertIsNone(result)

    def test_subdirectory_allowed(self):
        (self.video_dir / "sub").mkdir()
        with patch("sau_backend.BASE_DIR", self.base_dir):
            result = _resolve_video_file_path_safely("sub/video.mp4")
        self.assertIsNotNone(result)

    def test_symlink_escape_rejected(self):
        # Create a symlink that escapes videoFile
        secret = self.base_dir / "secret.txt"
        secret.write_text("secret")
        symlink = self.video_dir / "escape.txt"
        try:
            symlink.symlink_to(secret)
            with patch("sau_backend.BASE_DIR", self.base_dir):
                result = _resolve_video_file_path_safely("escape.txt")
            # The resolved path would be outside videoFile, so it should be rejected
            self.assertIsNone(result)
        except OSError:
            # Symlinks may not be supported on all platforms
            self.skipTest("Symlinks not supported")


class OAuthPayloadSafetyTests(unittest.TestCase):
    """Tests proving OAuth postMessage payloads no longer contain accessToken or refreshToken."""

    def _get_callback_payload_fields(self, platform: str) -> set[str]:
        """Read the callback_payload construction for a platform and return its field names."""
        import ast
        import re

        with open("sau_backend.py", "r") as f:
            content = f.read()

        # Find the callback_payload construction for this platform
        patterns = {
            "youtube": r"callback_payload = \{[^}]+\}",
            "reddit": r"callback_payload = \{[^}]+\}",
            "twitter": r"callback_payload = \{[^}]+\}",
        }

        # Find all callback_payload constructions
        matches = list(re.finditer(r"callback_payload = \{", content))

        for match in matches:
            start = match.start()
            # Find the matching closing brace
            brace_count = 0
            end = start
            for i in range(start, len(content)):
                if content[i] == '{':
                    brace_count += 1
                elif content[i] == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        end = i + 1
                        break

            payload_str = content[start:end]

            # Check if this is for the right platform by looking at nearby context
            context_before = content[max(0, start - 500):start].lower()
            if platform not in context_before:
                continue

            # Extract field names
            fields = set()
            for field_match in re.finditer(r"'(\w+)':", payload_str):
                fields.add(field_match.group(1))
            return fields

        return set()

    def test_youtube_no_tokens_in_payload(self):
        fields = self._get_callback_payload_fields("youtube")
        self.assertNotIn("accessToken", fields, "YouTube callback payload should not contain accessToken")
        self.assertNotIn("refreshToken", fields, "YouTube callback payload should not contain refreshToken")

    def test_reddit_no_tokens_in_payload(self):
        fields = self._get_callback_payload_fields("reddit")
        self.assertNotIn("accessToken", fields, "Reddit callback payload should not contain accessToken")
        self.assertNotIn("refreshToken", fields, "Reddit callback payload should not contain refreshToken")

    def test_twitter_no_tokens_in_payload(self):
        fields = self._get_callback_payload_fields("twitter")
        self.assertNotIn("accessToken", fields, "Twitter callback payload should not contain accessToken")
        self.assertNotIn("refreshToken", fields, "Twitter callback payload should not contain refreshToken")


flask_available = importlib.util.find_spec("flask") is not None


@unittest.skipUnless(flask_available, "Flask not installed")
class UploadRegisterKeyValidationTests(unittest.TestCase):
    """Tests proving /upload/register rejects malicious keys."""

    def setUp(self):
        import sau_backend
        import db.createTable as create_table

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

    def tearDown(self):
        self._base_dir_patch.stop()
        self.sau_backend.app.config["SECURITY_POLICY"] = self._orig_policy
        self._tmp.cleanup()

    def test_valid_key_accepted(self):
        import uuid
        key = f"uploads/{uuid.uuid4()}_video.mp4"
        resp = self.client.post("/upload/register", json={
            "filename": "video.mp4", "key": key, "public_url": "https://cdn.example.com/video.mp4", "size": 1024,
        })
        self.assertEqual(resp.status_code, 200)

    def test_rejects_not_a_uuid(self):
        resp = self.client.post("/upload/register", json={
            "filename": "file.mp4", "key": "uploads/not-a-uuid_file.mp4", "public_url": "", "size": 0,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("invalid key format", resp.get_json()["msg"])

    def test_rejects_path_in_filename(self):
        import uuid
        key = f"uploads/{uuid.uuid4()}_bad/path.mp4"
        resp = self.client.post("/upload/register", json={
            "filename": "file.mp4", "key": key, "public_url": "", "size": 0,
        })
        self.assertEqual(resp.status_code, 400)

    def test_rejects_traversal_in_filename(self):
        import uuid
        key = f"uploads/{uuid.uuid4()}_..evil.mp4"
        resp = self.client.post("/upload/register", json={
            "filename": "file.mp4", "key": key, "public_url": "", "size": 0,
        })
        self.assertEqual(resp.status_code, 400)

    def test_rejects_absolute_path(self):
        resp = self.client.post("/upload/register", json={
            "filename": "file.mp4", "key": "/absolute/path.mp4", "public_url": "", "size": 0,
        })
        self.assertEqual(resp.status_code, 400)

    def test_rejects_dot_dot_traversal(self):
        resp = self.client.post("/upload/register", json={
            "filename": "file.mp4", "key": "../../conf.py", "public_url": "", "size": 0,
        })
        self.assertEqual(resp.status_code, 400)

    def test_rejects_empty_key(self):
        resp = self.client.post("/upload/register", json={
            "filename": "file.mp4", "key": "", "public_url": "", "size": 0,
        })
        self.assertEqual(resp.status_code, 400)

    def test_rejects_control_chars(self):
        import uuid
        key = f"uploads/{uuid.uuid4()}_file\x00.mp4"
        resp = self.client.post("/upload/register", json={
            "filename": "file.mp4", "key": key, "public_url": "", "size": 0,
        })
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
