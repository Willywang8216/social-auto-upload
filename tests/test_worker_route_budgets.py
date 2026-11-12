"""Provider budgets stay finite, durable and independent from notification retry."""
from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from unittest.mock import Mock

import pytest

from db import createTable
from myUtils import delivery, failure_classification as fc, jobs, profiles, sociamonials_fallback as sm
from myUtils import publish_alert_outbox as outbox, worker as worker_module
from myUtils.prepared_publishers import PreparedPublishError
from myUtils.worker import PublishWorker, WorkerConfig, RetryPolicy


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    path = tmp_path / "route.db"
    createTable.bootstrap(path)
    profile = profiles.create_profile("Fixture", db_path=path)
    account = profiles.add_account(profile.id, platform="bluesky", account_name="Fixture",
             auth_type="api", config={"sociamonials": {"network": "blsk", "profile_refs": ["fixture"]}}, db_path=path)
    job = jobs.enqueue_job(jobs.JobSpec(platform="bluesky", profile_id=profile.id,
            payload={"message": "Original approved caption"},
            targets=[(f"account:{account.id}", "campaign_post:1", None)]), db_path=path)
    monkeypatch.setenv("SAU_SOCIAMONIALS_FALLBACK", "1")
    monkeypatch.setenv("SOCIAMONIALS_API_KEY", "fixture-only")
    monkeypatch.delenv("SAU_BLUESKY_ENABLED", raising=False)
    monkeypatch.setattr(worker_module, "_ensure_artifact_paths_local", lambda *a, **k: None)
    return path, account, job


def run(path, executor, budget=2):
    config = WorkerConfig(poll_interval=.001, max_concurrent=1,
             retry=RetryPolicy(max_attempts=budget, base_backoff_seconds=.001, max_backoff_seconds=.001))
    instance = PublishWorker(executor, config=config, db_path=path)
    asyncio.run(instance.drain())
    return instance


def target(fixture):
    path, _, job = fixture
    return jobs.list_targets(job.id, db_path=path)[0]


def delivered():
    return {"post_id": "soci-receipt", "delivered": True, "delivery_state": "delivered",
            "network": "blsk", "profile_refs": ["fixture"], "workspace_id": "26985"}


def test_native_and_backup_each_have_a_finite_budget(fixture, monkeypatch):
    path, _, _ = fixture
    native = Mock(side_effect=RuntimeError("Native provider unavailable"))
    backup = Mock(side_effect=RuntimeError("Backup provider unavailable"))
    monkeypatch.setattr(sm, "publish_via_sociamonials", backup)
    run(path, native)
    row = target(fixture)
    assert row.status == "failed"
    assert native.call_count == 2 and backup.call_count == 2
    assert row.checkpoint["publish_routes"]["active"] == "sociamonials"
    assert row.checkpoint["publish_routes"]["native"]["attempts"] == 2
    assert row.checkpoint["publish_routes"]["sociamonials"]["attempts"] == 2
    with closing(sqlite3.connect(path)) as conn:
        body = conn.execute("SELECT body FROM publish_alert_outbox").fetchone()[0]
    assert "Native: failed" in body and "Sociamonials: failed" in body


def test_known_native_credential_failure_can_use_separate_verified_backup(fixture, monkeypatch):
    path, _, _ = fixture
    native = Mock(side_effect=PreparedPublishError("HTTP 401 invalid credentials", retryable=False))
    backup = Mock(return_value=delivered())
    monkeypatch.setattr(sm, "publish_via_sociamonials", backup)
    run(path, native)
    assert native.call_count == 1 and backup.call_count == 1
    assert target(fixture).status == "succeeded"


def test_backup_retry_resumes_without_restarting_native(fixture, monkeypatch):
    path, _, _ = fixture
    native = Mock(side_effect=RuntimeError("Native provider unavailable"))
    backup = Mock(side_effect=[RuntimeError("Backup connection refused before submit"), delivered()])
    monkeypatch.setattr(sm, "publish_via_sociamonials", backup)
    run(path, native)
    assert target(fixture).status == "succeeded"
    assert native.call_count == 2 and backup.call_count == 2


def test_backup_daily_retry_after_is_not_shortened(fixture, monkeypatch):
    path, _, _ = fixture
    error = sm.SociamonialsFallbackError("HTTP 429")
    error.retryable = True
    error.details = {"retryAfter": 86400, "retrySafe": True}
    native = Mock(side_effect=PreparedPublishError("HTTP 401 invalid credentials", retryable=False))
    monkeypatch.setattr(sm, "publish_via_sociamonials", Mock(side_effect=error))
    run(path, native)
    row = target(fixture)
    assert row.status == "retrying"
    assert row.checkpoint["publish_routes"]["active"] == "sociamonials"
    assert jobs._utc_datetime(row.next_attempt_at) > jobs._utc_datetime(row.started_at) + __import__('datetime').timedelta(hours=23)


def test_uncertain_native_submission_never_calls_backup(fixture, monkeypatch):
    path, _, _ = fixture
    async def native(platform, payload, _target):
        delivery.starting(payload, operation="native-create-post")
        raise TimeoutError("Response was lost")
    backup = Mock(return_value=delivered())
    monkeypatch.setattr(sm, "publish_via_sociamonials", backup)
    run(path, native)
    assert target(fixture).status == "needs_review"
    backup.assert_not_called()


@pytest.mark.parametrize("message", ["account disabled", "user banned", "content policy violation",
                                     "video file is missing", "message does not match account language"])
def test_unsafe_native_failure_never_uses_backup(fixture, monkeypatch, message):
    path, _, _ = fixture
    native = Mock(side_effect=PreparedPublishError(message, retryable=False))
    backup = Mock(return_value=delivered())
    monkeypatch.setattr(sm, "publish_via_sociamonials", backup)
    run(path, native)
    assert target(fixture).status == "failed"
    backup.assert_not_called()


def test_disabled_platform_preflight_stays_disabled(fixture, monkeypatch):
    path, _, _ = fixture
    monkeypatch.setenv("SAU_BLUESKY_ENABLED", "0")
    native, backup = Mock(), Mock(return_value=delivered())
    monkeypatch.setattr(sm, "publish_via_sociamonials", backup)
    run(path, native)
    assert target(fixture).status == "blocked"
    native.assert_not_called(); backup.assert_not_called()


def test_outbox_failure_does_not_stop_account_maintenance(fixture, monkeypatch):
    path, _, _ = fixture
    monkeypatch.setattr(outbox, "flush_due", Mock(side_effect=sqlite3.OperationalError("busy")))
    listed = Mock(return_value=[])
    monkeypatch.setattr(profiles, "list_accounts", listed)
    instance = PublishWorker(Mock(), db_path=path)
    asyncio.run(instance._run_maintenance_tick())
    listed.assert_called_once()


def test_backup_eligibility_does_not_override_policy_or_receipts():
    credential = PreparedPublishError("HTTP 401 invalid token", retryable=False)
    assert fc.backup_eligible(credential)
    assert not fc.backup_eligible(credential, checkpoint={"provider_receipt": {"id": "keep"}})
    assert not fc.backup_eligible(credential, checkpoint={"submission_started": True})
    assert not fc.backup_eligible(PreparedPublishError("user banned HTTP 401", retryable=False))


def test_provider_budget_survives_checkpoint_round_trip():
    payload = {}
    assert delivery.begin_route_attempt(payload, "native", max_attempts=1) == 1
    delivery.select_backup(payload)
    assert delivery.begin_route_attempt(payload, "sociamonials", max_attempts=1) == 1
    restored = {"_delivery": __import__('json').loads(__import__('json').dumps(delivery.state(payload)))}
    assert delivery.active_route(restored) == "sociamonials"
    with pytest.raises(delivery.RouteExhausted):
        delivery.begin_route_attempt(restored, "sociamonials", max_attempts=1)


def test_native_provider_credit_failure_uses_separate_backup(fixture, monkeypatch):
    path, _, _ = fixture
    native = Mock(side_effect=PreparedPublishError("HTTP 402 CreditsDepleted", retryable=False))
    backup = Mock(return_value=delivered())
    monkeypatch.setattr(sm, "publish_via_sociamonials", backup)
    run(path, native)
    assert target(fixture).status == "succeeded"
    assert native.call_count == 1 and backup.call_count == 1


def test_exhausted_native_429_pins_backup_but_retains_reset(fixture, monkeypatch):
    from datetime import timedelta
    path, _, _ = fixture
    error = PreparedPublishError("HTTP 429", retryable=True,
                                 details={"retryAfter": 3600, "definitiveNonDelivery": True})
    native = Mock(side_effect=error)
    backup = Mock(return_value=delivered())
    monkeypatch.setattr(sm, "publish_via_sociamonials", backup)
    run(path, native, budget=1)
    row = target(fixture)
    assert row.status == "retrying"
    assert row.checkpoint["publish_routes"]["active"] == "sociamonials"
    assert row.checkpoint["publish_routes"]["sociamonials"]["outcome"] == "waiting_for_reset"
    assert row.checkpoint["publish_routes"]["sociamonials"].get("attempts", 0) == 0
    assert jobs._utc_datetime(row.next_attempt_at) >= jobs._utc_datetime(row.started_at) + timedelta(seconds=3599)
    assert native.call_count == 1
    backup.assert_not_called()


@pytest.mark.parametrize("checkpoint", [
    {"checkpoint_invalid": True}, {"provider_receipt": []},
    {"provider_receipt": {"provider": "sociamonials"}},
    {"submission_started": "false"}, {"payload": []},
    {"publish_routes": []}, {"publish_routes": {"native": []}},
])
def test_malformed_checkpoint_stops_before_any_provider(fixture, monkeypatch, checkpoint):
    import json
    path, _, _ = fixture
    row = target(fixture)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE publish_job_targets SET checkpoint_json=? WHERE id=?",
                     (json.dumps(checkpoint), row.id))
    native, backup = Mock(), Mock(return_value=delivered())
    monkeypatch.setattr(sm, "publish_via_sociamonials", backup)
    run(path, native)
    assert target(fixture).status == "needs_review"
    native.assert_not_called()
    backup.assert_not_called()


def test_retry_after_supports_case_insensitive_headers_without_shortening():
    from types import SimpleNamespace
    error = RuntimeError("HTTP 429")
    error.response = SimpleNamespace(headers={"retry-after": "7776000"})
    assert worker_module._retry_after_seconds(error) == 7776000


def test_non_transport_error_after_submission_never_republishes(fixture, monkeypatch):
    path, _, _ = fixture
    async def execute(_platform, payload, _target):
        delivery.starting(payload, operation="native-create-post")
        raise ValueError("Invalid JSON response")
    native = Mock(side_effect=execute)
    backup = Mock(return_value=delivered())
    monkeypatch.setattr(sm, "publish_via_sociamonials", backup)
    run(path, native)
    assert target(fixture).status == "needs_review"
    assert native.call_count == 1
    backup.assert_not_called()


def test_verified_auth_rejection_after_start_can_retry_backup_without_losing_proof(fixture, monkeypatch):
    from types import SimpleNamespace
    path, _, _ = fixture
    error = PreparedPublishError("HTTP 401 invalid credentials", retryable=False)
    error.response = SimpleNamespace(status_code=401, headers={})
    async def execute(_platform, payload, _target):
        delivery.starting(payload, operation="native-create-post")
        raise error
    native = Mock(side_effect=execute)
    backup = Mock(side_effect=[RuntimeError("Backup HTTP 503 before submission"), delivered()])
    monkeypatch.setattr(sm, "publish_via_sociamonials", backup)
    run(path, native)
    assert target(fixture).status == "succeeded"
    assert native.call_count == 1 and backup.call_count == 2
    assert target(fixture).checkpoint["definitive_non_delivery"]["provider"] == "native"


@pytest.mark.parametrize("active", [[], {}])
def test_unhashable_provider_checkpoint_stops_for_review(fixture, monkeypatch, active):
    test_malformed_checkpoint_stops_before_any_provider(fixture, monkeypatch, {"publish_routes": {"active": active}})


@pytest.mark.parametrize("message", ["user banned", "user_banned", "account is suspended"])
def test_unflagged_ban_is_permanent_without_retry_or_backup(fixture, monkeypatch, message):
    path, _, _ = fixture
    native = Mock(side_effect=RuntimeError(message))
    backup = Mock(return_value=delivered())
    monkeypatch.setattr(sm, "publish_via_sociamonials", backup)
    run(path, native)
    assert target(fixture).status == "failed" and native.call_count == 1
    backup.assert_not_called()


def test_expired_backup_lease_uses_remaining_provider_budget_not_total_claims(fixture, monkeypatch):
    path, _, _ = fixture
    claimed = jobs.claim_next_targets(db_path=path, owner="fixture")[0]
    checkpoint = {"publish_routes": {"active": "sociamonials",
        "native": {"attempts": 2, "max_attempts": 2, "outcome": "failed"},
        "sociamonials": {"attempts": 1, "max_attempts": 2, "outcome": "retrying"}}}
    assert jobs.save_target_checkpoint(claimed.id, checkpoint, lease_token=claimed.lease_token, db_path=path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE publish_job_targets SET attempts=3,lease_expires_at='2000-01-01T00:00:00' WHERE id=?", (claimed.id,))
    assert jobs.requeue_stale_running(max_attempts=2, db_path=path) == 1
    assert target(fixture).status == "retrying"
    native = Mock()
    backup = Mock(return_value=delivered())
    monkeypatch.setattr(sm, "publish_via_sociamonials", backup)
    run(path, native)
    assert target(fixture).status == "succeeded"
    assert target(fixture).checkpoint["publish_routes"]["sociamonials"]["attempts"] == 2
    native.assert_not_called()
    backup.assert_called_once()


def test_rejection_proof_is_bound_to_one_operation_generation():
    payload = {}
    delivery.starting(payload, operation="native-create-post")
    delivery.record_non_delivery(payload, provider="native", reason="Explicit rejection")
    assert not delivery.has_unresolved_submission(payload)
    assert delivery.state(payload)["submission_started"] is True
    delivery.starting(payload, operation="native-create-post")
    assert delivery.has_unresolved_submission(payload)


def test_restart_cannot_raise_the_original_finite_provider_budget():
    payload = {}
    delivery.begin_route_attempt(payload, "native", max_attempts=2)
    delivery.begin_route_attempt(payload, "native", max_attempts=5)
    assert delivery.publish_routes(payload)["native"]["max_attempts"] == 2
    with pytest.raises(delivery.RouteExhausted):
        delivery.begin_route_attempt(payload, "native", max_attempts=5)


def test_exhausted_checkpoint_cannot_hide_a_previous_native_ban():
    assert not fc.backup_eligible(delivery.RouteExhausted("native"), checkpoint={
        "publish_routes": {"native": {"attempts": 2, "error": "user banned"}}})


def test_real_adapter_accepts_worker_scoped_rejection_and_confirms_exact_destination(fixture, monkeypatch):
    from types import SimpleNamespace
    from tests.test_sociamonials_fallback import ContractSession, profile
    path, _, _ = fixture
    session = ContractSession(profiles=[profile(network="blsk", ref="fixture")])
    monkeypatch.setattr(sm, "_get_session", lambda _session=None: session)
    error = PreparedPublishError("HTTP 401 invalid credentials", retryable=False)
    error.response = SimpleNamespace(status_code=401, headers={})
    async def execute(_platform, payload, _target):
        delivery.starting(payload, operation="native-create-post")
        raise error
    native = Mock(side_effect=execute)
    run(path, native)
    assert target(fixture).status == "succeeded" and native.call_count == 1
    assert len(session.calls_for("POST", "/posts", create=True)) == 1
    assert target(fixture).checkpoint["provider_receipt"]["network"] == "blsk"
    assert target(fixture).checkpoint["provider_receipt"]["profile_refs"] == ["fixture"]


def _use_x_fixture(fixture, monkeypatch, *, blocked=False):
    import json
    path, account, job = fixture
    config = {"twitterAuthType": "api", "accessToken": "fixture-access", "refreshToken": "fixture-refresh",
              "accessTokenExpiresAt": "2020-01-01T00:00:00+00:00",
              "sociamonials": {"network": "tw", "profile_refs": ["fixture"]}}
    if blocked:
        config["_nativeCredentialError"] = {"kind": "unknown", "code": "invalid_request", "httpStatus": 400}
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE accounts SET platform='twitter',config_json=? WHERE id=?", (json.dumps(config), account.id))
        conn.execute("UPDATE publish_jobs SET platform='twitter' WHERE id=?", (job.id,))
    monkeypatch.setattr(worker_module, "_x_direct_publish_enabled", lambda: True)
    return path, account, job


def test_known_configuration_warning_bypasses_native_only_for_existing_backup(fixture, monkeypatch):
    path, _, _ = _use_x_fixture(fixture, monkeypatch, blocked=True)
    native = Mock()
    result = {**delivered(), "network": "tw"}
    backup = Mock(return_value=result)
    monkeypatch.setattr(sm, "publish_via_sociamonials", backup)
    run(path, native)
    assert target(fixture).status == "succeeded"
    native.assert_not_called()
    backup.assert_called_once()
    assert target(fixture).checkpoint["publish_routes"]["native"]["outcome"] == "credential_unavailable"


def test_unsupported_original_intent_is_blocked_before_provider_creation(fixture, monkeypatch):
    path, _, _ = fixture
    native = Mock(side_effect=PreparedPublishError("HTTP 401 invalid credentials", retryable=False))
    backup = Mock(side_effect=sm.SociamonialsFallbackError("Original alt text cannot be preserved",
                  details={"intentReviewRequired": True, "field": "alt_text"}))
    monkeypatch.setattr(sm, "publish_via_sociamonials", backup)
    run(path, native)
    row = target(fixture)
    assert row.status == "blocked" and not row.checkpoint.get("provider_receipt")
    assert row.checkpoint["publish_routes"]["sociamonials"]["outcome"] == "blocked_intent"
    assert native.call_count == 1 and backup.call_count == 1


def _rejected_native_media_fixture(fixture, monkeypatch, tmp_path, *, override=None):
    import hashlib
    import json
    from PIL import Image
    from myUtils import generic_delivery, prepared_publishers
    from tests.test_publish_route_safety import Response
    from tests.test_sociamonials_fallback import ContractSession, profile
    path, _, job = _use_x_fixture(fixture, monkeypatch)
    image = tmp_path / "approved-image.jpg"
    Image.new("RGB", (64, 64), (30, 60, 90)).save(image)
    payload = {"message": "Original approved caption", "artifacts": [{"artifact_kind": "prepared_image",
               "local_path": str(image), "metadata": {"sha256": hashlib.sha256(image.read_bytes()).hexdigest(), "role": "image"}}]}
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE publish_jobs SET payload_json=? WHERE id=?", (json.dumps(payload), job.id))
    session = ContractSession(profiles=[profile(network="tw", ref="fixture")], override=override)
    monkeypatch.setattr(sm, "_RATE_WINDOWS", {})
    monkeypatch.setattr(sm, "_get_session", lambda supplied=None: session)
    def fixture_public_host(host):
        assert host in {"cdn.example", "upload.example"}
    monkeypatch.setattr(sm, "_resolve_public_host", fixture_public_host)
    async def native(_platform, working, _target):
        operations = generic_delivery.Operations(working, "twitter")
        operations.plan(["media:0", "tweet"])
        operations.begin("media:0:initialize")
        operations.record("media:0:initialize", "native-media-id", provider_receipt=False)
        try:
            operations.request("media:0:finalize", lambda: Response(402))
        except PreparedPublishError as exc:
            raise prepared_publishers._raise_x_publish_error(exc, stage="media upload (OAuth 2.0)") from exc
    execute = Mock(side_effect=native)
    return path, payload, session, execute


def test_real_adapter_handoffs_rejected_media_only_journal_without_dropping_image(fixture, monkeypatch, tmp_path):
    path, payload, session, execute = _rejected_native_media_fixture(fixture, monkeypatch, tmp_path)
    run(path, execute)
    row = target(fixture)
    assert row.status == "succeeded" and execute.call_count == 1
    assert row.checkpoint["provider_receipt"]["network"] == "tw"
    assert row.checkpoint["provider_receipt"]["profile_refs"] == ["fixture"]
    assert row.checkpoint["provider_operations"]["twitter"]["items"]["media:0:initialize"]["id"] == "native-media-id"
    assert len(session.calls_for("POST", "/posts", create=True)) == 1
    assert session.last_body["message"] == payload["message"]
    assert session.last_body["image_urls"]


def test_backup_create_429_retries_remaining_budget_without_repeating_native_media(fixture, monkeypatch, tmp_path):
    from tests.test_sociamonials_fallback import Response
    refused = []
    def override(method, url, kwargs):
        if method == "POST" and url.endswith("/posts") and not kwargs["json"].get("validate_only") and not refused:
            refused.append(True)
            return Response({"error": {"code": "rate_limit"}}, status=429)
    path, payload, session, execute = _rejected_native_media_fixture(fixture, monkeypatch, tmp_path, override=override)
    run(path, execute)
    row = target(fixture)
    assert row.status == "succeeded" and execute.call_count == 1
    assert row.checkpoint["publish_routes"]["native"]["attempts"] == 1
    assert row.checkpoint["publish_routes"]["sociamonials"]["attempts"] == 2
    creates = session.calls_for("POST", "/posts", create=True)
    assert len(creates) == 2 and creates[0][2]["json"] == creates[1][2]["json"]
    assert creates[1][2]["json"]["message"] == payload["message"]
    assert len([call for call in session.calls_for("POST", "/media/uploads") if call[1].endswith("/media/uploads")]) == 1
    assert row.checkpoint["provider_operations"]["twitter"]["items"]["media:0:initialize"]["id"] == "native-media-id"
    assert row.checkpoint["provider_receipt"]["profile_refs"] == ["fixture"]


def test_local_backup_create_rate_refusal_keeps_safe_remaining_budget(fixture, monkeypatch, tmp_path):
    path, _, session, execute = _rejected_native_media_fixture(fixture, monkeypatch, tmp_path)
    take_rate, refused = sm._take_rate, []
    def local_refusal(key, bucket, limit):
        last_body = session.calls[-1][2].get("json", {}) if session.calls else {}
        if (bucket == "write" and last_body.get("validate_only") is True
                and str(last_body.get("image_urls", [""])[0]).startswith("asset://") and not refused):
            refused.append(True)
            raise sm.SociamonialsFallbackError("Local rate refusal", retryable=True,
                details={"httpStatus": 429, "retrySafe": True})
        take_rate(key, bucket, limit)
    monkeypatch.setattr(sm, "_take_rate", local_refusal)
    run(path, execute)
    row = target(fixture)
    assert refused and row.status == "succeeded" and execute.call_count == 1
    assert row.checkpoint["publish_routes"]["sociamonials"]["attempts"] == 2
    assert len(session.calls_for("POST", "/posts", create=True)) == 1
    assert len([call for call in session.calls_for("POST", "/media/uploads") if call[1].endswith("/media/uploads")]) == 1


@pytest.mark.parametrize("unknown", ["transport", "missing_receipt", "rejected_then_transport"])
def test_unknown_backup_create_is_not_retried_despite_reconciled_native_media(fixture, monkeypatch, tmp_path, unknown):
    from myUtils import generic_delivery
    from tests.test_sociamonials_fallback import Response
    creates = []
    def override(method, url, kwargs):
        if method == "POST" and url.endswith("/posts") and not kwargs["json"].get("validate_only"):
            creates.append(True)
            if unknown == "rejected_then_transport" and len(creates) == 1:
                return Response({"error": {"code": "rate_limit"}}, status=429)
            if unknown in {"transport", "rejected_then_transport"}:
                raise TimeoutError("Lost backup create response")
            return Response({"status": "scheduled"})
    path, _, session, execute = _rejected_native_media_fixture(fixture, monkeypatch, tmp_path, override=override)
    run(path, execute)
    row = target(fixture)
    assert row.status == "needs_review" and execute.call_count == 1
    expected_attempts = 2 if unknown == "rejected_then_transport" else 1
    assert row.checkpoint["publish_routes"]["sociamonials"]["attempts"] == expected_attempts
    assert generic_delivery.media_only_reconciled(row.checkpoint, target_id=row.id)
    assert delivery.has_unresolved_submission({"_delivery": row.checkpoint})
    assert not row.checkpoint.get("provider_receipt")
    assert len(session.calls_for("POST", "/posts", create=True)) == expected_attempts


def _move_mapping_to_profile(fixture):
    import json
    path, account, job = fixture
    current = profiles.get_account(account.id, db_path=path)
    config = dict(current.config)
    mapping = config.pop("sociamonials")
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE accounts SET config_json=? WHERE id=?", (json.dumps(config), account.id))
    profiles.update_profile(job.profile_id, settings={"sociamonials": {str(account.id): mapping}}, db_path=path)


def test_profile_only_verified_mapping_bypasses_native_configuration_hold(fixture, monkeypatch):
    from tests.test_sociamonials_fallback import ContractSession, profile
    path, _, _ = _use_x_fixture(fixture, monkeypatch, blocked=True)
    _move_mapping_to_profile(fixture)
    session = ContractSession(profiles=[profile(network="tw", ref="fixture")])
    monkeypatch.setattr(sm, "_RATE_WINDOWS", {})
    monkeypatch.setattr(sm, "_get_session", lambda supplied=None: session)
    native = Mock()
    run(path, native)
    row = target(fixture)
    assert row.status == "succeeded"
    native.assert_not_called()
    assert row.checkpoint["publish_routes"]["native"]["outcome"] == "credential_unavailable"
    assert row.checkpoint["provider_receipt"]["network"] == "tw"
    assert row.checkpoint["provider_receipt"]["profile_refs"] == ["fixture"]


def test_profile_only_mapping_retains_native_reset_delay(fixture, monkeypatch):
    _move_mapping_to_profile(fixture)
    test_exhausted_native_429_pins_backup_but_retains_reset(fixture, monkeypatch)
