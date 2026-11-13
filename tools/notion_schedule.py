#!/usr/bin/env python3
"""Schedule long-form Notion pieces into the SAU publish queue.

Reads the NW and SW Notion databases for rows whose publish column is
``完成未發布``, assigns each one a publish slot that never jams (one post per
brand per day, staggered, never in the past — see ``myUtils.notion_posting``),
enqueues a scheduled ``nw_sw_blog`` job. Queue admission is not delivery:
the Notion publish status remains unchanged until confirmed delivery.

Usage:
    python tools/notion_schedule.py --db nw --dry-run     # show the plan
    python tools/notion_schedule.py --db nw --publish     # enqueue only
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
    """Shared alias-aware bookings, including confirmed/unknown history.

    Invalid/missing history is a refusal, never an empty calendar.
    """
    from contextlib import closing
    from myUtils import destination_pacing, jobs as job_runtime

    path = Path(db_path) if db_path else Path(job_runtime.DB_PATH)
    with closing(destination_pacing.connect_readonly(path)) as conn:
        conn.execute("BEGIN")
        aliases = destination_pacing.account_refs(conn)
        booked = destination_pacing.booked_slots(conn, aliases)
        return {identifier: booked.get(destination_pacing.identity_for_ref(aliases, f"account:{identifier}"), [])
                for identifier in notion_posting.BLOG_ACCOUNTS.values()}


def _existing_page_job(row: dict, db_path: Path | None):
    """Preserve every existing page submission, including failures/uncertainty."""
    from myUtils import jobs as job_runtime, profiles

    db_path = Path(db_path) if db_path else Path(job_runtime.DB_PATH)
    page = str(row.get("page_id") or "")
    if not page:
        raise ValueError("Notion row has no exact page identity")
    job = job_runtime.get_job_by_idempotency_key(f"notion-{page}", db_path=db_path)
    if job is None:
        return None
    account_id = notion_posting.resolve_account(row)
    account = profiles.get_account(account_id, db_path=db_path)
    targets = job_runtime.list_targets(job.id, db_path=db_path)
    if (job.platform != notion_posting.BLOG_PLATFORM or job.profile_id != account.profile_id
            or job.payload.get("notionPageId") != page or job.payload.get("message") != (row.get("content") or "")
            or job.payload.get("title") != (row.get("title") or "") or len(targets) != 1
            or targets[0].account_ref != f"account:{account_id}" or targets[0].file_ref != f"notion:{page}"):
        raise ValueError("Existing Notion page publication conflicts with approved content/destination; review required")
    return job


def _enqueue(slot: notion_posting.Slot, body: str, db_path: Path | None) -> dict:
    from myUtils import jobs as job_runtime, profiles

    db_path = Path(db_path) if db_path else Path(job_runtime.DB_PATH)
    kwargs = notion_posting.build_job_spec(slot, body)
    spec = job_runtime.JobSpec(
        platform=kwargs["platform"],
        payload=kwargs["payload"],
        targets=kwargs["targets"],
        profile_id=kwargs["profile_id"],
        idempotency_key=kwargs["idempotency_key"],
    )
    account = profiles.get_account(slot.account_id, db_path=db_path)
    if account.profile_id != slot.profile_id or account.platform != notion_posting.BLOG_PLATFORM:
        raise ValueError("Notion destination/profile no longer matches the approved row")
    job = job_runtime.enqueue_job(spec, workspace_id=account.workspace_id,
                                  db_path=db_path)
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
        new_rows = []
        for row in rows:
            existing = _existing_page_job(row, db_path)
            if existing is None:
                new_rows.append(row)
            else:
                print(f"   page already owns job {existing.id} ({existing.status}); no new submission")
        rows = new_rows
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
            print("      -> queued; Notion publish status unchanged pending confirmed delivery")
    if total == 0:
        print("nothing to schedule")
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Schedule Notion long-form into SAU")
    ap.add_argument("--db", choices=sorted(DBS), help="single database")
    ap.add_argument("--both", action="store_true", help="both NW and SW")
    ap.add_argument("--publish", action="store_true",
                    help="enqueue jobs; do not mark queued work as published")
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
