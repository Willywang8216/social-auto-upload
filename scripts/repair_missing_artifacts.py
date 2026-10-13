#!/usr/bin/env python3
"""Repair campaign artifacts that have no restorable mapping.

Problem
-------
Some campaign artifacts exist with bytes on disk or Drive but NO restorable
mapping: no ``file_records.storage_key`` + ``storage_backend_id``, and
``campaign_artifacts.public_url`` / ``remote_path`` are NULL. The restore has
no source, so the publish fails with "media missing".

What it does
------------
For each live target (pending / retrying / running) whose payload references an
artifact with no restorable mapping:

1. If the bytes exist locally → register a ``file_records`` row (idempotent).
2. If the bytes exist on Drive → set ``remote_path`` + ``storage_backend_id``
   on the ``campaign_artifacts`` row so the fallback restore works.
3. Otherwise → report it as unrecoverable.

Safety
------
* Dry-run by default; ``--apply`` writes.
* Takes a copy of the database before ``--apply`` writes.
* No live publishes, no campaign status changes.

Usage
-----
    python scripts/repair_missing_artifacts.py            # report only
    python scripts/repair_missing_artifacts.py --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Ensure the repo root is on sys.path so this works from any cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from myUtils import campaigns as campaign_store
from utils.conf_defaults import default_db_path

LIVE_STATUSES = {"pending", "retrying", "running"}


@dataclass
class ArtifactDiagnosis:
    """Classification of a single payload artifact."""

    artifact_index: int
    local_path: str
    artifact_id: int | None = None
    classification: str = "unknown"  # mapped | registered_local | registered_drive | unrecoverable
    detail: str = ""


@dataclass
class TargetDiagnosis:
    """Classification of a live target's payload."""

    target_id: int
    job_id: int
    account_ref: str
    file_ref: str
    status: str
    diagnoses: list[ArtifactDiagnosis]


def _now_tag() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backup_db(db_path: Path) -> Path:
    ts = _now_tag()
    backup = db_path.with_suffix(f".db.bak-pre-repair-{ts}")
    shutil.copy2(db_path, backup)
    return backup


def _artifact_has_mapping(
    conn: sqlite3.Connection, artifact: dict
) -> tuple[bool, str]:
    """Check whether a payload artifact has any restorable mapping.

    Returns ``(has_mapping, reason)``.
    """
    local_path = str(artifact.get("local_path") or "")
    public_url = str(artifact.get("public_url") or "").strip()
    remote_path = str(artifact.get("remote_path") or "").strip()
    source_id = artifact.get("source_file_record_id")
    campaign_id = artifact.get("campaign_id")
    artifact_db_id = artifact.get("id")

    # 1. public_url or remote_path on the campaign_artifacts row itself.
    if public_url:
        return True, "has public_url"
    if remote_path:
        # remote_path alone is only useful with storage_backend_id, but the
        # worker's _record_for also uses it as storage_key when backend_id is
        # present. Check if backend_id is set.
        if artifact_db_id:
            row = conn.execute(
                "SELECT storage_backend_id FROM campaign_artifacts WHERE id = ?",
                (artifact_db_id,),
            ).fetchone()
            if row and row[0] is not None:
                return True, "has remote_path + storage_backend_id"
        # Even without backend_id, remote_path + public_url on the row is
        # usable via the CDN fallback.
        if public_url:
            return True, "has remote_path + public_url"

    # 2. file_records row with storage_key + storage_backend_id.
    if source_id:
        row = conn.execute(
            "SELECT storage_key, storage_backend_id FROM file_records WHERE id = ?",
            (int(source_id),),
        ).fetchone()
        if row and row[0] is not None and row[1] is not None:
            return True, f"file_records[{source_id}] has storage mapping"

    # 3. Check campaign_artifacts row directly (for artifacts whose
    #    source_file_record_id was lost via ON DELETE SET NULL).
    if artifact_db_id:
        row = conn.execute(
            "SELECT public_url, remote_path, storage_backend_id "
            "FROM campaign_artifacts WHERE id = ?",
            (artifact_db_id,),
        ).fetchone()
        if row:
            if row[0] is not None:
                return True, "campaign_artifacts has public_url"
            if row[1] is not None and row[2] is not None:
                return True, "campaign_artifacts has remote_path + storage_backend_id"

    return False, "no restorable mapping"


def _local_file_exists(local_path: str) -> bool:
    """Check if the file exists at the given path (possibly container-prefixed)."""
    if not local_path:
        return False
    p = Path(local_path)
    if p.exists() and p.is_file() and p.stat().st_size > 0:
        return True
    # Try resolving container prefix to BASE_DIR
    from utils.conf_defaults import BASE_DIR

    raw = str(local_path).replace("\\", "/")
    if raw.startswith("/app/"):
        candidate = Path(BASE_DIR) / raw[len("/app/"):]
        if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0:
            return True
    return False


def _check_drive_has_file(
    conn: sqlite3.Connection, artifact: dict
) -> tuple[bool, str, int | None]:
    """Check if the file exists on Drive via a file_records or storage_backends mapping.

    Returns ``(exists_on_drive, storage_key, backend_id)``.
    """
    source_id = artifact.get("source_file_record_id")
    local_path = str(artifact.get("local_path") or "")
    artifact_db_id = artifact.get("id")

    # Check file_records for any storage mapping (even without backend_id,
    # the storage_key tells us the Drive path).
    candidates: list[str] = []
    if source_id:
        row = conn.execute(
            "SELECT storage_key, storage_backend_id FROM file_records WHERE id = ?",
            (int(source_id),),
        ).fetchone()
        if row and row[0]:
            return True, str(row[0]), int(row[1]) if row[1] else None

    # Check by path in file_records
    if local_path:
        from myUtils.campaigns import _artifact_relative_path

        relative = _artifact_relative_path(local_path)
        paths_to_try = [local_path]
        if relative:
            paths_to_try.extend([relative, f"/app/{relative}"])
        for path_candidate in paths_to_try:
            row = conn.execute(
                "SELECT storage_key, storage_backend_id FROM file_records "
                "WHERE file_path = ? AND storage_key IS NOT NULL",
                (path_candidate,),
            ).fetchone()
            if row:
                return True, str(row[0]), int(row[1]) if row[1] else None

    # Check campaign_artifacts for remote_path (Drive key)
    if artifact_db_id:
        row = conn.execute(
            "SELECT remote_path, storage_backend_id FROM campaign_artifacts WHERE id = ?",
            (artifact_db_id,),
        ).fetchone()
        if row and row[0]:
            return True, str(row[0]), int(row[1]) if row[1] else None

    return False, "", None


def _register_local_file_record(
    conn: sqlite3.Connection, local_path: str
) -> int | None:
    """Ensure a file_records row exists for local_path; return its id.

    Reuses campaign_store._ensure_artifact_file_record for consistency.
    """
    return campaign_store._ensure_artifact_file_record(conn, local_path)


def _find_default_backend_id(conn: sqlite3.Connection) -> int | None:
    """Return the id of the default storage backend, if any."""
    row = conn.execute(
        "SELECT id FROM storage_backends WHERE is_default = 1 AND enabled = 1 LIMIT 1"
    ).fetchone()
    return int(row[0]) if row else None


def diagnose_targets(conn: sqlite3.Connection) -> list[TargetDiagnosis]:
    """Find all live targets and classify their payload artifacts."""
    # Find live targets with their job payloads
    rows = conn.execute(
        "SELECT t.id AS target_id, t.job_id, t.account_ref, t.file_ref, t.status, "
        "j.payload_json "
        "FROM publish_job_targets t "
        "JOIN publish_jobs j ON j.id = t.job_id "
        "WHERE t.status IN ({})".format(",".join("?" for _ in LIVE_STATUSES)),
        list(LIVE_STATUSES),
    ).fetchall()

    results: list[TargetDiagnosis] = []
    for row in rows:
        payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
        artifacts = payload.get("artifacts") or []
        diagnoses: list[ArtifactDiagnosis] = []

        for idx, artifact in enumerate(artifacts):
            if not isinstance(artifact, dict):
                continue
            local_path = str(artifact.get("local_path") or "")
            if not local_path:
                continue

            has_mapping, reason = _artifact_has_mapping(conn, artifact)
            if has_mapping:
                diagnoses.append(
                    ArtifactDiagnosis(
                        artifact_index=idx,
                        local_path=local_path,
                        artifact_id=artifact.get("id"),
                        classification="mapped",
                        detail=reason,
                    )
                )
                continue

            # No mapping — try to find the bytes.
            if _local_file_exists(local_path):
                diagnoses.append(
                    ArtifactDiagnosis(
                        artifact_index=idx,
                        local_path=local_path,
                        artifact_id=artifact.get("id"),
                        classification="registered_local",
                        detail="bytes exist locally; will register file_records",
                    )
                )
                continue

            on_drive, storage_key, backend_id = _check_drive_has_file(
                conn, artifact
            )
            if on_drive:
                diagnoses.append(
                    ArtifactDiagnosis(
                        artifact_index=idx,
                        local_path=local_path,
                        artifact_id=artifact.get("id"),
                        classification="registered_drive",
                        detail=f"bytes on Drive (key={storage_key}, backend_id={backend_id})",
                    )
                )
                continue

            diagnoses.append(
                ArtifactDiagnosis(
                    artifact_index=idx,
                    local_path=local_path,
                    artifact_id=artifact.get("id"),
                    classification="unrecoverable",
                    detail="no bytes locally or on Drive, no mapping",
                )
            )

        # Only include targets that have at least one unmapped artifact.
        unmapped = [d for d in diagnoses if d.classification != "mapped"]
        if unmapped:
            results.append(
                TargetDiagnosis(
                    target_id=row["target_id"],
                    job_id=row["job_id"],
                    account_ref=row["account_ref"],
                    file_ref=row["file_ref"],
                    status=row["status"],
                    diagnoses=diagnoses,
                )
            )

    return results


def apply_repairs(
    conn: sqlite3.Connection, diagnoses: list[TargetDiagnosis]
) -> dict[str, int]:
    """Apply repairs for recoverable artifacts. Returns counts by classification."""
    counts: dict[str, int] = {
        "mapped": 0,
        "registered_local": 0,
        "registered_drive": 0,
        "unrecoverable": 0,
    }
    default_backend_id = _find_default_backend_id(conn)

    for target in diagnoses:
        for diag in target.diagnoses:
            counts[diag.classification] = counts.get(diag.classification, 0) + 1

            if diag.classification == "mapped":
                continue

            if diag.classification == "registered_local":
                # Register a file_records row for the local path.
                file_record_id = _register_local_file_record(conn, diag.local_path)
                if file_record_id and diag.artifact_id:
                    # Also link the campaign_artifact to the new file record.
                    conn.execute(
                        "UPDATE campaign_artifacts SET source_file_record_id = ? "
                        "WHERE id = ? AND source_file_record_id IS NULL",
                        (file_record_id, diag.artifact_id),
                    )

            elif diag.classification == "registered_drive":
                # Set remote_path + storage_backend_id on the campaign_artifacts row.
                if diag.artifact_id:
                    on_drive, storage_key, backend_id = _check_drive_has_file(
                        conn,
                        {"id": diag.artifact_id, "local_path": diag.local_path},
                    )
                    effective_backend = backend_id or default_backend_id
                    sets = ["remote_path = ?"]
                    params: list[object] = [storage_key]
                    if effective_backend:
                        sets.append("storage_backend_id = ?")
                        params.append(effective_backend)
                    params.append(diag.artifact_id)
                    conn.execute(
                        f"UPDATE campaign_artifacts SET {', '.join(sets)} WHERE id = ?",
                        params,
                    )

    conn.commit()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Repair campaign artifacts with no restorable mapping."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write repairs (default is dry-run).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to the SQLite database (default: db/database.db).",
    )
    args = parser.parse_args(argv)

    db_path = args.db or default_db_path()
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    if args.apply:
        backup = _backup_db(db_path)
        print(f"Backup: {backup}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        diagnoses = diagnose_targets(conn)

        if not diagnoses:
            print("No live targets with unmapped artifacts found.")
            return 0

        # Report
        total_targets = len(diagnoses)
        total_artifacts = sum(len(t.diagnoses) for t in diagnoses)
        counts: dict[str, int] = {
            "mapped": 0,
            "registered_local": 0,
            "registered_drive": 0,
            "unrecoverable": 0,
        }
        for target in diagnoses:
            for diag in target.diagnoses:
                counts[diag.classification] = counts.get(diag.classification, 0) + 1

        print(f"\nFound {total_targets} live target(s) with unmapped artifacts:")
        print(f"  Total payload artifacts examined: {total_artifacts}")
        print(f"  Already mapped: {counts['mapped']}")
        print(f"  Recoverable (local): {counts['registered_local']}")
        print(f"  Recoverable (Drive): {counts['registered_drive']}")
        print(f"  Unrecoverable: {counts['unrecoverable']}")
        print()

        for target in diagnoses:
            unmapped = [d for d in target.diagnoses if d.classification != "mapped"]
            if not unmapped:
                continue
            print(
                f"  target {target.target_id} (job={target.job_id}, "
                f"account={target.account_ref}, status={target.status}):"
            )
            for diag in target.diagnoses:
                marker = {
                    "mapped": "✓",
                    "registered_local": "→ register local",
                    "registered_drive": "→ register Drive",
                    "unrecoverable": "✗ UNRECOVERABLE",
                }.get(diag.classification, "?")
                print(f"    [{marker}] {diag.local_path}")
                if diag.classification != "mapped":
                    print(f"           {diag.detail}")

        if args.apply:
            repair_counts = apply_repairs(conn, diagnoses)
            print(f"\nApplied repairs:")
            print(f"  Local registrations: {repair_counts.get('registered_local', 0)}")
            print(f"  Drive registrations: {repair_counts.get('registered_drive', 0)}")
            print(f"  Unrecoverable: {repair_counts.get('unrecoverable', 0)}")
        else:
            print("\nDry-run mode. Pass --apply to write repairs.")

    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
