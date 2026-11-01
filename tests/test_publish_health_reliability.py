"""Reliability health uses migrated SQLite fixtures, never real providers."""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
from flask import Flask

from db import createTable as create_table
from myUtils import jobs, pipeline_monitor as monitor, profiles, publish_health as health
from myUtils.health_checks import database_ready
from sau_app.extensions import register_readiness_check
from sau_app.health import health_bp

NOW = datetime(2030, 4, 5, 12, 0, 0)


def stamp(*, seconds=0, minutes=0, hours=0, days=0):
    return (NOW + timedelta(seconds=seconds, minutes=minutes, hours=hours, days=days)).isoformat()


@pytest.fixture(autouse=True)
def isolated_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.replace(tzinfo=timezone.utc).astimezone(tz) if tz else NOW
    monkeypatch.setattr(health, "datetime", Clock)
    monkeypatch.setattr(jobs, "_now_iso", lambda: stamp())
    monkeypatch.setattr(monitor, "_now", lambda: NOW)
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("network forbidden")))
    monkeypatch.setenv("SAU_BUILD_REVISION", "abc123-release")
    monkeypatch.delenv("SAU_DB_URL", raising=False)
    monkeypatch.delenv("SAU_REQUIRE_WORKER_HEALTH", raising=False)


@pytest.fixture(scope="module")
def schema_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("health-schema") / "schema.db"
    with patch.dict(os.environ, {"SAU_DB_URL": f"sqlite:///{path.resolve()}"}):
        create_table.bootstrap(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert "lease_token" in {r[1] for r in conn.execute("PRAGMA table_info(publish_job_targets)")}
        assert conn.execute("SELECT COUNT(*) FROM worker_heartbeats").fetchone()[0] == 0
    return path


@pytest.fixture
def db(tmp_path, schema_db):
    path = tmp_path / "health.db"
    shutil.copyfile(schema_db, path)
    return path


def add_target(db, status="pending", *, platform="telegram", workspace=None,
               parent_status="pending", created_at=None, account_ref="account:1", **fields):
    with closing(sqlite3.connect(db)) as conn, conn:
        job_id = conn.execute(
            "INSERT INTO publish_jobs (idempotency_key, platform, payload_json, status, created_at, workspace_id) "
            "VALUES (?, ?, '{}', ?, ?, ?)",
            (uuid.uuid4().hex, platform, parent_status, created_at or stamp(hours=-3), workspace),
        ).lastrowid
        values = {"job_id": job_id, "account_ref": account_ref, "file_ref": "file:1", "status": status, **fields}
        columns = ", ".join(values)
        target_id = conn.execute(f"INSERT INTO publish_job_targets ({columns}) VALUES ({','.join('?' for _ in values)})",
                                 list(values.values())).lastrowid
    return target_id


def worker(db, name="worker-a", *, age=10, draining=False, role="publisher", heartbeat=None):
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO worker_heartbeats (worker_id, heartbeat_at, draining, role) VALUES (?, ?, ?, ?)",
                     (name, heartbeat or stamp(seconds=-age), int(draining), role))


def snapshot(db, **kwargs):
    return health.publish_health_snapshot(db, **kwargs).to_dict()


def test_all_durable_states_and_lifetime_vs_recent_delivery_metrics(db):
    for state in health._STATES:
        fields = {"finished_at": stamp(hours=-1)} if state in {"failed", "succeeded"} else {}
        if state == "succeeded":
            fields["delivery_state"] = "delivered"
        add_target(db, state, **fields)
    add_target(db, "succeeded", finished_at=stamp(days=-4))  # Legacy success, no receipt.
    add_target(db, "failed", finished_at=stamp(days=-3))
    add_target(db, "succeeded", platform="youtube", finished_at=stamp(hours=-1), delivery_state="delivered")
    worker(db)
    body = snapshot(db)
    assert {"platforms", "totals", "recentFailures", "staleTargets", "accountsNeedingReconnect", "timestamp"} <= body.keys()
    assert body["revision"] == "abc123-release"
    tg = next(p for p in body["platforms"] if p["platform"] == "telegram")
    assert tg["total"] == 11
    assert tg["blocked"] == tg["awaiting_delivery"] == tg["needs_review"] == 1
    assert tg["succeeded"] == tg["failed"] == 2
    assert tg["successRate"] == tg["lifetimeSuccessRate"] == .5
    assert tg["delivered"] == tg["legacySucceeded"] == tg["acceptedPending"] == 1
    assert body["totals"]["total"] == 12
    assert body["totals"]["succeeded"] == 3
    assert body["totals"]["lifetimeSuccessRate"] == .6
    assert body["recent"]["succeeded"] == 2
    assert body["recent"]["failed"] == 1
    assert body["recent"]["successRate"] == .667
    assert body["recent"]["delivered"] == 2
    assert body["recent"]["legacySucceeded"] == 0
    assert body["recent"]["windowHours"] == 24
    assert body["statisticsScope"] == "lifetime"


def test_due_queue_age_respects_schedule_backoff_and_immediate_work(db):
    add_target(db, created_at=stamp(days=-100), schedule_at=stamp(days=2))
    add_target(db, "retrying", created_at=stamp(days=-50), schedule_at=stamp(days=-40), next_attempt_at=stamp(hours=1))
    add_target(db, created_at=stamp(hours=-2))
    add_target(db, created_at=stamp(days=-20), schedule_at=stamp(minutes=-30))
    add_target(db, "retrying", created_at=stamp(days=-30), next_attempt_at=stamp(minutes=-10))
    body = snapshot(db)
    queue = body["queue"]
    assert body["totals"]["pending"] == 3
    assert queue["due"] == 3
    assert queue["scheduled"] == 1
    assert queue["retryDelayed"] == 1
    assert queue["oldestDueAt"] == stamp(hours=-2)
    assert queue["oldestDueAgeSeconds"] == 7200
    assert queue["overdue"] == 2
    assert queue["deliveryPollsDue"] == 0


def test_future_only_queue_has_no_due_age_or_overdue_incident(db):
    add_target(db, created_at=stamp(days=-180), schedule_at=stamp(days=50))
    add_target(db, "retrying", created_at=stamp(days=-200), next_attempt_at=stamp(hours=1))
    add_target(db, "awaiting_delivery", created_at=stamp(days=-100), next_attempt_at=stamp(minutes=1),
               delivery_state="awaiting_delivery")
    worker(db)
    view = health.publish_health_snapshot(db)
    assert view.queue["due"] == 0
    assert view.queue["oldestDueAgeSeconds"] is None
    assert view.queue["oldestDueAt"] is None
    assert monitor._publishing_problems(view) == {}


def test_schedule_dates_with_offsets_match_utc_and_invalid_dates_are_actionable(db):
    add_target(db, created_at=stamp(days=-1), schedule_at="2030-04-05T18:00:00+08:00")
    add_target(db, created_at=stamp(days=-1), schedule_at="2030-04-05T09:00:00-08:00")
    bad = add_target(db, schedule_at="Bearer invalid-schedule-secret")
    add_target(db, "retrying", next_attempt_at="not-a-date")
    body = snapshot(db)
    assert body["queue"]["due"] == 1
    assert body["queue"]["scheduled"] == 1
    assert body["queue"]["oldestDueAgeSeconds"] == 7200
    assert body["queue"]["invalidSchedule"] == 2
    assert body["queue"]["actionRequired"] == 2
    assert any(t["targetId"] == bad and t["reason"] == "invalid_schedule" for t in body["actionTargets"])
    assert "invalid-schedule-secret" not in json.dumps(body)


def test_pending_provider_is_not_success_and_receipt_poll_ignores_submission_schedule(db):
    add_target(db, "awaiting_delivery", schedule_at=stamp(days=90), next_attempt_at=stamp(minutes=-5),
               delivery_state="awaiting_delivery", finished_at=stamp(minutes=-4))
    add_target(db, "running", delivery_state="awaiting_delivery", started_at=stamp(minutes=-1),
               lease_owner="worker-a", lease_token="private-lease", lease_expires_at=stamp(minutes=3))
    worker(db)
    body = snapshot(db)
    assert body["totals"]["acceptedPending"] == 2
    assert body["totals"]["succeeded"] == body["totals"]["delivered"] == 0
    assert body["recent"]["succeeded"] == body["recent"]["delivered"] == 0
    assert body["queue"]["deliveryPollsDue"] == 1
    assert body["queue"]["oldestDeliveryPollAgeSeconds"] == 300
    assert body["queue"]["due"] == body["queue"]["scheduled"] == 0
    assert body["staleTargets"] == []


def test_parent_and_prep_states_hold_submissions_but_not_receipt_polls(db):
    profile = profiles.create_profile("Brand", db_path=db)
    with closing(sqlite3.connect(db)) as conn, conn:
        group = conn.execute("INSERT INTO media_groups (name) VALUES ('Media')").lastrowid
        campaign = conn.execute("INSERT INTO campaigns (profile_id, media_group_id, status) VALUES (?, ?, 'preparing')",
                                (profile.id, group)).lastrowid
        post = conn.execute("INSERT INTO campaign_posts (campaign_id, platform) VALUES (?, 'telegram')", (campaign,)).lastrowid
    add_target(db, file_ref=f"campaign_post:{post}")
    add_target(db, parent_status="needs_review")
    add_target(db, parent_status="cancelled")
    add_target(db, "awaiting_delivery", file_ref=f"campaign_post:{post}", delivery_state="awaiting_delivery")
    body = snapshot(db)
    assert body["queue"]["due"] == 0
    assert body["queue"]["heldByParent"] == 2
    assert body["queue"]["waitingForPreparation"] == 1
    assert body["queue"]["deliveryPollsDue"] == 1


def test_expired_orphaned_and_legacy_stale_leases_not_healthy_long_running_work(db):
    worker(db)
    worker(db, "dead-worker", age=3600)
    live = add_target(db, "running", started_at=stamp(hours=-4), lease_owner="worker-a",
                      lease_token="live-token", lease_expires_at=stamp(minutes=5))
    expired = add_target(db, "running", started_at=stamp(minutes=-1), lease_owner="worker-a",
                         lease_token="expired-token", lease_expires_at=stamp(seconds=-1))
    missing = add_target(db, "running", started_at=stamp(minutes=-1), lease_owner="missing-worker",
                         lease_token="missing-token", lease_expires_at=stamp(minutes=5))
    dead = add_target(db, "running", started_at=stamp(minutes=-1), lease_owner="dead-worker",
                      lease_token="dead-token", lease_expires_at=stamp(minutes=5))
    corrupt = add_target(db, "running", started_at=stamp(minutes=-1), lease_token="bad-token", lease_expires_at="invalid")
    legacy = add_target(db, "running", started_at=stamp(hours=-1))
    body = snapshot(db)
    assert body["queue"]["expiredLeases"] == 1
    assert body["queue"]["orphanedLeases"] == 3
    assert body["queue"]["staleRunning"] == 5
    by_id = {t["targetId"]: t for t in body["staleTargets"]}
    assert set(by_id) == {expired, missing, dead, corrupt, legacy}
    assert live not in by_id
    assert by_id[expired]["reason"] == "expired_lease"
    assert by_id[missing]["reason"] == "orphaned_lease"
    assert by_id[legacy]["reason"] == "legacy_stale_execution"
    assert by_id[corrupt]["leaseExpiresAt"] is None


@pytest.mark.parametrize("age,draining,role,expected", [
    (0, False, "publisher", True), (90, False, "publisher", True),
    (91, False, "publisher", False), (-30, False, "publisher", False),
    (5, True, "publisher", False), (5, False, "other", False),
])
def test_worker_freshness_and_draining_policy(db, age, draining, role, expected):
    worker(db, age=age, draining=draining, role=role)
    view = snapshot(db)["workerHealth"]
    assert view["ok"] is expected
    assert view["maxAgeSeconds"] == 90
    assert view["scope"] == "deployment"
    assert set(view["workers"][0]) == {"worker_id", "heartbeat_at", "role", "draining", "alive", "age_seconds"}


def test_worker_missing_or_malformed_heartbeat_is_not_healthy(db):
    assert snapshot(db)["workerHealth"]["ok"] is False
    worker(db, heartbeat="access_token=heartbeat-secret")
    view = snapshot(db)["workerHealth"]
    assert view["ok"] is False
    assert view["workers"][0]["heartbeat_at"] is None
    assert "heartbeat-secret" not in json.dumps(view)
    worker(db, "new-worker")
    assert snapshot(db)["workerHealth"]["ok"] is True  # Historical stale rows are not an incident alone.


def test_redaction_precedes_truncation_and_no_checkpoint_config_or_lease_tokens_escape(db):
    errors = [
        "upload failed api_key=" + "KEYSECRET" * 80,
        "https://user:URLSECRET@example.invalid/video?X-Amz-Signature=SIGNSECRET failed",
        'config={"unexpected_field":"CONFIGSECRET"}',
        "Authorization: Basic BASICSECRET==",
        "checkpoint={'receipt': 'CHECKSECRET'}",
        "lease_token=LEASESECRET Bearer BEARERSECRET",
    ]
    for error in errors:
        add_target(db, "failed", finished_at=stamp(minutes=-1), last_error=error,
                   checkpoint_json='{"opaque":"CHECKPOINTSECRET"}', lease_token="TOKENNOTPUBLIC")
        add_target(db, "blocked", last_error=error, error_category="blocked")
    profile = profiles.create_profile("Brand", db_path=db)
    profiles.add_account(profile.id, "telegram", "account", auth_type="api", status=0,
                         config={"_needsReconnect": True, "accessToken": "ACCOUNTSECRET"}, db_path=db)
    text = json.dumps(snapshot(db))
    for secret in ("KEYSECRET", "URLSECRET", "SIGNSECRET", "CONFIGSECRET", "BASICSECRET", "CHECKSECRET",
                   "LEASESECRET", "BEARERSECRET", "CHECKPOINTSECRET", "TOKENNOTPUBLIC", "ACCOUNTSECRET"):
        assert secret not in text
    assert "checkpoint_json" not in text
    assert "lease_token" not in text
    assert "[REDACTED]" in text or "redacted" in text
    assert len(snapshot(db)["accountsNeedingReconnect"]) == 1


@pytest.mark.parametrize("config", ['{broken "token":"SECRET"', '[]', 'null', '"scalar"', '42'])
def test_malformed_optional_account_config_is_isolated_and_flagged(db, config):
    profile = profiles.create_profile("Brand", db_path=db)
    good = profiles.add_account(profile.id, "telegram", "good", auth_type="api", config={"_needsReconnect": True}, db_path=db)
    bad = profiles.add_account(profile.id, "telegram", "bad", auth_type="api", db_path=db)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE accounts SET config_json = ? WHERE id = ?", (config, bad.id))
    body = snapshot(db)
    assert [a["id"] for a in body["accountsNeedingReconnect"]] == [good.id]
    assert body["accountConfigIssues"] == [{"id": bad.id, "platform": "telegram", "nickname": "",
                                           "accountName": "bad", "reason": "invalid_account_config"}]
    assert body["totals"]["accountConfigIssues"] == 1
    assert "SECRET" not in json.dumps(body)


def test_workspace_scope_uses_parent_jobs_and_separately_scoped_account_join(db):
    pa = profiles.create_profile("A", workspace_id="a", db_path=db)
    pb = profiles.create_profile("B", workspace_id="b", db_path=db)
    aa = profiles.add_account(pa.id, "telegram", "A-label", auth_type="api", config={"_needsReconnect": True}, db_path=db)
    ab = profiles.add_account(pb.id, "youtube", "B-private-label", auth_type="api", config={"_needsReconnect": True}, db_path=db)
    visible = add_target(db, "blocked", workspace="a", workspace_id="b", account_ref=f"account:{aa.id}", last_error="A error")
    # A corrupt reference must never cause the other tenant's account name to leak.
    add_target(db, "needs_review", workspace="a", account_ref=f"account:{ab.id}")
    add_target(db, "failed", workspace="b", platform="youtube", finished_at=stamp(minutes=-1), last_error="B-private-error")
    add_target(db, "running", workspace="b", platform="youtube", started_at=stamp(hours=-10))
    add_target(db, "pending", workspace="b")
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE accounts SET config_json = '{malformed' WHERE id = ?", (ab.id,))
    body = snapshot(db, workspace_id="a")
    assert body["totals"]["total"] == 2
    assert body["totals"]["failed"] == body["queue"]["staleRunning"] == 0
    assert body["recentFailures"] == body["staleTargets"] == body["accountConfigIssues"] == []
    assert [a["id"] for a in body["accountsNeedingReconnect"]] == [aa.id]
    assert any(t["targetId"] == visible and t["accountName"] == "A-label" for t in body["actionTargets"])
    assert "B-private" not in json.dumps(body)
    assert {p["platform"] for p in body["platforms"]} == {"telegram"}
    assert snapshot(db)["totals"]["total"] == 5
    assert snapshot(db, workspace_id="missing")["totals"]["total"] == 0


def test_recent_metrics_and_failure_groups_are_not_capped_to_sample_size(db):
    for _ in range(125):
        add_target(db, "failed", finished_at=stamp(minutes=-5), last_error="provider unavailable", error_category="transient")
    add_target(db, "failed", finished_at=stamp(days=-2), last_error="old failure")
    add_target(db, "failed", finished_at=stamp(days=1), last_error="future timestamp")
    body = snapshot(db, failure_window_hours=1)
    assert body["totals"]["failed"] == 127
    assert body["recent"]["failed"] == 125
    assert len(body["recentFailures"]) == 1
    assert body["recentFailures"][0]["count"] == 125
    assert body["recentFailures"][0]["category"] == "transient"


def test_missing_database_is_not_created_or_claimed_healthy(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        health.publish_health_snapshot(path)
    assert not path.exists()


def test_snapshot_is_read_only(db, monkeypatch):
    add_target(db)
    statements = []
    original = sqlite3.connect
    def connect(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(sqlite3, "connect", connect)
    snapshot(db)
    assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")) for s in statements)


@pytest.fixture
def quiet_monitor(monkeypatch):
    for name in ("_check_stt", "_check_disk", "_check_offload", "_check_inbox_health", "_check_routing"):
        monkeypatch.setattr(monitor, name, Mock(return_value=[]))
    monkeypatch.setattr(monitor, "_check_healthz", Mock(return_value=None))
    monkeypatch.setattr(monitor, "_load_dotenv", lambda: None)


def test_monitor_uses_due_work_and_reports_action_delivery_and_worker_problems(db, tmp_path, quiet_monitor):
    add_target(db, created_at=stamp(hours=-2))
    add_target(db, "awaiting_delivery", delivery_state="awaiting_delivery", next_attempt_at=stamp(hours=-1))
    add_target(db, "blocked")
    add_target(db, "needs_review")
    add_target(db, "running", started_at=stamp(hours=-1), lease_token="secret", lease_expires_at=stamp(minutes=-5))
    worker(db, age=120)
    problems = monitor.collect_problems(db, tmp_path, tmp_path)
    assert {"overdue", "awaiting-delivery", "action-needed", "stale-leases", "workers"} <= problems.keys()
    assert "NOT confirmed delivered" in problems["awaiting-delivery"][0]
    assert all(len(items) <= 6 for items in problems.values())
    assert "publish-failures" not in problems


def test_monitor_future_queue_is_not_an_incident(db, tmp_path, quiet_monitor):
    add_target(db, created_at=stamp(days=-100), schedule_at=stamp(days=50))
    add_target(db, "awaiting_delivery", delivery_state="awaiting_delivery", next_attempt_at=stamp(minutes=2))
    worker(db)
    assert monitor.collect_problems(db, tmp_path, tmp_path) == {}
    with closing(monitor._connect(db)) as conn:
        assert monitor._check_overdue(conn, 30) == []


def test_monitor_failure_and_campaign_text_is_redacted_before_truncation(db, tmp_path, quiet_monitor):
    add_target(db, "failed", finished_at=stamp(minutes=-2), last_error="api_key=" + "SECRETVALUE" * 80)
    profile = profiles.create_profile("Brand", db_path=db)
    with closing(sqlite3.connect(db)) as conn, conn:
        group = conn.execute("INSERT INTO media_groups (name) VALUES ('Media')").lastrowid
        conn.execute("INSERT INTO campaigns (profile_id, media_group_id, status, created_at, last_error) "
                     "VALUES (?, ?, 'needs_review', ?, ?)",
                     (profile.id, group, stamp(minutes=-10), "api_key=" + "SECRETVALUE" * 80))
    worker(db)
    problems = monitor.collect_problems(db, tmp_path, tmp_path)
    assert "publish-failures" in problems
    assert "needs-review" in problems
    assert "SECRETVALUE" not in json.dumps(problems)
    assert "SECRETVALUE" not in monitor._render(problems)[1]
    with closing(monitor._connect(db)) as conn:
        assert "SECRETVALUE" not in str(monitor._check_failed_targets(conn, 60))


def test_monitor_cooldown_status_mode_and_failed_send_are_finite_and_safe(tmp_path, quiet_monitor, monkeypatch, capsys):
    from myUtils import ops_alerts
    state = tmp_path / "monitor.json"
    monkeypatch.setattr(monitor, "STATE_PATH", state)
    problems = {"workers": ["worker stale 95 seconds; api_key=DO_NOT_PRINT"],
                "overdue": ["1 overdue (oldest due 2030-04-05T10:00:00)"]}
    monkeypatch.setattr(monitor, "collect_problems", lambda *a, **k: problems)
    send = Mock(return_value=True)
    monkeypatch.setattr(ops_alerts, "send_ops_alert", send)
    assert monitor.main(["--status"]) == 1
    assert not state.exists()
    send.assert_not_called()
    assert monitor.main([]) == 1
    assert send.call_count == 2
    problems["workers"] = ["worker stale 110 seconds; api_key=DO_NOT_PRINT"]
    problems["overdue"] = ["1 overdue (oldest due 2030-04-05T10:01:00)"]
    assert monitor.main([]) == 1
    assert send.call_count == 2
    assert "DO_NOT_PRINT" not in state.read_text(encoding="utf-8")
    assert "DO_NOT_PRINT" not in capsys.readouterr().out
    assert "DO_NOT_PRINT" not in str(send.call_args_list)
    state.unlink()
    send.return_value = False
    monitor.main([])
    assert json.loads(state.read_text(encoding="utf-8"))["alerts"] == {}


@pytest.fixture
def app():
    instance = Flask("health-reliability-tests")
    instance.config["TESTING"] = True
    instance.register_blueprint(health_bp)
    return instance


def test_liveness_is_only_status_revision_and_never_runs_checks(app):
    check = Mock(side_effect=AssertionError("liveness must not touch dependencies"))
    register_readiness_check(app, "database", check)
    register_readiness_check(app, "worker", check)
    response = app.test_client().get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "revision": "abc123-release"}
    check.assert_not_called()


def test_readiness_requires_database_registration_and_never_creates_missing_db(app, tmp_path):
    client = app.test_client()
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.get_json()["checks"]["database"] == "not_registered"
    missing = tmp_path / "missing.db"
    register_readiness_check(app, "database", lambda: database_ready(missing))
    register_readiness_check(app, "worker", lambda: ("not_required", True))
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.get_json()["checks"]["database"] == "uninitialized"
    assert not missing.exists()


def test_readiness_runs_registered_database_and_worker_checks(app, db):
    db_check = Mock(side_effect=lambda: database_ready(db))
    def worker_check():
        ready = jobs.worker_health(db_path=db)["ok"]
        return ("ok" if ready else "worker_unavailable", ready)
    w_check = Mock(side_effect=worker_check)
    register_readiness_check(app, "database", db_check)
    register_readiness_check(app, "worker", w_check)
    client = app.test_client()
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.get_json()["checks"] == {"database": "ok", "worker": "worker_unavailable"}
    worker(db)
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ready", "revision": "abc123-release",
                                   "checks": {"database": "ok", "worker": "ok"}}
    assert db_check.call_count == w_check.call_count == 2
    worker(db, draining=True)
    assert client.get("/readyz").status_code == 503


def test_worker_registration_is_required_when_deployment_requires_workers(app, monkeypatch):
    register_readiness_check(app, "database", lambda: True)
    monkeypatch.setenv("SAU_REQUIRE_WORKER_HEALTH", "1")
    response = app.test_client().get("/readyz")
    assert response.status_code == 503
    assert response.get_json()["checks"]["worker"] == "not_registered"


@pytest.mark.parametrize("outcome", [("wrong-length",), ("ok", "false"), None, {"ok": True}, "yes"])
def test_malformed_readiness_results_fail_without_500(app, outcome):
    register_readiness_check(app, "database", lambda: outcome)
    response = app.test_client().get("/readyz")
    assert response.status_code == 503
    assert response.get_json()["checks"]["database"] == "invalid_result"


def test_readiness_exception_and_credential_status_are_not_exposed(app, monkeypatch, caplog):
    register_readiness_check(app, "database", Mock(side_effect=ValueError("https://user:SECRET@db.invalid/?token=TOKEN")))
    register_readiness_check(app, "worker", lambda: ("access_token=STATUSSECRET", False))
    monkeypatch.setenv("SAU_BUILD_REVISION", "https://user:BUILDSECRET@private.invalid/revision")
    response = app.test_client().get("/readyz")
    assert response.status_code == 503
    body = response.get_json()
    assert body["checks"] == {"database": "error", "worker": "error"}
    assert body["revision"] == "unknown"
    assert "SECRET" not in json.dumps(body) + caplog.text
    assert "TOKEN" not in json.dumps(body) + caplog.text


def test_incomplete_modern_lease_is_orphaned_without_waiting_for_legacy_age(db):
    worker(db)
    target = add_target(db, "running", started_at=stamp(seconds=-1),
                        lease_owner="worker-a", lease_token="incomplete-token")
    body = snapshot(db)
    assert body["queue"]["orphanedLeases"] == 1
    assert body["staleTargets"][0]["targetId"] == target
    assert body["staleTargets"][0]["reason"] == "orphaned_lease"


def test_unavailable_worker_probe_is_safe_and_does_not_hide_queue_counts(db, monkeypatch):
    add_target(db, "blocked")
    monkeypatch.setattr(jobs, "worker_health", Mock(side_effect=RuntimeError("config=DO_NOT_EXPOSE")))
    body = snapshot(db)
    assert body["totals"]["blocked"] == 1
    assert body["workerHealth"]["ok"] is False
    assert body["workerHealth"]["status"] == "unavailable"
    assert "DO_NOT_EXPOSE" not in json.dumps(body)


def test_detail_limits_do_not_truncate_action_counts(db):
    for _ in range(105):
        add_target(db, "blocked", last_error="operator action required")
    body = snapshot(db)
    assert body["queue"]["actionRequired"] == body["totals"]["blocked"] == 105
    assert body["detailLimit"] == len(body["actionTargets"]) == 100


def test_monitor_database_error_does_not_hide_independent_prep_fault(db, tmp_path, quiet_monitor, monkeypatch):
    monkeypatch.setattr(monitor, "publish_health_snapshot", Mock(side_effect=sqlite3.OperationalError("token=DBSECRET")))
    monkeypatch.setattr(monitor, "_check_needs_review", lambda *a: ["campaign 1 needs review"])
    problems = monitor.collect_problems(db, tmp_path, tmp_path)
    assert "database" in problems and "needs-review" in problems
    assert "DBSECRET" not in json.dumps(problems)
