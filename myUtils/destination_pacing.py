"""Shared destination bookings and bounded allocation, without HTTP/frameworks.

All readers use the caller's SQLite snapshot and the same native/managed identity
union. Timestamps stored in the queue are UTC; only the daily budget uses the
operator's timezone. Bad history is a refusal, never an empty booking calendar.
"""
from __future__ import annotations

import re
import sqlite3
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

from myUtils.account_identity import canonical_account_keys, json_object
from myUtils.sqlite_utils import connect as sqlite_connect


MAX_ALLOCATION_DAYS = 3660
MAX_ALLOCATION_STEPS = 100000
_ISO_DATETIME = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)?\Z"
)


class PacingRefused(ValueError):
    """Fixed, non-secret refusal for invalid history or an exhausted horizon."""


def utc_datetime(value: datetime | str) -> datetime:
    """Strict UTC-naive queue timestamp, including legacy explicit offsets."""
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and _ISO_DATETIME.fullmatch(value.strip()):
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            raise ValueError
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except (TypeError, ValueError, OverflowError):
        raise PacingRefused("unreadable destination booking history") from None


def connect_readonly(db_path: Path | str) -> sqlite3.Connection:
    """Open an existing regular database, never create it (even after a race)."""
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError("Destination pacing database must be an existing regular file")
    conn = sqlite_connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
    except BaseException:
        conn.close()
        raise
    return conn


def account_refs(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Canonical group -> every current/historical reference, including new rows."""
    aliases: dict[str, list[str]] = {}
    for ref, key in canonical_account_keys(conn).items():
        aliases.setdefault(key, []).append(ref)
    return aliases


def identity_for_ref(aliases: Mapping[str, list[str]], account_ref: str) -> str:
    return next(key for key, refs in aliases.items() if account_ref in refs)


def _checkpoint(text: str | None) -> dict:
    try:
        value = json_object("{}" if text is None else text)
        if value.get("checkpoint_invalid"):
            raise ValueError
        receipt = value.get("provider_receipt")
        if receipt is not None and not isinstance(receipt, dict):
            raise ValueError
        return value
    except ValueError:
        raise PacingRefused("unreadable destination booking history") from None


def _receipt_key(receipt: dict, *, confirmed: bool) -> tuple | None:
    """Deduplicate only a confirmed, stable receipt, scoped to its provider route."""
    provider, identifier = receipt.get("provider"), receipt.get("id")
    if (not confirmed or not isinstance(provider, str) or not provider.strip()
            or isinstance(identifier, bool) or not isinstance(identifier, (str, int))
            or str(identifier).strip().lower() in {"", "0", "confirmed", "delivered", "unknown", "pending", "processing"}):
        return None
    refs = receipt.get("profile_refs", [])
    if not isinstance(refs, list) or any(not isinstance(ref, (str, int)) or isinstance(ref, bool) for ref in refs):
        raise PacingRefused("unreadable destination booking history")
    return (provider.strip().lower(), str(identifier).strip(),
            str(receipt.get("workspace_id") or ""), str(receipt.get("network") or ""),
            tuple(sorted(str(ref) for ref in refs)))


def _pending_evidence(checkpoint: dict) -> bool:
    from myUtils.delivery import has_unresolved_submission

    receipt = checkpoint.get("provider_receipt") or {}
    if (receipt.get("provider") == "tiktok" and receipt.get("status") == "failed"
            and receipt.get("provider_status") == "failed" and receipt.get("terminal_failure") is True
            and not any(checkpoint.get(key) for key in (
                "completed_operations", "provider_operations", "telegramCompletedByDelivery"))):
        return False  # A scoped, provider-confirmed terminal non-delivery.
    return has_unresolved_submission({"_delivery": checkpoint})


def booked_slots(
    conn: sqlite3.Connection, aliases: Mapping[str, list[str]], *, now: datetime | str | None = None,
) -> dict[str, list[str]]:
    """Bookings plus confirmed/unknown delivery history consume a group's cap.

    Receipt delivery time wins over finished/started/schedule for confirmations.
    Unresolved overdue or undated work reserves capacity now, not an empty/past
    calendar. Only the SAME confirmed provider receipt may collapse alias rows.
    """
    identities = {ref: key for key, refs in aliases.items() for ref in refs}
    current = utc_datetime(now if now is not None else datetime.now(timezone.utc))
    entries: dict[str, dict[tuple, tuple[datetime, bool]]] = {}
    for row in conn.execute(
        "SELECT id,account_ref,status,schedule_at,started_at,finished_at,checkpoint_json,delivery_state "
        "FROM publish_job_targets WHERE status IN "
        "('pending','retrying','running','awaiting_delivery','needs_review','succeeded','cancelled','failed','blocked') ORDER BY id"
    ):
        checkpoint = _checkpoint(row["checkpoint_json"])
        receipt = checkpoint.get("provider_receipt") or {}
        confirmed = (row["status"] == "succeeded" or row["delivery_state"] == "delivered"
                     or receipt.get("status") == "delivered")
        if row["status"] in {"cancelled", "failed", "blocked"} and not confirmed and not _pending_evidence(checkpoint):
            continue
        actual = receipt.get("delivered_at")
        if actual is not None and actual != "":
            actual = utc_datetime(actual)
        if confirmed:
            moment = actual or row["finished_at"] or row["started_at"] or row["schedule_at"] or current
        elif row["status"] in {"running", "awaiting_delivery", "needs_review", "cancelled", "failed", "blocked"}:
            moment = receipt.get("submitted_at") or row["started_at"] or current
        else:
            moment = row["schedule_at"] or row["started_at"] or current
        moment = utc_datetime(moment)
        if not confirmed:
            moment = max(moment, current)
        identity = identities.get(row["account_ref"])
        if identity is None:
            raise PacingRefused("unreadable destination booking history")
        key = _receipt_key(receipt, confirmed=confirmed) or ("target", row["id"])
        group = entries.setdefault(identity, {})
        previous = group.get(key)
        if previous is None or (actual and not previous[1]):
            group[key] = (moment, bool(actual))
        elif bool(actual) == previous[1]:
            if actual and moment != previous[0]:
                raise PacingRefused("conflicting confirmed destination booking history")
            group[key] = (min(moment, previous[0]), bool(actual))
    return {identity: [moment.isoformat() for moment, _actual in sorted(group.values())]
            for identity, group in entries.items()}


def _integer(value: int, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise PacingRefused(f"invalid {label}")
    return value


def effective_daily_cap(configured: int, requested: int | None = None) -> int:
    """A request can tighten a cap, never raise/disable a configured positive cap."""
    _integer(configured, "configured daily cap")
    if requested is None:
        return configured
    _integer(requested, "requested daily cap")
    if configured == 0:  # Existing explicit unlimited policy.
        return requested
    return min(configured, requested) if requested else configured


def effective_gap_minutes(configured: int, requested: int | None = None) -> int:
    _integer(configured, "configured minimum gap")
    if requested is not None:
        _integer(requested, "requested minimum gap")
    # Historically even a configured zero gap enforced at least one minute.
    return max(1, configured, requested or 0)


def allocate_slot(
    taken: list[str], base_time: datetime | None, offset_index: int, *,
    configured_gap: int, configured_cap: int, requested_gap: int | None = None,
    requested_cap: int | None = None, stagger_minutes: int = 5,
    operator_timezone: tzinfo = timezone.utc, now_fn: Callable | None = None,
    horizon_days: int = MAX_ALLOCATION_DAYS,
    not_before: datetime | str | None = None,
) -> datetime:
    """Allocate and append a UTC slot, including publish-now, with finite bounds."""
    cap = effective_daily_cap(configured_cap, requested_cap)
    minutes = effective_gap_minutes(configured_gap, requested_gap)
    _integer(offset_index, "stagger offset")
    _integer(stagger_minutes, "stagger minutes")
    _integer(horizon_days, "allocation horizon", minimum=1)
    existing = sorted(utc_datetime(value) for value in taken)

    def local(moment: datetime) -> datetime:
        return moment.replace(tzinfo=timezone.utc).astimezone(operator_timezone)

    try:
        anchor = base_time if base_time is not None else (now_fn or (lambda: datetime.now(timezone.utc)))()
        candidate = utc_datetime(anchor) + timedelta(minutes=stagger_minutes * offset_index)
        if not_before is not None:
            candidate = max(candidate, utc_datetime(not_before))
        if candidate.microsecond:
            candidate = candidate.replace(microsecond=0) + timedelta(seconds=1)
        deadline = candidate + timedelta(days=horizon_days)
        gap = timedelta(minutes=minutes)
        day_counts = Counter(local(moment).date() for moment in existing)
        slot_time = local(candidate).time()
        for _step in range(MAX_ALLOCATION_STEPS):
            if candidate > deadline:
                break
            day = local(candidate).date()
            if cap and day_counts[day] >= cap:
                candidate = datetime.combine(day + timedelta(days=1), slot_time, tzinfo=operator_timezone)
                candidate = utc_datetime(candidate)
                continue
            index = bisect_left(existing, candidate)
            conflicts = [moment for moment in existing[max(0, index - 1):index + 1]
                         if abs(moment - candidate) < gap]
            if not conflicts:
                taken.append(candidate.isoformat(timespec="seconds"))
                return candidate
            candidate = max(moment + gap for moment in conflicts)
    except (OverflowError, TypeError, ValueError) as exc:
        if isinstance(exc, PacingRefused):
            raise
        raise PacingRefused("destination allocation is outside the supported datetime range") from None
    raise PacingRefused("destination allocation horizon exhausted")
