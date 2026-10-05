#!/usr/bin/env python3
"""Backfill a new account into already-queued campaigns.

When a destination account is added after campaigns were queued (e.g. the new
NW Chinese Telegram channel), its targets do not exist on those campaigns. This
copies the copy-bearing payload from a sibling account of the same profile and
language to the new account, scheduled at the sibling's time.

It reuses the sibling's draft verbatim (same brand + language), creates a
``campaign_post`` for the new account and enqueues a job for it. The
idempotency key ``backfill-<to>-<source-target-id>`` makes a re-run a no-op.

Dry-run is the default.

Usage::

    python scripts/backfill_account_targets.py --to-account 127 \
        --from-accounts 119,124 --profile 1                 # dry run
    python scripts/backfill_account_targets.py --to-account 127 \
        --from-accounts 119,124 --profile 1 --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from myUtils import campaigns as campaign_store  # noqa: E402
from myUtils import jobs as job_runtime  # noqa: E402
from myUtils import profiles as profile_registry  # noqa: E402

DB_PATH = Path(os.environ.get("SAU_DB_PATH") or (REPO_ROOT / "db" / "database.db"))


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def plan(
    *,
    db_path: Path = DB_PATH,
    to_account: int,
    from_accounts: list[int],
    profile_id: int | None = None,
) -> list[dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT t.id, t.job_id, t.account_ref, t.schedule_at, j.platform,"
            " j.payload_json, j.profile_id "
            "FROM publish_job_targets t JOIN publish_jobs j ON j.id = t.job_id "
            "WHERE t.status = 'pending' AND t.account_ref IN ("
            + ",".join("?" for _ in from_accounts)
            + ") ORDER BY t.schedule_at, t.id",
            [f"account:{a}" for a in from_accounts],
        ).fetchall()
        existing = {
            r["idempotency_key"]
            for r in conn.execute("SELECT idempotency_key FROM publish_jobs")
        }
    finally:
        conn.close()

    plan: list[dict] = []
    seen_campaigns: set[int] = set()
    for row in rows:
        if profile_id is not None and int(row["profile_id"] or 0) != int(profile_id):
            continue
        key = f"backfill-{to_account}-{row['id']}"
        if key in existing:
            continue
        payload = json.loads(row["payload_json"] or "{}")
        # The same campaign usually has both a Bluesky and an X zh source; only
        # one destination target should be created per campaign.
        campaign_id = payload.get("campaignId")
        if campaign_id is not None:
            if int(campaign_id) in seen_campaigns:
                continue
            seen_campaigns.add(int(campaign_id))
        plan.append(
            {
                "source_target_id": int(row["id"]),
                "source_job_id": int(row["job_id"]),
                "source_platform": row["platform"],
                "schedule_at": row["schedule_at"],
                "payload": payload,
                "idempotency_key": key,
            }
        )
    return plan


def apply(
    plan: list[dict],
    *,
    db_path: Path = DB_PATH,
    to_account: int,
    profile_id: int,
    platform: str = "telegram",
) -> int:
    created = 0
    for item in plan:
        payload = dict(item["payload"])
        payload["platform"] = platform
        draft = payload.get("draft") if isinstance(payload.get("draft"), dict) else {}
        campaign_id = payload.get("campaignId")
        post = campaign_store.add_campaign_post(
            int(campaign_id),
            platform,
            account_ids=[to_account],
            draft=draft,
            status=campaign_store.CAMPAIGN_POST_READY,
            db_path=db_path,
        )
        job_runtime.enqueue_job(
            job_runtime.JobSpec(
                platform=platform,
                payload=payload,
                targets=[(f"account:{to_account}", f"campaign_post:{post.id}", item["schedule_at"])],
                profile_id=profile_id,
                idempotency_key=item["idempotency_key"],
            ),
            db_path=db_path,
        )
        created += 1
    return created


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=str(DB_PATH))
    parser.add_argument("--to-account", type=int, required=True)
    parser.add_argument("--from-accounts", required=True, help="comma-separated account ids")
    parser.add_argument("--profile", type=int, default=None)
    parser.add_argument("--platform", default="telegram")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    db_path = Path(args.db_path)
    from_accounts = [int(v) for v in args.from_accounts.split(",") if v.strip()]
    target = profile_registry.get_account(args.to_account, db_path=db_path)
    profile_id = args.profile if args.profile is not None else target.profile_id
    items = plan(
        db_path=db_path,
        to_account=args.to_account,
        from_accounts=from_accounts,
        profile_id=profile_id,
    )
    print(f"backfill plan for account {args.to_account} ({target.account_name}): {len(items)}")
    for item in items[:8]:
        print(f"  from target {item['source_target_id']:>6} {item['source_platform']:>10} "
              f"@ {item['schedule_at']} key={item['idempotency_key']}")
    if len(items) > 8:
        print(f"  ... and {len(items) - 8} more")
    if not args.apply:
        print("\nDRY RUN: re-run with --apply to create the targets.")
        return 0
    created = apply(items, db_path=db_path, to_account=args.to_account,
                    profile_id=profile_id, platform=args.platform)
    print(f"\ncreated={created}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
