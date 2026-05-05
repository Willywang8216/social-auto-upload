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

    def test_tiktok_oauth_start_persists_request_and_returns_authorize_url(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'TikTok Brand'})
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'tiktok',
                'accountName': 'brand-tiktok',
                'authType': 'oauth',
                'config': {'publishMode': 'direct', 'accessTokenEnv': 'TIKTOK_ACCESS_TOKEN'},
            },
        )
        account_id = account_response.get_json()['data']['id']

        with patch.object(self.sau_backend.tiktok_auth, 'build_authorize_url_from_env', return_value='https://www.tiktok.com/v2/auth/authorize/?demo=1'):
            response = self.client.post('/oauth/tiktok/start', json={
                'profileId': profile_id,
                'accountId': account_id,
                'accountName': 'brand-tiktok',
                'scopes': ['user.info.basic', 'video.publish'],
            })

        self.assertEqual(response.status_code, 200)
        body = response.get_json()['data']
        self.assertEqual(body['authorizeUrl'], 'https://www.tiktok.com/v2/auth/authorize/?demo=1')
        status = self.client.get('/admin/tiktok/status').get_json()['data']
        self.assertTrue(any(event['type'] == 'start' for event in status['recentEvents']))

    def test_tiktok_oauth_callback_updates_account_and_status(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'TikTok Brand'})
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'tiktok',
                'accountName': 'brand-tiktok',
                'authType': 'oauth',
                'config': {'publishMode': 'direct', 'accessTokenEnv': 'TIKTOK_ACCESS_TOKEN'},
            },
        )
        account_id = account_response.get_json()['data']['id']
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                'INSERT INTO tiktok_oauth_requests (state_token, profile_id, account_id, account_name, redirect_uri, scopes_json, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
                ('state-123', profile_id, account_id, 'brand-tiktok', 'https://up.iamwillywang.com/oauth/tiktok/callback', '["user.info.basic", "video.publish"]', 'started'),
            )
            conn.commit()

        with patch.object(self.sau_backend.tiktok_auth, 'exchange_code_for_token', return_value={
            'access_token': 'token-1',
            'refresh_token': 'refresh-1',
            'open_id': 'open-1',
            'scope': 'user.info.basic,video.publish',
        }), patch.object(self.sau_backend.tiktok_auth, 'fetch_user_info', return_value={
            'data': {'user': {'display_name': 'TikTok Demo', 'avatar_url': 'https://example.com/avatar.jpg'}}
        }):
            response = self.client.get('/oauth/tiktok/callback?state=state-123&code=demo-code')

        self.assertEqual(response.status_code, 200)
        account = self.client.get(f'/profiles/{profile_id}/accounts').get_json()['data'][0]
        self.assertEqual(account['config']['accessToken'], 'token-1')
        status = self.client.get('/admin/tiktok/status').get_json()['data']
        self.assertEqual(status['lastCallback']['status'], 'ok')
        self.assertEqual(status['lastCallback']['displayName'], 'TikTok Demo')

    def test_tiktok_webhook_records_signature_status(self) -> None:
        response = self.client.post('/webhooks/tiktok', json={'event': 'demo'})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()['data']
        self.assertFalse(body['signatureVerified'])
        status = self.client.get('/admin/tiktok/status').get_json()['data']
        self.assertEqual(status['lastWebhook']['type'], 'webhook')
        self.assertIn(status['lastWebhook']['signatureStatus'], {'missing_signature', 'missing_secret'})


    def test_tiktok_admin_status_can_filter_by_account(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'TikTok Brand'})
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'tiktok',
                'accountName': 'brand-tiktok',
                'authType': 'oauth',
                'config': {'accessTokenEnv': 'TIKTOK_ACCESS_TOKEN', 'publishMode': 'direct'},
            },
        )
        account_id = account_response.get_json()['data']['id']

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                'INSERT INTO tiktok_review_events (event_type, status, account_id, account_name, payload_json, headers_json, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?)',
                ('refresh', 'ok', account_id, 'brand-tiktok', '{"status":"ok"}', '{}', '{}'),
            )
            conn.commit()

        response = self.client.get(f'/admin/tiktok/status?accountId={account_id}')
        self.assertEqual(response.status_code, 200)
        body = response.get_json()['data']
        self.assertEqual(body['accountId'], account_id)
        self.assertEqual(body['lastRefresh']['type'], 'refresh')


    def test_refresh_stale_tiktok_tokens_endpoint_refreshes_stale_account(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'TikTok Brand'})
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
                    'accessTokenExpiresAt': '2000-01-01T00:00:00+00:00',
                    'publishMode': 'direct',
                },
            },
        )
        account_id = account_response.get_json()['data']['id']

        with patch.object(self.sau_backend.tiktok_auth, 'refresh_access_token', return_value={
            'access_token': 'fresh-token',
            'refresh_token': 'fresh-refresh',
            'expires_in': 3600,
            'scope': 'user.info.basic,video.publish',
            'open_id': 'open-xyz',
        }), patch.object(self.sau_backend.tiktok_auth, 'fetch_user_info', return_value={
            'data': {'user': {'display_name': 'TikTok Demo', 'avatar_url': 'https://example.com/avatar.jpg'}}
        }):
            response = self.client.post('/accounts/tiktok/refresh-stale', json={'accountId': account_id})

        self.assertEqual(response.status_code, 200)
        body = response.get_json()['data']
        self.assertEqual(body['refreshed'], 1)
        self.assertEqual(body['results'][0]['status'], 'refreshed')

    def test_refresh_reddit_token_updates_structured_account(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'Reddit Brand'})
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'reddit',
                'accountName': 'brand-reddit',
                'authType': 'oauth',
                'config': {
                    'clientIdEnv': 'REDDIT_CLIENT_ID',
                    'clientSecretEnv': 'REDDIT_CLIENT_SECRET',
                    'refreshTokenEnv': 'REDDIT_REFRESH_TOKEN',
                    'subreddits': ['suba'],
                },
            },
        )
        account_id = account_response.get_json()['data']['id']
        with patch.object(self.sau_backend.prepared_publishers, 'refresh_reddit_access_token', return_value={
            'access_token': 'reddit-token',
            'expires_in': 3600,
            'scope': 'submit identity read',
            'me': {'name': 'reddit-user'},
        }):
            response = self.client.post(f'/accounts/{account_id}/refresh-token')
        self.assertEqual(response.status_code, 200)
        config = response.get_json()['data']['config']
        self.assertEqual(config['accessToken'], 'reddit-token')
        self.assertEqual(config['redditUserName'], 'reddit-user')
        self.assertTrue(config['lastManualRefreshAt'])

    def test_refresh_youtube_token_updates_structured_account(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'YouTube Brand'})
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'youtube',
                'accountName': 'brand-youtube',
                'authType': 'oauth',
                'config': {
                    'channelId': 'UC123',
                    'clientIdEnv': 'YT_CLIENT_ID',
                    'clientSecretEnv': 'YT_CLIENT_SECRET',
                    'refreshTokenEnv': 'YT_REFRESH_TOKEN',
                },
            },
        )
        account_id = account_response.get_json()['data']['id']
        with patch.object(self.sau_backend.prepared_publishers, 'refresh_youtube_access_token', return_value={
            'access_token': 'yt-token',
            'expires_in': 3600,
            'channel': {'items': [{'snippet': {'title': 'Demo Channel'}}]},
        }):
            response = self.client.post(f'/accounts/{account_id}/refresh-token')
        self.assertEqual(response.status_code, 200)
        config = response.get_json()['data']['config']
        self.assertEqual(config['accessToken'], 'yt-token')
        self.assertEqual(config['channelTitle'], 'Demo Channel')
        self.assertTrue(config['lastManualRefreshAt'])

    def test_check_facebook_connection_updates_structured_account(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'Meta Brand'})
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'facebook',
                'accountName': 'brand-facebook',
                'authType': 'oauth',
                'config': {'pageId': '123', 'accessTokenEnv': 'FB_PAGE_TOKEN'},
            },
        )
        account_id = account_response.get_json()['data']['id']
        with patch.object(self.sau_backend.prepared_publishers, 'validate_facebook_config_live', return_value={'id': '123', 'name': 'Brand Page'}):
            response = self.client.post(f'/accounts/{account_id}/check-connection')
        self.assertEqual(response.status_code, 200)
        config = response.get_json()['data']['config']
        self.assertEqual(config['facebookPageName'], 'Brand Page')
        self.assertTrue(config['lastConnectionCheckAt'])

    def test_check_instagram_connection_updates_structured_account(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'Meta Brand'})
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'instagram',
                'accountName': 'brand-instagram',
                'authType': 'oauth',
                'config': {'igUserId': '1789', 'accessTokenEnv': 'IG_ACCESS_TOKEN'},
            },
        )
        account_id = account_response.get_json()['data']['id']
        with patch.object(self.sau_backend.prepared_publishers, 'validate_instagram_config_live', return_value={'id': '1789', 'username': 'ig-demo'}):
            response = self.client.post(f'/accounts/{account_id}/check-connection')
        self.assertEqual(response.status_code, 200)
        config = response.get_json()['data']['config']
        self.assertEqual(config['instagramUserName'], 'ig-demo')
        self.assertTrue(config['lastConnectionCheckAt'])

    def test_check_threads_connection_updates_structured_account(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'Meta Brand'})
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'threads',
                'accountName': 'brand-threads',
                'authType': 'oauth',
                'config': {'userId': '42', 'accessTokenEnv': 'THREADS_ACCESS_TOKEN'},
            },
        )
        account_id = account_response.get_json()['data']['id']
        with patch.object(self.sau_backend.prepared_publishers, 'validate_threads_config_live', return_value={'id': '42', 'username': 'threads-demo'}):
            response = self.client.post(f'/accounts/{account_id}/check-connection')
        self.assertEqual(response.status_code, 200)
        config = response.get_json()['data']['config']
        self.assertEqual(config['threadsUserName'], 'threads-demo')
        self.assertTrue(config['lastConnectionCheckAt'])

    def test_check_telegram_connection_updates_structured_account(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'Telegram Brand'})
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'telegram',
                'accountName': 'brand-telegram',
                'authType': 'manual',
                'config': {'chatId': '@brand', 'botTokenEnv': 'TELEGRAM_BOT_TOKEN'},
            },
        )
        account_id = account_response.get_json()['data']['id']
        with patch.object(self.sau_backend.prepared_publishers, 'validate_telegram_config_live', return_value={'bot': {'result': {'username': 'brand_bot'}}, 'chat': {'result': {'title': 'Brand Chat'}}}):
            response = self.client.post(f'/accounts/{account_id}/check-connection')
        self.assertEqual(response.status_code, 200)
        config = response.get_json()['data']['config']
        self.assertEqual(config['telegramBotName'], 'brand_bot')
        self.assertEqual(config['telegramChatTitle'], 'Brand Chat')
        self.assertTrue(config['lastConnectionCheckAt'])

    def test_check_discord_connection_updates_structured_account(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'Discord Brand'})
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'discord',
                'accountName': 'brand-discord',
                'authType': 'manual',
                'config': {'webhookUrlEnv': 'DISCORD_WEBHOOK_URL'},
            },
        )
        account_id = account_response.get_json()['data']['id']
        with patch.object(self.sau_backend.prepared_publishers, 'validate_discord_config_live', return_value={'name': 'Brand Hook', 'channel_id': '999'}):
            response = self.client.post(f'/accounts/{account_id}/check-connection')
        self.assertEqual(response.status_code, 200)
        config = response.get_json()['data']['config']
        self.assertEqual(config['discordWebhookName'], 'Brand Hook')
        self.assertEqual(config['discordWebhookChannel'], '999')
        self.assertTrue(config['lastConnectionCheckAt'])

    def test_batch_check_connections_returns_summary_and_updated_accounts(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'Ops Brand'})
        profile_id = profile_response.get_json()['data']['id']
        facebook_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'facebook',
                'accountName': 'brand-facebook',
                'authType': 'oauth',
                'config': {'pageId': '123', 'accessTokenEnv': 'FB_PAGE_TOKEN'},
            },
        )
        telegram_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'telegram',
                'accountName': 'brand-telegram',
                'authType': 'manual',
                'config': {'chatId': '@brand', 'botTokenEnv': 'TELEGRAM_BOT_TOKEN'},
            },
        )
        facebook_id = facebook_response.get_json()['data']['id']
        telegram_id = telegram_response.get_json()['data']['id']
        with patch.object(self.sau_backend.prepared_publishers, 'validate_facebook_config_live', return_value={'id': '123', 'name': 'Brand Page'}),              patch.object(self.sau_backend.prepared_publishers, 'validate_telegram_config_live', return_value={'bot': {'result': {'username': 'brand_bot'}}, 'chat': {'result': {'title': 'Brand Chat'}}}):
            response = self.client.post('/accounts/batch/check-connections', json={'accountIds': [facebook_id, telegram_id]})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()['data']
        self.assertEqual(body['operation'], 'check')
        self.assertEqual(body['succeeded'], 2)
        self.assertEqual(body['failed'], 0)
        self.assertEqual(len(body['accounts']), 2)

    def test_batch_refresh_tokens_returns_summary_and_updated_accounts(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'Ops Brand'})
        profile_id = profile_response.get_json()['data']['id']
        reddit_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'reddit',
                'accountName': 'brand-reddit',
                'authType': 'oauth',
                'config': {
                    'clientIdEnv': 'REDDIT_CLIENT_ID',
                    'clientSecretEnv': 'REDDIT_CLIENT_SECRET',
                    'refreshTokenEnv': 'REDDIT_REFRESH_TOKEN',
                    'subreddits': ['suba'],
                },
            },
        )
        youtube_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'youtube',
                'accountName': 'brand-youtube',
                'authType': 'oauth',
                'config': {
                    'channelId': 'UC123',
                    'clientIdEnv': 'YT_CLIENT_ID',
                    'clientSecretEnv': 'YT_CLIENT_SECRET',
                    'refreshTokenEnv': 'YT_REFRESH_TOKEN',
                },
            },
        )
        reddit_id = reddit_response.get_json()['data']['id']
        youtube_id = youtube_response.get_json()['data']['id']
        with patch.object(self.sau_backend.prepared_publishers, 'refresh_reddit_access_token', return_value={
            'access_token': 'reddit-token', 'expires_in': 3600, 'scope': 'submit identity read', 'me': {'name': 'reddit-user'}
        }), patch.object(self.sau_backend.prepared_publishers, 'refresh_youtube_access_token', return_value={
            'access_token': 'yt-token', 'expires_in': 3600, 'channel': {'items': [{'snippet': {'title': 'Demo Channel'}}]}
        }):
            response = self.client.post('/accounts/batch/refresh-tokens', json={'accountIds': [reddit_id, youtube_id]})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()['data']
        self.assertEqual(body['operation'], 'refresh')
        self.assertEqual(body['succeeded'], 2)
        self.assertEqual(body['failed'], 0)
        self.assertEqual(len(body['accounts']), 2)

    def test_check_connection_writes_account_event_and_events_endpoint_lists_it(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'Meta Brand'})
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'facebook',
                'accountName': 'brand-facebook',
                'authType': 'oauth',
                'config': {'pageId': '123', 'accessTokenEnv': 'FB_PAGE_TOKEN'},
            },
        )
        account_id = account_response.get_json()['data']['id']
        with patch.object(self.sau_backend.prepared_publishers, 'validate_facebook_config_live', return_value={'id': '123', 'name': 'Brand Page'}):
            self.client.post(f'/accounts/{account_id}/check-connection')
        response = self.client.get(f'/accounts/events?accountId={account_id}')
        self.assertEqual(response.status_code, 200)
        events = response.get_json()['data']
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(events[0]['action'], 'check_connection')
        self.assertEqual(events[0]['status'], 'ok')

    def test_health_summary_reports_recent_events(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'Ops Brand'})
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'reddit',
                'accountName': 'brand-reddit',
                'authType': 'oauth',
                'config': {
                    'clientIdEnv': 'REDDIT_CLIENT_ID',
                    'clientSecretEnv': 'REDDIT_CLIENT_SECRET',
                    'refreshTokenEnv': 'REDDIT_REFRESH_TOKEN',
                    'subreddits': ['suba'],
                },
            },
        )
        account_id = account_response.get_json()['data']['id']
        with patch.object(self.sau_backend.prepared_publishers, 'refresh_reddit_access_token', return_value={
            'access_token': 'reddit-token', 'expires_in': 3600, 'scope': 'submit identity read', 'me': {'name': 'reddit-user'}
        }):
            self.client.post(f'/accounts/{account_id}/refresh-token')
        response = self.client.get('/accounts/health-summary')
        self.assertEqual(response.status_code, 200)
        body = response.get_json()['data']
        self.assertGreaterEqual(body['total'], 1)
        self.assertGreaterEqual(body['recentEventTotals']['total'], 1)
        self.assertIn('reddit', body['byPlatform'])

    def test_accounts_maintenance_run_refreshes_multiple_platforms(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'Ops Brand'})
        profile_id = profile_response.get_json()['data']['id']
        tiktok_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'tiktok',
                'accountName': 'brand-tiktok',
                'authType': 'oauth',
                'config': {
                    'accessToken': 'old-token',
                    'refreshToken': 'refresh-token',
                    'accessTokenExpiresAt': '2000-01-01T00:00:00+00:00',
                    'publishMode': 'direct',
                },
            },
        )
        reddit_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'reddit',
                'accountName': 'brand-reddit',
                'authType': 'oauth',
                'config': {
                    'clientIdEnv': 'REDDIT_CLIENT_ID',
                    'clientSecretEnv': 'REDDIT_CLIENT_SECRET',
                    'refreshTokenEnv': 'REDDIT_REFRESH_TOKEN',
                    'subreddits': ['suba'],
                    'accessTokenExpiresAt': '2000-01-01T00:00:00+00:00',
                },
            },
        )
        tiktok_id = tiktok_response.get_json()['data']['id']
        reddit_id = reddit_response.get_json()['data']['id']
        with patch.object(self.sau_backend.tiktok_auth, 'refresh_access_token', return_value={
            'access_token': 'fresh-token',
            'refresh_token': 'fresh-refresh',
            'expires_in': 3600,
            'scope': 'user.info.basic,video.publish',
            'open_id': 'open-xyz',
        }), patch.object(self.sau_backend.tiktok_auth, 'fetch_user_info', return_value={
            'data': {'user': {'display_name': 'TikTok Demo', 'avatar_url': 'https://example.com/avatar.jpg'}}
        }), patch.object(self.sau_backend.prepared_publishers, 'refresh_reddit_access_token', return_value={
            'access_token': 'reddit-token', 'expires_in': 3600, 'scope': 'submit identity read', 'me': {'name': 'reddit-user'}
        }):
            response = self.client.post('/accounts/maintenance/run', json={
                'accountIds': [tiktok_id, reddit_id],
                'platforms': ['tiktok', 'reddit'],
                'expiringWithinSeconds': 300,
            })
        self.assertEqual(response.status_code, 200)
        body = response.get_json()['data']
        self.assertEqual(body['refreshed'], 2)
        self.assertEqual(body['stale'], 2)
        self.assertEqual(body['mode'], 'auto')

    def test_accounts_maintenance_status_reports_last_result(self) -> None:
        response = self.client.get('/accounts/maintenance/status')
        self.assertEqual(response.status_code, 200)
        body = response.get_json()['data']
        self.assertIn('enabled', body)
        self.assertIn('lastResult', body)

    def test_reddit_oauth_start_persists_request_and_returns_authorize_url(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'Reddit Brand'})
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'reddit',
                'accountName': 'brand-reddit',
                'authType': 'oauth',
                'config': {
                    'clientIdEnv': 'REDDIT_CLIENT_ID',
                    'clientSecretEnv': 'REDDIT_CLIENT_SECRET',
                    'subreddits': ['suba'],
                },
            },
        )
        account_id = account_response.get_json()['data']['id']
        with patch.object(self.sau_backend.reddit_auth, 'build_authorize_url_from_env', return_value='https://www.reddit.com/api/v1/authorize?demo=1'):
            response = self.client.post('/oauth/reddit/start', json={
                'profileId': profile_id,
                'accountId': account_id,
                'accountName': 'brand-reddit',
                'scopes': ['identity', 'submit', 'read'],
            })
        self.assertEqual(response.status_code, 200)
        body = response.get_json()['data']
        self.assertEqual(body['authorizeUrl'], 'https://www.reddit.com/api/v1/authorize?demo=1')
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute('SELECT COUNT(*) FROM reddit_oauth_requests WHERE account_id = ?', (account_id,)).fetchone()
        self.assertEqual(row[0], 1)

    def test_reddit_oauth_callback_updates_structured_account(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'Reddit Brand'})
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'reddit',
                'accountName': 'brand-reddit',
                'authType': 'oauth',
                'config': {
                    'clientIdEnv': 'REDDIT_CLIENT_ID',
                    'clientSecretEnv': 'REDDIT_CLIENT_SECRET',
                    'subreddits': ['suba'],
                },
            },
        )
        account_id = account_response.get_json()['data']['id']
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                'INSERT INTO reddit_oauth_requests (state_token, profile_id, account_id, account_name, redirect_uri, scopes_json, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
                ('reddit-state-1', profile_id, account_id, 'brand-reddit', 'https://up.iamwillywang.com/oauth/reddit/callback', '["identity", "submit", "read"]', 'started'),
            )
            conn.commit()
        with patch.object(self.sau_backend.reddit_auth, 'exchange_code_for_token', return_value={
            'access_token': 'reddit-access',
            'refresh_token': 'reddit-refresh',
            'expires_in': 3600,
            'scope': 'identity submit read',
        }), patch.object(self.sau_backend.reddit_auth, 'fetch_user_info', return_value={'name': 'reddit-user'}):
            response = self.client.get('/oauth/reddit/callback?state=reddit-state-1&code=demo-code')
        self.assertEqual(response.status_code, 200)
        account = self.client.get(f'/profiles/{profile_id}/accounts').get_json()['data'][0]
        self.assertEqual(account['config']['refreshToken'], 'reddit-refresh')
        self.assertEqual(account['config']['redditUserName'], 'reddit-user')

    def test_validate_account_config_allows_youtube_oauth_without_channel_id(self) -> None:
        response = self.client.post(
            '/accounts/validate-config',
            json={
                'platform': 'youtube',
                'authType': 'oauth',
                'config': {
                    'clientIdEnv': 'YT_CLIENT_ID',
                    'clientSecretEnv': 'YT_CLIENT_SECRET',
                    'refreshTokenEnv': 'YT_REFRESH_TOKEN',
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()['data']
        self.assertTrue(body['valid'])
        self.assertTrue(body['warnings'])

    def test_youtube_oauth_start_persists_request_and_returns_authorize_url(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'YouTube Brand'})
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'youtube',
                'accountName': 'brand-youtube',
                'authType': 'oauth',
                'config': {
                    'clientIdEnv': 'YT_CLIENT_ID',
                    'clientSecretEnv': 'YT_CLIENT_SECRET',
                },
            },
        )
        account_id = account_response.get_json()['data']['id']
        with patch.object(self.sau_backend.youtube_auth, 'build_authorize_url_from_env', return_value='https://accounts.google.com/o/oauth2/v2/auth?demo=1'):
            response = self.client.post('/oauth/youtube/start', json={
                'profileId': profile_id,
                'accountId': account_id,
                'accountName': 'brand-youtube',
                'scopes': ['https://www.googleapis.com/auth/youtube.upload'],
            })
        self.assertEqual(response.status_code, 200)
        body = response.get_json()['data']
        self.assertEqual(body['authorizeUrl'], 'https://accounts.google.com/o/oauth2/v2/auth?demo=1')
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute('SELECT COUNT(*) FROM youtube_oauth_requests WHERE account_id = ?', (account_id,)).fetchone()
        self.assertEqual(row[0], 1)

    def test_youtube_oauth_callback_updates_structured_account(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'YouTube Brand'})
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'youtube',
                'accountName': 'brand-youtube',
                'authType': 'oauth',
                'config': {
                    'clientIdEnv': 'YT_CLIENT_ID',
                    'clientSecretEnv': 'YT_CLIENT_SECRET',
                },
            },
        )
        account_id = account_response.get_json()['data']['id']
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                'INSERT INTO youtube_oauth_requests (state_token, profile_id, account_id, account_name, redirect_uri, scopes_json, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
                ('youtube-state-1', profile_id, account_id, 'brand-youtube', 'https://up.iamwillywang.com/oauth/youtube/callback', '["https://www.googleapis.com/auth/youtube.upload"]', 'started'),
            )
            conn.commit()
        with patch.object(self.sau_backend.youtube_auth, 'exchange_code_for_token', return_value={
            'access_token': 'yt-access',
            'refresh_token': 'yt-refresh',
            'expires_in': 3600,
            'scope': 'https://www.googleapis.com/auth/youtube.upload',
        }), patch.object(self.sau_backend.youtube_auth, 'fetch_my_channels', return_value={'items': [{'id': 'UC123', 'snippet': {'title': 'Demo Channel'}}]}):
            response = self.client.get('/oauth/youtube/callback?state=youtube-state-1&code=demo-code')
        self.assertEqual(response.status_code, 200)
        account = self.client.get(f'/profiles/{profile_id}/accounts').get_json()['data'][0]
        self.assertEqual(account['config']['refreshToken'], 'yt-refresh')
        self.assertEqual(account['config']['channelId'], 'UC123')
        self.assertEqual(account['config']['channelTitle'], 'Demo Channel')

    def test_meta_oauth_start_persists_request_and_returns_authorize_url(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'Meta Brand'})
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'facebook',
                'accountName': 'brand-facebook',
                'authType': 'oauth',
                'config': {'pageId': '123'},
            },
        )
        account_id = account_response.get_json()['data']['id']
        with patch.object(self.sau_backend.meta_auth, 'build_authorize_url_from_env', return_value='https://facebook.com/dialog/oauth?demo=1'):
            response = self.client.post('/oauth/meta/start', json={
                'profileId': profile_id,
                'accountId': account_id,
                'accountName': 'brand-facebook',
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['data']['authorizeUrl'], 'https://facebook.com/dialog/oauth?demo=1')
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute('SELECT COUNT(*) FROM meta_oauth_requests WHERE account_id = ?', (account_id,)).fetchone()
        self.assertEqual(row[0], 1)

    def test_meta_oauth_callback_updates_facebook_account(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'Meta Brand'})
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'facebook',
                'accountName': 'brand-facebook',
                'authType': 'oauth',
                'config': {'pageId': '123'},
            },
        )
        account_id = account_response.get_json()['data']['id']
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                'INSERT INTO meta_oauth_requests (state_token, profile_id, account_id, account_name, platform, redirect_uri, scopes_json, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                ('meta-state-1', profile_id, account_id, 'brand-facebook', 'facebook', 'https://up.iamwillywang.com/oauth/meta/callback', '["pages_show_list"]', 'started'),
            )
            conn.commit()
        with patch.object(self.sau_backend.meta_auth, 'exchange_code_for_token', return_value={'access_token': 'short-token', 'expires_in': 3600}),              patch.object(self.sau_backend.meta_auth, 'exchange_for_long_lived_token', return_value={'access_token': 'long-token', 'expires_in': 5184000}),              patch.object(self.sau_backend.meta_auth, 'fetch_managed_pages', return_value={'data': [{'id': '123', 'name': 'Brand Page', 'access_token': 'page-token'}]}):
            response = self.client.get('/oauth/meta/callback?state=meta-state-1&code=demo-code')
        self.assertEqual(response.status_code, 200)
        account = self.client.get(f'/profiles/{profile_id}/accounts').get_json()['data'][0]
        self.assertEqual(account['config']['pageId'], '123')
        self.assertEqual(account['config']['facebookPageName'], 'Brand Page')
        self.assertEqual(account['config']['accessToken'], 'page-token')

    def test_meta_oauth_callback_updates_instagram_account(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'Meta Brand'})
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'instagram',
                'accountName': 'brand-instagram',
                'authType': 'oauth',
                'config': {},
            },
        )
        account_id = account_response.get_json()['data']['id']
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                'INSERT INTO meta_oauth_requests (state_token, profile_id, account_id, account_name, platform, redirect_uri, scopes_json, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                ('meta-state-2', profile_id, account_id, 'brand-instagram', 'instagram', 'https://up.iamwillywang.com/oauth/meta/callback', '["instagram_basic"]', 'started'),
            )
            conn.commit()
        with patch.object(self.sau_backend.meta_auth, 'exchange_code_for_token', return_value={'access_token': 'short-token', 'expires_in': 3600}),              patch.object(self.sau_backend.meta_auth, 'exchange_for_long_lived_token', return_value={'access_token': 'long-token', 'expires_in': 5184000}),              patch.object(self.sau_backend.meta_auth, 'fetch_managed_pages', return_value={'data': [{'id': '321', 'name': 'Brand Page', 'access_token': 'page-token', 'instagram_business_account': {'id': 'ig-1', 'username': 'brand_ig'}}]}):
            response = self.client.get('/oauth/meta/callback?state=meta-state-2&code=demo-code')
        self.assertEqual(response.status_code, 200)
        account = self.client.get(f'/profiles/{profile_id}/accounts').get_json()['data'][0]
        self.assertEqual(account['config']['pageId'], '321')
        self.assertEqual(account['config']['igUserId'], 'ig-1')
        self.assertEqual(account['config']['instagramUserName'], 'brand_ig')
        self.assertEqual(account['config']['accessToken'], 'page-token')

    def test_validate_account_config_allows_threads_oauth_without_user_id(self) -> None:
        response = self.client.post(
            '/accounts/validate-config',
            json={
                'platform': 'threads',
                'authType': 'oauth',
                'config': {},
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()['data']
        self.assertTrue(body['valid'])
        self.assertTrue(body['warnings'])

    def test_threads_oauth_start_persists_request_and_returns_authorize_url(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'Threads Brand'})
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'threads',
                'accountName': 'brand-threads',
                'authType': 'oauth',
                'config': {},
            },
        )
        account_id = account_response.get_json()['data']['id']
        with patch.object(self.sau_backend.threads_auth, 'build_authorize_url_from_env', return_value='https://threads.net/oauth/authorize?demo=1'):
            response = self.client.post('/oauth/threads/start', json={
                'profileId': profile_id,
                'accountId': account_id,
                'accountName': 'brand-threads',
                'scopes': ['threads_basic', 'threads_content_publish'],
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['data']['authorizeUrl'], 'https://threads.net/oauth/authorize?demo=1')
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute('SELECT COUNT(*) FROM threads_oauth_requests WHERE account_id = ?', (account_id,)).fetchone()
        self.assertEqual(row[0], 1)

    def test_threads_oauth_callback_updates_structured_account(self) -> None:
        profile_response = self.client.post('/profiles', json={'name': 'Threads Brand'})
        profile_id = profile_response.get_json()['data']['id']
        account_response = self.client.post(
            f'/profiles/{profile_id}/accounts',
            json={
                'platform': 'threads',
                'accountName': 'brand-threads',
                'authType': 'oauth',
                'config': {},
            },
        )
        account_id = account_response.get_json()['data']['id']
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                'INSERT INTO threads_oauth_requests (state_token, profile_id, account_id, account_name, redirect_uri, scopes_json, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
                ('threads-state-1', profile_id, account_id, 'brand-threads', 'https://up.iamwillywang.com/oauth/threads/callback', '["threads_basic", "threads_content_publish"]', 'started'),
            )
            conn.commit()
        with patch.object(self.sau_backend.threads_auth, 'exchange_code_for_token', return_value={
            'access_token': 'threads-short',
            'user_id': 'th-1',
            'expires_in': 3600,
        }), patch.object(self.sau_backend.threads_auth, 'exchange_for_long_lived_token', return_value={
            'access_token': 'threads-long',
            'expires_in': 5184000,
        }), patch.object(self.sau_backend.threads_auth, 'fetch_me', return_value={'id': 'th-1', 'username': 'threads-user'}):
            response = self.client.get('/oauth/threads/callback?state=threads-state-1&code=demo-code')
        self.assertEqual(response.status_code, 200)
        account = self.client.get(f'/profiles/{profile_id}/accounts').get_json()['data'][0]
        self.assertEqual(account['config']['threadUserId'], 'th-1')
        self.assertEqual(account['config']['threadsUserName'], 'threads-user')
        self.assertEqual(account['config']['accessToken'], 'threads-long')


if __name__ == "__main__":
    unittest.main()
