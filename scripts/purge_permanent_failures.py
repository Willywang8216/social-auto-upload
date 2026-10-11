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

A pending target has no ``last_error`` to match, so the ``--pending-media``
mode inspects the job payload instead and cancels only when EVERY artifact is
gone: absent from local disk, absent from the supplied Drive listing, with no
storage mapping and no ``remote_path``, and its ``public_url`` (if any) is
verified dead. It requires ``--drive-listing`` and ``--verify-urls`` so it can
never cancel on a guess:

    python scripts/purge_permanent_failures.py --pending-media \
        --drive-listing /tmp/drive_index.txt --verify-urls
    python scripts/purge_permanent_failures.py --pending-media \
        --drive-listing /tmp/drive_index.txt --verify-urls --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
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
            # A publish target that opens a media path and gets FileNotFoundError
            # is missing an artifact that no retry can recreate (e.g. targets
            # #75/#76, whose /app/uploads PNGs are gone and whose old
            # up.iamwillywang.com URL returns 404).
            "FileNotFoundError",
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
            # The Reddit registry refuses every subreddit on the account for
            # this content (explicit media, or a monetised brand). The rules are
            # content-based, so the same payload is refused on every retry.
            "No subreddit on this account can accept this content",
        ),
    ),
    (
        # The destination cannot serve the content that was submitted, and the
        # payload cannot change: YouTube has no video (the campaign is image
        # only). The orchestrator now skips such destinations at enqueue time;
        # these rows predate that guard.
        "destination-incompatible",
        ("YouTube publish requires a local video artifact",),
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


# Media trees the offloader writes to Drive; the relative path under one of
# these is what a Drive listing addresses.
MEDIA_ROOTS = ("videoFile", "generated", "uploads")


def _local_media_exists(local_path: str) -> bool:
    raw = str(local_path or "")
    if not raw:
        return False
    candidates = [raw]
    if raw.startswith("/app/"):
        candidates.append(raw[len("/app/"):])
    for candidate in candidates:
        try:
            if Path(candidate).is_file() or (REPO_ROOT / candidate).is_file():
                return True
        except OSError:
            continue
    return False


def _drive_has(local_path: str, drive_index: set[str]) -> bool:
    raw = str(local_path or "").replace("\\", "/").lstrip("/")
    for root in MEDIA_ROOTS:
        marker = f"/{root}/"
        if marker in f"/{raw}":
            tail = raw.split(marker, 1)[1]
            if f"{root}/{tail}" in drive_index:
                return True
    return False


def _storage_route(
    connection: sqlite3.Connection,
    local_path: str,
    source_file_record_id: object,
) -> bool:
    """True when a file_records row gives a usable restore route."""
    rows: list[sqlite3.Row] = []
    if source_file_record_id is not None:
        rows = connection.execute(
            "SELECT storage_key, storage_backend_id, storage_cdn_url "
            "FROM file_records WHERE id = ?",
            (int(source_file_record_id),),
        ).fetchall()
    if not rows and local_path:
        raw = str(local_path)
        rel = raw[len("/app/"):] if raw.startswith("/app/") else raw
        candidates = [raw, rel, f"/app/{rel}"]
        placeholders = ",".join("?" for _ in candidates)
        rows = connection.execute(
            f"SELECT storage_key, storage_backend_id, storage_cdn_url "
            f"FROM file_records WHERE file_path IN ({placeholders})",
            tuple(candidates),
        ).fetchall()
    for storage_key, backend_id, cdn_url in rows:
        if (storage_key and backend_id) or cdn_url:
            return True
    return False


def _url_is_live(url: str, *, timeout: int = 20) -> bool | None:
    """True/False when the URL answered, None when it could not be checked."""
    request = urllib.request.Request(
        url, method="GET", headers={"Range": "bytes=0-0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", response.getcode()))
            return 200 <= status < 400
    except urllib.error.HTTPError as exc:
        return 200 <= int(exc.code) < 400
    except Exception:  # noqa: BLE001 - a network blip is not proof of death
        return None


def classify_pending_target(
    connection: sqlite3.Connection,
    payload: dict,
    drive_index: set[str],
    *,
    verify_urls: bool,
) -> str | None:
    """Classify a pending payload's media as permanently unrecoverable.

    Returns ``"media-unrecoverable"`` only when EVERY artifact is gone. A single
    recoverable artifact keeps the target alive, because the publish may still
    succeed. Anything this cannot prove is left alone (``None``).
    """
    artifacts = payload.get("artifacts") or []
    if not artifacts:
        return None
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            return None
        local_path = str(artifact.get("local_path") or "")
        public_url = str(artifact.get("public_url") or "")
        if artifact.get("remote_path"):
            return None
        if local_path and _local_media_exists(local_path):
            return None
        if local_path and _drive_has(local_path, drive_index):
            return None
        if _storage_route(
            connection, local_path, artifact.get("source_file_record_id")
        ):
            return None
        if public_url:
            if not verify_urls:
                return None
            if _url_is_live(public_url) is not False:
                return None  # live, or could not be checked: leave it
    return "media-unrecoverable"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--pending-media",
        action="store_true",
        help="also cancel PENDING targets whose payload media is provably gone",
    )
    parser.add_argument(
        "--drive-listing",
        default="",
        help="a `rclone lsf REMOTE: --recursive --files-only --format p` "
        "listing; required by --pending-media",
    )
    parser.add_argument(
        "--verify-urls",
        action="store_true",
        help="HTTP-probe artifact public_urls; a 404 is not a recovery route",
    )
    parser.add_argument("--min-age-days", type=int, default=7)
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

    # --- pending targets with provably unrecoverable media ---
    pending_buckets: dict[str, list[int]] = {}
    pending_review = 0
    pending_scanned = 0
    if args.pending_media:
        if not args.drive_listing:
            print(
                "\n--pending-media requires --drive-listing; "
                "pending targets left alone"
            )
        else:
            drive_index = {
                line.strip()
                for line in Path(args.drive_listing).read_text().splitlines()
                if line.strip()
            }
            cutoff = (
                datetime.now(tz=timezone.utc).replace(tzinfo=None)
                - timedelta(days=args.min_age_days)
            ).isoformat(sep=" ")
            pending_rows = connection.execute(
                "SELECT t.id, t.job_id, j.created_at, j.payload_json "
                "FROM publish_job_targets t "
                "JOIN publish_jobs j ON j.id = t.job_id "
                "WHERE t.status = 'pending'"
            ).fetchall()
            for row in pending_rows:
                created = str(row["created_at"] or "")
                if created and created > cutoff:
                    continue  # a fresh target may simply not be offloaded yet
                pending_scanned += 1
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except (TypeError, ValueError):
                    pending_review += 1
                    continue
                cls = classify_pending_target(
                    connection,
                    payload,
                    drive_index,
                    verify_urls=args.verify_urls,
                )
                if cls is None:
                    pending_review += 1
                else:
                    pending_buckets.setdefault(cls, []).append(int(row["id"]))

            print(
                f"\npending targets scanned (>= {args.min_age_days}d): "
                f"{pending_scanned}"
            )
            for name, ids in sorted(pending_buckets.items(), key=lambda kv: -len(kv[1])):
                print(f"  {name:<22} {len(ids):4}  -> cancel")
            if pending_buckets:
                print(
                    "  ids:", sorted(
                        i for ids in pending_buckets.values() for i in ids
                    )
                )
            print(
                f"  {'NOT CLASSIFIED':<22} {pending_review:4}  "
                "-> LEFT ALONE (review)"
            )

    to_cancel = [i for ids in buckets.values() for i in ids]
    to_cancel += [i for ids in pending_buckets.values() for i in ids]
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
            cursor = connection.execute(
                "UPDATE publish_job_targets SET status='cancelled', last_error=? "
                "WHERE id=? AND status='failed'",
                (reason, target_id),
            )
            cancelled += cursor.rowcount
    for name, ids in pending_buckets.items():
        reason = f"cancelled: permanent failure ({name}) - retry cannot succeed"
        for target_id in ids:
            cursor = connection.execute(
                "UPDATE publish_job_targets SET status='cancelled', last_error=? "
                "WHERE id=? AND status='pending'",
                (reason, target_id),
            )
            cancelled += cursor.rowcount
    connection.commit()
    print(f"cancelled {cancelled} target(s)")

    remaining = connection.execute(
        "SELECT COUNT(*) FROM publish_job_targets WHERE status='failed'"
    ).fetchone()[0]
    print(f"failed targets remaining: {remaining}")
    remaining_pending = connection.execute(
        "SELECT COUNT(*) FROM publish_job_targets WHERE status='pending'"
    ).fetchone()[0]
    print(f"pending targets remaining: {remaining_pending}")
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
