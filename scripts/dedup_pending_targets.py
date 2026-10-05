#!/usr/bin/env python3
"""Cancel duplicate pending targets so the same media never posts twice to the
same account on the same day.

The publish-failure churn of early October queued the same video/image to the
same account through several campaigns, landing up to four targets on the exact
same minute. This script groups pending targets by account + normalised media
name and keeps only the earliest scheduled one per calendar day; it leaves
groups that deliberately span different days (a monthly re-post) alone unless
``--include-cross-day`` is given.

Dry-run is the default and nothing is written until ``--apply`` is passed. Use
the supported ``jobs.cancel_target`` path so the parent job counters are
recomputed correctly.

Usage::

    python scripts/dedup_pending_targets.py                 # dry run
    python scripts/dedup_pending_targets.py --apply
    python scripts/dedup_pending_targets.py --include-cross-day --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from myUtils import jobs as job_runtime  # noqa: E402


DB_PATH = Path(os.environ.get("SAU_DB_PATH") or (REPO_ROOT / "db" / "database.db"))

# The pipeline decorates a stored file with ``_pub`` / ``_part1`` suffixes and
# an inbox-cache hash prefix. Strip them so the same source video counts once.
_SUFFIX_RE = re.compile(r"(?:_part\d+|_pub|_sfw_pub|_nsfw_pub)+$", re.IGNORECASE)
_HASH_RE = re.compile(r"^[0-9a-f]{32}_", re.IGNORECASE)


def normalise_media(name: str) -> str:
    stem = Path(str(name)).stem
    stem = _SUFFIX_RE.sub("", stem)
    stem = _HASH_RE.sub("", stem)
    return stem.strip().lower()


def media_key(payload_json: str) -> str | None:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError):
        return None
    for artifact in payload.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        candidate = str(artifact.get("local_path") or artifact.get("public_url") or "")
        if candidate:
            return normalise_media(os.path.basename(candidate))
    return None


def _connect(db_path: Path):
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def plan_dedup(
    *,
    db_path: Path = DB_PATH,
    include_cross_day: bool = False,
) -> list[dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT t.id, t.job_id, t.account_ref, t.schedule_at,
                   j.platform, j.payload_json
            FROM publish_job_targets t JOIN publish_jobs j ON j.id = t.job_id
            WHERE t.status = 'pending'
            ORDER BY t.schedule_at IS NULL, t.schedule_at
            """
        ).fetchall()
    finally:
        conn.close()

    groups: dict[tuple[str, str], list] = defaultdict(list)
    for row in rows:
        key = media_key(row["payload_json"])
        if key:
            groups[(row["account_ref"], key)].append(row)

    plan: list[dict] = []
    skipped_cross_day = 0
    for (account_ref, media), members in groups.items():
        if len(members) < 2:
            continue
        days = {(m["schedule_at"] or "")[:10] for m in members}
        if len(days) > 1 and not include_cross_day:
            skipped_cross_day += 1
            continue
        keeper = members[0]
        for member in members[1:]:
            plan.append(
                {
                    "target_id": int(member["id"]),
                    "job_id": int(member["job_id"]),
                    "account_ref": account_ref,
                    "platform": member["platform"],
                    "media": media,
                    "schedule_at": member["schedule_at"],
                    "kept_target_id": int(keeper["id"]),
                    "kept_schedule_at": keeper["schedule_at"],
                }
            )
    if plan:
        plan.append({"_summary": {"cancellations": len(plan), "cross_day_groups_left": skipped_cross_day}})
    return plan


def apply_dedup(plan: list[dict], *, db_path: Path = DB_PATH) -> dict:
    cancelled, errors = 0, []
    for item in plan:
        if "_summary" in item:
            continue
        try:
            job_runtime.cancel_target(item["target_id"], db_path=db_path)
            cancelled += 1
        except Exception as exc:  # noqa: BLE001 - a per-target error must not abort the batch
            errors.append({"target_id": item["target_id"], "error": str(exc)[:200]})
    return {"cancelled": cancelled, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=str(DB_PATH))
    parser.add_argument(
        "--include-cross-day",
        action="store_true",
        help="also collapse groups that span multiple days",
    )
    parser.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    args = parser.parse_args(argv)

    db_path = Path(args.db_path)
    plan = plan_dedup(db_path=db_path, include_cross_day=args.include_cross_day)
    by_platform: Counter[str] = Counter()
    for item in plan:
        if "_summary" in item:
            continue
        by_platform[item["platform"]] += 1
        print(
            f"cancel target {item['target_id']:>6} {item['platform']:>10} "
            f"{item['account_ref']:>12} @ {item['schedule_at']}  "
            f"(keep {item['kept_target_id']} @ {item['kept_schedule_at']})"
        )
    summary = next((i["_summary"] for i in plan if "_summary" in i), {"cancellations": 0, "cross_day_groups_left": 0})
    print("\nby platform:", dict(by_platform))
    print("summary:", json.dumps(summary))
    if not args.apply:
        print("\nDRY RUN: re-run with --apply to cancel these targets.")
        return 0
    result = apply_dedup(plan, db_path=db_path)
    print(f"\ncancelled={result['cancelled']} errors={len(result['errors'])}")
    for err in result["errors"]:
        print("  error:", err)
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
