#!/usr/bin/env python3
"""Recover failed publish targets without spamming.

The worker retries a target three times and then, if Sociamonials is enabled,
hands it to the fallback; only a genuine dead end is left ``failed``. This
script finds the *recoverable* failures in a recent window and moves them to
fresh, collision-free time slots, so a daily cron can drain the backlog a few
posts at a time instead of dumping them all at once.

It deliberately does **not** resubmit:

* failures the publisher classified as permanent (banned subreddit, missing
  media, a required-media platform with no media, "reconnect required", ...);
* targets whose media already reached the same account successfully (that would
  be a duplicate post).

Dry-run is the default. Nothing is written until ``--apply`` is passed; the
plan (target id, account, platform, chosen slot, reason) is printed first.

Usage::

    # what would happen for yesterday's failures
    python scripts/recover_failed_targets.py --lookback-days 2

    # apply, spacing posts with the same allocator the Publish Center uses
    python scripts/recover_failed_targets.py --lookback-days 2 --apply

    # one platform only
    python scripts/recover_failed_targets.py --platform twitter --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from myUtils import jobs as job_runtime  # noqa: E402
from myUtils import publish_orchestrator  # noqa: E402


DB_PATH = Path(os.environ.get("SAU_DB_PATH") or (REPO_ROOT / "db" / "database.db"))

# Failures that will not be fixed by trying again. Matched case-insensitively
# against the recorded last_error.
PERMANENT_ERROR_PATTERNS = re.compile(
    r"banned|missing|unsupported|no usable media|requires (an? )?(video|image)|"
    r"no_enabled_accounts|nsfw_no_adult_safe_account|restrict(ed|ion)|"
    r"not allowed|permission denied|forbidden|invalid_grant|"
    r"requires reconnection|reconnect|media\.write|"
    r"requires a local video|no images|SUBMIT_VALIDATION_LINK_WHITELIST|"
    r"\[content-guard\]|"
    # Deterministic media/format refusals that a retry cannot fix.
    r"duration\s+\d+s\s+exceeds|exceeds the\s+\d+\s*(?:s|MB)\s+limit|"
    r"require[s]? Direct Post",
    re.IGNORECASE,
)


_SUFFIX_RE = re.compile(r"(?:_part\d+|_pub|_sfw_pub|_nsfw_pub)+$", re.IGNORECASE)
_HASH_RE = re.compile(r"^[0-9a-f]{32}_", re.IGNORECASE)


def _normalise_media(name: str) -> str:
    stem = Path(str(name)).stem
    stem = _SUFFIX_RE.sub("", stem)
    stem = _HASH_RE.sub("", stem)
    return stem.strip().lower()


def _media_key(payload_json: str) -> str | None:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError):
        return None
    for artifact in payload.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        candidate = str(artifact.get("local_path") or artifact.get("public_url") or "")
        if candidate:
            return _normalise_media(os.path.basename(candidate))
    return None


def _classify(last_error: str | None) -> str:
    if not last_error:
        return "unknown"
    return "permanent" if PERMANENT_ERROR_PATTERNS.search(last_error) else "transient"


def _connect(db_path: Path):
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def plan_recovery(
    *,
    db_path: Path = DB_PATH,
    lookback_days: int = 2,
    platform: str | None = None,
    limit: int | None = None,
    per_day_cap: int | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Return the ordered recovery plan without mutating anything."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = (now - timedelta(days=max(1, lookback_days))).isoformat(timespec="seconds")

    conn = _connect(db_path)
    try:
        succeeded = set()
        for row in conn.execute(
            """
            SELECT t.account_ref, j.payload_json
            FROM publish_job_targets t JOIN publish_jobs j ON j.id = t.job_id
            WHERE t.status = 'succeeded'
            """
        ):
            key = _media_key(row["payload_json"])
            if key:
                succeeded.add((row["account_ref"], key))

        query = """
            SELECT t.id, t.job_id, t.account_ref, t.last_error, t.finished_at,
                   j.platform, j.payload_json
            FROM publish_job_targets t JOIN publish_jobs j ON j.id = t.job_id
            WHERE t.status = 'failed' AND t.finished_at >= ?
        """
        params: list[object] = [cutoff]
        if platform:
            query += " AND j.platform = ?"
            params.append(platform)
        query += " ORDER BY t.finished_at ASC"
        failed = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    booked = publish_orchestrator._load_booked_slots(db_path)
    base_time = (now + timedelta(minutes=5)).replace(second=0, microsecond=0)
    counter = 0
    plan: list[dict] = []
    skipped: Counter[str] = Counter()
    for row in failed:
        media = _media_key(row["payload_json"])
        if media and (row["account_ref"], media) in succeeded:
            skipped["duplicate of a succeeded post"] += 1
            continue
        if _classify(row["last_error"]) == "permanent":
            skipped["permanent failure"] += 1
            continue
        try:
            account_id = int(str(row["account_ref"]).split(":", 1)[1])
        except (IndexError, ValueError):
            skipped["unresolved account"] += 1
            continue
        slot = publish_orchestrator._next_free_slot(
            account_id, base_time, counter, booked, max_per_day=per_day_cap
        )
        counter += 1
        plan.append(
            {
                "target_id": int(row["id"]),
                "job_id": int(row["job_id"]),
                "account_ref": row["account_ref"],
                "platform": row["platform"],
                "slot": slot.isoformat(timespec="seconds") if slot else None,
                "error": (row["last_error"] or "")[:120],
            }
        )
        if limit and len(plan) >= limit:
            break

    if skipped:
        plan.append({"_skipped": dict(skipped)})
    return plan


def apply_recovery(plan: list[dict], *, db_path: Path = DB_PATH) -> dict:
    """Reschedule every planned target. Skips the ``_skipped`` summary entry."""
    applied, errors = 0, []
    for item in plan:
        if "_skipped" in item:
            continue
        slot = item.get("slot")
        if not slot:
            continue
        try:
            job_runtime.reschedule_target(item["target_id"], slot, db_path=db_path)
            applied += 1
        except Exception as exc:  # noqa: BLE001 - report, never abort the batch
            errors.append({"target_id": item["target_id"], "error": str(exc)[:200]})
    return {"applied": applied, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=str(DB_PATH))
    parser.add_argument("--lookback-days", type=int, default=2)
    parser.add_argument("--platform", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--per-day-cap",
        type=int,
        default=4,
        help="posts allowed per account/day when placing recoveries (default 4; 0 = unlimited)",
    )
    parser.add_argument(
        "--apply", action="store_true", help="write the plan (default: dry-run)"
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db_path)
    plan = plan_recovery(
        db_path=db_path,
        lookback_days=args.lookback_days,
        platform=args.platform,
        limit=args.limit,
        per_day_cap=args.per_day_cap,
    )
    for item in plan:
        if "_skipped" in item:
            print("skipped:", json.dumps(item["_skipped"], ensure_ascii=False))
            continue
        print(
            f"target {item['target_id']:>6} {item['platform']:>10} "
            f"{item['account_ref']:>12} -> {item['slot']}  "
            f"({item['error']})"
        )
    if not args.apply:
        print(f"\nDRY RUN: {sum(1 for i in plan if '_skipped' not in i)} target(s) would be rescheduled.")
        print("Re-run with --apply to write these changes.")
        return 0
    result = apply_recovery(plan, db_path=db_path)
    print(f"\napplied={result['applied']} errors={len(result['errors'])}")
    for err in result["errors"]:
        print("  error:", err)
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
