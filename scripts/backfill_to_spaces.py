#!/usr/bin/env python3
"""One-time backfill: upload existing local files to DO Spaces.

Iterates file_records where storage_key IS NULL, uploads each local file
to the default storage backend, and updates the DB row.

Usage:
    python scripts/backfill_to_spaces.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.conf_defaults import BASE_DIR
from myUtils.do_spaces import client_from_row


def main():
    parser = argparse.ArgumentParser(description="Backfill local files to DO Spaces")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be uploaded without uploading")
    args = parser.parse_args()

    db_path = Path(BASE_DIR) / "db" / "database.db"
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Get default storage backend
    backend = conn.execute(
        "SELECT * FROM storage_backends WHERE is_default = 1 AND enabled = 1"
    ).fetchone()
    if not backend:
        print("No default storage backend configured. Run the migration first.")
        sys.exit(1)

    backend = dict(backend)
    print(f"Using storage backend: {backend['slug']} ({backend['bucket']})")

    # Find files without storage_key
    rows = conn.execute(
        "SELECT id, file_path, filename FROM file_records WHERE storage_key IS NULL"
    ).fetchall()

    if not rows:
        print("All files already have storage_key. Nothing to backfill.")
        return

    print(f"Found {len(rows)} files to backfill")

    if args.dry_run:
        for row in rows:
            local = Path(BASE_DIR) / "videoFile" / row["file_path"]
            exists = "EXISTS" if local.exists() else "MISSING"
            print(f"  [{exists}] {row['file_path']}")
        print("Dry run complete. Re-run without --dry-run to upload.")
        return

    client = client_from_row(backend)
    now = datetime.utcnow()
    success = 0
    failed = 0

    for row in rows:
        local = Path(BASE_DIR) / "videoFile" / row["file_path"]
        if not local.exists():
            print(f"  SKIP (missing): {row['file_path']}")
            failed += 1
            continue

        storage_key = f"uploads/{now:%Y/%m}/{row['file_path']}"
        try:
            ext = local.suffix.lower()
            content_type = ""
            if ext in (".mp4", ".mov", ".avi", ".mkv"):
                content_type = "video/mp4"
            elif ext in (".jpg", ".jpeg"):
                content_type = "image/jpeg"
            elif ext in (".png",):
                content_type = "image/png"
            elif ext in (".webp",):
                content_type = "image/webp"

            cdn = client.upload_file(local, storage_key, content_type)
            conn.execute(
                "UPDATE file_records SET storage_backend_id = ?, storage_key = ?, storage_cdn_url = ? WHERE id = ?",
                (backend["id"], storage_key, cdn, row["id"]),
            )
            conn.commit()
            print(f"  OK: {row['file_path']} -> {storage_key}")
            success += 1
        except Exception as e:
            print(f"  FAIL: {row['file_path']}: {e}")
            failed += 1

    conn.close()
    print(f"\nBackfill complete: {success} uploaded, {failed} failed/skipped")


if __name__ == "__main__":
    main()
