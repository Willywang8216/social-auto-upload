#!/usr/bin/env python3
"""Preview or register missing generated-media Drive mappings.

This is deliberately separate from the cron offloader: reconciliation writes
production metadata and must be explicitly invoked after reviewing its dry-run.
Every mapping requires an exact remote key and byte-size match.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

BASE_DIR = Path(__file__).resolve().parents[1]
REMOTE = "GDrive-willywang8216"
ROOT = "generated"
ENDPOINT = "sau/generated"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=BASE_DIR / "db" / "database.db")
    parser.add_argument("--rclone-config", type=Path, default=Path.home() / ".config/rclone/rclone.conf")
    parser.add_argument("--source-root", type=Path, default=BASE_DIR)
    parser.add_argument("--apply", action="store_true", help="write the verified backend and file mappings")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        rows = conn.execute(
            "SELECT id, file_path, filesize, storage_key, storage_backend_id "
            "FROM file_records WHERE file_path LIKE 'generated/%' ORDER BY id"
        ).fetchall()
        remote = None
        for attempt in range(3):
            remote = subprocess.run(
                ["rclone", "--config", str(args.rclone_config), "--tpslimit", "1", "--tpslimit-burst", "1", "lsf", "-R", "--files-only", "--format", "ps", f"{REMOTE}:{ENDPOINT}"],
                capture_output=True,
                text=True,
            )
            if remote.returncode == 0:
                break
            if attempt < 2:
                time.sleep(60 * (attempt + 1))
        if remote is None or remote.returncode:
            print(f"remote listing failed after bounded retries: {remote.stderr.strip() if remote else 'no response'}", file=sys.stderr)
            return 2
        remote_sizes: dict[str, int] = {}
        for line in remote.stdout.splitlines():
            try:
                key, size = line.rsplit(";", 1)
                remote_sizes[str(PurePosixPath(key))] = int(size)
            except ValueError:
                continue

        valid: list[tuple[int, str, int]] = []
        unresolved: list[dict] = []
        for row in rows:
            path = str(row["file_path"] or "")
            key = path[len("generated/"):] if path.startswith("generated/") else ""
            local = args.source_root / path
            if not key:
                unresolved.append({"id": row["id"], "reason": "unsupported file_path", "file_path": path})
                continue
            if local.exists():
                # This recovery utility is only for files already removed locally;
                # the normal offloader records local files before purge.
                unresolved.append({"id": row["id"], "reason": "local file still exists", "file_path": path})
                continue
            remote_size = remote_sizes.get(key)
            if remote_size is None:
                unresolved.append({"id": row["id"], "reason": "remote key absent", "key": key})
                continue
            if row["filesize"] is None or remote_size != int(row["filesize"]):
                unresolved.append({"id": row["id"], "reason": "size mismatch", "key": key, "db_size": row["filesize"], "remote_size": remote_size})
                continue
            valid.append((int(row["id"]), key, remote_size))

        backend = conn.execute(
            "SELECT id, slug, provider, bucket, endpoint, enabled FROM storage_backends "
            "WHERE provider='rclone' AND bucket=? AND endpoint=?",
            (REMOTE, ENDPOINT),
        ).fetchone()
        print(json.dumps({
            "mode": "apply" if args.apply else "dry-run",
            "records": len(rows),
            "remote_objects": len(remote_sizes),
            "exact_path_and_size_matches": len(valid),
            "unresolved": unresolved,
            "backend_exists": dict(backend) if backend else False,
            "matched_records": [{"id": ident, "key": key, "bytes": size} for ident, key, size in valid],
        }, ensure_ascii=False, indent=2))

        if not args.apply:
            print("Dry-run only; pass --apply after reviewing the mapping and backing up the DB.")
            return 0 if not unresolved else 1
        if unresolved:
            print("Refusing apply while any generated record is unresolved.", file=sys.stderr)
            return 3

        conn.execute("BEGIN IMMEDIATE")
        if backend is None:
            conn.execute(
                "INSERT INTO storage_backends "
                "(slug,label,provider,bucket,region,endpoint,access_key,secret_key,cdn_url,is_default,enabled) "
                "VALUES (?,?,?,?,?,?,?,?,?,0,1)",
                ("gdrive-generated", "GDrive generated cache", "rclone", REMOTE, "", ENDPOINT, "", "", ""),
            )
            backend_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        else:
            if not backend["enabled"]:
                raise RuntimeError("existing generated backend is disabled; refusing overwrite")
            backend_id = backend["id"]
        for ident, key, _size in valid:
            conn.execute(
                "UPDATE file_records SET storage_key=?, storage_backend_id=? WHERE id=?",
                (key, backend_id, ident),
            )
        conn.commit()
        print(f"Applied {len(valid)} exact, size-verified mappings using backend id={backend_id}.")
        return 0
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        print(f"reconciliation failed: {exc}", file=sys.stderr)
        return 4
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
