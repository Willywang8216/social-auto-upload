"""Durable publish-failure alerts, independent of the publish retry budget.

Enqueue after all publishing routes fail, then ``flush_due(event_key=...)`` for
an immediate attempt. Periodically call ``flush_due()`` to recover interrupted
sends and retry failures, including after a worker restart. The database must
already exist and have Alembic revision 0026 applied; these APIs never bootstrap
it. No network or alert-sender import occurs during enqueue.

Delivery is at-least-once, not externally exactly-once: a crash after delivery
but before acknowledgement, or a sender outliving its lease, can cause a resend.
Only literal ``True`` acknowledges delivery. Sent event keys are retained for
permanent deduplication, and unsuccessful alerts retry indefinitely.

Each flush attempts at most 100 alerts (20 by default), claiming just one at a
time under BEGIN IMMEDIATE and closing the transaction before calling a sender.
The 15-minute lease allows for the existing sender's 30-second channel/chunk
operations; custom senders must also finish within the lease. There is no sleep,
network timeout wrapper, or publish-job dependency here. Flush counts describe
committed acknowledgements/failures; a worker that loses its lease counts neither.
All stored timestamps are fixed-width, aware UTC ISO 8601 strings.
"""

from __future__ import annotations

import sqlite3
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit
from uuid import uuid4

from myUtils.sqlite_utils import connect

MAX_EVENT_KEY_LENGTH = 512
MAX_SUBJECT_LENGTH = 256
MAX_BODY_LENGTH = 16_384
MAX_FLUSH_LIMIT = 100
LEASE_SECONDS = 900
BASE_BACKOFF_SECONDS = 60
MAX_BACKOFF_SECONDS = 6 * 60 * 60

# These diagnostics are deliberately fixed: sender exceptions can contain tokens.
_ERROR_NO_ACK = "sender_did_not_acknowledge"
_ERROR_RAISED = "sender_raised"
_LATEST_NOW = datetime.max.replace(tzinfo=timezone.utc) - timedelta(
    seconds=max(LEASE_SECONDS, MAX_BACKOFF_SECONDS)
)


@dataclass(frozen=True)
class _Claim:
    id: int
    subject: str
    body: str
    attempts: int
    token: str


def _validate_text(name: str, value: str, maximum: int) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{name} must be nonempty, NUL-free and at most {maximum} characters")
    if name == "subject" and ("\r" in value or "\n" in value):
        raise ValueError("subject must be a single line")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{name} must be valid UTF-8 text") from None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_now(now: datetime | None) -> datetime | None:
    if now is None:
        return None
    if not isinstance(now, datetime):
        raise TypeError("now must be an aware datetime or None")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    try:
        result = now.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise ValueError("now cannot be represented in UTC") from None
    if result > _LATEST_NOW:
        raise ValueError("now must allow room for lease and retry timestamps")
    return result


def _timestamp(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds")


def _connect_existing(db_path: Path) -> sqlite3.Connection:
    if not isinstance(db_path, Path):
        raise TypeError("db_path must be a pathlib.Path")
    path = db_path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("db_path must refer to an existing regular file")
    # mode=rw also prevents creation if the file disappears after the check.
    conn = connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def enqueue_alert(*, db_path: Path, event_key: str, subject: str, body: str) -> int:
    """Insert an immutable alert, or return its existing id for an exact retry.

    Keys/subjects/bodies are limited to 512/256/16384 characters respectively.
    Whitespace-only, NUL-containing and invalid UTF-8 strings are rejected;
    subjects must be single-line. No normalization or truncation is performed.
    Reusing a key with different content raises ValueError, even after delivery.
    """
    with _connect_existing(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        return enqueue_in_transaction(conn, event_key=event_key, subject=subject, body=body)


def enqueue_in_transaction(conn: sqlite3.Connection, *, event_key: str,
                           subject: str, body: str) -> int:
    """Enqueue without opening/committing a transaction or importing a sender.

    Used by job transitions so neither a terminal outcome nor its notification
    can commit alone. A failed enqueue rolls back the caller's state change.
    """
    if not conn.in_transaction:
        raise ValueError("Alert enqueue requires the caller's active transaction")
    _validate_text("event_key", event_key, MAX_EVENT_KEY_LENGTH)
    _validate_text("subject", subject, MAX_SUBJECT_LENGTH)
    _validate_text("body", body, MAX_BODY_LENGTH)
    at = _timestamp(_utc_now())
    conn.execute(
        "INSERT INTO publish_alert_outbox "
        "(event_key,subject,body,state,attempts,next_attempt_at,created_at) "
        "VALUES (?,?,?,'pending',0,?,?) ON CONFLICT(event_key) DO NOTHING",
        (event_key, subject, body, at, at),
    )
    row = conn.execute(
        "SELECT id,subject,body FROM publish_alert_outbox WHERE event_key=?", (event_key,)
    ).fetchone()
    if row[1] != subject or row[2] != body:
        raise ValueError("event_key already exists with different alert content")
    return int(row[0])


def stop_event_key(target_id: int, generation: str = "initial") -> str:
    return f"publish-stop:{int(target_id)}:{generation}"


def enqueue_publish_stop(conn: sqlite3.Connection, target_id: int, *,
                         max_attempts: int | None = None) -> int:
    """Build a safe immutable event from the stopped row in this transaction.

    One notice per target/recovery generation, even if a review outcome is
    later reclassified. Existing receipts and publishing state are untouched.
    """
    from myUtils.secret_redaction import redact_text

    row = conn.execute(
        "SELECT t.*,j.platform,j.profile_id,a.account_name,a.nickname "
        "FROM publish_job_targets t JOIN publish_jobs j ON j.id=t.job_id "
        "LEFT JOIN accounts a ON t.account_ref='account:'||a.id WHERE t.id=?",
        (target_id,),
    ).fetchone()
    if row is None or row["status"] not in {"failed", "blocked", "needs_review"}:
        raise ValueError("Only an existing stopped target can enqueue a stop alert")
    try:
        checkpoint = json.loads(row["checkpoint_json"] or "{}")
    except (TypeError, ValueError):
        checkpoint = {}
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    generation = str(checkpoint.get("recovery_generation") or "initial")[:128]
    key = stop_event_key(target_id, generation)
    prior = conn.execute("SELECT id FROM publish_alert_outbox WHERE event_key=?", (key,)).fetchone()
    if prior is not None:
        return int(prior[0])
    status = row["status"]
    routes = checkpoint.get("publish_routes") or {}
    if not isinstance(routes, dict):
        routes = {}
    native, backup = routes.get("native") or {}, routes.get("sociamonials") or {}
    action = ("Verify the existing provider receipt before any retry." if status == "needs_review" or checkpoint.get("provider_receipt") else
              "Fix the cause, preprocess media and use guarded recovery; no automatic retry.")
    label = row["nickname"] or row["account_name"] or ""
    payload = checkpoint.get("payload") or {}
    part = (payload.get("preparedPost") or payload.get("preparedPart") or {}) if isinstance(payload, dict) else {}
    if not isinstance(part, dict):
        part = {}
    lines = [f"Publishing stopped with outcome: {status}. {action}", "",
             f"Occurred (UTC): {row['finished_at'] or _timestamp(_utc_now())}",
             f"Job: #{row['job_id']} ({row['platform']})", f"Target: #{target_id}",
             f"Account: {label} ({row['account_ref']})" if label else f"Account: {row['account_ref']}",
             f"Profile: {row['profile_id'] if row['profile_id'] is not None else 'n/a'}",
             f"Attempts: {row['attempts']}/{max_attempts if max_attempts is not None else 'n/a'}"]
    if part:
        lines.append(f"Part: {part.get('index', '?')}/{part.get('count', '?')}")
    if isinstance(native, dict) and isinstance(backup, dict) and native.get("attempts") and backup.get("attempts"):
        lines[7] = (f"Attempts: native {native['attempts']}/{native.get('max_attempts', max_attempts or 'n/a')}; "
                    f"Sociamonials {backup['attempts']}/{backup.get('max_attempts', max_attempts or 'n/a')} "
                    f"({row['attempts']} queue claims)")
    for provider, detail in (("Native", native), ("Sociamonials", backup)):
        if isinstance(detail, dict) and detail:
            lines.append(f"{provider}: {detail.get('outcome', 'stopped')} "
                         f"({detail.get('attempts', 0)} attempt(s)); "
                         + redact_text(detail.get("error") or "")[:1200])
    if not backup:
        lines.append("Sociamonials: unavailable, not selected, or not safe for this intent.")
    lines.append("Error: " + redact_text(row["last_error"] or "")[:2000])
    origin = str(os.environ.get("SAU_PUBLIC_APP_URL") or os.environ.get("SAU_PUBLIC_BASE_URL") or "").rstrip("/")
    try:
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            origin = ""
    except ValueError:
        origin = ""  # Operator link config cannot roll back a terminal outcome.
    lines.extend(["", f"Open in UI: {origin + '/' if origin else ''}#/jobs?job={row['job_id']}"])
    subject = f"[SAU] Publish {'failed' if status == 'failed' else status}: {row['platform']} target #{target_id} (job #{row['job_id']})"
    return enqueue_in_transaction(conn, event_key=key, subject=subject[:MAX_SUBJECT_LENGTH],
                                  body="\n".join(lines)[:MAX_BODY_LENGTH])


def _claim_one(db_path: Path, event_key: str | None, now: datetime | None) -> _Claim | None:
    with _connect_existing(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Take the real clock after acquiring the lock, not at the start of a batch.
        at = now if now is not None else _utc_now()
        stamp = _timestamp(at)
        predicate = ""
        parameters = [stamp, stamp]
        if event_key is not None:
            predicate = " AND event_key = ?"
            parameters.append(event_key)
        row = conn.execute(
            """
            SELECT id, subject, body, attempts FROM publish_alert_outbox
            WHERE ((state = 'pending' AND next_attempt_at <= ?)
                OR (state = 'sending' AND lease_expires_at <= ?))
            """ + predicate + " ORDER BY next_attempt_at, id LIMIT 1",
            parameters,
        ).fetchone()
        if row is None:
            return None
        token = str(uuid4())
        conn.execute(
            """
            UPDATE publish_alert_outbox
            SET state = 'sending', attempts = attempts + 1,
                lease_token = ?, lease_expires_at = ?
            WHERE id = ?
            """,
            (token, _timestamp(at + timedelta(seconds=LEASE_SECONDS)), row["id"]),
        )
        return _Claim(row["id"], row["subject"], row["body"], row["attempts"] + 1, token)


def _backoff_seconds(attempts: int) -> int:
    # Saturate the exponent before calculation, even after millions of retries.
    return min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2 ** min(attempts - 1, 9)))


def _finish(db_path: Path, claim: _Claim, delivered: bool, error: str | None,
            now: datetime | None) -> bool:
    with _connect_existing(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        at = now if now is not None else _utc_now()
        if delivered:
            cursor = conn.execute(
                """
                UPDATE publish_alert_outbox
                SET state = 'sent', sent_at = ?, lease_token = NULL,
                    lease_expires_at = NULL, last_error = NULL
                WHERE id = ? AND state = 'sending' AND lease_token = ?
                """,
                (_timestamp(at), claim.id, claim.token),
            )
        else:
            cursor = conn.execute(
                """
                UPDATE publish_alert_outbox
                SET state = 'pending', next_attempt_at = ?, lease_token = NULL,
                    lease_expires_at = NULL, last_error = ?
                WHERE id = ? AND state = 'sending' AND lease_token = ?
                """,
                (_timestamp(at + timedelta(seconds=_backoff_seconds(claim.attempts))),
                 error, claim.id, claim.token),
            )
        return cursor.rowcount == 1


def flush_due(*, db_path: Path, limit: int = 20, event_key: str | None = None,
              sender: Callable | None = None, now: datetime | None = None) -> dict:
    """Attempt a bounded number of due alerts; return {"sent": n, "failed": n}.

    ``limit`` must be an integer from 1 to 100 (bool is not accepted). An optional
    event key restricts the attempt to that alert, without bypassing retry times
    or live leases. An aware ``now`` is converted to UTC and fixes the clock for
    deterministic calls; otherwise the real UTC clock is refreshed per claim
    and completion. Sender exceptions/non-True results retry from 60 seconds,
    doubling to a six-hour cap indefinitely. BaseException interrupts leave the
    lease intact for recovery. Database errors propagate, never discard alerts.
    """
    if type(limit) is not int:
        raise TypeError("limit must be an integer")
    if not 1 <= limit <= MAX_FLUSH_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_FLUSH_LIMIT}")
    if event_key is not None:
        _validate_text("event_key", event_key, MAX_EVENT_KEY_LENGTH)
    if sender is not None and not callable(sender):
        raise TypeError("sender must be callable or None")
    fixed_now = _validate_now(now)
    counts = {"sent": 0, "failed": 0}
    for _ in range(limit):
        claim = _claim_one(db_path, event_key, fixed_now)
        if claim is None:
            break
        try:
            if sender is None:
                # Import only after claiming work; enqueue/empty flush stay offline.
                from myUtils.ops_alerts import send_ops_alert

                sender = send_ops_alert
            delivered = sender(subject=claim.subject, body=claim.body) is True
            error = None if delivered else _ERROR_NO_ACK
        except Exception:
            delivered, error = False, _ERROR_RAISED
        if _finish(db_path, claim, delivered, error, fixed_now):
            counts["sent" if delivered else "failed"] += 1
    return counts
