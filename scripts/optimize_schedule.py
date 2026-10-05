#!/usr/bin/env python3
"""Move pending targets into each platform's best-performing UTC windows.

Researched 2026 windows (logs/posting-times-research.md) target the operator's
two audiences: a global English audience (US ET the anchor) and a Traditional
Chinese audience in Taiwan. The current calendar pins almost everything to
13:00 UTC; this spreads each platform's posts across the windows that actually
match its peak engagement, while keeping the anti-spam rules (>= 30 min between
posts to one account, <= 3 posts/account/day) and never moving a post earlier
than its existing date.

Dry-run is the default. ``--apply`` writes the new ``schedule_at`` values.

Usage::

    python scripts/optimize_schedule.py                # dry run
    python scripts/optimize_schedule.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from myUtils import platform_limits  # noqa: E402

DB_PATH = Path(os.environ.get("SAU_DB_PATH") or (REPO_ROOT / "db" / "database.db"))

# Preferred posting hours in UTC per platform. Two windows dominate the
# operator's audience overlap:
#   A) 12:00-14:00 UTC = Taiwan 20:00-22:00 (peak IG/FB) + US ET 08:00-10:00
#   B) 22:00-01:00 UTC = US ET 18:00-21:00 (peak X/IG) + Taiwan 06:00-09:00
# YouTube/LinkedIn lean US business hours; Reddit's peak is US afternoon.
PREFERRED_UTC_HOURS: dict[str, list[int]] = {
    "twitter": [13, 14, 23, 0],
    "bluesky": [13, 22, 23, 0],
    "facebook": [12, 13, 23, 0],
    "instagram": [12, 13, 23, 0],
    "threads": [12, 13, 23, 0],
    "tiktok": [13, 14, 23, 0],
    "youtube": [14, 15, 16],
    "reddit": [13, 14, 22],
    "telegram": [12, 13, 22],
    "linkedin": [13, 14, 15],
    "pinterest": [12, 13, 14],
    "nw_sw_blog": [13],
    "teaching_blog": [13],
}
DEFAULT_HOURS = [13, 23]

MIN_GAP_MINUTES = int(os.environ.get("SAU_PUBLISH_MIN_GAP_MINUTES", "30") or 30)
MAX_PER_DAY = int(os.environ.get("SAU_PUBLISH_MAX_PER_DAY", "3") or 3)


def _connect(db_path: Path):
    import sqlite3

    conn = sqlite3.connect(str(db_path), timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


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


def _candidate_times(platform: str, day, account_id: int) -> list[datetime]:
    hours = PREFERRED_UTC_HOURS.get(str(platform or "").lower(), DEFAULT_HOURS)
    # Stagger accounts inside the hour so a fan-out does not fire every account
    # at :00; 5-minute steps keyed on the account id.
    offset = (int(account_id) % 10) * 5
    candidates = []
    for hour in hours:
        base_day = day
        if hour == 0:
            # 00:00 belongs to the window that starts the previous evening.
            base_day = day
        candidates.append(datetime.combine(base_day, time(hour, 0)) + timedelta(minutes=offset))
    return sorted(candidates)


def plan_optimize(
    *,
    db_path: Path = DB_PATH,
    now: datetime | None = None,
) -> tuple[list[dict], int]:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT t.id, t.account_ref, t.schedule_at, j.platform
            FROM publish_job_targets t JOIN publish_jobs j ON j.id = t.job_id
            WHERE t.status = 'pending' AND t.schedule_at IS NOT NULL
            ORDER BY t.schedule_at, t.id
            """
        ).fetchall()
    finally:
        conn.close()

    by_account: dict[str, list] = defaultdict(list)
    for row in rows:
        by_account[row["account_ref"]].append(row)

    gap = timedelta(minutes=max(1, MIN_GAP_MINUTES))
    cap = max(0, MAX_PER_DAY)
    changes: list[dict] = []
    for account_ref, members in by_account.items():
        try:
            account_id = int(str(account_ref).split(":", 1)[1])
        except (IndexError, ValueError):
            continue
        assigned: list[datetime] = []
        for row in members:
            original = _parse(row["schedule_at"])
            if original is None:
                continue
            if original <= now:
                assigned.append(original)
                continue
            candidate: datetime | None = None
            day = original.date()
            for _ in range(400):  # bounded: 400 days forward
                for slot in _candidate_times(row["platform"], day, account_id):
                    if slot < original or slot <= now:
                        continue
                    same_day = sum(1 for t in assigned if t.date() == slot.date())
                    conflict = any(
                        t.date() == slot.date()
                        and abs((t - slot).total_seconds()) < gap.total_seconds()
                        for t in assigned
                    )
                    if cap and same_day >= cap:
                        continue
                    if conflict:
                        continue
                    candidate = slot
                    break
                if candidate is not None:
                    break
                day = day + timedelta(days=1)
            if candidate is None:
                candidate = original
            assigned.append(candidate)
            if candidate != original:
                changes.append(
                    {
                        "target_id": int(row["id"]),
                        "account_ref": account_ref,
                        "platform": row["platform"],
                        "old": original.isoformat(timespec="seconds"),
                        "new": candidate.isoformat(timespec="seconds"),
                    }
                )
    return changes, sum(1 for row in rows if _parse(row["schedule_at"]) and _parse(row["schedule_at"]) <= now)


def apply_optimize(changes: list[dict], *, db_path: Path = DB_PATH) -> int:
    import sqlite3

    conn = sqlite3.connect(str(db_path), timeout=60)
    try:
        with conn:
            for item in changes:
                conn.execute(
                    "UPDATE publish_job_targets SET schedule_at = ? WHERE id = ?",
                    (item["new"], item["target_id"]),
                )
        return len(changes)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=str(DB_PATH))
    parser.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    args = parser.parse_args(argv)

    changes, past = plan_optimize(db_path=Path(args.db_path))
    by_platform: Counter[str] = Counter(item["platform"] for item in changes)
    window_counts = Counter(item["new"][11:13] for item in changes)
    for item in changes[:15]:
        print(f"move {item['target_id']:>6} {item['platform']:>10} {item['account_ref']:>12} "
              f"{item['old']} -> {item['new']}")
    if len(changes) > 15:
        print(f"... and {len(changes) - 15} more")
    print("\nby platform:", dict(by_platform))
    print("new UTC hours:", dict(sorted(window_counts.items())))
    print(f"past-due left untouched: {past} | moves: {len(changes)}")
    if not args.apply:
        print("\nDRY RUN: re-run with --apply to write these changes.")
        return 0
    applied = apply_optimize(changes, db_path=Path(args.db_path))
    print(f"\napplied={applied}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
