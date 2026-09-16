"""Publish-job persistence and lifecycle.

A *publish job* is one user intent to publish a set of files to a set of
accounts on a single platform. Each (account, file) pair becomes a *target*,
which is the unit of retry and progress tracking.

State machine
-------------

Jobs:
    pending -> running -> succeeded | failed | cancelled

Targets:
    pending -> running -> (succeeded | retrying | failed | cancelled)

Retries are driven by ``attempts`` and the worker's retry policy. A target
flagged as ``retrying`` is re-claimed by the worker on the next pass.

Idempotency
-----------

Each job has a unique ``idempotency_key``. Re-enqueuing a job with the same
key returns the existing job rather than creating a duplicate. If the caller
does not provide a key, one is derived from a stable hash of the canonicalised
payload + platform + target list.

This module is deliberately framework-agnostic: it speaks SQLite + dataclasses
and exposes pure functions. The async worker (``myUtils/worker.py``) orchestrates
target execution on top of it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from utils.conf_defaults import BASE_DIR

DB_PATH = Path(BASE_DIR) / "db" / "database.db"


# Job lifecycle states.
JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_SUCCEEDED = "succeeded"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"

JOB_TERMINAL = {JOB_SUCCEEDED, JOB_FAILED, JOB_CANCELLED}

# Target lifecycle states.
TARGET_PENDING = "pending"
TARGET_RUNNING = "running"
TARGET_SUCCEEDED = "succeeded"
TARGET_RETRYING = "retrying"
TARGET_FAILED = "failed"
TARGET_CANCELLED = "cancelled"

TARGET_CLAIMABLE = {TARGET_PENDING, TARGET_RETRYING}
TARGET_TERMINAL = {TARGET_SUCCEEDED, TARGET_FAILED, TARGET_CANCELLED}


@dataclass(slots=True)
class Job:
    id: int
    idempotency_key: str
    profile_id: int | None
    platform: str
    payload: dict
    status: str
    total_targets: int
    completed_targets: int
    failed_targets: int
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict:
        out = asdict(self)
        return out


@dataclass(slots=True)
class Target:
    id: int
    job_id: int
    account_ref: str
    file_ref: str
    schedule_at: str | None
    status: str
    attempts: int
    last_error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class JobSpec:
    """Caller-facing input for ``enqueue_job``.

    ``targets`` is a list of (account_ref, file_ref, optional schedule_at) tuples.
    """

    platform: str
    payload: dict
    targets: Sequence[tuple[str, str, datetime | None]]
    profile_id: int | None = None
    idempotency_key: str | None = None
    extra_canonical: tuple = field(default_factory=tuple)


def _resolve_db_path(db_path: Path | None) -> Path:
    """Resolve the DB path lazily so monkeypatching ``DB_PATH`` works.

    Default-argument expressions are evaluated once at function-definition
    time, which would freeze the original ``DB_PATH``. By passing ``None`` and
    looking up the module attribute at call time we let tests rebind ``DB_PATH``
    via ``unittest.mock.patch`` and have it actually take effect.
    """

    return db_path if db_path is not None else DB_PATH


@contextmanager
def _connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    resolved = _resolve_db_path(db_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _now_iso() -> str:
    # Use timezone-aware UTC and strip the offset so the isoformat string
    # remains compatible with the existing ``DATETIME`` SQLite columns.
    return datetime.now(tz=timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _claimable_clause(now: str) -> tuple[str, list]:
    """SQL fragment + params selecting targets that may be claimed *now*.

    A target is claimable when it is pending/retrying AND its ``schedule_at``
    is empty or already due. ``schedule_at`` is stored as a tz-naive UTC ISO
    string (see ``publish_orchestrator._resolve_base_time``), the same shape
    ``_now_iso`` produces, so a plain string comparison is chronological.
    """
    return (
        "status IN (?, ?) AND (schedule_at IS NULL OR schedule_at = '' OR schedule_at <= ?)",
        [TARGET_PENDING, TARGET_RETRYING, now],
    )


def has_claimable_targets(*, db_path: Path | None = None) -> bool:
    """Cheap existence check: is any target due for pickup right now?"""
    clause, params = _claimable_clause(_now_iso())
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT 1 FROM publish_job_targets WHERE {clause} LIMIT 1", params
        ).fetchone()
    return row is not None


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        idempotency_key=row["idempotency_key"],
        profile_id=row["profile_id"],
        platform=row["platform"],
        payload=json.loads(row["payload_json"]) if row["payload_json"] else {},
        status=row["status"],
        total_targets=row["total_targets"],
        completed_targets=row["completed_targets"],
        failed_targets=row["failed_targets"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _row_to_target(row: sqlite3.Row) -> Target:
    return Target(
        id=row["id"],
        job_id=row["job_id"],
        account_ref=row["account_ref"],
        file_ref=row["file_ref"],
        schedule_at=row["schedule_at"],
        status=row["status"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def derive_idempotency_key(spec: JobSpec) -> str:
    """Stable SHA-256 over (platform, payload, sorted targets, extras).

    Used when the caller does not supply an explicit key. The same logical job
    submitted twice will collapse into a single row.
    """

    canonical = {
        "platform": spec.platform,
        "payload": _canonical(spec.payload),
        "targets": sorted(
            (acct, file_ref, _schedule_canonical(sched))
            for acct, file_ref, sched in spec.targets
        ),
        "extra": list(spec.extra_canonical),
    }
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "auto-" + hashlib.sha256(blob).hexdigest()[:32]


def _canonical(value):
    if isinstance(value, dict):
        return {k: _canonical(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _schedule_canonical(sched: datetime | None) -> str:
    if sched is None:
        return ""
    if isinstance(sched, datetime):
        return sched.isoformat()
    return str(sched)


# --------------------------- enqueue / lookup ---------------------------


def enqueue_job(spec: JobSpec, *, workspace_id: str | None = None, db_path: Path | None = None) -> Job:
    """Insert a job and its targets atomically.

    If an existing job already has the same ``idempotency_key`` the existing
    row is returned unchanged — neither the job nor the targets are mutated.
    When ``workspace_id`` is given, the new job is stamped with it (tenant
    ownership); ``None`` leaves it unset (legacy/single-tenant).
    """

    if not spec.targets:
        raise ValueError("Job must have at least one target")

    key = spec.idempotency_key or derive_idempotency_key(spec)
    payload_json = json.dumps(spec.payload, sort_keys=True, ensure_ascii=False)

    # Deduplicate targets first so total_targets matches the actual row count.
    seen: set[tuple[str, str]] = set()
    deduped_targets: list[tuple[str, str, Any]] = []
    for account_ref, file_ref, schedule_at in spec.targets:
        tup = (account_ref, file_ref)
        if tup in seen:
            continue
        seen.add(tup)
        deduped_targets.append((account_ref, file_ref, schedule_at))

    with _connect(db_path) as conn:
        existing = conn.execute(
            "SELECT * FROM publish_jobs WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if existing is not None:
            return _row_to_job(existing)

        if workspace_id is not None:
            cursor = conn.execute(
                """
                INSERT INTO publish_jobs (idempotency_key, profile_id, platform,
                                           payload_json, status, total_targets, workspace_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (key, spec.profile_id, spec.platform, payload_json,
                 JOB_PENDING, len(deduped_targets), workspace_id),
            )
        else:
            cursor = conn.execute(
                """
                INSERT INTO publish_jobs (idempotency_key, profile_id, platform,
                                           payload_json, status, total_targets)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (key, spec.profile_id, spec.platform, payload_json,
                 JOB_PENDING, len(deduped_targets)),
            )
        job_id = cursor.lastrowid

        for account_ref, file_ref, schedule_at in deduped_targets:
            conn.execute(
                """
                INSERT INTO publish_job_targets
                    (job_id, account_ref, file_ref, schedule_at, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, account_ref, file_ref,
                 schedule_at.isoformat() if isinstance(schedule_at, datetime) else schedule_at,
                 TARGET_PENDING),
            )
        conn.commit()
        return _row_to_job(
            conn.execute("SELECT * FROM publish_jobs WHERE id = ?", (job_id,)).fetchone()
        )


def get_job(job_id: int, *, workspace_id: str | None = None, db_path: Path | None = None) -> Job:
    """Fetch a job. When ``workspace_id`` is given, a job owned by another
    workspace is treated as not found (tenant isolation)."""
    with _connect(db_path) as conn:
        if workspace_id is not None:
            row = conn.execute(
                "SELECT * FROM publish_jobs WHERE id = ? AND workspace_id = ?",
                (job_id, workspace_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM publish_jobs WHERE id = ?", (job_id,)
            ).fetchone()
    if row is None:
        raise LookupError(f"Job not found: id={job_id}")
    return _row_to_job(row)


def get_job_by_idempotency_key(key: str, *, db_path: Path | None = None) -> Job | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM publish_jobs WHERE idempotency_key = ?", (key,)
        ).fetchone()
    return _row_to_job(row) if row else None


LIST_JOBS_MAX_LIMIT = 500


def _action_title(payload: dict) -> str:
    """Derive a human-readable job/target title from a publish payload.

    Resolution order: ``payload.title`` → ``payload.message`` → a single
    ``draft.message`` (the publish-center caption) → the first artifact's
    basename → "Untitled". Handles two legacy shapes the pipeline produced:
    ``draft.message`` being a stringified JSON object (old jobs stored the whole
    generated draft as a JSON *string*) and ``payload.message`` holding the same
    stringified object.
    """
    if not isinstance(payload, dict):
        return "Untitled"

    def _snip(value) -> str:
        if isinstance(value, dict):
            value = value.get("message") or ""
        text = str(value or "").strip()
        if not text or text.startswith("{") or text.startswith("["):
            return ""
        return text if len(text) <= 80 else text[:80] + "..."

    for key in ("title", "message", "scheduledAt"):
        text = _snip(payload.get(key))
        if text:
            return text
    draft = payload.get("draft")
    if isinstance(draft, dict):
        text = _snip(draft.get("message"))
        if text:
            return text
    artifacts = payload.get("artifacts") or []
    if isinstance(artifacts, list) and artifacts:
        first = artifacts[0]
        if isinstance(first, dict):
            local = str(first.get("local_path") or "").strip()
            if local:
                return local.rsplit("/", 1)[-1] or "Untitled"
        else:
            name = str(first or "").strip()
            if name:
                return name.rsplit("/", 1)[-1] or "Untitled"
    return "Untitled"


def list_jobs(
    *,
    status: str | None = None,
    platform: str | None = None,
    limit: int = 50,
    workspace_id: str | None = None,
    db_path: Path | None = None,
) -> list[Job]:
    # Guard against the SQLite quirk where ``LIMIT -1`` (and any negative
    # integer) means "no limit". A 0 limit is also rejected because an
    # empty page is almost certainly a caller bug rather than an intent.
    try:
        limit_int = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"limit must be an integer, got {limit!r}") from exc
    if limit_int < 1:
        raise ValueError(f"limit must be >= 1, got {limit_int}")
    if limit_int > LIST_JOBS_MAX_LIMIT:
        limit_int = LIST_JOBS_MAX_LIMIT

    query = "SELECT * FROM publish_jobs"
    clauses: list[str] = []
    params: list = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if platform is not None:
        clauses.append("platform = ?")
        params.append(platform)
    if workspace_id is not None:
        clauses.append("workspace_id = ?")
        params.append(workspace_id)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit_int)

    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_job(row) for row in rows]


def list_targets(job_id: int, *, db_path: Path | None = None) -> list[Target]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM publish_job_targets WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
    return [_row_to_target(row) for row in rows]


def cancel_job(job_id: int, *, db_path: Path | None = None) -> Job:
    """Mark a non-terminal job (and its non-terminal targets) cancelled."""

    now = _now_iso()
    with _connect(db_path) as conn:
        job_row = conn.execute(
            "SELECT * FROM publish_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if job_row is None:
            raise LookupError(f"Job not found: id={job_id}")
        if job_row["status"] in JOB_TERMINAL:
            return _row_to_job(job_row)

        conn.execute(
            "UPDATE publish_jobs SET status = ?, finished_at = ? WHERE id = ?",
            (JOB_CANCELLED, now, job_id),
        )
        conn.execute(
            """
            UPDATE publish_job_targets
            SET status = ?, finished_at = ?
            WHERE job_id = ? AND status NOT IN (?, ?, ?)
            """,
            (TARGET_CANCELLED, now, job_id,
             TARGET_SUCCEEDED, TARGET_FAILED, TARGET_CANCELLED),
        )
        conn.commit()
        return _row_to_job(
            conn.execute("SELECT * FROM publish_jobs WHERE id = ?", (job_id,)).fetchone()
        )


# --------------------------- worker-facing transitions ---------------------------


def claim_next_targets(
    *,
    limit: int = 1,
    excluded_accounts: Iterable[str] = (),
    db_path: Path | None = None,
) -> list[Target]:
    """Atomically claim up to ``limit`` claimable targets.

    Targets are claimed by transitioning them from ``pending``/``retrying``
    to ``running``. Targets whose ``account_ref`` is in ``excluded_accounts``
    are skipped — the worker passes the set of accounts currently held by
    other in-flight tasks, ensuring the same account never runs concurrently
    across workers.
    """

    excluded = list(excluded_accounts)
    now = _now_iso()
    clause, clause_params = _claimable_clause(now)
    claimed: list[Target] = []

    with _connect(db_path) as conn:
        conn.isolation_level = None  # explicit transaction
        conn.execute("BEGIN IMMEDIATE")
        try:
            placeholders = ",".join("?" * len(excluded)) if excluded else ""
            sql = f"""
                SELECT * FROM publish_job_targets
                WHERE {clause}
                {f'AND account_ref NOT IN ({placeholders})' if excluded else ''}
                ORDER BY id
                LIMIT ?
            """
            params: list = list(clause_params)
            params.extend(excluded)
            params.append(int(limit))

            rows = conn.execute(sql, params).fetchall()
            for row in rows:
                conn.execute(
                    """
                    UPDATE publish_job_targets
                    SET status = ?, attempts = attempts + 1,
                        started_at = COALESCE(started_at, ?)
                    WHERE id = ? AND status IN (?, ?)
                    """,
                    (TARGET_RUNNING, now, row["id"],
                     TARGET_PENDING, TARGET_RETRYING),
                )
                # Mark the parent job as running on the first target claim.
                conn.execute(
                    """
                    UPDATE publish_jobs
                    SET status = ?, started_at = COALESCE(started_at, ?)
                    WHERE id = ? AND status = ?
                    """,
                    (JOB_RUNNING, now, row["job_id"], JOB_PENDING),
                )
                refreshed = conn.execute(
                    "SELECT * FROM publish_job_targets WHERE id = ?", (row["id"],)
                ).fetchone()
                claimed.append(_row_to_target(refreshed))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return claimed


# Worker-facing transitions are written so they only mutate a target that is
# still ``running``. If the target was cancelled (or otherwise transitioned)
# while the executor was in flight, the cancellation wins and the late
# success/failure/retry call becomes a no-op. This avoids the cancel-race
# where a cancelled target gets resurrected as succeeded/failed/retrying and
# the corresponding job counters drift.
#
# Note on in-flight uploads: cancelling a job updates the DB row immediately,
# but the worker has no way to abort an already-running Playwright upload —
# the platform-side post may still complete. The caller should treat
# cancellation as "stop processing further attempts on our side", not as a
# guarantee that no upload happens. This matches what every browser-driven
# automation tool can promise.


def mark_target_success(target_id: int, *, db_path: Path | None = None) -> bool:
    """Transition a running target to succeeded.

    Returns ``True`` when the row actually changed (i.e. it was still
    ``running`` at the time of the UPDATE), ``False`` when the call was a
    no-op because the target had already been cancelled or otherwise moved
    on. Callers can use the return value to detect cancel races.
    """

    now = _now_iso()
    with _connect(db_path) as conn:
        target = conn.execute(
            "SELECT job_id FROM publish_job_targets WHERE id = ?", (target_id,)
        ).fetchone()
        if target is None:
            raise LookupError(f"Target not found: id={target_id}")

        cursor = conn.execute(
            """
            UPDATE publish_job_targets
            SET status = ?, finished_at = ?, last_error = NULL
            WHERE id = ? AND status = ?
            """,
            (TARGET_SUCCEEDED, now, target_id, TARGET_RUNNING),
        )
        if cursor.rowcount == 0:
            # Cancelled (or otherwise transitioned) while the executor was
            # running. Leave the row alone; do not bump the counters.
            conn.commit()
            return False

        conn.execute(
            "UPDATE publish_jobs SET completed_targets = completed_targets + 1 WHERE id = ?",
            (target["job_id"],),
        )
        _maybe_finalise_job(conn, target["job_id"])
        conn.commit()
        return True


def mark_target_retry(
    target_id: int, error: str, *, db_path: Path | None = None
) -> bool:
    """Push a transiently-failed target back into the queue for another attempt.

    Guarded on ``status = 'running'`` so a cancellation that happened while
    the executor was in flight is preserved (the cancelled target is not
    resurrected into the queue). Returns ``True`` when the row was updated.
    """

    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE publish_job_targets
            SET status = ?, last_error = ?
            WHERE id = ? AND status = ?
            """,
            (TARGET_RETRYING, error[:2000], target_id, TARGET_RUNNING),
        )
        conn.commit()
        return cursor.rowcount > 0


def mark_target_failed(
    target_id: int, error: str, *, db_path: Path | None = None
) -> bool:
    """Transition a running target to permanently failed.

    Same cancel-race semantics as ``mark_target_success``: returns ``False``
    when the target was no longer ``running`` at UPDATE time.
    """

    now = _now_iso()
    with _connect(db_path) as conn:
        target = conn.execute(
            "SELECT job_id FROM publish_job_targets WHERE id = ?", (target_id,)
        ).fetchone()
        if target is None:
            raise LookupError(f"Target not found: id={target_id}")

        cursor = conn.execute(
            """
            UPDATE publish_job_targets
            SET status = ?, finished_at = ?, last_error = ?
            WHERE id = ? AND status = ?
            """,
            (TARGET_FAILED, now, error[:2000], target_id, TARGET_RUNNING),
        )
        if cursor.rowcount == 0:
            conn.commit()
            return False

        conn.execute(
            "UPDATE publish_jobs SET failed_targets = failed_targets + 1 WHERE id = ?",
            (target["job_id"],),
        )
        _maybe_finalise_job(conn, target["job_id"])
        conn.commit()
        return True


def _maybe_finalise_job(conn: sqlite3.Connection, job_id: int) -> None:
    """If every target has reached a terminal state, finalise the job."""

    row = conn.execute(
        """
        SELECT total_targets, completed_targets, failed_targets,
               (SELECT COUNT(*) FROM publish_job_targets
                WHERE job_id = ? AND status = ?) AS cancelled_count
        FROM publish_jobs
        WHERE id = ?
        """,
        (job_id, TARGET_CANCELLED, job_id),
    ).fetchone()
    if row is None:
        return
    settled = row["completed_targets"] + row["failed_targets"] + row["cancelled_count"]
    if settled < row["total_targets"]:
        return

    if row["failed_targets"] == 0 and row["cancelled_count"] == 0:
        final = JOB_SUCCEEDED
    elif row["completed_targets"] == 0:
        final = JOB_CANCELLED if row["failed_targets"] == 0 else JOB_FAILED
    else:
        # Partial success counts as failed at the job level so the caller
        # treats the result as needing attention; the per-target detail is
        # always available via list_targets.
        final = JOB_FAILED if row["failed_targets"] > 0 else JOB_CANCELLED
    conn.execute(
        """
        UPDATE publish_jobs
        SET status = ?, finished_at = COALESCE(finished_at, ?)
        WHERE id = ? AND status NOT IN (?, ?, ?)
        """,
        (final, _now_iso(), job_id,
         JOB_SUCCEEDED, JOB_FAILED, JOB_CANCELLED),
    )


# ---------------------------------------------------------------------------
# TikTok publish status tracking
# ---------------------------------------------------------------------------

def upsert_tiktok_publish_status(
    publish_id: str,
    *,
    job_id: str | None = None,
    account_id: str,
    status: str = "processing",
    fail_reason: str | None = None,
    post_id: str | None = None,
    platform_url: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Insert or update a TikTok publish status row."""
    now = _now_iso()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO tiktok_publish_status
                (publish_id, job_id, account_id, status, fail_reason,
                 post_id, platform_url, polled_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(publish_id) DO UPDATE SET
                status = excluded.status,
                fail_reason = excluded.fail_reason,
                post_id = excluded.post_id,
                platform_url = excluded.platform_url,
                polled_at = excluded.polled_at,
                updated_at = excluded.updated_at
            """,
            (publish_id, job_id, account_id, status, fail_reason,
             post_id, platform_url, now, now, now),
        )
        conn.commit()


def get_tiktok_publish_status(
    publish_id: str, *, db_path: Path | None = None
) -> dict | None:
    """Return a single TikTok publish status row as a dict, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM tiktok_publish_status WHERE publish_id = ?",
            (publish_id,),
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Calendar / schedule management (API-driven target operations)
# ---------------------------------------------------------------------------

def list_scheduled_targets(
    *,
    month: str | None = None,
    platform: str | None = None,
    status: str | None = None,
    limit: int = 500,
    workspace_id: str | None = None,
    db_path: Path | None = None,
) -> list[tuple[dict, dict]]:
    """JOIN publish_job_targets x publish_jobs, return (target, job) tuples.

    This is the data source for the calendar view. Only targets with a
    non-empty ``schedule_at`` are returned (transient jobs have NULL there
    and are the job dashboard's concern). ``status`` may be a comma-separated
    list; ``month`` narrows to ``schedule_at LIKE 'YYYY-MM%'``.
    """

    try:
        limit_int = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"limit must be an integer, got {limit!r}") from exc
    if limit_int < 1:
        raise ValueError(f"limit must be >= 1, got {limit_int}")
    if limit_int > 2000:
        raise ValueError(f"limit must be <= 2000, got {limit_int}")

    statuses = [s.strip() for s in status.split(",") if s.strip()] if status \
        else [TARGET_PENDING, TARGET_RETRYING, TARGET_FAILED]

    clauses = ["t.schedule_at IS NOT NULL", "t.schedule_at != ''"]
    params: list = []
    placeholders = ",".join("?" * len(statuses))
    clauses.append(f"t.status IN ({placeholders})")
    params.extend(statuses)
    if month:
        clauses.append("t.schedule_at LIKE ?")
        params.append(month + "%")
    if platform:
        clauses.append("j.platform = ?")
        params.append(platform)
    if workspace_id is not None:
        clauses.append("j.workspace_id = ?")
        params.append(workspace_id)

    sql = f"""
        SELECT t.*, j.platform, j.profile_id, j.status AS job_status,
               j.payload_json
        FROM publish_job_targets t
        JOIN publish_jobs j ON j.id = t.job_id
        WHERE {" AND ".join(clauses)}
        ORDER BY t.schedule_at ASC
        LIMIT ?
    """
    params.append(limit_int)

    results: list[tuple[dict, dict]] = []
    with _connect(db_path) as conn:
        for row in conn.execute(sql, params).fetchall():
            target = dict(row)
            payload_text = target.pop("payload_json") or "{}"
            try:
                payload = json.loads(payload_text) if payload_text else {}
            except (json.JSONDecodeError, AttributeError):
                payload = {}

            # Title resolution: explicit title → top-level message → a single
            # draft.message → artifact basename → fallback. Each source is
            # snipped via _action_title, which also skips stringified-JSON
            # legacy captions (see its docstring).
            title = _action_title(payload)
            job_meta = {
                "platform": target.pop("platform"),
                "profile_id": target.pop("profile_id"),
                "job_status": target.pop("job_status"),
                "title": title,
            }
            results.append((target, job_meta))
    return results


def _target_context(
    conn: sqlite3.Connection,
    target_id: int,
    *,
    workspace_id: str | None,
) -> sqlite3.Row:
    """Load a target row joined with its job, enforcing tenant isolation.

    Uses the legacy plain SELECT when ``workspace_id`` is ``None`` (works on
    pre-migration DBs that lack the column); scopes to the workspace when one
    is passed (requires the 0017 ``workspace_id`` migration, present in
    multi-tenant production). Mismatched ownership surfaces as not-found.
    """
    if workspace_id is None:
        row = conn.execute(
            "SELECT job_id, status, schedule_at FROM publish_job_targets WHERE id = ?",
            (target_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"Target not found: id={target_id}")
        return row
    try:
        row = conn.execute(
            """
            SELECT j.id AS job_id, j.workspace_id AS job_workspace,
                   t.status, t.schedule_at
            FROM publish_job_targets t
            JOIN publish_jobs j ON j.id = t.job_id
            WHERE t.id = ?
            """,
            (target_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        # Pre-migration DB without the workspace_id column: fall back to the
        # legacy scoping (no column) and treat the target as reachable, which
        # is the correct behaviour for single-tenant databases.
        row = conn.execute(
            "SELECT job_id, status, schedule_at FROM publish_job_targets WHERE id = ?",
            (target_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"Target not found: id={target_id}")
        return row
    if row is None:
        raise LookupError(f"Target not found: id={target_id}")
    if row["job_workspace"] != workspace_id:
        raise LookupError(f"Target not found: id={target_id}")
    return row


def reschedule_target(
    target_id: int,
    schedule_at: str,
    *,
    workspace_id: str | None = None,
    db_path: Path | None = None,
) -> Target:
    """Move a non-running target to a new time.

    Guarded on status NOT IN (running, succeeded) — a browser upload that is
    already in flight cannot be relocated, and a finished post should not be
    rewound. When ``workspace_id`` is given, a target owned by another
    workspace is treated as not found (tenant isolation).
    """

    candidate = schedule_at.strip()
    if not candidate:
        raise ValueError("scheduleAt must be a non-empty ISO datetime")
    candidate, _ = _normalise_due(candidate)
    if not candidate:
        raise ValueError("scheduleAt must be a non-empty ISO datetime")

    with _connect(db_path) as conn:
        row = _target_context(conn, target_id, workspace_id=workspace_id)

        cursor = conn.execute(
            """
            UPDATE publish_job_targets
            SET schedule_at = ?, status = ?, last_error = NULL,
                attempts = 0, finished_at = NULL
            WHERE id = ? AND status NOT IN (?, ?)
            """,
            (candidate, TARGET_PENDING, target_id,
             TARGET_RUNNING, TARGET_SUCCEEDED),
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise ValueError(
                f"Cannot reschedule target {target_id} in status {row['status']}")
        _recount_job(conn, row["job_id"])
        conn.commit()
        refreshed = conn.execute(
            "SELECT * FROM publish_job_targets WHERE id = ?", (target_id,)
        ).fetchone()
        return _row_to_target(refreshed)


def cancel_target(
    target_id: int,
    *,
    workspace_id: str | None = None,
    db_path: Path | None = None,
) -> Target:
    """Cancel one pending/retrying target.

    A ``running`` target is untouched here — the running executor can't be
    aborted mid-upload (see the worker notes above) and the whole job gives
    the same guarantee via ``cancel_job``. When ``workspace_id`` is given, a
    target owned by another workspace is treated as not found.
    """

    with _connect(db_path) as conn:
        row = _target_context(conn, target_id, workspace_id=workspace_id)

        cursor = conn.execute(
            """
            UPDATE publish_job_targets
            SET status = ?, finished_at = ?
            WHERE id = ? AND status IN (?, ?)
            """,
            (TARGET_CANCELLED, _now_iso(), target_id,
             TARGET_PENDING, TARGET_RETRYING),
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise ValueError(
                f"Cannot cancel target {target_id} in status {row['status']}")
        _recount_job(conn, row["job_id"])
        conn.commit()
        refreshed = conn.execute(
            "SELECT * FROM publish_job_targets WHERE id = ?", (target_id,)
        ).fetchone()
        return _row_to_target(refreshed)


def resubmit_target(
    target_id: int,
    *,
    workspace_id: str | None = None,
    db_path: Path | None = None,
) -> Target:
    """Re-queue a failed (or cancelled) target.

    Preserves a still-future ``schedule_at``; clears it when the time has
    passed so the worker claims the target immediately. When ``workspace_id``
    is given, a target owned by another workspace is treated as not found.
    """

    now = _now_iso()
    with _connect(db_path) as conn:
        row = _target_context(conn, target_id, workspace_id=workspace_id)

        new_schedule, _ = _normalise_due(row["schedule_at"], now)

        cursor = conn.execute(
            """
            UPDATE publish_job_targets
            SET status = ?, attempts = 0, last_error = NULL,
                started_at = NULL, finished_at = NULL, schedule_at = ?
            WHERE id = ? AND status IN (?, ?)
            """,
            (TARGET_PENDING, new_schedule, target_id,
             TARGET_FAILED, TARGET_CANCELLED),
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise ValueError(
                f"Cannot resubmit target {target_id} in status {row['status']}")
        _recount_job(conn, row["job_id"])
        conn.commit()
        refreshed = conn.execute(
            "SELECT * FROM publish_job_targets WHERE id = ?", (target_id,)
        ).fetchone()
        return _row_to_target(refreshed)


def _normalise_due(schedule: str | None, now: str | None = None) -> tuple[str | None, bool]:
    """Normalise an ``schedule_at`` value into a tz-naive UTC string if it is
    still in the future, else ``None`` (worker picks it up immediately).

    Accepts the canonical ``YYYY-MM-DDTHH:MM:SS`` naive-UTC shape the
    orchestrator writes, plus simple offsets (``+08:00`` / ``Z``) that the
    legacy file-scheduling path produced. Returns ``(kept_schedule, is_due)``.
    """
    if not schedule:
        return None, True
    text = str(schedule).strip()
    if not text:
        return None, True
    base = now or _now_iso()
    try:
        parsed = datetime.fromisoformat(text)
        due = datetime.fromisoformat(base)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return (parsed.isoformat(timespec="seconds") if parsed > due else None,
                parsed <= due)
    except ValueError:
        # Non-parseable legacy value: keep it, the worker's own claim clause
        # (string comparison) still decides. Do not silently drop data.
        return text, False


def _recount_job(conn: sqlite3.Connection, job_id: int) -> None:
    """Recompute job counters/status after a target-level mutation.

    ``mark_target_*`` increments counters on the running->terminal transition,
    but reschedule/cancel/resubmit move targets between non-running states and
    bypass those counters entirely. Recompute from the source of truth.

    Unlike ``_maybe_finalise_job`` this recomputes *counts* too, and it treats
    ``running``/``retrying`` targets as in-flight (never finalises a job while
    a target is mid-flight, and never drops a job's finished_at once set).
    """

    row = conn.execute(
        """
        SELECT
            SUM(status = ?) AS completed,
            SUM(status = ?) AS failed,
            SUM(status = ?) AS cancelled,
            SUM(status = ?) AS pending,
            SUM(status IN (?, ?)) AS in_flight
        FROM publish_job_targets WHERE job_id = ?
        """,
        (TARGET_SUCCEEDED, TARGET_FAILED, TARGET_CANCELLED, TARGET_PENDING,
         TARGET_RUNNING, TARGET_RETRYING, job_id),
    ).fetchone()
    if row is None:
        return
    completed = row["completed"] or 0
    failed = row["failed"] or 0
    cancelled = row["cancelled"] or 0
    pending = row["pending"] or 0
    in_flight = row["in_flight"] or 0

    if completed + failed + cancelled + pending + in_flight == 0:
        return  # no targets at all — leave the job row alone

    if in_flight > 0:
        # A target is mid-upload: the job is running, matching what
        # claim_next_targets wrote when the first target was claimed.
        job_status = JOB_RUNNING
    elif pending > 0:
        job_status = JOB_PENDING
    elif failed > 0 or completed > 0:
        # Any terminal target: partial success reads as failed (same rule as
        # _maybe_finalise_job) unless everything succeeded or cancelled.
        job_status = JOB_FAILED if failed > 0 else (
            JOB_SUCCEEDED if completed > 0 else JOB_CANCELLED)
    else:
        job_status = JOB_CANCELLED

    conn.execute(
        """
        UPDATE publish_jobs
        SET completed_targets = ?, failed_targets = ?,
            status = ?,
            finished_at = CASE
                WHEN ? = ? THEN NULL
                ELSE COALESCE(finished_at, ?)
            END
        WHERE id = ?
        """,
        (completed, failed, job_status, job_status, JOB_PENDING,
         _now_iso(), job_id),
    )


def list_tiktok_publish_statuses(
    job_id: str, *, db_path: Path | None = None
) -> list[dict]:
    """Return all TikTok publish status rows for a given job_id."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM tiktok_publish_status WHERE job_id = ? ORDER BY created_at",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]
