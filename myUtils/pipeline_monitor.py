"""Alert the operator when the publishing pipeline breaks, instead of waiting
for them to notice nothing was posted.

Runs from cron every 15 minutes and checks the things that have actually failed
silently on this box:

  * the app container / ``/healthz`` (deploys, OOM, bad image),
  * the offload-to-Drive run (a non-zero rc means media can no longer move out
    of the VPS, which eventually fills the disk),
  * publish targets that failed recently (grouped by error, so "Reddit is
    disabled" is one line, not fifty),
  * campaigns stuck in ``needs_review`` (prep failed — nothing will publish),
  * targets whose ``schedule_at`` passed more than ``--overdue-min`` ago and are
    still ``pending`` (the scheduler thread is not draining),
  * the inbox watcher heartbeat (delegated to ``inbox_monitor.py`` logic).

Every alert is deduplicated per signature for ``--cooldown`` seconds and sent
through :func:`myUtils.ops_alerts.send_ops_alert` (Telegram / webhook / SMTP).

    python -m myUtils.pipeline_monitor            # check + alert
    python -m myUtils.pipeline_monitor --status   # print, never alert
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DB = Path(os.environ.get("SAU_DB_PATH") or (REPO / "db" / "database.db"))
STATE_PATH = REPO / "logs" / "pipeline-monitor-state.json"
HEALTH_URL = os.environ.get("SAU_HEALTH_URL", "http://127.0.0.1:5409/healthz")


def _load_dotenv() -> None:
    """Pick up SAU_ALERT_* from the repo .env when run outside the container.

    Cron runs this with the host interpreter, where ``env_file`` (docker
    compose) has not been applied, so without this every alert would silently
    no-op for "Telegram not configured".
    """
    env_file = REPO / ".env"
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("SAU_ALERT_"):
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def _now() -> datetime:
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(f"{STATE_PATH.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def _check_healthz(timeout: float = 10.0) -> str | None:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as resp:
            if resp.status != 200:
                return f"HTTP {resp.status} from {HEALTH_URL}"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    return None


def _check_failed_targets(conn: sqlite3.Connection, minutes: int) -> list[str]:
    cutoff = _iso(_now() - timedelta(minutes=minutes))
    rows = conn.execute(
        """
        SELECT COALESCE(substr(last_error, 1, 90), 'unknown') AS err, COUNT(*) AS n,
               MAX(finished_at) AS last
        FROM publish_job_targets
        WHERE status = 'failed' AND finished_at >= ?
        GROUP BY err ORDER BY n DESC LIMIT 6
        """,
        (cutoff,),
    ).fetchall()
    # "disabled by the operator" is a deliberate setting, not a fault.
    interesting = [r for r in rows if "disabled by the operator" not in str(r["err"])]
    return [
        f"{r['n']}× {r['err']} (last {r['last']})" for r in interesting
    ]


def _check_needs_review(conn: sqlite3.Connection, hours: int) -> list[str]:
    cutoff = _iso(_now() - timedelta(hours=hours))
    rows = conn.execute(
        """
        SELECT id, profile_id, COALESCE(substr(last_error, 1, 90), '') AS err,
               COALESCE(prepared_at, created_at) AS at
        FROM campaigns
        WHERE status = 'needs_review' AND COALESCE(prepared_at, created_at) >= ?
        ORDER BY id DESC LIMIT 8
        """,
        (cutoff,),
    ).fetchall()
    return [f"campaign {r['id']} (profile {r['profile_id']}): {r['err']}" for r in rows]


def _check_overdue(conn: sqlite3.Connection, overdue_min: int) -> list[str]:
    cutoff = _iso(_now() - timedelta(minutes=overdue_min))
    rows = conn.execute(
        """
        SELECT account_ref, COUNT(*) AS n, MIN(schedule_at) AS oldest
        FROM publish_job_targets
        WHERE status = 'pending' AND schedule_at IS NOT NULL AND schedule_at != ''
          AND replace(schedule_at, 'T', ' ') <= replace(?, 'T', ' ')
        GROUP BY account_ref ORDER BY n DESC LIMIT 5
        """,
        (cutoff,),
    ).fetchall()
    return [f"{r['n']} overdue on {r['account_ref']} (oldest {r['oldest']})" for r in rows]


def _check_offload(repo: Path) -> list[str]:
    log = repo / "logs" / "offload.log"
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [f"offload log missing: {log}"]
    for line in reversed(lines):
        if "offload done" in line:
            if "rc=0" in line:
                return []
            return [f"last offload run failed: {line[-160:]}"]
    return ["no completed offload run found in offload.log"]


def _check_inbox_health(inbox: Path, stale_min: float) -> list[str]:
    health_path = inbox / ".watch-health.json"
    try:
        health = json.loads(health_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [f"inbox watcher never reported health ({health_path} missing)"]
    last = float(health.get("last_success_epoch") or 0)
    if not last:
        return ["inbox watcher has no successful scan recorded"]
    age_min = (time.time() - last) / 60
    if age_min > stale_min:
        return [f"inbox watcher last succeeded {age_min:.0f} min ago"]
    return []


def collect_problems(db: Path, repo: Path, inbox: Path, *, overdue_min: int = 45,
                     failed_minutes: int = 120, review_hours: int = 24,
                     watch_stale_min: float = 20) -> dict[str, list[str]]:
    problems: dict[str, list[str]] = {}
    health = _check_healthz()
    if health:
        problems["app"] = [health]
    if not db.exists():
        problems["database"] = [f"missing database {db}"]
        return problems
    conn = _connect(db)
    try:
        failed = _check_failed_targets(conn, failed_minutes)
        if failed:
            problems["publish-failures"] = failed
        review = _check_needs_review(conn, review_hours)
        if review:
            problems["needs-review"] = review
        overdue = _check_overdue(conn, overdue_min)
        if overdue:
            problems["overdue"] = overdue
    finally:
        conn.close()
    offload = _check_offload(repo)
    if offload:
        problems["offload"] = offload
    inbox_problems = _check_inbox_health(inbox, watch_stale_min)
    if inbox_problems:
        problems["inbox-watcher"] = inbox_problems
    return problems


def _render(problems: dict[str, list[str]]) -> tuple[str, str]:
    subject = f"🚨 SAU pipeline: {len(problems)} area(s) need attention"
    lines = []
    for area, items in problems.items():
        lines.append(f"• {area}")
        lines.extend(f"    {item}" for item in items)
    lines.append("")
    lines.append("Logs: /home/will/social-auto-upload/logs/{worker,offload,ci-watch}.log")
    lines.append("/home/will/sau-inbox/watch.log · db/database.db")
    return subject, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    ap = argparse.ArgumentParser(description="SAU pipeline monitor")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--repo", type=Path, default=REPO)
    ap.add_argument("--inbox", type=Path, default=Path("/home/will/sau-inbox"))
    ap.add_argument("--overdue-min", type=int, default=45)
    ap.add_argument("--failed-minutes", type=int, default=120)
    ap.add_argument("--review-hours", type=int, default=24)
    ap.add_argument("--watch-stale-min", type=float, default=20)
    ap.add_argument("--cooldown", type=int, default=3600)
    ap.add_argument("--status", action="store_true", help="print, never alert")
    args = ap.parse_args(argv)

    problems = collect_problems(
        args.db, args.repo, args.inbox,
        overdue_min=args.overdue_min,
        failed_minutes=args.failed_minutes,
        review_hours=args.review_hours,
        watch_stale_min=args.watch_stale_min,
    )
    payload = {area: items for area, items in problems.items()}
    print(json.dumps(payload, ensure_ascii=False, indent=2) if payload else "healthy")
    if args.status or not problems:
        return 0 if not problems else 1

    state = _load_state()
    alerts = dict(state.get("alerts") or {})
    now = time.time()
    sent: list[str] = []
    for area, items in problems.items():
        signature = f"{area}:{'|'.join(sorted(items))[:200]}"
        last = float(alerts.get(signature) or 0)
        if now - last < args.cooldown:
            continue
        subject, body = _render({area: items})
        try:
            from myUtils import ops_alerts

            if ops_alerts.send_ops_alert(subject=subject, body=body):
                alerts[signature] = now
                sent.append(area)
        except Exception as exc:  # noqa: BLE001
            print(f"alert failed for {area}: {exc}", file=sys.stderr)
    state["alerts"] = {k: v for k, v in alerts.items() if now - float(v or 0) < 7 * 86400}
    state["checked_at"] = _iso(_now())
    _save_state(state)
    print(f"alerted: {sent or 'none (cooldown or send failure)'}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
