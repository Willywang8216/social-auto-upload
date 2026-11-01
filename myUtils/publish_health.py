"""Read-only publishing health, with delivery and queue eligibility kept distinct.

Existing status counts/successRate fields are lifetime target statistics. The
``recent`` objects explicitly describe the requested window; ``delivered`` is
confirmed delivery, while ``legacySucceeded`` has no durable delivery evidence.
Queue age starts when work became due, never when a future post was enqueued.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from myUtils import jobs
from myUtils.secret_redaction import redact_text

_STATES = ("succeeded", "failed", "pending", "running", "retrying", "cancelled",
           "blocked", "awaiting_delivery", "needs_review")
_DETAIL_LIMIT = 100


def safe_text(value, max_len: int = 160) -> str:
    """Redact *before* shortening. Health evidence never needs credential URLs."""
    text = redact_text(value).strip()
    text = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", "[redacted-url]", text)
    text = re.sub(r"(?i)\bBasic\s+[A-Za-z0-9+/=]+", "Basic [REDACTED]", text)
    text = re.sub(
        r"(?is)\b(?:config(?:_json)?|checkpoint(?:_json)?|headers|cookies?|credentials|storage_state)"
        r"[\"']?\s*[:=].*", "[redacted context]", text,
    )
    text = re.sub(
        r"(?i)\b(?:token|secret|authorization|proxy-authorization|sessionid|lease[_-]?token)"
        r"[\"']?\s*[:=]\s*[\"']?[^\s&,;\"')\]}]+", "[redacted credential]", text,
    )
    return text if len(text) <= max_len else text[:max(0, max_len - 1)] + "…"


def _account_ref(value) -> str:
    ref = str(value or "")
    return ref if re.fullmatch(r"account:\d+", ref) else "legacy account"


def _date(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    except (ValueError, TypeError, OverflowError):
        return None


def _stamp(value) -> str | None:
    parsed = _date(value)
    return parsed.isoformat(timespec="seconds") if parsed else None


def _rate(succeeded: int, failed: int) -> float:
    return round(succeeded / (succeeded + failed), 3) if succeeded + failed else 0.0


@dataclass(slots=True)
class PlatformHealth:
    platform: str
    succeeded: int = 0
    failed: int = 0
    pending: int = 0
    running: int = 0
    retrying: int = 0
    cancelled: int = 0
    blocked: int = 0
    awaiting_delivery: int = 0
    needs_review: int = 0
    unknown: int = 0
    delivered: int = 0
    legacy_succeeded: int = 0
    accepted_pending: int = 0
    recent: dict = field(default_factory=dict)
    queue: dict = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(getattr(self, status) for status in _STATES) + self.unknown

    @property
    def success_rate(self) -> float:
        return self.succeeded / (self.succeeded + self.failed) if self.succeeded + self.failed else 0.0

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            **{status: getattr(self, status) for status in _STATES},
            "unknown": self.unknown, "total": self.total,
            "successRate": round(self.success_rate, 3),
            "lifetimeSuccessRate": round(self.success_rate, 3),
            "delivered": self.delivered, "legacySucceeded": self.legacy_succeeded,
            "acceptedPending": self.accepted_pending,
            "recent": self.recent, "queue": self.queue,
        }


@dataclass(slots=True)
class FailureGroup:
    error_pattern: str
    count: int
    last_seen: str
    platform: str = ""
    category: str = ""

    def to_dict(self) -> dict:
        return {"errorPattern": self.error_pattern, "count": self.count,
                "lastSeen": self.last_seen, "platform": self.platform, "category": self.category}


@dataclass(slots=True)
class StaleTarget:
    target_id: int
    account_ref: str
    platform: str
    started_at: str
    stuck_minutes: int
    reason: str = "legacy_stale_execution"
    lease_expires_at: str | None = None
    delivery_state: str | None = None

    def to_dict(self) -> dict:
        return {"targetId": self.target_id, "accountRef": self.account_ref,
                "platform": self.platform, "startedAt": self.started_at,
                "stuckMinutes": self.stuck_minutes, "reason": self.reason,
                "leaseExpiresAt": self.lease_expires_at, "deliveryState": self.delivery_state}


@dataclass(slots=True)
class HealthSnapshot:
    timestamp: str
    platforms: list[PlatformHealth] = field(default_factory=list)
    recent_failures: list[FailureGroup] = field(default_factory=list)
    stale_targets: list[StaleTarget] = field(default_factory=list)
    accounts_needing_reconnect: list[dict] = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    revision: str = "unknown"
    queue: dict = field(default_factory=dict)
    recent: dict = field(default_factory=dict)
    worker_health: dict = field(default_factory=dict)
    action_targets: list[dict] = field(default_factory=list)
    account_config_issues: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp, "revision": self.revision,
            "platforms": [p.to_dict() for p in self.platforms],
            "recentFailures": [f.to_dict() for f in self.recent_failures],
            "staleTargets": [s.to_dict() for s in self.stale_targets],
            "accountsNeedingReconnect": self.accounts_needing_reconnect,
            "totals": self.totals, "queue": self.queue, "recent": self.recent,
            "workerHealth": self.worker_health, "actionTargets": self.action_targets,
            "accountConfigIssues": self.account_config_issues,
            "detailLimit": _DETAIL_LIMIT, "statisticsScope": "lifetime",
        }


# The predicates mirror jobs._claimable_clause. Receipt polls bypass the post's
# original schedule/preparation; submissions respect BOTH schedule and backoff.
# Ownership comes from the parent job, including older targets with NULL tenant
# columns. Account labels are joined only inside the caller's workspace.
_TARGET_CTE = """
WITH targets AS (
    SELECT t.id, t.job_id, t.account_ref, t.status, t.schedule_at,
           t.next_attempt_at, t.started_at, t.finished_at, t.last_error,
           t.error_category, t.delivery_state, t.lease_expires_at,
           j.platform, j.status AS parent_status, j.created_at,
           a.nickname, a.account_name, c.status AS campaign_status,
           (t.lease_token IS NOT NULL AND t.lease_token != '') AS has_lease,
           (t.lease_owner IS NOT NULL AND t.lease_owner != '') AS has_owner,
           (t.lease_owner IS NOT NULL AND t.lease_owner != '' AND
             (h.worker_id IS NULL OR julianday(h.heartbeat_at) IS NULL OR
              julianday(h.heartbeat_at) < julianday(:worker_cutoff) OR
              julianday(h.heartbeat_at) > julianday(:now))) AS missing_worker
    FROM publish_job_targets t JOIN publish_jobs j ON j.id = t.job_id
    LEFT JOIN accounts a ON a.id = CAST(substr(t.account_ref, 9) AS INTEGER)
         AND t.account_ref = ('account:' || a.id)
         AND (:workspace IS NULL OR a.workspace_id = :workspace)
    LEFT JOIN campaign_posts cp ON cp.id = CAST(substr(t.file_ref, 15) AS INTEGER)
         AND t.file_ref = ('campaign_post:' || cp.id)
    LEFT JOIN campaigns c ON c.id = cp.campaign_id
    LEFT JOIN worker_heartbeats h ON h.worker_id = t.lease_owner
    WHERE (:workspace IS NULL OR j.workspace_id = :workspace)
), timing AS (
    SELECT *,
        status IN ('pending', 'retrying') AS submission,
        parent_status IN ('pending', 'running', 'awaiting_delivery') AS parent_ready,
        (campaign_status IS NULL OR campaign_status NOT IN ('preparing', 'needs_review', 'draft')) AS media_ready,
        (schedule_at IS NULL OR schedule_at = '' OR julianday(schedule_at) <= julianday(:now)) AS schedule_due,
        (next_attempt_at IS NULL OR julianday(next_attempt_at) <= julianday(:now)) AS retry_due,
        MAX(COALESCE(julianday(created_at), 0), COALESCE(julianday(schedule_at), 0),
            COALESCE(julianday(next_attempt_at), 0)) AS due_jd,
        MAX(COALESCE(julianday(created_at), 0), COALESCE(julianday(started_at), 0),
            COALESCE(julianday(next_attempt_at), 0)) AS poll_jd,
        (status = 'running' AND julianday(lease_expires_at) <= julianday(:now)) AS expired,
        (status = 'running' AND (missing_worker OR
            (lease_expires_at IS NOT NULL AND (NOT has_lease OR julianday(lease_expires_at) IS NULL)) OR
            (lease_expires_at IS NULL AND (has_lease OR has_owner)))) AS orphaned,
        (status = 'running' AND lease_expires_at IS NULL AND
            (julianday(started_at) IS NULL OR julianday(started_at) < julianday(:stale_cutoff))) AS legacy_stale
    FROM targets
), flags AS (
    SELECT *,
        (submission AND parent_ready AND media_ready AND schedule_due AND retry_due) AS due,
        (status = 'awaiting_delivery' AND parent_ready AND retry_due) AS poll_due,
        (submission AND julianday(schedule_at) > julianday(:now)) AS scheduled,
        (submission AND julianday(next_attempt_at) > julianday(:now)) AS retry_delayed,
        (status = 'awaiting_delivery' AND julianday(next_attempt_at) > julianday(:now)) AS poll_delayed,
        (status IN ('pending', 'retrying', 'awaiting_delivery') AND NOT parent_ready) AS held_by_parent,
        (submission AND NOT media_ready) AS waiting_for_preparation,
        (status IN ('pending', 'retrying', 'awaiting_delivery') AND (
            (next_attempt_at IS NOT NULL AND julianday(next_attempt_at) IS NULL) OR
            (submission AND schedule_at IS NOT NULL AND schedule_at != '' AND julianday(schedule_at) IS NULL))) AS invalid_schedule,
        (COALESCE(expired, 0) OR COALESCE(orphaned, 0) OR legacy_stale) AS stale_running,
        (status = 'awaiting_delivery' OR
            (status NOT IN ('succeeded', 'failed', 'cancelled') AND delivery_state = 'awaiting_delivery')) AS accepted_pending,
        (status = 'succeeded' AND delivery_state = 'delivered') AS delivered,
        (status = 'succeeded' AND (delivery_state IS NULL OR delivery_state = '')) AS legacy_succeeded,
        (julianday(finished_at) >= julianday(:since) AND julianday(finished_at) <= julianday(:now)) AS recent
    FROM timing
)
"""

_METRICS = {
    "delivered": "delivered", "legacy_succeeded": "legacy_succeeded",
    "accepted_pending": "accepted_pending", "due": "due", "scheduled": "scheduled",
    "retry_delayed": "retry_delayed", "poll_due": "poll_due", "poll_delayed": "poll_delayed",
    "held_by_parent": "held_by_parent", "waiting_for_preparation": "waiting_for_preparation",
    "invalid_schedule": "invalid_schedule", "expired": "expired", "orphaned": "orphaned",
    "stale_running": "stale_running",
    "overdue": "due AND due_jd <= julianday(:stale_cutoff)",
    "overdue_polls": "poll_due AND poll_jd <= julianday(:stale_cutoff)",
    "action_required": "status IN ('blocked', 'needs_review') OR invalid_schedule OR stale_running",
    "recent_succeeded": "recent AND status = 'succeeded'",
    "recent_failed": "recent AND status = 'failed'", "recent_delivered": "recent AND delivered",
    "recent_legacy_succeeded": "recent AND legacy_succeeded",
}


def _aggregate(conn, params) -> list[dict]:
    fields = {**{s: f"status = '{s}'" for s in _STATES}, **_METRICS}
    sums = ", ".join(f"SUM(CASE WHEN {expr} THEN 1 ELSE 0 END) AS {name}"
                     for name, expr in fields.items())
    return [dict(row) for row in conn.execute(
        _TARGET_CTE + f"SELECT platform, COUNT(*) AS total, {sums}, "
        "MIN(CASE WHEN due AND due_jd > 0 THEN due_jd END) AS oldest_due, "
        "MIN(CASE WHEN poll_due AND poll_jd > 0 THEN poll_jd END) AS oldest_poll "
        "FROM flags GROUP BY platform", params,
    )]


def _due_age(julian, now: datetime) -> tuple[str | None, int | None]:
    if julian is None:
        return None, None
    dt = datetime.fromtimestamp((float(julian) - 2440587.5) * 86400, tz=timezone.utc).replace(tzinfo=None)
    # SQLite Julian-day floating-point rounding must not turn exact hours into
    # 3599 seconds or the preceding second in operator-facing timestamps.
    dt += timedelta(microseconds=500000)
    dt = dt.replace(microsecond=0)
    return dt.isoformat(timespec="seconds"), max(0, round((now - dt).total_seconds()))


def _queue(row, now) -> dict:
    stamp, age = _due_age(row.get("oldest_due"), now)
    poll_stamp, poll_age = _due_age(row.get("oldest_poll"), now)
    mapping = {
        "due": "due", "scheduled": "scheduled", "retryDelayed": "retry_delayed",
        "deliveryPollsDue": "poll_due", "deliveryPollsDelayed": "poll_delayed",
        "awaitingDelivery": "awaiting_delivery", "acceptedPending": "accepted_pending",
        "blocked": "blocked", "needsReview": "needs_review",
        "heldByParent": "held_by_parent", "waitingForPreparation": "waiting_for_preparation",
        "invalidSchedule": "invalid_schedule", "expiredLeases": "expired", "orphanedLeases": "orphaned",
        "staleRunning": "stale_running", "overdue": "overdue", "overdueDeliveryPolls": "overdue_polls",
        "actionRequired": "action_required",
    }
    return {**{key: int(row.get(column) or 0) for key, column in mapping.items()},
            "oldestDueAt": stamp, "oldestDueAgeSeconds": age,
            "oldestDeliveryPollDueAt": poll_stamp, "oldestDeliveryPollAgeSeconds": poll_age}


def _recent(row, params, hours) -> dict:
    succeeded, failed = int(row.get("recent_succeeded") or 0), int(row.get("recent_failed") or 0)
    return {"windowHours": hours, "since": params["since"], "until": params["now"],
            "succeeded": succeeded, "failed": failed, "successRate": _rate(succeeded, failed),
            "delivered": int(row.get("recent_delivered") or 0),
            "legacySucceeded": int(row.get("recent_legacy_succeeded") or 0)}


def _truncate_error(error: str, max_len: int) -> str:
    return safe_text(error, max_len)


def _categorize_error(error: str) -> str:
    from myUtils.failure_classification import classify_failure
    return classify_failure(Exception(error)).category.value


def _recent_failures(conn, params) -> list[FailureGroup]:
    # Redact full text before grouping/truncating; SQL LIMIT applies to groups,
    # never to the failure population used for counts or rates.
    conn.create_function("health_error", 1, lambda value: safe_text(value, 80))
    rows = conn.execute(
        _TARGET_CTE + "SELECT platform, health_error(last_error) AS pattern, "
        "COUNT(*) AS n, MAX(julianday(finished_at)) AS last_jd, "
        "health_error(error_category) AS category FROM flags "
        "WHERE status = 'failed' AND recent GROUP BY platform, pattern, category "
        "ORDER BY last_jd DESC, n DESC LIMIT 20", params,
    )
    result = []
    for row in rows:
        last, _ = _due_age(row["last_jd"], _date(params["now"]))
        pattern = row["pattern"] or "unknown failure"
        result.append(FailureGroup(pattern, int(row["n"]), last or "",
                                   safe_text(row["platform"], 80), row["category"] or _categorize_error(pattern)))
    return result


def _stale_targets(conn, params, now) -> list[StaleTarget]:
    rows = conn.execute(_TARGET_CTE + "SELECT * FROM flags WHERE stale_running "
                        "ORDER BY COALESCE(julianday(lease_expires_at), julianday(started_at), 0), id LIMIT :limit", params)
    result = []
    for row in rows:
        started = _date(row["started_at"])
        reason = "expired_lease" if row["expired"] else "orphaned_lease" if row["orphaned"] else "legacy_stale_execution"
        result.append(StaleTarget(
            int(row["id"]), _account_ref(row["account_ref"]), safe_text(row["platform"], 80),
            _stamp(row["started_at"]) or "", max(0, int((now - started).total_seconds() / 60)) if started else 0,
            reason, _stamp(row["lease_expires_at"]), safe_text(row["delivery_state"], 40) or None,
        ))
    return result


def _action_targets(conn, params) -> list[dict]:
    return [{"targetId": int(row["id"]), "jobId": int(row["job_id"]),
             "platform": safe_text(row["platform"], 80), "accountRef": _account_ref(row["account_ref"]),
             "accountName": safe_text(row["nickname"] or row["account_name"], 80),
             "status": safe_text(row["status"], 40), "errorCategory": safe_text(row["error_category"], 80),
             "reason": "invalid_schedule" if row["invalid_schedule"] else safe_text(row["last_error"], 160),
             "nextAttemptAt": _stamp(row["next_attempt_at"])}
            for row in conn.execute(_TARGET_CTE + "SELECT * FROM flags "
                                    "WHERE status IN ('blocked', 'needs_review') OR invalid_schedule "
                                    "ORDER BY id LIMIT :limit", params)]


def _account_health(conn, workspace_id):
    reconnect, issues = [], []
    for row in conn.execute(
        "SELECT id, platform, nickname, account_name, config_json FROM accounts "
        "WHERE (:workspace IS NULL OR workspace_id = :workspace) ORDER BY id", {"workspace": workspace_id},
    ):
        summary = {"id": int(row["id"]), "platform": safe_text(row["platform"], 80),
                   "nickname": safe_text(row["nickname"], 80), "accountName": safe_text(row["account_name"], 80)}
        try:
            config = json.loads(row["config_json"] or "{}")
            if not isinstance(config, dict):
                raise ValueError("expected object")
        except (ValueError, TypeError, RecursionError):
            issues.append({**summary, "reason": "invalid_account_config"})
            continue
        if config.get("_needsReconnect") in (True, 1, "true", "1"):
            reconnect.append(summary)
    return reconnect, issues


def _worker_snapshot(db_path, max_age_seconds) -> dict:
    try:
        health = jobs.worker_health(db_path=db_path, max_age_seconds=max_age_seconds)
    except Exception:  # noqa: BLE001 — expose an unavailable probe, never exception/config text
        return {"ok": False, "status": "unavailable", "workers": [], "maxAgeSeconds": max_age_seconds}
    workers = [{"worker_id": safe_text(w.get("worker_id"), 100),
                "heartbeat_at": _stamp(w.get("heartbeat_at")), "role": safe_text(w.get("role"), 40),
                "draining": bool(w.get("draining")), "alive": bool(w.get("alive")),
                "age_seconds": w.get("age_seconds")} for w in health.get("workers", [])]
    return {"ok": bool(health.get("ok")), "status": "ok" if health.get("ok") else "worker_unavailable",
            "workers": workers, "maxAgeSeconds": max_age_seconds,
            "freshPublishers": sum(w["alive"] and not w["draining"] and w["role"] == "publisher" for w in workers),
            "staleWorkers": sum(not w["alive"] for w in workers)}


def publish_health_snapshot(
    db_path: Path, *, failure_window_hours: float = 24,
    stale_threshold_minutes: float = 30, workspace_id: str | None = None,
    worker_max_age_seconds: float = 90,
) -> HealthSnapshot:
    """Read a migrated DB without creating it; no provider calls or repairs.

    Target ownership is the parent job's workspace; account details are scoped
    separately. Worker presence is deployment-wide (explicitly marked), not a
    tenant queue. At most 100 actionable/stale/account detail records are sent;
    aggregate counts are never sampled. Existing success-rate aliases remain
    lifetime statistics for UI compatibility, with recent metrics separate.
    """
    for value in (failure_window_hours, stale_threshold_minutes, worker_max_age_seconds):
        if not math.isfinite(float(value)) or float(value) < 0:
            raise ValueError("Health windows must be finite and non-negative")
    if workspace_id is not None and (not isinstance(workspace_id, str) or not workspace_id.strip()):
        raise ValueError("workspace_id must be a non-empty string or None")
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError("Publish database is not initialized")
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    snapshot = HealthSnapshot(timestamp=now.isoformat(), revision=safe_text(os.environ.get("SAU_BUILD_REVISION") or "unknown", 128))
    params = {"now": snapshot.timestamp, "since": (now - timedelta(hours=failure_window_hours)).isoformat(),
              "stale_cutoff": (now - timedelta(minutes=stale_threshold_minutes)).isoformat(),
              "worker_cutoff": (now - timedelta(seconds=worker_max_age_seconds)).isoformat(),
              "workspace": workspace_id, "limit": _DETAIL_LIMIT}
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")  # Consistent read snapshot; never takes a writer lock.
        rows = _aggregate(conn, params)
        total = {key: sum(int(row.get(key) or 0) for row in rows) for key in (*_STATES, *_METRICS, "total")}
        for key in ("oldest_due", "oldest_poll"):
            total[key] = min((r[key] for r in rows if r[key] is not None), default=None)
        for row in rows:
            snapshot.platforms.append(PlatformHealth(
                safe_text(row["platform"] or "unknown", 80), **{s: int(row[s]) for s in _STATES},
                unknown=int(row["total"]) - sum(int(row[s]) for s in _STATES),
                delivered=int(row["delivered"]), legacy_succeeded=int(row["legacy_succeeded"]),
                accepted_pending=int(row["accepted_pending"]), recent=_recent(row, params, failure_window_hours),
                queue=_queue(row, now),
            ))
        snapshot.platforms.sort(key=lambda p: (-p.total, p.platform))
        snapshot.totals = {s: total[s] for s in _STATES}
        snapshot.totals.update(total=total["total"], unknown=total["total"] - sum(total[s] for s in _STATES),
                               overallSuccessRate=_rate(total["succeeded"], total["failed"]),
                               lifetimeSuccessRate=_rate(total["succeeded"], total["failed"]),
                               delivered=total["delivered"], legacySucceeded=total["legacy_succeeded"],
                               acceptedPending=total["accepted_pending"])
        snapshot.queue, snapshot.recent = _queue(total, now), _recent(total, params, failure_window_hours)
        snapshot.recent_failures = _recent_failures(conn, params)
        snapshot.stale_targets = _stale_targets(conn, params, now)
        snapshot.action_targets = _action_targets(conn, params)
        reconnect, issues = _account_health(conn, workspace_id)
        snapshot.accounts_needing_reconnect, snapshot.account_config_issues = reconnect[:_DETAIL_LIMIT], issues[:_DETAIL_LIMIT]
        snapshot.totals.update(accountsNeedingReconnect=len(reconnect), accountConfigIssues=len(issues))
    finally:
        conn.close()
    snapshot.worker_health = {**_worker_snapshot(path, worker_max_age_seconds), "scope": "deployment"}
    return snapshot
