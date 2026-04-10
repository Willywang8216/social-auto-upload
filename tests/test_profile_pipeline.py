import sqlite3
import tempfile
import unittest
from pathlib import Path

from utils.profile_pipeline import (
    SHEET_COLUMNS,
    build_google_sheet_rows,
    ensure_profile_tables,
    extract_json_payload,
    get_google_service_account_config,
    list_profiles,
    resolve_google_sheet_worksheet_name,
    save_google_service_account_config,
    save_profile,
    trim_text,
)


class ProfilePipelineTests(unittest.TestCase):
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
                    },
                },
            )

            self.assertEqual(saved["name"], "Creator Alpha")
            self.assertEqual(saved["accountIds"], [1, 2])
            self.assertEqual(saved["settings"]["storage"]["remoteName"], "remote-a")

            profiles = list_profiles(db_path)
            self.assertEqual(len(profiles), 1)
            self.assertEqual(profiles[0]["accountIds"], [1, 2])

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

            loaded = get_google_service_account_config(base_dir)
            self.assertTrue(loaded["configured"])
            self.assertEqual(loaded["projectId"], "demo-project")


if __name__ == "__main__":
    unittest.main()
