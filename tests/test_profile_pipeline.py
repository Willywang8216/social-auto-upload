import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.profile_pipeline import (
    SHEET_COLUMNS,
    build_google_sheet_row_mappings,
    build_google_sheet_rows,
    ensure_profile_tables,
    export_profiles_yaml,
    extract_json_payload,
    get_google_service_account_config,
    import_profiles_yaml,
    list_profiles,
    resolve_google_sheet_worksheet_name,
    save_google_service_account_config,
    save_profile,
    trim_text,
)


class ProfilePipelineTests(unittest.TestCase):
    def setUp(self):
        try:
            import yaml  # noqa: F401
            self.yaml_available = True
        except ImportError:
            self.yaml_available = False

    def test_save_profile_persists_account_links_and_settings(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "database.db"
            ensure_profile_tables(db_path)

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
                cursor.executemany(
                    "INSERT INTO user_info (type, filePath, userName, status) VALUES (?, ?, ?, ?)",
                    [
                        (3, "x.json", "x-account", 1),
                        (4, "k.json", "k-account", 1),
                    ],
                )
                conn.commit()

            saved = save_profile(
                db_path,
                {
                    "name": "Creator Alpha",
                    "systemPrompt": "Write in Chinese.",
                    "contactDetails": "Telegram: @alpha",
                    "cta": "Join Patreon",
                    "accountIds": [1, 2],
                    "settings": {
                        "storage": {"remoteName": "remote-a"},
                        "postPresets": {"twitter": "Preset X"},
                        "contentAccounts": [
                            {
                                "id": "acct-twitter-main",
                                "platform": "twitter",
                                "name": "光光 X 主帳",
                                "prompt": "語氣要更犀利",
                                "contactDetails": "@guang_main",
                                "cta": "追蹤主帳",
                                "postPreset": "X Main Preset",
                            },
                            {
                                "platform": "facebook",
                                "name": "光光 FB 備用",
                            },
                        ],
                    },
                },
            )

            self.assertEqual(saved["name"], "Creator Alpha")
            self.assertEqual(saved["accountIds"], [1, 2])
            self.assertEqual(saved["settings"]["storage"]["remoteName"], "remote-a")
            self.assertEqual(len(saved["settings"]["contentAccounts"]), 2)
            self.assertEqual(saved["settings"]["contentAccounts"][0]["platform"], "twitter")
            self.assertTrue(saved["settings"]["contentAccounts"][1]["id"])

            profiles = list_profiles(db_path)
            self.assertEqual(len(profiles), 1)
            self.assertEqual(profiles[0]["accountIds"], [1, 2])
            self.assertEqual(profiles[0]["settings"]["contentAccounts"][0]["postPreset"], "X Main Preset")

    def test_build_google_sheet_rows_maps_platform_presets_and_schedule(self):
        profile = {
            "settings": {
                "socialImport": {
                    "defaultLink": "https://example.com",
                    "category": "Launch",
                    "watermarkName": "Default",
                    "hashtagGroup": "Main Tags",
                    "ctaGroup": "Main CTA",
                    "firstComment": "First!",
                    "story": True,
                    "pinterestBoard": "Board A",
                    "altText": "Alt text",
                },
                "postPresets": {
                    "twitter": "Preset X",
                    "instagram": "Preset IG",
                },
            }
        }
        posts = {
            "twitter": "Tweet body",
            "instagram": "Instagram body",
            "facebook": "",
            "threads": "",
            "youtube": "",
            "tiktok": "",
        }
        upload_result = {
            "publicUrl": "https://cdn.example.com/video.mp4",
            "mediaKind": "video",
        }

        rows = build_google_sheet_rows(profile, posts, upload_result, "2026-05-03T14:30:00")

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "Tweet body")
        self.assertEqual(rows[0][1], "https://example.com")
        self.assertEqual(rows[0][3], "https://cdn.example.com/video.mp4")
        self.assertEqual(rows[0][4:9], ["5", "3", "2026", "14", "30"])
        self.assertEqual(rows[0][19], "Preset X")
        self.assertEqual(rows[1][19], "Preset IG")
        self.assertEqual(len(rows[0]), len(SHEET_COLUMNS))

    def test_build_google_sheet_rows_uses_content_account_post_presets(self):
        profile = {
            "settings": {
                "socialImport": {
                    "defaultLink": "https://example.com",
                },
                "postPresets": {
                    "twitter": "Fallback Twitter Preset",
                },
            }
        }
        upload_result = {
            "publicUrl": "https://cdn.example.com/image.jpg",
            "mediaKind": "image",
        }
        content_account_results = [
            {
                "account": {
                    "id": "acct-twitter-main",
                    "platform": "twitter",
                    "name": "光光 X 主帳",
                    "postPreset": "Twitter Main Preset",
                },
                "content": "Tweet body",
            },
            {
                "account": {
                    "id": "acct-facebook-main",
                    "platform": "facebook",
                    "name": "光光 FB 主帳",
                    "postPreset": "",
                },
                "content": "Facebook body",
            },
        ]

        rows = build_google_sheet_rows(
            profile,
            {},
            upload_result,
            "2026-05-03T14:30:00",
            content_account_results,
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "Tweet body")
        self.assertEqual(rows[0][2], "https://cdn.example.com/image.jpg")
        self.assertEqual(rows[0][19], "Twitter Main Preset")
        self.assertEqual(rows[1][19], "")

    def test_build_google_sheet_row_mappings_include_content_account_identity(self):
        mappings = build_google_sheet_row_mappings(
            {},
            [
                {
                    "account": {
                        "id": "acct-twitter-main",
                        "platform": "twitter",
                        "name": "光光 X 主帳",
                        "postPreset": "Twitter Main Preset",
                    },
                    "content": "Tweet body",
                },
                {
                    "account": {
                        "id": "acct-telegram-main",
                        "platform": "telegram",
                        "name": "光光 Telegram 主帳",
                        "postPreset": "",
                    },
                    "content": "Telegram body",
                },
            ],
        )

        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0]["rowNumber"], 2)
        self.assertEqual(mappings[0]["accountId"], "acct-twitter-main")
        self.assertEqual(mappings[0]["accountName"], "光光 X 主帳")
        self.assertEqual(mappings[0]["platform"], "twitter")
        self.assertEqual(mappings[0]["postPreset"], "Twitter Main Preset")

    def test_extract_json_payload_accepts_fenced_json(self):
        payload = extract_json_payload(
            """```json
            {"twitter":"hello","threads":"world"}
            ```"""
        )
        self.assertEqual(payload["twitter"], "hello")
        self.assertEqual(payload["threads"], "world")

    def test_trim_text_adds_ellipsis_when_limit_exceeded(self):
        self.assertEqual(trim_text("abcdef", 4), "abc…")
        self.assertEqual(trim_text("abc", 4), "abc")

    def test_resolve_google_sheet_worksheet_name_uses_date_and_profile_name(self):
        worksheet_name = resolve_google_sheet_worksheet_name(
            {"name": "Creator Alpha"},
            "2026-05-03T14:30:00",
        )
        self.assertEqual(worksheet_name, "2026-05-03-Creator Alpha")

    def test_save_google_service_account_config_persists_local_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            result = save_google_service_account_config(
                base_dir,
                """
                {
                  "type": "service_account",
                  "project_id": "demo-project",
                  "private_key_id": "abc123",
                  "private_key": "-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----\\n",
                  "client_email": "demo@demo-project.iam.gserviceaccount.com",
                  "client_id": "1234567890",
                  "token_uri": "https://oauth2.googleapis.com/token"
                }
                """,
            )

            self.assertTrue(result["configured"])
            self.assertEqual(result["source"], "stored_file")
            self.assertEqual(result["clientEmail"], "demo@demo-project.iam.gserviceaccount.com")

    def test_export_profiles_yaml_serializes_profiles(self):
        if not self.yaml_available:
            self.skipTest("PyYAML not installed")

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "database.db"
            ensure_profile_tables(db_path)
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
                cursor.executemany(
                    "INSERT INTO user_info (type, filePath, userName, status) VALUES (?, ?, ?, ?)",
                    [
                        (3, "x.json", "x-account", 1),
                        (4, "k.json", "k-account", 1),
                    ],
                )
                conn.commit()

            save_profile(
                db_path,
                {
                    "name": "Creator Alpha",
                    "systemPrompt": "Write in Chinese.",
                    "contactDetails": "Telegram: @alpha",
                    "cta": "Join Patreon",
                    "accountIds": [1, 2],
                    "settings": {
                        "storage": {"remoteName": "remote-a"},
                        "contentAccounts": [
                            {
                                "id": "acct-twitter-main",
                                "platform": "twitter",
                                "name": "Alpha X",
                            }
                        ],
                    },
                },
            )

            yaml_text = export_profiles_yaml(db_path)

            import yaml
            payload = yaml.safe_load(yaml_text)
            self.assertEqual(payload["version"], 1)
            self.assertEqual(len(payload["profiles"]), 1)
            self.assertEqual(payload["profiles"][0]["name"], "Creator Alpha")
            self.assertEqual(payload["profiles"][0]["settings"]["storage"]["remoteName"], "remote-a")

    def test_import_profiles_yaml_upserts_by_name(self):
        if not self.yaml_available:
            self.skipTest("PyYAML not installed")

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "database.db"
            ensure_profile_tables(db_path)
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
                cursor.executemany(
                    "INSERT INTO user_info (type, filePath, userName, status) VALUES (?, ?, ?, ?)",
                    [
                        (3, "x.json", "x-account", 1),
                        (4, "k.json", "k-account", 1),
                    ],
                )
                conn.commit()

            original = save_profile(
                db_path,
                {
                    "name": "Creator Alpha",
                    "systemPrompt": "Original prompt",
                    "contactDetails": "Old contact",
                    "cta": "Old CTA",
                    "accountIds": [1],
                    "settings": {
                        "storage": {"remoteName": "remote-a"},
                    },
                },
            )

            result = import_profiles_yaml(
                db_path,
                """
                version: 1
                profiles:
                  - name: "Creator Alpha"
                    systemPrompt: "Updated prompt"
                    contactDetails: "New contact"
                    cta: "New CTA"
                    accountIds: [1, 2, 2]
                    settings:
                      storage:
                        remoteName: "remote-b"
                      contentAccounts:
                        - id: "acct-twitter-main"
                          platform: "twitter"
                          name: "Alpha X"
                  - name: "Creator Beta"
                    systemPrompt: "Beta prompt"
                    contactDetails: ""
                    cta: ""
                    accountIds: []
                    settings: {}
                """,
            )

            self.assertEqual(result["created"], 1)
            self.assertEqual(result["updated"], 1)

            profiles = sorted(list_profiles(db_path), key=lambda item: item["name"])
            self.assertEqual(len(profiles), 2)
            self.assertEqual(profiles[0]["id"], original["id"])
            self.assertEqual(profiles[0]["systemPrompt"], "Updated prompt")
            self.assertEqual(profiles[0]["accountIds"], [1, 2])
            self.assertEqual(profiles[0]["settings"]["storage"]["remoteName"], "remote-b")
            self.assertEqual(profiles[1]["name"], "Creator Beta")

            loaded = get_google_service_account_config(base_dir)
            self.assertTrue(loaded["configured"])
            self.assertEqual(loaded["projectId"], "demo-project")

    def test_get_google_service_account_config_handles_invalid_env_json(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            with patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_JSON": "not-json"}, clear=False):
                result = get_google_service_account_config(base_dir)

            self.assertFalse(result["configured"])
            self.assertEqual(result["source"], "env_json")
            self.assertIn("not valid JSON", result["error"])

    def test_get_google_service_account_config_handles_missing_env_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            with patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_FILE": str(base_dir / "missing.json")}, clear=False):
                result = get_google_service_account_config(base_dir)

            self.assertFalse(result["configured"])
            self.assertEqual(result["source"], "env_file")
            self.assertIn("file not found", result["error"])

    def test_get_google_service_account_config_handles_unreadable_stored_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            storage_path = base_dir / "db" / "google_service_account.json"
            storage_path.parent.mkdir(parents=True, exist_ok=True)
            storage_path.write_bytes(b"\xff\xfe\x00\x00")

            result = get_google_service_account_config(base_dir)

            self.assertFalse(result["configured"])
            self.assertEqual(result["source"], "stored_file")
            self.assertIn("Failed to read Google service account file", result["error"])


if __name__ == "__main__":
    unittest.main()
