"""Tests for permanent publish-failure operator alerts.

When a target exhausts its retry budget and ``mark_target_failed`` performs a
real transition, the worker must emit one actionable alert (job/target ids,
platform, account, profile, attempts, scrubbed error, deep link). Transient
retries and cancel-race no-ops must stay silent, and an alerting failure must
never change the target's outcome.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db.createTable as create_table
from myUtils import jobs
from myUtils import ops_alerts as ops_alerts_module
from myUtils import profiles as profile_registry
from myUtils.worker import (
    PublishWorker,
    RetryPolicy,
    WorkerConfig,
    _scrub_secrets,
    _logger,
)
from myUtils.sqlite_utils import connect
from tests.worker_publish_fixtures import WORKSPACE_ID, create_test_profile, native_config, record_native_proof


def _spec(targets, *, platform: str = "twitter", profile_id: int | None = None):
    return jobs.JobSpec(
        platform=platform,
        payload={"message": "Original approved text"},
        targets=targets,
        profile_id=profile_id,
        idempotency_key=f"key-{id(targets)}",
    )


class PublishFailureAlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "worker.db"
        create_table.bootstrap(self.db_path)
        profile = create_test_profile(self.db_path, "Alert Test")
        self.profile_id, self.workspace_id = profile.id, WORKSPACE_ID
        # Never let a real channel configured on the host leak into the tests.
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        # Neither origin variable may leak in: links fall back from
        # SAU_PUBLIC_APP_URL to SAU_PUBLIC_BASE_URL.
        os.environ.pop("SAU_PUBLIC_APP_URL", None)
        os.environ.pop("SAU_PUBLIC_BASE_URL", None)
        # myUtils.__init__ loads the repo .env, which may switch the X path to
        # the Sociamonials fallback and bypass the executor these tests drive.
        for key in (
            "SAU_SOCIAMONIALS_FALLBACK",
            "SOCIAMONIALS_API_KEY",
            "SOCIAMONIALS_SECRETS_FILE",
            "SAU_X_DIRECT_PUBLISH",
        ):
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _spec(self, targets, *, platform: str = "twitter"):
        # Alert fixtures must clear real native readiness, not bypass preflight.
        with connect(self.db_path) as conn:
            for reference, _file, _schedule in targets:
                account_id = int(reference.split(":", 1)[1])
                conn.execute("INSERT OR IGNORE INTO accounts "
                             "(id,profile_id,platform,account_name,cookie_path,auth_type,status,workspace_id,config_json) "
                             "VALUES (?,?,?,?,'','api',1,?,?)",
                             (account_id, self.profile_id, platform, f"fixture-{account_id}",
                              self.workspace_id, json.dumps(native_config(platform))))
        for reference, _file, _schedule in targets:
            account_id = int(reference.split(":", 1)[1])
            account = profile_registry.update_account(account_id, config_patch=native_config(platform), db_path=self.db_path)
            self.assertTrue(record_native_proof(self.db_path, account)["ready"])
        return _spec(targets, platform=platform, profile_id=self.profile_id)

    def _drain(self, executor, *, retry: RetryPolicy | None = None) -> None:
        config = WorkerConfig(
            poll_interval=0.001,
            batch_size=4,
            max_concurrent=1,
            retry=retry
            or RetryPolicy(
                max_attempts=2, base_backoff_seconds=0.001, max_backoff_seconds=0.01
            ),
        )
        worker = PublishWorker(executor, config=config, db_path=self.db_path)
        asyncio.run(worker.drain())

    # ------------------------------------------------------------------
    # Happy path: one alert per permanent failure, with actionable fields
    # ------------------------------------------------------------------

    def test_permanent_failure_alerts_once_with_actionable_fields(self) -> None:
        profile_registry.add_account(
            self.profile_id,
            platform="twitter",
            account_name="handle",
            nickname="Friendly Account",
            config={"twitterAuthType": "api"},
            db_path=self.db_path,
        )
        job = jobs.enqueue_job(self._spec([("account:1", "text-only", None)]), workspace_id=self.workspace_id, db_path=self.db_path)

        async def executor(platform, payload, target):
            raise RuntimeError("boom")

        sent: list[dict] = []
        with patch.object(
            ops_alerts_module,
            "send_ops_alert",
            side_effect=lambda **kwargs: sent.append(kwargs) or True,
        ):
            self._drain(executor)

        self.assertEqual(len(sent), 1, "exactly one alert for a permanent failure")
        subject = sent[0]["subject"]
        body = sent[0]["body"]

        target = jobs.list_targets(job.id, db_path=self.db_path)[0]
        self.assertEqual(target.status, jobs.TARGET_FAILED)
        self.assertEqual(target.attempts, 2)

        self.assertIn(f"job #{job.id}", subject)
        self.assertIn(f"target #{target.id}", subject)
        self.assertIn("twitter", subject)

        for expected in (
            f"Job: #{job.id} (twitter)",
            f"Target: #{target.id}",
            "Account: Friendly Account (account:1)",
            f"Profile: {self.profile_id}",
            "Attempts: 2/2",
            "Error: RuntimeError: boom",
            "#/jobs?job=" + str(job.id),
        ):
            self.assertIn(expected, body, expected)

    def test_public_app_url_prefixes_the_deep_link(self) -> None:
        with patch.dict(
            os.environ, {"SAU_PUBLIC_APP_URL": "https://socialupload.example.com/"}
        ):
            self.assertEqual(
                PublishWorker._publish_failure_deep_link(42),
                "https://socialupload.example.com/#/jobs?job=42",
            )

    def test_missing_public_app_url_falls_back_to_route_fragment(self) -> None:
        # No SAU_PUBLIC_APP_URL -> bare fragment, never a guessed domain.
        link = PublishWorker._publish_failure_deep_link(42)
        self.assertEqual(link, "#/jobs?job=42")
        self.assertNotIn("http", link)

    # ------------------------------------------------------------------
    # Only the permanent transition alerts
    # ------------------------------------------------------------------

    def test_non_retryable_failure_alerts_immediately_with_account_name(self) -> None:
        profile_registry.add_account(
            self.profile_id,
            platform="twitter",
            account_name="handle",
            nickname="Friendly Account",
            auth_type="oauth",
            config={"twitterAuthType": "api"},
            db_path=self.db_path,
        )
        job = jobs.enqueue_job(self._spec([("account:1", "text-only", None)]), workspace_id=self.workspace_id, db_path=self.db_path)
        calls = {"n": 0}

        async def executor(platform, payload, target):
            calls["n"] += 1
            from myUtils.prepared_publishers import PreparedPublishError
            raise PreparedPublishError("Reconnect required", retryable=False)

        sent: list[dict] = []
        with patch.object(ops_alerts_module, "send_ops_alert", side_effect=lambda **kwargs: sent.append(kwargs) or True):
            self._drain(executor)

        target = jobs.list_targets(job.id, db_path=self.db_path)[0]
        self.assertEqual(calls["n"], 1)
        self.assertEqual(target.status, jobs.TARGET_FAILED)
        self.assertEqual(target.attempts, 1)
        self.assertIn("Friendly Account (account:1)", sent[0]["body"])

    def test_transient_retry_does_not_alert(self) -> None:
        job = jobs.enqueue_job(self._spec([("account:5", "text-only", None)]), workspace_id=self.workspace_id, db_path=self.db_path)
        calls = {"n": 0}

        async def executor(platform, payload, target):
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("transient")

        sent: list[dict] = []
        with patch.object(
            ops_alerts_module,
            "send_ops_alert",
            side_effect=lambda **kwargs: sent.append(kwargs) or True,
        ):
            self._drain(
                executor,
                retry=RetryPolicy(
                    max_attempts=3, base_backoff_seconds=0.001, max_backoff_seconds=0.01
                ),
            )

        self.assertEqual(calls["n"], 2)
        target = jobs.list_targets(job.id, db_path=self.db_path)[0]
        self.assertEqual(target.status, jobs.TARGET_SUCCEEDED)
        self.assertEqual(target.checkpoint["publish_routes"]["native"]["attempts"], 2)
        self.assertEqual(target.checkpoint["publish_routes"]["native"]["max_attempts"], 3)
        self.assertEqual(sent, [], "a target that ultimately succeeds must not alert")

    def test_cancel_race_no_op_transition_does_not_alert(self) -> None:
        job = jobs.enqueue_job(self._spec([("account:5", "text-only", None)]),
                               workspace_id=self.workspace_id, db_path=self.db_path)
        target = jobs.claim_next_targets(db_path=self.db_path, owner="alert-fixture")[0]
        jobs.cancel_job(job.id, db_path=self.db_path)
        instance = PublishWorker(lambda *a: None,
            config=WorkerConfig(retry=RetryPolicy(max_attempts=1)), db_path=self.db_path)
        sent: list[dict] = []
        with patch.object(ops_alerts_module, "send_ops_alert", side_effect=lambda **kwargs: sent.append(kwargs) or True):
            asyncio.run(instance._handle_failure(target, RuntimeError("boom"), _logger))
        self.assertEqual(jobs.get_target(target.id, db_path=self.db_path).status, jobs.TARGET_CANCELLED)
        self.assertEqual(sent, [], "real cancellation CAS no-op must not alert")
        with connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM publish_alert_outbox").fetchone()[0], 0)

    def test_committed_stop_alert_survives_purged_job_context(self) -> None:
        job = jobs.enqueue_job(self._spec([("account:9", "text-only", None)]),
                               workspace_id=self.workspace_id, db_path=self.db_path)
        claimed = jobs.claim_next_targets(db_path=self.db_path, owner="alert-fixture")[0]
        self.assertTrue(jobs.mark_target_failed(claimed.id, "RuntimeError: boom", lease_token=claimed.lease_token,
                                               alert_max_attempts=1, db_path=self.db_path))
        target = jobs.get_target(claimed.id, db_path=self.db_path)
        # The atomic stop event must outlive context loss, without fabricated CAS.
        with connect(self.db_path) as conn:
            subject, body = conn.execute("SELECT subject,body FROM publish_alert_outbox").fetchone()
            conn.execute("DELETE FROM publish_job_targets WHERE job_id=?", (job.id,))
            conn.execute("DELETE FROM publish_jobs WHERE id=?", (job.id,))
        instance = PublishWorker(lambda *a: None,
            config=WorkerConfig(retry=RetryPolicy(max_attempts=1)), db_path=self.db_path)
        sent: list[dict] = []
        with patch.object(ops_alerts_module, "send_ops_alert", side_effect=lambda **kwargs: sent.append(kwargs) or True):
            instance._alert_publish_failure(target, "RuntimeError: boom")
        self.assertEqual(sent, [{"subject": subject, "body": body}])
        self.assertIn(f"job #{job.id}", subject)
        self.assertIn("Account: fixture-9 (account:9)", body)
        self.assertIn(f"Profile: {self.profile_id}", body)
        with connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT state FROM publish_alert_outbox").fetchone()[0], "sent")

    # ------------------------------------------------------------------
    # Best-effort contract: alert errors never change the outcome
    # ------------------------------------------------------------------

    def test_alert_error_does_not_change_worker_outcome(self) -> None:
        job = jobs.enqueue_job(self._spec([("account:5", "text-only", None)]), workspace_id=self.workspace_id, db_path=self.db_path)

        async def executor(platform, payload, target):
            raise RuntimeError("boom")

        with patch.object(
            ops_alerts_module,
            "send_ops_alert",
            side_effect=RuntimeError("alert transport down"),
        ):
            self._drain(executor)  # must not raise

        finalised = jobs.get_job(job.id, db_path=self.db_path)
        target = jobs.list_targets(job.id, db_path=self.db_path)[0]
        self.assertEqual(finalised.status, jobs.JOB_FAILED)
        self.assertEqual(target.status, jobs.TARGET_FAILED)
        self.assertEqual(target.checkpoint["publish_routes"]["native"]["attempts"], 2)
        self.assertEqual(target.checkpoint["publish_routes"]["native"]["max_attempts"], 2)
        with connect(self.db_path) as conn:
            event = conn.execute("SELECT state,attempts,next_attempt_at,last_error FROM publish_alert_outbox").fetchone()
        self.assertEqual(tuple(event[:2]), ("pending", 1), "notification retry must remain independent")
        self.assertIsNotNone(event[2])
        self.assertEqual(event[3], "sender_raised", "sender exception text must not enter the outbox")

    # ------------------------------------------------------------------
    # No secret material in the alert
    # ------------------------------------------------------------------

    def test_error_credentials_are_scrubbed_from_alert(self) -> None:
        job = jobs.enqueue_job(self._spec([("account:5", "text-only", None)]), workspace_id=self.workspace_id, db_path=self.db_path)

        async def executor(platform, payload, target):
            raise RuntimeError(
                "HTTP 401 for https://api.example.com/v1/upload"
                "?access_token=SEKRET123&x=1"
            )

        sent: list[dict] = []
        with patch.object(
            ops_alerts_module,
            "send_ops_alert",
            side_effect=lambda **kwargs: sent.append(kwargs) or True,
        ):
            self._drain(executor)

        self.assertEqual(len(sent), 1)
        body = sent[0]["body"]
        self.assertNotIn("SEKRET123", body)
        self.assertIn("[REDACTED_QUERY]", body)
        self.assertNotIn("access_token=", body)

    def test_scrub_secrets_masks_common_credential_shapes(self) -> None:
        cleaned = _scrub_secrets(
            "refresh_token=rt-1 Bearer eyJ.a.b client_secret: cs-9 password=hunter2"
        )
        for secret in ("rt-1", "eyJ.a.b", "cs-9", "hunter2"):
            self.assertNotIn(secret, cleaned)
        self.assertIn("<redacted>", cleaned)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class PrepLeaseConfigTests(unittest.TestCase):
    """Prep lease must be tunable and must not hang a crashed prep for hours.

    A container restart mid-prep left five campaigns in `preparing` for 32+
    minutes because the lease defaulted to 120 min - four times the ~30 min
    worst-case prep - so the stale sweep would not release them. 45 min is ~1.5x
    headroom. The env override exists so a slower host can raise it without a
    rebuild; an explicit argument must still win.
    """

    def test_default_lease_is_roughly_1_5x_the_worst_case_prep(self):
        from myUtils.worker import WorkerConfig

        self.assertEqual(WorkerConfig().prep_lease_minutes, 45)

    def test_env_override_is_honoured(self):
        from unittest.mock import patch

        from myUtils.worker import WorkerConfig

        with patch.dict(os.environ, {"SAU_PREP_LEASE_MINUTES": "20"}):
            self.assertEqual(WorkerConfig().prep_lease_minutes, 20)
        with patch.dict(os.environ, {"SAU_PREP_CONCURRENT": "2"}):
            pass  # unrelated key must not matter

    def test_explicit_argument_beats_the_env(self):
        from unittest.mock import patch

        from myUtils.worker import WorkerConfig

        with patch.dict(os.environ, {"SAU_PREP_LEASE_MINUTES": "20"}):
            self.assertEqual(
                WorkerConfig(prep_lease_minutes=99).prep_lease_minutes, 99
            )

    def test_invalid_env_value_falls_back_instead_of_raising(self):
        from unittest.mock import patch

        from myUtils.worker import WorkerConfig

        with patch.dict(os.environ, {"SAU_PREP_LEASE_MINUTES": "not-a-number"}):
            self.assertEqual(WorkerConfig().prep_lease_minutes, 45)

    def test_constructing_the_config_does_not_recurse(self):
        # A previous version built a default WorkerConfig inside __post_init__,
        # which recursed until RecursionError.
        from myUtils.worker import WorkerConfig

        WorkerConfig()
