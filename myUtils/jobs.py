"""Publish-job persistence and lifecycle.

A *publish job* is one user intent to publish a set of files to a set of
accounts on a single platform. Each (account, file) pair becomes a *target*,
which is the unit of retry and progress tracking.

Claims are durable leases, fenced by a fresh token on every attempt. Delayed
retries and delivery polls use ``next_attempt_at`` rather than a sleeping worker.
``blocked`` and ``needs_review`` require operator attention; ``awaiting_delivery``
is eligible only for receipt reconciliation, not a second submission.

Every state mutation and parent recount shares one SQLite write transaction.
Tokenless worker transitions remain available for legacy callers; independent
workers must pass the claim's token to obtain stale-completion fencing.

Idempotency keys identify the original, immutable enqueue intent (including
profile/workspace). Reusing an explicit key with another intent is rejected.

This module is deliberately framework-agnostic: it speaks SQLite + dataclasses
and exposes pure functions. The async worker (``myUtils/worker.py``) orchestrates
target execution on top of it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from myUtils.account_identity import canonical_account_key
from myUtils.secret_redaction import is_secret_key
from utils.conf_defaults import BASE_DIR, default_db_path

DB_PATH = default_db_path()


# Job lifecycle states.
JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_SUCCEEDED = "succeeded"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"
JOB_BLOCKED = "blocked"
JOB_AWAITING_DELIVERY = "awaiting_delivery"
JOB_NEEDS_REVIEW = "needs_review"

JOB_TERMINAL = {JOB_SUCCEEDED, JOB_FAILED, JOB_CANCELLED}

# Target lifecycle states.
TARGET_PENDING = "pending"
TARGET_RUNNING = "running"
TARGET_SUCCEEDED = "succeeded"
TARGET_RETRYING = "retrying"
TARGET_FAILED = "failed"
TARGET_CANCELLED = "cancelled"
TARGET_BLOCKED = "blocked"
TARGET_AWAITING_DELIVERY = "awaiting_delivery"
TARGET_NEEDS_REVIEW = "needs_review"

TARGET_CLAIMABLE = {TARGET_PENDING, TARGET_RETRYING, TARGET_AWAITING_DELIVERY}
TARGET_TERMINAL = {TARGET_SUCCEEDED, TARGET_FAILED, TARGET_CANCELLED}
TARGET_STATES = TARGET_CLAIMABLE | TARGET_TERMINAL | {
    TARGET_RUNNING, TARGET_BLOCKED, TARGET_NEEDS_REVIEW,
}
CHECKPOINT_MAX_BYTES = 64 * 1024
CLAIM_MAX_LIMIT = 500


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
        return _safe_data(asdict(self))


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
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: str | None = None
    next_attempt_at: str | None = None
    error_category: str | None = None
    checkpoint: dict = field(default_factory=dict)
    delivery_state: str | None = None

    def to_dict(self) -> dict:
        # The lease token is a write capability, not public queue metadata.
        out = asdict(self)
        out.pop("lease_token", None)
        return _safe_data(out)


@dataclass(slots=True)
class JobSpec:
    """Caller-facing input for ``enqueue_job``.

    ``targets`` is a list of (account_ref, file_ref, optional schedule_at) tuples.
    """

    platform: str
    payload: dict
    targets: Sequence[tuple[str, str, datetime | str | None]]
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

    return Path(db_path if db_path is not None else DB_PATH)


@contextmanager
def _connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    resolved = _resolve_db_path(db_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    # A long UI read and the worker's write can overlap; without a generous
    # busy timeout the loser of that race fails immediately ("database is
    # locked"), which the browser surfaces as a schedule request that never
    # loads. Waits instead of erroring. (journal_mode stays the default
    # "delete": the backup/offload scripts copy database.db as a plain file,
    # and WAL content lives in side files those copies would miss.)
    conn = sqlite3.connect(resolved, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _transaction(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def _now_iso() -> str:
    # Use timezone-aware UTC and strip the offset so the isoformat string
    # remains compatible with the existing ``DATETIME`` SQLite columns.
    return datetime.now(tz=timezone.utc).replace(tzinfo=None).isoformat(timespec="microseconds")


_ISO_DATETIME = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)?\Z"
)


def _utc_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and _ISO_DATETIME.fullmatch(value.strip()):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("scheduleAt must be a valid ISO datetime") from exc
    else:
        raise ValueError("scheduleAt must be a valid ISO datetime")
    if parsed.tzinfo is not None:
        try:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        except (ValueError, OverflowError) as exc:
            raise ValueError("scheduleAt is outside the supported UTC datetime range") from exc
    return parsed


def _seconds(value: float, *, positive: bool = False) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("seconds must be finite and non-negative") from exc
    if not math.isfinite(seconds) or seconds < 0 or (positive and seconds == 0):
        raise ValueError("seconds must be finite and positive" if positive
                         else "seconds must be finite and non-negative")
    return seconds


def _after_seconds(seconds: float, *, now: str | None = None) -> str:
    try:
        return (_utc_datetime(now or _now_iso()) + timedelta(seconds=seconds)).isoformat()
    except OverflowError as exc:
        raise ValueError("delay is outside the supported datetime range") from exc


def _safe_data(value):
    """Copy JSON data, excluding credentials/config blobs (also nested ones)."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if is_secret_key(normalized) or normalized in {
                "authorization", "proxyauthorization", "headers", "config",
                "configjson", "accountconfig", "account", "accounts", "auth",
                "oauth", "storagestate", "cookiepath", "sessionid",
            }:
                continue
            out[key] = _safe_data(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_safe_data(item) for item in value]
    if isinstance(value, str):
        # Redact common credentials embedded in error strings/URLs as well.
        value = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", r"\1 [redacted]", value)
        return re.sub(
            r"(?i)(\b(?:access[_-]?token|refresh[_-]?token|api[_-]?key|"
            r"client[_-]?secret|password|authorization)\s*[=:]\s*)[^\s&,;]+",
            r"\1[redacted]", value,
        )
    return value


def _decode_checkpoint(text: str | None) -> dict:
    try:
        checkpoint = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {"checkpoint_invalid": True}
    return _safe_data(checkpoint) if isinstance(checkpoint, dict) else {"checkpoint_invalid": True}


def _merge_checkpoint(current: str | None, checkpoint: dict) -> tuple[str, dict]:
    if not isinstance(checkpoint, dict):
        raise ValueError("checkpoint must be a JSON object")
    saved = _decode_checkpoint(current)
    merged = {**saved, **_safe_data(checkpoint)}
    # Evidence cannot be accidentally erased by an incremental checkpoint.
    if saved.get("submission_started") is True:
        merged["submission_started"] = True
    if saved.get("provider_receipt") and not merged.get("provider_receipt"):
        merged["provider_receipt"] = saved["provider_receipt"]
    try:
        text = json.dumps(merged, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("checkpoint must contain JSON-compatible values") from exc
    if len(text.encode("utf-8")) > CHECKPOINT_MAX_BYTES:
        raise ValueError(f"checkpoint exceeds {CHECKPOINT_MAX_BYTES} bytes")
    return text, merged


def _chunk(values: Sequence[int], size: int = 400) -> Iterator[Sequence[int]]:
    """Split ids for ``IN (...)`` clauses; SQLite's variable limit is the cap."""
    for start in range(0, len(values), size):
        yield values[start:start + size]


#: Campaign states in which the media is not ready to be published. A target
#: belonging to one of these must not be claimed: its artifacts may not exist
#: yet, so the publish fails with "media artifacts were supplied but none is a
#: supported image" and burns one of its three attempts. That is what happened
#: to 11 targets on 2026-10-29 — the auto-publisher creates the campaign and
#: queues targets immediately, while prep still runs in the background, and the
#: first scheduled slot was already in the past.
_NOT_READY_CAMPAIGN_STATES = ("preparing", "needs_review", "draft")


def _claimable_clause(now: str) -> tuple[str, list]:
    """Due submissions or receipt polls; terminal/review parents never reopen.

    SQLite date parsing also handles legacy offsets and fails closed on invalid
    old schedules. Polls bypass campaign preparation and submission scheduling.
    """
    placeholders = ", ".join("?" * len(_NOT_READY_CAMPAIGN_STATES))
    return (
        "status IN (?, ?, ?) "
        "AND (next_attempt_at IS NULL OR julianday(next_attempt_at) <= julianday(?)) "
        "AND EXISTS (SELECT 1 FROM publish_jobs j WHERE j.id = publish_job_targets.job_id "
        "  AND j.status IN (?, ?, ?)) "
        "AND (status = ? OR ("
        "  (schedule_at IS NULL OR schedule_at = '' OR julianday(schedule_at) <= julianday(?)) "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM campaign_posts cp JOIN campaigns c ON c.id = cp.campaign_id"
        "    WHERE ('campaign_post:' || cp.id) = publish_job_targets.file_ref"
        f"      AND c.status IN ({placeholders})"
        "  )))",
        [TARGET_PENDING, TARGET_RETRYING, TARGET_AWAITING_DELIVERY, now,
         JOB_PENDING, JOB_RUNNING, JOB_AWAITING_DELIVERY, TARGET_AWAITING_DELIVERY,
         now, *_NOT_READY_CAMPAIGN_STATES],
    )


def _eligible_targets(conn: sqlite3.Connection, now: str, excluded_accounts=()):
    """Stream due rows, with canonical account exclusion across all processes."""
    cache: dict[str, str] = {}

    def key(ref: str) -> str:
        if ref not in cache:
            cache[ref] = canonical_account_key(conn, ref)
        return cache[ref]

    held = {key(ref) for ref in excluded_accounts}
    for row in conn.execute(
        "SELECT account_ref FROM publish_job_targets WHERE status = ? "
        "AND (lease_expires_at IS NULL OR julianday(lease_expires_at) IS NULL "
        "     OR julianday(lease_expires_at) > julianday(?))",
        (TARGET_RUNNING, now),
    ):
        held.add(key(row["account_ref"]))
    clause, params = _claimable_clause(now)
    cursor = conn.execute(
        f"SELECT * FROM publish_job_targets WHERE {clause} "
        "ORDER BY COALESCE(julianday(schedule_at), 0), id", params,
    )
    for row in cursor:
        account_key = key(row["account_ref"])
        if account_key not in held:
            held.add(account_key)
            yield row


def has_claimable_targets(*, db_path: Path | None = None) -> bool:
    """Whether a due target has a currently unleased canonical account."""
    with _connect(db_path) as conn:
        return next(_eligible_targets(conn, _now_iso()), None) is not None


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
        lease_owner=row["lease_owner"],
        lease_token=row["lease_token"],
        lease_expires_at=row["lease_expires_at"],
        next_attempt_at=row["next_attempt_at"],
        error_category=row["error_category"],
        checkpoint=_decode_checkpoint(row["checkpoint_json"]),
        delivery_state=row["delivery_state"],
    )


def _canonical(value):
    if isinstance(value, dict):
        return {k: _canonical(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _schedule_canonical(sched: datetime | str | None) -> str:
    return "" if sched is None or sched == "" else _utc_datetime(sched).isoformat()


def _deduplicated_targets(spec: JobSpec) -> list[tuple[str, str, str | None]]:
    seen: set[tuple[str, str]] = set()
    targets = []
    for account_ref, file_ref, schedule_at in spec.targets:
        pair = (str(account_ref), str(file_ref))
        if pair not in seen:
            seen.add(pair)
            targets.append((*pair, _schedule_canonical(schedule_at) or None))
    return targets


def _intent_hash(spec: JobSpec, workspace_id: str | None) -> str:
    canonical = {
        "platform": spec.platform,
        "profile_id": spec.profile_id,
        "workspace_id": workspace_id,
        "payload": _canonical(spec.payload),
        "targets": sorted((a, f, s or "") for a, f, s in _deduplicated_targets(spec)),
        "extra": _canonical(spec.extra_canonical),
    }
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def derive_idempotency_key(spec: JobSpec, *, workspace_id: str | None = None) -> str:
    """Stable intent identity, scoped to both profile and workspace."""
    return "auto-" + _intent_hash(spec, workspace_id)[:32]


# --------------------------- enqueue / lookup ---------------------------


def enqueue_job(spec: JobSpec, *, workspace_id: str | None = None, db_path: Path | None = None,
                campaign_post_id: int | None = None,
                prep_fence: tuple[str, int] | None = None) -> Job:
    """Atomically insert or return the same original intent, never a different one.

    ``BEGIN IMMEDIATE`` serializes lookup + insertion across independent
    processes. The immutable hash survives later payload/schedule mutations.
    Legacy rows without a hash are compared against their stored intent once.
    """
    if not spec.targets:
        raise ValueError("Job must have at least one target")
    targets = _deduplicated_targets(spec)
    intent_hash = _intent_hash(spec, workspace_id)
    key = spec.idempotency_key or "auto-" + intent_hash[:32]
    payload_json = json.dumps(spec.payload, sort_keys=True, ensure_ascii=False, allow_nan=False)

    with _transaction(db_path) as conn:
        if campaign_post_id is not None:
            linked = conn.execute(
                "SELECT c.id, c.status, c.metadata_json, c.workspace_id "
                "FROM campaign_posts p JOIN campaigns c ON c.id=p.campaign_id WHERE p.id=?",
                (campaign_post_id,),
            ).fetchone()
            if linked is None or linked["id"] != spec.payload.get("campaignId"):
                raise ValueError("Campaign post no longer belongs to this submission")
            metadata = json.loads(linked["metadata_json"] or "{}")
            lease = metadata.get("_prepLease") or {}
            if prep_fence is not None or lease:
                observed = (str(lease.get("owner") or ""), int(metadata.get("_prepAttempts") or 0))
                if linked["status"] != "preparing" or observed != prep_fence:
                    from myUtils.delivery import LeaseLost
                    raise LeaseLost("Preparation ownership changed before enqueue")
            if linked["workspace_id"] != workspace_id:
                raise ValueError("Campaign post belongs to a different workspace")
        existing = conn.execute(
            "SELECT * FROM publish_jobs WHERE idempotency_key = ?", (key,),
        ).fetchone()
        if existing is not None:
            if existing["workspace_id"] != workspace_id:
                raise ValueError("Idempotency key already belongs to a different publish intent")
            previous_hash = existing["intent_hash"]
            if previous_hash is None:
                old_targets = conn.execute(
                    "SELECT account_ref, file_ref, schedule_at FROM publish_job_targets "
                    "WHERE job_id = ? ORDER BY id", (existing["id"],),
                ).fetchall()
                previous = JobSpec(
                    platform=existing["platform"], payload=json.loads(existing["payload_json"]),
                    profile_id=existing["profile_id"], targets=[tuple(row) for row in old_targets],
                    # Legacy rows did not persist this optional identity-only input.
                    extra_canonical=spec.extra_canonical,
                )
                previous_hash = _intent_hash(previous, existing["workspace_id"])
            if previous_hash != intent_hash:
                raise ValueError("Idempotency key already belongs to a different publish intent")
            if existing["intent_hash"] is None:
                conn.execute("UPDATE publish_jobs SET intent_hash = ? WHERE id = ?",
                             (intent_hash, existing["id"]))
            return _row_to_job(existing)

        cursor = conn.execute(
            """
            INSERT INTO publish_jobs
                (idempotency_key, profile_id, platform, payload_json, status,
                 total_targets, workspace_id, intent_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (key, spec.profile_id, spec.platform, payload_json, JOB_PENDING,
             len(targets), workspace_id, intent_hash),
        )
        job_id = cursor.lastrowid
        conn.executemany(
            """
            INSERT INTO publish_job_targets
                (job_id, account_ref, file_ref, schedule_at, status, workspace_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [(job_id, account_ref, file_ref, schedule_at, TARGET_PENDING, workspace_id)
             for account_ref, file_ref, schedule_at in targets],
        )
        if campaign_post_id is not None:
            conn.execute(
                "UPDATE campaign_posts SET status='queued', last_published_job_id=?, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (job_id, campaign_post_id),
            )
        return _row_to_job(
            conn.execute("SELECT * FROM publish_jobs WHERE id = ?", (job_id,)).fetchone()
        )


def update_job_payload(job_id: int, payload: dict, *, db_path: Path | None = None) -> None:
    """Persist a worker-maintained payload update without changing job state."""
    payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE publish_jobs SET payload_json=? WHERE id=?",
            (payload_json, job_id),
        )
        if cursor.rowcount != 1:
            raise LookupError(f"Job not found: id={job_id}")
        conn.commit()


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
    offset: int = 0,
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
    try:
        offset_int = max(0, int(offset))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"offset must be a non-negative integer, got {offset!r}") from exc

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
    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend((limit_int, offset_int))

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


def get_jobs_by_ids(
    job_ids: Iterable[int],
    *,
    workspace_id: str | None = None,
    db_path: Path | None = None,
) -> dict[int, Job]:
    """Batch form of :func:`get_job` for list views.

    One connection and one query per chunk instead of a connect+SELECT per id;
    the entity/queue endpoints hydrate hundreds of jobs per request, so the
    per-row form dominated their latency. Missing ids are simply absent from
    the result, matching the ``LookupError``-skipping callers.
    """
    ids = sorted({int(job_id) for job_id in job_ids})
    if not ids:
        return {}
    found: dict[int, Job] = {}
    with _connect(db_path) as conn:
        for chunk in _chunk(ids):
            placeholders = ",".join("?" * len(chunk))
            query = f"SELECT * FROM publish_jobs WHERE id IN ({placeholders})"
            params: list = list(chunk)
            if workspace_id is not None:
                query += " AND workspace_id = ?"
                params.append(workspace_id)
            for row in conn.execute(query, params):
                job = _row_to_job(row)
                found[job.id] = job
    return found


def list_targets_for_jobs(
    job_ids: Iterable[int], *, db_path: Path | None = None
) -> dict[int, list[Target]]:
    """Batch form of :func:`list_targets`; every requested id gets a list
    (empty when the job has no targets) so callers can index directly."""
    ids = sorted({int(job_id) for job_id in job_ids})
    grouped: dict[int, list[Target]] = {job_id: [] for job_id in ids}
    if not ids:
        return grouped
    with _connect(db_path) as conn:
        for chunk in _chunk(ids):
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT * FROM publish_job_targets WHERE job_id IN ({placeholders}) "
                "ORDER BY job_id, id",
                chunk,
            ).fetchall()
            for row in rows:
                target = _row_to_target(row)
                grouped.setdefault(target.job_id, []).append(target)
    return grouped


def get_target(target_id: int, *, db_path: Path | None = None) -> Target:
    """Refresh a target, including its private lease token and decoded checkpoint."""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM publish_job_targets WHERE id = ?", (target_id,)).fetchone()
    if row is None:
        raise LookupError(f"Target not found: id={target_id}")
    return _row_to_target(row)


def cancel_job(job_id: int, *, db_path: Path | None = None) -> Job:
    """Cancel further processing, atomically fencing every nonterminal target.

    This cannot undo a provider-side submission already in flight. Checkpoints
    are retained for review, and the explicit parent cancellation is sticky.
    """
    with _transaction(db_path) as conn:
        job_row = conn.execute("SELECT * FROM publish_jobs WHERE id = ?", (job_id,)).fetchone()
        if job_row is None:
            raise LookupError(f"Job not found: id={job_id}")
        if job_row["status"] in {JOB_SUCCEEDED, JOB_FAILED}:
            return _row_to_job(job_row)
        now = _now_iso()
        conn.execute("UPDATE publish_jobs SET status = ?, finished_at = ? WHERE id = ?",
                     (JOB_CANCELLED, job_row["finished_at"] or now, job_id))
        conn.execute(
            """
            UPDATE publish_job_targets
            SET status = ?, finished_at = ?, next_attempt_at = NULL,
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
            WHERE job_id = ? AND status NOT IN (?, ?, ?)
            """,
            (TARGET_CANCELLED, now, job_id, TARGET_SUCCEEDED, TARGET_FAILED, TARGET_CANCELLED),
        )
        _recount_job(conn, job_id)
        return _row_to_job(conn.execute("SELECT * FROM publish_jobs WHERE id = ?", (job_id,)).fetchone())


# --------------------------- worker-facing transitions ---------------------------


def claim_next_targets(
    *,
    limit: int = 1,
    excluded_accounts: Iterable[str] = (),
    db_path: Path | None = None,
    owner: str | None = None,
    lease_seconds: float = 180,
) -> list[Target]:
    """Lease at most one due target per canonical provider account, globally.

    Receipt polls retain ``delivery_state='awaiting_delivery'`` when claimed;
    they do not consume another submission attempt. All claims receive a new
    random token, including legacy calls without an owner. A batch is capped
    at ``CLAIM_MAX_LIMIT``; zero remains a supported no-op.
    """
    limit = int(limit)
    if limit < 0:
        raise ValueError("limit must be non-negative")
    if limit == 0:
        return []
    limit = min(limit, CLAIM_MAX_LIMIT)
    duration = _seconds(lease_seconds, positive=True)
    if owner is not None:
        _worker_label(owner)
    claimed = []
    with _transaction(db_path) as conn:
        now = _now_iso()
        expires = _after_seconds(duration, now=now)
        rows = []
        candidates = _eligible_targets(conn, now, excluded_accounts)
        try:
            for row in candidates:
                rows.append(row)
                if len(rows) == limit:
                    break
        finally:
            candidates.close()
        affected_jobs = set()
        for row in rows:
            conn.execute(
                """
                UPDATE publish_job_targets
                SET status = ?, attempts = attempts + ?, started_at = ?, finished_at = NULL,
                    lease_owner = ?, lease_token = ?, lease_expires_at = ?, next_attempt_at = NULL
                WHERE id = ? AND status = ?
                """,
                (TARGET_RUNNING, int(row["status"] != TARGET_AWAITING_DELIVERY), now,
                 owner, secrets.token_urlsafe(32), expires, row["id"], row["status"]),
            )
            conn.execute("UPDATE publish_jobs SET started_at = COALESCE(started_at, ?) WHERE id = ?",
                         (now, row["job_id"]))
            affected_jobs.add(row["job_id"])
            claimed.append(_row_to_target(conn.execute(
                "SELECT * FROM publish_job_targets WHERE id = ?", (row["id"],),
            ).fetchone()))
        for job_id in affected_jobs:
            _recount_job(conn, job_id)
    return claimed


def _running_target(conn, target_id, lease_token, *, missing_error=False):
    row = conn.execute("SELECT * FROM publish_job_targets WHERE id = ?", (target_id,)).fetchone()
    if row is None:
        if missing_error:
            raise LookupError(f"Target not found: id={target_id}")
        return None
    if row["status"] != TARGET_RUNNING:
        return None
    if lease_token is not None and row["lease_token"] != lease_token:
        return None
    if lease_token is None and row["lease_owner"] is not None:
        # A legacy executor must not complete a replacement worker's claim.
        # Tokenless compatibility applies only to unowned (legacy) claims.
        return None
    return row


def _write_target_state(
    conn, row, status, *, error=None, error_category=None, next_attempt_at=None,
    checkpoint_json=None, delivery_state=None,
) -> bool:
    """Caller holds BEGIN IMMEDIATE; still fence by the selected generation."""
    cursor = conn.execute(
        """
        UPDATE publish_job_targets
        SET status = ?, last_error = ?, error_category = ?, finished_at = ?,
            next_attempt_at = ?, checkpoint_json = ?, delivery_state = ?,
            lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
        WHERE id = ? AND status = ? AND lease_token IS ?
        """,
        (status, _safe_data(str(error))[:2000] if error is not None else None,
         _safe_data(str(error_category))[:100] if error_category is not None else None,
         _now_iso() if status in TARGET_TERMINAL else None, next_attempt_at,
         checkpoint_json if checkpoint_json is not None else row["checkpoint_json"],
         delivery_state, row["id"], TARGET_RUNNING, row["lease_token"]),
    )
    return cursor.rowcount == 1


def _transition_target(
    target_id, status, *, error=None, error_category=None, delay_seconds=None,
    checkpoint=None, delivery_state=None, lease_token=None, db_path=None, missing_error=False,
) -> bool:
    if delay_seconds is not None:
        delay_seconds = _seconds(delay_seconds)
    with _transaction(db_path) as conn:
        row = _running_target(conn, target_id, lease_token, missing_error=missing_error)
        if row is None:
            return False
        checkpoint_json = None
        if checkpoint is not None:
            checkpoint_json, _ = _merge_checkpoint(row["checkpoint_json"], checkpoint)
        changed = _write_target_state(
            conn, row, status, error=error, error_category=error_category,
            next_attempt_at=_after_seconds(delay_seconds) if delay_seconds is not None else None,
            checkpoint_json=checkpoint_json, delivery_state=delivery_state,
        )
        if changed:
            _recount_job(conn, row["job_id"])
        return changed


def mark_target_success(
    target_id: int, *, db_path: Path | None = None, lease_token: str | None = None,
) -> bool:
    """CAS running -> succeeded, release its lease, and recount the parent."""
    return _transition_target(target_id, TARGET_SUCCEEDED, delivery_state="delivered",
                              lease_token=lease_token, db_path=db_path, missing_error=True)


def mark_target_retry(
    target_id: int, error: str, *, db_path: Path | None = None,
    delay_seconds: float = 0, error_category: str | None = None, lease_token: str | None = None,
) -> bool:
    """CAS running -> retrying, durably storing the delay rather than sleeping."""
    return _transition_target(target_id, TARGET_RETRYING, error=error,
                              delay_seconds=delay_seconds, error_category=error_category,
                              lease_token=lease_token, db_path=db_path)


def mark_target_failed(
    target_id: int, error: str, *, db_path: Path | None = None,
    lease_token: str | None = None, error_category: str | None = None,
) -> bool:
    """CAS running -> failed. Late cancellation/stale-token completions are no-ops."""
    return _transition_target(target_id, TARGET_FAILED, error=error, delivery_state="failed",
                              error_category=error_category, lease_token=lease_token,
                              db_path=db_path, missing_error=True)


def mark_target_blocked(
    target_id: int, error: str, *, lease_token: str | None = None,
    error_category: str = "blocked", db_path: Path | None = None,
) -> bool:
    """Release a claim into a non-claimable prerequisite/account-blocked state."""
    return _transition_target(target_id, TARGET_BLOCKED, error=error, error_category=error_category,
                              lease_token=lease_token, db_path=db_path)


def mark_target_needs_review(
    target_id: int, error: str, *, lease_token: str | None = None,
    error_category: str = "needs_review", db_path: Path | None = None,
) -> bool:
    """Release an ambiguous delivery for manual review, never automatic resubmit."""
    return _transition_target(target_id, TARGET_NEEDS_REVIEW, error=error,
                              error_category=error_category, delivery_state="unknown",
                              lease_token=lease_token, db_path=db_path)


def mark_target_awaiting_delivery(
    target_id: int, checkpoint: dict, *, delay_seconds: float = 30,
    lease_token: str | None = None, db_path: Path | None = None,
) -> bool:
    """Persist/merge a safe receipt and schedule a later delivery poll."""
    return _transition_target(target_id, TARGET_AWAITING_DELIVERY, checkpoint=checkpoint,
                              delay_seconds=delay_seconds, delivery_state="awaiting_delivery",
                              lease_token=lease_token, db_path=db_path)


def renew_target_lease(
    target_id: int, lease_token: str, lease_seconds: float = 180, *, db_path: Path | None = None,
) -> bool:
    """Renew a still-live claim; an expired/cancelled/replaced lease cannot revive."""
    duration = _seconds(lease_seconds, positive=True)
    if not lease_token:
        return False
    with _transaction(db_path) as conn:
        now = _now_iso()
        cursor = conn.execute(
            "UPDATE publish_job_targets SET lease_expires_at = ? "
            "WHERE id = ? AND status = ? AND lease_token = ? "
            "AND julianday(lease_expires_at) > julianday(?)",
            (_after_seconds(duration, now=now), target_id, TARGET_RUNNING, lease_token, now),
        )
        return cursor.rowcount == 1


def save_target_checkpoint(
    target_id: int, checkpoint: dict, *, lease_token: str, db_path: Path | None = None,
) -> bool:
    """Merge a <=64 KiB JSON checkpoint, removing credential/config fields.

    Submission evidence is sticky. Only the current running generation may
    save it; no public target serialization exposes the lease capability.
    """
    if not lease_token:
        return False
    with _transaction(db_path) as conn:
        row = _running_target(conn, target_id, lease_token)
        if row is None:
            return False
        text, merged = _merge_checkpoint(row["checkpoint_json"], checkpoint)
        delivery_state = row["delivery_state"]
        if merged.get("provider_receipt"):
            delivery_state = "awaiting_delivery"
        elif merged.get("submission_started") is True:
            delivery_state = "submission_started"
        cursor = conn.execute(
            "UPDATE publish_job_targets SET checkpoint_json = ?, delivery_state = ? "
            "WHERE id = ? AND status = ? AND lease_token = ?",
            (text, delivery_state, target_id, TARGET_RUNNING, lease_token),
        )
        return cursor.rowcount == 1


def requeue_stale_running(
    *, older_than_minutes: int = 120, max_attempts: int = 3, db_path: Path | None = None,
) -> int:
    """Recover up to 500 abandoned claims without blindly resubmitting a post.

    Lease expiry supersedes started_at; the old age/budget policy applies only
    to pre-lease rows. A receipt resumes polling even after the retry budget;
    submission-started without a receipt (or corrupt evidence) needs review.
    """
    age = _seconds(older_than_minutes)
    budget = int(max_attempts)
    if budget < 1:
        raise ValueError("max_attempts must be positive")
    with _transaction(db_path) as conn:
        now = _now_iso()
        cutoff = _after_seconds(-age * 60, now=now)
        rows = conn.execute(
            """
            SELECT t.* FROM publish_job_targets t JOIN publish_jobs j ON j.id = t.job_id
            WHERE t.status = ? AND j.status NOT IN (?, ?, ?) AND (
                (t.lease_expires_at IS NOT NULL AND
                 (julianday(t.lease_expires_at) IS NULL OR julianday(t.lease_expires_at) <= julianday(?)))
                OR (t.lease_expires_at IS NULL AND
                    (julianday(t.started_at) IS NULL OR julianday(t.started_at) < julianday(?)))
            ) ORDER BY t.id LIMIT ?
            """,
            (TARGET_RUNNING, JOB_SUCCEEDED, JOB_FAILED, JOB_CANCELLED, now, cutoff, CLAIM_MAX_LIMIT),
        ).fetchall()
        affected_jobs = set()
        moved = 0
        for row in rows:
            checkpoint = _decode_checkpoint(row["checkpoint_json"])
            due = None
            delivery = None
            category = "stale_execution"
            if checkpoint.get("provider_receipt"):
                state, due, delivery = TARGET_AWAITING_DELIVERY, now, "awaiting_delivery"
                error = "stale execution: resume reconciliation of the recorded provider receipt"
            elif checkpoint.get("submission_started") is True or checkpoint.get("checkpoint_invalid"):
                state, delivery, category = TARGET_NEEDS_REVIEW, "unknown", "submission_unknown"
                error = "stale execution: submission may have started; manual delivery review required"
            elif row["attempts"] >= budget:
                state, delivery = TARGET_FAILED, "failed"
                error = "requeued as stale: worker died mid-run and retry budget exhausted"
            else:
                state, due = TARGET_RETRYING, now
                error = "requeued as stale: worker died before submission; will retry"
            if _write_target_state(conn, row, state, error=error, error_category=category,
                                   next_attempt_at=due, delivery_state=delivery):
                moved += 1
                affected_jobs.add(row["job_id"])
        for job_id in affected_jobs:
            _recount_job(conn, job_id)
        return moved


def _maybe_finalise_job(conn: sqlite3.Connection, job_id: int) -> None:
    """Compatibility alias: all paths now use the same row-based aggregation."""
    _recount_job(conn, job_id)


def _worker_label(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,199}", value):
        raise ValueError("worker id/role must be a short non-secret identifier")
    return value


def heartbeat_worker(
    worker_id: str, role: str = "publisher", draining: bool = False, *, db_path: Path | None = None,
) -> None:
    """Record worker liveness without storing process config or account data."""
    _worker_label(worker_id)
    _worker_label(role)
    with _transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO worker_heartbeats(worker_id, heartbeat_at, role, draining) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(worker_id) DO UPDATE SET heartbeat_at = excluded.heartbeat_at, "
            "role = excluded.role, draining = excluded.draining",
            (worker_id, _now_iso(), role, int(bool(draining))),
        )


def worker_health(*, db_path: Path | None = None, max_age_seconds: float = 90) -> dict:
    """Safe liveness view; ok requires a fresh, non-draining publisher."""
    max_age = _seconds(max_age_seconds)
    now = _utc_datetime(_now_iso())
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT worker_id, heartbeat_at, role, draining FROM worker_heartbeats "
            "ORDER BY heartbeat_at DESC LIMIT 1000",
        ).fetchall()
    workers = []
    for row in rows:
        try:
            age = (now - _utc_datetime(row["heartbeat_at"])).total_seconds()
        except (TypeError, ValueError):
            age = None
        alive = age is not None and 0 <= age <= max_age
        workers.append({"worker_id": row["worker_id"], "heartbeat_at": row["heartbeat_at"],
                        "role": row["role"], "draining": bool(row["draining"]),
                        "alive": alive, "age_seconds": round(age, 3) if age is not None else None})
    return {"ok": any(w["alive"] and not w["draining"] and w["role"] == "publisher" for w in workers),
            "workers": workers}


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
        else [TARGET_PENDING, TARGET_RETRYING, TARGET_FAILED, TARGET_BLOCKED,
              TARGET_AWAITING_DELIVERY, TARGET_NEEDS_REVIEW]

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
            target.pop("lease_token", None)
            target["checkpoint"] = _decode_checkpoint(target.pop("checkpoint_json", None))
            results.append((_safe_data(target), _safe_data(job_meta)))
    return results


def _target_context(
    conn: sqlite3.Connection, target_id: int, *, workspace_id: str | None,
) -> sqlite3.Row:
    """Load the target and enforce tenant ownership in the same write transaction."""
    sql = ("SELECT t.*, j.status AS job_status FROM publish_job_targets t "
           "JOIN publish_jobs j ON j.id = t.job_id WHERE t.id = ?")
    params: list = [target_id]
    if workspace_id is not None:
        sql += " AND j.workspace_id = ?"
        params.append(workspace_id)
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise LookupError(f"Target not found: id={target_id}")
    return row


def _check_resubmission(row, allowed: set[str]) -> None:
    if row["status"] not in allowed:
        raise ValueError(f"Cannot requeue target {row['id']} in status {row['status']}; "
                         "ambiguous deliveries require a new deliberate publish intent")
    checkpoint = _decode_checkpoint(row["checkpoint_json"])
    if row["status"] not in {TARGET_FAILED, TARGET_CANCELLED} and (
        checkpoint.get("submission_started") is True or checkpoint.get("provider_receipt")
        or checkpoint.get("checkpoint_invalid")
    ):
        raise ValueError("Submission evidence exists; reconcile delivery or create a new deliberate intent")


def _reset_target(conn, row, schedule_at) -> Target:
    conn.execute(
        """
        UPDATE publish_job_targets
        SET schedule_at = ?, status = ?, attempts = 0, last_error = NULL, error_category = NULL,
            started_at = NULL, finished_at = NULL, next_attempt_at = NULL,
            lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
            checkpoint_json = '{}', delivery_state = NULL
        WHERE id = ? AND status = ?
        """,
        (schedule_at, TARGET_PENDING, row["id"], row["status"]),
    )
    _recount_job(conn, row["job_id"], allow_reopen=True)
    return _row_to_target(conn.execute(
        "SELECT * FROM publish_job_targets WHERE id = ?", (row["id"],),
    ).fetchone())


def reschedule_target(
    target_id: int, schedule_at: str, *, workspace_id: str | None = None,
    db_path: Path | None = None,
) -> Target:
    """Atomically reschedule eligible work to a strictly valid future UTC time.

    Naive ISO inputs mean UTC; offsets are normalized. Running, succeeded and
    ambiguous-delivery targets cannot be rewound with the calendar action.
    """
    candidate = _utc_datetime(schedule_at)
    with _transaction(db_path) as conn:
        row = _target_context(conn, target_id, workspace_id=workspace_id)
        if candidate <= _utc_datetime(_now_iso()):
            raise ValueError("scheduleAt must be in the future")
        _check_resubmission(row, {TARGET_PENDING, TARGET_RETRYING, TARGET_FAILED,
                                  TARGET_CANCELLED, TARGET_BLOCKED})
        return _reset_target(conn, row, candidate.isoformat())


def cancel_target(
    target_id: int, *, workspace_id: str | None = None, db_path: Path | None = None,
) -> Target:
    """Cancel waiting/blocked/review work, retaining any delivery evidence.

    A running executor requires whole-job cancellation. Cancelling a receipt
    poll stops polling, not the submission that the provider already accepted.
    """
    with _transaction(db_path) as conn:
        row = _target_context(conn, target_id, workspace_id=workspace_id)
        if row["status"] not in TARGET_CLAIMABLE | {TARGET_BLOCKED, TARGET_NEEDS_REVIEW}:
            raise ValueError(f"Cannot cancel target {target_id} in status {row['status']}")
        conn.execute(
            "UPDATE publish_job_targets SET status = ?, finished_at = ?, next_attempt_at = NULL, "
            "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL "
            "WHERE id = ? AND status = ?",
            (TARGET_CANCELLED, _now_iso(), target_id, row["status"]),
        )
        _recount_job(conn, row["job_id"])
        return _row_to_target(conn.execute(
            "SELECT * FROM publish_job_targets WHERE id = ?", (target_id,),
        ).fetchone())


def resubmit_target(
    target_id: int, *, workspace_id: str | None = None, db_path: Path | None = None,
) -> Target:
    """Explicit retry of failed/cancelled work, or a resolved pre-submission block.

    Future schedules are preserved/normalized; expired schedules are cleared,
    and invalid schedules are rejected. Awaiting/review deliveries require a
    separately enqueued, deliberate new intent instead of overwriting evidence.
    """
    with _transaction(db_path) as conn:
        row = _target_context(conn, target_id, workspace_id=workspace_id)
        _check_resubmission(row, {TARGET_FAILED, TARGET_CANCELLED, TARGET_BLOCKED})
        new_schedule, _ = _normalise_due(row["schedule_at"])
        return _reset_target(conn, row, new_schedule)


def _normalise_due(schedule: str | None, now: str | None = None) -> tuple[str | None, bool]:
    """Keep a valid future schedule as naive UTC, otherwise mark it immediately due."""
    if schedule is None or schedule == "":
        return None, True
    parsed = _utc_datetime(schedule)
    due = parsed <= _utc_datetime(now or _now_iso())
    return (None if due else parsed.isoformat(), due)


def aggregate_job_state(
    job, counts: dict[str, int], *, allow_reopen: bool = False, now: str | None = None,
) -> dict:
    """Pure aggregation shared by mutations and the read-only reconciliation tool.

    Counters count actual succeeded/failed rows, never cached deltas. Attention
    states remain unfinished. Terminal intent is preserved unless an explicit
    reschedule/resubmit requests reopening; contradictory history is reported,
    not made publishable. A nonterminal job without targets needs review.
    """
    total = sum(counts.values())
    completed = counts.get(TARGET_SUCCEEDED, 0)
    failed = counts.get(TARGET_FAILED, 0)
    cancelled = counts.get(TARGET_CANCELLED, 0)
    unknown = sum(n for status, n in counts.items() if status not in TARGET_STATES)
    reasons = []
    if not total:
        reasons.append("empty_job")
    if unknown:
        reasons.append("unknown_target_state")
    if counts.get(TARGET_NEEDS_REVIEW, 0):
        reasons.append("target_needs_review")
    if not total or unknown:
        computed = JOB_NEEDS_REVIEW
    elif counts.get(TARGET_RUNNING, 0) or counts.get(TARGET_RETRYING, 0):
        computed = JOB_RUNNING
    elif counts.get(TARGET_PENDING, 0):
        computed = JOB_PENDING
    elif counts.get(TARGET_AWAITING_DELIVERY, 0):
        computed = JOB_AWAITING_DELIVERY
    elif counts.get(TARGET_NEEDS_REVIEW, 0):
        computed = JOB_NEEDS_REVIEW
    elif counts.get(TARGET_BLOCKED, 0):
        computed = JOB_BLOCKED
    elif failed:
        computed = JOB_FAILED
    elif cancelled:
        computed = JOB_CANCELLED  # some success + cancellation is NOT success
    else:
        computed = JOB_SUCCEEDED

    status = computed
    if job["status"] in JOB_TERMINAL and not allow_reopen:
        status = job["status"]
        contradictory = (computed != status if status != JOB_CANCELLED else
                         total > completed + failed + cancelled or not cancelled)
        if contradictory:
            reasons.append("terminal_contradiction")
    finished = (job["finished_at"] or now or _now_iso()) if status in JOB_TERMINAL else None
    return {"total_targets": total, "completed_targets": completed, "failed_targets": failed,
            "status": status, "finished_at": finished, "computed_status": computed,
            "needs_review": bool(reasons), "review_reasons": reasons}


def _recount_job(conn: sqlite3.Connection, job_id: int, *, allow_reopen: bool = False) -> None:
    """Recount inside the caller's transaction; never commit it independently."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    job = conn.execute("SELECT status, finished_at FROM publish_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        return
    counts = {row["status"]: row["n"] for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM publish_job_targets WHERE job_id = ? GROUP BY status",
        (job_id,),
    )}
    state = aggregate_job_state(job, counts, allow_reopen=allow_reopen)
    conn.execute(
        "UPDATE publish_jobs SET total_targets = ?, completed_targets = ?, failed_targets = ?, "
        "status = ?, finished_at = ? WHERE id = ?",
        (state["total_targets"], state["completed_targets"], state["failed_targets"],
         state["status"], state["finished_at"], job_id),
    )
    from myUtils.campaign_delivery import sync_job_delivery
    sync_job_delivery(conn, job_id, state["status"])


def recount_job(job_id: int, *, db_path: Path | None = None) -> Job:
    """Repair cached counts without reopening terminal intent or publishing work."""
    with _transaction(db_path) as conn:
        _recount_job(conn, job_id)
        row = conn.execute("SELECT * FROM publish_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise LookupError(f"Job not found: id={job_id}")
        return _row_to_job(row)


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
