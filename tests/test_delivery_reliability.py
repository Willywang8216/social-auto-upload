"""Regression tests for the audited worker/provider failures. No real publishing."""
import asyncio
import copy
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from db.createTable import bootstrap
from myUtils import delivery, failure_classification as fc, jobs, pre_flight
from myUtils import prepared_publishers as publishers
from myUtils import worker as workers
from myUtils.meta_delivery import finish_container


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "test.db"
    bootstrap(path)
    return path


def claimed(database):
    job = jobs.enqueue_job(jobs.JobSpec(platform="bluesky", payload={},
                          targets=[("test-cookie.json", "test.mp4", None)]), db_path=database)
    return job, jobs.claim_next_targets(db_path=database)[0]


def test_preflight_compares_aware_expiry_and_keeps_refreshable_tokens():
    account = SimpleNamespace(auth_type="oauth")
    expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    config = {"accessToken": "synthetic", "accessTokenExpiresAt": expired}
    assert pre_flight._check_token_validity(account, "youtube", config)[0] is False
    assert pre_flight._check_token_validity(account, "youtube", {**config, "refreshToken": "synthetic"})[0] is True
    assert pre_flight._check_token_validity(account, "youtube", {**config, "accessTokenExpiresAt": "2035-01-01T00:00:00Z"})[0]
    assert pre_flight._account_enabled(SimpleNamespace(enabled=True, status=0))
    assert not pre_flight._account_enabled(SimpleNamespace(enabled=False, status=1))


def test_legacy_reference_is_not_rejected_as_missing_account(database):
    assert pre_flight.run_pre_flight(account_ref="legacy.cookie", job_id=1,
               platform="douyin", payload={}, db_path=database).ok


def test_refreshable_expiry_category_and_revoked_refresh():
    assert fc.classify_failure(RuntimeError("token expired; refresh required")).category is fc.RECOVERABLE
    assert fc.classify_failure(RuntimeError("invalid_grant: refresh_token_revoked")).category is fc.PERMANENT


@pytest.mark.parametrize("attempts", [3, 10])
def test_recoverable_failures_obey_attempt_limit(database, monkeypatch, attempts):
    _, target = claimed(database)
    target = replace(target, attempts=attempts)
    worker = workers.PublishWorker(Mock(), db_path=database)
    monkeypatch.setattr(worker, "_alert_publish_failure", Mock())
    monkeypatch.setattr(workers.sociamonials_fallback, "is_enabled", lambda: False)
    asyncio.run(worker._handle_failure(target, workers.MediaRestoreError("artifact not materialised"), Mock()))
    assert jobs.get_target(target.id, db_path=database).status == "failed"


def test_preflight_exception_is_settled_not_stranded(database, monkeypatch):
    _, target = claimed(database)
    async def executor(*args):
        pytest.fail("must not publish after preflight failure")
    worker = workers.PublishWorker(executor, db_path=database)
    monkeypatch.setattr(workers.pre_flight, "run_pre_flight", Mock(side_effect=sqlite3.OperationalError("database is locked")))
    asyncio.run(worker._run_target_inline(target))
    current = jobs.get_target(target.id, db_path=database)
    assert current.status == "retrying"
    assert current.next_attempt_at


def test_filtered_payload_preserves_partial_progress(monkeypatch):
    async def publisher(**kwargs):
        assert len(kwargs["payload"]["artifacts"]) == 1
        kwargs["payload"]["telegramCompletedByDelivery"] = {"one": {"chat": ["media"]}}
        raise RuntimeError("synthetic partial failure")
    monkeypatch.setitem(workers.PREPARED_PUBLISHER_REGISTRY, "telegram", publisher)
    payload = {"fileRecordIds": [1], "artifacts": [{"source_file_record_id": 1}, {"source_file_record_id": 2}]}
    with pytest.raises(RuntimeError):
        asyncio.run(workers._run_prepared_campaign_upload("telegram", payload, None, account=None, account_file=None))
    assert payload["telegramCompletedByDelivery"]["one"]["chat"] == ["media"]
    assert len(payload["artifacts"]) == 2


def test_pending_outcome_is_not_success_or_repost(database):
    _, target = claimed(database)
    async def executor(platform, payload, target):
        delivery.accepted(payload, provider="synthetic", receipt_id="receipt-1")
        raise delivery.DeliveryPending("processing")
    worker = workers.PublishWorker(executor, db_path=database)
    asyncio.run(worker._run_target_inline(target))
    current = jobs.get_target(target.id, db_path=database)
    assert current.status == "awaiting_delivery"
    assert current.checkpoint["provider_receipt"]["id"] == "receipt-1"
    assert not jobs.claim_next_targets(db_path=database)


def test_lost_response_never_becomes_blind_retry(database, monkeypatch):
    _, target = claimed(database)
    async def executor(platform, payload, target):
        delivery.starting(payload, operation="synthetic-create")
        raise TimeoutError("write operation timed out")
    worker = workers.PublishWorker(executor, db_path=database)
    monkeypatch.setattr(worker, "_alert_publish_failure", Mock())
    asyncio.run(worker._run_target_inline(target))
    assert jobs.get_target(target.id, db_path=database).status == "needs_review"


def test_telegram_persists_before_send_and_after_success():
    checkpoints = []
    payload = {"_save_checkpoint": lambda state: checkpoints.append(copy.deepcopy(state))}
    completed = {}
    publishers._telegram_checkpoint(payload, "target", completed, "chat", {"ambiguous:media"})
    assert checkpoints[-1]["submission_started"]
    publishers._telegram_checkpoint(payload, "target", completed, "chat", {"media"})
    assert not checkpoints[-1]["submission_started"]
    assert checkpoints[-1]["payload"]["telegramCompletedByDelivery"]["target"]["chat"] == ["media"]


def test_meta_receipt_reuses_container_and_does_not_repost_after_lost_response(monkeypatch):
    payload = {}
    delivery.accepted(payload, provider="instagram", receipt_id="container-1", stage="publishing")
    http = Mock()
    monkeypatch.setattr(publishers, "_wait_for_container_status", lambda *a, **kw: "FINISHED")
    with pytest.raises(delivery.DeliveryPending):
        finish_container(http, platform="instagram", user_id="user", access_token="synthetic",
                         payload=payload, container_id="ignored")
    http.post.assert_not_called()
    monkeypatch.setattr(publishers, "_wait_for_container_status", lambda *a, **kw: "PUBLISHED")
    finish_container(http, platform="instagram", user_id="user", access_token="synthetic",
                     payload=payload, container_id="ignored")
    assert delivery.receipt_for(payload, "instagram")["status"] == "delivered"
    http.post.assert_not_called()


def test_receipt_deadline_does_not_reset_on_update():
    payload = {}
    delivery.accepted(payload, provider="synthetic", receipt_id="1")
    payload["_delivery"]["provider_receipt"]["submitted_at"] = "2000-01-01T00:00:00+00:00"
    delivery.accepted(payload, provider="synthetic", receipt_id="1", stage="publishing")
    with pytest.raises(delivery.DeliveryUnknown):
        delivery.pending_or_unknown(payload, "expired receipt")


def test_retry_after_and_error_redaction():
    error = publishers.PreparedPublishError("HTTP 429", details={"retryAfter": "90"})
    assert workers._retry_after_seconds(error) == 90
    assert "very-secret" not in workers._scrub_secrets('accessToken="very-secret"')
    assert "123:test-secret" not in workers._scrub_secrets("https://api.telegram.org/bot123:test-secret/sendMessage")
