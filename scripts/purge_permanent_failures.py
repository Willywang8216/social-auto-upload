#!/usr/bin/env python3
"""Cancel permanently-failed publish targets whose retry can never succeed.

Why
---
`publish_job_targets` accumulates `failed` rows that will never be retried by
the worker (the retry budget is spent) and can never succeed if resubmitted,
because the cause is permanent:

  * the media is gone (absent locally, absent from Drive, no HTTPS URL) — the
    offload/reconnect tooling has already recovered everything recoverable,
  * the platform itself refused: a banned subreddit, a submission rule, a title
    format,
  * the content guard rejected the copy (placeholder, or wrong language),
  * the platform is in a mode that cannot publish (a TikTok app still in
    development mode),
  * the account no longer exists.

They are dead weight: they inflate the failure count the operator sees, they make
`jobs_system_health` unreadable, and they hide genuinely new failures.

What this does
--------------
Sets `status='cancelled'` on targets matching those permanent classes, and
records why. It **never deletes** a row, never touches media, and never touches a
`succeeded`, `pending`, `retrying` or `running` target.

It is deliberately conservative: anything it cannot classify with confidence is
left alone and reported, so a genuinely retryable failure is never buried.

Usage
-----
    python scripts/purge_permanent_failures.py            # report only
    python scripts/purge_permanent_failures.py --apply
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# A failure is permanent when the cause cannot change by trying again. Each
# marker is matched against last_error. Ordered most specific first.
PERMANENT_CLASSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "media-unrecoverable",
        (
            "MediaRestoreError",
            "media file is missing or empty",
            "video artifact not found",
            "requires either a public_url or a local_path",
            "requires at least one video or image",
        ),
    ),
    (
        "platform-refused",
        (
            "SUBREDDIT_NOTALLOWED_BANNED",
            "SUBMIT_VALIDATION",
            "NO_SELFS",
            "cannot post to private",
            "app is in development mode",
            "Unable to fetch video file from URL",
            "failed to process: no diagnostic",
            "failed to process: Error: Media upl",
            "Range Not Satisfiable",
        ),
    ),
    (
        "content-permanent",
        ("content-guard",),
    ),
    (
        "account-gone",
        ("Account not found",),
    ),
)

# Rate limits are NOT permanent - a later attempt can succeed - so they are
# excluded even though they appear as failures.
NEVER_PERMANENT = ("RATELIMIT", "rate limited", "429")


def _db_path() -> Path:
    import os

    configured = os.environ.get("SAU_DB_PATH")
    return Path(configured) if configured else REPO_ROOT / "db" / "database.db"


def classify(message: str) -> str | None:
    """Return the permanent class for an error, or None to leave it alone."""
    text = str(message or "")
    if any(marker in text for marker in NEVER_PERMANENT):
        return None
    for name, markers in PERMANENT_CLASSES:
        if any(marker in text for marker in markers):
            return name
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db = _db_path()
    connection = sqlite3.connect(str(db))
    connection.row_factory = sqlite3.Row

    rows = connection.execute(
        "SELECT id, last_error FROM publish_job_targets WHERE status = 'failed'"
    ).fetchall()

    buckets: dict[str, list[int]] = {}
    review: list[tuple[int, str]] = []
    for row in rows:
        cls = classify(row["last_error"])
        if cls is None:
            review.append((row["id"], str(row["last_error"] or "")[:90]))
        else:
            buckets.setdefault(cls, []).append(row["id"])

    print(f"failed targets: {len(rows)}")
    for name, ids in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        print(f"  {name:<22} {len(ids):4}  -> cancel")
    print(f"  {'NOT CLASSIFIED':<22} {len(review):4}  -> LEFT ALONE (review)")
    for target_id, message in review[:10]:
        print(f"       {target_id}: {message}")

    to_cancel = [i for ids in buckets.values() for i in ids]
    if not args.apply:
        print(f"\nDRY RUN - would cancel {len(to_cancel)} target(s).")
        return 0
    if not to_cancel:
        print("\nNothing to cancel.")
        return 0

    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = db.with_name(f"{db.name}.bak-pre-permfail-{stamp}")
    shutil.copy2(db, backup)
    print(f"database backed up to {backup}")

    cancelled = 0
    for name, ids in buckets.items():
        reason = f"cancelled: permanent failure ({name}) - retry cannot succeed"
        for target_id in ids:
            connection.execute(
                "UPDATE publish_job_targets SET status='cancelled', last_error=? "
                "WHERE id=? AND status='failed'",
                (reason, target_id),
            )
            cancelled += connection.total_changes and 1 or 0
    connection.commit()
    print(f"cancelled {len(to_cancel)} target(s)")

    remaining = connection.execute(
        "SELECT COUNT(*) FROM publish_job_targets WHERE status='failed'"
    ).fetchone()[0]
    print(f"failed targets remaining: {remaining}")
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
