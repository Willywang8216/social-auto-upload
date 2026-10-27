#!/usr/bin/env python3
"""Watchdog for the SAU-Inbox watcher itself.

The 2026-10-17..20 outage happened because the watcher crashed every minute for
four days and only wrote a traceback to `watch.log`. A log file is not a
notification channel, so this small checker (cron, every 10 minutes) reads
`watch.py`'s health file and shouts on Telegram when:

  * the health file is missing (the watcher has never completed a scan), or
  * the last successful scan is older than ``--stale-min`` (cron died, the run
    lock is stuck, Drive is unreachable, …), or
  * the last scan failed and no newer success replaced it.

Alerts are deduplicated per signature for an hour so a permanent outage does not
produce 144 messages a day.

    python3 inbox_monitor.py                 # check once (cron)
    python3 inbox_monitor.py --stale-min 20
    python3 inbox_monitor.py --status        # print, never alert
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HEALTH = Path(os.environ.get("SAU_WATCH_HEALTH", str(HERE / ".watch-health.json")))
STATE = HERE / ".monitor-state.json"
ALERT_COOLDOWN = 3600.0


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save(path: Path, data: dict) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _env() -> tuple[str, str]:
    values = {
        "SAU_WATCH_TG_BOT_TOKEN": "",
        "SAU_WATCH_TG_CHAT_ID": "",
    }
    dotenv = HERE / ".env"
    if dotenv.exists():
        for raw in dotenv.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key in values:
                values[key] = val.strip().strip('"').strip("'")
    token = os.environ.get("SAU_WATCH_TG_BOT_TOKEN", "") or values["SAU_WATCH_TG_BOT_TOKEN"]
    chat = os.environ.get("SAU_WATCH_TG_CHAT_ID", "") or values["SAU_WATCH_TG_CHAT_ID"]
    return token.strip(), chat.strip()


def send(title: str, body: str) -> bool:
    token, chat = _env()
    if not token or not chat:
        print("Telegram 未設定，無法通知", file=sys.stderr)
        return False
    import urllib.request

    ok = False
    for chat_id in [c.strip() for c in chat.replace(";", ",").split(",") if c.strip()]:
        payload = json.dumps({
            "chat_id": chat_id,
            "text": f"{title}\n\n{body}",
            "disable_web_page_preview": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                ok = resp.status == 200 or ok
        except Exception as exc:  # noqa: BLE001
            print(f"TG 通知失敗 {chat_id}: {exc}", file=sys.stderr)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="SAU-Inbox watcher watchdog")
    ap.add_argument("--stale-min", type=float, default=15.0)
    ap.add_argument("--stuck-hours", type=float, default=24.0,
                    help="alert when ready/pending items are older than this")
    ap.add_argument("--status", action="store_true", help="print health, never alert")
    args = ap.parse_args()

    health = _load_json(HEALTH)
    now = time.time()
    last_epoch = float(health.get("last_success_epoch") or 0)
    age_min = (now - last_epoch) / 60 if last_epoch else None

    summary = {
        "health_file": str(HEALTH),
        "exists": HEALTH.exists(),
        "last_success": health.get("last_success"),
        "age_minutes": round(age_min, 1) if age_min is not None else None,
        "ready": health.get("ready"),
        "pending": health.get("pending"),
        "last_error": health.get("last_error_message"),
    }
    print(json.dumps(summary, ensure_ascii=False))
    if args.status:
        return 0

    problems: list[tuple[str, str, str]] = []
    if not HEALTH.exists() or not last_epoch:
        problems.append((
            "watch-never-ran",
            "🚨 SAU inbox watcher 沒有心跳",
            "找不到成功掃描紀錄（.watch-health.json）。\n"
            "代表 watcher 從未成功跑完，或 cron 沒有在執行。\n"
            "新上傳不會被排程。\n\n"
            "查：crontab -l | grep sau-inbox-watch；tail -50 /home/will/sau-inbox/watch.log",
        ))
    elif age_min is not None and age_min > args.stale_min:
        problems.append((
            "watch-stale",
            "🚨 SAU inbox watcher 停擺",
            f"最後一次成功掃描是 {age_min:.0f} 分鐘前（門檻 {args.stale_min:.0f} 分鐘）。\n"
            f"最後錯誤：{health.get('last_error_message') or '（無）'}\n\n"
            "新上傳這段時間不會進佇列。\n"
            "查：tail -50 /home/will/sau-inbox/watch.log",
        ))
    elif (health.get("last_error_message")
          and float(health.get("last_error_epoch") or 0) > last_epoch):
        problems.append((
            "watch-error",
            "⚠️ SAU inbox watcher 上一次掃描失敗",
            f"{health.get('last_error_type')}: {health.get('last_error_message')}\n"
            f"（最後成功：{health.get('last_success')}）",
        ))

    # Items that arrived but were never acted on: the watcher notifies once on
    # arrival, which is easy to miss (that is how a four-day outage went
    # unnoticed). Remind daily while they sit there.
    state_path = HERE / "state.json"
    state = _load_json(state_path)
    from datetime import datetime, timezone

    def _age_hours(entry: dict) -> float | None:
        stamp = str(entry.get("createdAt") or entry.get("at") or "")
        if not stamp:
            return None
        try:
            when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (datetime.now(tz=timezone.utc) - when).total_seconds() / 3600

    stuck = []
    for bucket, label in (("ready", "已備好待你一句話"), ("pending", "檔名缺 sfw/nsfw 標籤")):
        for entry in state.get(bucket, []):
            age = _age_hours(entry)
            if age is None or age < args.stuck_hours:
                continue
            name = (entry.get("remotePath") or entry.get("path") or entry.get("name") or "?").split("/")[-1]
            stuck.append(f"{bucket}: {name}（{age:.0f} 小時，{label}）")
    if stuck:
        problems.append((
            "inbox-stuck:" + str(len(stuck)),
            f"⏳ SAU 收件匣有 {len(stuck)} 件卡著沒排程",
            "\n".join(stuck[:10])
            + "\n\n這些檔案還在 Drive 收件匣，不會被發布也不會被歸檔。"
              "\n要發：在 Telegram 回一句，或用網頁版 approve；不要了：reject。",
        ))

    monitor_state = _load_json(STATE)
    alerts = dict(monitor_state.get("alerts") or {})
    sent_any = False
    for signature, title, body in problems:
        last = float(alerts.get(signature) or 0)
        if now - last < ALERT_COOLDOWN:
            print(f"[cooldown] {signature}")
            continue
        if send(title, body):
            alerts[signature] = now
            sent_any = True
            print(f"[alerted] {signature}")
    monitor_state["alerts"] = {
        k: v for k, v in alerts.items() if now - float(v or 0) < 7 * 24 * 3600
    }
    monitor_state["checked_at"] = health.get("last_success")
    _save(STATE, monitor_state)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
