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

    def test_validate_account_config_warns_when_tiktok_profile_has_watermark(self) -> None:
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
        self.assertTrue(body["valid"])
        self.assertTrue(body["warnings"])
        self.assertIn("TikTok", body["warnings"][0])

    def test_campaign_prepare_tiktok_with_watermark_uses_raw_remote_artifacts(self) -> None:
        source_file = self.base_dir / "clip.jpg"
        source_file.write_bytes(b"img")
        file_record_id = self._insert_file_record("clip.jpg", str(source_file))

        profile_response = self.client.post(
            "/profiles",
            json={
                "name": "TikTok Brand",
                "settings": {"watermark": "Brand watermark"},
            },
        )
        profile_id = profile_response.get_json()["data"]["id"]

        account_response = self.client.post(
            f"/profiles/{profile_id}/accounts",
            json={
                "platform": "tiktok",
                "accountName": "brand-main",
                "authType": "oauth",
                "enabled": True,
                "config": {"accessTokenEnv": "TIKTOK_ACCESS_TOKEN", "publishMode": "direct"},
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
        media_group_id = media_group_response.get_json()["data"]["id"]

        uploads = []

        class _RemoteArtifact:
            def __init__(self, local_path):
                self.local_path = str(local_path)
                self.remote_path = f"remote/{Path(local_path).name}"
                self.public_url = f"https://cdn.example/{Path(local_path).name}"

        def fake_upload(local_path, **kwargs):
            uploads.append(Path(local_path).name)
            return _RemoteArtifact(local_path)

        with patch.object(self.sau_backend.rclone_storage, "upload_artifact", side_effect=fake_upload),              patch.dict("os.environ", {"SAU_DEFAULT_RCLONE_REMOTE": "demo-remote", "TIKTOK_ACCESS_TOKEN": "token"}, clear=False):
            prepare_response = self.client.post(
                "/campaigns/prepare",
                json={
                    "profileId": profile_id,
                    "mediaGroupId": media_group_id,
                    "selectedAccountIds": [account_id],
                    "title": "Launch day",
                    "useLlm": False,
                    "exportToSheet": False,
                    "uploadToRemote": True,
                },
            )
        self.assertEqual(prepare_response.status_code, 200)
        self.assertGreaterEqual(len(uploads), 2)
        campaign_payload = prepare_response.get_json()["data"]
        artifacts = self.sau_backend._artifact_payloads_for_platform(campaign_payload["artifacts"], "tiktok")
        artifact_kinds = {artifact["artifact_kind"] for artifact in artifacts}
        self.assertIn("raw_remote_upload", artifact_kinds)
        self.assertTrue(any(artifact["artifact_kind"] == "raw_remote_upload" for artifact in artifacts))

    def test_tiktok_admin_status_reports_expected_review_configuration(self) -> None:
        response = self.client.get('/admin/tiktok/status')
        self.assertEqual(response.status_code, 200)
        body = response.get_json()['data']
        self.assertEqual(body['redirectUri'], 'https://up.iamwillywang.com/oauth/tiktok/callback')
        self.assertIn('Login Kit for Web', body['selectedProducts'])
        self.assertIn('video.publish', body['selectedScopes'])

    def test_refresh_tiktok_token_updates_structured_account(self) -> None:
        profile_response = self.client.post(
            '/profiles',
            json={'name': 'TikTok Brand'},
        )
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'tiktok',
                'accountName': 'brand-tiktok',
                'authType': 'oauth',
                'config': {
                    'accessToken': 'old-token',
                    'refreshToken': 'refresh-token',
                    'publishMode': 'direct',
                },
            },
        )
        self.assertEqual(account_response.status_code, 200)
        account_id = account_response.get_json()['data']['id']

        with patch.object(self.sau_backend.tiktok_auth, 'refresh_access_token', return_value={
            'access_token': 'new-token',
            'refresh_token': 'new-refresh',
            'scope': 'user.info.basic,video.publish',
            'open_id': 'open-123',
        }), patch.object(self.sau_backend.tiktok_auth, 'fetch_user_info', return_value={
            'data': {
                'user': {
                    'display_name': 'TikTok Demo',
                    'avatar_url': 'https://example.com/avatar.jpg',
                }
            }
        }):
            response = self.client.post(f'/accounts/{account_id}/refresh-token')

        self.assertEqual(response.status_code, 200)
        config = response.get_json()['data']['config']
        self.assertEqual(config['accessToken'], 'new-token')
        self.assertEqual(config['refreshToken'], 'new-refresh')
        self.assertEqual(config['openId'], 'open-123')
        self.assertEqual(config['displayName'], 'TikTok Demo')


if __name__ == "__main__":
    unittest.main()
