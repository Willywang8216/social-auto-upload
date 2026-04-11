import sqlite3
import tempfile
import unittest
from pathlib import Path

from utils.account_registry import ensure_account_tables, merge_sensitive_account_metadata, serialize_account_row


class AccountRegistryTests(unittest.TestCase):
    def test_ensure_account_tables_adds_extended_columns_and_backfills_domestic_rows(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "database.db"
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    CREATE TABLE user_info (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        type INTEGER NOT NULL,
                        filePath TEXT NOT NULL,
                        userName TEXT NOT NULL,
                        status INTEGER DEFAULT 0
                    )
                    """
                )
                cursor.execute(
                    "INSERT INTO user_info (type, filePath, userName, status) VALUES (?, ?, ?, ?)",
                    (3, "douyin_cookie.json", "Douyin 主帳", 1),
                )
                conn.commit()

            ensure_account_tables(db_path)

            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM user_info")
                row = cursor.fetchone()

            self.assertEqual(row["platform_key"], "douyin")
            self.assertEqual(row["auth_mode"], "qr_cookie")
            self.assertEqual(row["metadata_json"], "{}")

    def test_serialize_account_row_supports_international_accounts(self):
        row = {
            "id": 99,
            "type": 0,
            "platform_key": "twitter",
            "filePath": "",
            "userName": "光光 X 主帳",
            "status": 0,
            "auth_mode": "oauth_token",
            "metadata_json": '{"handle":"@guang"}',
        }

        serialized = serialize_account_row(row)

        self.assertEqual(serialized["platformKey"], "twitter")
        self.assertEqual(serialized["platform"], "X / Twitter")
        self.assertEqual(serialized["authMode"], "oauth_token")
        self.assertEqual(serialized["metadata"]["handle"], "@guang")
        self.assertTrue(serialized["isInternational"])

    def test_serialize_account_row_masks_sensitive_metadata_when_requested(self):
        row = {
            "id": 100,
            "type": 0,
            "platform_key": "twitter",
            "filePath": "",
            "userName": "X Main",
            "status": 0,
            "auth_mode": "oauth_token",
            "metadata_json": '{"handle":"@guang","apiKey":"key","accessToken":"token"}',
        }

        serialized = serialize_account_row(row, include_sensitive=False)

        self.assertEqual(serialized["metadata"]["handle"], "@guang")
        self.assertEqual(serialized["metadata"]["apiKey"], "********")
        self.assertEqual(serialized["metadata"]["accessToken"], "********")

    def test_merge_sensitive_account_metadata_preserves_existing_secrets_when_masked(self):
        merged = merge_sensitive_account_metadata(
            "twitter",
            {"handle": "@new", "apiKey": "********", "accessToken": ""},
            {"handle": "@old", "apiKey": "stored-key", "accessToken": "stored-token"},
        )

        self.assertEqual(merged["handle"], "@new")
        self.assertEqual(merged["apiKey"], "stored-key")
        self.assertEqual(merged["accessToken"], "stored-token")


if __name__ == "__main__":
    unittest.main()
