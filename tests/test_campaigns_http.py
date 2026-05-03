"""HTTP tests for the profile/media-group/campaign endpoints."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import db.createTable as create_table


flask_available = importlib.util.find_spec("flask") is not None

if "conf" not in sys.modules:
    conf_module = types.ModuleType("conf")
    conf_module.BASE_DIR = Path(".").resolve()
    conf_module.XHS_SERVER = "http://127.0.0.1:11901"
    conf_module.LOCAL_CHROME_PATH = ""
    conf_module.LOCAL_CHROME_HEADLESS = True
    conf_module.DEBUG_MODE = False
    sys.modules["conf"] = conf_module


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class CampaignApiTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import sau_backend
        except ModuleNotFoundError as exc:  # pragma: no cover - environment-specific
            self.skipTest(f"backend dependencies unavailable: {exc}")

        self.sau_backend = sau_backend
        self._tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self._tmp.name)
        self.db_path = self.base_dir / "db" / "database.db"
        create_table.bootstrap(self.db_path)

        self._base_dir_patch = patch.object(sau_backend, "BASE_DIR", self.base_dir)
        self._base_dir_patch.start()
        sau_backend.app.config["TESTING"] = True
        self.client = sau_backend.app.test_client()

    def tearDown(self) -> None:
        if hasattr(self, "_base_dir_patch"):
            self._base_dir_patch.stop()
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    def _insert_file_record(self, filename: str, file_path: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO file_records (filename, filesize, file_path)
                VALUES (?, ?, ?)
                """,
                (filename, 1.0, file_path),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def test_profile_account_media_group_campaign_flow(self) -> None:
        source_file = self.base_dir / "source.jpg"
        source_file.write_bytes(b"img")
        file_record_id = self._insert_file_record("source.jpg", str(source_file))

        profile_response = self.client.post(
            "/profiles",
            json={
                "name": "Brand A",
                "description": "Primary brand",
                "settings": {"systemPrompt": "friendly"},
            },
        )
        self.assertEqual(profile_response.status_code, 200)
        profile_id = profile_response.get_json()["data"]["id"]

        account_response = self.client.post(
            f"/profiles/{profile_id}/accounts",
            json={
                "platform": "twitter",
                "accountName": "brand-main",
                "authType": "oauth",
                "enabled": True,
                "config": {"sheetPostPreset": "Brand preset"},
            },
        )
        self.assertEqual(account_response.status_code, 200)
        account_id = account_response.get_json()["data"]["id"]

        media_group_response = self.client.post(
            "/media-groups",
            json={
                "name": "Launch batch",
                "items": [{"fileRecordId": file_record_id, "role": "image"}],
            },
        )
        self.assertEqual(media_group_response.status_code, 200)
        media_group_id = media_group_response.get_json()["data"]["id"]

        prepare_response = self.client.post(
            "/campaigns/prepare",
            json={
                "profileId": profile_id,
                "mediaGroupId": media_group_id,
                "selectedAccountIds": [account_id],
                "title": "Launch day",
                "notes": "A fuller explanation of the launch",
                "hashtags": ["launch", "brand"],
                "useLlm": False,
                "exportToSheet": False,
                "uploadToRemote": False,
            },
        )
        self.assertEqual(prepare_response.status_code, 200)
        campaign_payload = prepare_response.get_json()["data"]
        self.assertEqual(campaign_payload["status"], "prepared")
        self.assertEqual(len(campaign_payload["posts"]), 1)
        self.assertEqual(campaign_payload["posts"][0]["platform"], "twitter")

        publish_response = self.client.post(
            f"/campaigns/{campaign_payload['id']}/publish",
            json={},
        )
        self.assertEqual(publish_response.status_code, 200)
        publish_payload = publish_response.get_json()["data"]
        self.assertEqual(len(publish_payload["jobs"]), 1)
        self.assertEqual(
            publish_payload["campaign"]["posts"][0]["status"],
            "queued",
        )

    def test_validate_account_config_reports_tiktok_watermark_conflict(self) -> None:
        profile_response = self.client.post(
            "/profiles",
            json={
                "name": "TikTok Brand",
                "settings": {"watermark": "Brand watermark"},
            },
        )
        self.assertEqual(profile_response.status_code, 200)
        profile_id = profile_response.get_json()["data"]["id"]

        response = self.client.post(
            "/accounts/validate-config",
            json={
                "profileId": profile_id,
                "platform": "tiktok",
                "authType": "oauth",
                "config": {
                    "accessTokenEnv": "TIKTOK_ACCESS_TOKEN",
                    "publishMode": "direct",
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()["data"]
        self.assertFalse(body["valid"])
        self.assertIn("浮水印", body["errors"][0])


if __name__ == "__main__":
    unittest.main()
