import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils.account_registry import ensure_account_tables
from utils.account_validation import (
    get_validation_result,
    save_validation_result,
    validate_account,
)


class AccountValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.db_path = self.base_dir / "db" / "database.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        ensure_account_tables(self.db_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _insert_account(self, *, platform_key: str, metadata: dict | None = None, file_path: str = "", name: str = "Demo"):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO user_info (type, filePath, userName, status, platform_key, auth_mode, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    0 if platform_key not in {"xiaohongshu", "channels", "douyin", "kuaishou"} else 3,
                    file_path,
                    name,
                    0,
                    platform_key,
                    "manual",
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def test_save_validation_result_persists_to_json_store(self):
        saved = save_validation_result(
            self.base_dir,
            3,
            {
                "statusCode": 1,
                "message": "驗證成功",
                "lastValidatedAt": "2026-04-11T10:00:00",
                "details": {"username": "demo"},
            },
        )

        loaded = get_validation_result(self.base_dir, 3)
        self.assertEqual(saved["statusCode"], 1)
        self.assertEqual(loaded["message"], "驗證成功")
        self.assertEqual(loaded["details"]["username"], "demo")

    def test_validate_account_returns_missing_fields_for_twitter_without_tokens(self):
        account_id = self._insert_account(platform_key="twitter", metadata={"handle": "@demo"}, name="X Demo")

        result = validate_account(self.base_dir, self.db_path, account_id)

        self.assertEqual(result["statusCode"], 0)
        self.assertIn("apiKey", result["lastError"])
        self.assertIn("缺少驗證欄位", result["validationMessage"])

    @patch("utils.account_validation._get_requests_module")
    def test_validate_account_marks_twitter_account_valid_and_updates_db_status(self, requests_module_mock):
        account_id = self._insert_account(
            platform_key="twitter",
            metadata={
                "apiKey": "key",
                "apiKeySecret": "secret",
                "accessToken": "token",
                "accessTokenSecret": "token-secret",
            },
            name="X Demo",
        )
        response = Mock()
        response.ok = True
        response.json.return_value = {"data": {"id": "u123", "username": "demo_user"}}
        requests_module_mock.return_value.get.return_value = response

        result = validate_account(self.base_dir, self.db_path, account_id)

        self.assertEqual(result["statusCode"], 1)
        self.assertEqual(result["status"], "正常")
        self.assertEqual(result["validationDetails"]["username"], "demo_user")
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM user_info WHERE id = ?", (account_id,))
            status = cursor.fetchone()[0]
        self.assertEqual(status, 1)


if __name__ == "__main__":
    unittest.main()
