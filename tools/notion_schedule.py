#!/usr/bin/env python3
"""Schedule long-form Notion pieces into the SAU publish queue.

Reads the NW and SW Notion databases for rows whose publish column is
``完成未發布``, assigns each one a publish slot that never jams (one post per
brand per day, staggered, never in the past — see ``myUtils.notion_posting``),
enqueues a scheduled ``nw_sw_blog`` job, and flips the Notion row to
``完成已發布`` once it is queued.

Usage:
    python tools/notion_schedule.py --db nw --dry-run     # show the plan
    python tools/notion_schedule.py --db nw --publish     # enqueue + mark
    python tools/notion_schedule.py --both --publish      # both databases
    python tools/notion_schedule.py --density-check       # next free slots

Environment:
    NOTION_TOKEN / NOTION_TOKEN_FILE   integration token (see tools/notion/accessors.py)
    SAU_DB_PATH                        publish DB (defaults to the project DB)

Exit codes: 0 ok, 2 nothing to do, 1 error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from myUtils import notion_posting  # noqa: E402
from tools.notion.accessors import NotionAccessor, NotionError  # noqa: E402

DBS = {"nw": notion_posting.NW_DB_ID, "sw": notion_posting.SW_DB_ID}


def _booked_by_account(db_path: Path | None) -> dict[int, list[str]]:
    """Existing scheduled/queued blog targets, so new slots never collide."""
    import sqlite3
    from myUtils import jobs as job_runtime

    path = Path(db_path) if db_path else Path(job_runtime.DB_PATH)
    if not path.exists():
        return {}
    booked: dict[int, list[str]] = {}
    try:
        with sqlite3.connect(str(path)) as conn:
            rows = conn.execute(
                """
                SELECT t.account_ref, t.schedule_at
                FROM publish_job_targets t
                JOIN publish_jobs j ON j.id = t.job_id
                WHERE j.platform = ?
                  AND t.schedule_at IS NOT NULL AND t.schedule_at != ''
                  AND t.status IN ('pending', 'running', 'retrying')
                """,
                (notion_posting.BLOG_PLATFORM,),
            ).fetchall()
    except sqlite3.Error:
        return {}
    for account_ref, schedule_at in rows:
        try:
            account_id = int(str(account_ref).split(":", 1)[1])
        except (IndexError, ValueError):
            continue
        booked.setdefault(account_id, []).append(str(schedule_at))
    return booked


def _enqueue(slot: notion_posting.Slot, body: str, db_path: Path | None) -> dict:
    from myUtils import jobs as job_runtime

    kwargs = notion_posting.build_job_spec(slot, body)
    spec = job_runtime.JobSpec(
        platform=kwargs["platform"],
        payload=kwargs["payload"],
        targets=kwargs["targets"],
        profile_id=kwargs["profile_id"],
        idempotency_key=kwargs["idempotency_key"],
    )
    job = job_runtime.enqueue_job(
        spec, db_path=Path(db_path) if db_path else None)
    return {"jobId": job.id, "targets": job.total_targets}


def run(db_keys: list[str], *, publish: bool, dry_run: bool, db_path: Path | None) -> int:
    accessor = NotionAccessor()
    total = 0
    for key in db_keys:
        db_id = DBS[key]
        try:
            rows = accessor.fetch_pending_rows(db_id)
        except NotionError as exc:
            print(f"[{key}] Notion error: {exc}", file=sys.stderr)
            return 1
        for row in rows:
            row.setdefault("brand", key)
        print(f"[{key}] {len(rows)} row(s) with publish={notion_posting.STATUS_FINISHED_UNPUBLISHED}")
        if not rows:
            continue
        booked = _booked_by_account(db_path)
        slots = notion_posting.select_rows_to_schedule(
            rows, booked, now=datetime.now(timezone.utc).replace(tzinfo=None))
        for slot, row in zip(slots, rows):
            print(f"   {slot.scheduled_at}  {slot.brand}/{slot.account_id}  {slot.title[:60]}")
            total += 1
            if dry_run or not publish:
                continue
            result = _enqueue(slot, row.get("content") or "", db_path)
            print(f"      -> job {result['jobId']} queued")
            try:
                accessor.mark_published(slot.page_id, db_id)
                print("      -> notion marked 完成已發布")
            except NotionError as exc:
                print(f"      !! enqueued but Notion update failed: {exc}", file=sys.stderr)
    if total == 0:
        print("nothing to schedule")
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Schedule Notion long-form into SAU")
    ap.add_argument("--db", choices=sorted(DBS), help="single database")
    ap.add_argument("--both", action="store_true", help="both NW and SW")
    ap.add_argument("--publish", action="store_true",
                    help="enqueue the jobs and flip Notion to 完成已發布")
    ap.add_argument("--dry-run", action="store_true", help="plan only (default)")
    ap.add_argument("--density-check", action="store_true",
                    help="print the next free slot per brand and exit")
    ap.add_argument("--db-path", default=os.environ.get("SAU_DB_PATH"),
                    help="publish DB path (defaults to the project DB)")
    args = ap.parse_args(argv)

    if not args.db and not args.both:
        ap.error("pass --db nw|sw or --both")
    keys = ["nw", "sw"] if args.both else [args.db]
    db_path = Path(args.db_path) if args.db_path else None

    if args.density_check:
        booked = _booked_by_account(db_path)
        for key in keys:
            print(json.dumps({
                "brand": key,
                "accountId": notion_posting.BLOG_ACCOUNTS[key],
                "bookedDays": sorted({b[:10] for b in booked.get(notion_posting.BLOG_ACCOUNTS[key], [])}),
            }, ensure_ascii=False))
        return 0

    dry_run = args.dry_run or not args.publish
    return run(keys, publish=args.publish, dry_run=dry_run, db_path=db_path)


if __name__ == "__main__":
    raise SystemExit(main())
