"""SAU system-status CLI.

Aggregates system health (publish/job counts, SAU-Inbox state, last
offload-to-Drive run, last daily digest) and prints it as a human block,
JSON, or a Telegram status message.

Usage:
    python tools/system_status.py              # human-readable block
    python tools/system_status.py --json       # raw collect() dict as JSON
    python tools/system_status.py --tg         # same block via Telegram

Telegram send is best-effort: missing ``SAU_ALERT_TELEGRAM_BOT_TOKEN`` /
``SAU_ALERT_TELEGRAM_CHAT_ID`` prints "TG not configured" and exits 0,
exactly as the ops-alert conventions in ``myUtils/ops_alerts.py``.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# Repo root on sys.path so ``myUtils`` resolves when invoked as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from myUtils.system_health import collect  # noqa: E402

DEFAULT_TIMEOUT = 30


def _fmt_optional(value, suffix: str = "") -> str:
    return f"{value}{suffix}" if value is not None else "-"


def render_block(data: dict) -> str:
    publish = data.get("publish") or {}
    targets = publish.get("targetsByStatus") or {}
    jobs = publish.get("jobsByStatus") or {}
    inbox = data.get("inbox") or {}
    offload = data.get("offload") or {}
    digest = data.get("digest") or {}

    target_line = ", ".join(f"{k}={v}" for k, v in sorted(targets.items())) or "none"
    job_line = ", ".join(f"{k}={v}" for k, v in sorted(jobs.items())) or "none"
    healthy = offload.get("healthy")
    healthy_text = (
        "yes" if healthy is True else "no" if healthy is False else "-"
    )

    lines = [
        "SAU system status",
        f"  publish targets : {target_line}",
        f"  publish jobs    : {job_line}",
        f"  inbox           : ready={inbox.get('ready', 0)} "
        f"pending={inbox.get('pending', 0)} quarantined={inbox.get('quarantined', 0)}",
        f"  offload         : lastRun={_fmt_optional(offload.get('lastRun'))} "
        f"exitCode={_fmt_optional(offload.get('exitCode'))} "
        f"files={_fmt_optional(offload.get('localFiles'))} healthy={healthy_text}",
        f"  digest          : lastSent={_fmt_optional(digest.get('lastSent'))}",
    ]
    return "\n".join(lines)


def send_telegram(text: str) -> bool:
    token = os.environ.get("SAU_ALERT_TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("SAU_ALERT_TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return False
    # parse_mode HTML: only < > & are special, everything else is literal.
    escaped = html.escape(text)
    payload = urllib.parse.urlencode(
        {
            "chat_id": chat,
            "text": escaped,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Telegram sendMessage returned HTTP {resp.status}")
        body = resp.read().decode("utf-8", errors="replace")
    ok = json.loads(body).get("ok") if body else False
    if not ok:
        raise RuntimeError("Telegram sendMessage returned ok=false")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SAU system status reporter")
    parser.add_argument(
        "--json", action="store_true", help="print the raw collect() dict as JSON"
    )
    parser.add_argument(
        "--tg", action="store_true", help="send the summary via Telegram"
    )
    parser.add_argument("action", nargs="?", default="status", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    data = collect()
    block = render_block(data)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if args.tg:
        try:
            if not send_telegram(block):
                print("TG not configured")
                return 0
        except Exception as exc:  # noqa: BLE001 - best-effort alerting
            print(f"TG send failed: {exc}", file=sys.stderr)
            return 1
        print("TG sent")
        return 0

    print(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())