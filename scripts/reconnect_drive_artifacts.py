#!/usr/bin/env python3
"""Reconnect campaign artifacts whose bytes are on Google Drive but unregistered.

The problem
-----------
An artifact is restorable only through the mapping the offload cron writes into
``file_records`` (``storage_key`` + ``storage_backend_id``) or through an HTTPS
``public_url``. Some artifacts have neither, so the worker declares them
"missing and has no safe public HTTPS recovery URL" and the target can never
publish.

The bytes are usually not gone. The offload cron uploads ``generated/`` and
``videoFile/`` to Google Drive and removes the local copy; when its registration
step did not run (or ran for a different row), the file sits on Drive with
nothing in the database pointing at it.

Measured on the live database (2026-10-09):

    unrecoverable artifacts (no local file, no https url, no remote_path)  648
      ... actually present on Google Drive                                 543
      ... genuinely absent from Drive                                      105
    of the 136 referenced by a live target, all 136 are on Drive

So purging would have destroyed recoverable media. This script reconnects them
instead, and only reports the genuinely-absent ones for an operator decision.

What this does
--------------
For each unrecoverable artifact, it looks the file up on Drive by its path
relative to the ``sau/videoFile`` / ``sau/generated`` root. When found it points
the artifact's ``file_records`` row at the Drive backend:

    storage_key       = <path relative to the root>
    storage_backend_id = the enabled rclone backend for that root
    file_path          = <root>/<relative>   (and the /app/ form)

which is exactly the mapping the offload cron writes, so ``worker`` restores it
with the existing code path.

Dry-run by default. ``--apply`` writes. It never deletes anything: a not-found
file is only reported.

Usage
-----
    python scripts/reconnect_drive_artifacts.py                 # report only
    python scripts/reconnect_drive_artifacts.py --live-only      # just live targets
    python scripts/reconnect_drive_artifacts.py --apply          # register them
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
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
    """Every object path under the Drive root, relative to it."""
    result = subprocess.run(
        [RCLONE, "--config", str(RCLONE_CONFIG), "lsf",
         f"{REMOTE}:sau/{root}", "--recursive", "--files-only", "--format", "p"],
        capture_output=True, text=True, timeout=900,
    )
    if result.returncode != 0:
        raise SystemExit(f"rclone lsf failed for {root}: {result.stderr.strip()[:200]}")
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _relative_parts(local_path: str) -> tuple[str, str] | None:
    """Split ``/app/<root>/<rel>`` into (root, rel), or None if not a media root."""
    raw = str(local_path or "")
    for root in ROOTS:
        marker = f"/{root}/"
        if marker in raw:
            return root, raw.split(marker, 1)[1]
    return None


def _live_paths(connection: sqlite3.Connection) -> set[str]:
    placeholders = ",".join("?" for _ in LIVE_STATUSES)
    needed: set[str] = set()
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
                needed.add(path)
    return needed


def _unrecoverable(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        "SELECT id, campaign_id, artifact_kind, local_path, public_url, remote_path "
        "FROM campaign_artifacts"
    ).fetchall()
    out: list[dict] = []
    for artifact_id, campaign_id, kind, local_path, public_url, remote_path in rows:
        raw = str(local_path or "")
        relative = raw.replace("/app/", "")
        if relative and (REPO_ROOT / relative).is_file():
            continue
        if str(public_url or "").startswith("https://"):
            continue
        if remote_path:
            continue
        out.append({
            "id": artifact_id, "campaign_id": campaign_id, "artifact_kind": kind,
            "local_path": raw,
        })
    return out


def _backend_id(connection: sqlite3.Connection, root: str) -> int | None:
    row = connection.execute(
        "SELECT id FROM storage_backends WHERE provider='rclone' AND bucket=? "
        "AND endpoint=? AND enabled=1",
        (REMOTE, f"sau/{root}"),
    ).fetchone()
    return int(row[0]) if row else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="register the mapping")
    parser.add_argument("--live-only", action="store_true",
                        help="only artifacts a live target still needs")
    args = parser.parse_args()

    db = _db_path()
    connection = sqlite3.connect(str(db))
    connection.row_factory = sqlite3.Row

    live = _live_paths(connection)
    candidates = _unrecoverable(connection)
    print(f"unrecoverable artifacts: {len(candidates)}")
    if args.live_only:
        candidates = [a for a in candidates if a["local_path"] in live]
        print(f"restricted to live-referenced: {len(candidates)}")

    print("building the Drive index (this walks the whole tree)...")
    indices = {root: _drive_index(root) for root in ROOTS}
    for root, entries in indices.items():
        print(f"  sau/{root}: {len(entries)} objects")
    backends = {root: _backend_id(connection, root) for root in ROOTS}

    recoverable: list[tuple[dict, str, str]] = []
    absent: list[dict] = []
    for artifact in candidates:
        parts = _relative_parts(artifact["local_path"])
        if parts is None:
            absent.append(artifact)
            continue
        root, relative = parts
        if relative in indices.get(root, set()) and backends.get(root):
            recoverable.append((artifact, root, relative))
        else:
            absent.append(artifact)

    print()
    print(f"RECOVERABLE from Drive : {len(recoverable)}")
    print(f"ABSENT from Drive      : {len(absent)}")
    live_absent = [a for a in absent if a["local_path"] in live]
    print(f"  ... of the absent, still needed by a live target: {len(live_absent)}")

    if not args.apply:
        for artifact, root, relative in recoverable[:5]:
            print(f"  would register: artifact {artifact['id']} -> sau/{root}/{relative}")
        if absent[:5]:
            print("  absent examples:")
            for artifact in absent[:5]:
                print(f"    artifact {artifact['id']}: {artifact['local_path']}")
        print()
        print("DRY RUN - re-run with --apply to write the mappings.")
        return 0

    updated = 0
    for artifact, root, relative in recoverable:
        backend_id = backends[root]
        # Match the offload cron's mapping shape exactly, so the worker's
        # existing restore path finds it without change.
        candidates_paths = (
            [relative, f"{root}/{relative}", f"/app/{root}/{relative}"]
            if root != "videoFile"
            else [relative, f"videoFile/{relative}", f"/app/videoFile/{relative}"]
        )
        placeholders = ",".join("?" for _ in candidates_paths)
        row = connection.execute(
            f"SELECT id FROM file_records WHERE file_path IN ({placeholders})",
            tuple(candidates_paths),
        ).fetchone()
        if row is not None:
            connection.execute(
                "UPDATE file_records SET storage_key=?, storage_backend_id=? WHERE id=?",
                (relative, backend_id, row[0]),
            )
        else:
            connection.execute(
                "INSERT INTO file_records (filename, filesize, file_path, storage_key, "
                "storage_backend_id) VALUES (?, ?, ?, ?, ?)",
                (Path(relative).name, None, f"{root}/{relative}", relative, backend_id),
            )
        updated += 1
    connection.commit()
    print(f"registered {updated} artifact(s) against Drive")
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
