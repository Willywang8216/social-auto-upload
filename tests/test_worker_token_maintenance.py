"""Tests for OAuth token-refresh staleness detection.

These cover the platform-aware refresh margin and the naive/aware datetime
normalisation added so long-lived-token platforms (Facebook/Instagram/Threads)
are refreshed with a generous buffer without stranding short-lived platforms or
crashing on timezone-aware stored timestamps.
"""

from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from myUtils import profiles as profile_registry
from myUtils import token_state
from myUtils.worker import PublishWorker, _persist_rotated_config


def _aware(delta: timedelta) -> str:
    """ISO timestamp WITH a +00:00 offset (as several OAuth callbacks store)."""
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")


def _naive(delta: timedelta) -> str:
    """ISO timestamp with no offset (as the Meta callback stores)."""
    return (datetime.now(timezone.utc).replace(tzinfo=None) + delta).isoformat(timespec="seconds")


def _account(platform: str, config: dict) -> profile_registry.Account:
    return profile_registry.Account(
        id=1,
        profile_id=1,
        platform=platform,
        account_name="acct",
        cookie_path="",
        auth_type="oauth",
        config=config,
    )


def _managed_tiktok() -> profile_registry.Account:
    return profile_registry.Account(
        id=3, profile_id=3, platform="tiktok", account_name="Synthetic_SW_TK",
        cookie_path="", auth_type="api", enabled=True, status=1,
        config={"publishingProvider": "sociamonials", "sociamonials": {
            "network": "tiktok", "workspace_id": 26985, "profile_refs": [3917]}},
    )


async def _noop_executor(platform, payload, target):  # pragma: no cover - never run
    return None


class WorkerStalenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.worker = PublishWorker(_noop_executor)

    def test_aware_timestamp_does_not_raise(self) -> None:
        # Regression: worker._utc_now() is naive; a tz-aware stored expiry used
        # to raise "can't compare offset-naive and offset-aware datetimes".
        acct = _account("tiktok", {
            "accessToken": "a", "refreshToken": "r",
            "accessTokenExpiresAt": _aware(timedelta(days=200)),
        })
        self.assertFalse(self.worker._is_account_stale(acct))

    def test_long_lived_platforms_refresh_within_seven_days(self) -> None:
        # A 60-day token 3 days from expiry must be flagged stale early so a
        # restart / transient error near expiry can't strand the account.
        for platform, config in (
            ("facebook", {"metaUserAccessToken": "u", "accessToken": "p",
                          "metaUserAccessTokenExpiresAt": _aware(timedelta(days=3))}),
            ("instagram", {"metaUserAccessToken": "u", "accessToken": "p",
                           "metaUserAccessTokenExpiresAt": _aware(timedelta(days=3))}),
            ("threads", {"accessToken": "a", "accessTokenExpiresAt": _aware(timedelta(days=3))}),
        ):
            with self.subTest(platform=platform):
                self.assertTrue(self.worker._is_account_stale(_account(platform, config)))

    def test_long_lived_platforms_not_stale_when_far_out(self) -> None:
        for platform, config in (
            ("facebook", {"metaUserAccessToken": "u", "accessToken": "p",
                          'metaUserAccessTokenExpiresAt': _aware(timedelta(days=30)),
                          '_lastTokenSelfCheckAt': _aware(timedelta())}),
            ("threads", {"accessToken": "a", "accessTokenExpiresAt": _naive(timedelta(days=30))}),
        ):
            with self.subTest(platform=platform):
                self.assertFalse(self.worker._is_account_stale(_account(platform, config)))

    def test_short_lived_platforms_keep_tight_skew(self) -> None:
        # reddit/twitter/tiktok/youtube must NOT be refreshed 3 days early —
        # that would churn tokens and risk rate limits.
        for platform in ("reddit", "twitter", "tiktok", "youtube"):
            config = {"accessToken": "a", "refreshToken": "r",
                      "accessTokenExpiresAt": _aware(timedelta(days=3))}
            with self.subTest(platform=platform):
                self.assertFalse(self.worker._is_account_stale(_account(platform, config)))

    def test_short_lived_stale_within_five_minutes(self) -> None:
        acct = _account("reddit", {
            "accessToken": "a", "refreshToken": "r",
            "accessTokenExpiresAt": _aware(timedelta(minutes=2)),
        })
        self.assertTrue(self.worker._is_account_stale(acct))

    def test_cookie_twitter_never_stale(self) -> None:
        acct = _account("twitter", {"twitterAuthType": "cookie"})
        self.assertFalse(self.worker._is_account_stale(acct))

    def test_reconnect_required_twitter_is_not_refreshed(self) -> None:
        acct = _account("twitter", {
            "twitterAuthType": "api",
            "accessToken": "expired",
            "refreshToken": "present",
            "accessTokenExpiresAt": _aware(timedelta(minutes=-10)),
            "_needsReconnect": True,
        })
        self.assertFalse(self.worker._is_account_stale(acct))

    def test_non_refreshable_platform_never_stale(self) -> None:
        acct = _account("douyin", {"accessToken": "a", "accessTokenExpiresAt": _aware(timedelta(minutes=1))})
        self.assertFalse(self.worker._is_account_stale(acct))

    def test_managed_tiktok_does_not_require_native_oauth(self) -> None:
        managed = _managed_tiktok()
        self.assertFalse(self.worker._is_account_stale(managed))
        native = _account("tiktok", {"sociamonials": managed.config["sociamonials"]})
        self.assertTrue(self.worker._is_account_stale(native))


flask_available = importlib.util.find_spec("flask") is not None


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class BackendEffectiveSkewTests(unittest.TestCase):
    def test_effective_skew_widens_only_long_lived(self) -> None:
        from sau_backend import _effective_refresh_skew, _LONG_LIVED_REFRESH_MARGIN_SECONDS

        self.assertEqual(_effective_refresh_skew("facebook", 3600), _LONG_LIVED_REFRESH_MARGIN_SECONDS)
        self.assertEqual(_effective_refresh_skew("instagram", 3600), _LONG_LIVED_REFRESH_MARGIN_SECONDS)
        self.assertEqual(_effective_refresh_skew("threads", 3600), _LONG_LIVED_REFRESH_MARGIN_SECONDS)
        # Short-lived platforms are returned untouched.
        self.assertEqual(_effective_refresh_skew("reddit", 3600), 3600)
        self.assertEqual(_effective_refresh_skew("youtube", 3600), 3600)
        self.assertEqual(_effective_refresh_skew("tiktok", 3600), 3600)

    def test_effective_skew_never_narrows(self) -> None:
        from sau_backend import _effective_refresh_skew, _LONG_LIVED_REFRESH_MARGIN_SECONDS

        # If the caller already asks for a wider window, keep it.
        wide = _LONG_LIVED_REFRESH_MARGIN_SECONDS + 10_000
        self.assertEqual(_effective_refresh_skew("facebook", wide), wide)

    def test_is_refreshable_account_stale_honours_margin(self) -> None:
        from sau_backend import _is_refreshable_account_stale

        fb = _account("facebook", {
            "metaUserAccessToken": "u", "accessToken": "p",
            "metaUserAccessTokenExpiresAt": _aware(timedelta(days=3)),
        })
        # 1-hour maintenance window, but the 7-day margin flags it stale.
        self.assertTrue(_is_refreshable_account_stale(fb, skew_seconds=3600))

        reddit = _account("reddit", {
            "accessToken": "a", "accessTokenExpiresAt": _aware(timedelta(days=3)),
        })
        self.assertFalse(_is_refreshable_account_stale(reddit, skew_seconds=3600))

        broken_twitter = _account("twitter", {
            "twitterAuthType": "api", "accessToken": "expired", "refreshToken": "present",
            "accessTokenExpiresAt": _aware(timedelta(days=-1)), "_needsReconnect": True,
        })
        self.assertFalse(_is_refreshable_account_stale(broken_twitter, skew_seconds=3600))
        recovered_twitter = _account("twitter", {
            "twitterAuthType": "api", "accessToken": "new", "refreshToken": "rotated",
            "accessTokenExpiresAt": _aware(timedelta(days=1)),
        })
        self.assertFalse(_is_refreshable_account_stale(recovered_twitter, skew_seconds=3600))
        backed_off_twitter = _account("twitter", {
            "twitterAuthType": "api", "accessToken": "expired", "refreshToken": "present",
            "accessTokenExpiresAt": _aware(timedelta(days=-1)),
            "_nextMaintenanceAttemptAt": _aware(timedelta(minutes=5)),
        })
        self.assertFalse(_is_refreshable_account_stale(backed_off_twitter, skew_seconds=3600))

    def test_managed_tiktok_does_not_require_native_oauth(self) -> None:
        from sau_backend import _is_refreshable_account_stale

        managed = _managed_tiktok()
        self.assertFalse(_is_refreshable_account_stale(managed))
        native = _account("tiktok", {"sociamonials": managed.config["sociamonials"]})
        self.assertTrue(_is_refreshable_account_stale(native))


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class MetaLiveSelfCheckTests(unittest.TestCase):
    """The 12-hour live self-check that keeps never-expiring Meta tokens honest:
    due cadence, faster retry after a failure, and "all fail -> expired + alert".
    """

    def _naive_now(self):
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def _ago(self, hours):
        return (self._naive_now() - timedelta(hours=hours)).isoformat(timespec='seconds')

    def test_self_check_due_when_no_stamp(self):
        from sau_backend import _meta_self_check_due
        self.assertTrue(_meta_self_check_due({}))

    def test_self_check_not_due_right_after_stamp(self):
        from sau_backend import _meta_self_check_due
        cfg = {'_lastTokenSelfCheckAt': self._naive_now().isoformat(timespec='seconds'),
               '_tokenSelfCheckFailures': '0'}
        self.assertFalse(_meta_self_check_due(cfg))

    def test_self_check_due_after_healthy_cadence(self):
        from sau_backend import _meta_self_check_due, _META_SELFCHECK_HOURS
        cfg = {'_lastTokenSelfCheckAt': self._ago(_META_SELFCHECK_HOURS + 1),
               '_tokenSelfCheckFailures': '0'}
        self.assertTrue(_meta_self_check_due(cfg))

    def test_failure_retries_sooner_than_healthy_cadence(self):
        from sau_backend import _meta_self_check_due
        # 2h ago, healthy cadence (12h) -> not due yet
        self.assertFalse(_meta_self_check_due({'_lastTokenSelfCheckAt': self._ago(2), '_tokenSelfCheckFailures': '0'}))
        # 2h ago with a recorded failure (1h retry) -> due
        self.assertTrue(_meta_self_check_due({'_lastTokenSelfCheckAt': self._ago(2), '_tokenSelfCheckFailures': '1'}))

    def test_transient_failures_never_fabricate_expiry_and_terminal_alerts_once(self):
        import tempfile
        from pathlib import Path
        from db.createTable import bootstrap
        from myUtils import ops_alerts
        from sau_backend import _record_meta_selfcheck_failure
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'auth.db'
            bootstrap(db_path)
            profile = profile_registry.create_profile('Synthetic', db_path=db_path)
            expiry = _aware(timedelta(days=30))
            account = profile_registry.add_account(profile.id, 'facebook', 'synthetic', auth_type='oauth',
                config={'pageId': 'p', 'accessToken': 'page', 'metaUserAccessToken': 'user',
                        'accessTokenExpiresAt': expiry, 'metaUserAccessTokenExpiresAt': expiry}, db_path=db_path)
            with patch.object(ops_alerts, 'send_ops_alert') as alert:
                for _ in range(5):
                    _record_meta_selfcheck_failure(account=account, config=account.config, db_path=db_path, exc=TimeoutError())
                    account = profile_registry.get_account(account.id, db_path=db_path)
                self.assertFalse(account.config.get('_needsReconnect'))
                self.assertEqual(account.config['accessTokenExpiresAt'], expiry)
                self.assertEqual(account.config['metaUserAccessTokenExpiresAt'], expiry)
                alert.assert_not_called()
                terminal = token_state.provider_error({'code': 190, 'error_subcode': 460})
                # Even identical concurrent snapshots cannot alert twice.
                for _ in range(2):
                    _record_meta_selfcheck_failure(account=account, config=account.config, db_path=db_path, exc=terminal)
                stored = profile_registry.get_account(account.id, db_path=db_path).config
                self.assertTrue(stored['_needsReconnect'])
                self.assertEqual(stored['accessTokenExpiresAt'], expiry)
                self.assertEqual(stored['_nativeCredentialError']['subcode'], 460)
                alert.assert_called_once()


class PersistRotatedConfigTests(unittest.TestCase):
    """A rotated single-use refresh token must land in the account's own DB."""

    def test_uses_payload_db_path(self):
        from pathlib import Path as _Path
        account = _account("twitter", {})
        seen: dict = {}
        with patch.object(
            token_state, 'commit_success',
            side_effect=lambda account, changes, **k: seen.update(config=changes, **k) or account,
        ):
            _persist_rotated_config(
                account, {"accessToken": "new"}, payload={"_db_path": "/tmp/tenant.db"},
            )
        self.assertEqual(seen.get("db_path"), _Path("/tmp/tenant.db"))
        self.assertEqual(seen.get("config"), {"accessToken": "new"})

    def test_uses_registry_db_when_payload_has_none(self):
        account = _account("twitter", {})
        seen: dict = {}
        with patch.object(
            token_state, 'commit_success',
            side_effect=lambda account, changes, **k: seen.update(config=changes, **k) or account,
        ):
            _persist_rotated_config(account, {"accessToken": "new"}, payload={})
        self.assertEqual(seen.get('db_path'), profile_registry.DB_PATH)

    def test_empty_config_is_a_noop(self):
        account = _account("twitter", {})
        with patch.object(token_state, 'commit_success') as upd:
            _persist_rotated_config(account, {}, payload={"_db_path": "/tmp/x.db"})
        upd.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
