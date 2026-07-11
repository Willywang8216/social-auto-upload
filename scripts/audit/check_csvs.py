#!/usr/bin/env python
"""Validator for the Phase 0 audit inventories under ``reports/``.

Checks, for every committed CSV: exact header schema, enum-valued columns,
required non-blank annotation columns, row counts, and coverage of the
security-relevant facts each inventory exists to capture (e.g. every entry of
``myUtils.security.PUBLIC_PATHS`` must appear in the public-route inventory).

Exit 0 = all inventories valid. Any problem prints a line and exits 1.

Usage:
    uv run python scripts/audit/check_csvs.py
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS = REPO_ROOT / "reports"
sys.path.insert(0, str(REPO_ROOT))

from myUtils.security import PUBLIC_PATHS, PUBLIC_PREFIXES  # noqa: E402

PROBLEMS: list[str] = []


def problem(msg: str) -> None:
    PROBLEMS.append(msg)


def load(name: str) -> list[dict[str, str]]:
    path = REPORTS / name
    if not path.exists():
        problem(f"{name}: file missing")
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def check_header(name: str, rows: list[dict[str, str]], expected: list[str]) -> None:
    if not rows:
        return
    actual = list(rows[0].keys())
    if actual != expected:
        problem(f"{name}: header mismatch\n    expected: {expected}\n    actual:   {actual}")


def check_enum(name: str, rows: list[dict[str, str]], column: str, allowed: set[str],
               multi: bool = False, allow_blank: bool = False) -> None:
    for i, row in enumerate(rows, start=2):  # header is line 1
        value = (row.get(column) or "").strip()
        if not value:
            if not allow_blank:
                problem(f"{name}:{i}: column {column!r} is blank")
            continue
        values = value.split(";") if multi else [value]
        for v in values:
            if v.strip() not in allowed:
                problem(f"{name}:{i}: column {column!r} has invalid value {v.strip()!r} "
                        f"(allowed: {sorted(allowed)})")


def check_nonblank(name: str, rows: list[dict[str, str]], columns: list[str]) -> None:
    for i, row in enumerate(rows, start=2):
        for col in columns:
            if not (row.get(col) or "").strip():
                problem(f"{name}:{i}: required column {col!r} is blank")


def check_coverage(name: str, rows: list[dict[str, str]], needles: list[str]) -> None:
    haystack = "\n".join(",".join(row.values()) for row in rows)
    for needle in needles:
        if needle not in haystack:
            problem(f"{name}: expected coverage of {needle!r} but no row mentions it")


def count_route_decorators() -> int:
    total = 0
    for rel in ("sau_backend.py", "sau_app/health.py"):
        path = REPO_ROOT / rel
        if not path.exists():
            continue
        total += len(re.findall(r"^@\w+\.route\(", path.read_text(encoding="utf-8"), re.MULTILINE))
    return total


def main() -> int:
    # --- route-authorization-matrix.csv -----------------------------------
    name = "route-authorization-matrix.csv"
    rows = load(name)
    check_header(name, rows, [
        "path", "methods", "endpoint", "source_file", "source_line", "auth_class",
        "tables_detected", "tables_touched", "ownership_check", "secrets_in_response",
        "filesystem_access", "csrf_needed", "rate_limit_needed",
        "planned_workspace_scope", "risk", "notes",
    ])
    expected_routes = count_route_decorators()
    if rows and len(rows) != expected_routes:
        problem(f"{name}: {len(rows)} rows but sau_backend.py has {expected_routes} @app.route decorators")
    check_enum(name, rows, "auth_class", {"public", "bearer", "sse-query"})
    check_enum(name, rows, "ownership_check",
               {"none", "profile-scoped", "account-scoped", "n/a-no-data"})
    check_enum(name, rows, "secrets_in_response",
               {"none", "oauth_tokens", "cookies", "storage_keys", "config_json", "oauth_state"},
               multi=True)
    check_enum(name, rows, "csrf_needed", {"y", "n"})
    check_enum(name, rows, "rate_limit_needed", {"none", "standard", "strict"})
    check_enum(name, rows, "planned_workspace_scope",
               {"workspace", "workspace-via-profile", "public-keep", "public-remove",
                "admin-only", "system"})
    check_enum(name, rows, "risk", {"P0", "P1", "P2"})

    # --- table-ownership-inventory.csv -------------------------------------
    name = "table-ownership-inventory.csv"
    rows = load(name)
    check_header(name, rows, [
        "table", "defined_in", "current_ownership_column", "ownership_nullable",
        "planned_scoping", "backfill_source", "constraints_to_change", "notes",
    ])
    known_tables = {
        "user_info", "file_records", "profiles", "accounts", "publish_jobs",
        "publish_job_targets", "media_groups", "media_group_items", "campaigns",
        "campaign_artifacts", "campaign_posts", "tiktok_oauth_requests",
        "reddit_oauth_requests", "youtube_oauth_requests", "meta_oauth_requests",
        "threads_oauth_requests", "twitter_oauth_requests", "tiktok_review_events",
        "account_events", "video_analytics_videos", "video_analytics_snapshots",
        "analytics_sync_log", "storage_backends", "tiktok_publish_status",
        "publish_templates", "watermark_configs", "media_assets", "sheet_exports",
        "prepared_posts", "alembic_version",
    }
    if rows:
        listed = [row["table"].strip() for row in rows]
        if len(listed) != len(set(listed)):
            problem(f"{name}: duplicate table rows")
        missing = known_tables - set(listed)
        extra = set(listed) - known_tables
        if missing:
            problem(f"{name}: tables missing from inventory: {sorted(missing)}")
        if extra:
            problem(f"{name}: unknown tables in inventory: {sorted(extra)}")
    check_enum(name, rows, "ownership_nullable", {"y", "n", "n/a"})
    check_enum(name, rows, "planned_scoping",
               {"direct-workspace_id", "via-profiles", "via-publish_jobs",
                "via-accounts", "via-campaigns", "via-media_groups",
                "global-config", "system"})

    # --- credential-storage-inventory.csv -----------------------------------
    name = "credential-storage-inventory.csv"
    rows = load(name)
    check_header(name, rows, [
        "credential_type", "platform", "storage_location", "location_kind",
        "encryption", "written_by", "returned_by_routes", "exposure_risk",
        "remediation_phase", "notes",
    ])
    check_enum(name, rows, "location_kind",
               {"db-column", "file", "env", "in-memory", "browser-storage"})
    check_enum(name, rows, "exposure_risk", {"P0", "P1", "P2"})
    check_coverage(name, rows, [
        "accounts.config_json", "storage_backends", "localStorage",
        "SAU_API_TOKENS", "SAU_COOKIE_ENCRYPTION_KEY", "_PATREON_OAUTH_REQUESTS",
        "cookiesFile", "/api/auth/cookies", "/downloadCookie", "_account_payload",
    ])

    # --- public-route-inventory.csv -----------------------------------------
    name = "public-route-inventory.csv"
    rows = load(name)
    check_header(name, rows, [
        "path_or_prefix", "match_type", "handler_endpoint", "reason_public",
        "data_exposed", "abuse_risk", "keep_public_in_target", "mitigation", "notes",
    ])
    check_enum(name, rows, "match_type", {"exact", "prefix", "sse-query"})
    check_enum(name, rows, "keep_public_in_target", {"y", "n", "conditional"})
    if rows:
        covered = {row["path_or_prefix"].strip() for row in rows}
        for path in PUBLIC_PATHS:
            normalized = path.rstrip("/") or "/"
            if normalized not in covered and path not in covered:
                problem(f"{name}: PUBLIC_PATHS entry {path!r} not covered")
        for prefix in PUBLIC_PREFIXES:
            if prefix not in covered:
                problem(f"{name}: PUBLIC_PREFIXES entry {prefix!r} not covered")
        if not any(row["match_type"].strip() == "sse-query" for row in rows):
            problem(f"{name}: missing the /login sse-query row")

    # --- filesystem-path-inventory.csv ---------------------------------------
    name = "filesystem-path-inventory.csv"
    rows = load(name)
    check_header(name, rows, [
        "path_pattern", "base", "purpose", "constructed_at", "tenancy_in_path",
        "user_controlled_segments", "served_by_route", "planned_layout", "notes",
    ])
    check_coverage(name, rows, [
        "videoFile", "uploads/", "cookies/", "cookiesFile", "logs/jobs",
        "db/database.db",
    ])

    # --- oauth-flow-inventory.csv ---------------------------------------------
    name = "oauth-flow-inventory.csv"
    rows = load(name)
    check_header(name, rows, [
        "platform", "start_route", "callback_route", "state_storage", "state_expiry",
        "redirect_uri_source", "callback_host_default", "postmessage_target_origin",
        "token_destination", "token_encryption", "multi_tenant_gaps", "notes",
    ])
    if rows:
        platforms = {row["platform"].strip() for row in rows}
        expected_platforms = {"meta", "youtube", "reddit", "twitter", "threads",
                              "tiktok", "patreon"}
        if platforms != expected_platforms:
            problem(f"{name}: platforms {sorted(platforms)} != expected {sorted(expected_platforms)}")

    if PROBLEMS:
        print(f"{len(PROBLEMS)} problem(s) found:", file=sys.stderr)
        for entry in PROBLEMS:
            print(f"  - {entry}", file=sys.stderr)
        return 1
    print("all report CSVs valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
