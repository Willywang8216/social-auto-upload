"""Daily publish-schedule digest.

Once a day an operator wants a single short summary of everything the publish
queue still has scheduled for *today* — not a sprawling dashboard, just the
destinations, accounts, status and times, with a link to act on. This module
builds that summary from the ``publish_job_targets`` / ``publish_jobs`` tables
and delivers it through :func:`myUtils.ops_alerts.send_ops_alert`, so it lands
on whichever channel (Telegram / webhook / SMTP) the operator already
configured for alerts.

Design notes
------------
* **Asia/Shanghai local day.** ``schedule_at`` is stored as a tz-naive UTC ISO
  string (see ``jobs._now_iso`` / ``publish_orchestrator``), but operators think
  in their own wall clock. The digest window is the Asia/Shanghai calendar day,
  so it contains everything from 00:00 to 23:59 in UTC+8. The offset is fixed
  (China has had no DST since 1991), which keeps the window deterministic and
  free of a system tzdata dependency.
* **Pending/retrying only.** Succeeded, failed and cancelled targets are
  history, not schedule; they are excluded.
* **Grouped by campaign media group.** A campaign fans out into one job per
  platform/post, all sharing a ``media_group_id``. When a target's payload
  carries a ``campaignId`` we resolve it to the campaign's media group and fold
  every target of that group into a single entry. Legacy jobs without a
  campaign id are grouped per job.
* **Links only from ``SAU_PUBLIC_APP_URL``.** A guessed hostname reads as a
  working link and silently sends the operator nowhere, so when no base URL is
  configured the digest simply omits the link.
* **At most one send per local day.** A small ``publish_digest_log`` table with
  ``UNIQUE(local_date)`` plus a ``BEGIN IMMEDIATE`` reservation transaction makes
  concurrent invocations (cron overlap, a manual run next to the scheduled one)
  safe: exactly one caller wins the reservation and sends. The reservation is
  only marked *sent* after the sender reports success; a failure or a false
  return releases it so the next run can retry.

There is intentionally no built-in scheduler: run ``python -m
myUtils.publish_digest`` from whatever timer the operator already uses. Docs and
cron wiring are out of scope for this module.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from myUtils import jobs as job_runtime
from myUtils import ops_alerts

# Asia/Shanghai is a fixed UTC+8 offset (no DST since 1991). Using a fixed
# offset keeps the day window deterministic and avoids depending on the host
# tzdata database.
SHANGHAI_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

DIGEST_TABLE = "publish_digest_log"

# Reservation states for a local date.
STATE_SENDING = "sending"
STATE_SENT = "sent"
STATE_FAILED = "failed"

# Only these target states are still "scheduled".
SCHEDULED_TARGET_STATUSES = (job_runtime.TARGET_PENDING, job_runtime.TARGET_RETRYING)

# SPA route fragments (hash history). ``entity=media-group:<id>`` identifies the
# shared media of a campaign; ``job=<id>`` is the per-job fallback for legacy
# jobs that never had a campaign.
_MEDIA_GROUP_ROUTE = "#/publish/queue?entity=mg-{ident}"
_JOB_ROUTE = "#/publish/queue?job={ident}"

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {DIGEST_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    local_date TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT '{STATE_SENDING}',
    groups_count INTEGER NOT NULL DEFAULT 0,
    targets_count INTEGER NOT NULL DEFAULT 0,
    subject TEXT,
    body TEXT,
    last_error TEXT,
    reserved_at DATETIME,
    sent_at DATETIME
)
"""


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #


def _resolve_db_path(db_path: Path | str | None) -> Path:
    """Resolve the DB path lazily.

    Explicit argument wins, then ``SAU_DB_PATH`` (matching the CLI/migration
    convention), then the shared app default (``jobs.DB_PATH``, which tests can
    rebind via monkeypatch).
    """

    if db_path is not None:
        return Path(db_path)
    env = str(os.environ.get("SAU_DB_PATH", "") or "").strip()
    if env:
        return Path(env)
    return job_runtime.DB_PATH


@contextmanager
def _connect(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    resolved = _resolve_db_path(db_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None gives us explicit transaction control (BEGIN
    # IMMEDIATE / COMMIT) instead of sqlite3's implicit DML transactions.
    conn = sqlite3.connect(resolved, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_TABLE_SQL)


def _to_local_date(now: datetime | None) -> date:
    """Return the Asia/Shanghai calendar date for ``now``.

    ``now`` is interpreted as UTC when naive (the project's canonical shape);
    ``None`` means "right now".
    """

    if now is None:
        moment = datetime.now(tz=timezone.utc)
    elif now.tzinfo is None:
        moment = now.replace(tzinfo=timezone.utc)
    else:
        moment = now
    return moment.astimezone(SHANGHAI_TZ).date()


def _parse_schedule(value: object) -> datetime | None:
    """Parse a stored ``schedule_at`` into an aware UTC datetime.

    Accepts the canonical tz-naive UTC ISO shape plus offset-aware values
    (``Z`` / ``+08:00``) the legacy file-scheduling path produced. Non-ISO
    legacy values return ``None`` — they cannot be placed in a day window.
    """

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _app_base(app_url: str | None) -> str:
    """The configured public origin, or ``""`` — never an invented domain."""

    value = app_url if app_url is not None else os.environ.get("SAU_PUBLIC_APP_URL", "")
    return str(value or "").strip().rstrip("/")


def _deep_link(base: str, kind: str, ident: object) -> str | None:
    if not base:
        return None
    fragment = (
        _MEDIA_GROUP_ROUTE.format(ident=ident)
        if kind == "media-group"
        else _JOB_ROUTE.format(ident=ident)
    )
    return f"{base}/{fragment}"


# --------------------------------------------------------------------------- #
# digest construction
# --------------------------------------------------------------------------- #


def _load_campaigns(conn: sqlite3.Connection, campaign_ids: set[int]) -> dict[int, dict]:
    if not campaign_ids:
        return {}
    placeholders = ",".join("?" * len(campaign_ids))
    rows = conn.execute(
        f"""
        SELECT c.id AS id, c.media_group_id AS media_group_id,
               mg.name AS media_group_name
        FROM campaigns c
        LEFT JOIN media_groups mg ON mg.id = c.media_group_id
        WHERE c.id IN ({placeholders})
        """,
        sorted(campaign_ids),
    ).fetchall()
    return {
        int(row["id"]): {
            "media_group_id": row["media_group_id"],
            "media_group_name": row["media_group_name"],
        }
        for row in rows
    }


def _campaign_id_from_payload(payload_text: object) -> int | None:
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get("campaignId")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def build_daily_digest(
    local_date: date,
    *,
    db_path: Path | str | None = None,
    app_url: str | None = None,
) -> dict:
    """Build the digest subject/body and grouping summary for ``local_date``.

    Pure read: it never sends anything and never touches the reservation table,
    which makes it the natural preview path for the CLI's ``--dry-run``.
    """

    base = _app_base(app_url)

    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" * len(SCHEDULED_TARGET_STATUSES))
        rows = conn.execute(
            f"""
            SELECT t.id AS target_id, t.job_id AS job_id,
                   t.account_ref AS account_ref, t.file_ref AS file_ref,
                   t.schedule_at AS schedule_at, t.status AS status,
                   j.platform AS platform, j.payload_json AS payload_json
            FROM publish_job_targets t
            JOIN publish_jobs j ON j.id = t.job_id
            WHERE t.schedule_at IS NOT NULL AND t.schedule_at != ''
              AND t.status IN ({placeholders})
            ORDER BY t.schedule_at ASC, t.id ASC
            """,
            SCHEDULED_TARGET_STATUSES,
        ).fetchall()

        parsed_rows: list[dict] = []
        campaign_ids: set[int] = set()
        for row in rows:
            when = _parse_schedule(row["schedule_at"])
            if when is None:
                continue
            if when.astimezone(SHANGHAI_TZ).date() != local_date:
                continue
            campaign_id = _campaign_id_from_payload(row["payload_json"])
            if campaign_id is not None:
                campaign_ids.add(campaign_id)
            parsed_rows.append(
                {
                    "target_id": int(row["target_id"]),
                    "job_id": int(row["job_id"]),
                    "account_ref": str(row["account_ref"]),
                    "status": str(row["status"]),
                    "platform": str(row["platform"]),
                    "campaign_id": campaign_id,
                    "when_utc": when,
                    "when_local": when.astimezone(SHANGHAI_TZ),
                }
            )

        campaigns = _load_campaigns(conn, campaign_ids)

    groups: dict[tuple[str, object], dict] = {}
    for item in parsed_rows:
        kind = "job"
        ident: object = item["job_id"]
        label = f"job #{item['job_id']}"
        campaign_id = item["campaign_id"]
        if campaign_id is not None:
            campaign = campaigns.get(campaign_id)
            media_group_id = campaign["media_group_id"] if campaign else None
            if media_group_id is not None:
                kind = "media-group"
                ident = int(media_group_id)
                name = campaign.get("media_group_name") if campaign else None
                label = name or f"media group #{media_group_id}"

        key = (kind, ident)
        group = groups.get(key)
        if group is None:
            group = {
                "key": f"{kind}:{ident}",
                "kind": kind,
                "id": ident,
                "label": label,
                "url": _deep_link(base, kind, ident),
                "targets": [],
            }
            groups[key] = group
        group["targets"].append(item)

    ordered_groups = sorted(
        groups.values(), key=lambda g: (g["targets"][0]["when_utc"], g["key"])
    )

    platforms: set[str] = set()
    target_count = 0
    for group in ordered_groups:
        group["platforms"] = sorted({t["platform"] for t in group["targets"]})
        platforms.update(group["platforms"])
        target_count += len(group["targets"])

    group_count = len(ordered_groups)
    platform_list = sorted(platforms)

    subject, body = _render(local_date, ordered_groups, group_count, target_count, platform_list)

    return {
        "local_date": local_date.isoformat(),
        "group_count": group_count,
        "target_count": target_count,
        "platforms": platform_list,
        "groups": ordered_groups,
        "subject": subject,
        "body": body,
    }


def _render(
    local_date: date,
    groups: list[dict],
    group_count: int,
    target_count: int,
    platforms: list[str],
) -> tuple[str, str]:
    day = local_date.isoformat()
    if group_count == 0:
        subject = f"[SAU] Publish schedule {day} — nothing scheduled"
        body = (
            f"Daily publish schedule for {day} (Asia/Shanghai)\n\n"
            "No publishes scheduled for today."
        )
        return subject, body

    plural = "s" if group_count != 1 else ""
    subject = (
        f"[SAU] Publish schedule {day} — "
        f"{group_count} group{plural} · {target_count} target{'' if target_count == 1 else 's'}"
    )

    lines = [
        f"Daily publish schedule for {day} (Asia/Shanghai)",
        "",
        f"{group_count} group{plural} · {target_count} target"
        f"{'' if target_count == 1 else 's'} · {len(platforms)} platform"
        f"{'' if len(platforms) == 1 else 's'}",
    ]
    for group in groups:
        lines.append("")
        lines.append(
            f"[{group['key']}] {group['label']} ({', '.join(group['platforms'])})"
        )
        for target in group["targets"]:
            lines.append(
                f"  - {target['when_local'].strftime('%H:%M')} · {target['platform']}"
                f" · {target['account_ref']} · {target['status']}"
            )
        if group["url"]:
            lines.append(f"  link: {group['url']}")
    return subject, "\n".join(lines)


# --------------------------------------------------------------------------- #
# send + idempotence
# --------------------------------------------------------------------------- #


def _default_sender(*, subject: str, body: str) -> bool:
    return bool(ops_alerts.send_ops_alert(subject=subject, body=body))


def _reserve(conn: sqlite3.Connection, local_date: str, *, stale_after_seconds: int = 900) -> bool:
    """Claim today's send slot; reclaim a stale reservation left by a crash."""


    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            f"SELECT status FROM {DIGEST_TABLE} WHERE local_date = ?", (local_date,)
        ).fetchone()
        if row is not None:
            reserved_row = conn.execute(
                f"SELECT reserved_at FROM {DIGEST_TABLE} WHERE local_date = ?", (local_date,)
            ).fetchone()
            reserved_at = _parse_schedule(reserved_row["reserved_at"] if reserved_row else None)
            reservation_is_stale = (
                reserved_at is None
                or (datetime.now(timezone.utc) - reserved_at).total_seconds() > stale_after_seconds
            )
            if row["status"] == STATE_SENT or (row["status"] == STATE_SENDING and not reservation_is_stale):
                conn.execute("COMMIT")
                return False
            conn.execute(
                f"UPDATE {DIGEST_TABLE} SET status = ?, last_error = NULL, "
                f"reserved_at = ? WHERE local_date = ?",
                (STATE_SENDING, _now_iso(), local_date),
            )
        else:
            conn.execute(
                f"INSERT INTO {DIGEST_TABLE} (local_date, status, reserved_at) "
                f"VALUES (?, ?, ?)",
                (local_date, STATE_SENDING, _now_iso()),
            )
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _mark_sent(conn: sqlite3.Connection, local_date: str, digest: dict) -> None:
    conn.execute(
        f"UPDATE {DIGEST_TABLE} SET status = ?, sent_at = ?, groups_count = ?, "
        f"targets_count = ?, subject = ?, body = ?, last_error = NULL "
        f"WHERE local_date = ?",
        (
            STATE_SENT,
            _now_iso(),
            digest["group_count"],
            digest["target_count"],
            digest["subject"],
            digest["body"],
            local_date,
        ),
    )


def _release(conn: sqlite3.Connection, local_date: str, error: str) -> None:
    """Release the reservation so a later run can retry today's digest."""

    conn.execute(
        f"UPDATE {DIGEST_TABLE} SET status = ?, last_error = ? WHERE local_date = ?",
        (STATE_FAILED, error[:2000], local_date),
    )


def send_daily_digest(
    now: datetime | None = None,
    db_path: Path | str | None = None,
    sender: Callable[..., bool] | None = None,
    app_url: str | None = None,
) -> dict:
    """Send the Asia/Shanghai daily schedule digest exactly once per day.

    Args:
        now: Moment used to pick the local day (naive values are treated as
            UTC). Defaults to the current time.
        db_path: SQLite path; falls back to ``SAU_DB_PATH`` then ``jobs.DB_PATH``.
        sender: Callable ``sender(subject=..., body=...) -> truthy``. Defaults to
            :func:`myUtils.ops_alerts.send_ops_alert`. Injecting one keeps the
            function unit-testable without any alert channel configured.
        app_url: Overrides ``SAU_PUBLIC_APP_URL`` for building links. When both
            are empty the digest carries no links.

    Returns:
        A dict with ``status`` (``sent`` / ``skipped`` / ``failed``), ``reason``,
        ``local_date``, ``group_count``, ``target_count``, ``platforms`` and
        ``sent`` — deliberately plain so the CLI and operators can read it.
    """

    local_date = _to_local_date(now)
    day = local_date.isoformat()

    digest = build_daily_digest(local_date, db_path=db_path, app_url=app_url)

    result_base = {
        "local_date": day,
        "group_count": digest["group_count"],
        "target_count": digest["target_count"],
        "platforms": digest["platforms"],
    }

    send = sender or _default_sender

    with _connect(db_path) as conn:
        _ensure_table(conn)
        if not _reserve(conn, day):
            return {
                **result_base,
                "status": "skipped",
                "sent": False,
                "reason": "already sent or in progress for this local date",
            }

        try:
            delivered = bool(send(subject=digest["subject"], body=digest["body"]))
        except Exception as exc:  # noqa: BLE001 — release + report, never crash a timer
            _release(conn, day, f"sender raised: {exc!r}")
            return {
                **result_base,
                "status": "failed",
                "sent": False,
                "reason": f"sender raised: {exc!r}",
            }

        if not delivered:
            _release(conn, day, "sender returned false")
            return {
                **result_base,
                "status": "failed",
                "sent": False,
                "reason": "sender returned false",
            }

        _mark_sent(conn, day, digest)
        return {**result_base, "status": "sent", "sent": True, "reason": "delivered"}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m myUtils.publish_digest",
        description=(
            "Send (or preview) the daily Asia/Shanghai publish-schedule digest. "
            "Run it from your own timer; this module does not schedule itself."
        ),
    )
    parser.add_argument("--db-path", default=None, help="SQLite DB (default: SAU_DB_PATH or the app DB)")
    parser.add_argument("--app-url", default=None, help="Public app origin override (default: SAU_PUBLIC_APP_URL)")
    parser.add_argument("--dry-run", action="store_true", help="Build and print without sending")
    parser.add_argument("--date", default=None, help="Local digest date YYYY-MM-DD (dry-run preview)")
    args = parser.parse_args(argv)

    if args.dry_run:
        preview_date = date.fromisoformat(args.date) if args.date else _to_local_date(None)
        digest = build_daily_digest(
            preview_date, db_path=args.db_path, app_url=args.app_url
        )
        print(f"Subject: {digest['subject']}")
        print()
        print(digest["body"])
        return 0

    result = send_daily_digest(db_path=args.db_path, app_url=args.app_url)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in ("sent", "skipped") else 1


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess/manual run
    sys.exit(main())
