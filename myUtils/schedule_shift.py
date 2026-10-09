"""Move scheduled publish targets forward by N days — smartly.

An operator who wants to delay a batch ("move the schedule by 3 days") should
not have to hand-edit ``schedule_at`` values, and definitely should not do so
naively: a raw ``+3 days`` on every pending target can push two posts for the
same account inside the anti-spam window, or stack more than
``MAX_POSTS_PER_ACCOUNT_PER_DAY`` posts onto one day. This module makes the
operation a first-class, repeatable feature:

* **Selection scope** — by profile, platform, account, campaign, status, date
  range, explicit target ids, or everything pending (the default).
* **Wall-clock preservation** — ``schedule_at`` is stored as naive UTC (see
  ``jobs._now_iso`` and ``publish_orchestrator._resolve_base_time``). The
  operator reasons in their own timezone (``Asia/Shanghai`` by default,
  ``SAU_OPERATOR_TIMEZONE`` overrides). An N-day shift adds N *calendar* days
  to the operator's local wall clock and converts back to UTC, so an 08:00
  post stays an 08:00 post. For a DST-free zone this is identical to adding
  ``86400*N`` seconds; that equivalence is asserted, not assumed.
* **Smart adjustment** — after the wall-clock shift, each target is walked
  through the *existing* allocator (``publish_orchestrator._next_free_slot``,
  guarded by ``slot_reservation_lock``) so the result still honours
  ``MIN_GAP_MINUTES`` and ``MAX_POSTS_PER_ACCOUNT_PER_DAY``. Targets that
  collide get moved further than N days, and the plan says why.

Safety by default: :func:`plan_shift` is read-only. :func:`apply_shift` is the
only writer, it re-plans under the slot lock so a concurrent submit cannot
invalidate the allocation, and it refuses to move a target that changed status
after the plan was built. ``status='running'`` is never touched and
``succeeded``/``cancelled`` are excluded unless ``include_terminal`` is set.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Sequence

from myUtils import publish_orchestrator as po

DEFAULT_SHIFT_DAYS = 3
DEFAULT_STATUSES: tuple[str, ...] = ("pending", "retrying")
# ``running`` is never moved (no way to abort an in-flight upload). Terminal
# success/cancellation is skipped unless the caller explicitly opts in.
NEVER_MOVE_STATUSES: frozenset[str] = frozenset({"running"})
TERMINAL_STATUSES: frozenset[str] = frozenset({"succeeded", "cancelled"})


# ---------------------------------------------------------------------------
# Wall-clock arithmetic
# ---------------------------------------------------------------------------

def shift_wall_clock(dt_utc: datetime, days: int, operator_tz: tzinfo) -> datetime:
    """Add ``days`` calendar days to ``dt_utc`` while preserving local time.

    ``dt_utc`` is a naive datetime interpreted as UTC (the storage format).
    Returns a naive UTC datetime. The arithmetic happens on the *local* wall
    clock: convert to the operator's zone, add the days to the calendar date,
    then re-localise and convert back. In a DST-free zone this equals the naive
    ``dt_utc + timedelta(days=days)``; the tests assert the equivalence and also
    pin a DST-observing zone where the two diverge.
    """
    aware_utc = dt_utc.replace(tzinfo=timezone.utc)
    local = aware_utc.astimezone(operator_tz)
    # ``.time()`` is always naive; combine against the shifted calendar date so
    # a DST transition changes the UTC offset rather than the wall clock.
    shifted_local = datetime.combine(
        local.date() + timedelta(days=int(days)), local.time()
    )
    localised = shifted_local.replace(tzinfo=operator_tz)
    return localised.astimezone(timezone.utc).replace(tzinfo=None)


def local_date_range_to_utc(
    start: date | None,
    end: date | None,
    operator_tz: tzinfo,
) -> tuple[datetime | None, datetime | None]:
    """Convert operator-local inclusive dates to a naive-UTC [start, end) range.

    ``end`` is inclusive on input; the returned upper bound is the start of the
    day *after* it so the query can use a simple ``<`` comparison.
    """
    start_utc = end_utc = None
    if start is not None:
        start_utc = (
            datetime.combine(start, time(0, 0))
            .replace(tzinfo=operator_tz)
            .astimezone(timezone.utc)
            .replace(tzinfo=None)
        )
    if end is not None:
        end_utc = (
            datetime.combine(end + timedelta(days=1), time(0, 0))
            .replace(tzinfo=operator_tz)
            .astimezone(timezone.utc)
            .replace(tzinfo=None)
        )
    return start_utc, end_utc


def parse_operator_date(value: object) -> date | None:
    """Parse ``YYYY-MM-DD`` (or a leading ISO datetime) as an operator date."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Scope + plan types
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ShiftScope:
    """Which targets a shift applies to. All conditions are ANDed."""

    profile_id: int | None = None
    platform: str | None = None
    account_ids: tuple[int, ...] = ()
    campaign_id: int | None = None
    statuses: tuple[str, ...] = DEFAULT_STATUSES
    start_at: datetime | None = None  # naive UTC, inclusive
    end_at: datetime | None = None    # naive UTC, exclusive
    include_terminal: bool = False
    target_ids: tuple[int, ...] = ()


@dataclass(slots=True)
class ShiftChange:
    target_id: int
    job_id: int
    account_ref: str
    account_id: int | None
    platform: str | None
    profile_id: int | None
    campaign_id: int | None
    status: str
    original: str
    shifted: str
    new: str
    moved_further: bool
    reason: str
    original_in_past: bool

    @property
    def extra_seconds(self) -> float:
        return (po._parse_schedule(self.new) - po._parse_schedule(self.shifted)).total_seconds()

    def to_dict(self) -> dict:
        return {
            "targetId": self.target_id,
            "jobId": self.job_id,
            "accountRef": self.account_ref,
            "accountId": self.account_id,
            "platform": self.platform,
            "profileId": self.profile_id,
            "campaignId": self.campaign_id,
            "status": self.status,
            "old": self.original,
            "shifted": self.shifted,
            "new": self.new,
            "movedFurther": self.moved_further,
            "reason": self.reason,
            "originalInPast": self.original_in_past,
        }


@dataclass(slots=True)
class ShiftPlan:
    days: int
    timezone: str
    min_gap_minutes: int
    max_per_day: int
    changes: list[ShiftChange] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    dropped_protected: list[str] = field(default_factory=list)

    @property
    def adjusted(self) -> list[ShiftChange]:
        return [c for c in self.changes if c.moved_further]

    def summary(self) -> dict:
        by_reason = Counter(c.reason for c in self.adjusted)
        by_platform = Counter(c.platform or "unknown" for c in self.changes)
        by_account = Counter(c.account_ref for c in self.changes)
        return {
            "days": self.days,
            "timezone": self.timezone,
            "minGapMinutes": self.min_gap_minutes,
            "maxPerDay": self.max_per_day,
            "total": len(self.changes),
            "movedFurther": len(self.adjusted),
            "byReason": dict(by_reason),
            "byPlatform": dict(by_platform),
            "byAccount": dict(by_account),
            "pastDueOriginal": sum(1 for c in self.changes if c.original_in_past),
            "skipped": len(self.skipped),
            "droppedProtectedStatuses": list(self.dropped_protected),
        }


@dataclass(slots=True)
class ShiftApplyResult:
    plan: ShiftPlan
    applied: int
    write_skipped: list[dict] = field(default_factory=list)
    backup_path: Path | None = None

    def summary(self) -> dict:
        out = dict(self.plan.summary())
        out["applied"] = self.applied
        out["writeSkipped"] = len(self.write_skipped)
        out["backupPath"] = str(self.backup_path) if self.backup_path else None
        return out


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def _effective_statuses(scope: ShiftScope) -> tuple[list[str], list[str]]:
    statuses = [s.strip() for s in (scope.statuses or DEFAULT_STATUSES) if s and s.strip()]
    dropped: list[str] = []
    if not scope.include_terminal:
        dropped = [s for s in statuses if s in TERMINAL_STATUSES]
        statuses = [s for s in statuses if s not in TERMINAL_STATUSES]
    # ``running`` can never be relocated regardless of the caller's list.
    statuses = [s for s in statuses if s not in NEVER_MOVE_STATUSES]
    if not statuses:
        statuses = list(DEFAULT_STATUSES)
    return statuses, dropped


def _account_id(account_ref: str) -> int | None:
    try:
        return int(str(account_ref).split(":", 1)[1])
    except (IndexError, ValueError):
        return None


def _fetch_selected_rows(
    *,
    db_path: Path,
    scope: ShiftScope,
    workspace_id: str | None,
) -> tuple[list[sqlite3.Row], list[str]]:
    statuses, dropped = _effective_statuses(scope)
    clauses = ["t.schedule_at IS NOT NULL", "t.schedule_at != ''"]
    params: list = []
    placeholders = ",".join("?" * len(statuses))
    clauses.append(f"t.status IN ({placeholders})")
    params.extend(statuses)
    if scope.profile_id is not None:
        clauses.append("j.profile_id = ?")
        params.append(int(scope.profile_id))
    if scope.platform:
        clauses.append("j.platform = ?")
        params.append(str(scope.platform))
    if scope.account_ids:
        refs = [f"account:{int(a)}" for a in scope.account_ids]
        clauses.append(f"t.account_ref IN ({','.join('?' * len(refs))})")
        params.extend(refs)
    if scope.campaign_id is not None:
        clauses.append("cp.campaign_id = ?")
        params.append(int(scope.campaign_id))
    if scope.target_ids:
        clauses.append(f"t.id IN ({','.join('?' * len(scope.target_ids))})")
        params.extend(int(t) for t in scope.target_ids)
    if scope.start_at is not None:
        clauses.append("t.schedule_at >= ?")
        params.append(scope.start_at.isoformat(timespec="seconds"))
    if scope.end_at is not None:
        clauses.append("t.schedule_at < ?")
        params.append(scope.end_at.isoformat(timespec="seconds"))
    if workspace_id is not None:
        clauses.append("j.workspace_id = ?")
        params.append(workspace_id)

    sql = f"""
        SELECT t.id, t.job_id, t.account_ref, t.file_ref, t.schedule_at, t.status,
               j.platform, j.profile_id, cp.campaign_id AS campaign_id
        FROM publish_job_targets t
        JOIN publish_jobs j ON j.id = t.job_id
        LEFT JOIN campaign_posts cp ON ('campaign_post:' || cp.id) = t.file_ref
        WHERE {" AND ".join(clauses)}
        ORDER BY t.schedule_at, t.id
    """
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return rows, dropped


def _remove_selected_bookings(
    booked: dict[int, list[str]],
    rows: Sequence[sqlite3.Row],
) -> None:
    """Drop the selected targets' own current slots from the booking map.

    Without this a target's *old* time would block its new one, and the
    allocator would see the very bookings it is about to rewrite. Non
    pending/retrying targets are not in ``_load_booked_slots`` to begin with,
    so a ``remove`` miss is harmless.
    """
    for row in rows:
        account_id = _account_id(row["account_ref"])
        if account_id is None:
            continue
        times = booked.get(account_id)
        slot = row["schedule_at"]
        if times and slot in times:
            times.remove(slot)


def plan_shift(
    *,
    db_path: Path,
    days: int = DEFAULT_SHIFT_DAYS,
    scope: ShiftScope | None = None,
    min_gap_minutes: int | None = None,
    max_per_day: int | None = None,
    operator_tz: tzinfo | None = None,
    now: datetime | None = None,
    workspace_id: str | None = None,
) -> ShiftPlan:
    """Compute (but never write) the shifted, collision-free schedule.

    ``days`` must be positive; the operation is a forward move. The returned
    plan lists every target that would change, the wall-clock-shifted time, the
    final allocated time, and — for anything the allocator pushed beyond the
    plain shift — the reason (``"min gap"`` or ``"daily cap"``).
    """
    if int(days) < 1:
        raise ValueError("days must be a positive integer (forward move only)")
    scope = scope or ShiftScope()
    tz = operator_tz or po._operator_timezone()
    now = now or datetime.now(tz=timezone.utc).replace(tzinfo=None)
    gap = max(1, min_gap_minutes if min_gap_minutes is not None else po.MIN_GAP_MINUTES)
    cap = max(0, max_per_day if max_per_day is not None else po.MAX_POSTS_PER_ACCOUNT_PER_DAY)

    rows, dropped = _fetch_selected_rows(
        db_path=db_path, scope=scope, workspace_id=workspace_id
    )

    booked = po._load_booked_slots(db_path)
    _remove_selected_bookings(booked, rows)

    # Group by account and allocate in chronological order of the *shifted*
    # time so the earliest target keeps its slot and later ones walk forward.
    planned: list[tuple[int, datetime, sqlite3.Row, datetime]] = []
    skipped: list[dict] = []
    for row in rows:
        original = po._parse_schedule(row["schedule_at"])
        if original is None:
            skipped.append({
                "targetId": int(row["id"]),
                "reason": "unparseable schedule_at",
            })
            continue
        shifted = shift_wall_clock(original, int(days), tz)
        planned.append((int(row["id"]), shifted, row, original))
    planned.sort(key=lambda item: (item[2]["account_ref"], item[1], item[0]))

    changes: list[ShiftChange] = []
    for _target_id, shifted, row, original in planned:
        account_ref = str(row["account_ref"])
        account_id = _account_id(account_ref)
        if account_id is None:
            # A legacy filename ref has no per-account invariant to enforce; the
            # wall-clock shift is still the right move, it just cannot collide.
            changes.append(ShiftChange(
                target_id=int(row["id"]),
                job_id=int(row["job_id"]),
                account_ref=account_ref,
                account_id=None,
                platform=row["platform"],
                profile_id=row["profile_id"],
                campaign_id=row["campaign_id"],
                status=str(row["status"]),
                original=original.isoformat(timespec="seconds"),
                shifted=shifted.isoformat(timespec="seconds"),
                new=shifted.isoformat(timespec="seconds"),
                moved_further=False,
                reason="",
                original_in_past=original <= now,
            ))
            continue

        before = list(booked.get(account_id, []))
        existing = [t for t in (po._parse_schedule(v) for v in before) if t is not None]
        same_day = sum(1 for t in existing if t.date() == shifted.date())
        has_conflict = any(
            abs((t - shifted).total_seconds()) < gap * 60
            for t in existing
        )
        new = po._next_free_slot(
            account_id, shifted, 0, booked,
            min_gap_minutes=gap, max_per_day=cap,
        )
        if cap and same_day >= cap:
            reason = "daily cap"
        elif has_conflict:
            reason = "min gap"
        else:
            reason = ""
        moved_further = bool(new is not None and new > shifted)
        changes.append(ShiftChange(
            target_id=int(row["id"]),
            job_id=int(row["job_id"]),
            account_ref=account_ref,
            account_id=account_id,
            platform=row["platform"],
            profile_id=row["profile_id"],
            campaign_id=row["campaign_id"],
            status=str(row["status"]),
            original=original.isoformat(timespec="seconds"),
            shifted=shifted.isoformat(timespec="seconds"),
            new=new.isoformat(timespec="seconds") if new is not None else "",
            moved_further=moved_further,
            reason=reason,
            original_in_past=original <= now,
        ))

    changes.sort(key=lambda c: (c.new, c.target_id))
    return ShiftPlan(
        days=int(days),
        timezone=str(getattr(tz, "key", tz)),
        min_gap_minutes=gap,
        max_per_day=cap,
        changes=changes,
        skipped=skipped,
        dropped_protected=dropped,
    )


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def backup_database(db_path: Path, *, label: str = "schedule-shift") -> Path:
    """Copy the SQLite file beside itself before a write.

    A plain ``shutil.copy2`` is used deliberately: the connection journal mode
    is the default ``delete`` (see ``jobs._connect``), so no WAL sidecar exists
    to miss.
    """
    db_path = Path(db_path)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = db_path.with_name(f"{db_path.name}.bak-{label}-{stamp}")
    shutil.copy2(db_path, dest)
    return dest


def apply_shift(
    *,
    db_path: Path,
    days: int = DEFAULT_SHIFT_DAYS,
    scope: ShiftScope | None = None,
    min_gap_minutes: int | None = None,
    max_per_day: int | None = None,
    operator_tz: tzinfo | None = None,
    now: datetime | None = None,
    workspace_id: str | None = None,
    backup: bool = False,
) -> ShiftApplyResult:
    """Plan and write the shift, re-planning under the slot reservation lock.

    Re-planning inside the lock is what makes the allocation safe: another
    submit's slot pass cannot interleave between the read and the write. The
    ``UPDATE`` is additionally guarded on the status the plan saw, so a target
    the worker claimed (pending -> running) in the meantime is skipped rather
    than relocated mid-upload.
    """
    backup_path = backup_database(db_path) if backup else None
    with po.slot_reservation_lock(db_path):
        plan = plan_shift(
            db_path=db_path,
            days=days,
            scope=scope,
            min_gap_minutes=min_gap_minutes,
            max_per_day=max_per_day,
            operator_tz=operator_tz,
            now=now,
            workspace_id=workspace_id,
        )
        applied = 0
        write_skipped: list[dict] = []
        conn = sqlite3.connect(str(db_path), timeout=30)
        try:
            with conn:
                for change in plan.changes:
                    cursor = conn.execute(
                        """
                        UPDATE publish_job_targets
                        SET schedule_at = ?
                        WHERE id = ? AND status = ?
                        """,
                        (change.new, change.target_id, change.status),
                    )
                    if cursor.rowcount == 1:
                        applied += 1
                    else:
                        write_skipped.append({
                            "targetId": change.target_id,
                            "reason": "status changed after planning; left untouched",
                        })
        finally:
            conn.close()
    return ShiftApplyResult(
        plan=plan,
        applied=applied,
        write_skipped=write_skipped,
        backup_path=backup_path,
    )


def format_plan_table(plan: ShiftPlan, *, limit: int = 40) -> str:
    """Render a compact before/after table for the CLI and the report."""
    lines = [
        f"{'TARGET':>8}  {'PLATFORM':<10}  {'ACCOUNT':<12}  {'STATUS':<9}  "
        f"{'OLD (UTC)':<19}  {'NEW (UTC)':<19}  EXTRA  REASON"
    ]
    for change in plan.changes[:limit]:
        extra = ""
        if change.moved_further:
            seconds = change.extra_seconds
            extra = f"{seconds / 3600:.1f}h"
        lines.append(
            f"{change.target_id:>8}  {(change.platform or '?'):<10}  "
            f"{change.account_ref:<12}  {change.status:<9}  "
            f"{change.original:<19}  {change.new:<19}  {extra:<5}  {change.reason}"
        )
    if len(plan.changes) > limit:
        lines.append(f"... and {len(plan.changes) - limit} more")
    return "\n".join(lines)
