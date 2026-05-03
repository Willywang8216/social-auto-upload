"""Tests for structured account validation."""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

if "conf" not in sys.modules:
    conf_module = types.ModuleType("conf")
    conf_module.BASE_DIR = str(Path(__file__).resolve().parent.parent)
    sys.modules["conf"] = conf_module

from myUtils import account_validation
from myUtils import profiles


class AccountValidationTests(unittest.TestCase):
    def test_reddit_requires_subreddits_and_oauth_material(self) -> None:
        result = account_validation.validate_structured_account_config(
            platform=profiles.PLATFORM_REDDIT,
            auth_type='oauth',
            config={},
        )
        self.assertFalse(result.valid)
        self.assertGreaterEqual(len(result.errors), 3)

    def test_telegram_valid_config_passes(self) -> None:
        result = account_validation.validate_structured_account_config(
            platform=profiles.PLATFORM_TELEGRAM,
            auth_type='manual',
            config={'chatId': '@brand', 'botTokenEnv': 'TELEGRAM_BOT_TOKEN'},
        )
        self.assertTrue(result.valid)
        self.assertEqual(result.errors, [])

    def test_tiktok_rejects_profile_watermark(self) -> None:
        result = account_validation.validate_structured_account_config(
            platform=profiles.PLATFORM_TIKTOK,
            auth_type='oauth',
            config={'accessTokenEnv': 'TIKTOK_ACCESS_TOKEN', 'publishMode': 'direct'},
            profile_settings={'watermark': 'Brand watermark'},
        )
        self.assertFalse(result.valid)
        self.assertIn('浮水印', result.errors[0])

    def test_patreon_warns_content_only(self) -> None:
        result = account_validation.validate_structured_account_config(
            platform=profiles.PLATFORM_PATREON,
            auth_type='manual',
            config={'campaignId': 'abc'},
        )
        self.assertTrue(result.valid)
        self.assertTrue(result.warnings)

    def test_live_validation_uses_telegram_helper(self) -> None:
        from unittest.mock import patch

        with patch('myUtils.account_validation.prepared_publishers.validate_telegram_config_live', return_value={'ok': True}) as mocked:
            result = account_validation.validate_structured_account_config(
                platform=profiles.PLATFORM_TELEGRAM,
                auth_type='manual',
                config={'chatId': '@brand', 'botTokenEnv': 'TELEGRAM_BOT_TOKEN'},
                perform_live_checks=True,
            )
        self.assertTrue(result.valid)
        self.assertEqual(result.metadata['telegram'], {'ok': True})
        mocked.assert_called_once()

    def test_live_validation_failure_bubbles_into_errors(self) -> None:
        from unittest.mock import patch

        with patch('myUtils.account_validation.prepared_publishers.validate_youtube_config_live', side_effect=RuntimeError('bad token')):
            result = account_validation.validate_structured_account_config(
                platform=profiles.PLATFORM_YOUTUBE,
                auth_type='oauth',
                config={
                    'channelId': 'UC123',
                    'clientIdEnv': 'YT_CLIENT_ID',
                    'clientSecretEnv': 'YT_CLIENT_SECRET',
                    'refreshTokenEnv': 'YT_REFRESH_TOKEN',
                },
                perform_live_checks=True,
            )
        self.assertFalse(result.valid)
        self.assertIn('youtube live', result.errors[0])


if __name__ == '__main__':
    unittest.main()
