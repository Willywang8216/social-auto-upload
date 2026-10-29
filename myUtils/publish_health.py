"""Publish pipeline health and observability.

Provides a :func:`publish_health_snapshot` function that returns a structured
summary of the pipeline's current state: success rates, active failures,
stale targets, and account health. Designed to back both the ``/api/publish/health``
endpoint and the operator alert digest.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class PlatformHealth:
    """Per-platform publish statistics."""
    platform: str
    succeeded: int = 0
    failed: int = 0
    pending: int = 0
    running: int = 0
    retrying: int = 0
    cancelled: int = 0

    @property
    def total(self) -> int:
        return self.succeeded + self.failed + self.pending + self.running + self.retrying + self.cancelled

    @property
    def success_rate(self) -> float:
        finished = self.succeeded + self.failed
        if finished == 0:
            return 0.0
        return self.succeeded / finished

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "pending": self.pending,
            "running": self.running,
            "retrying": self.retrying,
            "cancelled": self.cancelled,
            "total": self.total,
            "successRate": round(self.success_rate, 3),
        }


@dataclass(slots=True)
class FailureGroup:
    """A group of targets that failed with the same error pattern."""
    error_pattern: str
    count: int
    last_seen: str
    platform: str = ""
    category: str = ""  # transient / permanent / recoverable

    def to_dict(self) -> dict:
        return {
            "errorPattern": self.error_pattern,
            "count": self.count,
            "lastSeen": self.last_seen,
            "platform": self.platform,
            "category": self.category,
        }


@dataclass(slots=True)
class StaleTarget:
    """A target stuck in 'running' state."""
    target_id: int
    account_ref: str
    platform: str
    started_at: str
    stuck_minutes: int

    def to_dict(self) -> dict:
        return {
            "targetId": self.target_id,
            "accountRef": self.account_ref,
            "platform": self.platform,
            "startedAt": self.started_at,
            "stuckMinutes": self.stuck_minutes,
        }


@dataclass(slots=True)
class HealthSnapshot:
    """Complete health snapshot of the publish pipeline."""
    timestamp: str
    platforms: list[PlatformHealth] = field(default_factory=list)
    recent_failures: list[FailureGroup] = field(default_factory=list)
    stale_targets: list[StaleTarget] = field(default_factory=list)
    accounts_needing_reconnect: list[dict] = field(default_factory=list)
    totals: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "platforms": [p.to_dict() for p in self.platforms],
            "recentFailures": [f.to_dict() for f in self.recent_failures],
            "staleTargets": [s.to_dict() for s in self.stale_targets],
            "accountsNeedingReconnect": self.accounts_needing_reconnect,
            "totals": self.totals,
        }


def publish_health_snapshot(
    db_path: Path,
    *,
    failure_window_hours: int = 24,
    stale_threshold_minutes: int = 30,
) -> HealthSnapshot:
    """Build a health snapshot from the database.

    Parameters
    ----------
    db_path:
        Path to the SQLite database.
    failure_window_hours:
        How far back to look for recent failures.
    stale_threshold_minutes:
        How long a target must be ``running`` before it's considered stale.
    """
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    snapshot = HealthSnapshot(timestamp=now.isoformat(timespec="seconds"))

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # Per-platform stats.
        snapshot.platforms = _platform_stats(conn)

        # Totals.
        snapshot.totals = _totals(conn)

        # Recent failures grouped by error pattern.
        cutoff = (now - timedelta(hours=failure_window_hours)).isoformat(timespec="seconds")
        snapshot.recent_failures = _recent_failures(conn, cutoff)

        # Stale running targets.
        snapshot.stale_targets = _stale_targets(conn, now, stale_threshold_minutes)

        # Accounts flagged for reconnect.
        snapshot.accounts_needing_reconnect = _accounts_needing_reconnect(conn)

    finally:
        conn.close()

    return snapshot


def _platform_stats(conn: sqlite3.Connection) -> list[PlatformHealth]:
    """Aggregate target counts by platform and status."""
    rows = conn.execute("""
        SELECT j.platform, t.status, COUNT(*) as cnt
        FROM publish_job_targets t
        JOIN publish_jobs j ON j.id = t.job_id
        GROUP BY j.platform, t.status
    """).fetchall()

    by_platform: dict[str, PlatformHealth] = {}
    for row in rows:
        platform = str(row["platform"] or "unknown")
        status = str(row["status"] or "unknown")
        count = int(row["cnt"])

        if platform not in by_platform:
            by_platform[platform] = PlatformHealth(platform=platform)

        ph = by_platform[platform]
        if status == "succeeded":
            ph.succeeded = count
        elif status == "failed":
            ph.failed = count
        elif status == "pending":
            ph.pending = count
        elif status == "running":
            ph.running = count
        elif status == "retrying":
            ph.retrying = count
        elif status == "cancelled":
            ph.cancelled = count

    return sorted(by_platform.values(), key=lambda p: p.total, reverse=True)


def _totals(conn: sqlite3.Connection) -> dict:
    """Overall target counts."""
    rows = conn.execute("""
        SELECT status, COUNT(*) as cnt
        FROM publish_job_targets
        GROUP BY status
    """).fetchall()

    totals = {}
    for row in rows:
        totals[str(row["status"])] = int(row["cnt"])

    total = sum(totals.values())
    succeeded = totals.get("succeeded", 0)
    failed = totals.get("failed", 0)
    finished = succeeded + failed

    totals["total"] = total
    totals["overallSuccessRate"] = round(succeeded / finished, 3) if finished > 0 else 0.0
    return totals


def _recent_failures(conn: sqlite3.Connection, since: str) -> list[FailureGroup]:
    """Group recent failures by error pattern."""
    rows = conn.execute("""
        SELECT j.platform, t.last_error, t.finished_at
        FROM publish_job_targets t
        JOIN publish_jobs j ON j.id = t.job_id
        WHERE t.status = 'failed'
          AND t.finished_at >= ?
          AND t.last_error IS NOT NULL
        ORDER BY t.finished_at DESC
        LIMIT 100
    """, (since,)).fetchall()

    # Group by truncated error pattern in Python (SQLite doesn't have custom functions).
    groups: dict[str, FailureGroup] = {}
    for row in rows:
        error = str(row["last_error"] or "")
        pattern = _truncate_error(error, 80)
        key = f"{row['platform']}|{pattern}"

        if key not in groups:
            groups[key] = FailureGroup(
                error_pattern=pattern,
                count=0,
                last_seen=str(row["finished_at"] or ""),
                platform=str(row["platform"] or ""),
                category=_categorize_error(error),
            )

        groups[key].count += 1
        # Keep the most recent finished_at.
        finished = str(row["finished_at"] or "")
        if finished > groups[key].last_seen:
            groups[key].last_seen = finished

    # Sort by last_seen descending, take top 20.
    return sorted(groups.values(), key=lambda g: g.last_seen, reverse=True)[:20]


def _truncate_error(error: str, max_len: int) -> str:
    """Truncate an error message to a pattern."""
    text = error.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0] + "…"


def _categorize_error(error: str) -> str:
    """Quick categorization for display."""
    from myUtils.failure_classification import classify_failure, TRANSIENT, PERMANENT, RECOVERABLE

    class _FakeExc(Exception):
        def __init__(self, msg):
            super().__init__(msg)

    classified = classify_failure(_FakeExc(error))
    return classified.category.value


def _stale_targets(
    conn: sqlite3.Connection,
    now: datetime,
    threshold_minutes: int,
) -> list[StaleTarget]:
    """Find targets stuck in 'running' state."""
    cutoff = (now - timedelta(minutes=threshold_minutes)).isoformat(timespec="seconds")
    rows = conn.execute("""
        SELECT t.id, t.account_ref, j.platform, t.started_at
        FROM publish_job_targets t
        JOIN publish_jobs j ON j.id = t.job_id
        WHERE t.status = 'running'
          AND t.started_at < ?
    """, (cutoff,)).fetchall()

    stale = []
    for row in rows:
        started = str(row["started_at"] or "")
        try:
            started_dt = datetime.fromisoformat(started)
            stuck = int((now - started_dt).total_seconds() / 60)
        except (ValueError, TypeError):
            stuck = 0

        stale.append(StaleTarget(
            target_id=int(row["id"]),
            account_ref=str(row["account_ref"] or ""),
            platform=str(row["platform"] or ""),
            started_at=started,
            stuck_minutes=stuck,
        ))

    return stale


def _accounts_needing_reconnect(conn: sqlite3.Connection) -> list[dict]:
    """Find accounts flagged for manual reconnect."""
    rows = conn.execute("""
        SELECT id, platform, nickname, account_name, config_json
        FROM accounts
        WHERE json_extract(config_json, '$._needsReconnect') = 1
          AND status > 0
    """).fetchall()

    accounts = []
    for row in rows:
        accounts.append({
            "id": int(row["id"]),
            "platform": str(row["platform"] or ""),
            "nickname": str(row["nickname"] or ""),
            "accountName": str(row["account_name"] or ""),
        })

    return accounts
