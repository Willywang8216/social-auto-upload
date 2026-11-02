"""Explicit managed routing and recovery idempotency; no live provider calls."""
import asyncio
import copy
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from db.createTable import bootstrap
from myUtils import delivery, jobs, pre_flight, worker
from myUtils import sociamonials_fallback as sm


def managed_config(**changes):
    return {
        "publishingProvider": "sociamonials",
        "sociamonials": {"network": "thrd", "profile_refs": ["fixture-only"], "workspace_id": "123"},
        **changes,
    }


def test_primary_route_requires_explicit_single_matching_destination():
    assert not sm.prefers_sociamonials(None, platform="threads")
    assert sm.prefers_sociamonials({"config": managed_config()}, platform="threads")
    for config in (
        {"publishingProvider": "unknown"},
        {"publishingProvider": "sociamonials"},
        managed_config(sociamonials={"network": "tw", "profile_refs": ["other"]}),
        managed_config(sociamonials={"network": "thrd", "profile_refs": ["first", "second"]}),
    ):
        with pytest.raises(sm.SociamonialsNotConfigured):
            sm.prefers_sociamonials({"config": config}, platform="threads")


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "routing.db"
    bootstrap(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("INSERT INTO profiles(id,name,slug) VALUES (900,'fixture','fixture')")
        conn.execute("INSERT INTO accounts(id,profile_id,platform,account_name,auth_type,enabled,cookie_path,config_json) "
                     "VALUES (900,900,'threads','fixture','oauth',1,'','{}')")
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.mark.parametrize("receipt_provider,prefer,needs_reconnect,expected", [
    (None, True, True, "managed"),
    ("sociamonials", False, True, "managed"),
    ("threads", True, False, "direct"),
    ("threads", True, True, "blocked"),
])
def test_receipt_provider_wins_without_direct_token_blocking_managed_route(
    database, monkeypatch, receipt_provider, prefer, needs_reconnect, expected,
):
    config = managed_config(_needsReconnect=needs_reconnect) if prefer else {"_needsReconnect": needs_reconnect}
    conn = sqlite3.connect(database)
    try:
        conn.execute("UPDATE accounts SET config_json=? WHERE id=900", (json.dumps(config),))
        conn.commit()
    finally:
        conn.close()
    job = jobs.enqueue_job(jobs.JobSpec(platform="threads", profile_id=900, payload={"message": "fixture"},
                           targets=[("account:900", "/fixture.mp4", None)]), db_path=database)
    target = jobs.claim_next_targets(db_path=database)[0]
    if receipt_provider:
        checkpoint = {"submission_started": True, "provider_receipt": {
            "provider": receipt_provider, "id": "receipt-fixture", "submitted_at": delivery.utc_now(),
        }}
        jobs.save_target_checkpoint(target.id, checkpoint, lease_token=target.lease_token, db_path=database)
        target = jobs.get_target(target.id, db_path=database)
    calls = []

    async def direct(platform, payload, claimed):
        calls.append("direct")
        raise delivery.DeliveryPending("fixture waiting")

    instance = worker.PublishWorker(direct, db_path=database)
    monkeypatch.setattr(instance, "_alert_publish_failure", Mock())
    monkeypatch.setattr(worker, "_content_guard_error", lambda *_args: None)
    monkeypatch.setattr(worker, "_language_routing_error", lambda *_args, **_kwargs: None)

    async def managed(claimed, message, log):
        calls.append("managed")
        payload = instance._payloads[claimed.id]
        if delivery.receipt_for(payload, "sociamonials") is None:
            delivery.accepted(payload, provider="sociamonials", receipt_id="receipt-fixture")
        raise delivery.DeliveryPending("fixture waiting")

    monkeypatch.setattr(instance, "_try_sociamonials_fallback", managed)
    asyncio.run(instance._run_target_inline(target))
    current = jobs.get_target(target.id, db_path=database)
    if expected == "blocked":
        assert calls == [] and current.status == "blocked"
    else:
        assert calls == [expected] and current.status == "awaiting_delivery"
        assert current.checkpoint["provider_receipt"]["provider"] == ("sociamonials" if expected == "managed" else "threads")
    assert jobs.get_job(job.id, db_path=database).status != "succeeded"


def test_disabled_account_stays_blocked_for_managed_route(database):
    conn = sqlite3.connect(database)
    try:
        conn.execute("UPDATE accounts SET enabled=0,config_json=? WHERE id=900", (json.dumps(managed_config()),))
        conn.commit()
    finally:
        conn.close()
    result = pre_flight.run_pre_flight(account_ref="account:900", job_id=1, platform="threads",
                                     payload={}, db_path=database, validate_credentials=False)
    assert not result.ok and result.checks["account_enabled"] is False


class FakeSession:
    def __init__(self):
        self.bodies = []

    def post(self, url, **kwargs):
        assert url == sm.POSTS_URL
        self.bodies.append(copy.deepcopy(kwargs["json"]))
        return SimpleNamespace(status_code=200, json=lambda: {"post_id": 123}, text="", headers={})


def test_recovery_generation_changes_only_the_deliberate_recovery_key():
    session = FakeSession()
    account = {"id": 900, "config": managed_config()}
    for generation in (None, "a" * 64, "a" * 64, "b" * 64):
        payload = {"message": "Synthetic only"}
        if generation:
            payload["_delivery"] = {"recovery_generation": generation}
        sm.publish_via_sociamonials(platform="threads", account=account, payload=payload,
                                  target_id=99, session=session, api_key="fixture-only",
                                  workspace_id="123", delivery_timeout=0)
    keys = [body["idempotency_key"] for body in session.bodies]
    assert keys == ["sau-target-99", "sau-target-99-recovery-" + "a" * 64,
                    "sau-target-99-recovery-" + "a" * 64, "sau-target-99-recovery-" + "b" * 64]


def test_invalid_recovery_generation_never_creates_a_post():
    session = FakeSession()
    with pytest.raises(sm.SociamonialsFallbackError, match="Invalid recovery generation"):
        sm.publish_via_sociamonials(platform="threads", account={"id": 900, "config": managed_config()},
                                  payload={"message": "fixture", "_delivery": {"recovery_generation": []}},
                                  target_id=99, session=session, api_key="fixture-only", workspace_id="123")
    assert not session.bodies


@pytest.mark.parametrize("artifacts", [[{}], [{"public_url": "https://example.test/video.mp4"}]])
def test_declared_media_is_never_silently_replaced_by_text(monkeypatch, artifacts):
    session = FakeSession()
    monkeypatch.setattr(sm, "_verify_remote_media", lambda *_args, **_kwargs: False)
    with pytest.raises(sm.SociamonialsFallbackError, match="refusing a text-only replacement"):
        sm.publish_via_sociamonials(
            platform="threads", account={"id": 900, "config": managed_config()},
            payload={"message": "fixture", "artifacts": artifacts}, target_id=99,
            session=session, api_key="fixture-only", workspace_id="123", delivery_timeout=0,
        )
    assert not session.bodies


@pytest.mark.parametrize("reason,receipt", [("disabled", False), ("disabled", True), ("no-key", False), ("no-key", True)])
def test_real_managed_helper_blocks_unavailable_configuration(database, monkeypatch, reason, receipt):
    conn = sqlite3.connect(database)
    try:
        conn.execute("UPDATE accounts SET config_json=? WHERE id=900", (json.dumps(managed_config()),))
        conn.commit()
    finally:
        conn.close()
    jobs.enqueue_job(jobs.JobSpec(platform="threads", profile_id=900, payload={"message": "fixture"},
                     targets=[("account:900", "/fixture.mp4", None)]), db_path=database)
    target = jobs.claim_next_targets(db_path=database)[0]
    saved = None
    if receipt:
        saved = {"provider": "sociamonials", "id": "keep-this", "submitted_at": delivery.utc_now()}
        jobs.save_target_checkpoint(target.id, {"submission_started": True, "provider_receipt": saved},
                                    lease_token=target.lease_token, db_path=database)
        target = jobs.get_target(target.id, db_path=database)
    instance = worker.PublishWorker(Mock(side_effect=AssertionError("no direct publish")), db_path=database)
    monkeypatch.setattr(instance, "_alert_publish_failure", Mock())
    monkeypatch.setattr(worker, "_content_guard_error", lambda *_args: None)
    monkeypatch.setattr(worker, "_language_routing_error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sm, "is_enabled", lambda: reason != "disabled")
    publish = Mock(side_effect=sm.SociamonialsNotConfigured("No configured key"))
    monkeypatch.setattr(sm, "publish_via_sociamonials", publish)
    asyncio.run(instance._run_target_inline(target))
    current = jobs.get_target(target.id, db_path=database)
    assert current.status == "blocked"
    if receipt:
        assert current.checkpoint["provider_receipt"] == saved
    if reason == "disabled":
        publish.assert_not_called()
