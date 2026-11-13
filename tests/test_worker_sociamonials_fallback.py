"""Worker fallback wiring through real admission, preparation and queue logic.

Only provider HTTP reads/writes and codec probes are replaced. Acceptance stays
pending until exact-destination delivery is confirmed; approval/uncertain creates
retain their evidence for review and never permit another submission.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import db.createTable as create_table
from myUtils import jobs, media_compliance, outbound_http, publish_readiness
from myUtils import profiles as profile_registry
from myUtils import sociamonials_fallback, sociamonials_reclaim, worker
from myUtils import campaigns as campaign_store
from myUtils.sqlite_utils import connect
from myUtils.worker import PublishWorker, RetryPolicy, WorkerConfig
from tests.test_sociamonials_fallback import ContractSession, Response, asset_for, profile as managed_profile
from tests.worker_publish_fixtures import WORKSPACE_ID, campaign_media_payload, create_test_profile, native_config, record_native_proof, video_probe


class _BoomPermanent(RuntimeError):
    retryable = False


class _ReceiptSession(ContractSession):
    """Exact HTTP receipt fixtures, not a replacement for the real publisher."""
    def __init__(self):
        super().__init__(profiles=[managed_profile(network=network, ref="worker-fixture") for network in ("blsk", "tw")],
                         override=self._receipt_response)
        self.receipt_id = 999
        self.receipt_state = "delivered"
        self.requires_approval = False
        self.create_failure = None

    def _receipt_response(self, method, url, kwargs):
        path = urlparse(url).path
        if method == "POST" and path == "/api/v1/posts" and not kwargs["json"].get("validate_only"):
            if self.create_failure is not None:
                if isinstance(self.create_failure, BaseException):
                    raise self.create_failure
                return self.create_failure
            body = kwargs["json"]
            self.last_body = copy.deepcopy(body)
            result = {"status": "scheduled", "requires_approval": self.requires_approval,
                      "message": body["message"], "profile_resolution": self._resolution(body), "warnings": []}
            if self.receipt_id is not None:
                result["post_id"] = self.receipt_id
                self.created.setdefault(body["idempotency_key"], self.receipt_id)
                self.published[self.receipt_id] = self.last_body
            return Response(result)
        if method == "GET" and path.startswith("/api/v1/posts/"):
            receipt_id = int(path.rsplit("/", 1)[1])
            body = self.published[receipt_id]
            network, detail = next(iter(body["networks"].items()))
            state = self.receipt_state
            profile_state = {"delivered": "published", "failed": "needs_attention", "pending": "accepted",
                             "requires_approval": "accepted"}[state]
            destination = {"profile_ref": detail["profile_refs"][0], "delivery_state": profile_state,
                           "platform_post_id": f"native-post-{receipt_id}" if state == "delivered" else None}
            if state == "requires_approval":
                destination["reason"] = "APPROVAL_REQUIRED"
            return Response({"post_id": receipt_id, "workspace_registration_id": body["workspace_registration_id"],
                "approved": not self.requires_approval,
                "networks": {network: {"delivered": state == "delivered",
                    "delivery_status": "pending" if state == "requires_approval" else state,
                    "profiles": [destination]}}})


class _WorkerFallbackCase(unittest.TestCase):
    platform = "bluesky"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "worker.db"
        create_table.bootstrap(self.db_path)
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        for key in ("SAU_PUBLIC_APP_URL", "SAU_PUBLIC_BASE_URL", "SAU_SOCIAMONIALS_FALLBACK",
                    "SOCIAMONIALS_API_KEY", "SOCIAMONIALS_SECRETS_FILE", "SAU_X_DIRECT_PUBLISH"):
            os.environ.pop(key, None)
        os.environ["SAU_SOCIAMONIALS_DELIVERY_TIMEOUT"] = "0"
        os.environ["SAU_SOCIAMONIALS_REUSE_ASSETS"] = "1"
        profile = create_test_profile(self.db_path, "Fallback Test")
        self.profile_id, self.workspace_id = profile.id, WORKSPACE_ID
        self.account_id = self._add_account(self.platform).id
        self.session = _ReceiptSession()
        transport = patch.object(sociamonials_fallback, "_get_session", side_effect=lambda supplied=None: supplied or self.session)
        transport.start()
        self.addCleanup(transport.stop)
        rates = patch.object(sociamonials_fallback, "_RATE_WINDOWS", {})
        rates.start()
        self.addCleanup(rates.stop)
        codec = patch.object(media_compliance.media_prep, "probe", side_effect=video_probe)
        codec.start()
        self.addCleanup(codec.stop)
        duration = patch.object(sociamonials_fallback.media_pipeline, "probe_video_duration",
                                side_effect=lambda path: video_probe(path)["duration"])
        duration.start()
        self.addCleanup(duration.stop)

    def _add_account(self, platform):
        network = {"bluesky": "blsk", "twitter": "tw"}[platform]
        account = profile_registry.add_account(self.profile_id, platform=platform, account_name=f"fixture-{platform}",
            auth_type="api", config={**native_config(platform), "sociamonials": {
                "network": network, "profile_refs": ["worker-fixture"], "workspace_id": "26985"}}, db_path=self.db_path)
        self.assertTrue(record_native_proof(self.db_path, account)["ready"])
        return account

    def _enable_fallback(self) -> None:
        os.environ["SAU_SOCIAMONIALS_FALLBACK"] = "1"
        os.environ["SOCIAMONIALS_API_KEY"] = "fixture-only"

    def _enqueue_target(self, *, account_id=None, payload=None, key="fallback"):
        account_id = account_id or self.account_id
        account = profile_registry.get_account(account_id, db_path=self.db_path)
        payload = payload or {"draft": {"message": "hi"}}
        file_ref = f"campaign_post:{payload['campaignPostId']}" if payload.get("campaignPostId") else "text-only"
        job = jobs.enqueue_job(jobs.JobSpec(platform=account.platform, payload=payload,
            targets=[(f"account:{account_id}", file_ref, None)], profile_id=self.profile_id, idempotency_key=key),
            workspace_id=self.workspace_id, db_path=self.db_path)
        self.assertTrue(job.payload["publishReadiness"]["ready"])
        self._job_id = job.id
        return job

    def _enqueue_failing_target(self) -> int:
        return self._enqueue_target().id

    def _drain(self, exc_type=RuntimeError, *, budget=2) -> None:
        self.native_calls = 0

        async def executor(platform, payload, target):
            self.native_calls += 1
            raise exc_type("native provider said no")

        config = WorkerConfig(poll_interval=0.001, batch_size=4, max_concurrent=1,
            retry=RetryPolicy(max_attempts=budget, base_backoff_seconds=0.001, max_backoff_seconds=0.01))
        asyncio.run(PublishWorker(executor, config=config, db_path=self.db_path).drain())

    def _target(self):
        targets = jobs.list_targets(self._job_id, db_path=self.db_path)
        self.assertEqual(len(targets), 1)
        return targets[0]

    def _status(self) -> str:
        return self._target().status

    def _creates(self):
        return self.session.calls_for("POST", "/posts", create=True)


class SociamonialsFallbackHookTests(_WorkerFallbackCase):
    def test_missing_local_media_is_retryable_not_a_dead_url(self) -> None:
        # Admit real registered bytes first; loss/restore failure happens later.
        self._enable_fallback()
        account = self._add_account("twitter")
        media = Path(self._tmp.name) / "clip.mp4"
        media.write_bytes(b"exact-approved-video" * 100)
        payload = campaign_media_payload(self.db_path, account, media, {"message": "hi"},
                                         public_url="https://cdn.example/clip.mp4")
        job = self._enqueue_target(account_id=account.id, payload=payload, key="missing-media")
        target = jobs.claim_next_targets(db_path=self.db_path, owner="restore-fixture")[0]
        media.unlink()
        instance = PublishWorker(lambda *a: None, db_path=self.db_path)
        with patch.object(outbound_http, "probe_public_url", return_value={"size_bytes": payload["artifacts"][0]["size_bytes"]}), patch.object(
            outbound_http, "download_public_file", side_effect=TimeoutError("fixture restore unavailable")
        ) as download:
            with self.assertRaises(worker.MediaRestoreError) as raised:
                asyncio.run(instance._try_sociamonials_fallback(target, "X direct publish disabled", worker._logger))
        self.assertTrue(raised.exception.retryable)
        download.assert_called_once()
        self.assertEqual(download.call_args.args[0], payload["artifacts"][0]["public_url"])
        self.assertEqual(self._creates(), [])
        self.assertEqual(self.session.calls, [])
        retained = jobs.get_target(target.id, db_path=self.db_path).checkpoint
        self.assertEqual(retained["publish_routes"]["active"], "sociamonials")
        self.assertEqual(retained["publish_routes"].get("sociamonials", {}).get("attempts", 0), 0)
        self.assertEqual(jobs.get_job(job.id, db_path=self.db_path).payload["artifacts"], payload["artifacts"])

    def test_flag_off_falls_through_to_permanent_failure(self) -> None:
        self._enqueue_failing_target()
        self._drain()
        self.assertEqual(self._creates(), [])
        self.assertEqual(self._status(), jobs.TARGET_FAILED)
        self.assertEqual(self.native_calls, 2)

    def test_non_retryable_failure_does_not_attempt_the_fallback(self) -> None:
        self._enable_fallback()
        self._enqueue_failing_target()
        self._drain(exc_type=_BoomPermanent)
        self.assertEqual(self._creates(), [])
        self.assertEqual(self._status(), jobs.TARGET_FAILED)
        self.assertEqual(self.native_calls, 1)

    def test_permanent_direct_x_failure_is_not_rerouted(self) -> None:
        os.environ["SAU_X_DIRECT_PUBLISH"] = "1"
        self._enable_fallback()
        account = self._add_account("twitter")
        self._enqueue_target(account_id=account.id, key="x-permanent")
        self._drain(exc_type=_BoomPermanent)
        self.assertEqual(self._creates(), [])
        self.assertEqual(self._status(), jobs.TARGET_FAILED)
        self.assertEqual(self._target().attempts, 1)
        self.assertEqual(self.native_calls, 1)

    def test_x_target_goes_straight_to_the_fallback_when_direct_is_disabled(self) -> None:
        self._enable_fallback()
        os.environ["SAU_X_DIRECT_PUBLISH"] = "0"  # Explicit selection, never inferred from availability.
        self.session.receipt_id, self.session.receipt_state = 9, "pending"
        account = self._add_account("twitter")
        self._enqueue_target(account_id=account.id, key="x-skip")
        self._drain()
        self.assertEqual(self.native_calls, 0)
        self.assertEqual(len(self._creates()), 1)
        self.assertEqual(self._status(), jobs.TARGET_AWAITING_DELIVERY)
        self.assertEqual(self._target().checkpoint["provider_receipt"]["id"], "9")

    def test_direct_x_runs_when_explicitly_enabled(self) -> None:
        self._enable_fallback()
        os.environ["SAU_X_DIRECT_PUBLISH"] = "1"
        account = self._add_account("twitter")
        self._enqueue_target(account_id=account.id, key="x-direct")
        self._drain(exc_type=_BoomPermanent, budget=1)
        self.assertEqual(self.native_calls, 1)
        self.assertEqual(self._creates(), [])

    def test_placeholder_copy_is_refused_before_any_publish(self) -> None:
        self._enqueue_target(payload={"draft": {"message": "✨ publish-center-20260921-015359"}}, key="placeholder-guard")
        self._drain(budget=1)
        self.assertEqual(self.native_calls, 0)
        self.assertEqual(self._creates(), [])
        self.assertEqual(self._status(), jobs.TARGET_FAILED)
        self.assertIn("[content-guard]", self._target().last_error or "")

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
        self.session.receipt_id = 4242
        self._enqueue_failing_target()
        self._drain()
        self.assertEqual(len(self._creates()), 1)
        self.assertEqual(self._status(), jobs.TARGET_SUCCEEDED)
        self.assertEqual(self.native_calls, 2)
        checkpoint = self._target().checkpoint
        self.assertEqual(checkpoint["publish_routes"]["native"]["attempts"], 2)
        self.assertEqual(checkpoint["publish_routes"]["sociamonials"]["attempts"], 1)
        self.assertEqual(checkpoint["provider_receipt"]["profile_refs"], ["worker-fixture"])

    def test_live_backup_admission_does_not_clear_the_primary_native_hold(self) -> None:
        self._enable_fallback()
        os.environ["SAU_X_DIRECT_PUBLISH"] = "1"
        account = self._add_account("twitter")
        self._enqueue_target(account_id=account.id, key="native-hold")
        held = profile_registry.update_account(account.id, config_patch={"_nativeCredentialError": {
            "kind": "unknown", "code": "invalid_request", "httpStatus": 400}}, db_path=self.db_path)
        original_config = copy.deepcopy(held.config)
        settings = profile_registry.get_profile(self.profile_id, db_path=self.db_path).settings
        self.assertEqual(publish_readiness.destination_readiness(held, settings, db_path=self.db_path)["reasonCode"], "native_hold")
        self._drain()
        self.assertEqual(self.native_calls, 0)
        self.assertEqual(self._status(), jobs.TARGET_SUCCEEDED)
        self.assertEqual(len(self._creates()), 1)
        current = profile_registry.get_account(account.id, db_path=self.db_path)
        self.assertEqual(current.config, original_config)
        self.assertEqual(publish_readiness.destination_readiness(current, settings, db_path=self.db_path)["reasonCode"], "native_hold")
        with connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT provider FROM publish_destination_readiness WHERE account_id=?", (account.id,)).fetchone()[0], "direct")
        self.assertEqual(self._target().checkpoint["publish_routes"]["native"]["outcome"], "credential_unavailable")

    def test_fallback_api_error_falls_through_to_permanent_failure(self) -> None:
        self._enable_fallback()
        self.session.create_failure = Response({"error": {"code": "invalid_request"}}, status=400)
        self._enqueue_failing_target()
        self._drain()
        self.assertEqual(self._status(), jobs.TARGET_FAILED)
        self.assertEqual(self.native_calls, 2)
        self.assertEqual(len(self._creates()), 1)
        self.assertEqual(self._target().checkpoint["definitive_non_delivery"]["provider"], "sociamonials")

    def test_unconfigured_fallback_has_a_finite_prerequisite_budget_without_create(self) -> None:
        self._enable_fallback()
        account = profile_registry.get_account(self.account_id, db_path=self.db_path)
        config = dict(account.config)
        config.pop("sociamonials")
        account = profile_registry.update_account(account.id, config=config, db_path=self.db_path)
        self.assertTrue(record_native_proof(self.db_path, account)["ready"])
        self._enqueue_failing_target()
        self._drain()
        self.assertEqual(self.native_calls, 2)
        maximum = jobs.PREREQUISITE_MAX_ATTEMPTS
        for attempt in range(1, maximum + 1):
            row = self._target()
            self.assertEqual(row.checkpoint["prerequisites"]["attempts"], attempt)
            self.assertEqual(row.checkpoint["prerequisites"]["max_attempts"], maximum)
            self.assertEqual(row.checkpoint["publish_routes"]["native"]["attempts"], 2)
            self.assertEqual(row.checkpoint["publish_routes"]["active"], "sociamonials")
            self.assertEqual(row.checkpoint["publish_routes"].get("sociamonials", {}).get("attempts", 0), 0)
            self.assertEqual(self.session.calls, [])
            if attempt == maximum:
                self.assertEqual(row.status, jobs.TARGET_BLOCKED)
                self.assertTrue(row.checkpoint["prerequisites"]["exhausted"])
                break
            self.assertEqual(row.status, jobs.TARGET_RETRYING)
            self.assertIsNotNone(row.next_attempt_at)
            # Expire only the fixture's persisted delay; leave every counter/fence intact.
            with connect(self.db_path) as conn:
                conn.execute("UPDATE publish_job_targets SET next_attempt_at='2000-01-01T00:00:00' WHERE id=?", (row.id,))
            self._drain()
            self.assertEqual(self.native_calls, 0)

    def test_unexpected_fallback_exception_is_contained_without_resubmission(self) -> None:
        self._enable_fallback()
        self.session.create_failure = RuntimeError("kaboom")
        self._enqueue_failing_target()
        self._drain()
        self.assertEqual(self._status(), jobs.TARGET_NEEDS_REVIEW)
        self.assertEqual(self.native_calls, 2)
        self.assertEqual(len(self._creates()), 1)
        checkpoint = self._target().checkpoint
        self.assertTrue(checkpoint["submission_started"])
        self.assertEqual(checkpoint["operation"], "sociamonials-create-post")
        self.assertRegex(checkpoint["operation_generation"], r"^[0-9a-f]{32}$")
        self._drain()
        self.assertEqual(self.native_calls, 0)
        self.assertEqual(len(self._creates()), 1)
        self.assertEqual(self._target().checkpoint, checkpoint)

    def test_retryable_backup_rejections_exhaust_only_its_original_budget(self) -> None:
        self._enable_fallback()
        self.session.create_failure = Response({"error": {"code": "rate_limit"}}, status=429)
        self._enqueue_failing_target()
        self._drain()
        self.assertEqual(self._status(), jobs.TARGET_FAILED)
        self.assertEqual(self.native_calls, 2)
        self.assertEqual(len(self._creates()), 2)
        routes = self._target().checkpoint["publish_routes"]
        for route in ("native", "sociamonials"):
            self.assertEqual(routes[route]["attempts"], 2)
            self.assertEqual(routes[route]["max_attempts"], 2)
        self.assertEqual(self._creates()[0][2]["json"], self._creates()[1][2]["json"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class SociamonialsFallbackMissingAccountTests(_WorkerFallbackCase):
    def test_missing_account_is_blocked_without_fallback(self) -> None:
        self._enable_fallback()
        job = self._enqueue_target(key="deleted-account")
        # Loss of ownership occurs AFTER valid admission, not in a bogus new spec.
        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM publish_destination_readiness WHERE account_id=?", (self.account_id,))
            conn.execute("DELETE FROM accounts WHERE id=?", (self.account_id,))
        self._drain(budget=1)
        self.assertEqual(jobs.list_targets(job.id, db_path=self.db_path)[0].status, jobs.TARGET_BLOCKED)
        self.assertEqual(self.native_calls, 0)
        self.assertEqual(self.session.calls, [])


class SociamonialsDeliveryVerificationTests(_WorkerFallbackCase):
    platform = "twitter"

    def _run(self, result: dict) -> tuple[str, str]:
        self._enable_fallback()
        os.environ["SAU_X_DIRECT_PUBLISH"] = "1"
        self.session.receipt_id = result.get("post_id")
        self.session.receipt_state = result.get("delivery_state", "pending")
        self.session.requires_approval = bool(result.get("requires_approval"))
        payload = {"draft": {"message": "hi"}}
        if result.get("used_assets"):
            # Real bytes and exact registered asset facts, not a invented ledger receipt.
            expected_asset = result["used_assets"][0]
            media = Path(self._tmp.name) / expected_asset["filename"]
            media.write_bytes(b"v" * expected_asset["size_bytes"])
            account = profile_registry.get_account(self.account_id, db_path=self.db_path)
            payload = campaign_media_payload(self.db_path, account, media, payload["draft"])
            asset = asset_for(media, asset_id=expected_asset["asset_id"], ws=result["workspace_id"], kind="video")
            self.session.assets[asset["asset_id"]] = asset
        job = self._enqueue_target(payload=payload, key="delivery")
        native_calls = []

        async def executor(platform, working, target):
            native_calls.append(target.id)
            raise RuntimeError("X media upload failed (HTTP 402)")

        config = WorkerConfig(poll_interval=0.001, batch_size=4, max_concurrent=1,
            retry=RetryPolicy(max_attempts=1, base_backoff_seconds=0.001, max_backoff_seconds=0.01))
        instance = PublishWorker(executor, config=config, db_path=self.db_path)
        asyncio.run(instance.drain())
        target = jobs.list_targets(job.id, db_path=self.db_path)[0]
        self._last_target = target
        self.assertEqual(len(native_calls), 1)
        self.assertEqual(len(self._creates()), 1)
        for route in ("native", "sociamonials"):
            self.assertEqual(target.checkpoint["publish_routes"][route]["attempts"], 1)
            self.assertEqual(target.checkpoint["publish_routes"][route]["max_attempts"], 1)
        if self.session.receipt_id is not None:
            receipt = target.checkpoint["provider_receipt"]
            self.assertEqual(receipt["id"], str(self.session.receipt_id))
            self.assertEqual(receipt["network"], "tw")
            self.assertEqual(receipt["profile_refs"], ["worker-fixture"])
            self.assertEqual(str(receipt["workspace_id"]), "26985")
        self.assertEqual(profile_registry.get_account(self.account_id, db_path=self.db_path).config.get("publishingProvider", "direct"), "direct")
        # A second drain cannot erase evidence or turn receipt handling into create.
        retained = copy.deepcopy(target.checkpoint)
        asyncio.run(instance.drain())
        self.assertEqual(len(self._creates()), 1)
        self.assertEqual(len(native_calls), 1)
        self.assertEqual(jobs.get_target(target.id, db_path=self.db_path).checkpoint, retained)
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
        self.assertIn("failure", error)
        self.assertEqual(self._last_target.checkpoint["provider_receipt"]["status"], "failed")

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

    def test_delivered_flag_without_a_receipt_requires_review(self):
        status, _ = self._run({"ok": True, "delivered": True, "delivery_state": "delivered"})
        self.assertEqual(status, jobs.TARGET_NEEDS_REVIEW)

    def test_unconfirmed_delivery_stays_pending(self):
        # Retain the receipt for another poll; do not report delivery or repost.
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
        self.assertEqual(status, jobs.TARGET_AWAITING_DELIVERY)
        self.assertEqual(self._last_target.checkpoint["provider_receipt"]["id"], "2")

    def test_delivered_assets_are_recorded_for_reclaim(self):
        status, _ = self._run({
            "ok": True,
            "post_id": 7,
            "status": "delivered",
            "network": "tw",
            "workspace_id": 26985,
            "used_assets": [
                {"asset_id": 9001, "filename": "clip.mp4", "size_bytes": 1234, "media_type": "video"}
            ],
            "delivery_state": "delivered",
            "delivered": True,
            "delivery_error": "",
            "requires_approval": False,
            "warnings": [],
        })
        self.assertEqual(status, jobs.TARGET_SUCCEEDED)
        ledger = sociamonials_reclaim.load_delivered_assets(self.db_path)
        self.assertIn(9001, ledger)

    def test_pending_assets_do_not_enter_the_delivered_ledger(self):
        status, _ = self._run({
            "ok": True, "post_id": 8, "status": "scheduled", "network": "tw",
            "workspace_id": 26985, "delivery_state": "pending", "delivered": False,
            "used_assets": [{"asset_id": 9002, "filename": "pending.mp4", "size_bytes": 123}],
        })
        self.assertEqual(status, jobs.TARGET_AWAITING_DELIVERY)
        self.assertNotIn(9002, sociamonials_reclaim.load_delivered_assets(self.db_path))

    def test_a_post_held_for_approval_is_not_recorded_as_delivered(self):
        # Sociamonials can hold a post until a human releases it. It will NOT
        # deliver on its own, so reporting success tells the operator a post is
        # live when nobody can see it - the "200 but nothing landed" failure this
        # fallback exists to prevent. A pending receipt instead remains pollable.
        status, error = self._run({
            "ok": True,
            "post_id": 2,
            "status": "scheduled",
            "network": "tw",
            "delivery_state": "pending",
            "delivered": False,
            "delivery_error": "",
            "requires_approval": True,
            "warnings": [],
        })
        self.assertEqual(status, jobs.TARGET_NEEDS_REVIEW)
        self.assertIn("approval", error)

    def test_an_explicit_approval_delivery_state_is_not_delivered(self):
        status, error = self._run({
            "ok": True,
            "post_id": 3,
            "status": "scheduled",
            "network": "tw",
            "delivery_state": "requires_approval",
            "delivered": False,
            "delivery_error": "",
            "requires_approval": False,
            "warnings": [],
        })
        self.assertEqual(status, jobs.TARGET_NEEDS_REVIEW)
        self.assertIn("approval", error)


class ArtifactUrlNormalisationTests(unittest.TestCase):
    """Stored artifact URLs must be valid URIs before use.

    Artifact URLs are built from media filenames and were persisted unencoded
    before the upload-side fix, so rows still carry raw spaces. A raw space is
    not a valid URI: the R2 object answers 200 for the encoded form and fails
    for the raw one, and URL-fetching platforms plus the Sociamonials fallback
    reject it. Normalisation happens at the point of use, so old and new rows
    behave identically.
    """

    def test_raw_space_is_percent_encoded(self):
        from myUtils.worker import _normalise_artifact_url

        url = _normalise_artifact_url(
            "https://cdn.example/campaigns/2210/videos/SFW 20260821094430455_pub_pub.mp4"
        )
        self.assertNotIn(" ", url)
        self.assertIn("SFW%2020260821094430455_pub_pub.mp4", url)

    def test_already_encoded_url_is_unchanged(self):
        from myUtils.worker import _normalise_artifact_url

        url = "https://cdn.example/campaigns/1/videos/already%20encoded.mp4"
        self.assertEqual(_normalise_artifact_url(url), url)

    def test_non_ascii_is_encoded(self):
        from myUtils.worker import _normalise_artifact_url

        url = _normalise_artifact_url(
            "https://cdn.example/campaigns/1/videos/\u4e2d\u6587 \u6a94\u540d.mp4"
        )
        self.assertNotIn(" ", url)
        self.assertIn("%E4%B8%AD%E6%96%87", url)

    def test_plain_and_empty_values_pass_through(self):
        from myUtils.worker import _normalise_artifact_url

        self.assertEqual(_normalise_artifact_url("not-a-url"), "not-a-url")
        self.assertEqual(_normalise_artifact_url(""), "")
        self.assertEqual(_normalise_artifact_url(None), "")

    def test_fallback_media_paths_normalises_the_public_url(self):
        # The fallback hands this value to Sociamonials, which fetches it, so it
        # must already be a valid URI.
        from myUtils.worker import _fallback_media_paths

        paths = _fallback_media_paths({
            "artifacts": [{
                "artifact_kind": "remote_upload",
                "public_url": "https://cdn.example/videos/SFW clip_pub.mp4",
            }],
        })
        self.assertEqual(len(paths), 1)
        self.assertNotIn(" ", paths[0])


class ArtifactPublicUrlBackfillTests(unittest.TestCase):
    """Refresh an exact registered variant, never borrow its source's URL."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "backfill.db"
        create_table.bootstrap(self.db_path)
        self.profile_id = create_test_profile(self.db_path, "Backfill").id
        self.media = Path(self._tmp.name) / "x.mp4"
        self.media.write_bytes(b"measured-watermarked-variant" * 100)
        self.metadata = {"role": "video", "sha256": hashlib.sha256(self.media.read_bytes()).hexdigest(),
                         "size_bytes": self.media.stat().st_size}
        with connect(self.db_path) as conn:
            conn.execute("INSERT INTO media_groups (id,name,status,workspace_id) VALUES (1,'g','ready',?)", (WORKSPACE_ID,))
            conn.execute("INSERT INTO file_records (id,filename,file_path,filesize,sha256,size_bytes,workspace_id) "
                         "VALUES (1085,?,?,?,?,?,?)", (self.media.name, str(self.media), self.media.stat().st_size,
                            self.metadata["sha256"], self.metadata["size_bytes"], WORKSPACE_ID))

    def _campaign_with(self, kinds):
        campaign = campaign_store.create_campaign(profile_id=self.profile_id, media_group_id=1,
                                                   workspace_id=WORKSPACE_ID, db_path=self.db_path)
        self.artifacts = [campaign_store.add_campaign_artifact(campaign.id, source_file_record_id=1085,
            artifact_kind=kind, local_path=str(self.media), public_url=url, metadata=self.metadata, db_path=self.db_path)
            for kind, url in kinds]
        return campaign

    def _artifact(self, *, public_url=None):
        registered = self.artifacts[0]
        return {"id": registered.id, "campaign_id": registered.campaign_id, "artifact_kind": registered.artifact_kind,
                "source_file_record_id": registered.source_file_record_id, "local_path": registered.local_path,
                "public_url": public_url, "sha256": self.metadata["sha256"], "size_bytes": self.metadata["size_bytes"],
                "metadata": dict(registered.metadata)}

    def test_missing_url_is_recovered_from_the_exact_registered_variant(self):
        self._campaign_with([("watermarked_video", None)])
        artifact = self._artifact()
        # Hosting finished after the job snapshot; the SAME measured variant got a URL.
        with connect(self.db_path) as conn:
            conn.execute("UPDATE campaign_artifacts SET public_url=? WHERE id=?",
                         ("https://cdn.example/w.mp4", artifact["id"]))
        worker._backfill_artifact_public_urls([artifact], self.db_path)
        self.assertEqual(artifact["public_url"], "https://cdn.example/w.mp4")

    def test_existing_url_is_not_overwritten(self):
        self._campaign_with([("watermarked_video", "https://cdn.example/keep.mp4"),
                             ("remote_upload", "https://cdn.example/other.mp4")])
        artifact = self._artifact(public_url="https://cdn.example/keep.mp4")
        worker._backfill_artifact_public_urls([artifact], self.db_path)
        self.assertEqual(artifact["public_url"], "https://cdn.example/keep.mp4")

    def test_exact_kind_path_and_checksum_are_required(self):
        self._campaign_with([("watermarked_video", "https://cdn.example/watermarked.mp4"),
                             ("remote_upload", "https://cdn.example/raw.mp4")])
        artifact = self._artifact()
        worker._backfill_artifact_public_urls([artifact], self.db_path)
        self.assertEqual(artifact["public_url"], "https://cdn.example/watermarked.mp4")

    def test_different_kind_same_source_is_never_adopted(self):
        self._campaign_with([("watermarked_video", None), ("remote_upload", "https://cdn.example/raw.mp4")])
        artifact = self._artifact()
        worker._backfill_artifact_public_urls([artifact], self.db_path)
        self.assertIsNone(artifact["public_url"])

    def test_same_basename_different_bytes_is_never_adopted(self):
        self._campaign_with([("watermarked_video", "https://cdn.example/w.mp4")])
        variant = Path(self._tmp.name) / "other-variant" / self.media.name
        variant.parent.mkdir()
        variant.write_bytes(b"different-measured-variant")
        artifact = self._artifact()
        artifact.update(local_path=str(variant), sha256=hashlib.sha256(variant.read_bytes()).hexdigest(), size_bytes=variant.stat().st_size)
        artifact["metadata"].update(sha256=artifact["sha256"], size_bytes=artifact["size_bytes"])
        worker._backfill_artifact_public_urls([artifact], self.db_path)
        self.assertIsNone(artifact["public_url"])

    def test_conflicting_exact_variant_urls_are_not_guessed(self):
        self._campaign_with([("watermarked_video", "https://cdn.example/one.mp4"),
                             ("watermarked_video", "https://cdn.example/two.mp4")])
        artifact = self._artifact()
        worker._backfill_artifact_public_urls([artifact], self.db_path)
        self.assertIsNone(artifact["public_url"])

    def test_no_campaign_id_is_a_safe_noop(self):
        self._campaign_with([("watermarked_video", "https://cdn.example/w.mp4")])
        artifact = self._artifact()
        artifact.pop("campaign_id")
        worker._backfill_artifact_public_urls([artifact], self.db_path)
        self.assertIsNone(artifact["public_url"])

    def test_unknown_campaign_is_a_safe_noop(self):
        self._campaign_with([("watermarked_video", "https://cdn.example/w.mp4")])
        artifact = self._artifact()
        artifact["campaign_id"] = 999999
        worker._backfill_artifact_public_urls([artifact], self.db_path)
        self.assertIsNone(artifact["public_url"])

    def test_bad_db_path_never_raises(self):
        self._campaign_with([("watermarked_video", "https://cdn.example/w.mp4")])
        artifact = self._artifact()
        absent = Path(self._tmp.name) / "nonexistent" / "db.sqlite"
        worker._backfill_artifact_public_urls([artifact], absent)
        self.assertIsNone(artifact["public_url"])
        self.assertFalse(absent.exists())


class FallbackNormalisesArtifactUrlsTests(unittest.TestCase):
    """Raw spaces in a stored URL must be encoded before the fallback sees them.

    Artifact URLs are built from filenames, so older rows carry raw spaces. A raw
    space is not a valid URI: the R2 object serves 206 for the encoded form and
    fails for the raw one, and Sociamonials reports "Video URL not accessible
    after retries" for a URL that is actually fine. _fallback_media_paths()
    normalises its own output, but collect_media() also reads
    payload["artifacts"][*]["public_url"] directly, so the payload itself must be
    normalised.
    """

    def test_the_payload_artifacts_are_normalised_before_the_fallback(self):
        import inspect

        source = inspect.getsource(PublishWorker._try_sociamonials_fallback)
        marker = source.index("_fallback_media_paths(payload)")
        preceding = source[:marker]
        self.assertIn("_normalise_artifact_url", preceding)
        self.assertRegex(preceding, r'\b\w+\["public_url"\]\s*=\s*_normalise_artifact_url\(')
