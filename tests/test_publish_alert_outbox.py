"""Atomic failure/outbox, restart recovery and bounded notification retries."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from db import createTable
from myUtils import jobs, profiles, publish_alert_outbox as outbox


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "outbox.db"
    createTable.bootstrap(path)
    profile = profiles.create_profile("Fixture", db_path=path)
    account = profiles.add_account(profile.id, platform="threads", account_name="Fixture",
                                   config={}, db_path=path)
    job = jobs.enqueue_job(jobs.JobSpec(platform="threads", profile_id=profile.id,
                          payload={"message": "Original"},
                          targets=[(f"account:{account.id}", "fixture.mp4", None)]), db_path=path)
    target = jobs.claim_next_targets(db_path=path, owner="fixture")[0]
    return path, job, target


def records(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM publish_alert_outbox")]


@pytest.mark.parametrize("transition,status", [(jobs.mark_target_failed, "failed"),
                         (jobs.mark_target_blocked, "blocked"), (jobs.mark_target_needs_review, "needs_review")])
def test_transition_and_alert_commit_together(database, transition, status):
    path, _job, target = database
    assert transition(target.id, "HTTP 400", lease_token=target.lease_token,
                      db_path=path, alert_max_attempts=3)
    assert jobs.get_target(target.id, db_path=path).status == status
    event = records(path)[0]
    assert event["state"] == "pending" and event["attempts"] == 0
    assert "Occurred (UTC):" in event["body"]
    assert f"outcome: {status}" in event["body"]
    assert "Attempts: 1/3" in event["body"]
    if status == "needs_review":
        assert "Verify the existing provider receipt" in event["body"]
    assert not transition(target.id, "again", lease_token=target.lease_token, db_path=path)
    assert len(records(path)) == 1


def test_enqueue_error_rolls_back_terminal_outcome(database, monkeypatch):
    path, job, target = database
    monkeypatch.setattr(outbox, "enqueue_publish_stop", Mock(side_effect=sqlite3.OperationalError("disk full")))
    with pytest.raises(sqlite3.OperationalError):
        jobs.mark_target_failed(target.id, "failure", lease_token=target.lease_token, db_path=path)
    assert jobs.get_target(target.id, db_path=path).status == "running"
    assert jobs.get_job(job.id, db_path=path).status == "running"
    assert records(path) == []


def test_stale_completion_and_cancel_never_enqueue(database):
    path, job, target = database
    assert not jobs.mark_target_failed(target.id, "failure", lease_token="wrong", db_path=path)
    jobs.cancel_job(job.id, db_path=path)
    assert not jobs.mark_target_failed(target.id, "failure", lease_token=target.lease_token, db_path=path)
    assert records(path) == []


def test_failed_notification_retains_event_and_restart_flushes_once(database):
    path, _job, target = database
    jobs.mark_target_failed(target.id, "failure", lease_token=target.lease_token, db_path=path)
    sender = Mock(side_effect=[False, True])
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert outbox.flush_due(db_path=path, sender=sender, now=now) == {"sent": 0, "failed": 1}
    assert outbox.flush_due(db_path=path, sender=sender, now=now + timedelta(seconds=30)) == {"sent": 0, "failed": 0}
    assert outbox.flush_due(db_path=path, sender=sender, now=now + timedelta(minutes=2)) == {"sent": 1, "failed": 0}
    assert outbox.flush_due(db_path=path, sender=sender, now=now + timedelta(hours=2)) == {"sent": 0, "failed": 0}
    assert sender.call_count == 2
    assert jobs.get_target(target.id, db_path=path).status == "failed"


def test_crashed_sender_lease_is_recovered(database):
    path, _job, target = database
    jobs.mark_target_failed(target.id, "failure", lease_token=target.lease_token, db_path=path)
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    with pytest.raises(KeyboardInterrupt):
        outbox.flush_due(db_path=path, sender=Mock(side_effect=KeyboardInterrupt), now=now)
    assert records(path)[0]["state"] == "sending"
    sender = Mock(return_value=True)
    assert outbox.flush_due(db_path=path, sender=sender, now=now + timedelta(seconds=10)) == {"sent": 0, "failed": 0}
    assert outbox.flush_due(db_path=path, sender=sender,
                            now=now + timedelta(seconds=outbox.LEASE_SECONDS + 1)) == {"sent": 1, "failed": 0}
    sender.assert_called_once()


def test_alert_contains_route_outcomes_not_tokens(database):
    path, _job, target = database
    state = {"publish_routes": {"active": "sociamonials", "native": {"attempts": 3,
             "outcome": "rejected", "error": "access_token=secret"},
             "sociamonials": {"attempts": 2, "outcome": "failed", "error": "Bearer secret"}},
             "payload": {"preparedPart": {"index": 2, "count": 3}}}
    assert jobs.save_target_checkpoint(target.id, state, lease_token=target.lease_token, db_path=path)
    jobs.mark_target_failed(target.id, "https://api.example/error?access_token=secret",
                            lease_token=target.lease_token, db_path=path)
    body = records(path)[0]["body"]
    assert "Part: 2/3" in body and "Native: rejected" in body and "Sociamonials: failed" in body
    assert "secret" not in body


def test_enqueue_requires_transaction_and_keeps_immutable_content(database):
    path, *_ = database
    with closing(sqlite3.connect(path)) as conn:
        with pytest.raises(ValueError, match="active transaction"):
            outbox.enqueue_in_transaction(conn, event_key="k", subject="s", body="b")
    first = outbox.enqueue_alert(db_path=path, event_key="k", subject="s", body="b")
    assert first == outbox.enqueue_alert(db_path=path, event_key="k", subject="s", body="b")
    with pytest.raises(ValueError, match="different"):
        outbox.enqueue_alert(db_path=path, event_key="k", subject="s", body="changed")


@pytest.mark.parametrize("transition,status", [(jobs.mark_target_failed, "failed"),
                         (jobs.mark_target_blocked, "blocked"), (jobs.mark_target_needs_review, "needs_review")])
def test_malformed_ui_origin_cannot_roll_back_terminal_event(database, monkeypatch, transition, status):
    path, job, target = database
    monkeypatch.setenv("SAU_PUBLIC_APP_URL", "https://[broken")
    assert transition(target.id, "Stop", lease_token=target.lease_token, db_path=path)
    assert jobs.get_target(target.id, db_path=path).status == status
    assert f"Open in UI: #/jobs?job={job.id}" in records(path)[0]["body"]
