"""Synthetic, offline failed-recovery contracts; never use the operator database."""
from __future__ import annotations

import gc
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from db import createTable
from myUtils import jobs
from scripts import recover_failed_targets as recovery

NOW = datetime(2035, 1, 2, 12)
SECRET = "synthetic-token-never-print-this"


@pytest.fixture(scope="module")
def schema(tmp_path_factory):
    path = tmp_path_factory.mktemp("failed-recovery-schema") / "schema.db"
    createTable.bootstrap(path)
    gc.collect()  # The legacy bootstrap leaves some SQLite context handles open.
    return path


@pytest.fixture(autouse=True)
def isolated_policy(monkeypatch):
    for platform in recovery.profiles.SUPPORTED_PLATFORMS:
        monkeypatch.delenv(f"SAU_{platform.upper()}_ENABLED", raising=False)
    for name in ("SAU_X_DIRECT_PUBLISH", "SAU_REDDIT_DIRECT_PUBLISH", "SAU_SOCIAMONIALS_FALLBACK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(jobs, "_now_iso", lambda: NOW.isoformat())
    monkeypatch.setattr(recovery.publish_orchestrator, "MIN_GAP_MINUTES", 30)
    monkeypatch.setattr(recovery.publish_orchestrator, "STAGGER_MINUTES", 5)
    monkeypatch.setattr(recovery.publish_orchestrator, "MAX_POSTS_PER_ACCOUNT_PER_DAY", 12)


@pytest.fixture
def db_path(tmp_path, schema):
    path = tmp_path / "synthetic # 測試.db"  # URI escaping must work on Windows too.
    shutil.copyfile(schema, path)
    _account(path)
    return path


def _sql(path, statement, params=()):
    with closing(sqlite3.connect(path)) as conn, conn:
        return conn.execute(statement, params).lastrowid


def _row(path, table, row_id):
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
        return dict(row) if row is not None else None


def _update(path, table, row_id, **fields):
    _sql(path, f"UPDATE {table} SET " + ", ".join(f"{key}=?" for key in fields) + " WHERE id=?",
         (*fields.values(), row_id))


def _dump(path):
    with closing(sqlite3.connect(path)) as conn:
        return "\n".join(conn.iterdump())


def _account(path, account_id=101, *, profile_id=100, platform="threads", workspace=None,
             config=None, cookie=""):
    _sql(path, "INSERT OR IGNORE INTO profiles(id, name, slug, workspace_id) VALUES (?, ?, ?, ?)",
         (profile_id, f"fixture-{profile_id}", f"fixture-{profile_id}", workspace))
    if config is None:
        config = {"threadUserId": f"identity-{account_id}", "accessToken": SECRET,
                  "tokenExpiresAt": "2036-01-01T00:00:00"}
    _sql(path, "INSERT INTO accounts(id, profile_id, platform, account_name, cookie_path, "
               "config_json, auth_type, enabled, workspace_id) VALUES (?, ?, ?, ?, ?, ?, 'oauth', 1, ?)",
         (account_id, profile_id, platform, f"account-{account_id}", cookie, json.dumps(config), workspace))
    return f"account:{account_id}"


def _job(path, *, file_ref="/media/original.mp4", account_ref="account:101", platform="threads",
         profile_id=100, workspace=None, payload=None, status="failed", error="HTTP 503 temporary outage",
         targets=None, finished=None):
    with closing(sqlite3.connect(path)) as conn:
        sequence = conn.execute("SELECT COUNT(*) FROM publish_jobs").fetchone()[0]
    target_specs = targets or [(account_ref, file_ref, "2034-12-01T08:00:00")]
    job = jobs.enqueue_job(jobs.JobSpec(
        platform=platform, profile_id=profile_id, idempotency_key=f"fixture-{sequence}",
        payload=payload if payload is not None else {"message": "Synthetic post"}, targets=target_specs,
    ), workspace_id=workspace, db_path=path)
    ids = [target.id for target in jobs.list_targets(job.id, db_path=path)]
    for target_id in ids:
        _update(path, "publish_job_targets", target_id, status=status, attempts=3, last_error=error,
                started_at=(NOW - timedelta(hours=2)).isoformat(),
                finished_at=finished or (NOW - timedelta(hours=1)).isoformat(),
                error_category="transient", delivery_state="failed" if status == "failed" else None)
    jobs.recount_job(job.id, db_path=path)
    return job.id, ids


def _target(path, **kwargs):
    return _job(path, **kwargs)[1][0]


def _plan(path, **kwargs):
    return recovery.plan_recovery(db_path=path, now=NOW, **kwargs)


def _entries(plan):
    return [item for item in plan if "_skipped" not in item]


def _assert_refused_unchanged(path, plan):
    before = _dump(path)
    result = recovery.apply_recovery(plan, db_path=path)
    assert result["applied"] == 0 and result["errors"]
    assert _dump(path) == before
    return result


def _campaign(path, target_id):
    target = _row(path, "publish_job_targets", target_id)
    group_id = _sql(path, "INSERT INTO media_groups(name) VALUES ('synthetic group')")
    campaign_id = _sql(path, "INSERT INTO campaigns(profile_id, media_group_id, status) VALUES (100, ?, 'failed')",
                       (group_id,))
    post_id = _sql(path, "INSERT INTO campaign_posts(campaign_id, platform, status, last_published_job_id) "
                        "VALUES (?, 'threads', 'failed', ?)", (campaign_id, target["job_id"]))
    _update(path, "publish_job_targets", target_id, file_ref=f"campaign_post:{post_id}")
    _update(path, "publish_jobs", target["job_id"],
            payload_json=json.dumps({"campaignId": campaign_id, "campaignPostId": post_id, "message": "Fixture"}))
    return campaign_id, post_id


def test_missing_database_and_parent_are_never_created(tmp_path):
    path = tmp_path / "not-created" / "missing.db"
    with pytest.raises(FileNotFoundError, match="existing regular file"):
        _plan(path)
    with pytest.raises(FileNotFoundError):
        jobs.reschedule_failed_target(1, "2035-02-01T00:00:00", expected_fingerprint="0" * 64,
                                      revalidate=lambda conn: "0" * 64, db_path=path)
    assert not path.parent.exists()


def test_missing_database_between_plan_and_apply_is_not_created(db_path):
    _target(db_path)
    plan = _plan(db_path)
    db_path.unlink()
    assert recovery.apply_recovery(plan, db_path=db_path)["applied"] == 0
    assert not db_path.exists()


def test_incompatible_database_fails_without_schema_changes(tmp_path):
    path = tmp_path / "old.db"
    _sql(path, "CREATE TABLE unrelated(id INTEGER)")
    before = path.read_bytes()
    with pytest.raises(sqlite3.Error):
        _plan(path)
    assert path.read_bytes() == before


def test_planning_is_read_only_deterministic_and_never_emits_secrets(db_path, monkeypatch):
    _target(db_path, error=f"HTTP 503 Bearer {SECRET}; accessToken={SECRET}")
    monkeypatch.setattr(recovery.publish_orchestrator, "_load_booked_slots",
                        lambda *_: pytest.fail("must not open the writable booking helper"))
    monkeypatch.setattr(recovery.profiles, "get_account", lambda *a, **k: pytest.fail("must not decrypt config"))
    before = db_path.read_bytes()
    files = set(db_path.parent.iterdir())
    first = _plan(db_path)
    assert first == _plan(db_path)
    assert len(_entries(first)) == 1
    assert len(first[0]["expected_fingerprint"]) == 64
    assert SECRET not in json.dumps(first)
    assert "accessToken" not in json.dumps(first)
    assert db_path.read_bytes() == before and set(db_path.parent.iterdir()) == files
    with recovery._connect(db_path) as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE accounts SET enabled=0")


def test_eligible_recovery_only_resets_selected_failed_target_and_recounts(db_path):
    job_id, ids = _job(db_path, targets=[
        ("account:101", "/a.mp4", None), ("account:101", "/b.mp4", "2036-01-01T00:00:00"),
        ("account:101", "/c.mp4", None),
    ])
    _update(db_path, "publish_job_targets", ids[1], status="succeeded",
            checkpoint_json='{"provider_receipt":{"id":"already-delivered"}}', delivery_state="delivered")
    _update(db_path, "publish_job_targets", ids[2], status="cancelled")
    jobs.recount_job(job_id, db_path=db_path)
    siblings = [_row(db_path, "publish_job_targets", row_id) for row_id in ids[1:]]
    account = _row(db_path, "accounts", 101)
    prior_job = _row(db_path, "publish_jobs", job_id)
    plan = _plan(db_path, target_ids=[ids[0]])
    assert recovery.apply_recovery(plan, db_path=db_path) == {"applied": 1, "errors": []}
    target = jobs.get_target(ids[0], db_path=db_path)
    assert (target.status, target.attempts, target.last_error) == ("pending", 0, None)
    assert target.checkpoint == {"recovery_generation": plan[0]["expected_fingerprint"]}
    assert target.schedule_at == plan[0]["slot"]
    assert target.started_at is None and target.finished_at is None and target.next_attempt_at is None
    assert [_row(db_path, "publish_job_targets", row_id) for row_id in ids[1:]] == siblings
    assert _row(db_path, "accounts", 101) == account
    parent = _row(db_path, "publish_jobs", job_id)
    assert (parent["status"], parent["completed_targets"], parent["failed_targets"]) == ("pending", 1, 0)
    for key in ("intent_hash", "payload_json", "idempotency_key", "created_at"):
        assert parent[key] == prior_job[key]


def test_multiple_failed_siblings_apply_despite_derived_recounts(db_path):
    job_id, ids = _job(db_path, targets=[("account:101", "/a.mp4", None), ("account:101", "/b.mp4", None)])
    plan = _plan(db_path)
    assert len(_entries(plan)) == 2
    assert recovery.apply_recovery(plan, db_path=db_path) == {"applied": 2, "errors": []}
    assert all(jobs.get_target(target_id, db_path=db_path).status == "pending" for target_id in ids)
    assert jobs.get_job(job_id, db_path=db_path).failed_targets == 0


@pytest.mark.parametrize("field,value", [
    ("enabled", 0), ("enabled", 2), ("platform", "instagram"), ("profile_id", 999),
    ("workspace_id", "other-workspace"), ("config_json", '{"_needsReconnect":true}'),
    ("config_json", "[]"), ("config_json", "broken"),
    ("config_json", '{"_needsReconnect":true,"_needsReconnect":false}'),
])
@pytest.mark.parametrize("resolved", [False, True])
def test_invalid_accounts_refuse_planning_and_applying(db_path, field, value, resolved):
    target = _target(db_path, error="forbidden" if resolved else "HTTP 503")
    selection = {"resolved_target_ids" if resolved else "target_ids": [target]}
    plan = _plan(db_path, **selection)
    _update(db_path, "accounts", 101, **{field: value})
    assert not _entries(_plan(db_path, **selection))
    _assert_refused_unchanged(db_path, plan)


@pytest.mark.parametrize("table", ["accounts", "profiles", "publish_jobs"])
def test_missing_account_profile_or_parent_refuses(db_path, table):
    job_id, ids = _job(db_path)
    plan = _plan(db_path)
    _sql(db_path, f"DELETE FROM {table} WHERE id=?", ({"accounts": 101, "profiles": 100, "publish_jobs": job_id}[table],))
    assert not _entries(_plan(db_path, target_ids=ids))
    _assert_refused_unchanged(db_path, plan)


@pytest.mark.parametrize("ref", ["cookies/old.json", "cookie:101", "account:-1", "account:0",
                                  "account:999", "account:101:extra", "account:9223372036854775808"])
def test_unresolved_account_refs_are_not_guessed(db_path, ref):
    target = _target(db_path, account_ref=ref)
    assert not _entries(_plan(db_path, resolved_target_ids=[target]))


@pytest.mark.parametrize("table,field,value", [
    ("publish_job_targets", "workspace_id", "other"), ("publish_jobs", "workspace_id", "other"),
    ("profiles", "workspace_id", "other"), ("publish_jobs", "profile_id", None),
])
def test_ownership_mismatches_refuse(db_path, table, field, value):
    job_id, ids = _job(db_path)
    row_id = {"publish_job_targets": ids[0], "publish_jobs": job_id, "profiles": 100}[table]
    _update(db_path, table, row_id, **{field: value})
    assert not _entries(_plan(db_path))


def test_matching_concrete_workspaces_are_eligible(db_path):
    _update(db_path, "profiles", 100, workspace_id="workspace-a")
    _update(db_path, "accounts", 101, workspace_id="workspace-a")
    _target(db_path, workspace="workspace-a")
    assert recovery.apply_recovery(_plan(db_path), db_path=db_path)["applied"] == 1


EVIDENCE = [
    '{"provider_receipt":{"provider":"threads","id":"receipt","status":"failed"}}',
    '{"provider_receipt":{}}', '{"provider_receipt":null}', '{"submission_started":true}',
    '{"submission_started":1}', '{"submission_started":"false"}', '{"checkpoint_invalid":true}',
    "not-json", "[]", "null", "", '{"x":NaN}',
    '{"submission_started":true,"submission_started":false}',
    '{"provider_operations":{"threads":{"items":{"container":{"status":"started"}}}}}',
    '{"provider_operations":{"threads":{"items":{"container":{"status":"complete","id":"abc"}}}}}',
    '{"provider_operations":{"threads":{"items":{"container":{"status":"completed"}}}}}',
    '{"provider_operations":[]}', '{"operation_started_at":"2034-01-01"}',
    '{"payload":{"telegramCompletedByDelivery":{"one":["message-id"]}}}',
    '{"_delivery":"broken"}', '{"number":1e999}',
]


@pytest.mark.parametrize("checkpoint", EVIDENCE)
def test_all_receipt_and_operation_evidence_is_preserved_even_when_failed(db_path, checkpoint):
    target = _target(db_path)
    plan = _plan(db_path)
    _update(db_path, "publish_job_targets", target, checkpoint_json=checkpoint)
    assert not _entries(_plan(db_path, resolved_target_ids=[target]))
    _assert_refused_unchanged(db_path, plan)
    with pytest.raises(ValueError, match="Submission evidence"):
        jobs.reschedule_failed_target(target, plan[0]["slot"], db_path=db_path,
                                      expected_fingerprint=plan[0]["expected_fingerprint"],
                                      revalidate=lambda conn: pytest.fail("evidence has no override"))


@pytest.mark.parametrize("checkpoint", [{}, {"submission_started": False}, {"prepared": True},
    {"provider_operations": {"threads": {"plan": ["container"], "items": {}}}},
])
def test_pre_submission_checkpoints_are_eligible(db_path, checkpoint):
    target = _target(db_path)
    _update(db_path, "publish_job_targets", target, checkpoint_json=json.dumps(checkpoint))
    assert recovery.apply_recovery(_plan(db_path), db_path=db_path)["applied"] == 1


@pytest.mark.parametrize("state", ["submission_started", "awaiting_delivery", "delivered", "unknown", "invalid"])
def test_delivery_state_alone_is_a_guard(db_path, state):
    target = _target(db_path)
    _update(db_path, "publish_job_targets", target, delivery_state=state)
    assert not _entries(_plan(db_path))


@pytest.mark.parametrize("payload", [
    {"_delivery": {"provider_receipt": {"id": "receipt"}}},
    {"_delivery": {"submission_started": True}}, {"_delivery": "broken"},
    {"telegramCompletedByDelivery": {"delivery": ["message-id"]}},
])
def test_legacy_payload_delivery_evidence_is_not_lost(db_path, payload):
    target = _target(db_path, payload=payload)
    assert not _entries(_plan(db_path, resolved_target_ids=[target]))
    before = _dump(db_path)
    with pytest.raises(ValueError, match="Submission evidence"):
        jobs.reschedule_failed_target(target, "2035-02-01T00:00:00", db_path=db_path,
                                      expected_fingerprint="0" * 64,
                                      revalidate=lambda conn: pytest.fail("payload evidence has no override"))
    assert _dump(db_path) == before


@pytest.mark.parametrize("state", ["cancelled", "succeeded", "invalid"])
def test_terminal_or_invalid_parent_intent_cannot_be_reopened(db_path, state):
    job_id, ids = _job(db_path, error="forbidden")
    plan = _plan(db_path, resolved_target_ids=ids)
    _update(db_path, "publish_jobs", job_id, status=state)
    assert not _entries(_plan(db_path, resolved_target_ids=ids))
    _assert_refused_unchanged(db_path, plan)


@pytest.mark.parametrize("platform,flag,value", [
    ("reddit", "SAU_REDDIT_ENABLED", "1"), ("reddit", "SAU_REDDIT_ENABLED", "0"),
    ("threads", "SAU_THREADS_ENABLED", "0"), ("twitter", "SAU_X_DIRECT_PUBLISH", "off"),
    ("threads", "SAU_THREADS_ENABLED", "malformed"),
])
def test_resolved_selection_never_overrides_retired_or_disabled_routes(db_path, monkeypatch, platform, flag, value):
    _update(db_path, "accounts", 101, platform=platform)
    target = _target(db_path, platform=platform, error="forbidden")
    monkeypatch.setenv(flag, value)
    before = _dump(db_path)
    assert not _entries(_plan(db_path, resolved_target_ids=[target]))
    assert _dump(db_path) == before


def test_route_disabled_after_planning_refuses_apply(db_path, monkeypatch):
    _target(db_path)
    plan = _plan(db_path)
    monkeypatch.setenv("SAU_THREADS_ENABLED", "0")
    _assert_refused_unchanged(db_path, plan)


@pytest.mark.parametrize("other_status,checkpoint", [
    ("succeeded", {}), ("pending", {}), ("running", {}), ("retrying", {}),
    ("awaiting_delivery", {}), ("needs_review", {}),
    ("failed", {"provider_receipt": {"id": "accepted"}}),
    ("cancelled", {"submission_started": True}),
    ("failed", {"provider_operations": {"threads": {"items": {"post": {"status": "complete"}}}}}),
])
def test_exact_duplicate_success_or_unresolved_delivery_is_protected(db_path, other_status, checkpoint):
    target = _target(db_path, error="forbidden")
    other = _target(db_path, status=other_status)
    _update(db_path, "publish_job_targets", other, checkpoint_json=json.dumps(checkpoint))
    before = _dump(db_path)
    assert not _entries(_plan(db_path, resolved_target_ids=[target]))
    assert _dump(db_path) == before


@pytest.mark.parametrize("old_ref,new_ref", [
    ("/one/clip.mp4", "/two/clip.mp4"), ("/one/clip_pub.mp4", "/one/clip.mp4"),
    ("/one/clip_part1.mp4", "/one/clip_part2.mp4"),
    ("/one/" + "a" * 32 + "_clip.mp4", "/one/" + "b" * 32 + "_clip.mp4"),
])
def test_unrelated_paths_and_parts_are_never_equated_by_stem(db_path, old_ref, new_ref):
    _target(db_path, file_ref=old_ref, payload={"artifacts": [{"local_path": old_ref}]}, status="succeeded")
    target = _target(db_path, file_ref=new_ref, payload={"artifacts": [{"local_path": new_ref}]})
    assert [entry["target_id"] for entry in _entries(_plan(db_path))] == [target]


@pytest.mark.parametrize("old,new,duplicate", [
    ([{"sha256": "a" * 64}], [{"metadata": {"sha256": "A" * 64}}], True),
    ([{"id": 700}], [{"id": 700, "local_path": "/renamed.mp4"}], True),
    ([{"id": 1}, {"id": 2}], [{"id": 2}, {"id": 1}], True),
    ([{"id": 1}, {"id": 2}], [{"id": 1}, {"id": 3}], False),
    ([{"sha256": "a" * 64}], [{"sha256": "b" * 64}], False),
    ([{"sha256": "weak"}], [{"sha256": "weak"}], False),
])
def test_only_complete_strong_artifact_sets_deduplicate(db_path, old, new, duplicate):
    _target(db_path, file_ref="/old.mp4", payload={"artifacts": old}, status="succeeded")
    _target(db_path, file_ref="/new.mp4", payload={"artifacts": new})
    assert bool(_entries(_plan(db_path))) is not duplicate


def test_job_wide_artifacts_are_not_assigned_to_ambiguous_multiple_file_refs(db_path):
    payload = {"artifacts": [{"id": 500}]}
    _target(db_path, file_ref="/already.mp4", payload=payload, status="succeeded")
    _job(db_path, payload=payload, targets=[("account:101", "/a.mp4", None), ("account:101", "/b.mp4", None)])
    assert len(_entries(_plan(db_path))) == 2


@pytest.mark.parametrize("workspace", [None, "another-workspace"])
def test_canonical_provider_identity_deduplicates_account_aliases(db_path, workspace):
    alias = _account(db_path, 102, profile_id=200, workspace=workspace,
                     config={"userId": "identity-101", "accessToken": "different-synthetic-token"})
    _target(db_path, account_ref=alias, profile_id=200, workspace=workspace, status="succeeded")
    _update(db_path, "accounts", 102, enabled=0)  # Historical deliveries still count.
    _target(db_path)
    assert not _entries(_plan(db_path))


def test_canonical_cookie_identity_and_different_provider_identity(db_path):
    _update(db_path, "accounts", 101, config_json="{}", cookie_path=r"C:\cookies\same.json")
    alias = _account(db_path, 102, config={}, cookie="c:/cookies/same.json")
    _target(db_path, account_ref=alias, status="succeeded")
    _target(db_path)
    assert not _entries(_plan(db_path))
    _update(db_path, "accounts", 102, cookie_path="c:/cookies/different.json")
    assert len(_entries(_plan(db_path))) == 1


def test_duplicate_clean_failures_are_planned_only_once(db_path):
    first = _target(db_path)
    _target(db_path)
    plan = _plan(db_path)
    assert [entry["target_id"] for entry in _entries(plan)] == [first]
    assert plan[-1]["_skipped"] == {"duplicate within recovery plan": 1}
    assert recovery.apply_recovery(plan, db_path=db_path)["applied"] == 1


@pytest.mark.parametrize("table,field,value", [
    ("publish_job_targets", "status", "pending"), ("publish_job_targets", "attempts", 4),
    ("publish_job_targets", "last_error", "new failure"),
    ("publish_job_targets", "schedule_at", "2036-01-01T00:00:00"),
    ("publish_job_targets", "checkpoint_json", '{"prepared":true}'),
    ("publish_job_targets", "file_ref", "/changed.mp4"),
    ("publish_job_targets", "lease_token", "synthetic-new-lease"),
    ("publish_jobs", "payload_json", '{"message":"changed intent"}'),
    ("publish_jobs", "intent_hash", "changed-intent"),
    ("profiles", "settings_json", '{"language":"zh"}'),
    ("accounts", "account_name", "renamed"), ("accounts", "last_checked_at", "2035-01-02"),
    ("accounts", "config_json", '{"threadUserId":"identity-101","accessToken":"rotated"}'),
    ("accounts", "config_json", '{"threadUserId":"identity-101","tokenExpiresAt":"2037-01-01"}'),
    ("accounts", "config_json", '{"threadUserId":"identity-101","accessToken":"sau:enc:v1:new-envelope"}'),
])
def test_stale_target_job_profile_account_and_token_metadata_refuse(db_path, table, field, value):
    job_id, ids = _job(db_path)
    plan = _plan(db_path)
    row_id = {"publish_job_targets": ids[0], "publish_jobs": job_id, "profiles": 100, "accounts": 101}[table]
    _update(db_path, table, row_id, **{field: value})
    _assert_refused_unchanged(db_path, plan)


@pytest.mark.parametrize("status", ["succeeded", "awaiting_delivery"])
def test_duplicate_created_after_plan_is_rechecked_under_apply(db_path, status):
    _target(db_path)
    plan = _plan(db_path)
    _target(db_path, status=status)
    _assert_refused_unchanged(db_path, plan)


def test_canonical_identity_change_on_another_account_is_rechecked(db_path):
    alias = _account(db_path, 102, config={"threadUserId": "originally-different"})
    _target(db_path, account_ref=alias, status="succeeded")
    _target(db_path)
    plan = _plan(db_path)
    assert len(_entries(plan)) == 1
    _update(db_path, "accounts", 102, config_json='{"threadUserId":"identity-101"}')
    _assert_refused_unchanged(db_path, plan)


def test_reapplying_plan_does_not_reset_pending_target_or_schedule(db_path):
    _target(db_path)
    plan = _plan(db_path)
    assert recovery.apply_recovery(plan, db_path=db_path)["applied"] == 1
    _assert_refused_unchanged(db_path, plan)


def test_existing_canonical_bookings_spacing_caps_and_new_collision(db_path):
    alias = _account(db_path, 102, config={"userId": "identity-101"})
    occupied = _target(db_path, account_ref=alias, file_ref="/occupied.mp4", status="pending")
    _update(db_path, "publish_job_targets", occupied, schedule_at="2035-01-02T12:05:00")
    _target(db_path)
    plan = _plan(db_path, per_day_cap=1)
    assert plan[0]["slot"].startswith("2035-01-03T")
    plan = _plan(db_path, per_day_cap=0)
    assert plan[0]["slot"] == "2035-01-02T12:35:00"
    new = _target(db_path, file_ref="/new-booking.mp4", status="pending")
    _update(db_path, "publish_job_targets", new, schedule_at=plan[0]["slot"])
    _assert_refused_unchanged(db_path, plan)


def test_apply_validation_holds_same_sqlite_write_lock(db_path, monkeypatch):
    _target(db_path)
    plan = _plan(db_path)
    original = recovery._snapshot
    calls = []

    def locked(conn, *args, **kwargs):
        assert conn.in_transaction
        with closing(sqlite3.connect(db_path, timeout=0)) as rival:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                rival.execute("BEGIN IMMEDIATE")
        calls.append(conn)
        return original(conn, *args, **kwargs)

    monkeypatch.setattr(recovery, "_snapshot", locked)
    assert recovery.apply_recovery(plan, db_path=db_path)["applied"] == 1
    assert len(calls) == 1


def test_reset_and_recount_roll_back_together(db_path, monkeypatch):
    _target(db_path)
    plan = _plan(db_path)

    def fail(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(jobs, "_recount_job", fail)
    result = _assert_refused_unchanged(db_path, plan)
    assert SECRET not in json.dumps(result)


def test_guard_callback_must_be_read_only(db_path):
    target = _target(db_path)
    before = _dump(db_path)

    def writes(conn):
        conn.execute("UPDATE accounts SET enabled=0 WHERE id=101")
        return "0" * 64

    with pytest.raises(ValueError, match="read-only"):
        jobs.reschedule_failed_target(target, "2035-02-01T00:00:00", expected_fingerprint="0" * 64,
                                      revalidate=writes, db_path=db_path)
    assert _dump(db_path) == before


def test_campaign_recovery_updates_only_linked_projections(db_path):
    target = _target(db_path)
    campaign, post = _campaign(db_path, target)
    assert recovery.apply_recovery(_plan(db_path), db_path=db_path)["applied"] == 1
    assert _row(db_path, "campaign_posts", post)["status"] == "queued"
    assert _row(db_path, "campaigns", campaign)["status"] == "publishing"


@pytest.mark.parametrize("table,field,value", [
    ("campaign_posts", "status", "cancelled"), ("campaign_posts", "status", "published"),
    ("campaigns", "status", "cancelled"), ("campaigns", "status", "published"),
    ("campaigns", "status", "preparing"), ("campaigns", "profile_id", 999),
    ("campaign_posts", "workspace_id", "other"), ("campaign_posts", "last_published_job_id", None),
])
def test_campaign_terminal_or_superseded_intent_refuses(db_path, table, field, value):
    target = _target(db_path)
    campaign, post = _campaign(db_path, target)
    plan = _plan(db_path)
    _update(db_path, table, campaign if table == "campaigns" else post, **{field: value})
    assert not _entries(_plan(db_path, resolved_target_ids=[target]))
    _assert_refused_unchanged(db_path, plan)


def test_lookback_explicit_selection_and_resolved_permanent_are_separate(db_path):
    old = _target(db_path, file_ref="/old.mp4", finished="2030-01-01T00:00:00", error="forbidden reconnect")
    recent = _target(db_path, file_ref="/recent.mp4")
    assert [entry["target_id"] for entry in _entries(_plan(db_path))] == [recent]
    assert not _entries(_plan(db_path, target_ids=[old]))
    plan = _plan(db_path, resolved_target_ids=[old])
    assert [entry["target_id"] for entry in _entries(plan)] == [old]
    assert plan[0]["resolved_permanent"] is True
    assert recovery.apply_recovery(plan, db_path=db_path)["applied"] == 1
    assert _plan(db_path, target_ids=[]) == []


def test_explicit_target_ids_cover_all_dates_and_union_with_resolved_ids(db_path):
    old = _target(db_path, file_ref="/old.mp4", finished="2030-01-01T00:00:00")
    fixed = _target(db_path, file_ref="/fixed.mp4", error="permission denied", finished="2031-01-01T00:00:00")
    _target(db_path, file_ref="/unselected.mp4")
    plan = _plan(db_path, target_ids=[old, old], resolved_target_ids=[fixed])
    assert [entry["target_id"] for entry in _entries(plan)] == [old, fixed]
    assert recovery.apply_recovery(plan, db_path=db_path)["applied"] == 2


@pytest.mark.parametrize("kwargs", [
    {"lookback_days": 0}, {"lookback_days": -1}, {"lookback_days": "2"}, {"lookback_days": True},
    {"limit": 0}, {"limit": -1}, {"per_day_cap": -1}, {"per_day_cap": "4"},
    {"target_ids": "1"}, {"target_ids": [True]}, {"target_ids": [1.5]}, {"target_ids": [2**63]},
    {"target_ids": [999999]}, {"resolved_target_ids": [0]}, {"resolved_target_ids": 1},
    {"platform": "unrecognized"}, {"now": "bad"},
])
def test_malformed_arguments_fail_clearly_without_writes(db_path, kwargs):
    before = _dump(db_path)
    options = {"now": NOW, **kwargs}
    with pytest.raises(ValueError):
        recovery.plan_recovery(db_path=db_path, **options)
    assert _dump(db_path) == before


@pytest.mark.parametrize("field,value", [
    ("expected_fingerprint", None), ("slot", "not-a-date"), ("target_id", True),
    ("resolved_permanent", "true"), ("per_day_cap", -1), ("account_ref", "cookie:101"),
])
def test_malformed_plan_is_checked_in_full_before_any_apply(db_path, field, value):
    _target(db_path)
    plan = _plan(db_path)
    malformed = {**plan[0], "target_id": plan[0]["target_id"] + 1, field: value}
    before = _dump(db_path)
    with pytest.raises(ValueError):
        recovery.apply_recovery([*plan, malformed], db_path=db_path)
    assert _dump(db_path) == before


def test_plan_flags_and_slots_are_bound_to_the_fingerprint(db_path):
    _target(db_path)
    item = _plan(db_path)[0]
    for changes in ({"resolved_permanent": True}, {"slot": "2035-02-01T00:00:00"}, {"per_day_cap": 0}):
        _assert_refused_unchanged(db_path, [{**item, **changes}])


def test_timezone_aware_plans_are_utc_and_limit_is_retained(db_path):
    _target(db_path, file_ref="/a.mp4")
    _target(db_path, file_ref="/b.mp4")
    aware = NOW.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8)))
    assert recovery.plan_recovery(db_path=db_path, now=aware, limit=1) == _plan(db_path, limit=1)
    assert len(_entries(_plan(db_path, limit=1))) == 1


def test_cli_repeatable_ids_and_default_dry_run_do_not_apply(db_path, monkeypatch, capsys):
    a = _target(db_path, file_ref="/a.mp4")
    b = _target(db_path, file_ref="/b.mp4", error="permission denied")
    before = _dump(db_path)
    monkeypatch.setattr(recovery, "apply_recovery", lambda *a, **k: pytest.fail("dry-run must not apply"))
    assert recovery.main(["--db-path", str(db_path), "--target-id", str(a), "--target-id", str(a),
                          "--resolved-target-id", str(b)]) == 0
    output = capsys.readouterr().out
    assert "DRY RUN: 2 target(s)" in output and "Re-run with --apply" in output
    assert SECRET not in output
    assert _dump(db_path) == before


def test_cli_reports_invalid_selection_without_traceback(db_path, capsys):
    with pytest.raises(SystemExit) as exc:
        recovery.main(["--db-path", str(db_path), "--target-id", "0"])
    assert exc.value.code == 2
    assert "must be an integer" in capsys.readouterr().err


@pytest.mark.parametrize("action", ["reschedule", "resubmit"])
def test_existing_explicit_retry_semantics_are_unchanged(db_path, action):
    target = _target(db_path)
    _update(db_path, "publish_job_targets", target, checkpoint_json='{"provider_receipt":{"id":"explicit-intent"}}')
    if action == "reschedule":
        result = jobs.reschedule_target(target, "2035-02-01T00:00:00", db_path=db_path)
    else:
        result = jobs.resubmit_target(target, db_path=db_path)
    assert result.status == "pending" and result.checkpoint == {}


@pytest.mark.parametrize("payload", [
    {"artifacts": {}}, {"artifacts": None}, {"artifacts": [False]},
    {"artifacts": [{"metadata": []}]}, {"platform": "facebook"},
])
def test_malformed_or_mismatched_payload_is_not_recovered(db_path, payload):
    target = _target(db_path, payload=payload)
    assert not _entries(_plan(db_path, resolved_target_ids=[target]))


def test_orphaned_delivery_history_fails_closed(db_path):
    _target(db_path)
    plan = _plan(db_path)
    other_job, _ = _job(db_path, status="succeeded")
    _sql(db_path, "DELETE FROM publish_jobs WHERE id=?", (other_job,))
    assert not _entries(_plan(db_path))
    _assert_refused_unchanged(db_path, plan)


def test_platform_filter_does_not_include_other_platform_refusals(db_path):
    target = _target(db_path)
    _target(db_path, platform="reddit", file_ref="/retired.mp4")
    plan = _plan(db_path, platform="threads")
    assert len(plan) == 1 and plan[0]["target_id"] == target


def test_unsupported_registry_route_is_not_recovered(db_path):
    _update(db_path, "accounts", 101, platform="patreon")
    target = _target(db_path, platform="patreon", error="unsupported")
    assert not _entries(_plan(db_path, resolved_target_ids=[target]))


def test_expired_plan_is_refused_without_writes(db_path, monkeypatch):
    _target(db_path)
    plan = _plan(db_path)
    monkeypatch.setattr(jobs, "_now_iso", lambda: "2035-03-01T00:00:00")
    _assert_refused_unchanged(db_path, plan)


def test_job_target_membership_changes_invalidate_plan(db_path):
    job_id, _ = _job(db_path, payload={"artifacts": [{"id": 700}]})
    plan = _plan(db_path)
    _sql(db_path, "INSERT INTO publish_job_targets(job_id, account_ref, file_ref) VALUES (?, 'account:101', '/added.mp4')",
         (job_id,))
    _assert_refused_unchanged(db_path, plan)


@pytest.mark.parametrize("platform", ["threads", "twitter", "instagram"])
def test_explicit_managed_route_can_recover_with_revoked_direct_token(db_path, monkeypatch, platform):
    network = {"threads": "thrd", "twitter": "tw", "instagram": "in"}[platform]
    config = {"_needsReconnect": True, "publishingProvider": "sociamonials",
              "sociamonials": {"network": network, "profile_refs": ["synthetic-profile"], "workspace_id": "123"}}
    _update(db_path, "accounts", 101, platform=platform, config_json=json.dumps(config))
    monkeypatch.setenv("SAU_SOCIAMONIALS_FALLBACK", "1")
    target = _target(db_path, platform=platform)
    plan = _plan(db_path, target_ids=[target])
    assert len(_entries(plan)) == 1
    assert recovery.apply_recovery(plan, db_path=db_path) == {"applied": 1, "errors": []}
    nonce = jobs.get_target(target, db_path=db_path).checkpoint["recovery_generation"]
    assert nonce == plan[0]["expected_fingerprint"]
    assert recovery.apply_recovery(plan, db_path=db_path)["applied"] == 0
    assert jobs.get_target(target, db_path=db_path).checkpoint["recovery_generation"] == nonce


def test_managed_route_change_or_disabling_refuses_stale_plan(db_path, monkeypatch):
    config = {"publishingProvider": "sociamonials",
              "sociamonials": {"network": "thrd", "profile_refs": ["first"], "workspace_id": "123"}}
    _update(db_path, "accounts", 101, config_json=json.dumps(config))
    monkeypatch.setenv("SAU_SOCIAMONIALS_FALLBACK", "1")
    target = _target(db_path)
    plan = _plan(db_path, target_ids=[target])
    config["sociamonials"]["profile_refs"] = ["changed"]
    _update(db_path, "accounts", 101, config_json=json.dumps(config))
    _assert_refused_unchanged(db_path, plan)
    plan = _plan(db_path, target_ids=[target])
    monkeypatch.setenv("SAU_SOCIAMONIALS_FALLBACK", "0")
    _assert_refused_unchanged(db_path, plan)


def _managed_account_config(native_id, *, workspace="123", profile_ref="shared"):
    result = {"publishingProvider": "sociamonials", "sociamonials": {
        "network": "thrd", "profile_refs": [profile_ref], "workspace_id": workspace}}
    if native_id:
        result["threadUserId"] = native_id
    return result


@pytest.mark.parametrize("native_ids", [(None, None), ("first", "different")])
@pytest.mark.parametrize("status", ["succeeded", "awaiting_delivery"])
def test_shared_managed_destination_protects_native_aliases(db_path, monkeypatch, native_ids, status):
    monkeypatch.setenv("SAU_SOCIAMONIALS_FALLBACK", "1")
    _update(db_path, "accounts", 101, config_json=json.dumps(_managed_account_config(native_ids[0])))
    alias = _account(db_path, 102, config=_managed_account_config(native_ids[1]))
    _target(db_path, account_ref=alias, status=status)
    target = _target(db_path)
    assert not _entries(_plan(db_path, target_ids=[target]))


def test_managed_alias_dedupes_plan_and_revalidates_changed_destination(db_path, monkeypatch):
    monkeypatch.setenv("SAU_SOCIAMONIALS_FALLBACK", "1")
    _update(db_path, "accounts", 101, config_json=json.dumps(_managed_account_config("first")))
    alias = _account(db_path, 102, config=_managed_account_config("second"))
    first = _target(db_path)
    second = _target(db_path, account_ref=alias)
    plan = _plan(db_path, target_ids=[first, second])
    assert len(_entries(plan)) == 1
    assert recovery.apply_recovery(plan, db_path=db_path)["applied"] == 1
    _update(db_path, "accounts", 102, config_json=json.dumps(_managed_account_config("second", profile_ref="other")))
    plan = _plan(db_path, target_ids=[second])
    assert len(_entries(plan)) == 1
    _update(db_path, "accounts", 102, config_json=json.dumps(_managed_account_config("second")))
    _assert_refused_unchanged(db_path, plan)


def test_managed_profile_reference_is_namespaced_by_workspace(db_path, monkeypatch):
    monkeypatch.setenv("SAU_SOCIAMONIALS_FALLBACK", "1")
    _update(db_path, "accounts", 101, config_json=json.dumps(_managed_account_config("first", workspace="123")))
    alias = _account(db_path, 102, config=_managed_account_config("second", workspace="456"))
    _target(db_path, account_ref=alias, status="succeeded")
    target = _target(db_path)
    assert len(_entries(_plan(db_path, target_ids=[target]))) == 1


def test_x_recovery_validates_profile_override_instead_of_builtin_mapping(db_path, monkeypatch):
    monkeypatch.setenv("SAU_SOCIAMONIALS_FALLBACK", "1")
    monkeypatch.setenv("SAU_X_DIRECT_PUBLISH", "0")
    alias = _account(db_path, 77, platform="twitter", config={})
    target = _target(db_path, account_ref=alias, platform="twitter")
    plan = _plan(db_path, target_ids=[target])
    assert len(_entries(plan)) == 1
    settings = {"sociamonials": {"network": "tw", "profile_refs": ["one", "two"]}}
    _update(db_path, "profiles", 100, settings_json=json.dumps(settings))
    assert not _entries(_plan(db_path, target_ids=[target]))
    _assert_refused_unchanged(db_path, plan)


def test_recovery_cannot_raise_or_disable_current_pacing(db_path, monkeypatch):
    monkeypatch.setattr(recovery.publish_orchestrator, "MAX_POSTS_PER_ACCOUNT_PER_DAY", 3)
    _target(db_path)
    for cap in (None, 4, 999, 0):
        assert _entries(_plan(db_path, per_day_cap=cap))[0]["per_day_cap"] == 3
    assert _entries(_plan(db_path, per_day_cap=2))[0]["per_day_cap"] == 2


@pytest.mark.parametrize("status", ["succeeded", "running", "awaiting_delivery", "needs_review"])
def test_shared_delivered_and_inflight_history_consumes_capacity(db_path, monkeypatch, status):
    monkeypatch.setattr(recovery.publish_orchestrator, "MAX_POSTS_PER_ACCOUNT_PER_DAY", 1)
    _account(db_path, 102, config={"threadUserId": "identity-101"})
    _target(db_path, account_ref="account:102", file_ref="different.mp4", status=status,
            finished=NOW.isoformat())
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("UPDATE publish_job_targets SET schedule_at=?", (NOW.isoformat(),))
    selected = _target(db_path)
    plan = _entries(_plan(db_path, target_ids=[selected]))
    assert len(plan) == 1 and plan[0]["slot"][:10] > NOW.date().isoformat()
