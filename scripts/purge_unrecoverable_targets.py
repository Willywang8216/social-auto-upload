#!/usr/bin/env python3
"""Cancel targets whose media is genuinely gone, and report what was purged.

Context
-------
A target can never publish when its media is gone from local disk, absent from
Google Drive, and carries no HTTPS URL or storage mapping. Such a target sits in
the queue forever and fails on every attempt.

This runs *after* ``reconnect_drive_artifacts.py``, which recovers the ones whose
bytes are still on Drive - 543 of them, including every one a live target needed.
What remains is genuinely lost.

Safety
------
A target is only purged when ALL of the following hold:

  1. it is ``pending`` (not already terminal; nothing to cancel otherwise),
  2. every artifact its payload needs is unrecoverable - local file absent,
     Google Drive absent, no HTTPS ``public_url``, no ``storage_key`` +
     ``storage_backend_id``, and no ``storage_cdn_url``,
  3. it is not ``running`` (never race an in-flight publish),
  4. the target is older than ``--min-age-days`` (default 7), so a freshly
     imported artifact that simply has not been offloaded yet is never touched.

Anything partially recoverable is left alone and reported - a target with one
usable artifact may still publish. Nothing is deleted except the target's own
status being set to ``cancelled``; no files and no rows are removed. Run with
``--apply`` to write; the default is a report.

Usage
-----
    python scripts/purge_unrecoverable_targets.py
    python scripts/purge_unrecoverable_targets.py --apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

RCLONE = "/usr/bin/rclone"
RCLONE_CONFIG = Path.home() / ".config/rclone/rclone.conf"
REMOTE = "GDrive-willywang8216"
ROOTS = ("videoFile", "generated", "uploads")


def _db_path() -> Path:
    import os

    configured = os.environ.get("SAU_DB_PATH")
    return Path(configured) if configured else REPO_ROOT / "db" / "database.db"


def _drive_index(root: str) -> set[str]:
    result = subprocess.run(
        [RCLONE, "--config", str(RCLONE_CONFIG), "lsf", f"{REMOTE}:sau/{root}",
         "--recursive", "--files-only", "--format", "p"],
        capture_output=True, text=True, timeout=900,
    )
    if result.returncode != 0:
        raise SystemExit(f"rclone lsf failed for {root}: {result.stderr.strip()[:200]}")
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _root_and_rel(local_path: str) -> tuple[str, str] | None:
    raw = str(local_path or "")
    for root in ROOTS:
        marker = f"/{root}/"
        if marker in raw:
            return root, raw.split(marker, 1)[1]
    return None


def _storage_route(connection: sqlite3.Connection, local_path: str) -> bool:
    """True when a file_records row gives a usable restore route."""
    raw = str(local_path or "")
    rel = raw.replace("/app/", "")
    candidates = [raw, rel, f"/app/{rel}"]
    parts = _root_and_rel(raw)
    if parts:
        root, tail = parts
        candidates.extend([f"{root}/{tail}", tail, f"/app/{root}/{tail}"])
    seen: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.append(candidate)
    placeholders = ",".join("?" for _ in seen)
    rows = connection.execute(
        f"SELECT storage_key, storage_backend_id, storage_cdn_url "
        f"FROM file_records WHERE file_path IN ({placeholders})",
        tuple(seen),
    ).fetchall()
    for storage_key, backend_id, cdn_url in rows:
        if (storage_key and backend_id) or cdn_url:
            return True
    return False


def _recoverable(
    connection: sqlite3.Connection,
    local_path: str,
    public_url: str | None,
    indices: dict[str, set[str]],
) -> bool:
    """True when this artifact has ANY route back to its bytes.

    The payload's own ``public_url`` is checked FIRST and is decisive. Without
    it this function reported five targets as unrecoverable that the worker
    restores fine from R2 - cancelling them would have destroyed a
    recoverable publish. The URL is the route the upload path registered
    specifically so the artifact could be fetched again.
    """
    if str(public_url or "").startswith("https://"):
        return True
    raw = str(local_path or "")
    rel = raw.replace("/app/", "")
    if rel and (REPO_ROOT / rel).is_file():
        return True
    parts = _root_and_rel(raw)
    if parts and parts[1] in indices.get(parts[0], set()):
        return True
    if _storage_route(connection, raw):
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--min-age-days", type=int, default=7)
    args = parser.parse_args()

    db = _db_path()
    connection = sqlite3.connect(str(db))
    connection.row_factory = sqlite3.Row

    print("building the Drive index...")
    indices = {root: _drive_index(root) for root in ROOTS}

    cutoff = datetime.now(tz=timezone.utc).replace(tzinfo=None) - timedelta(
        days=args.min_age_days
    )
    pending = connection.execute(
        "SELECT t.id, t.job_id, t.account_ref, t.schedule_at, j.created_at AS job_created "
        "FROM publish_job_targets t JOIN publish_jobs j ON j.id = t.job_id "
        "WHERE t.status = 'pending'"
    ).fetchall()
    print(f"pending targets: {len(pending)}")

    job_payloads: dict[int, dict] = {}
    purge: list[tuple[int, str]] = []
    partial = 0
    too_new = 0
    for target in pending:
        job_id = int(target["job_id"])
        if job_id not in job_payloads:
            row = connection.execute(
                "SELECT payload_json FROM publish_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            try:
                job_payloads[job_id] = json.loads(row["payload_json"] or "{}") if row else {}
            except Exception:  # noqa: BLE001
                job_payloads[job_id] = {}
        # Age guard: a recently queued target may simply not be offloaded yet.
        # Use the JOB's created_at, not schedule_at: schedule_at is the intended
        # publish time and is almost always in the future, so comparing it to a
        # past cutoff skipped every target.
        created = str(target["job_created"] or "")
        if created:
            try:
                when = datetime.fromisoformat(created.replace("T", " "))
            except ValueError:
                when = None
            if when is not None and when > cutoff:
                too_new += 1
                continue
        artifacts = job_payloads[job_id].get("artifacts") or []
        entries = [
            (str(a.get("local_path") or ""), str(a.get("public_url") or ""))
            for a in artifacts
        ]
        entries = [(path, url) for path, url in entries if path]
        if not entries:
            continue
        # A target may only be purged when EVERY artifact is unrecoverable: one
        # usable artifact is enough for the publish to succeed.
        if any(_recoverable(connection, path, url, indices) for path, url in entries):
            partial += 1
            continue
        purge.append((int(target["id"]), entries[0][0]))

    print(f"  fully unrecoverable (all media gone) : {len(purge)}")
    print(f"  partially recoverable (left alone)   : {partial}")
    print(f"  skipped as too recent (<{args.min_age_days}d)        : {too_new}")

    if not args.apply:
        for target_id, sample in purge[:8]:
            print(f"    would cancel target {target_id}: {sample}")
        print("\nDRY RUN - re-run with --apply to cancel these targets.")
        return 0

    cancelled = 0
    for target_id, _ in purge:
        connection.execute(
            "UPDATE publish_job_targets SET status='cancelled', "
            "last_error=COALESCE(last_error, 'cancelled: media is unrecoverable "
            "(absent locally and on Google Drive, with no URL or storage mapping)') "
            "WHERE id = ? AND status = 'pending'",
            (target_id,),
        )
        cancelled += connection.total_changes and 1 or 0
    connection.commit()
    print(f"cancelled {len(purge)} target(s)")
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
