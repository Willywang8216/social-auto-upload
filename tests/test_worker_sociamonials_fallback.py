"""Tests for the Sociamonials fallback hook in the publish worker.

When a target exhausts its retry budget the worker hands the post to
Sociamonials instead of declaring it permanently failed. These tests pin the
contract:

* the fallback only fires after a genuine retry-budget exhaustion (never on a
  target the publisher classified non-retryable, never before the last
  attempt);
* a successful fallback transitions the target to ``succeeded``;
* every fallback failure path falls through to the normal permanent-failure
  transition — the feature must never turn a failure into a silent loss.

The real publisher is stubbed: these tests assert the *wiring*, and
``tests/test_sociamonials_fallback.py`` covers the API client itself.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db.createTable as create_table
from myUtils import jobs
from myUtils import profiles as profile_registry
from myUtils import sociamonials_fallback
from myUtils.worker import PublishWorker, RetryPolicy, WorkerConfig


class _Boom(RuntimeError):
    """Failure the publisher classified as retryable (the default)."""


class _BoomPermanent(RuntimeError):
    """Failure the publisher classified as permanent."""

    retryable = False


class SociamonialsFallbackHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "worker.db"
        create_table.bootstrap(self.db_path)
        self.profile_id = profile_registry.create_profile(
            "Fallback Test", db_path=self.db_path
        ).id
        self.account_id = profile_registry.add_account(
            self.profile_id,
            platform="bluesky",
            account_name="fb-bluesky",
            auth_type="api",
            config={},
            db_path=self.db_path,
        ).id

        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for key in (
            "SAU_PUBLIC_APP_URL",
            "SAU_PUBLIC_BASE_URL",
            "SAU_SOCIAMONIALS_FALLBACK",
            "SOCIAMONIALS_API_KEY",
        ):
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _enable_fallback(self) -> None:
        os.environ["SAU_SOCIAMONIALS_FALLBACK"] = "1"
        os.environ["SOCIAMONIALS_API_KEY"] = "sm_agent_test"

    def _enqueue_failing_target(self, exc_type=type("Boom", (RuntimeError,), {})) -> int:
        job = jobs.enqueue_job(
            jobs.JobSpec(
                platform="bluesky",
                payload={
                    "campaignId": 1,
                    "campaignPostId": 1,
                    "draft": {"message": "hi"},
                },
                targets=[(f"account:{self.account_id}", "campaign_post:1", None)],
                profile_id=self.profile_id,
                idempotency_key=f"fallback-{self.account_id}",
            ),
            db_path=self.db_path,
        )
        self._job_id = job.id
        return job.id

    def _drain(self, exc_type=RuntimeError) -> None:
        async def executor(platform, payload, target):
            raise exc_type("bluesky said no")

        config = WorkerConfig(
            poll_interval=0.001,
            batch_size=4,
            max_concurrent=1,
            retry=RetryPolicy(
                max_attempts=2, base_backoff_seconds=0.001, max_backoff_seconds=0.01
            ),
        )
        worker = PublishWorker(executor, config=config, db_path=self.db_path)
        asyncio.run(worker.drain())

    def _status(self) -> str:
        targets = jobs.list_targets(self._job_id, db_path=self.db_path)
        self.assertEqual(len(targets), 1)
        return targets[0].status

    # ------------------------------------------------------------------
    # disabled / non-retryable guards
    # ------------------------------------------------------------------

    def test_flag_off_falls_through_to_permanent_failure(self) -> None:
        with patch.object(
            sociamonials_fallback, "publish_via_sociamonials"
        ) as patched:
            self._enqueue_failing_target()
            self._drain()
        patched.assert_not_called()
        self.assertEqual(self._status(), jobs.TARGET_FAILED)

    def test_non_retryable_failure_never_reaches_the_fallback(self) -> None:
        self._enable_fallback()
        with patch.object(
            sociamonials_fallback, "publish_via_sociamonials"
        ) as patched:
            self._enqueue_failing_target()
            self._drain(exc_type=_BoomPermanent)
        patched.assert_not_called()
        self.assertEqual(self._status(), jobs.TARGET_FAILED)

    def test_non_retryable_x_failure_falls_back_to_sociamonials(self) -> None:
        """A dead/flagged X credential must still reach the fallback.

        Sociamonials holds its own X OAuth connection, so a ``retryable=False``
        X reconnect failure is deliverable even though a permanent failure on
        any other platform must not be re-routed.
        """
        self._enable_fallback()
        twitter_account_id = profile_registry.add_account(
            self.profile_id,
            platform="twitter",
            account_name="x-acct",
            auth_type="oauth",
            config={},
            db_path=self.db_path,
        ).id
        job = jobs.enqueue_job(
            jobs.JobSpec(
                platform="twitter",
                payload={"draft": {"message": "hi"}},
                targets=[(f"account:{twitter_account_id}", "campaign_post:1", None)],
                profile_id=self.profile_id,
                idempotency_key=f"x-fallback-{twitter_account_id}",
            ),
            db_path=self.db_path,
        )
        self._job_id = job.id
        with patch.object(
            sociamonials_fallback,
            "publish_via_sociamonials",
            return_value={
                "ok": True,
                "post_id": 77,
                "status": "scheduled",
                "network": "tw",
                "delivered": None,
                "delivery_state": "pending",
                "warnings": [],
            },
        ) as patched:
            self._drain(exc_type=_BoomPermanent)
        patched.assert_called_once()
        self.assertEqual(self._status(), jobs.TARGET_SUCCEEDED)

    def test_refresh_failure_does_not_clobber_a_rotated_token(self) -> None:
        """The failure handler must merge into the latest row, not the snapshot.

        The refresh endpoint rotates the single-use refresh token, so the
        publish path can win a race and persist a fresh token while the
        maintenance attempt is still in flight. Writing the pre-refresh
        snapshot back would delete that token and strand the account.
        """
        account_id = profile_registry.add_account(
            self.profile_id,
            platform="twitter",
            account_name="race-acct",
            auth_type="oauth",
            config={"refreshToken": "old", "_maintenanceFailures": 4},
            db_path=self.db_path,
        ).id
        stale_snapshot = dict(profile_registry.get_account(account_id, db_path=self.db_path).config)
        # A concurrent publish-path refresh persists the rotated token first.
        profile_registry.update_account(
            account_id,
            config={"refreshToken": "rotated", "accessToken": "fresh"},
            db_path=self.db_path,
        )

        async def executor(platform, payload, target):  # pragma: no cover - unused
            return None

        worker = PublishWorker(executor, config=WorkerConfig(), db_path=self.db_path)
        account = profile_registry.get_account(account_id, db_path=self.db_path)
        worker._handle_refresh_failure(account, stale_snapshot, RuntimeError("boom"))

        persisted = profile_registry.get_account(account_id, db_path=self.db_path).config
        self.assertEqual(persisted.get("refreshToken"), "rotated")
        self.assertEqual(persisted.get("accessToken"), "fresh")
        self.assertEqual(persisted.get("_maintenanceFailures"), 1)

    # ------------------------------------------------------------------
    # success path
    # ------------------------------------------------------------------

    def test_successful_fallback_marks_the_target_succeeded(self) -> None:
        self._enable_fallback()
        with patch.object(
            sociamonials_fallback,
            "publish_via_sociamonials",
            return_value={
                "ok": True,
                "post_id": 4242,
                "status": "delivered",
                "network": "blsk",
                "requires_approval": False,
                "warnings": [],
            },
        ) as patched:
            self._enqueue_failing_target()
            self._drain()

        patched.assert_called_once()
        self.assertEqual(self._status(), jobs.TARGET_SUCCEEDED)

    # ------------------------------------------------------------------
    # failure paths must not lose the post
    # ------------------------------------------------------------------

    def test_fallback_api_error_falls_through_to_permanent_failure(self) -> None:
        self._enable_fallback()
        with patch.object(
            sociamonials_fallback,
            "publish_via_sociamonials",
            side_effect=sociamonials_fallback.SociamonialsFallbackError("no mapping"),
        ):
            self._enqueue_failing_target()
            self._drain()
        self.assertEqual(self._status(), jobs.TARGET_FAILED)

    def test_fallback_not_configured_does_not_raise(self) -> None:
        self._enable_fallback()
        with patch.object(
            sociamonials_fallback,
            "publish_via_sociamonials",
            side_effect=sociamonials_fallback.SociamonialsNotConfigured("off"),
        ):
            self._enqueue_failing_target()
            self._drain()
        self.assertEqual(self._status(), jobs.TARGET_FAILED)

    def test_unexpected_fallback_exception_is_contained(self) -> None:
        self._enable_fallback()
        with patch.object(
            sociamonials_fallback,
            "publish_via_sociamonials",
            side_effect=RuntimeError("kaboom"),
        ):
            self._enqueue_failing_target()
            self._drain()
        # A crash inside the fallback must not escape into the worker loop.
        self.assertEqual(self._status(), jobs.TARGET_FAILED)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class SociamonialsFallbackMissingAccountTests(unittest.TestCase):
    """A deleted account must not crash the worker through the fallback.

    ``_resolve_structured_account`` raises LookupError when the account row is
    gone. Targets for a deleted account can still be queued (the FK is
    ON DELETE SET NULL, not CASCADE), so the fallback has to treat that as
    "cannot fall back" and let the caller mark the target permanently failed -
    not escape into the worker loop, which previously killed the whole task.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "worker.db"
        create_table.bootstrap(self.db_path)
        self.profile_id = profile_registry.create_profile(
            "Missing Account", db_path=self.db_path
        ).id
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ["SAU_SOCIAMONIALS_FALLBACK"] = "1"
        os.environ["SOCIAMONIALS_API_KEY"] = "sm_agent_test"

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_missing_account_falls_through_without_crashing(self) -> None:
        job = jobs.enqueue_job(
            jobs.JobSpec(
                platform="bluesky",
                payload={"draft": {"message": "hi"}},
                # account:999999 has no row.
                targets=[("account:999999", "campaign_post:1", None)],
                profile_id=self.profile_id,
                idempotency_key="missing-account",
            ),
            db_path=self.db_path,
        )

        async def executor(platform, payload, target):
            raise RuntimeError("bluesky said no")

        config = WorkerConfig(
            poll_interval=0.001,
            batch_size=4,
            max_concurrent=1,
            retry=RetryPolicy(
                max_attempts=1, base_backoff_seconds=0.001, max_backoff_seconds=0.01
            ),
        )
        worker = PublishWorker(executor, config=config, db_path=self.db_path)
        with patch.object(
            sociamonials_fallback, "publish_via_sociamonials"
        ) as patched:
            asyncio.run(worker.drain())
        patched.assert_not_called()

        targets = jobs.list_targets(job.id, db_path=self.db_path)
        self.assertEqual(targets[0].status, jobs.TARGET_FAILED)


class SociamonialsDeliveryVerificationTests(unittest.TestCase):
    """A post Sociamonials accepts but fails to deliver is NOT a success.

    Submission returns 200 as soon as the post is queued; the platform hand-off
    happens after. Three real fallbacks were marked succeeded while Sociamonials
    reported ``delivery_status: failed`` ("Video URL not accessible after
    retries"), so nothing ever reached X. The worker must record the failure.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "worker.db"
        create_table.bootstrap(self.db_path)
        self.profile_id = profile_registry.create_profile(
            "Delivery", db_path=self.db_path
        ).id
        self.account_id = profile_registry.add_account(
            self.profile_id,
            platform="twitter",
            account_name="deliv",
            auth_type="oauth",
            config={},
            db_path=self.db_path,
        ).id
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ["SAU_SOCIAMONIALS_FALLBACK"] = "1"
        os.environ["SOCIAMONIALS_API_KEY"] = "sm_agent_test"

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _run(self, result: dict) -> tuple[int, str]:
        job = jobs.enqueue_job(
            jobs.JobSpec(
                platform="twitter",
                payload={"draft": {"message": "hi"}},
                targets=[(f"account:{self.account_id}", "campaign_post:1", None)],
                profile_id=self.profile_id,
                idempotency_key=f"deliv-{id(result)}",
            ),
            db_path=self.db_path,
        )

        async def executor(platform, payload, target):
            raise RuntimeError("X media upload failed (HTTP 402)")

        config = WorkerConfig(
            poll_interval=0.001,
            batch_size=4,
            max_concurrent=1,
            retry=RetryPolicy(
                max_attempts=1, base_backoff_seconds=0.001, max_backoff_seconds=0.01
            ),
        )
        worker = PublishWorker(executor, config=config, db_path=self.db_path)
        with patch.object(
            sociamonials_fallback, "publish_via_sociamonials", return_value=result
        ):
            asyncio.run(worker.drain())
        target = jobs.list_targets(job.id, db_path=self.db_path)[0]
        return target.status, str(target.last_error or "")

    def test_failed_delivery_is_recorded_as_failed(self):
        status, error = self._run({
            "ok": True,
            "post_id": 10689041,
            "status": "scheduled",
            "network": "tw",
            "delivery_state": "failed",
            "delivered": False,
            "delivery_error": "Video URL not accessible after retries",
            "requires_approval": False,
            "warnings": [],
        })
        self.assertEqual(status, jobs.TARGET_FAILED)
        self.assertIn("delivery failed", error)

    def test_delivered_post_is_recorded_as_succeeded(self):
        status, _ = self._run({
            "ok": True,
            "post_id": 1,
            "status": "delivered",
            "network": "tw",
            "delivery_state": "delivered",
            "delivered": True,
            "delivery_error": "",
            "requires_approval": False,
            "warnings": [],
        })
        self.assertEqual(status, jobs.TARGET_SUCCEEDED)

    def test_unconfirmed_delivery_is_still_a_success(self):
        # A still-queued post must not be failed: the platform may deliver it a
        # few seconds later, and the idempotency key prevents a duplicate.
        status, _ = self._run({
            "ok": True,
            "post_id": 2,
            "status": "scheduled",
            "network": "tw",
            "delivery_state": "pending",
            "delivered": False,
            "delivery_error": "",
            "requires_approval": False,
            "warnings": [],
        })
        self.assertEqual(status, jobs.TARGET_SUCCEEDED)
