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
from myUtils.worker import PublishWorker


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
                          "metaUserAccessTokenExpiresAt": _aware(timedelta(days=30))}),
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

    def test_non_refreshable_platform_never_stale(self) -> None:
        acct = _account("douyin", {"accessToken": "a", "accessTokenExpiresAt": _aware(timedelta(minutes=1))})
        self.assertFalse(self.worker._is_account_stale(acct))


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

    def test_repeated_failures_flag_expired_and_alert_once(self):
        from pathlib import Path
        from myUtils import ops_alerts as _ops_alerts
        from sau_backend import _record_meta_selfcheck_failure, _META_SELFCHECK_FAILURE_THRESHOLD

        acct = profile_registry.Account(
            id=99, profile_id=1, platform='facebook', account_name='t',
            cookie_path='', auth_type='oauth', config={},
        )
        written: dict = {}
        alerts = {'n': 0}
        cfg: dict = {}
        with patch.object(profile_registry, 'update_account', side_effect=lambda *a, **k: written.update(config=k.get('config', {}))):
            with patch.object(_ops_alerts, 'send_ops_alert', side_effect=lambda *a, **k: alerts.__setitem__('n', alerts['n'] + 1)):
                # below the threshold: counted but not yet "expired"
                for _ in range(_META_SELFCHECK_FAILURE_THRESHOLD - 1):
                    _record_meta_selfcheck_failure(account=acct, config=cfg, db_path=Path('/tmp/none.db'), error_text='boom')
                    cfg = written.get('config', cfg)  # next tick re-reads the persisted row
                self.assertEqual(alerts['n'], 0)
                self.assertNotIn('_needsReconnect', written.get('config', {}))
                # threshold failure -> expired + alert once
                _record_meta_selfcheck_failure(account=acct, config=cfg, db_path=Path('/tmp/none.db'), error_text='boom')
                self.assertEqual(alerts['n'], 1)
                expired_cfg = written.get('config', {})
                self.assertIs(expired_cfg.get('_needsReconnect'), True)
                past = self._naive_now().isoformat(timespec='seconds')
                self.assertLess(expired_cfg['metaUserAccessTokenExpiresAt'], past)
                self.assertLess(expired_cfg['accessTokenExpiresAt'], past)
                # further failures must not re-alert
                cfg = expired_cfg
                _record_meta_selfcheck_failure(account=acct, config=cfg, db_path=Path('/tmp/none.db'), error_text='boom')
                self.assertEqual(alerts['n'], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
