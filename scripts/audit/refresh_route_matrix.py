"""Regenerate reports/route-authorization-matrix.csv without losing annotations.

The committed matrix was generated before an earlier refactor, so its
``source_line`` column no longer matches the live app and ``--check`` fails.
This script refreshes every machine-derived column from the live application
while carrying the human annotation columns forward for routes that still
exist, and applies dated overrides for the routes Phase 1 changed.

Run from the repository root:
    python scripts/audit/refresh_route_matrix.py
"""
from __future__ import annotations

import csv
import io
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSV_PATH = ROOT / "reports" / "route-authorization-matrix.csv"
AUTO = ["path", "methods", "endpoint", "source_file", "source_line", "auth_class"]
HEURISTIC = ["tables_detected"]
ANNOTATION = [
    "tables_touched", "ownership_check", "secrets_in_response", "filesystem_access",
    "csrf_needed", "rate_limit_needed", "planned_workspace_scope", "risk", "notes",
]
ALL = AUTO + HEURISTIC + ANNOTATION

DATE = "2026-11-03"

#: Dated overrides for the routes Phase 1 changed. Only the listed columns are
#: replaced; everything else is carried forward.
OVERRIDES: dict[tuple[str, str], dict[str, str]] = {
    ("/deleteFile", "DELETE"): {
        "ownership_check": "route-permission",
        "csrf_needed": "y",
        "rate_limit_needed": "standard",
        "filesystem_access": "y-delete",
        "planned_workspace_scope": "workspace",
        "risk": "P1",
        "notes": (
            f"{DATE} Phase 1: GET removed (now 405); DELETE-only; refuses while unresolved "
            "work references the media; row committed before bytes are unlinked; permission media:write"
        ),
    },
    ("/deleteAccount", "DELETE"): {
        "ownership_check": "route-permission",
        "csrf_needed": "y",
        "rate_limit_needed": "standard",
        "filesystem_access": "y-delete",
        "planned_workspace_scope": "workspace",
        "risk": "P1",
        "notes": (
            f"{DATE} Phase 1: GET removed (now 405); DELETE-only; explicit `type` namespace "
            "(legacy|structured) so overlapping ids cannot delete the wrong row; refuses when a "
            "delivery outcome is unknown; row committed before the cookie file; permission accounts:connect"
        ),
    },
    ("/deleteFiles", "POST"): {
        "ownership_check": "route-permission",
        "csrf_needed": "y",
        "rate_limit_needed": "standard",
        "filesystem_access": "y-delete",
        "planned_workspace_scope": "workspace",
        "risk": "P1",
        "notes": (
            f"{DATE} Phase 1: per-id reference guard; in-use items are reported (reason=in_use) "
            "and left intact rather than partially deleted; permission media:write"
        ),
    },
    ("/downloadCookie", "GET"): {
        "ownership_check": "route-permission+workspace",
        "csrf_needed": "n",
        "rate_limit_needed": "strict",
        "planned_workspace_scope": "owner-only",
        "risk": "P0",
        "notes": (
            f"{DATE} Phase 1: now owner-only via credentials:export (viewer/editor/admin 403) and "
            "still workspace-scoped (foreign path -> 404, never 403); throttled"
        ),
    },
    ("/api/auth/cookies/<int:account_id>/export", "GET"): {
        "ownership_check": "route-permission+workspace",
        "csrf_needed": "n",
        "rate_limit_needed": "strict",
        "planned_workspace_scope": "owner-only",
        "risk": "P0",
        "notes": (
            f"{DATE} Phase 1: owner-only via credentials:export; workspace-scoped; throttled. "
            "The old nonexistent read_cookie_file fallback is already resolved."
        ),
    },
    ("/admin/oauth/status", "GET"): {
        "ownership_check": "route-permission+workspace",
        "csrf_needed": "n",
        "rate_limit_needed": "strict",
        "planned_workspace_scope": "admin-only",
        "risk": "P1",
        "notes": (
            f"{DATE} Phase 1: account lookup and live OAuth state are workspace/platform scoped before "
            "any read; other principals' in-flight state is withheld; permission workspace:manage"
        ),
    },
}


def live_rows() -> list[dict[str, str]]:
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(ROOT / "scripts" / "audit" / "dump_route_matrix.py")],
        capture_output=True, text=True, check=True, cwd=str(ROOT),
    )
    return list(csv.DictReader(io.StringIO(proc.stdout)))


def main() -> int:
    existing: dict[tuple[str, str], dict[str, str]] = {}
    if CSV_PATH.is_file():
        with CSV_PATH.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                existing[(row["path"], row["methods"])] = row

    rows = live_rows()
    merged: list[dict[str, str]] = []
    carried = 0
    overridden = 0
    for row in rows:
        out = {column: row.get(column, "") for column in ALL}
        prior = existing.get((row["path"], row["methods"]))
        if prior:
            for column in ANNOTATION:
                out[column] = prior.get(column, "")
            carried += 1
        override = OVERRIDES.get((row["path"], row["methods"]))
        if override:
            out.update(override)
            overridden += 1
        merged.append(out)

    with CSV_PATH.open("w", encoding="utf-8", newline="\n") as handle:
        writer = csv.DictWriter(handle, fieldnames=ALL)
        writer.writeheader()
        writer.writerows(merged)

    print(f"rows={len(merged)} annotations_carried={carried} overrides_applied={overridden}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
