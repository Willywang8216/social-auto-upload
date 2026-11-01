#!/usr/bin/env python3
"""Conservatively reconcile publish metadata, never publish or reschedule work.

Usage: python scripts/reconcile_publish_state.py --db PATH [--apply] [--report PATH]

The default opens SQLite in read-only mode and reports proposed changes from
one consistent read transaction. --apply takes one BEGIN IMMEDIATE transaction.
Use an online SQLite backup/snapshot as --db for an offline audit if desired.
Only cached parent state, dangling pre-submission targets and explicitly nullable
ON DELETE SET NULL references are repaired. Historical CASCADE/NOT NULL orphans
are counted, never deleted. Reports contain IDs/counts, not payloads or accounts.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

# Allow direct script execution without importing the Flask application.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from myUtils import jobs
from myUtils.account_identity import structured_account_id


REPORT_DETAIL_LIMIT = 1000
_BATCH_SIZE = 400
_JOB_FIELDS = ("status", "total_targets", "completed_targets", "failed_targets", "finished_at")


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _detail(report: dict, section: str, item: dict) -> None:
    if len(report[section]) < REPORT_DETAIL_LIMIT:
        report[section].append(item)
    else:
        report["truncated"][section] = True


def _block_dangling_accounts(conn, report, *, apply):
    """Return status-count deltas for dry-run aggregation; apply changes in place."""
    deltas = defaultdict(Counter)
    account_exists = {}
    last_id = 0
    while True:
        rows = conn.execute(
            "SELECT id, job_id, account_ref, status FROM publish_job_targets "
            "WHERE id > ? AND status IN (?, ?) AND account_ref LIKE 'account:%' "
            "ORDER BY id LIMIT ?",
            (last_id, jobs.TARGET_PENDING, jobs.TARGET_RETRYING, _BATCH_SIZE),
        ).fetchall()
        if not rows:
            break
        last_id = rows[-1]["id"]
        for row in rows:
            account_id = structured_account_id(row["account_ref"])
            if account_id not in account_exists:
                account_exists[account_id] = account_id is not None and conn.execute(
                    "SELECT 1 FROM accounts WHERE id = ?", (account_id,),
                ).fetchone() is not None
            if account_exists[account_id]:
                continue
            report["summary"]["dangling_account_targets"] += 1
            _detail(report, "dangling_account_targets", {
                "target_id": row["id"], "job_id": row["job_id"],
                "from_status": row["status"], "to_status": jobs.TARGET_BLOCKED,
                "reason": "missing_account",
            })
            if apply:
                conn.execute(
                    "UPDATE publish_job_targets SET status = ?, error_category = 'missing_account', "
                    "last_error = 'Referenced account is missing; reconnect or review before retrying', "
                    "finished_at = NULL, next_attempt_at = NULL, lease_owner = NULL, "
                    "lease_token = NULL, lease_expires_at = NULL WHERE id = ? AND status = ?",
                    (jobs.TARGET_BLOCKED, row["id"], row["status"]),
                )
            else:
                deltas[row["job_id"]][row["status"]] -= 1
                deltas[row["job_id"]][jobs.TARGET_BLOCKED] += 1
    return deltas


def _foreign_key_catalog(conn, report, *, apply):
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
    )}
    columns = {table: {row["name"]: dict(row) for row in conn.execute(
        f"PRAGMA table_info({_quote(table)})",
    )} for table in tables}
    for table in sorted(tables):
        grouped = defaultdict(list)
        for row in conn.execute(f"PRAGMA foreign_key_list({_quote(table)})"):
            grouped[row["id"]].append(dict(row))
        for foreign_key in grouped.values():
            foreign_key.sort(key=lambda row: row["seq"])
            parent = foreign_key[0]["table"]
            child_columns = [row["from"] for row in foreign_key]
            parent_pk = sorted(
                (col for col in columns.get(parent, {}).values() if col["pk"]),
                key=lambda col: col["pk"],
            )
            parent_columns = [row["to"] or (parent_pk[i]["name"] if i < len(parent_pk) else None)
                              for i, row in enumerate(foreign_key)]
            parent_valid = parent in tables and all(
                column in columns[parent] for column in parent_columns
            )
            # Any NULL component exempts a composite FK in SQLite.
            where = " AND ".join(f"{_quote(table)}.{_quote(col)} IS NOT NULL" for col in child_columns)
            if parent_valid:
                match = " AND ".join(
                    f"p.{_quote(dst)} = {_quote(table)}.{_quote(src)}"
                    for src, dst in zip(child_columns, parent_columns)
                )
                where += f" AND NOT EXISTS (SELECT 1 FROM {_quote(parent)} p WHERE {match})"
            count = conn.execute(f"SELECT COUNT(*) FROM {_quote(table)} WHERE {where}").fetchone()[0]
            if not count:
                continue
            nullable = all(not columns[table][col]["notnull"] and not columns[table][col]["pk"]
                           for col in child_columns)
            on_delete = foreign_key[0]["on_delete"].upper()
            repairable = nullable and on_delete == "SET NULL" and parent_valid
            action = "set_null" if repairable else "report_only"
            summary_key = "nullable_orphan_repairs" if repairable else "historical_orphans"
            report["summary"][summary_key] += count
            _detail(report, "foreign_key_issues", {
                "table": table, "columns": child_columns, "parent_table": parent,
                "on_delete": on_delete, "nullable": nullable, "count": count,
                "action": action, "parent_schema_valid": parent_valid,
            })
            if apply and repairable:
                assignments = ", ".join(f"{_quote(col)} = NULL" for col in child_columns)
                conn.execute(f"UPDATE {_quote(table)} SET {assignments} WHERE {where}")


def _reconcile_jobs(conn, report, deltas, *, apply):
    now = jobs._now_iso()
    last_id = 0
    while True:
        rows = conn.execute(
            f"SELECT id, {', '.join(_JOB_FIELDS)} FROM publish_jobs WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, _BATCH_SIZE),
        ).fetchall()
        if not rows:
            break
        last_id = rows[-1]["id"]
        for row in rows:
            report["summary"]["jobs_scanned"] += 1
            counts = Counter({item["status"]: item["n"] for item in conn.execute(
                "SELECT status, COUNT(*) AS n FROM publish_job_targets WHERE job_id = ? GROUP BY status",
                (row["id"],),
            )})
            counts.update(deltas.get(row["id"], {}))
            state = jobs.aggregate_job_state(row, counts, now=now)
            if state["needs_review"]:
                report["summary"]["needs_review_jobs"] += 1
                _detail(report, "needs_review", {
                    "job_id": row["id"], "disposition": "needs_review",
                    "reasons": state["review_reasons"],
                    "terminal_intent_preserved": row["status"] in jobs.JOB_TERMINAL,
                })
            before = {field: row[field] for field in _JOB_FIELDS}
            after = {field: state[field] for field in _JOB_FIELDS}
            if before == after:
                continue
            report["summary"]["job_corrections"] += 1
            # Unknown/corrupt state labels are not echoed as arbitrary data.
            safe_before = dict(before)
            if safe_before["status"] not in jobs.TARGET_STATES:
                safe_before["status"] = "unknown"
            _detail(report, "job_corrections", {"job_id": row["id"], "before": safe_before, "after": after})
            if apply:
                conn.execute(
                    f"UPDATE publish_jobs SET {', '.join(field + ' = ?' for field in _JOB_FIELDS)} WHERE id = ?",
                    [after[field] for field in _JOB_FIELDS] + [row["id"]],
                )


def reconcile_publish_state(db_path: Path | str, *, apply: bool = False) -> dict:
    """Audit an explicitly selected, migrated DB; optionally apply only safe repairs.

    Reports are bounded to 1,000 details per section; summary counts remain
    complete. No account refs/configs, checkpoints, error texts or payloads are
    included. A failed apply rolls back every change, including FK repairs.
    """
    path = Path(db_path).resolve()
    if not path.is_file():
        raise ValueError("--db must name an existing SQLite database file")
    report = {
        "mode": "apply" if apply else "dry_run", "applied": False,
        "summary": {"jobs_scanned": 0, "job_corrections": 0, "dangling_account_targets": 0,
                    "nullable_orphan_repairs": 0, "historical_orphans": 0, "needs_review_jobs": 0},
        "job_corrections": [], "dangling_account_targets": [], "foreign_key_issues": [],
        "needs_review": [], "truncated": {},
    }
    uri = path.as_uri() + ("?mode=rw" if apply else "?mode=ro")
    with closing(sqlite3.connect(uri, uri=True, timeout=15, isolation_level=None)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if not apply:
            conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        try:
            required = {"lease_token", "checkpoint_json", "next_attempt_at", "error_category", "delivery_state"}
            present = {row["name"] for row in conn.execute("PRAGMA table_info(publish_job_targets)")}
            if not required <= present:
                raise ValueError("Database must be upgraded through 0023_publish_reliability before reconciliation")
            deltas = _block_dangling_accounts(conn, report, apply=apply)
            _foreign_key_catalog(conn, report, apply=apply)
            _reconcile_jobs(conn, report, deltas, apply=apply)
            conn.execute("COMMIT" if apply else "ROLLBACK")
            report["applied"] = bool(apply)
        except BaseException:
            conn.rollback()
            raise
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="Existing migrated SQLite DB or online snapshot")
    parser.add_argument("--apply", action="store_true", help="Apply safe repairs atomically (default: read-only)")
    parser.add_argument("--report", type=Path, help="Optional JSON report path; never contains account/config data")
    args = parser.parse_args(argv)
    if args.report:
        db = args.db.resolve()
        report_path = args.report.resolve()
        reserved = {db, *(Path(str(db) + suffix) for suffix in ("-wal", "-shm", "-journal"))}
        if report_path in reserved or (report_path.is_file() and db.is_file() and report_path.samefile(db)):
            parser.error("--report must not overwrite the database or its journal files")
    try:
        report = reconcile_publish_state(args.db, apply=args.apply)
    except (OSError, sqlite3.Error, ValueError) as exc:
        # SQL errors can contain data; only emit the exception type, not values.
        print(f"Reconciliation failed ({type(exc).__name__}); no repairs committed.", file=sys.stderr)
        return 1
    rendered = json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    if args.report:
        try:
            args.report.write_text(rendered, encoding="utf-8")
        except OSError:
            print("Report write failed; reconciliation completed. Result follows on stdout.", file=sys.stderr)
            print(rendered, end="")
            return 1
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
