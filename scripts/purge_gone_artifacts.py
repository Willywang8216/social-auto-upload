#!/usr/bin/env python3
"""Delete campaign_artifacts rows whose media is genuinely gone.

Scope, established before this script existed
---------------------------------------------
After ``reconnect_drive_artifacts.py`` recovered the 543 artifacts that were
merely unregistered, 105 artifacts remained whose bytes are **absent locally,
absent from Google Drive, and have no HTTPS URL or storage mapping at all**.
None of them is reachable by any restore path, so each row is dead metadata: it
documents a file that does not exist and cannot be fetched.

What it deletes, and what it does not
-------------------------------------
It deletes **only the campaign_artifacts rows themselves**. It does not touch
any file, any publish job, any target, or any published post. Those 105 rows
affect only targets that are already terminal:

    cancelled  220 targets
    failed      52 targets
    succeeded  117 targets
    pending      0 targets   <- nothing live depends on them

So this changes no publish outcome; it stops the database from claiming media it
cannot produce.

Safety
------
* Dry-run by default; ``--apply`` writes.
* **Refuses to delete a row any live target needs** (pending/retrying/running).
  If one is found it is reported and skipped, never silently removed.
* Takes a copy of the database to ``db/database.db.bak-pre-artifact-purge-<ts>``
  before writing.
* Re-verifies against Google Drive at run time, so a file that has since been
  re-uploaded is not deleted.

Usage
-----
    python scripts/purge_gone_artifacts.py            # report only
    python scripts/purge_gone_artifacts.py --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

RCLONE = "/usr/bin/rclone"
RCLONE_CONFIG = Path.home() / ".config/rclone/rclone.conf"
REMOTE = "GDrive-willywang8216"
ROOTS = ("videoFile", "generated", "uploads")
LIVE_STATUSES = ("pending", "retrying", "running")


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
        if f"/{root}/" in raw:
            return root, raw.split(f"/{root}/", 1)[1]
    return None


def _recoverable(connection: sqlite3.Connection, local_path: str, public_url: str,
                 indices: dict[str, set[str]]) -> bool:
    """Any route back to the bytes: local file, Drive, HTTPS URL, or a mapping."""
    if str(public_url or "").startswith("https://"):
        return True
    raw = str(local_path or "")
    rel = raw.replace("/app/", "")
    if rel and (REPO_ROOT / rel).is_file():
        return True
    parts = _root_and_rel(raw)
    if parts and parts[1] in indices.get(parts[0], set()):
        return True
    candidates = [raw, rel, f"/app/{rel}"]
    if parts:
        candidates.extend([f"{parts[0]}/{parts[1]}", parts[1]])
    seen = [c for i, c in enumerate(candidates) if c and c not in candidates[:i]]
    placeholders = ",".join("?" for _ in seen)
    for storage_key, backend_id, cdn_url in connection.execute(
        f"SELECT storage_key, storage_backend_id, storage_cdn_url "
        f"FROM file_records WHERE file_path IN ({placeholders})", tuple(seen)
    ):
        if (storage_key and backend_id) or cdn_url:
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db = _db_path()
    connection = sqlite3.connect(str(db))
    connection.row_factory = sqlite3.Row

    # Which artifact paths does a LIVE target still need?
    live_needed: set[str] = set()
    placeholders = ",".join("?" for _ in LIVE_STATUSES)
    for (payload_json,) in connection.execute(
        f"SELECT j.payload_json FROM publish_job_targets t "
        f"JOIN publish_jobs j ON j.id = t.job_id WHERE t.status IN ({placeholders})",
        LIVE_STATUSES,
    ):
        try:
            data = json.loads(payload_json or "{}")
        except Exception:  # noqa: BLE001
            continue
        for artifact in data.get("artifacts") or []:
            path = str(artifact.get("local_path") or "")
            if path:
                live_needed.add(path)

    print("building the Drive index...")
    indices = {root: _drive_index(root) for root in ROOTS}

    killed: list[int] = []
    protected: list[tuple[int, str]] = []
    for row in connection.execute(
        "SELECT id, local_path, public_url FROM campaign_artifacts"
    ):
        if _recoverable(connection, row["local_path"], row["public_url"], indices):
            continue
        if str(row["local_path"] or "") in live_needed:
            # Never remove an artifact a live target still points at, even if it
            # looks unrecoverable right now - it may be restored by another path.
            protected.append((row["id"], row["local_path"]))
            continue
        killed.append(row["id"])

    print(f"dead artifact rows (media gone, nothing live needs them): {len(killed)}")
    print(f"protected because a live target needs them            : {len(protected)}")
    for artifact_id, path in protected[:5]:
        print(f"    PROTECTED {artifact_id}: {path}")

    if not args.apply:
        for artifact_id in killed[:10]:
            print(f"    would delete artifact row {artifact_id}")
        print("\nDRY RUN - re-run with --apply to delete these rows.")
        return 0

    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = db.with_name(f"{db.name}.bak-pre-artifact-purge-{stamp}")
    shutil.copy2(db, backup)
    print(f"database backed up to {backup}")

    deleted = 0
    for artifact_id in killed:
        connection.execute("DELETE FROM campaign_artifacts WHERE id = ?", (artifact_id,))
        deleted += 1
    connection.commit()
    print(f"deleted {deleted} campaign_artifacts row(s) whose media is gone")
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
