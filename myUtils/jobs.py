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

import copy
import hashlib
import json
import math
import re
import secrets
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from myUtils.account_identity import canonical_account_key, canonical_account_keys
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
# Preparation is a stage of an owned target lease, not a second queue/state.
PROSPECTIVE_ADMISSION_MODE = "owned_prospective_v1"
PREREQUISITE_MAX_ATTEMPTS = 8
PREPARATION_WARM_SECONDS = 30 * 60


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
        out["retryEligibility"] = retry_eligibility(self)
        out["readiness"] = self.checkpoint.get("readiness", {})
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
def _connect(db_path: Path | None = None, *, must_exist: bool = False) -> Iterator[sqlite3.Connection]:
    resolved = _resolve_db_path(db_path)
    if must_exist:
        if not resolved.is_file():
            raise FileNotFoundError("Recovery database must be an existing regular file")
    else:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    # A long UI read and the worker's write can overlap; without a generous
    # busy timeout the loser of that race fails immediately ("database is
    # locked"), which the browser surfaces as a schedule request that never
    # loads. Waits instead of erroring. (journal_mode stays the default
    # "delete": the backup/offload scripts copy database.db as a plain file,
    # and WAL content lives in side files those copies would miss.)
    database = resolved.resolve().as_uri() + "?mode=rw" if must_exist else resolved
    conn = sqlite3.connect(database, timeout=15, uri=must_exist)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _transaction(db_path: Path | None = None, *, must_exist: bool = False) -> Iterator[sqlite3.Connection]:
    connection = _connect(db_path, must_exist=True) if must_exist else _connect(db_path)
    with connection as conn:
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
    # The browser journal is append-only evidence. A partial/empty save cannot
    # hide unknown media requests, a confirmed thread prefix, or spent budgets.
    for key in ("x_browser_operations", "x_browser_non_delivery"):
        if key not in saved:
            continue
        old, new = saved[key], merged.get(key)
        if not new:
            merged[key] = copy.deepcopy(old)
        elif key == "x_browser_operations" and isinstance(old, dict) and isinstance(new, dict):
            journal = copy.deepcopy(old)
            for field in ("version", "target_id", "account_key", "plan"):
                if old.get(field) is not None and new.get(field, old[field]) != old[field]:
                    raise ValueError("X browser journal identity/plan cannot change")
            journal.update(new)
            for field in ("items", "budgets"):
                prior, incoming = old.get(field, {}), new.get(field, {})
                if not isinstance(prior, dict) or not isinstance(incoming, dict):
                    raise ValueError("Invalid retained X browser journal")
                entries = copy.deepcopy(prior)
                for name, entry in incoming.items():
                    previous = prior.get(name)
                    if previous is not None:
                        if not isinstance(previous, dict) or not isinstance(entry, dict):
                            raise ValueError("Invalid retained X browser operation")
                        if field == "items":
                            for fact in ("part", "part_index", "effect", "operation_generation", "target_id", "account_key", "id"):
                                if fact in previous and entry.get(fact, previous[fact]) != previous[fact]:
                                    raise ValueError("X browser operation evidence cannot be replaced")
                            if previous.get("status") in {"complete", "rejected"}:
                                entry = {**entry, **previous}
                        else:
                            if (type(previous.get("attempts")) is not int or type(entry.get("attempts")) is not int
                                    or entry["attempts"] < previous["attempts"]
                                    or type(previous.get("max_attempts")) is not int
                                    or type(entry.get("max_attempts")) is not int
                                    or entry["max_attempts"] > previous["max_attempts"]):
                                raise ValueError("X browser request budgets cannot be refunded or enlarged")
                        entry = {**previous, **entry}
                    entries[name] = copy.deepcopy(entry)
                journal[field] = entries
            merged[key] = journal
        elif new != old:
            history = list(saved.get("x_browser_evidence_history") or [])
            if old not in history:
                history.append(copy.deepcopy(old))
            merged["x_browser_evidence_history"] = history
    for key in ("previous_failure", "original_pending", "preparation_history", "x_browser_evidence_history"):
        if key in saved and not merged.get(key):
            merged[key] = copy.deepcopy(saved[key])
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


def _eligible_targets(conn: sqlite3.Connection, now: str, excluded_accounts=(), *, prepare_ahead_seconds: float = 0):
    """Due work plus a bounded warming window for implementation-owned sources."""
    excluded_accounts = tuple(excluded_accounts)
    account_keys = canonical_account_keys(conn, excluded_accounts)

    def key(ref: str) -> str:
        return canonical_account_key(conn, ref, keys=account_keys)

    held = {key(ref) for ref in excluded_accounts}
    for row in conn.execute(
        "SELECT account_ref FROM publish_job_targets WHERE status = ? "
        "AND (lease_expires_at IS NULL OR julianday(lease_expires_at) IS NULL "
        "     OR julianday(lease_expires_at) > julianday(?))",
        (TARGET_RUNNING, now),
    ):
        held.add(key(row["account_ref"]))
    clause, params = _claimable_clause(now)
    if prepare_ahead_seconds:
        warm = ("status IN ('pending','retrying') "
                "AND (next_attempt_at IS NULL OR julianday(next_attempt_at)<=julianday(?)) "
                "AND julianday(schedule_at)>julianday(?) AND julianday(schedule_at)<=julianday(?) "
                "AND EXISTS (SELECT 1 FROM publish_jobs j WHERE j.id=publish_job_targets.job_id "
                "AND j.status IN ('pending','running','awaiting_delivery')) "
                "AND CASE WHEN json_valid(checkpoint_json) THEN "
                "json_extract(checkpoint_json,'$.admission.mode')=? ELSE 0 END")
        clause = f"({clause}) OR ({warm})"
        params.extend([now, now, _after_seconds(prepare_ahead_seconds, now=now), PROSPECTIVE_ADMISSION_MODE])
    cursor = conn.execute(
        f"SELECT * FROM publish_job_targets WHERE {clause} "
        "ORDER BY COALESCE(julianday(schedule_at), 0), id", params,
    )
    for row in cursor:
        from myUtils.reviewed_recovery import predecessor_confirmed
        if not predecessor_confirmed(conn, row):
            continue
        account_key = key(row["account_ref"])
        if account_key not in held:
            held.add(account_key)
            yield row


def has_claimable_targets(*, db_path: Path | None = None, prepare_ahead_seconds: float = 0) -> bool:
    """Whether due work or bounded owned preparation has an unleased account."""
    duration = min(_seconds(prepare_ahead_seconds), PREPARATION_WARM_SECONDS)
    with _connect(db_path) as conn:
        return next(_eligible_targets(conn, _now_iso(), prepare_ahead_seconds=duration), None) is not None


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

def intent_payload(payload: dict) -> dict:
    """Exclude ONLY admission's exported runtime keys, at the payload root."""
    from myUtils.publish_admission import RUNTIME_PAYLOAD_KEYS
    return {key: value for key, value in payload.items() if key not in RUNTIME_PAYLOAD_KEYS}



def _intent_hash(spec: JobSpec, workspace_id: str | None) -> str:
    canonical = {
        "platform": spec.platform,
        "profile_id": spec.profile_id,
        "workspace_id": workspace_id,
        "payload": _canonical(intent_payload(spec.payload)),
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
    """Admit outside the transaction, then durably insert the exact intent."""
    from myUtils.publish_admission import prepare_job_spec
    from myUtils.publish_orchestrator import slot_reservation_lock

    admitted, snapshot = prepare_job_spec(spec, workspace_id=workspace_id, db_path=db_path or DB_PATH)
    with slot_reservation_lock(db_path or DB_PATH), _transaction(db_path) as conn:
        return enqueue_job_in_transaction(conn, admitted, workspace_id=workspace_id,
            campaign_post_id=campaign_post_id, prep_fence=prep_fence, admission_snapshot=snapshot)


def enqueue_job_in_transaction(
    conn: sqlite3.Connection, spec: JobSpec, *, workspace_id: str | None = None,
    campaign_post_id: int | None = None, prep_fence: tuple[str, int] | None = None,
    admission_snapshot=None, calendar_now=None,
) -> Job:
    """Insert/link one member in the caller's atomic complete-group transaction.

    The caller owns its reservation and write transaction. No commits, provider
    calls, encoding or media probes occur here. Public raw callers use enqueue_job.
    """
    from myUtils.publish_admission import validate_admission_snapshot

    if not conn.in_transaction:
        raise ValueError("Publication installation requires an owned write transaction")
    if admission_snapshot is None:
        raise ValueError("Publication installation requires a validated admission snapshot")
    validate_admission_snapshot(conn, spec, admission_snapshot)
    if not spec.targets:
        raise ValueError("Job must have at least one target")
    targets = _deduplicated_targets(spec)
    intent_hash = _intent_hash(spec, workspace_id)
    key = spec.idempotency_key or "auto-" + intent_hash[:32]
    payload_json = json.dumps(spec.payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
    if campaign_post_id is not None:
        linked = conn.execute(
            "SELECT c.id,c.status,c.metadata_json,c.workspace_id "
            "FROM campaign_posts p JOIN campaigns c ON c.id=p.campaign_id WHERE p.id=?",
            (campaign_post_id,),
        ).fetchone()
        if linked is None or linked["id"] != spec.payload.get("campaignId"):
            raise ValueError("Campaign post no longer belongs to this submission")
        metadata = _recovery_json_object(linked["metadata_json"] or "{}")
        lease = metadata.get("_prepLease") or {}
        if prep_fence is not None or lease:
            observed = (str(lease.get("owner") or ""), int(metadata.get("_prepAttempts") or 0))
            if linked["status"] != "preparing" or observed != prep_fence:
                from myUtils.delivery import LeaseLost
                raise LeaseLost("Preparation ownership changed before enqueue")
        if linked["workspace_id"] != workspace_id:
            raise ValueError("Campaign post belongs to a different workspace")
    existing = conn.execute(
        "SELECT * FROM publish_jobs WHERE idempotency_key=?", (key,),
    ).fetchone()
    if existing is not None:
        if existing["workspace_id"] != workspace_id:
            raise ValueError("Idempotency key already belongs to a different publish intent")
        previous_hash = existing["intent_hash"]
        if previous_hash is None:
            old_targets = conn.execute(
                "SELECT account_ref,file_ref,schedule_at FROM publish_job_targets "
                "WHERE job_id=? ORDER BY id", (existing["id"],),
            ).fetchall()
            previous = JobSpec(
                platform=existing["platform"], payload=json.loads(existing["payload_json"]),
                profile_id=existing["profile_id"], targets=[tuple(row) for row in old_targets],
                extra_canonical=spec.extra_canonical,
            )
            previous_hash = _intent_hash(previous, existing["workspace_id"])
        if previous_hash != intent_hash:
            raise ValueError("Idempotency key already belongs to a different publish intent")
        if existing["intent_hash"] is None:
            conn.execute("UPDATE publish_jobs SET intent_hash=? WHERE id=?",
                         (intent_hash, existing["id"]))
        return _row_to_job(existing)
    if admission_snapshot.ready:
        from myUtils import destination_pacing, publish_orchestrator
        destination_pacing.assert_fixed_slots(conn, targets, now=calendar_now,
            configured_gap=max(30, publish_orchestrator.MIN_GAP_MINUTES),
            configured_cap=destination_pacing.effective_daily_cap(publish_orchestrator.MAX_POSTS_PER_ACCOUNT_PER_DAY, 3),
            operator_timezone=publish_orchestrator._operator_timezone())
    cursor = conn.execute(
        "INSERT INTO publish_jobs (idempotency_key,profile_id,platform,payload_json,status,"
        "total_targets,workspace_id,intent_hash) VALUES (?,?,?,?,?,?,?,?)",
        (key, spec.profile_id, spec.platform, payload_json, JOB_PENDING,
         len(targets), workspace_id, intent_hash),
    )
    job_id = cursor.lastrowid
    # The opaque admission was validated above. A client ready/resolved JSON
    # flag cannot install this worker-owned preparation authorization.
    from myUtils.publish_admission import ADMISSION_VERSION
    meaningful = all(str(a).startswith("account:") for a, _, _ in targets)
    initial = {}
    if meaningful:
        raw = not spec.payload.get("campaignPostId") and not any(
            str((a or {}).get("artifact_kind") or "").startswith("prepared_")
            or any(k in ((a or {}).get("metadata") or {}) for k in ("part_index", "part_count", "preparation_policy_version"))
            for a in spec.payload.get("artifacts") or [])
        initial = {"admission": {"version": ADMISSION_VERSION, "intent_hash": intent_hash,
                   "mode": PROSPECTIVE_ADMISSION_MODE if raw and not admission_snapshot.ready else "validate_only"},
                   "readiness": copy.deepcopy(spec.payload.get("publishReadiness") or {}),
                   "provider_budget_baseline": 0}
    text, _ = _merge_checkpoint("{}", initial)
    conn.executemany(
        "INSERT INTO publish_job_targets (job_id,account_ref,file_ref,schedule_at,status,workspace_id,checkpoint_json) "
        "VALUES (?,?,?,?,?,?,?)",
        [(job_id, account_ref, file_ref, schedule_at, TARGET_PENDING, workspace_id, text)
         for account_ref, file_ref, schedule_at in targets],
    )
    if campaign_post_id is not None:
        conn.execute(
            "UPDATE campaign_posts SET status='queued',last_published_job_id=?,"
            "updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id, campaign_post_id),
        )
    return _row_to_job(conn.execute("SELECT * FROM publish_jobs WHERE id=?", (job_id,)).fetchone())


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
    prepare_ahead_seconds: float = 0,
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
    warm_duration = min(_seconds(prepare_ahead_seconds), PREPARATION_WARM_SECONDS)
    if owner is not None:
        _worker_label(owner)
    claimed = []
    with _transaction(db_path) as conn:
        now = _now_iso()
        expires = _after_seconds(duration, now=now)
        rows = []
        candidates = _eligible_targets(conn, now, excluded_accounts, prepare_ahead_seconds=warm_duration)
        try:
            for row in candidates:
                rows.append(row)
                if len(rows) == limit:
                    break
        finally:
            candidates.close()
        affected_jobs = set()
        for row in rows:
            current = _decode_checkpoint(row["checkpoint_json"])
            if (current.get("admission") or {}).get("mode") == PROSPECTIVE_ADMISSION_MODE:
                text, _ = _merge_checkpoint(row["checkpoint_json"], {"readiness": {
                    **(current.get("readiness") or {}), "ready": False, "phase": "preparing",
                    "stage": "preparing", "reasonCode": "media_preparation_required"}})
                conn.execute("UPDATE publish_job_targets SET checkpoint_json=? WHERE id=?", (text, row["id"]))
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
    if lease_token is not None:
        try:
            if not row["lease_expires_at"] or _utc_datetime(row["lease_expires_at"]) <= _utc_datetime(_now_iso()):
                return None
        except ValueError:
            return None
    if lease_token is None and row["lease_owner"] is not None:
        # A legacy executor must not complete a replacement worker's claim.
        # Tokenless compatibility applies only to unowned (legacy) claims.
        return None
    return row


def _write_target_state(
    conn, row, status, *, error=None, error_category=None, next_attempt_at=None,
    checkpoint_json=None, delivery_state=None, alert_max_attempts=None,
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
    changed = cursor.rowcount == 1
    if changed and status in {TARGET_FAILED, TARGET_BLOCKED, TARGET_NEEDS_REVIEW}:
        from myUtils.publish_alert_outbox import enqueue_publish_stop
        enqueue_publish_stop(conn, row["id"], max_attempts=alert_max_attempts)
    return changed


def _transition_target(
    target_id, status, *, error=None, error_category=None, delay_seconds=None,
    checkpoint=None, delivery_state=None, lease_token=None, db_path=None, missing_error=False,
    alert_max_attempts=None,
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
        if status == TARGET_RETRYING:
            from myUtils import delivery as evidence
            retained = _decode_checkpoint(checkpoint_json or row["checkpoint_json"])
            nested = {k: v for k, v in retained.items() if k in {"previous_failure", "original_pending", "preparation_history"}}
            if evidence.has_unresolved_submission({"_delivery": retained}) or _recovery_has_submission_evidence(json.dumps(nested)):
                receipt = retained.get("provider_receipt")
                if isinstance(receipt, dict) and receipt.get("id") and receipt.get("status") != "failed":
                    status, delivery_state, delay_seconds = TARGET_AWAITING_DELIVERY, "awaiting_delivery", max(30, delay_seconds or 0)
                else:
                    status, delivery_state, delay_seconds = TARGET_NEEDS_REVIEW, "unknown", None
                    error_category = "submission_unknown"
        changed = _write_target_state(
            conn, row, status, error=error, error_category=error_category,
            next_attempt_at=_after_seconds(delay_seconds) if delay_seconds is not None else None,
            checkpoint_json=checkpoint_json, delivery_state=delivery_state,
            alert_max_attempts=alert_max_attempts,
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
    alert_max_attempts: int | None = None,
) -> bool:
    """CAS running -> failed. Late cancellation/stale-token completions are no-ops."""
    return _transition_target(target_id, TARGET_FAILED, error=error, delivery_state="failed",
                              error_category=error_category, lease_token=lease_token,
                              db_path=db_path, missing_error=True, alert_max_attempts=alert_max_attempts)


def mark_target_blocked(
    target_id: int, error: str, *, lease_token: str | None = None,
    error_category: str = "blocked", db_path: Path | None = None,
    alert_max_attempts: int | None = None,
) -> bool:
    """Release a claim into a non-claimable prerequisite/account-blocked state."""
    return _transition_target(target_id, TARGET_BLOCKED, error=error, error_category=error_category,
                              lease_token=lease_token, db_path=db_path, alert_max_attempts=alert_max_attempts)


def mark_target_needs_review(
    target_id: int, error: str, *, lease_token: str | None = None,
    error_category: str = "needs_review", db_path: Path | None = None,
    alert_max_attempts: int | None = None,
) -> bool:
    """Release an ambiguous delivery for manual review, never automatic resubmit."""
    return _transition_target(target_id, TARGET_NEEDS_REVIEW, error=error,
                              error_category=error_category, delivery_state="unknown",
                              lease_token=lease_token, db_path=db_path, alert_max_attempts=alert_max_attempts)


def mark_target_awaiting_delivery(
    target_id: int, checkpoint: dict, *, delay_seconds: float = 30,
    lease_token: str | None = None, db_path: Path | None = None,
) -> bool:
    """Persist/merge a safe receipt and schedule a later delivery poll."""
    return _transition_target(target_id, TARGET_AWAITING_DELIVERY, checkpoint=checkpoint,
                              delay_seconds=delay_seconds, delivery_state="awaiting_delivery",
                              lease_token=lease_token, db_path=db_path)

def defer_target_prerequisite(target_id: int, error: str, *, lease_token: str,
        category: str, delay_seconds: float = 120, retryable: bool = True,
        max_attempts: int = PREREQUISITE_MAX_ATTEMPTS, db_path: Path | None = None,
        stage: str = "waiting_dependency", cached_wait: bool = False) -> bool:
    """Bounded no-post failures; cached waits spend no attempt or provider budget.

    Existing counters/maxima are never reset or refunded. Claims and retained
    submission evidence use the same fences for waits as for actual failures.
    """
    from myUtils import delivery as evidence
    delay = _seconds(delay_seconds, positive=True)
    if type(max_attempts) is not int or not 1 <= max_attempts <= 100:
        raise ValueError("Prerequisite attempts must have a positive bounded limit")
    if type(cached_wait) is not bool:
        raise ValueError("Cached prerequisite wait must be a boolean")
    with _transaction(db_path, must_exist=True) as conn:
        row = _running_target(conn, target_id, lease_token)
        if row is None:
            return False
        checkpoint = _decode_checkpoint(row["checkpoint_json"])
        if evidence.has_unresolved_submission({"_delivery": checkpoint}):
            raise evidence.DeliveryUnknown("Prerequisite deferral cannot authorize retained provider effects")
        prior = checkpoint.get("prerequisites") or {}
        attempts, maximum = prior.get("attempts", 0), prior.get("max_attempts", max_attempts)
        if type(attempts) is not int or attempts < 0 or type(maximum) is not int or maximum < 1:
            raise evidence.DeliveryUnknown("Invalid prerequisite stage counter")
        maximum = min(maximum, max_attempts)
        attempts += 0 if cached_wait else 1
        stopped = not retryable or attempts >= maximum
        due = None if stopped else _after_seconds(delay)
        progress = {**prior, "attempts": attempts, "max_attempts": maximum,
                    "category": str(category)[:64], "next_attempt_at": due,
                    "updated_at": _now_iso(), "exhausted": stopped, "cached_wait": cached_wait}
        text, _ = _merge_checkpoint(row["checkpoint_json"], {"prerequisites": progress,
            "readiness": {"ready": False, "phase": "blocked_intent" if stopped else stage,
                          "stage": stage, "reasonCode": str(category)[:64], "nextAttemptAt": due}})
        changed = _write_target_state(conn, row, TARGET_BLOCKED if stopped else TARGET_RETRYING,
            error=error, error_category="prerequisite_" + str(category), next_attempt_at=due,
            checkpoint_json=text, delivery_state=row["delivery_state"])
        if changed:
            _recount_job(conn, row["job_id"])
        return changed



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
            from myUtils import delivery as delivery_checkpoint
            route_attempts, route_budget, invalid_route = checkpoint.get("provider_budget_baseline", row["attempts"]), budget, False
            try:
                route = delivery_checkpoint.active_route({"_delivery": checkpoint})
                detail = delivery_checkpoint.publish_routes({"_delivery": checkpoint}).get(route) or {}
                route_attempts = detail.get("attempts", route_attempts)
                stored_budget = detail.get("max_attempts", budget)
                if type(route_attempts) is not int or route_attempts < 0 or type(stored_budget) is not int or stored_budget < 1:
                    raise delivery_checkpoint.DeliveryUnknown("Invalid persisted provider budget")
                route_budget = min(budget, stored_budget)
            except delivery_checkpoint.DeliveryUnknown:
                invalid_route = True
            due = None
            delivery = None
            category = "stale_execution"
            text = None
            prerequisite = (not detail.get("attempts") and (
                (checkpoint.get("admission") or {}).get("mode") == PROSPECTIVE_ADMISSION_MODE
                or (checkpoint.get("readiness") or {}).get("stage") in {"preparing", "waiting_dependency", "waiting_capacity"}
                or checkpoint.get("stage") == "browser_preparing")) if not invalid_route else False
            if invalid_route:
                state, delivery, category = TARGET_NEEDS_REVIEW, "unknown", "checkpoint_invalid"
                error = "stale execution: invalid persisted delivery/provider state; review required"
            elif checkpoint.get("provider_receipt"):
                state, due, delivery = TARGET_AWAITING_DELIVERY, now, "awaiting_delivery"
                error = "stale execution: resume reconciliation of the recorded provider receipt"
            elif delivery_checkpoint.has_unresolved_submission({"_delivery": checkpoint}):
                state, delivery, category = TARGET_NEEDS_REVIEW, "unknown", "submission_unknown"
                error = "stale execution: submission may have started; manual delivery review required"
            elif prerequisite:
                prior = checkpoint.get("prerequisites") or {}
                count, maximum = prior.get("attempts", 0), prior.get("max_attempts", PREREQUISITE_MAX_ATTEMPTS)
                if type(count) is not int or count < 0 or type(maximum) is not int or maximum < 1:
                    state, delivery, category = TARGET_NEEDS_REVIEW, "unknown", "checkpoint_invalid"
                    error = "stale preparation: invalid prerequisite counter; review required"
                else:
                    stopped = count + 1 >= min(maximum, PREREQUISITE_MAX_ATTEMPTS)
                    state, category = (TARGET_BLOCKED if stopped else TARGET_RETRYING), "prerequisite_interrupted"
                    due = None if stopped else _after_seconds(120, now=now)
                    error = "stale preparation interrupted before provider create; prerequisite retry/hold retained"
                    text, _ = _merge_checkpoint(row["checkpoint_json"], {"prerequisites": {
                        **prior, "attempts": count + 1, "max_attempts": min(maximum, PREREQUISITE_MAX_ATTEMPTS),
                        "category": "interrupted", "next_attempt_at": due, "exhausted": stopped},
                        "readiness": {"ready": False, "phase": "blocked_intent" if stopped else "waiting_dependency",
                                      "stage": "preparing", "reasonCode": "interrupted", "nextAttemptAt": due}})
            elif route_attempts >= route_budget:
                state, delivery = TARGET_FAILED, "failed"
                error = "requeued as stale: worker died mid-run and retry budget exhausted"
            else:
                state, due = TARGET_RETRYING, now
                error = "requeued as stale: worker died before submission; will retry"
            if _write_target_state(conn, row, state, error=error, error_category=category,
                                   next_attempt_at=due, checkpoint_json=text, delivery_state=delivery, alert_max_attempts=budget):
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
    if _recovery_has_submission_evidence(row["checkpoint_json"], row["delivery_state"]):
        raise ValueError("Submission evidence exists; reconcile delivery or create a new deliberate intent")


def _prior_target_history(row) -> dict:
    return {key: row[key] for key in ("status", "attempts", "last_error", "error_category",
            "started_at", "finished_at", "schedule_at", "next_attempt_at", "delivery_state")} | {
            "checkpoint": _decode_checkpoint(row["checkpoint_json"])}


def _reset_target(conn, row, schedule_at, *, reviewed_prerequisite: bool = False) -> Target:
    # Ordinary recovery stays failed-only. The separate reviewed group boundary
    # rechecks genuine pre-submission prerequisite state under the same lock.
    if reviewed_prerequisite:
        from myUtils.reviewed_recovery import assert_prerequisite
        payload = conn.execute("SELECT payload_json FROM publish_jobs WHERE id=?", (row["job_id"],)).fetchone()[0]
        assert_prerequisite(row, payload)
    else:
        _check_resubmission(row, {TARGET_FAILED})
    changed = conn.execute(
        """
        UPDATE publish_job_targets
        SET schedule_at = ?, status = ?, last_error = NULL, error_category = NULL,
            started_at = NULL, finished_at = NULL, next_attempt_at = NULL,
            lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
        WHERE id = ? AND status = ? AND checkpoint_json IS ?
        """,
        (schedule_at, TARGET_PENDING, row["id"], row["status"], row["checkpoint_json"]),
    )
    if changed.rowcount != 1:
        raise ValueError("Recovery target changed; no history or counters reset")
    _recount_job(conn, row["job_id"], allow_reopen=True)
    return _row_to_target(conn.execute(
        "SELECT * FROM publish_job_targets WHERE id = ?", (row["id"],),
    ).fetchone())


def _recovery_json_object(text: str) -> dict:
    """Strict recovery-only decoding; ambiguous/corrupt evidence is never empty."""
    def invalid(*_args):
        raise ValueError("Invalid recovery metadata")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                invalid()
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid)
        if isinstance(value, dict):
            return value
    except (TypeError, ValueError, RecursionError):
        pass
    raise ValueError("Invalid recovery metadata; expected a JSON object")


def _recovery_has_submission_evidence(text: str, delivery_state: str | None = None) -> bool:
    """Fail closed on receipts, partial operations and malformed checkpoints.

    Only an operation *plan* with no items is pre-submission evidence. Never
    reinterpret a failed provider receipt as permission to create a new post.
    Explicit failed recovery uses this same strict retained-evidence gate.
    """
    if delivery_state not in {None, "", "failed"}:
        return True

    def evidence(value):
        if isinstance(value, float) and not math.isfinite(value):
            return True
        if isinstance(value, list):
            return any(evidence(item) for item in value)
        if not isinstance(value, dict):
            return False
        if "provider_receipt" in value:
            return True
        if "_delivery" in value and not isinstance(value["_delivery"], dict):
            return True
        for key in ("submission_started", "checkpoint_invalid"):
            if key in value and value[key] is not False:
                return True
        if any(value.get(key) for key in (
            "operation", "operation_started_at", "telegramCompletedByDelivery",
        )):
            return True
        operations = value.get("provider_operations", {})
        if not isinstance(operations, dict):
            return True
        for progress in operations.values():
            if (not isinstance(progress, dict) or set(progress) - {"plan", "items"}
                    or progress.get("items", {}) != {}):
                return True
            plan = progress.get("plan", [])
            if not isinstance(plan, list) or any(not isinstance(item, str) for item in plan):
                return True
        if "x_browser_operations" in value:
            journal = value["x_browser_operations"]
            if (not isinstance(journal, dict) or not isinstance(journal.get("items"), dict)
                    or journal["items"] or not isinstance(journal.get("budgets"), dict)
                    or any(not isinstance(b, dict) or b.get("attempts", 0) != 0
                           for b in journal["budgets"].values())):
                return True
        if value.get("x_browser_non_delivery") or value.get("x_browser_evidence_history"):
            return True
        return any(evidence(item) for item in value.values())

    try:
        return evidence(_recovery_json_object(text))
    except (ValueError, RecursionError):
        return True


def _assert_unstarted_pending(row, payload: str) -> None:
    """Stricter than failed recovery: not even pre-submission progress may exist."""
    if row["status"] != TARGET_PENDING:
        raise ValueError("Pending preprocessing target is no longer pending")
    if type(row["attempts"]) is not int or row["attempts"] != 0:
        raise ValueError("Pending preprocessing requires zero attempts")
    if any(row[key] is not None for key in ("started_at", "finished_at")):
        raise ValueError("Pending preprocessing target has already started or finished")
    if any(row[key] is not None for key in ("lease_owner", "lease_token", "lease_expires_at")):
        raise ValueError("Pending preprocessing target still has a lease")
    if (_recovery_json_object(row["checkpoint_json"]) != {}
            or row["delivery_state"] not in {None, ""}
            or row["next_attempt_at"] is not None
            or any(row[key] not in {None, ""} for key in ("last_error", "error_category"))):
        raise ValueError("Pending preprocessing target has checkpoint or retry history")
    history_fields = {
        "delivery", "checkpoint", "checkpointjson", "checkpointinvalid", "savecheckpoint",
        "providerreceipt", "provideroperations", "completedoperations", "publishroutes",
        "definitivenondelivery", "telegramcompletedbydelivery", "submissionstarted",
        "operation", "operationstartedat", "operationgeneration", "deliverystate",
        "receipt", "receipts", "receiptid", "previousfailure", "originalpending",
        "xbrowseroperations", "xbrowsernondelivery", "xbrowserevidencehistory",
    }

    def has_history(value):
        if isinstance(value, list):
            return any(has_history(item) for item in value)
        if not isinstance(value, dict):
            return False
        for key, item in value.items():
            name = re.sub(r"[^a-z0-9]", "", key.lower())
            if (name in history_fields or name.startswith("recovery")
                    or name.endswith("history") or has_history(item)):
                return True
        return False

    if _recovery_has_submission_evidence(payload) or has_history(_recovery_json_object(payload)):
        raise ValueError("Pending preprocessing payload has delivery or recovery history")


def _unstarted_pending_identity(row, payload: str) -> str:
    """Opaque identity of the exact original target and approved payload, not its slot alone."""
    target = dict(row)
    target.pop("job_status", None)
    blob = json.dumps({"target": target, "payload_json": payload}, sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def reschedule_failed_target(
    target_id: int, schedule_at: str, *, expected_fingerprint: str,
    revalidate: Callable[[sqlite3.Connection], str],
    workspace_id: str | None = None, db_path: Path | None = None,
    prepared_payloads: list[dict] | None = None, part_schedule_ats: list[str] | None = None,
) -> Target:
    """Guarded recovery, not an explicit resubmit or delivery-reconciliation API.

    The mandatory read-only ``revalidate`` callback must recheck account, intent,
    policy and duplicate guards and return the freshly computed plan fingerprint.
    It runs under the SAME BEGIN IMMEDIATE as the failed -> pending reset/recount.
    Missing DBs, stale plans, changed states and submission evidence are refused;
    no receipt override exists. Other resubmit/reschedule semantics are unchanged.
    """
    if type(target_id) is not int or not 0 < target_id <= 2**63 - 1:
        raise ValueError("target_id must be a positive SQLite integer")
    if (not isinstance(expected_fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint)
            or not callable(revalidate)):
        raise ValueError("A recovery fingerprint and read-only validator are required")
    candidate = _utc_datetime(schedule_at)
    with _transaction(db_path, must_exist=True) as conn:
        row = _target_context(conn, target_id, workspace_id=workspace_id)
        if row["status"] != TARGET_FAILED:
            raise ValueError("Recovery target is no longer failed")
        if row["job_status"] not in {
            JOB_FAILED, JOB_PENDING, JOB_RUNNING, JOB_BLOCKED, JOB_AWAITING_DELIVERY, JOB_NEEDS_REVIEW,
        }:
            raise ValueError("Recovery parent intent is terminal or invalid")
        if any(row[key] is not None for key in ("lease_owner", "lease_token", "lease_expires_at")):
            raise ValueError("Recovery target still has a lease")
        payload = conn.execute("SELECT payload_json FROM publish_jobs WHERE id=?", (row["job_id"],)).fetchone()[0]
        if (_recovery_has_submission_evidence(row["checkpoint_json"], row["delivery_state"])
                or _recovery_has_submission_evidence(payload)):
            raise ValueError("Submission evidence exists; reconcile delivery separately")
        if candidate <= _utc_datetime(_now_iso()):
            raise ValueError("scheduleAt must be in the future")
        before = conn.total_changes
        observed = revalidate(conn)
        if conn.total_changes != before:
            raise ValueError("Recovery validator must be read-only")
        if not isinstance(observed, str) or not secrets.compare_digest(observed, expected_fingerprint):
            raise ValueError("Stale recovery plan; regenerate it")
        if prepared_payloads is not None:
            from myUtils.prepared_recovery import validate_parts
            validate_parts(conn, row, prepared_payloads, part_schedule_ats)
            if _utc_datetime(part_schedule_ats[0]) != candidate:
                raise ValueError("First prepared slot does not match the guarded recovery slot")
            if any(_utc_datetime(slot) <= _utc_datetime(_now_iso()) for slot in part_schedule_ats):
                raise ValueError("Every prepared part must be scheduled in the future")
        elif part_schedule_ats is not None:
            raise ValueError("Part schedules require prepared payloads")
        _reset_target(conn, row, candidate.isoformat())
        # A deliberate, validated recovery is a new provider submission intent.
        # Automatic retries retain this value, so their idempotency key is stable.
        if prepared_payloads is None:
            text, _ = _merge_checkpoint("{}", {"recovery_generation": expected_fingerprint,
                "previous_failure": _prior_target_history(row)})
            conn.execute("UPDATE publish_job_targets SET checkpoint_json=? WHERE id=?", (text, target_id))
        else:
            from myUtils.prepared_recovery import install_parts
            install_parts(conn, row, prepared_payloads, part_schedule_ats, expected_fingerprint)
        return _row_to_target(conn.execute(
            "SELECT * FROM publish_job_targets WHERE id=?", (target_id,),
        ).fetchone())


def recover_reviewed_group(entries: list[dict], *, reviewed_target_ids: Sequence[int],
        expected_fingerprint: str, revalidate: Callable[[sqlite3.Connection], str],
        workspace_id: str | None = None, db_path: Path | None = None) -> list[Target]:
    """Atomic COMPLETE reviewed group; a manifest flag alone is not authority.

    Frozen caller IDs, fresh read-only SQL/account/intent/duplicate/order/byte/
    budget/pacing validation and all future part slots are mandatory. Failed-only
    APIs are unchanged. No cancelled/review/unknown state or receipt override.
    """
    from myUtils.prepared_recovery import install_parts, validate_parts, _checkpoint
    from myUtils.reviewed_recovery import assert_budget, assert_prerequisite
    if (not isinstance(reviewed_target_ids, (list, tuple)) or not reviewed_target_ids
            or any(type(value) is not int or not 0 < value <= 2**63 - 1 for value in reviewed_target_ids)
            or len(set(reviewed_target_ids)) != len(reviewed_target_ids)):
        raise ValueError("Reviewed recovery requires explicit nonempty frozen target IDs")
    if (not isinstance(entries, list) or not 1 <= len(entries) <= 1000
            or any(not isinstance(item, dict) or type(item.get("target_id")) is not int
                   or item["target_id"] not in reviewed_target_ids for item in entries)
            or len({item["target_id"] for item in entries}) != len(entries)
            or not isinstance(expected_fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint) or not callable(revalidate)):
        raise ValueError("Reviewed recovery needs a complete frozen group and fingerprint validator")
    rows, slots = [], []
    with _transaction(db_path, must_exist=True) as conn:
        for item in entries:
            row = _target_context(conn, item["target_id"], workspace_id=workspace_id)
            payload = conn.execute("SELECT payload_json FROM publish_jobs WHERE id=?", (row["job_id"],)).fetchone()[0]
            if row["status"] == TARGET_BLOCKED:
                assert_prerequisite(row, payload)
            else:
                _check_resubmission(row, {TARGET_FAILED})
            assert_budget(_recovery_json_object(row["checkpoint_json"]))
            if (row["job_status"] not in {JOB_FAILED, JOB_PENDING, JOB_RUNNING, JOB_BLOCKED,
                    JOB_AWAITING_DELIVERY, JOB_NEEDS_REVIEW}
                    or any(row[key] is not None for key in ("lease_owner", "lease_token", "lease_expires_at"))
                    or _recovery_has_submission_evidence(payload)):
                raise ValueError("Reviewed group has changed intent, lease or submission evidence")
            validate_parts(conn, row, item["prepared_payloads"], item["part_schedule_ats"],
                           reviewed_selected_content=True)
            moments = [_utc_datetime(slot) for slot in item["part_schedule_ats"]]
            if not moments or any(moment <= _utc_datetime(_now_iso()) for moment in moments):
                raise ValueError("Every reviewed part must have a future recovery slot")
            slots.extend(moments)
            rows.append(row)
        if any(left >= right for left, right in zip(slots, slots[1:])):
            raise ValueError("Reviewed group would reverse approved content order")
        before = conn.total_changes
        observed = revalidate(conn)
        if conn.total_changes != before:
            raise ValueError("Reviewed recovery validator must be read-only")
        if not isinstance(observed, str) or not secrets.compare_digest(observed, expected_fingerprint):
            raise ValueError("Stale reviewed recovery plan; regenerate")
        installed = []
        for row, item in zip(rows, entries):
            _reset_target(conn, row, _utc_datetime(item["part_schedule_ats"][0]).isoformat(),
                          reviewed_prerequisite=row["status"] == TARGET_BLOCKED)
            installed.extend(install_parts(conn, row, item["prepared_payloads"],
                                           item["part_schedule_ats"], item["expected_fingerprint"]))
        # Cross-original and newly split parts use one durable confirmed-delivery
        # chain. Queued/submitted/uncertain predecessors cannot release a tail.
        for index, target_id in enumerate(installed):
            checkpoint = _recovery_json_object(conn.execute(
                "SELECT checkpoint_json FROM publish_job_targets WHERE id=?", (target_id,)).fetchone()[0])
            checkpoint["reviewed_recovery"] = {"selected_target_ids": sorted(reviewed_target_ids),
                "group_fingerprint": expected_fingerprint, "ordered_target_ids": installed}
            if index:
                checkpoint["recovery_predecessor_target_id"] = installed[index - 1]
            conn.execute("UPDATE publish_job_targets SET checkpoint_json=? WHERE id=?",
                         (_checkpoint(sys.modules[__name__], checkpoint), target_id))
        return [_row_to_target(conn.execute("SELECT * FROM publish_job_targets WHERE id=?", (value,)).fetchone())
                for value in installed]


def repair_unstarted_pending_target(
    target_id: int, schedule_at: str, *, include_unstarted_pending: bool,
    original_state_identity: str, expected_fingerprint: str,
    revalidate: Callable[[sqlite3.Connection], str], prepared_payloads: list[dict],
    part_schedule_ats: list[str], workspace_id: str | None = None, db_path: Path | None = None,
) -> Target:
    """Install one COMPLETE immutable generation; never reset started pending work.

    Only the preprocessing opt-in may call this API. The callback rechecks the
    frozen selection, full account/intent fingerprint, duplicates and pacing under
    this same write transaction. The failed-only recovery API is not broadened.
    """
    if include_unstarted_pending is not True:
        raise ValueError("Pending preprocessing requires explicit opt-in")
    if type(target_id) is not int or not 0 < target_id <= 2**63 - 1:
        raise ValueError("target_id must be a positive SQLite integer")
    if (not callable(revalidate) or any(not isinstance(value, str)
            or not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in (expected_fingerprint, original_state_identity))):
        raise ValueError("Pending preprocessing requires original identity and a read-only fingerprint validator")
    candidate = _utc_datetime(schedule_at)
    from myUtils.prepared_recovery import install_parts, validate_parts

    with _transaction(db_path, must_exist=True) as conn:
        row = _target_context(conn, target_id, workspace_id=workspace_id)
        if row["job_status"] not in {
            JOB_PENDING, JOB_RUNNING, JOB_BLOCKED, JOB_AWAITING_DELIVERY, JOB_NEEDS_REVIEW,
        }:
            raise ValueError("Pending preprocessing parent intent is terminal or invalid")
        payload = conn.execute("SELECT payload_json FROM publish_jobs WHERE id=?", (row["job_id"],)).fetchone()[0]
        _assert_unstarted_pending(row, payload)
        if not secrets.compare_digest(_unstarted_pending_identity(row, payload), original_state_identity):
            raise ValueError("Original pending state changed; regenerate the frozen plan")
        if candidate <= _utc_datetime(_now_iso()):
            raise ValueError("Every prepared part must be scheduled in the future")
        before = conn.total_changes
        observed = revalidate(conn)
        if conn.total_changes != before:
            raise ValueError("Pending preprocessing validator must be read-only")
        if not isinstance(observed, str) or not secrets.compare_digest(observed, expected_fingerprint):
            raise ValueError("Stale pending preprocessing plan; regenerate it")
        validate_parts(conn, row, prepared_payloads, part_schedule_ats)
        if _utc_datetime(part_schedule_ats[0]) != candidate:
            raise ValueError("First prepared slot does not match the guarded pending slot")
        if any(_utc_datetime(slot) <= _utc_datetime(_now_iso()) for slot in part_schedule_ats):
            raise ValueError("Every prepared part must be scheduled in the future")
        # Retain even the stored timestamp representation when its slot is compatible.
        schedule = row["schedule_at"] if (row["schedule_at"]
            and _utc_datetime(row["schedule_at"]) == candidate) else candidate.isoformat()
        changed = conn.execute(
            "UPDATE publish_job_targets SET schedule_at=? WHERE id=? AND status='pending' "
            "AND attempts=0 AND started_at IS NULL AND finished_at IS NULL "
            "AND lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL "
            "AND checkpoint_json IS ? AND schedule_at IS ? AND next_attempt_at IS NULL",
            (schedule, target_id, row["checkpoint_json"], row["schedule_at"]))
        if changed.rowcount != 1:
            raise ValueError("Original pending reservation changed; regenerate the frozen plan")
        install_parts(conn, row, prepared_payloads, part_schedule_ats, expected_fingerprint)
        checkpoint = _recovery_json_object(conn.execute(
            "SELECT checkpoint_json FROM publish_job_targets WHERE id=?", (target_id,)).fetchone()[0])
        checkpoint["original_pending"] = {**checkpoint.pop("previous_failure"),
            "status": TARGET_PENDING, "state_identity": original_state_identity}
        checkpoint["include_unstarted_pending"] = True
        text = json.dumps(checkpoint, ensure_ascii=False, allow_nan=False)
        if len(text.encode("utf-8")) > CHECKPOINT_MAX_BYTES:
            raise ValueError("Prepared pending checkpoint exceeds its byte budget")
        conn.execute("UPDATE publish_job_targets SET checkpoint_json=? WHERE id=?", (text, target_id))
        _recount_job(conn, row["job_id"])
        return _row_to_target(conn.execute(
            "SELECT * FROM publish_job_targets WHERE id=?", (target_id,)).fetchone())


def reschedule_target(
    target_id: int, schedule_at: str, *, workspace_id: str | None = None,
    db_path: Path | None = None,
) -> Target:
    """Change a future slot, never recover or reset a publication intent.

    Checkpoint payloads, generations, attempts, receipts and lifecycle state
    remain intact. Failed/cancelled work still needs evidence-backed recovery.
    """
    from myUtils.publish_orchestrator import slot_reservation_lock

    candidate = _utc_datetime(schedule_at)
    if candidate.microsecond:
        candidate = candidate.replace(microsecond=0) + timedelta(seconds=1)
    with slot_reservation_lock(db_path or DB_PATH), _transaction(db_path) as conn:
        row = _target_context(conn, target_id, workspace_id=workspace_id)
        now = _utc_datetime(_now_iso())
        if candidate <= now:
            raise ValueError("scheduleAt must be in the future")
        _check_resubmission(row, {TARGET_PENDING, TARGET_RETRYING, TARGET_FAILED,
                                  TARGET_CANCELLED, TARGET_BLOCKED})
        _validate_calendar_slot(conn, row, candidate, now)
        conn.execute("UPDATE publish_job_targets SET schedule_at=? WHERE id=? AND status=?",
                     (candidate.isoformat(), target_id, row["status"]))
        return _row_to_target(conn.execute(
            "SELECT * FROM publish_job_targets WHERE id=?", (target_id,)).fetchone())


def _calendar_payload(conn, row) -> dict:
    job = conn.execute("SELECT payload_json FROM publish_jobs WHERE id=?", (row["job_id"],)).fetchone()
    payload = _recovery_json_object(job["payload_json"])
    override = _recovery_json_object(row["checkpoint_json"]).get("payload", {})
    if not isinstance(override, dict):
        raise ValueError("Invalid calendar payload; reconcile the target first")
    return {**payload, **override}


def _calendar_part(payload: dict) -> tuple[str | None, int, int]:
    group = payload.get("publicationGroup") or {}
    part = payload.get("preparedPart") or {}
    if not isinstance(group, dict) or not isinstance(part, dict):
        raise ValueError("Invalid ordered publication metadata")
    index = group.get("index", part.get("index", payload.get("partIndex", 1)))
    count = group.get("count", part.get("count", payload.get("partCount", 1)))
    if type(index) is not int or type(count) is not int or not 1 <= index <= count:
        raise ValueError("Invalid ordered publication metadata")
    identifier = group.get("id")
    if identifier is not None and (not isinstance(identifier, str) or not identifier):
        raise ValueError("Invalid publication group identity")
    return identifier, index, count


def _validate_calendar_slot(conn, row, candidate: datetime, now: datetime) -> None:
    """Reuse the canonical booking snapshot without releasing durable evidence."""
    from myUtils import destination_pacing, publish_orchestrator

    aliases = destination_pacing.account_refs(conn)
    identity = destination_pacing.identity_for_ref(aliases, row["account_ref"])
    booked = destination_pacing.booked_slots(conn, aliases, now=now)
    taken = list(booked.get(identity, []))
    if row["status"] in {TARGET_PENDING, TARGET_RETRYING}:
        previous = max(_utc_datetime(row["schedule_at"] or row["started_at"] or now), now)
        try:
            taken.remove(previous.isoformat())  # Remove this one unfenced reservation only.
        except ValueError:
            raise ValueError("Current target reservation changed; reload the calendar") from None
    allocated = publish_orchestrator._next_free_slot(
        0, candidate, 0, {0: taken}, min_gap_minutes=30, max_per_day=3,
        now_fn=lambda: now)
    if allocated != candidate:
        raise ValueError("Requested slot conflicts with destination capacity or the 30-minute gap")

    own_payload = _calendar_payload(conn, row)
    group_id, index, count = _calendar_part(own_payload)
    if count == 1:
        return
    keys = {ref: key for key, refs in aliases.items() for ref in refs}
    for other in conn.execute(
        "SELECT t.*,j.platform,j.payload_json FROM publish_job_targets t "
        "JOIN publish_jobs j ON j.id=t.job_id WHERE t.id != ?", (row["id"],)):
        if keys.get(other["account_ref"]) != identity:
            continue
        other_payload = _calendar_payload(conn, other)
        other_group, other_index, other_count = _calendar_part(other_payload)
        same_group = bool(group_id and group_id == other_group)
        if not group_id:
            same_group = other["job_id"] == row["job_id"] or (
                own_payload.get("campaignId") is not None
                and own_payload.get("campaignId") == other_payload.get("campaignId")
                and own_payload.get("platform") == other_payload.get("platform")
                and other_count == count)
        if not same_group:
            continue
        if other_count != count or other_index == index:
            raise ValueError("Publication group is inconsistent; regenerate a guarded recovery plan")
        if not other["schedule_at"]:
            raise ValueError("An ordered sibling has no valid slot; edit the complete group")
        other_slot = _utc_datetime(other["schedule_at"])
        if ((other_index < index and other_slot >= candidate)
                or (other_index > index and other_slot <= candidate)):
            raise ValueError("Calendar edit would reverse the approved publication part order")


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
    recovery_plan: dict | None = None,
) -> Target:
    """Apply one validated preparation/recovery plan, never blindly reset a row.

    Receipt-bearing cancellation is not proof of non-delivery. Cancelled or
    unresolved blocked intents need a separately deliberate, validated intent.
    """
    with _transaction(db_path) as conn:
        row = _target_context(conn, target_id, workspace_id=workspace_id)
        _check_resubmission(row, {TARGET_FAILED, TARGET_CANCELLED, TARGET_BLOCKED})
        original = _row_to_target(row)
    if recovery_plan is None:
        raise ValueError("A validated prepared recovery plan is required; attempts and evidence were not reset")
    if (not isinstance(recovery_plan, dict) or type(recovery_plan.get("target_id")) is not int
            or recovery_plan["target_id"] != target_id
            or recovery_plan.get("original_status", "failed") != TARGET_FAILED
            or recovery_plan.get("include_unstarted_pending") is True):
        raise ValueError("Recovery plan must select exactly this failed target")
    if original.status != TARGET_FAILED:
        raise ValueError("This intent requires a separately validated recovery or deliberate new intent")
    from myUtils.publish_admission import validate_retry_plan
    from scripts.preprocess_failed_targets import apply_plan

    job = get_job(original.job_id, workspace_id=workspace_id, db_path=db_path)
    validate_retry_plan(job, original, recovery_plan, db_path=db_path or DB_PATH,
                        workspace_id=workspace_id)
    result = apply_plan([recovery_plan], db_path=db_path or DB_PATH, target_ids=[target_id])
    if result["applied"] != 1 or result["errors"]:
        raise ValueError("Recovery state, prerequisites, media or slots changed; retain the plan and regenerate")
    with _connect(db_path) as conn:
        return _row_to_target(conn.execute(
            "SELECT * FROM publish_job_targets WHERE id=?", (target_id,)).fetchone())


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
    preserve_terminal = job["status"] in JOB_TERMINAL and not allow_reopen
    if preserve_terminal:
        status = job["status"]
        contradictory = (computed != status if status != JOB_CANCELLED else
                         total > completed + failed + cancelled or not cancelled)
        if contradictory:
            reasons.append("terminal_contradiction")
        if not job["finished_at"]:
            reasons.append("missing_terminal_timestamp")
    # Recounting historical terminal rows is not a new delivery event. Keep an
    # unknown timestamp unknown rather than fabricating today's completion time.
    finished = None
    if status in JOB_TERMINAL:
        finished = job["finished_at"] if preserve_terminal else (job["finished_at"] or now or _now_iso())
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


def retry_eligibility(target: Target) -> dict:
    """Public action projection, not an authority supplied by a client payload."""
    result = {"allowed": False, "evidenceRequired": True}
    if target.status not in {TARGET_FAILED, TARGET_CANCELLED, TARGET_BLOCKED}:
        return result | {"reasonCode": "target_not_retryable", "reason": "This target is not eligible for recovery"}
    try:
        checkpoint = json.dumps(target.checkpoint, allow_nan=False)
        protected = _recovery_has_submission_evidence(checkpoint, target.delivery_state)
    except (TypeError, ValueError, RecursionError):
        protected = True
    if protected:
        return result | {"reasonCode": "delivery_evidence_retained",
                         "reason": "Reconcile retained delivery evidence; retry cannot erase it"}
    return result | {"reasonCode": "recovery_plan_required",
                     "reason": "Prepare and validate the resolved cause, exact media and slots before retrying",
                     "acceptsPreparedPlan": target.status == TARGET_FAILED}


# Owned prospective preparation is deliberately separate from historical failed
# recovery / frozen unstarted-pending repair. JSON flags never authorize it.
_PREPARED_MEDIA_FIELDS = {"artifacts", "preparedPart", "preparedPost", "partIndex", "partCount",
                          "preparedImageBatch", "_preserveApprovedText"}
_OWNED_PREPARATION_SEAL_KEY = secrets.token_bytes(32)


@dataclass(frozen=True)
class OwnedPreparationSnapshot:
    target_id: int
    lease_token: str
    identity: str
    source_spec: JobSpec
    source_admission: Any
    parts_json: str
    files: tuple
    generation: str
    seal: str


def _prepared_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _owned_preparation_identity(row, job) -> str:
    target = {k: row[k] for k in row.keys() if k not in {"lease_expires_at", "job_status"}}
    original = {k: job[k] for k in ("id", "idempotency_key", "profile_id", "workspace_id", "platform", "payload_json", "intent_hash")}
    return _prepared_digest([target, original])


def _owned_preparation_rows(conn, target_id, lease_token):
    from myUtils import delivery, publish_admission
    row = _running_target(conn, target_id, lease_token)
    if row is None or not lease_token or not row["lease_owner"]:
        raise delivery.LeaseLost("Preparation requires the original live owned target lease")
    job = conn.execute("SELECT * FROM publish_jobs WHERE id=?", (row["job_id"],)).fetchone()
    if job is None or job["status"] not in {JOB_PENDING, JOB_RUNNING, JOB_AWAITING_DELIVERY}:
        raise delivery.LeaseLost("Preparation parent was cancelled or stopped")
    checkpoint = _recovery_json_object(row["checkpoint_json"])
    admission = checkpoint.get("admission") or {}
    if (admission.get("version") != publish_admission.ADMISSION_VERSION
            or admission.get("mode") != PROSPECTIVE_ADMISSION_MODE
            or not job["intent_hash"] or admission.get("intent_hash") != job["intent_hash"]
            or checkpoint.get("preparation_generation") or checkpoint.get("recovery_generation")):
        raise ValueError("Only newly admitted prospective sources may install an owned generation")
    if (_recovery_has_submission_evidence(row["checkpoint_json"], row["delivery_state"])
            or _recovery_has_submission_evidence(job["payload_json"])):
        raise delivery.DeliveryUnknown("Retained provider effects forbid generation replacement")
    return row, job


def _same_approved_fields(original, prepared):
    left, right = intent_payload(original), intent_payload(prepared)
    if any(left.get(k) != right.get(k) for k in (set(left) | set(right)) - _PREPARED_MEDIA_FIELDS):
        raise ValueError("Preparation changed approved copy, options or original intent")


def prepare_owned_publication(target_id: int, *, lease_token: str, source_payload: dict,
        prepared_payloads: list[dict], db_path: Path, backup_verifications=None) -> OwnedPreparationSnapshot:
    """Seal complete, executed preparation evidence OUTSIDE the SQL install.

    This is not a public JSON admission or a legacy repair API. The live SQL
    admission mode, original immutable intent, exact registered sources, current
    grants/caps, every ordered part and all actual file facts are checked here.
    Installation rereads SQL and cheap stats only, never encodes/hashes/probes.
    """
    import hmac
    from myUtils import media_integrity, media_compliance, publish_admission as admission
    from myUtils import publish_media_guard as guard, prepared_recovery, profiles
    from myUtils.worker import _resolve_media_path, _file_record_local_path
    if not isinstance(source_payload, dict) or not isinstance(prepared_payloads, list) or not 1 <= len(prepared_payloads) <= media_compliance.MAX_PARTS:
        raise ValueError("Owned preparation requires a bounded complete payload set")
    with _connect(db_path, must_exist=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        row, job = _owned_preparation_rows(conn, target_id, lease_token)
        original = _recovery_json_object(job["payload_json"])
        original.update(_recovery_json_object(row["checkpoint_json"]).get("payload") or {})
        _same_approved_fields(original, source_payload)
        identity = _owned_preparation_identity(row, job)
        source_spec = JobSpec(platform=job["platform"], profile_id=job["profile_id"], payload=copy.deepcopy(source_payload),
                              targets=[(row["account_ref"], row["file_ref"], row["schedule_at"])])
        original_media = original.get("artifacts") or []
        sources = source_payload.get("artifacts") or []
        if original_media:
            if len(original_media) != len(sources):
                raise ValueError("Preparation dropped an approved source")
            for before, after in zip(original_media, sources):
                if (_resolve_media_path(before.get("local_path") or "").resolve() != _resolve_media_path(after.get("local_path") or "").resolve()
                        or any(before.get(k) != after.get(k) for k in ("artifact_kind", "source_file_record_id", "content_variant", "unwatermarked"))
                        or any((before.get("metadata") or {}).get(k) != (after.get("metadata") or {}).get(k)
                               for k in (set(before.get("metadata") or {}) | set(after.get("metadata") or {})) - {"sha256", "size_bytes"})):
                    raise ValueError("Preparation substituted an approved source variant")
        elif sources:
            refs = original.get("threadFileRefs") or [row["file_ref"]]
            if [_file_record_local_path(ref) for ref in refs] != [_resolve_media_path(a.get("local_path") or "").resolve() for a in sources]:
                raise ValueError("Raw preparation changed the exact ordered file references")
        account_id = int(row["account_ref"].split(":", 1)[1])
        account = profiles.get_account(account_id, db_path=db_path)
        config = account.config or {}
        backup = str(config.get("publishingProvider") or "direct") == "sociamonials"
        workspace_id = job["workspace_id"]
    source_spec, source_admission = admission.prepare_job_spec(source_spec, workspace_id=workspace_id, db_path=db_path,
        backup_verifications=backup_verifications)
    facts = json.loads(source_admission.facts_json)
    if not facts["accounts"] or any(not af["ready"] for af in facts["accounts"]):
        from myUtils.media_restore import PrerequisiteUnavailable
        raise PrerequisiteUnavailable("configuration")
    backup = all(af.get("provider") == "sociamonials" for af in facts["accounts"])
    # Every source must be an exact existing registration, even if its legacy
    # digest is unknown. Actual verified facts are installed with the generation.
    registered_sources = {(fact[0], fact[1]) for fact in facts["registry"] if fact[0] in {"file_records", "campaign_artifacts"}}
    for artifact in source_spec.payload.get("artifacts") or []:
        if guard.is_context_artifact(artifact):
            continue
        identifier = artifact.get("id", artifact.get("artifact_id"))
        file_id = artifact.get("file_record_id", artifact.get("source_file_record_id"))
        if ("campaign_artifacts", identifier) not in registered_sources and ("file_records", file_id) not in registered_sources:
            raise ValueError("Every approved source needs its exact registered identity")
    parts, files = copy.deepcopy(prepared_payloads), {}
    for index, part in enumerate(parts, 1):
        if not isinstance(part, dict) or _recovery_has_submission_evidence(json.dumps(part, allow_nan=False)):
            raise ValueError("Preparation payload carries invalid or provider evidence")
        _same_approved_fields(source_spec.payload, part)
        part["_preserveApprovedText"] = True
        admission.validate_intent(part, platform=job["platform"], effective_provider="sociamonials" if backup else "direct", account_config=config)
        guard.validate_payload(part, platform=job["platform"], account_config=config, backup_compatible=backup)
        if len(parts) > 1:
            label = part.get("preparedPost") or part.get("preparedPart") or {}
            if label.get("index") != index or label.get("count") != len(parts):
                raise ValueError("Prepared ordered payload set is incomplete or reordered")
        for artifact in part.get("artifacts") or []:
            if guard.is_context_artifact(artifact):
                raise ValueError("Context artifacts cannot become publishing media")
            path = Path(str(artifact.get("local_path") or "")).resolve()
            stamp = admission._stat(path)
            executed = media_integrity.verify_artifact(path, artifact, required=True)
            if stamp != admission._stat(path):
                raise ValueError("Prepared bytes changed during final verification")
            files[str(path)] = (str(path), stamp, executed["sha256"], executed["size_bytes"])
    videos = [a for part in parts for a in part.get("artifacts") or [] if guard.media_kind(a) == "video"]
    if videos:
        prepared_recovery.validate_video_provenance(source_spec.payload, videos, platform=job["platform"], probe_sources=True)
    images = [a for part in parts for a in part.get("artifacts") or [] if guard.media_kind(a) == "image"]
    image_sources = [a for a in source_spec.payload.get("artifacts") or [] if guard.media_kind(a) == "image"]
    if len(images) != len(image_sources):
        raise ValueError("Preparation dropped or duplicated an approved image")
    for source, output in zip(image_sources, images):
        path = Path(source["local_path"]).resolve()
        metadata = output.get("metadata") or {}
        if Path(output["local_path"]).resolve() != path and (
                Path(metadata.get("input_path") or "").resolve() != path
                or metadata.get("input_sha256") != media_integrity.verify_artifact(path, source)["sha256"]):
            raise ValueError("Preparation changed ordered image source identity")
        if any(source.get(k) != output.get(k) for k in ("source_file_record_id", "content_variant", "unwatermarked", "alt_text", "altText")):
            raise ValueError("Preparation changed approved image variant/accessibility")
    for path, stamp, digest, size in source_admission.files:
        files[path] = (path, stamp, digest, size)
    parts_json = json.dumps(parts, sort_keys=True, ensure_ascii=False, allow_nan=False)
    generation = _prepared_digest([job["intent_hash"], target_id, parts])
    file_tuple = tuple(sorted(files.values()))
    signed = [target_id, lease_token, identity, admission._spec_intent(source_spec), source_admission.seal, parts_json, file_tuple, generation]
    seal = hmac.new(_OWNED_PREPARATION_SEAL_KEY, json.dumps(signed, sort_keys=True, default=list).encode(), hashlib.sha256).hexdigest()
    return OwnedPreparationSnapshot(target_id, lease_token, identity, source_spec, source_admission, parts_json, file_tuple, generation, seal)


def _owned_part_admission(conn, spec, workspace_id, files, source_admission):
    """Derive admission from trusted SQL bindings + already executed file facts.

    Only install_owned_publication calls this AFTER authenticating its complete
    outside-TX snapshot. It cannot turn a source/client ready flag into proof.
    Uses admission's current seal/stat contract without any media/provider work.
    """
    from myUtils import publish_admission as admission
    facts = {"accounts": [], "registry": [], "ready": True, "placeholder": False}
    backup_verifications = admission._backup_verifications(source_admission)
    if backup_verifications:
        facts["backup_verifications"] = {ref: dict(vars(value)) for ref, value in backup_verifications.items()}
    for ref in dict.fromkeys(a for a, _, _ in spec.targets):
        _, _, af = admission._account_facts(conn, ref, spec, workspace_id, backup_verifications.get(ref))
        if not af["ready"]:
            raise ValueError("Current destination authorization/capabilities are not ready")
        facts["accounts"].append(af)
    facts["registry"], complete = admission._registry_facts(conn, {**spec.payload, "platform": spec.platform}, workspace_id)
    if not complete:
        raise ValueError("Complete prepared generation is not exactly registered")
    by_path = {path: entry for entry in files for path in [entry[0]]}
    paths = {str(Path(a["local_path"]).resolve()) for a in spec.payload.get("artifacts") or []}
    if not paths <= by_path.keys():
        raise ValueError("Prepared artifact lacks outside-transaction byte verification")
    selected = tuple(by_path[path] for path in sorted(paths))
    spec.payload["_publishAdmissionVersion"] = admission.ADMISSION_VERSION
    spec.payload["publishReadiness"] = {"ready": True, "phase": "ready", "reasonCode": "ready"}
    digest, facts_json = admission._digest(admission._spec_intent(spec)), admission._json(facts)
    snapshot = admission.AdmissionSnapshot(digest, workspace_id, facts_json, selected,
                                          admission._seal(digest, workspace_id, facts_json, selected))
    admission.validate_admission_snapshot(conn, spec, snapshot)
    return snapshot


def install_owned_publication(snapshot: OwnedPreparationSnapshot, *, db_path: Path) -> list[Target]:
    """ONE reservation/CAS transaction: complete registry/posts/targets/slots or none.

    Original job payload/hash and every old counter/checkpoint remain immutable.
    The target is released PENDING; this is preparation, NEVER publish success.
    Already-ready and historical/recovery generations cannot be replaced here.
    """
    import hmac
    from myUtils import delivery, publish_admission as admission, destination_pacing, publish_orchestrator as orchestrator
    if not isinstance(snapshot, OwnedPreparationSnapshot):
        raise ValueError("Owned installation requires an authentic preparation snapshot")
    signed = [snapshot.target_id, snapshot.lease_token, snapshot.identity, admission._spec_intent(snapshot.source_spec),
              snapshot.source_admission.seal, snapshot.parts_json, snapshot.files, snapshot.generation]
    expected = hmac.new(_OWNED_PREPARATION_SEAL_KEY, json.dumps(signed, sort_keys=True, default=list).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(snapshot.seal, expected):
        raise ValueError("Owned preparation snapshot was changed or forged")
    parts = json.loads(snapshot.parts_json)
    with orchestrator.slot_reservation_lock(db_path), _transaction(db_path, must_exist=True) as conn:
        row, job = _owned_preparation_rows(conn, snapshot.target_id, snapshot.lease_token)
        if _owned_preparation_identity(row, job) != snapshot.identity:
            raise delivery.LeaseLost("Original intent, generation or lease changed during preparation")
        admission.validate_admission_snapshot(conn, snapshot.source_spec, snapshot.source_admission)
        for path, stamp, _digest, _size in snapshot.files:
            if admission._stat(path) != tuple(stamp):
                raise ValueError("Verified generation bytes changed before installation")
        source = snapshot.source_spec.payload
        aliases = destination_pacing.account_refs(conn)
        account_key = destination_pacing.identity_for_ref(aliases, row["account_ref"])
        wanted = {str(Path(a["local_path"]).resolve()) for a in source.get("artifacts") or []}
        wanted_hashes = {entry[2] for entry in snapshot.source_admission.files}
        approved = {k: v for k, v in intent_payload(source).items() if k not in _PREPARED_MEDIA_FIELDS | {"campaignId", "campaignPostId", "platform"}}
        for other in conn.execute("SELECT t.*,j.platform,j.payload_json FROM publish_job_targets t JOIN publish_jobs j ON j.id=t.job_id WHERE t.id!=?", (row["id"],)):
            if other["platform"] != job["platform"] or canonical_account_key(conn, other["account_ref"]) != account_key:
                continue
            if other["status"] in {TARGET_FAILED, TARGET_CANCELLED, TARGET_BLOCKED} and not _recovery_has_submission_evidence(other["checkpoint_json"], other["delivery_state"]):
                continue
            payload = _recovery_json_object(other["payload_json"])
            payload.update(_recovery_json_object(other["checkpoint_json"]).get("payload") or {})
            body = {k: v for k, v in intent_payload(payload).items() if k not in _PREPARED_MEDIA_FIELDS | {"campaignId", "campaignPostId", "platform", "publicationGroup"}}
            if body != approved:
                continue
            for artifact in payload.get("artifacts") or []:
                md = artifact.get("metadata") or {}
                paths = {str(Path(value).resolve()) for value in (artifact.get("local_path"), md.get("input_path")) if value}
                if paths & wanted or md.get("input_sha256") in wanted_hashes:
                    raise ValueError("An exact approved destination intent is already queued/submitted")
        now = _utc_datetime(_now_iso())
        taken = list(destination_pacing.booked_slots(conn, aliases, now=now).get(account_key, []))
        # Only this owned no-effect reservation is released, never another row.
        taken.remove(max(_utc_datetime(row["started_at"] or now), now).isoformat())
        requested = _utc_datetime(row["schedule_at"]) if row["schedule_at"] else None
        base = requested if requested and requested > now else now + timedelta(minutes=30)
        slots, calendar = [], {0: taken}
        for index in range(len(parts)):
            slot = orchestrator._next_free_slot(0, base, 0, calendar, min_gap_minutes=30, max_per_day=3,
                                               now_fn=lambda: now, not_before=base)
            slots.append(slot.isoformat())
            base = slot + timedelta(minutes=30)
        workspace_id, account_id = job["workspace_id"], int(row["account_ref"].split(":", 1)[1])
        # Persist executed SOURCE facts too; never invent unknown legacy digests.
        for artifact in source.get("artifacts") or []:
            file_id = artifact.get("file_record_id")
            if file_id is None:
                continue
            facts = next(entry for entry in snapshot.files if entry[0] == str(Path(artifact["local_path"]).resolve()))
            current = conn.execute("SELECT sha256,size_bytes FROM file_records WHERE id=?", (file_id,)).fetchone()
            if current is None or current["sha256"] not in {None, facts[2]} or current["size_bytes"] not in {None, facts[3]}:
                raise ValueError("Exact source integrity facts changed during preparation")
            conn.execute("UPDATE file_records SET sha256=?,size_bytes=? WHERE id=?", (facts[2], facts[3], file_id))
        campaign_id = source.get("campaignId")
        if campaign_id is None:
            group = conn.execute("INSERT INTO media_groups (name,workspace_id) VALUES (?,?)", (f"Prepared publish intent {row['id']}", workspace_id)).lastrowid
            for order, artifact in enumerate(source.get("artifacts") or []):
                if artifact.get("file_record_id"):
                    conn.execute("INSERT OR IGNORE INTO media_group_items (media_group_id,file_record_id,role,sort_order,workspace_id) VALUES (?,?,?,?,?)",
                                 (group, artifact["file_record_id"], "attachment", order, workspace_id))
            campaign_id = conn.execute("INSERT INTO campaigns (profile_id,media_group_id,status,selected_account_ids_json,metadata_json,workspace_id,prepared_at) "
                "VALUES (?,?,'publishing',?,?,?,?)", (job["profile_id"], group, json.dumps([account_id]),
                json.dumps({"ownedPreparationOriginTargetId": row["id"], "originalIntentHash": job["intent_hash"]}), workspace_id, _now_iso())).lastrowid
        else:
            campaign = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if campaign is None or campaign["status"] in {"preparing", "draft", "needs_review", "cancelled", "failed"} or (_recovery_json_object(campaign["metadata_json"]) or {}).get("_prepLease"):
                raise ValueError("Campaign preparation is separately owned or needs explicit recovery")
        registered = {}
        for part in parts:
            part["campaignId"] = campaign_id
            for artifact in part.get("artifacts") or []:
                artifact.pop("file_record_id", None)  # Final variant owns a campaign registration, never the input's file ID.
                key = _prepared_digest([artifact["local_path"], artifact.get("metadata") or {}, artifact.get("artifact_kind")])
                if key not in registered:
                    identifier = conn.execute("INSERT INTO campaign_artifacts (campaign_id,source_file_record_id,artifact_kind,local_path,public_url,remote_path,metadata_json,workspace_id) VALUES (?,?,?,?,?,?,?,?)",
                        (campaign_id, artifact.get("source_file_record_id"), artifact["artifact_kind"], artifact["local_path"], artifact.get("public_url"), artifact.get("remote_path"),
                         json.dumps(artifact.get("metadata") or {}, sort_keys=True, ensure_ascii=False, allow_nan=False), workspace_id)).lastrowid
                    registered[key] = identifier
                artifact.update(id=registered[key], campaign_id=campaign_id)
        installed = []
        for index, (part, slot) in enumerate(zip(parts, slots), 1):
            part["publicationGroup"] = {"id": snapshot.generation, "index": index, "count": len(parts), "originTargetId": row["id"]}
            post_id = conn.execute("INSERT INTO campaign_posts (campaign_id,platform,account_ids_json,draft_json,sheet_row_json,file_record_ids_json,workspace_id,status) VALUES (?,?,?,?,?,?,?,'queued')",
                (campaign_id, job["platform"], json.dumps([account_id]), json.dumps(part.get("draft") or {}, ensure_ascii=False),
                 json.dumps(part.get("sheetRow") or {}), json.dumps(part.get("fileRecordIds") or []), workspace_id)).lastrowid
            part["campaignPostId"] = post_id
            spec = JobSpec(platform=job["platform"], profile_id=job["profile_id"], payload=part,
                           targets=[(row["account_ref"], f"campaign_post:{post_id}", slot)],
                           idempotency_key=f"owned-prepared:{row['id']}:{snapshot.generation}:{index}")
            part_admission = _owned_part_admission(conn, spec, workspace_id, snapshot.files, snapshot.source_admission)
            if index == 1:
                checkpoint = _recovery_json_object(row["checkpoint_json"])
                checkpoint.update(preparation_generation=snapshot.generation,
                    preparation_history=_prior_target_history(row), payload=part,
                    admission={**checkpoint["admission"], "mode": "validate_only"},
                    readiness={"ready": True, "phase": "ready", "stage": "ready", "reasonCode": "ready", "nextSlot": slot})
                text, _ = _merge_checkpoint(row["checkpoint_json"], checkpoint)
                changed = conn.execute("UPDATE publish_job_targets SET file_ref=?,schedule_at=?,status='pending',checkpoint_json=?,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,next_attempt_at=NULL "
                    "WHERE id=? AND status='running' AND lease_token=? AND checkpoint_json=?",
                    (f"campaign_post:{post_id}", slot, text, row["id"], snapshot.lease_token, row["checkpoint_json"]))
                if changed.rowcount != 1:
                    raise delivery.LeaseLost("Owned generation installation lost its lease/CAS")
                conn.execute("UPDATE campaign_posts SET last_published_job_id=? WHERE id=?", (job["id"], post_id))
                target_id = row["id"]
            else:
                sibling = enqueue_job_in_transaction(conn, spec, workspace_id=workspace_id, campaign_post_id=post_id, admission_snapshot=part_admission)
                target_id = conn.execute("SELECT id FROM publish_job_targets WHERE job_id=?", (sibling.id,)).fetchone()[0]
                text, _ = _merge_checkpoint(conn.execute("SELECT checkpoint_json FROM publish_job_targets WHERE id=?", (target_id,)).fetchone()[0],
                    {"preparation_generation": snapshot.generation, "preparation_origin_target_id": row["id"],
                     "readiness": {"ready": True, "phase": "ready", "stage": "ready", "reasonCode": "ready", "nextSlot": slot}})
                conn.execute("UPDATE publish_job_targets SET checkpoint_json=? WHERE id=?", (text, target_id))
            installed.append(_row_to_target(conn.execute("SELECT * FROM publish_job_targets WHERE id=?", (target_id,)).fetchone()))
        _recount_job(conn, row["job_id"])
        return installed
