"""Receipt-safe schedule edits use isolated databases and never provider writes."""
from __future__ import annotations

import json
import sqlite3

import pytest

from db.createTable import bootstrap
from myUtils import jobs, publish_orchestrator


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    bootstrap(path)
    monkeypatch.setattr(publish_orchestrator, "MIN_GAP_MINUTES", 30)
    monkeypatch.setattr(publish_orchestrator, "MAX_POSTS_PER_ACCOUNT_PER_DAY", 3)
    return path


def enqueue(database, targets=None):
    job = jobs.enqueue_job(jobs.JobSpec(
        platform="bluesky", payload={"message": "exact approved copy"},
        targets=targets or [("calendar-account", "file-1", "2100-01-01T01:00:00")]),
        db_path=database)
    return jobs.list_targets(job.id, db_path=database)


def snapshot(database, target_id):
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM publish_job_targets WHERE id=?", (target_id,)).fetchone())


def update(database, target_id, **values):
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE publish_job_targets SET " + ",".join(f"{key}=?" for key in values)
                     + " WHERE id=?", [*values.values(), target_id])


@pytest.mark.parametrize("status", ["failed", "cancelled", "blocked", "pending", "retrying"])
@pytest.mark.parametrize("evidence", [
    {"provider_receipt": {"provider": "sociamonials", "id": "fixture-accepted", "status": "accepted"}},
    {"submission_started": True},
    {"previous_failure": {"provider_receipt": {"id": "fixture-retained"}}},
    {"provider_operations": {"native": {"plan": ["post"], "items": {"post": {"state": "started"}}}}},
    {"x_browser_operations": {"version": 1, "target_id": 1, "account_key": "calendar-account",
        "plan": [{"index": 0, "identity": "a" * 64}], "budgets": {"media": {"attempts": 1, "max_attempts": 256}},
        "items": {"media:0:1": {"status": "unknown", "effect": "media_only"}}}},
    {"previous_failure": {"checkpoint": {"x_browser_operations": {"items": {"media": {"status": "unknown"}}}}}},
])
def test_all_labels_preserve_submission_evidence(database, status, evidence):
    target = enqueue(database)[0]
    update(database, target.id, status=status, checkpoint_json=json.dumps(evidence))
    original = snapshot(database, target.id)
    with pytest.raises(ValueError):
        jobs.reschedule_target(target.id, "2100-01-02T01:00:00", db_path=database)
    if status in {"failed", "cancelled", "blocked"}:
        with pytest.raises(ValueError):
            jobs.resubmit_target(target.id, db_path=database)
    assert snapshot(database, target.id) == original


def test_cancel_then_retry_never_discards_accepted_receipt(database):
    target = enqueue(database)[0]
    evidence = {"submission_started": True, "provider_receipt": {
        "provider": "sociamonials", "id": "fixture-accepted", "status": "accepted"}}
    update(database, target.id, status="awaiting_delivery", delivery_state="awaiting_delivery",
           checkpoint_json=json.dumps(evidence))
    cancelled = jobs.cancel_target(target.id, db_path=database)
    assert cancelled.checkpoint == evidence
    assert cancelled.delivery_state == "awaiting_delivery"
    original = snapshot(database, target.id)
    with pytest.raises(ValueError, match="Submission evidence"):
        jobs.resubmit_target(target.id, db_path=database)
    assert snapshot(database, target.id) == original


@pytest.mark.parametrize("checkpoint", ['{"duplicate":1,"duplicate":2}', '["invalid"]', 'not-json'])
def test_malformed_evidence_is_not_reset(database, checkpoint):
    target = enqueue(database)[0]
    update(database, target.id, status="failed", checkpoint_json=checkpoint)
    original = snapshot(database, target.id)
    with pytest.raises(ValueError):
        jobs.resubmit_target(target.id, db_path=database)
    assert snapshot(database, target.id) == original


@pytest.mark.parametrize("status", ["pending", "retrying", "failed", "cancelled", "blocked"])
def test_calendar_only_changes_schedule(database, status):
    target = enqueue(database)[0]
    checkpoint = {"recovery_generation": "exact-generation", "payload": {
        "message": "exact approved copy", "artifacts": [{"local_path": "exact-variant.mp4",
        "metadata": {"sha256": "1" * 64}}]},
        "previous_failure": {"status": "failed", "reason": "preflight failed"},
        "publish_routes": {"native": {"attempts": 2, "state": "failed"}}}
    update(database, target.id, status=status, attempts=2, checkpoint_json=json.dumps(checkpoint),
           last_error="preflight failed", error_category="configuration", next_attempt_at="2099-12-31T00:00:00",
           started_at="2026-11-12T04:00:00", finished_at="2026-11-12T04:01:00")
    original = snapshot(database, target.id)
    changed = jobs.reschedule_target(target.id, "2100-01-02T01:00:00", db_path=database)
    assert changed.status == status
    after = snapshot(database, target.id)
    assert after.pop("schedule_at") == "2100-01-02T01:00:00"
    original.pop("schedule_at")
    assert after == original


def test_calendar_refuses_fourth_destination_post(database):
    targets = enqueue(database, [
        ("calendar-account", "one", "2100-01-01T00:00:00"),
        ("calendar-account", "two", "2100-01-01T00:30:00"),
        ("calendar-account", "three", "2100-01-01T01:00:00"),
        ("calendar-account", "edit", "2100-01-02T00:00:00"),
    ])
    original = snapshot(database, targets[-1].id)
    with pytest.raises(ValueError, match="capacity"):
        jobs.reschedule_target(targets[-1].id, "2100-01-01T02:00:00", db_path=database)
    assert snapshot(database, targets[-1].id) == original


def test_calendar_gap_and_releasing_only_own_reservation(database):
    targets = enqueue(database, [
        ("calendar-account", "one", "2100-01-01T00:00:00"),
        ("calendar-account", "edit", "2100-01-01T01:00:00"),
    ])
    with pytest.raises(ValueError, match="gap"):
        jobs.reschedule_target(targets[-1].id, "2100-01-01T00:15:00", db_path=database)
    moved = jobs.reschedule_target(targets[-1].id, "2100-01-01T00:30:00", db_path=database)
    assert moved.schedule_at == "2100-01-01T00:30:00"
    again = jobs.reschedule_target(targets[-1].id, moved.schedule_at, db_path=database)
    assert again.schedule_at == moved.schedule_at


@pytest.mark.parametrize("part_index,new_slot", [(0, "2100-01-01T02:30:00"), (1, "2100-01-01T00:30:00")])
def test_calendar_preserves_ordered_siblings(database, part_index, new_slot):
    targets = enqueue(database, [
        ("calendar-account", "one", "2100-01-01T01:00:00"),
        ("calendar-account", "two", "2100-01-01T02:00:00"),
    ])
    for index, target in enumerate(targets, start=1):
        update(database, target.id, checkpoint_json=json.dumps({"payload": {
            "preparedPart": {"index": index, "count": 2}}}))
    original = snapshot(database, targets[part_index].id)
    with pytest.raises(ValueError, match="part order"):
        jobs.reschedule_target(targets[part_index].id, new_slot, db_path=database)
    assert snapshot(database, targets[part_index].id) == original
