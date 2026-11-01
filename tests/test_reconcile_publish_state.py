"""Read-only audits and conservative, all-or-nothing publish metadata repairs."""
from __future__ import annotations

import gc
import json
import sqlite3
from contextlib import closing

import pytest

from db import createTable
from myUtils import jobs
from scripts import reconcile_publish_state as reconcile


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "audit.db"
    createTable.bootstrap(path)
    gc.collect()
    yield path
    gc.collect()


def _job(db_path, key="intent", account="legacy-cookie"):
    return jobs.enqueue_job(jobs.JobSpec(
        "facebook", {"title": "private title", "config": {"accessToken": "SECRET-PAYLOAD"}},
        [(account, "file", "2100-01-01T00:00:00")], idempotency_key=key,
    ), db_path=db_path)


def _execute(db_path, sql, params=()):
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(sql, params)


def _rows(db_path, sql):
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(sql).fetchall()


def test_dry_run_is_read_only_and_includes_proposed_parent_and_account_fixes(db_path):
    job = _job(db_path, account="account:9999")
    _execute(db_path, "UPDATE publish_jobs SET status='running', total_targets=999, completed_targets=5, "
                      "failed_targets=6, finished_at='2000-01-01T00:00:00'")
    _execute(db_path, "UPDATE publish_job_targets SET last_error='SECRET-ERROR', "
                      "checkpoint_json='{\"accessToken\":\"SECRET-CHECKPOINT\"}'")
    before, mtime = db_path.read_bytes(), db_path.stat().st_mtime_ns
    report = reconcile.reconcile_publish_state(db_path)
    assert report["mode"] == "dry_run" and not report["applied"]
    assert report["summary"]["dangling_account_targets"] == 1
    assert report["summary"]["job_corrections"] == 1
    correction = report["job_corrections"][0]
    assert correction["job_id"] == job.id
    assert correction["after"] == {"status": "blocked", "total_targets": 1, "completed_targets": 0,
                                     "failed_targets": 0, "finished_at": None}
    assert report["dangling_account_targets"][0]["to_status"] == "blocked"
    assert db_path.read_bytes() == before and db_path.stat().st_mtime_ns == mtime
    rendered = json.dumps(report)
    assert "SECRET-" not in rendered and "private title" not in rendered
    assert "account:9999" not in rendered and "checkpoint" not in rendered


def test_apply_blocks_only_pre_submission_dangling_targets_and_never_reschedules(db_path):
    expected = ["pending", "retrying", "failed", "cancelled", "succeeded", "running", "awaiting_delivery", "needs_review"]
    target_ids = []
    for index, state in enumerate(expected):
        job = _job(db_path, key=f"job-{index}", account=f"account:{9000 + index}")
        target = jobs.list_targets(job.id, db_path=db_path)[0]
        target_ids.append(target.id)
        _execute(db_path, "UPDATE publish_job_targets SET status=?, attempts=2 WHERE id=?", (state, target.id))
    schedules = _rows(db_path, "SELECT id, schedule_at, attempts FROM publish_job_targets ORDER BY id")
    report = reconcile.reconcile_publish_state(db_path, apply=True)
    assert report["applied"] and report["summary"]["dangling_account_targets"] == 2
    assert [jobs.get_target(i, db_path=db_path).status for i in target_ids] == ["blocked", "blocked", *expected[2:]]
    assert _rows(db_path, "SELECT id, schedule_at, attempts FROM publish_job_targets ORDER BY id") == schedules
    again = reconcile.reconcile_publish_state(db_path, apply=True)
    assert again["summary"]["job_corrections"] == 0
    assert again["summary"]["dangling_account_targets"] == 0


def test_set_null_repairs_are_declared_nullable_only_and_historical_orphans_are_not_deleted(db_path):
    job = _job(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        # Simulate legacy writes with FK enforcement disabled, not a schema patch.
        conn.execute("UPDATE publish_jobs SET profile_id = 9999 WHERE id = ?", (job.id,))
        conn.execute("INSERT INTO media_groups(id, name, primary_video_file_id) VALUES (100, 'private', 9999)")
        conn.execute("INSERT INTO accounts(profile_id, platform, account_name, cookie_path, config_json) "
                     "VALUES (9999, 'facebook', 'PRIVATE-ACCOUNT', '', '{\"accessToken\":\"SECRET-CONFIG\"}')")
        conn.execute("INSERT INTO publish_job_targets(job_id, account_ref, file_ref) VALUES (9999, 'legacy', 'file')")
        # Fixture-only FK variations verify that nullable != automatically repairable.
        conn.execute("CREATE TABLE audit_nullable_cascade(id INTEGER PRIMARY KEY, parent INTEGER "
                     "REFERENCES profiles(id) ON DELETE CASCADE)")
        conn.execute("INSERT INTO audit_nullable_cascade VALUES (1, 9999)")
        conn.execute("CREATE TABLE audit_notnull_set_null(id INTEGER PRIMARY KEY, parent INTEGER NOT NULL "
                     "REFERENCES profiles(id) ON DELETE SET NULL)")
        conn.execute("INSERT INTO audit_notnull_set_null VALUES (1, 9999)")
    dry = reconcile.reconcile_publish_state(db_path)
    assert dry["summary"]["nullable_orphan_repairs"] == 2
    assert dry["summary"]["historical_orphans"] == 4
    actions = {issue["table"]: issue["action"] for issue in dry["foreign_key_issues"]}
    assert actions["publish_jobs"] == actions["media_groups"] == "set_null"
    assert actions["accounts"] == actions["publish_job_targets"] == "report_only"
    assert actions["audit_nullable_cascade"] == actions["audit_notnull_set_null"] == "report_only"
    report = reconcile.reconcile_publish_state(db_path, apply=True)
    assert report["summary"] == dry["summary"]
    assert _rows(db_path, "SELECT profile_id FROM publish_jobs") == [(None,)]
    assert _rows(db_path, "SELECT primary_video_file_id FROM media_groups WHERE id=100") == [(None,)]
    assert _rows(db_path, "SELECT profile_id FROM accounts") == [(9999,)]
    assert _rows(db_path, "SELECT COUNT(*) FROM publish_job_targets WHERE job_id=9999") == [(1,)]
    assert _rows(db_path, "SELECT parent FROM audit_nullable_cascade") == [(9999,)]
    assert _rows(db_path, "SELECT parent FROM audit_notnull_set_null") == [(9999,)]
    assert "SECRET" not in json.dumps(report) and "PRIVATE-ACCOUNT" not in json.dumps(report)


def test_composite_nullable_set_null_repairs_and_partially_null_references(db_path):
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("CREATE TABLE audit_parent(a INTEGER, b INTEGER, PRIMARY KEY(a,b))")
        conn.execute("CREATE TABLE audit_child(id INTEGER PRIMARY KEY, a INTEGER, b INTEGER, "
                     "FOREIGN KEY(a,b) REFERENCES audit_parent(a,b) ON DELETE SET NULL)")
        conn.execute("INSERT INTO audit_child VALUES (1, 999, 999), (2, 999, NULL)")
    report = reconcile.reconcile_publish_state(db_path, apply=True)
    assert report["summary"]["nullable_orphan_repairs"] == 1
    assert _rows(db_path, "SELECT a,b FROM audit_child ORDER BY id") == [(None, None), (999, None)]


@pytest.mark.parametrize("status", ["succeeded", "cancelled", "failed"])
def test_terminal_contradictions_are_flagged_for_review_not_reopened(db_path, status):
    job = _job(db_path)
    _execute(db_path, "UPDATE publish_jobs SET status=?, completed_targets=33, total_targets=99", (status,))
    target_before = jobs.list_targets(job.id, db_path=db_path)[0]
    report = reconcile.reconcile_publish_state(db_path, apply=True)
    refreshed = jobs.get_job(job.id, db_path=db_path)
    assert refreshed.status == status and refreshed.total_targets == 1 and refreshed.completed_targets == 0
    assert report["needs_review"][0]["disposition"] == "needs_review"
    assert "terminal_contradiction" in report["needs_review"][0]["reasons"]
    assert report["needs_review"][0]["terminal_intent_preserved"]
    assert jobs.get_target(target_before.id, db_path=db_path) == target_before
    assert not jobs.has_claimable_targets(db_path=db_path)


def test_empty_jobs_need_review_and_active_job_finished_at_is_cleared(db_path):
    empty = _job(db_path, key="empty")
    active = _job(db_path, key="active")
    _execute(db_path, "DELETE FROM publish_job_targets WHERE job_id=?", (empty.id,))
    _execute(db_path, "UPDATE publish_job_targets SET status='running' WHERE job_id=?", (active.id,))
    _execute(db_path, "UPDATE publish_jobs SET finished_at='2000-01-01T00:00:00'")
    reconcile.reconcile_publish_state(db_path, apply=True)
    assert jobs.get_job(empty.id, db_path=db_path).status == "needs_review"
    assert jobs.get_job(empty.id, db_path=db_path).total_targets == 0
    assert jobs.get_job(active.id, db_path=db_path).status == "running"
    assert jobs.get_job(active.id, db_path=db_path).finished_at is None


def test_apply_rolls_back_blocking_and_fk_repairs_together(db_path, monkeypatch):
    _job(db_path, account="account:9999")
    _execute(db_path, "UPDATE publish_jobs SET profile_id=9999")
    before = db_path.read_bytes()
    def fail(*args, **kwargs):
        raise RuntimeError("injected")
    monkeypatch.setattr(reconcile, "_reconcile_jobs", fail)
    with pytest.raises(RuntimeError):
        reconcile.reconcile_publish_state(db_path, apply=True)
    assert db_path.read_bytes() == before
    assert _rows(db_path, "SELECT status FROM publish_job_targets") == [("pending",)]
    assert _rows(db_path, "SELECT profile_id FROM publish_jobs") == [(9999,)]


def test_report_is_bounded_but_all_changes_are_counted_and_applied(db_path, monkeypatch):
    for index in range(3):
        _job(db_path, key=f"job-{index}", account=f"account:{9990 + index}")
    monkeypatch.setattr(reconcile, "REPORT_DETAIL_LIMIT", 1)
    report = reconcile.reconcile_publish_state(db_path, apply=True)
    assert report["summary"]["dangling_account_targets"] == 3
    assert len(report["dangling_account_targets"]) == 1
    assert report["truncated"]["dangling_account_targets"]
    assert _rows(db_path, "SELECT COUNT(*) FROM publish_job_targets WHERE status='blocked'") == [(3,)]


def test_cli_requires_db_is_dry_by_default_and_writes_safe_report(db_path, tmp_path, capsys):
    _job(db_path, account="account:9999")
    with pytest.raises(SystemExit) as error:
        reconcile.main([])
    assert error.value.code == 2
    capsys.readouterr()
    report_path = tmp_path / "report.json"
    before = db_path.read_bytes()
    assert reconcile.main(["--db", str(db_path), "--report", str(report_path)]) == 0
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert not written["applied"] and written["mode"] == "dry_run"
    assert db_path.read_bytes() == before
    assert json.loads(capsys.readouterr().out) == written
    with pytest.raises(SystemExit):
        reconcile.main(["--db", str(db_path), "--report", str(db_path), "--apply"])
    assert db_path.read_bytes() == before
    with pytest.raises(ValueError):
        reconcile.reconcile_publish_state(tmp_path / "does-not-exist.db")
    assert not (tmp_path / "does-not-exist.db").exists()


def test_accepts_online_sqlite_backup_without_changing_live_source(db_path, tmp_path):
    _job(db_path, account="account:9999")
    backup = tmp_path / "online-snapshot.db"
    with closing(sqlite3.connect(db_path)) as source, closing(sqlite3.connect(backup)) as destination:
        source.backup(destination)
    before = db_path.read_bytes()
    report = reconcile.reconcile_publish_state(backup, apply=True)
    assert report["summary"]["dangling_account_targets"] == 1
    assert db_path.read_bytes() == before
    assert _rows(db_path, "SELECT status FROM publish_job_targets") == [("pending",)]


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
@pytest.mark.parametrize("unknown_finish", [None, ""])
def test_recount_preserves_unknown_historical_finish_time(db_path, status, unknown_finish):
    job = _job(db_path)
    _execute(db_path, "UPDATE publish_job_targets SET status=?, finished_at='2020-01-01T00:00:00'", (status,))
    _execute(db_path, "UPDATE publish_jobs SET status=?, finished_at=?, total_targets=99", (status, unknown_finish))
    targets_before = _rows(db_path, "SELECT * FROM publish_job_targets")
    report = reconcile.reconcile_publish_state(db_path, apply=True)
    repaired = jobs.get_job(job.id, db_path=db_path)
    assert repaired.status == status
    assert repaired.finished_at == unknown_finish
    assert repaired.total_targets == 1
    assert "missing_terminal_timestamp" in report["needs_review"][0]["reasons"]
    assert _rows(db_path, "SELECT * FROM publish_job_targets") == targets_before
    assert reconcile.reconcile_publish_state(db_path, apply=True)["summary"]["job_corrections"] == 0


def test_new_terminal_transition_still_records_completion_time():
    result = jobs.aggregate_job_state({"status": "running", "finished_at": None}, {"succeeded": 1},
                                      now="2030-01-01T12:00:00")
    assert result["status"] == "succeeded"
    assert result["finished_at"] == "2030-01-01T12:00:00"
    assert "missing_terminal_timestamp" not in result["review_reasons"]
