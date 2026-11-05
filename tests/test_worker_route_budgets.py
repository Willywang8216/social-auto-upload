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
