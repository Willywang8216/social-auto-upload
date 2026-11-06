"""Durable queue contracts: process exclusion, fencing, receipts and atomic intent."""
from __future__ import annotations

import gc
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from db import createTable
from myUtils import jobs
from myUtils.account_identity import canonical_account_key, external_account_identity


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "queue.db"
    createTable.bootstrap(path)
    gc.collect()  # Legacy bootstrap uses SQLite context managers without close().
    with closing(sqlite3.connect(path)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(publish_job_targets)")}
    assert {"lease_token", "checkpoint_json", "next_attempt_at", "delivery_state"} <= columns
    yield path
    gc.collect()


def _enqueue(db_path, refs=("legacy-a",), *, key=None, schedule=None, platform="douyin"):
    return jobs.enqueue_job(jobs.JobSpec(
        platform=platform, payload={"title": "hello"}, idempotency_key=key,
        targets=[(ref, f"file-{i}", schedule) for i, ref in enumerate(refs)],
    ), db_path=db_path)


def _claim(db_path, **kwargs):
    return jobs.claim_next_targets(owner="worker-a", db_path=db_path, **kwargs)


def _update(db_path, target_id, **fields):
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("UPDATE publish_job_targets SET " + ", ".join(f"{key} = ?" for key in fields)
                     + " WHERE id = ?", [*fields.values(), target_id])


def _account(db_path, account_id, *, platform="facebook", config=None, profile_id=100, cookie=""):
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("INSERT OR IGNORE INTO profiles(id, name, slug) VALUES (?, ?, ?)",
                     (profile_id, f"Profile {profile_id}", f"profile-{profile_id}"))
        conn.execute(
            "INSERT INTO accounts(id, profile_id, platform, account_name, cookie_path, config_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (account_id, profile_id, platform, f"account-{account_id}", cookie, json.dumps(config or {})),
        )
    return f"account:{account_id}"


def test_migration_defaults_and_legacy_dataclass_constructor(db_path):
    job = _enqueue(db_path)
    target = jobs.list_targets(job.id, db_path=db_path)[0]
    assert target.checkpoint == {}
    assert target.lease_token is None and target.delivery_state is None
    legacy = jobs.Target(1, 1, "a", "f", None, "pending", 0)
    assert legacy.checkpoint == {}
    legacy.checkpoint["step"] = 1
    assert target.checkpoint == {}
    with closing(sqlite3.connect(db_path)) as conn:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        config = Config(str(createTable.ALEMBIC_INI_PATH))
        config.set_main_option("script_location", str(createTable._PROJECT_ROOT / "migrations"))
        current = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        ancestors = {revision.revision for revision in ScriptDirectory.from_config(config).walk_revisions(head=current)}
        assert "0023_publish_reliability" in ancestors
        assert conn.execute("SELECT COUNT(*) FROM worker_heartbeats").fetchone()[0] == 0
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(publish_job_targets)")}
        assert {"idx_pjt_retry_due", "idx_pjt_lease_expiry"} <= indexes


@pytest.mark.parametrize("platform,configs", [
    ("facebook", ({"pageId": "same"}, {"pageId": "same", "accessToken": "rotated"})),
    ("instagram", ({"igUserId": "same"}, {"igUserId": "same"})),
    ("threads", ({"threadUserId": "same"}, {"userId": "same"})),
    ("youtube", ({"channelId": "same"}, {"channelId": "same"})),
    ("tiktok", ({"openId": "same"}, {"open_id": "same"})),
])
def test_canonical_duplicate_rows_are_excluded_within_batch_and_across_claims(db_path, platform, configs):
    a = _account(db_path, 101, platform=platform, config=configs[0])
    duplicate = _account(db_path, 102, platform=platform, config=configs[1], profile_id=101)
    other = _account(db_path, 103, platform=platform, config={next(iter(configs[0])): "other"})
    _enqueue(db_path, (a, duplicate, a, other), platform=platform)
    first = _claim(db_path, limit=50)
    assert [target.account_ref for target in first] == [a, other]
    assert jobs.claim_next_targets(owner="another-process", limit=10, db_path=db_path) == []
    assert not jobs.has_claimable_targets(db_path=db_path)
    assert jobs.mark_target_success(first[0].id, lease_token=first[0].lease_token, db_path=db_path)
    assert _claim(db_path)[0].account_ref == duplicate


def test_excluded_alias_cookie_fallback_and_platform_are_canonical(db_path):
    a = _account(db_path, 101, cookie=r"C:\cookies\same.json")
    b = _account(db_path, 102, cookie="c:/cookies/same.json", profile_id=101)
    c = _account(db_path, 103, cookie="c:/cookies/same.json", platform="instagram")
    _enqueue(db_path, (a, b, c))
    assert [target.account_ref for target in _claim(db_path, limit=10, excluded_accounts=[b])] == [c]
    with closing(sqlite3.connect(db_path)) as conn:
        assert canonical_account_key(conn, a) == canonical_account_key(conn, b)
        assert canonical_account_key(conn, a) != canonical_account_key(conn, c)
    assert external_account_identity({"platform": " Threads ", "config": {"threadUserId": "123"}}) == ("threads", "123")


def test_two_actual_processes_cannot_claim_same_provider_account(db_path):
    a = _account(db_path, 101, config={"pageId": "one-page"})
    b = _account(db_path, 102, config={"pageId": "one-page"}, profile_id=101)
    _enqueue(db_path, (a, b))
    code = ("import sys; from pathlib import Path; from myUtils.jobs import claim_next_targets; "
            "print(len(claim_next_targets(owner=sys.argv[2], limit=5, db_path=Path(sys.argv[1]))))")
    processes = [subprocess.Popen([sys.executable, "-c", code, str(db_path), f"worker-{i}"],
                                  cwd=Path(__file__).resolve().parents[1],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for i in range(2)]
    try:
        outputs = [process.communicate(timeout=30) for process in processes]
        assert all(process.returncode == 0 for process in processes), outputs
        assert sorted(int(stdout.strip()) for stdout, _ in outputs) == [0, 1]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()


def test_due_order_and_invalid_legacy_schedule_fail_closed(db_path):
    later = _enqueue(db_path, ("a",), key="later", schedule="2001-01-01T00:00:00Z")
    earlier = _enqueue(db_path, ("b",), key="earlier", schedule="2000-01-01T08:00:00+08:00")
    invalid = _enqueue(db_path, ("c",), key="invalid")
    _update(db_path, jobs.list_targets(invalid.id, db_path=db_path)[0].id, schedule_at="garbage")
    assert [target.job_id for target in _claim(db_path, limit=5)] == [earlier.id, later.id]
    assert jobs.claim_next_targets(limit=0, db_path=db_path) == []
    with pytest.raises(ValueError):
        jobs.claim_next_targets(limit=-1, db_path=db_path)


def test_delayed_retry_releases_account_and_survives_restart(db_path):
    _enqueue(db_path, ("a", "a"))
    first = _claim(db_path)[0]
    assert jobs.mark_target_retry(first.id, "rate limited", lease_token=first.lease_token,
                                  delay_seconds=3600, error_category="rate_limit", db_path=db_path)
    waiting = jobs.get_target(first.id, db_path=db_path)
    assert waiting.next_attempt_at and waiting.error_category == "rate_limit"
    assert waiting.lease_owner is None and waiting.lease_token is None and waiting.lease_expires_at is None
    other = jobs.claim_next_targets(owner="worker-b", db_path=db_path)[0]
    assert other.id != first.id
    jobs.mark_target_success(other.id, lease_token=other.lease_token, db_path=db_path)
    assert not jobs.has_claimable_targets(db_path=db_path)
    _update(db_path, first.id, next_attempt_at="2000-01-01T00:00:00")
    retry = _claim(db_path)[0]
    assert retry.id == first.id and retry.attempts == 2
    assert retry.lease_token != first.lease_token


def test_renewal_and_fresh_lease_override_ancient_started_at(db_path):
    _enqueue(db_path)
    target = _claim(db_path)[0]
    _update(db_path, target.id, started_at="2000-01-01T00:00:00")
    assert jobs.requeue_stale_running(db_path=db_path) == 0
    assert not jobs.renew_target_lease(target.id, "wrong", db_path=db_path)
    assert jobs.renew_target_lease(target.id, target.lease_token, 600, db_path=db_path)
    assert jobs.get_target(target.id, db_path=db_path).lease_expires_at > target.lease_expires_at
    _update(db_path, target.id, lease_expires_at="2000-01-01T00:00:00")
    assert not jobs.renew_target_lease(target.id, target.lease_token, db_path=db_path)


def test_all_worker_writes_are_fenced_after_recovery_and_reclaim(db_path):
    job = _enqueue(db_path)
    old = _claim(db_path)[0]
    _update(db_path, old.id, lease_expires_at="2000-01-01T00:00:00")
    assert jobs.requeue_stale_running(db_path=db_path) == 1
    current = jobs.claim_next_targets(owner="replacement", db_path=db_path)[0]
    assert current.lease_token != old.lease_token
    calls = [
        lambda: jobs.mark_target_success(old.id, lease_token=old.lease_token, db_path=db_path),
        lambda: jobs.mark_target_failed(old.id, "old", lease_token=old.lease_token, db_path=db_path),
        lambda: jobs.mark_target_retry(old.id, "old", lease_token=old.lease_token, db_path=db_path),
        lambda: jobs.mark_target_blocked(old.id, "old", lease_token=old.lease_token, db_path=db_path),
        lambda: jobs.mark_target_needs_review(old.id, "old", lease_token=old.lease_token, db_path=db_path),
        lambda: jobs.mark_target_awaiting_delivery(old.id, {"provider_receipt": "old"}, lease_token=old.lease_token, db_path=db_path),
        lambda: jobs.save_target_checkpoint(old.id, {"submission_started": True}, lease_token=old.lease_token, db_path=db_path),
        lambda: jobs.renew_target_lease(old.id, old.lease_token, db_path=db_path),
    ]
    assert not any(call() for call in calls)
    assert not jobs.mark_target_success(old.id, db_path=db_path)
    assert not jobs.mark_target_failed(old.id, "legacy completion", db_path=db_path)
    assert not jobs.mark_target_retry(old.id, "legacy retry", db_path=db_path)
    assert jobs.get_target(old.id, db_path=db_path).checkpoint == {}
    assert jobs.mark_target_success(current.id, lease_token=current.lease_token, db_path=db_path)
    assert jobs.get_job(job.id, db_path=db_path).completed_targets == 1


@pytest.mark.parametrize("checkpoint,attempts,expected", [
    ({}, 1, "retrying"), ({}, 3, "failed"),
    ({"submission_started": True}, 1, "needs_review"),
    ({"submission_started": True}, 99, "needs_review"),
    ({"provider_receipt": {"publish_id": "p1"}}, 99, "needs_review"),
    ({"provider_receipt": {"provider": "tiktok", "id": "p1"}}, 99, "awaiting_delivery"),
])
def test_stale_recovery_uses_submission_evidence_before_retry_budget(db_path, checkpoint, attempts, expected):
    job = _enqueue(db_path)
    target = _claim(db_path)[0]
    jobs.save_target_checkpoint(target.id, checkpoint, lease_token=target.lease_token, db_path=db_path)
    _update(db_path, target.id, attempts=attempts, lease_expires_at="2000-01-01T00:00:00")
    assert jobs.requeue_stale_running(db_path=db_path) == 1
    refreshed = jobs.get_target(target.id, db_path=db_path)
    assert refreshed.status == expected
    assert refreshed.lease_token is None and refreshed.checkpoint == checkpoint
    assert jobs.get_job(job.id, db_path=db_path).status == ("running" if expected == "retrying" else expected)
    if expected == "needs_review":
        assert not jobs.has_claimable_targets(db_path=db_path)


def test_corrupt_checkpoint_never_requeues_blind(db_path):
    _enqueue(db_path)
    target = _claim(db_path)[0]
    _update(db_path, target.id, checkpoint_json="broken", lease_expires_at="2000-01-01T00:00:00")
    assert jobs.requeue_stale_running(db_path=db_path) == 1
    assert jobs.get_target(target.id, db_path=db_path).status == "needs_review"


def test_checkpoint_redaction_sticky_evidence_and_size_bound(db_path):
    _enqueue(db_path)
    target = _claim(db_path)[0]
    assert jobs.save_target_checkpoint(target.id, {
        "submission_started": True, "accessToken": "secret-a", "api_key": "secret-b",
        "config": {"userId": "private-config"}, "cookie": "secret-c",
        "provider_receipt": {"publish_id": "p1", "authorization": "secret-d"},
        "steps": [{"refresh_token": "secret-e", "done": True}],
    }, lease_token=target.lease_token, db_path=db_path)
    assert jobs.save_target_checkpoint(target.id, {"submission_started": False, "provider_receipt": None},
                                       lease_token=target.lease_token, db_path=db_path)
    saved = jobs.get_target(target.id, db_path=db_path)
    assert saved.checkpoint == {"submission_started": True, "provider_receipt": {"publish_id": "p1"},
                                "steps": [{"done": True}]}
    with closing(sqlite3.connect(db_path)) as conn:
        assert "secret-" not in conn.execute("SELECT checkpoint_json FROM publish_job_targets").fetchone()[0]
    assert "lease_token" not in saved.to_dict()
    with pytest.raises(ValueError):
        jobs.save_target_checkpoint(target.id, {"large": "x" * jobs.CHECKPOINT_MAX_BYTES},
                                    lease_token=target.lease_token, db_path=db_path)
    assert jobs.get_target(target.id, db_path=db_path).checkpoint == saved.checkpoint


def test_awaiting_delivery_polls_are_due_not_resubmissions(db_path):
    job = _enqueue(db_path)
    first = _claim(db_path)[0]
    receipt = {"submission_started": True, "provider_receipt": {"publish_id": "p1"}}
    assert jobs.mark_target_awaiting_delivery(first.id, receipt, lease_token=first.lease_token,
                                             delay_seconds=3600, db_path=db_path)
    waiting = jobs.get_target(first.id, db_path=db_path)
    assert waiting.status == "awaiting_delivery" and waiting.lease_token is None
    assert jobs.get_job(job.id, db_path=db_path).status == "awaiting_delivery"
    assert not jobs.has_claimable_targets(db_path=db_path)
    _update(db_path, first.id, next_attempt_at="2000-01-01T00:00:00", schedule_at="2100-01-01T00:00:00")
    poll = _claim(db_path)[0]
    assert poll.status == "running" and poll.delivery_state == "awaiting_delivery"
    assert poll.attempts == first.attempts and poll.checkpoint == receipt
    jobs.mark_target_success(poll.id, lease_token=poll.lease_token, db_path=db_path)
    assert jobs.get_target(poll.id, db_path=db_path).delivery_state == "delivered"


@pytest.mark.parametrize("transition,expected", [(jobs.mark_target_blocked, "blocked"),
                                                 (jobs.mark_target_needs_review, "needs_review")])
def test_attention_states_are_not_claimable_or_finished(db_path, transition, expected):
    job = _enqueue(db_path)
    target = _claim(db_path)[0]
    assert transition(target.id, "attention", lease_token=target.lease_token, db_path=db_path)
    assert jobs.get_target(target.id, db_path=db_path).status == expected
    assert jobs.get_job(job.id, db_path=db_path).status == expected
    assert jobs.get_job(job.id, db_path=db_path).finished_at is None
    assert not jobs.has_claimable_targets(db_path=db_path)


@pytest.mark.parametrize("state", ["awaiting_delivery", "needs_review"])
def test_ambiguous_states_require_new_deliberate_intent(db_path, state):
    job = _enqueue(db_path)
    target = jobs.list_targets(job.id, db_path=db_path)[0]
    _update(db_path, target.id, status=state, checkpoint_json='{"submission_started":true}')
    with pytest.raises(ValueError):
        jobs.resubmit_target(target.id, db_path=db_path)
    with pytest.raises(ValueError):
        jobs.reschedule_target(target.id, "2100-01-01T00:00:00Z", db_path=db_path)
    assert jobs.get_target(target.id, db_path=db_path).status == state


def test_cancel_intent_and_counts_survive_late_writes_and_recounts(db_path):
    job = _enqueue(db_path, ("a", "b"))
    first, second = _claim(db_path, limit=2)
    jobs.mark_target_failed(first.id, "failure", lease_token=first.lease_token, db_path=db_path)
    cancelled = jobs.cancel_job(job.id, db_path=db_path)
    assert cancelled.status == "cancelled" and cancelled.failed_targets == 1
    assert not jobs.mark_target_success(second.id, lease_token=second.lease_token, db_path=db_path)
    assert jobs.recount_job(job.id, db_path=db_path).status == "cancelled"
    jobs.resubmit_target(second.id, db_path=db_path)  # existing explicit retry semantics
    assert jobs.get_job(job.id, db_path=db_path).status == "pending"
    assert jobs.get_job(job.id, db_path=db_path).finished_at is None


def test_recount_uses_actual_rows_and_empty_job_needs_review(db_path):
    job = _enqueue(db_path, ("a", "b"))
    first, second = jobs.list_targets(job.id, db_path=db_path)
    _update(db_path, first.id, status="succeeded")
    _update(db_path, second.id, status="cancelled")
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("UPDATE publish_jobs SET total_targets = 99, completed_targets = 77 WHERE id = ?", (job.id,))
    recounted = jobs.recount_job(job.id, db_path=db_path)
    assert (recounted.status, recounted.total_targets, recounted.completed_targets) == ("cancelled", 2, 1)
    empty = _enqueue(db_path, key="empty")
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("DELETE FROM publish_job_targets WHERE job_id = ?", (empty.id,))
    recounted = jobs.recount_job(empty.id, db_path=db_path)
    assert recounted.status == "needs_review" and recounted.total_targets == 0


@pytest.mark.parametrize("invalid", ["", "junk", "2100-01-01", "2100-02-30T00:00:00", "2100-01-01T00:00:00+01:90",
                                      "2000-01-01T00:00:00Z", "2100-01-01X00:00:00", None])
def test_reschedule_strict_validation_rolls_back(db_path, invalid):
    job = _enqueue(db_path)
    before = jobs.list_targets(job.id, db_path=db_path)[0]
    with pytest.raises(ValueError):
        jobs.reschedule_target(before.id, invalid, db_path=db_path)
    assert jobs.get_target(before.id, db_path=db_path) == before


def test_reschedule_normalizes_offsets_and_invalid_resubmit_is_rejected(db_path):
    job = _enqueue(db_path)
    target = jobs.list_targets(job.id, db_path=db_path)[0]
    result = jobs.reschedule_target(target.id, "2100-01-01T12:00:00+08:00", db_path=db_path)
    assert result.schedule_at == "2100-01-01T04:00:00"
    _update(db_path, target.id, status="failed", schedule_at="garbage")
    with pytest.raises(ValueError):
        jobs.resubmit_target(target.id, db_path=db_path)
    assert jobs.get_target(target.id, db_path=db_path).schedule_at == "garbage"


@pytest.mark.parametrize("operation", ["cancel", "reschedule", "resubmit", "success", "retry", "cancel_job"])
def test_mutation_and_recount_roll_back_as_one_transaction(db_path, operation):
    job = _enqueue(db_path)
    target = jobs.list_targets(job.id, db_path=db_path)[0]
    if operation in {"success", "retry"}:
        target = _claim(db_path)[0]
    if operation == "resubmit":
        _update(db_path, target.id, status="failed")
        target = jobs.get_target(target.id, db_path=db_path)
    calls = {
        "cancel": lambda: jobs.cancel_target(target.id, db_path=db_path),
        "reschedule": lambda: jobs.reschedule_target(target.id, "2100-01-01T00:00:00Z", db_path=db_path),
        "resubmit": lambda: jobs.resubmit_target(target.id, db_path=db_path),
        "success": lambda: jobs.mark_target_success(target.id, lease_token=target.lease_token, db_path=db_path),
        "retry": lambda: jobs.mark_target_retry(target.id, "again", lease_token=target.lease_token, db_path=db_path),
        "cancel_job": lambda: jobs.cancel_job(job.id, db_path=db_path),
    }
    before = jobs.get_job(job.id, db_path=db_path)
    with patch.object(jobs, "_recount_job", side_effect=RuntimeError("injected")), pytest.raises(RuntimeError):
        calls[operation]()
    assert jobs.get_target(target.id, db_path=db_path) == target
    assert jobs.get_job(job.id, db_path=db_path) == before


def test_enqueue_race_and_immutable_explicit_intent(db_path):
    spec = jobs.JobSpec("douyin", {"title": "one"}, [("a", "f", None)], idempotency_key="same")
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: jobs.enqueue_job(spec, db_path=db_path).id, range(8)))
    assert len(set(ids)) == 1
    jobs.update_job_payload(ids[0], {"title": "worker update"}, db_path=db_path)
    target = jobs.list_targets(ids[0], db_path=db_path)[0]
    jobs.reschedule_target(target.id, "2100-01-01T00:00:00Z", db_path=db_path)
    assert jobs.enqueue_job(spec, db_path=db_path).id == ids[0]
    for changed in (replace(spec, payload={"title": "different"}),
                    replace(spec, targets=[("other", "f", None)]), replace(spec, extra_canonical=("new",))):
        with pytest.raises(ValueError, match="different publish intent"):
            jobs.enqueue_job(changed, db_path=db_path)


def test_auto_identity_and_explicit_conflicts_are_tenant_scoped(db_path):
    _account(db_path, 101, profile_id=100)
    _account(db_path, 102, profile_id=101)
    spec = jobs.JobSpec("douyin", {}, [("a", "f", None)], profile_id=100)
    first = jobs.enqueue_job(spec, workspace_id="workspace-a", db_path=db_path)
    second = jobs.enqueue_job(spec, workspace_id="workspace-b", db_path=db_path)
    third = jobs.enqueue_job(replace(spec, profile_id=101), workspace_id="workspace-a", db_path=db_path)
    assert len({first.id, second.id, third.id}) == 3
    explicit = replace(spec, idempotency_key="explicit")
    jobs.enqueue_job(explicit, workspace_id="workspace-a", db_path=db_path)
    with pytest.raises(ValueError):
        jobs.enqueue_job(explicit, workspace_id="workspace-b", db_path=db_path)
    with pytest.raises(ValueError):
        jobs.enqueue_job(replace(explicit, profile_id=101), workspace_id="workspace-a", db_path=db_path)


def test_worker_health_reports_only_safe_liveness_fields(db_path):
    assert jobs.worker_health(db_path=db_path) == {"ok": False, "workers": []}
    jobs.heartbeat_worker("publisher-a", db_path=db_path)
    health = jobs.worker_health(db_path=db_path)
    assert health["ok"] and health["workers"][0]["alive"]
    assert set(health["workers"][0]) == {"worker_id", "heartbeat_at", "role", "draining", "alive", "age_seconds"}
    jobs.heartbeat_worker("publisher-a", draining=True, db_path=db_path)
    jobs.heartbeat_worker("prep-a", role="prep", db_path=db_path)
    assert not jobs.worker_health(db_path=db_path)["ok"]
    jobs.heartbeat_worker("publisher-a", db_path=db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("UPDATE worker_heartbeats SET heartbeat_at = '2000-01-01T00:00:00' WHERE worker_id = 'publisher-a'")
    assert not jobs.worker_health(db_path=db_path)["ok"]


def test_legacy_explicit_intent_without_hash_is_compared_before_reuse(db_path):
    spec = jobs.JobSpec("douyin", {"title": "original"}, [("a", "f", None)], idempotency_key="legacy")
    job = jobs.enqueue_job(spec, db_path=db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("UPDATE publish_jobs SET intent_hash = NULL WHERE id = ?", (job.id,))
    with pytest.raises(ValueError):
        jobs.enqueue_job(replace(spec, payload={"title": "other"}), db_path=db_path)
    assert jobs.enqueue_job(spec, db_path=db_path).id == job.id


@pytest.mark.parametrize("seconds", [-1, float("inf"), float("nan")])
def test_invalid_lease_or_delay_does_not_mutate_target(db_path, seconds):
    job = _enqueue(db_path)
    with pytest.raises(ValueError):
        _claim(db_path, lease_seconds=seconds)
    target = _claim(db_path)[0]
    with pytest.raises(ValueError):
        jobs.mark_target_retry(target.id, "invalid delay", delay_seconds=seconds,
                               lease_token=target.lease_token, db_path=db_path)
    assert jobs.get_target(target.id, db_path=db_path) == target
    assert jobs.get_job(job.id, db_path=db_path).status == "running"


def _managed_tiktok_config(*, workspace="26985", profile_ref="3917"):
    return {"publishingProvider": "sociamonials", "sociamonials": {
        "workspace_id": workspace, "network": "tiktok", "profile_refs": [profile_ref],
    }}


def test_native_backup_and_managed_alias_share_one_lease_but_teaching_proceeds(db_path):
    native = _account(db_path, 109, platform="tiktok", config={"openId": "fixture-native"})
    managed = _account(db_path, 451, platform="tiktok", profile_id=101, config=_managed_tiktok_config())
    teaching = _account(db_path, 100, platform="tiktok", profile_id=102, config={"open_id": "fixture-teaching"})
    _enqueue(db_path, (native, managed, teaching), platform="tiktok")
    first = _claim(db_path, limit=10)
    assert [target.account_ref for target in first] == [native, teaching]
    assert jobs.claim_next_targets(owner="other-process", limit=10, db_path=db_path) == []
    assert not jobs.has_claimable_targets(db_path=db_path)
    assert jobs.mark_target_success(first[0].id, lease_token=first[0].lease_token, db_path=db_path)
    assert _claim(db_path)[0].account_ref == managed


def test_new_managed_alias_excludes_native_before_alias_has_target_history(db_path):
    native = _account(db_path, 109, platform="tiktok", config={"open_id": "fixture-native"})
    alias = _account(db_path, 451, platform="tiktok", profile_id=101, config=_managed_tiktok_config())
    unrelated = _account(db_path, 452, platform="tiktok", config=_managed_tiktok_config(workspace="34293"))
    _enqueue(db_path, (native, unrelated), platform="tiktok")
    with patch.object(jobs, "canonical_account_keys", wraps=jobs.canonical_account_keys) as build:
        claimed = _claim(db_path, limit=10, excluded_accounts=[alias])
    assert [target.account_ref for target in claimed] == [unrelated]
    assert build.call_count == 1


def test_two_processes_cannot_claim_native_and_managed_destination_simultaneously(db_path):
    native = _account(db_path, 109, platform="tiktok", config={"openId": "fixture-native"})
    alias = _account(db_path, 451, platform="tiktok", profile_id=101, config=_managed_tiktok_config())
    _enqueue(db_path, (native, alias), platform="tiktok")
    code = ("import sys; from pathlib import Path; from myUtils.jobs import claim_next_targets; "
            "print(len(claim_next_targets(owner=sys.argv[2], limit=5, db_path=Path(sys.argv[1]))))")
    processes = [subprocess.Popen([sys.executable, "-c", code, str(db_path), f"managed-worker-{i}"],
                                  cwd=Path(__file__).resolve().parents[1],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for i in range(2)]
    try:
        outputs = [process.communicate(timeout=30) for process in processes]
        assert all(process.returncode == 0 for process in processes), outputs
        assert sorted(int(stdout.strip()) for stdout, _ in outputs) == [0, 1]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
