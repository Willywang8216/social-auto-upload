"""Regression tests for the X/Twitter OAuth 2.0 token expiry + refresh bug.

Covers the three defects that let account 124's 2-hour access token lapse
silently and then permanently strand its rotated refresh token:

1. mixed naive/aware timestamp bases for ``accessTokenUpdatedAt`` /
   ``accessTokenExpiresAt`` (the reader treated a naive-local expiry as UTC, so
   a token looked valid for one UTC offset too long);
2. lazy refresh (the publish path only, so an account with no near-term job was
   never refreshed) — the staleness helper now treats an expired/unknown token
   as stale so the proactive maintenance passes fire before it lapses;
3. the refresh-token rotation race — refresh is now single-flight per account
   and re-reads the authoritative row under the lock, so a losing caller reuses
   the winner's rotated token instead of burning the old one.
"""

from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from myUtils import prepared_publishers
from myUtils.worker import PublishWorker

# The exact aware expiry stored on account 124 at the failure time, and a
# fixed "now" from the incident window (2026-10-06 ~05:05 UTC).
PAST_AWARE = "2026-10-05T06:39:59+00:00"
NOW = datetime(2026, 10, 6, 5, 5, 0, tzinfo=timezone.utc)


class TokenExpiryMathTests(unittest.TestCase):
    def test_aware_expiry_is_expired(self) -> None:
        with patch.object(prepared_publishers, "_utc_now", return_value=NOW):
            self.assertTrue(
                prepared_publishers._x_access_token_stale(
                    {"accessToken": "t", "accessTokenExpiresAt": PAST_AWARE}
                )
            )

    def test_aware_expiry_is_fresh(self) -> None:
        fresh = (NOW + timedelta(hours=2)).isoformat()
        with patch.object(prepared_publishers, "_utc_now", return_value=NOW):
            self.assertFalse(
                prepared_publishers._x_access_token_stale(
                    {"accessToken": "t", "accessTokenExpiresAt": fresh}
                )
            )

    def test_missing_or_blank_expiry_is_stale(self) -> None:
        self.assertTrue(prepared_publishers._x_access_token_stale({"accessToken": "t"}))
        self.assertTrue(
            prepared_publishers._x_access_token_stale(
                {"accessToken": "t", "accessTokenExpiresAt": "  "}
            )
        )

    def test_unparseable_expiry_is_stale(self) -> None:
        self.assertTrue(
            prepared_publishers._x_access_token_stale(
                {"accessToken": "t", "accessTokenExpiresAt": "not-a-date"}
            )
        )

    def test_naive_legacy_expiry_is_read_as_local_not_utc(self) -> None:
        # The root defect: writers stamped ``datetime.now()`` (naive local) while
        # readers assumed naive meant UTC, shifting a 2h token by the server's
        # UTC offset. The parser must recover the local instant.
        naive = "2026-10-05T12:39:59"
        parsed = prepared_publishers._parse_token_expiry(naive)
        expected = (
            datetime(2026, 10, 5, 12, 39, 59).astimezone().astimezone(timezone.utc)
        )
        self.assertEqual(parsed, expected)

    def test_naive_local_already_expired_is_stale(self) -> None:
        local_now = datetime.now().astimezone()
        naive_past = (
            (local_now - timedelta(minutes=10))
            .replace(tzinfo=None, microsecond=0)
            .isoformat()
        )
        self.assertTrue(
            prepared_publishers._x_access_token_stale(
                {"accessToken": "t", "accessTokenExpiresAt": naive_past}
            )
        )

    def test_token_payload_writes_one_utc_base(self) -> None:
        with patch.object(prepared_publishers, "_utc_now", return_value=NOW):
            updated = prepared_publishers._apply_twitter_token_payload(
                {"refreshToken": "old"},
                {"access_token": "new", "refresh_token": "rt2", "expires_in": 7200},
            )
        updated_at = updated["accessTokenUpdatedAt"]
        self.assertTrue(updated_at.endswith("+00:00"), updated_at)
        self.assertEqual(updated_at, NOW.replace(microsecond=0).isoformat())
        self.assertEqual(
            prepared_publishers._parse_token_expiry(updated["accessTokenExpiresAt"]),
            NOW + timedelta(hours=2),
        )


class WorkerProactiveStalenessTests(unittest.TestCase):
    def _worker(self) -> PublishWorker:
        return PublishWorker(lambda *a, **k: None)

    def test_legacy_naive_local_expiry_is_detected_stale(self) -> None:
        local_now = datetime.now().astimezone()
        naive_past = (
            (local_now - timedelta(minutes=5))
            .replace(tzinfo=None, microsecond=0)
            .isoformat()
        )
        account = SimpleNamespace(
            id=1,
            profile_id=1,
            platform="twitter",
            account_name="x",
            auth_type="oauth",
            config={
                "twitterAuthType": "api",
                "accessToken": "t",
                "refreshToken": "r",
                "accessTokenExpiresAt": naive_past,
            },
        )
        self.assertTrue(self._worker()._is_account_stale(account))

    def test_missing_expiry_is_stale_so_it_gets_refreshed(self) -> None:
        account = SimpleNamespace(
            id=1,
            profile_id=1,
            platform="twitter",
            account_name="x",
            auth_type="oauth",
            config={
                "twitterAuthType": "api",
                "accessToken": "t",
                "refreshToken": "r",
            },
        )
        self.assertTrue(self._worker()._is_account_stale(account))


class ProactiveRefreshTests(unittest.TestCase):
    def _store(self, config):
        state = {"config": dict(config)}
        guard = threading.Lock()

        def read_latest(account_id, db_path=None):
            with guard:
                return dict(state["config"])

        def persist(updated):
            with guard:
                state["config"] = dict(updated)

        return state, guard, read_latest, persist

    def test_expired_token_is_refreshed_before_use_and_rotation_persisted(self) -> None:
        state, _guard, read_latest, persist = self._store({
            "twitterAuthType": "api",
            "accessToken": "old",
            "refreshToken": "rt1",
            "accessTokenExpiresAt": PAST_AWARE,
        })
        calls = []

        def fake_refresh(config, session=None):
            calls.append(config.get("refreshToken"))
            return {
                "access_token": "new-access",
                "refresh_token": "rt2",
                "expires_in": 7200,
                "scope": "tweet.read tweet.write offline.access",
                "token_type": "bearer",
            }

        with patch.object(
            prepared_publishers, "_read_account_config", side_effect=read_latest,
        ), patch.object(
            prepared_publishers, "refresh_twitter_access_token", side_effect=fake_refresh,
        ):
            updated, refreshed = prepared_publishers.refresh_twitter_token_single_flight(
                dict(state["config"]), account_id=1, persist=persist,
            )

        self.assertTrue(refreshed)
        self.assertEqual(calls, ["rt1"])
        self.assertEqual(updated["refreshToken"], "rt2")
        self.assertEqual(state["config"]["refreshToken"], "rt2")

    def test_fresh_authoritative_row_skips_the_network(self) -> None:
        fresh = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        state, _guard, read_latest, persist = self._store({
            "twitterAuthType": "api",
            "accessToken": "fresh",
            "refreshToken": "rt1",
            "accessTokenExpiresAt": fresh,
        })
        calls = []

        with patch.object(
            prepared_publishers, "_read_account_config", side_effect=read_latest,
        ), patch.object(
            prepared_publishers,
            "refresh_twitter_access_token",
            side_effect=lambda *a, **k: calls.append("called"),
        ):
            updated, refreshed = prepared_publishers.refresh_twitter_token_single_flight(
                {
                    "twitterAuthType": "api",
                    "accessToken": "stale",
                    "refreshToken": "rt0",
                    "accessTokenExpiresAt": PAST_AWARE,
                },
                account_id=1,
                persist=persist,
            )

        self.assertFalse(refreshed)
        self.assertEqual(calls, [])
        self.assertEqual(updated["accessToken"], "fresh")
        self.assertEqual(updated["refreshToken"], "rt1")


class SingleFlightRefreshTests(unittest.TestCase):
    def test_concurrent_refreshes_use_one_network_call(self) -> None:
        state = {
            "config": {
                "twitterAuthType": "api",
                "accessToken": "old",
                "refreshToken": "rt1",
                "accessTokenExpiresAt": PAST_AWARE,
            }
        }
        guard = threading.Lock()
        calls = []
        barrier = threading.Barrier(2)

        def read_latest(account_id, db_path=None):
            with guard:
                return dict(state["config"])

        def persist(updated):
            with guard:
                state["config"] = dict(updated)

        def fake_refresh(config, session=None):
            calls.append(config.get("refreshToken"))
            time.sleep(0.05)  # widen the race window
            return {
                "access_token": "new-access",
                "refresh_token": "rt2",
                "expires_in": 7200,
                "scope": "tweet.read tweet.write offline.access",
                "token_type": "bearer",
            }

        results = []
        errors = []

        def run():
            try:
                barrier.wait(timeout=5)
                results.append(
                    prepared_publishers.refresh_twitter_token_single_flight(
                        dict(state["config"]), account_id=99, persist=persist,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - surface thread failures
                errors.append(exc)

        with patch.object(
            prepared_publishers, "_read_account_config", side_effect=read_latest,
        ), patch.object(
            prepared_publishers, "refresh_twitter_access_token", side_effect=fake_refresh,
        ):
            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertEqual(errors, [])
        # Only one caller burned the single-use token; the loser reused rt2.
        self.assertEqual(calls, ["rt1"])
        self.assertEqual(state["config"]["refreshToken"], "rt2")
        self.assertEqual(sorted(result[1] for result in results), [False, True])


class ReconnectClassificationTests(unittest.TestCase):
    def test_invalid_grant_requires_reconnect(self) -> None:
        from myUtils.x_auth import TwitterOAuthError

        exc = TwitterOAuthError(
            "X OAuth refresh failed: HTTP 400 (invalid_grant)",
            status_code=400,
            error_code="invalid_grant",
        )
        self.assertTrue(prepared_publishers._twitter_refresh_requires_reconnect(exc))

    def test_transport_error_is_retryable(self) -> None:
        self.assertFalse(
            prepared_publishers._twitter_refresh_requires_reconnect(
                RuntimeError("network timeout")
            )
        )

    def test_rejected_refresh_token_surfaces_reconnect_required(self) -> None:
        from myUtils.x_auth import TwitterOAuthError

        config = {
            "twitterAuthType": "api",
            "accessToken": "old",
            "refreshToken": "rt1",
            "accessTokenExpiresAt": PAST_AWARE,
        }
        persisted = []
        rejected = TwitterOAuthError(
            "X OAuth refresh failed: HTTP 400 (invalid_grant)",
            status_code=400,
            error_code="invalid_grant",
        )
        with patch.object(
            prepared_publishers, "_read_account_config", return_value=None,
        ), patch.object(
            prepared_publishers, "refresh_twitter_access_token", side_effect=rejected,
        ):
            with self.assertRaisesRegex(
                prepared_publishers.PreparedPublishError, "reconnect this account",
            ) as raised:
                prepared_publishers._maybe_refresh_twitter_token(
                    config, on_refresh=persisted.append,
                )
        self.assertFalse(raised.exception.retryable)
        self.assertTrue(persisted)
        self.assertTrue(persisted[0].get("_needsReconnect"))
        self.assertEqual(
            persisted[0].get("_lastMaintenanceError"),
            "X OAuth 2.0 refresh token was rejected; reconnect required",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
