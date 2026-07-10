#!/usr/bin/env python
"""Read-only route enumerator for the Phase 0 authorization audit.

Imports the Flask app from ``sau_backend`` and walks ``app.url_map``, emitting
one CSV row per route with machine-derived ("auto") columns filled in and the
human-annotated columns left blank. The committed inventory lives at
``reports/route-authorization-matrix.csv``; humans fill in the annotation
columns there and this script's ``--check`` mode verifies that the auto
columns still match the live application (a cheap route-drift detector for
later phases — a mismatch means the matrix needs regeneration + re-review).

Import safety: importing ``sau_backend`` is side-effect-light (verified for
Phase 0): no DB bootstrap happens at import; the account-maintenance
scheduler is a no-op unless ``SAU_ACCOUNT_MAINTENANCE_INTERVAL_SECONDS`` > 0;
the unconditional ``local-cleanup`` daemon thread sleeps 6h before doing
anything and dies with this short-lived process; ``do_spaces.ensure_bucket()``
returns early when ``DO_SPACES_KEY`` is unset. This script additionally
scrubs the relevant environment variables before importing, never calls any
view function, and never instantiates a test client.

CSV conventions (shared by every reports/*.csv):
  - ``methods`` values are joined with ``|`` (e.g. ``GET|POST``).
  - multi-value cells (``tables_*``, ``secrets_in_response``…) are joined
    with ``;`` so the comma stays a pure column separator.
  - ``auth_class`` is computed against the real ``SecurityPolicy``:
    ``public`` (in PUBLIC_PATHS / PUBLIC_PREFIXES), ``sse-query`` (the
    ``/login`` SSE endpoint, which accepts ``?auth=``), else ``bearer``.
    Caveat recorded once here rather than per-row: when ``SAU_API_TOKENS``
    is unset the backend runs in *open mode* and every route is public.

Usage:
    uv run python scripts/audit/dump_route_matrix.py                # skeleton to stdout
    uv run python scripts/audit/dump_route_matrix.py --out FILE     # skeleton to FILE
    uv run python scripts/audit/dump_route_matrix.py --check        # verify committed CSV
"""

from __future__ import annotations

import argparse
import csv
import inspect
import io
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV = REPO_ROOT / "reports" / "route-authorization-matrix.csv"

AUTO_COLUMNS = ["path", "methods", "endpoint", "source_file", "source_line", "auth_class"]
HEURISTIC_COLUMNS = ["tables_detected"]
ANNOTATION_COLUMNS = [
    "tables_touched",
    "ownership_check",
    "secrets_in_response",
    "filesystem_access",
    "csrf_needed",
    "rate_limit_needed",
    "planned_workspace_scope",
    "risk",
    "notes",
]
ALL_COLUMNS = AUTO_COLUMNS + HEURISTIC_COLUMNS + ANNOTATION_COLUMNS

# Store-module call → tables it touches (heuristic assist for annotators; the
# authoritative value is the hand-confirmed ``tables_touched`` column).
MODULE_TABLE_MAP: dict[str, str] = {
    r"profile_registry\.|profiles\.(create|get|list|update|delete|add_account|find_account|list_accounts|ensure_account|iter_accounts)": "profiles;accounts",
    r"job_runtime\.|jobs\.(enqueue_job|claim_next_targets|get_job|list_jobs)": "publish_jobs;publish_job_targets",
    r"campaign": "campaigns;campaign_artifacts;campaign_posts",
    r"media_groups\.": "media_groups;media_group_items",
    r"media_asset_service\.": "media_assets",
    r"analytics_store\.|analytics_sync\.": "video_analytics_videos;video_analytics_snapshots;analytics_sync_log",
    r"publish_templates\.": "publish_templates",
    r"watermark_service\.": "watermark_configs",
    r"sheet_export_service\.": "sheet_exports",
    r"account_events\.": "account_events",
    r"prepared_publishers\.|prepared_post": "prepared_posts",
    r"tiktok_review\.": "tiktok_oauth_requests;tiktok_review_events",
    r"reddit_review\.": "reddit_oauth_requests",
    r"youtube_review\.": "youtube_oauth_requests",
    r"meta_review\.": "meta_oauth_requests",
    r"threads_review\.": "threads_oauth_requests",
    r"x_review\.": "twitter_oauth_requests",
    r"tiktok_publish_status|publish_status": "tiktok_publish_status",
    r"storage_backend": "storage_backends",
    r"file_records|user_info": "file_records;user_info",
}
SQL_TABLE_RE = re.compile(
    r"(?:FROM|INTO|UPDATE|JOIN)\s+([a-z_][a-z0-9_]*)", re.IGNORECASE
)
KNOWN_TABLES = {
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


def _scrub_environment() -> None:
    """Neutralise import-time side effects before ``import sau_backend``."""
    for var in ("BROWSERLESS_URL", "SAU_API_TOKENS", "DO_SPACES_KEY", "DO_SPACES_SECRET"):
        os.environ.pop(var, None)
    os.environ["SAU_ACCOUNT_MAINTENANCE_INTERVAL_SECONDS"] = "0"


def _load_app():
    _scrub_environment()
    sys.path.insert(0, str(REPO_ROOT))
    import sau_backend  # noqa: PLC0415 — deliberate late import after scrubbing

    return sau_backend.app


def _concretize(rule_path: str) -> str:
    """Replace ``<converter:name>`` segments with a dummy so prefix matching works."""
    return re.sub(r"<[^>]+>", "x", rule_path)


def _auth_class(policy, rule_path: str) -> str:
    if rule_path == "/login":
        return "sse-query"
    if policy.is_public_path(rule_path) or policy.is_public_path(_concretize(rule_path)):
        return "public"
    return "bearer"


def _tables_detected(view_func) -> str:
    try:
        source = inspect.getsource(view_func)
    except (OSError, TypeError):
        return ""
    found: set[str] = set()
    for pattern, tables in MODULE_TABLE_MAP.items():
        if re.search(pattern, source):
            found.update(tables.split(";"))
    for match in SQL_TABLE_RE.finditer(source):
        if match.group(1) in KNOWN_TABLES:
            found.add(match.group(1))
    return ";".join(sorted(found))


def _count_route_decorators() -> int:
    source = (REPO_ROOT / "sau_backend.py").read_text(encoding="utf-8")
    return len(re.findall(r"^@app\.route\(", source, re.MULTILINE))


def enumerate_routes() -> list[dict[str, str]]:
    app = _load_app()
    policy = app.config["SECURITY_POLICY"]
    rows: list[dict[str, str]] = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            # Flask's built-in /static/<filename> rule; it is also a public
            # prefix in myUtils/security.py and is covered by the
            # public-route inventory rather than the route matrix.
            continue
        view = app.view_functions[rule.endpoint]
        code = getattr(view, "__code__", None)
        source_file = (
            str(Path(code.co_filename).resolve().relative_to(REPO_ROOT))
            if code and str(code.co_filename).startswith(str(REPO_ROOT))
            else (code.co_filename if code else "")
        )
        methods = sorted((rule.methods or set()) - {"HEAD", "OPTIONS"})
        rows.append(
            {
                "path": rule.rule,
                "methods": "|".join(methods),
                "endpoint": rule.endpoint,
                "source_file": source_file,
                "source_line": str(code.co_firstlineno) if code else "",
                "auth_class": _auth_class(policy, rule.rule),
                "tables_detected": _tables_detected(view),
                **{col: "" for col in ANNOTATION_COLUMNS},
            }
        )
    rows.sort(key=lambda r: (r["path"], r["methods"]))

    decorator_count = _count_route_decorators()
    if len(rows) != decorator_count:
        raise SystemExit(
            f"route count mismatch: url_map has {len(rows)} app routes but "
            f"sau_backend.py contains {decorator_count} @app.route decorators — "
            "investigate before regenerating the matrix"
        )
    return rows


def write_csv(rows: list[dict[str, str]], out) -> None:
    writer = csv.DictWriter(out, fieldnames=ALL_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)


def check_against(committed_path: Path) -> int:
    live = {(r["path"], r["methods"]): r for r in enumerate_routes()}
    with committed_path.open(newline="", encoding="utf-8") as fh:
        committed = {(r["path"], r["methods"]): r for r in csv.DictReader(fh)}

    problems: list[str] = []
    for key in sorted(set(live) - set(committed)):
        problems.append(f"missing from committed CSV: {key[0]} [{key[1]}]")
    for key in sorted(set(committed) - set(live)):
        problems.append(f"stale row in committed CSV (route gone): {key[0]} [{key[1]}]")
    for key in sorted(set(live) & set(committed)):
        for col in ("endpoint", "source_line", "auth_class"):
            if live[key][col] != committed[key].get(col, ""):
                problems.append(
                    f"{key[0]} [{key[1]}] column {col!r}: "
                    f"committed={committed[key].get(col, '')!r} live={live[key][col]!r}"
                )
    if problems:
        print(f"route matrix drift detected ({len(problems)} problem(s)):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"route matrix OK: {len(live)} routes match {committed_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="write the generated CSV to this path")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed CSV's auto columns against the live app",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV,
        help=f"committed CSV path for --check (default: {DEFAULT_CSV})",
    )
    args = parser.parse_args()

    if args.check:
        return check_against(args.csv)

    rows = enumerate_routes()
    if args.out:
        with args.out.open("w", newline="", encoding="utf-8") as fh:
            write_csv(rows, fh)
        print(f"wrote {len(rows)} routes to {args.out}")
    else:
        buffer = io.StringIO()
        write_csv(rows, buffer)
        sys.stdout.write(buffer.getvalue())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
