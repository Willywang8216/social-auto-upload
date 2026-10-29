#!/usr/bin/env python3
"""Humanize drafts that were written before the humanizer was wired in.

The operator asked for the humanizer to apply to *all* writings. New drafts get
it automatically (`sau_backend._generate_account_draft`), but the ones already
queued were written by the old prompt, so this walks the pending campaigns and
rewrites only the fields that actually carry a tell — an LLM call is spent only
where there is something to fix.

    python humanize_existing_drafts.py            # report
    python humanize_existing_drafts.py --apply
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path("/home/will/social-auto-upload")
sys.path.insert(0, str(REPO))

from myUtils import humanize  # noqa: E402

DB = REPO / "db" / "database.db"
FIELDS = ("message", "title", "summary", "description", "firstComment")


def _pending_posts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT DISTINCT cp.id, cp.campaign_id, cp.draft_json
        FROM campaign_posts cp
        JOIN publish_job_targets jt
          ON jt.file_ref = 'campaign_post:' || cp.id AND jt.status = 'pending'
        WHERE cp.draft_json IS NOT NULL AND cp.draft_json != ''
        ORDER BY cp.id
        """
    ).fetchall()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=60)
    posts = _pending_posts(conn)
    if args.limit:
        posts = posts[: args.limit]
    print(f"pending posts with a draft: {len(posts)}")

    checked = changed_fields = changed_posts = failed = 0
    started = time.time()
    for index, post in enumerate(posts, 1):
        try:
            draft = json.loads(post["draft_json"] or "{}")
        except ValueError:
            continue
        if not isinstance(draft, dict):
            continue
        dirty: dict[str, str] = {}
        for field in FIELDS:
            value = draft.get(field)
            if not isinstance(value, str) or not value.strip():
                continue
            checked += 1
            if humanize.find_tells(value):
                rewritten = humanize.humanize_text(value)
                if rewritten != value:
                    dirty[field] = rewritten
        if not dirty:
            continue
        changed_posts += 1
        changed_fields += len(dirty)
        print(f"  [{index}/{len(posts)}] post {post['id']} camp {post['campaign_id']}: "
              f"{', '.join(dirty)}")
        if args.apply:
            draft.update(dirty)
            for attempt in range(4):
                try:
                    conn.execute(
                        "UPDATE campaign_posts SET draft_json=? WHERE id=?",
                        (json.dumps(draft, ensure_ascii=False), post["id"]),
                    )
                    conn.commit()
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc) or attempt == 3:
                        failed += 1
                        print(f"      write failed: {exc}")
                        break
                    time.sleep(2 * (attempt + 1))

    print(f"\nfields checked={checked} rewritten={changed_fields} in {changed_posts} post(s)"
          f"{f', write failures={failed}' if failed else ''} ({time.time() - started:.0f}s)")
    if not args.apply:
        print("(dry run — pass --apply to write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
