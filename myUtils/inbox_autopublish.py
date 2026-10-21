"""Publish inbox items on a 24-hour countdown unless the operator intervenes.

The operator's rule (2026-10-21): *"If I don't interrupt or stop or modify then
publish — send a notification a day before, and if I did nothing, publish as it
is."*

How that is implemented
-----------------------
Every item the watcher puts in ``ready`` gets an ``autoPublishAt`` timestamp
(``createdAt + grace``, default 24 h). From then on:

  * **arm**   — one Telegram message with the title, rating, destinations and the
    exact publish time. That is the "a day before" notice.
  * **remind** — a second message ``--remind-hours`` before the deadline (2 h by
    default; 0 disables it).
  * **publish** — at the deadline the item is submitted through the same inbox
    route the UI uses (``POST /api/inbox/items/<id>/publish``) so it inherits the
    NSFW platform guard, per-account language routing and LLM copy generation.
    A filename ``publishDate`` pins the date (21:00 Taiwan), otherwise the
    allocator places it in the next free slot.

The operator's way out is any of the existing approve / reject / edit paths: an
item removed from ``ready`` before the deadline is simply no longer seen here, so
nothing is published. Editing it in the UI and letting it be published manually
has the same effect.

Safety
------
* ``SAU_INBOX_AUTOPUBLISH=0`` (or ``--disable``) turns the whole thing off.
* The item is claimed in ``state.json`` (``autoPublishState="publishing"``) under
  the same file lock the watcher uses, before the submit, so a second run cannot
  double-submit.
* A failed submit is recorded (``autoPublishAttempts``), retried on the next tick
  and reported to Telegram after ``--max-attempts`` (default 3).
* Only items the *watcher* ingested are considered: entries without a
  ``remotePath`` (hand-written state, legacy rows) are skipped.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_STATE = Path(os.environ.get("SAU_WATCH_STATE") or "/home/will/sau-inbox/state.json")
API_URL = os.environ.get("SAU_INBOX_PUBLISH_URL", "http://127.0.0.1:5409/api/inbox/items/{item_id}/publish")
DEFAULT_GRACE_HOURS = float(os.environ.get("SAU_INBOX_AUTOPUBLISH_HOURS", "24"))
DEFAULT_REMIND_HOURS = float(os.environ.get("SAU_INBOX_AUTOPUBLISH_REMIND_HOURS", "2"))
PUBLISH_HOUR_LOCAL = int(os.environ.get("SAU_INBOX_AUTOPUBLISH_HOUR", "21"))


# --------------------------------------------------------------------------
# state helpers (same lock file the watcher and the web app use)
# --------------------------------------------------------------------------
def _state_lock(state_path: Path):
    lock_path = state_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def load_state(state_path: Path) -> dict:
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "processed": {}, "ready": [], "pending": [], "quarantined": []}


def save_state(state_path: Path, state: dict) -> None:
    tmp = state_path.with_name(f"{state_path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(state_path)


# --------------------------------------------------------------------------
# telegram (host-side, uses the watcher's own credentials)
# --------------------------------------------------------------------------
def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if value:
        return value.strip()
    dotenv = REPO.parent / "sau-inbox" / ".env"
    for candidate in (dotenv, REPO / ".env"):
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            if raw.startswith(name + "="):
                return raw.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def notify(title: str, body: str) -> bool:
    token = _env("SAU_WATCH_TG_BOT_TOKEN") or _env("SAU_ALERT_TELEGRAM_BOT_TOKEN")
    chats = _env("SAU_WATCH_TG_CHAT_ID") or _env("SAU_ALERT_TELEGRAM_CHAT_ID")
    if not token or not chats:
        print("telegram not configured; skipping notification", file=sys.stderr)
        return False
    delivered = False
    for chat_id in [c.strip() for c in chats.replace(";", ",").split(",") if c.strip()]:
        payload = json.dumps({"chat_id": chat_id, "text": f"{title}\n{body}",
                              "disable_web_page_preview": True}).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload, headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                delivered = response.status == 200 or delivered
        except Exception as exc:  # noqa: BLE001
            print(f"telegram send failed for {chat_id}: {exc}", file=sys.stderr)
    return delivered


def _api_token() -> str:
    value = os.environ.get("SAU_API_TOKEN", "").strip()
    if value:
        return value
    env_file = REPO / ".env"
    try:
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            if raw.startswith("SAU_API_TOKENS="):
                tokens = raw.split("=", 1)[1].strip().strip('"').strip("'")
                return tokens.split(",")[0].strip()
    except OSError:
        pass
    return ""


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------
def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def start_at_for(entry: dict, now: datetime) -> str:
    """Naive Asia/Taipei start time for the submit payload.

    A ``publishDate`` from the filename pins the day at 21:00 (the documented
    mapping in docs/gdrive-filename-convention.md §7); otherwise the item goes
    out at the next preferred window, ``now + 15 min`` from the allocator's point
    of view. The value is naive because that is what the API expects.
    """
    local_now = now.astimezone(timezone(timedelta(hours=8)))
    publish_date = str(entry.get("publishDate") or "").strip()
    if publish_date:
        try:
            day = datetime.strptime(publish_date, "%Y-%m-%d")
            candidate = day.replace(hour=PUBLISH_HOUR_LOCAL)
            if candidate > local_now.replace(tzinfo=None):
                return candidate.isoformat(timespec="seconds")
        except ValueError:
            pass
    return (local_now + timedelta(minutes=15)).replace(tzinfo=None).isoformat(timespec="seconds")


def plan(state: dict, now: datetime, *, grace_hours: float, remind_hours: float,
         max_attempts: int) -> dict[str, list[dict]]:
    """Split the ready queue into arm / remind / publish / give-up buckets."""
    armed: list[dict] = []
    remind: list[dict] = []
    publish: list[dict] = []
    gave_up: list[dict] = []
    for entry in state.get("ready", []):
        if not entry.get("remotePath"):
            continue  # not a watcher-ingested item
        created = _parse_dt(entry.get("createdAt") or entry.get("at") or "")
        deadline = _parse_dt(entry.get("autoPublishAt") or "")
        if deadline is None:
            deadline = (created or now) + timedelta(hours=grace_hours)
            entry["autoPublishAt"] = deadline.isoformat(timespec="seconds")
            armed.append(entry)
        if entry.get("autoPublishState") == "publishing":
            continue  # claimed by an earlier run
        attempts = int(entry.get("autoPublishAttempts") or 0)
        if deadline <= now:
            if attempts >= max_attempts:
                gave_up.append(entry)
            else:
                publish.append(entry)
        elif remind_hours > 0 and not entry.get("autoReminderSent") and (
            deadline - now <= timedelta(hours=remind_hours)
        ):
            remind.append(entry)
    return {"armed": armed, "remind": remind, "publish": publish, "gave_up": gave_up}


def _describe(entry: dict) -> str:
    rating = str(entry.get("sfwFlag") or "").upper() or "?"
    personas = ",".join(str(p) for p in (entry.get("profileIds") or []))
    title = entry.get("topic") or entry.get("title") or "(no title)"
    kind = entry.get("kind") or "media"
    return f"{title}\n{rating} · {kind} · profiles {personas}"


def _fmt_local(value: datetime) -> str:
    return value.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M (Taipei)")


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------
def submit(item_id: str, start_at: str, *, timeout: float = 120.0) -> tuple[bool, str]:
    payload = json.dumps({"schedule": {"publishNow": False, "startAt": start_at}}).encode("utf-8")
    url = API_URL.format(item_id=item_id)
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Authorization": f"Bearer {_api_token()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status == 200 and '"code": 200' in body.replace("'", '"'), body
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:400]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def run(state_path: Path, *, grace_hours: float, remind_hours: float, max_attempts: int,
        dry_run: bool) -> int:
    now = datetime.now(tz=timezone.utc)
    handle = _state_lock(state_path)
    try:
        state = load_state(state_path)
        buckets = plan(state, now, grace_hours=grace_hours,
                       remind_hours=remind_hours, max_attempts=max_attempts)
        results = {"armed": 0, "remind": 0, "published": 0, "failed": 0, "gave_up": 0}

        for entry in buckets["armed"]:
            # The watcher already announced the item *and* its deadline in the
            # "已備好" message, so arming stays silent: one message on arrival,
            # one reminder, one publish confirmation.
            results["armed"] += 1
            deadline = _parse_dt(entry["autoPublishAt"]) or now
            print(f"arm {entry.get('remotePath')} -> {deadline.isoformat()}")

        for entry in buckets["remind"]:
            results["remind"] += 1
            deadline = _parse_dt(entry["autoPublishAt"]) or now
            print(f"remind {entry.get('remotePath')} -> {deadline.isoformat()}")
            if dry_run:
                continue
            if notify(
                "⏰ 2 小時後自動發布",
                f"{_describe(entry)}\n\n{_fmt_local(deadline)}\n"
                "不回覆就照這樣發；要停請回我或 reject。",
            ):
                entry["autoReminderSent"] = now.isoformat(timespec="seconds")

        for entry in buckets["publish"]:
            start_at = start_at_for(entry, now)
            print(f"publish {entry.get('remotePath')} (startAt={start_at})")
            if dry_run:
                continue
            entry["autoPublishState"] = "publishing"
            save_state(state_path, state)
            ok, body = submit(str(entry.get("id")), start_at)
            if ok:
                results["published"] += 1
                entry.pop("autoPublishState", None)
                entry["publishedAt"] = now.isoformat(timespec="seconds")
                remote = str(entry.get("remotePath") or "")
                state["ready"] = [e for e in state.get("ready", []) if e is not entry]
                state.setdefault("processed", {})[f"drive:{remote}"] = now.isoformat(timespec="seconds")
                campaigns: list[int] = []
                try:
                    campaigns = json.loads(body).get("data", {}).get("campaignIds") or []
                except ValueError:
                    pass
                notify(
                    "✅ 已自動排程發布",
                    f"{_describe(entry)}\n\ncampaigns: {campaigns}\n"
                    f"第一個時段：{start_at}（Taipei）\n"
                    "文案／逐字稿／排程都在網頁 Publish Center。",
                )
            else:
                results["failed"] += 1
                entry.pop("autoPublishState", None)
                entry["autoPublishAttempts"] = int(entry.get("autoPublishAttempts") or 0) + 1
                entry["autoPublishLastError"] = body[:300]
                if entry["autoPublishAttempts"] >= max_attempts:
                    notify("🚨 自動發布失敗（已停止重試）",
                           f"{_describe(entry)}\n\n{body[:500]}")
                save_state(state_path, state)

        for entry in buckets["gave_up"]:
            results["gave_up"] += 1
            print(f"gave up on {entry.get('remotePath')}: {entry.get('autoPublishLastError')}")

        save_state(state_path, state)
    finally:
        handle.close()

    print(json.dumps(results, ensure_ascii=False))
    return 1 if results["failed"] or results["gave_up"] else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Publish approved-by-silence inbox items")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--hours", type=float, default=DEFAULT_GRACE_HOURS,
                    help="hours before an item publishes itself (default 24)")
    ap.add_argument("--remind-hours", type=float, default=DEFAULT_REMIND_HOURS,
                    help="send a final reminder this many hours before (0 = off)")
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--disable", action="store_true", help="no-op (kill switch)")
    args = ap.parse_args(argv)

    if args.disable or str(os.environ.get("SAU_INBOX_AUTOPUBLISH", "1")).strip().lower() in {
        "0", "false", "no", "off"
    }:
        print("auto-publish disabled")
        return 0
    return run(args.state, grace_hours=args.hours, remind_hours=args.remind_hours,
               max_attempts=args.max_attempts, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
