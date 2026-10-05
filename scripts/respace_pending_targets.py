#!/usr/bin/env python3
"""Space out the already-queued pending targets so no account is jammed.

Dedup removes the same-media duplicates, but different media can still land on
the same minute for one account (the Publish Center's stagger reset on every
submit). This pass walks each account's pending targets in their existing
chronological order and pushes each one forward until it is at least
``SAU_PUBLISH_MIN_GAP_MINUTES`` from the previous booking and within
``SAU_PUBLISH_MAX_PER_ACCOUNT_PER_DAY`` for its day. A target is never moved
earlier, and past-due targets are left alone so the running worker can pick
them up.

Dry-run is the default; ``--apply`` writes the new ``schedule_at`` values.
Always back up ``db/database.db`` first.

Usage::

    python scripts/respace_pending_targets.py            # dry run
    python scripts/respace_pending_targets.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from myUtils import publish_orchestrator  # noqa: E402


DB_PATH = Path(os.environ.get("SAU_DB_PATH") or (REPO_ROOT / "db" / "database.db"))


def _parse(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _connect(db_path: Path):
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def plan_respace(
    *,
    db_path: Path = DB_PATH,
    now: datetime | None = None,
    gap_minutes: int | None = None,
    per_day_cap: int | None = None,
) -> tuple[list[dict], int]:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    gap = timedelta(minutes=max(1, gap_minutes or publish_orchestrator.MIN_GAP_MINUTES))
    cap = max(0, per_day_cap if per_day_cap is not None else publish_orchestrator.MAX_POSTS_PER_ACCOUNT_PER_DAY)

    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, account_ref, schedule_at
            FROM publish_job_targets
            WHERE status = 'pending' AND schedule_at IS NOT NULL
            ORDER BY schedule_at, id
            """
        ).fetchall()
    finally:
        conn.close()

    by_account: dict[str, list] = defaultdict(list)
    for row in rows:
        by_account[row["account_ref"]].append(row)

    changes: list[dict] = []
    left_in_past = 0
    for account_ref, members in by_account.items():
        assigned: list[datetime] = []
        for row in members:
            original = _parse(row["schedule_at"])
            if original is None:
                continue
            if original <= now:
                # Leave the imminent/past-due queue to the running worker.
                assigned.append(original)
                left_in_past += 1
                continue
            candidate = original
            slot_time = candidate.time()
            while True:
                same_day = sum(1 for t in assigned if t.date() == candidate.date())
                conflict = any(
                    abs((t - candidate).total_seconds()) < gap.total_seconds()
                    for t in assigned
                    if t.date() == candidate.date()
                )
                if not conflict and not (cap and same_day >= cap):
                    break
                if cap and same_day >= cap:
                    candidate = datetime.combine(candidate.date() + timedelta(days=1), slot_time)
                else:
                    candidate = candidate + gap
            assigned.append(candidate)
            if candidate != original:
                changes.append(
                    {
                        "target_id": int(row["id"]),
                        "account_ref": account_ref,
                        "old": original.isoformat(timespec="seconds"),
                        "new": candidate.isoformat(timespec="seconds"),
                    }
                )
    return changes, left_in_past


def apply_respace(changes: list[dict], *, db_path: Path = DB_PATH) -> dict:
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        applied = 0
        with conn:
            for item in changes:
                conn.execute(
                    "UPDATE publish_job_targets SET schedule_at = ? WHERE id = ?",
                    (item["new"], item["target_id"]),
                )
                applied += 1
        return {"applied": applied}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=str(DB_PATH))
    parser.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    args = parser.parse_args(argv)

    db_path = Path(args.db_path)
    changes, left_in_past = plan_respace(db_path=db_path)
    by_account: Counter[str] = Counter(item["account_ref"] for item in changes)
    for item in changes[:20]:
        print(f"move {item['target_id']:>6} {item['account_ref']:>12} {item['old']} -> {item['new']}")
    if len(changes) > 20:
        print(f"... and {len(changes) - 20} more")
    print("\nby account:", dict(by_account))
    print(f"past-due left untouched: {left_in_past}")
    print(f"moves: {len(changes)}")
    if not args.apply:
        print("\nDRY RUN: re-run with --apply to write these changes.")
        return 0
    result = apply_respace(changes, db_path=db_path)
    print(f"\napplied={result['applied']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
