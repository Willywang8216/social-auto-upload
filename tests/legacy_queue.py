"""Explicit pre-admission history fixtures; never import from runtime code.

Readers and recovery tests must be able to represent malformed historical jobs
without asking the NEW admission boundary to approve those old records. This
helper writes only the caller's existing isolated test database, with no new
readiness authorization, provider calls, or retroactive evidence.
"""
from __future__ import annotations

import json
from pathlib import Path

from myUtils import jobs


def enqueue_legacy_job(spec: jobs.JobSpec, *, db_path: Path, workspace_id=None):
    path = Path(db_path)
    if not path.is_file() or path.resolve() == Path(jobs.DB_PATH).resolve():
        raise ValueError("Legacy fixture requires an explicit existing isolated database")
    targets = jobs._deduplicated_targets(spec)
    key = spec.idempotency_key or jobs.derive_idempotency_key(spec, workspace_id=workspace_id)
    with jobs._transaction(path) as conn:
        previous = conn.execute("SELECT * FROM publish_jobs WHERE idempotency_key=?", (key,)).fetchone()
        if previous is not None:
            raise ValueError("Legacy fixture must not replace existing history")
        identifier = conn.execute(
            "INSERT INTO publish_jobs(idempotency_key,profile_id,platform,payload_json,status,total_targets,workspace_id) "
            "VALUES(?,?,?,?,'pending',?,?)",
            (key, spec.profile_id, spec.platform, json.dumps(spec.payload, ensure_ascii=False), len(targets), workspace_id),
        ).lastrowid
        conn.executemany(
            "INSERT INTO publish_job_targets(job_id,account_ref,file_ref,schedule_at,status,workspace_id,checkpoint_json) "
            "VALUES(?,?,?,?,'pending',?,'{}')",
            [(identifier, account, file_ref, slot, workspace_id) for account, file_ref, slot in targets],
        )
        return jobs._row_to_job(conn.execute("SELECT * FROM publish_jobs WHERE id=?", (identifier,)).fetchone())
