"""Pre-publish review over Telegram.

The operator wants to see, before anything goes out, *what* will be published
*where* — and to be able to change it from the same screen. So every queued
campaign post produces one Telegram card carrying:

* the media itself (video when small enough for a bot upload, otherwise a
  poster frame) plus the hosted URL when the pipeline produced one,
* the exact copy that will be posted,
* the profile, the accounts (platform + name) and the scheduled time.

The card's ``message_id`` is recorded, so a **reply to that card** is
interpreted as an instruction for that specific post:

============================  ==========================================
reply text                    effect
============================  ==========================================
plain text                    replaces the post copy
``OK``                        acknowledges; nothing is changed
``PAUSE``                     cancels the queued targets
``CANCEL``                    same, and marks the post cancelled
``TIME 2026-10-05T15:00:00``  moves the queued targets
``SHOW``                      re-sends the card
============================  ==========================================

Everything here is best-effort: a Telegram outage must never block a publish,
so every network call is wrapped and the caller runs this off the request path.

Replies are read with ``getUpdates`` long-polling, which needs the bot to have
no webhook configured (Telegram rejects ``getUpdates`` while one is set).
"""

from __future__ import annotations

import html
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - environment specific
    requests = None

logger = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org/bot{token}/{method}"

# A bot may upload 50 MB through the Bot API. Leave headroom for multipart
# framing: above this we send a poster frame instead of the file.
MAX_VIDEO_UPLOAD_BYTES = 45 * 1024 * 1024
# Telegram caps media captions at 1024 characters; longer copy moves to a
# normal text message that maps to the same post.
CAPTION_LIMIT = 1024

_POLLER: threading.Thread | None = None
_POLLER_LOCK = threading.Lock()

HELP_LINE = (
    "Reply to this message with new text to replace the copy.\n"
    "Commands: OK · PAUSE · CANCEL · TIME 2026-10-05T15:00:00 · SHOW"
)


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
def _token() -> str:
    return str(
        os.environ.get("SAU_TG_REVIEW_BOT_TOKEN")
        or os.environ.get("SAU_ALERT_TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()


def _chat_ids() -> list[str]:
    raw = str(
        os.environ.get("SAU_TG_REVIEW_CHAT_ID")
        or os.environ.get("SAU_ALERT_TELEGRAM_CHAT_ID")
        or ""
    )
    out: list[str] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def is_configured() -> bool:
    return bool(_token() and _chat_ids() and requests is not None)


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------
@contextmanager
def _connect(db_path: Path | None):
    """Yield a row-factory connection and always close it.

    ``with sqlite3.connect(...)`` commits but does *not* close, so the handle
    outlives the block — enough to break temp-dir cleanup on Windows and to
    leak descriptors in the long-running server. This wrapper does both.
    """
    conn = sqlite3.connect(str(db_path or Path("/app/db/database.db")))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _remember_message(
    *,
    db_path: Path | None,
    chat_id: str,
    message_id: int,
    campaign_id: int | None,
    post_id: int | None,
    job_id: int | None,
    profile_id: int | None,
    media_path: str,
    copy_text: str,
) -> None:
    """Map one sent message to the post it describes (idempotent on message_id)."""
    try:
        with _connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO tg_review_messages
                    (message_id, chat_id, campaign_id, post_id, job_id, profile_id,
                     media_path, copy_text, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', datetime('now'))
                ON CONFLICT(message_id) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    campaign_id = excluded.campaign_id,
                    post_id = excluded.post_id,
                    job_id = excluded.job_id,
                    profile_id = excluded.profile_id,
                    media_path = excluded.media_path,
                    copy_text = excluded.copy_text
                """,
                (
                    int(message_id), str(chat_id), campaign_id, post_id, job_id,
                    profile_id, media_path, copy_text,
                ),
            )
            conn.commit()
    except Exception:  # noqa: BLE001 — bookkeeping only
        logger.warning("tg_review: could not record message %s", message_id, exc_info=True)

def lookup_message(message_id: int, *, db_path: Path | None) -> dict | None:
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM tg_review_messages WHERE message_id = ?", (int(message_id),)
            ).fetchone()
        return dict(row) if row else None
    except Exception:  # noqa: BLE001
        return None


def _set_state(db_path: Path | None, key: str, value: str) -> None:
    try:
        with _connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO tg_review_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            conn.commit()
    except Exception:  # noqa: BLE001
        logger.warning("tg_review: cannot persist state %s", key, exc_info=True)


def _get_state(db_path: Path | None, key: str, default: str = "") -> str:
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT value FROM tg_review_state WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default
    except Exception:  # noqa: BLE001
        return default


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------
def _api(method: str, *, data: dict, files: dict | None = None, timeout: int = 120):
    if requests is None:
        return None
    url = API_ROOT.format(token=_token(), method=method)
    if files:
        resp = requests.post(url, data=data, files=files, timeout=timeout)
    else:
        resp = requests.post(url, data=data, timeout=timeout)
    if resp.status_code != 200:
        logger.warning("tg_review: %s failed %s %s", method, resp.status_code, resp.text[:300])
        return None
    body = resp.json() if resp.content else {}
    if not body.get("ok"):
        logger.warning("tg_review: %s not ok: %s", method, str(body)[:300])
        return None
    return body.get("result")


def _send_text(chat_id: str, text: str) -> dict | None:
    return _api(
        "sendMessage",
        data={
            "chat_id": chat_id,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )


def _poster_frame(video: Path) -> Path | None:
    """Extract one frame from a video so the card shows what it looks like."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    out = Path(tempfile.gettempdir()) / f"sau_poster_{video.stem[:40]}.jpg"
    try:
        subprocess.run(
            [ffmpeg, "-y", "-ss", "1", "-i", str(video), "-frames:v", "1",
             "-vf", "scale=720:-2", str(out)],
            capture_output=True, timeout=120, check=True,
        )
        return out if out.exists() and out.stat().st_size else None
    except Exception:  # noqa: BLE001
        logger.info("tg_review: poster frame failed for %s", video, exc_info=True)
        return None


def _send_media(chat_id: str, media: Path | None, caption: str) -> dict | None:
    """Send the media itself, degrading video → poster frame → text only."""
    if media is None or not media.exists():
        return None
    suffix = media.suffix.lower()
    size = media.stat().st_size
    try:
        if suffix in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}:
            if size <= MAX_VIDEO_UPLOAD_BYTES:
                with media.open("rb") as fh:
                    sent = _api(
                        "sendVideo",
                        data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML",
                              "supports_streaming": True},
                        files={"video": (media.name, fh, "video/mp4")},
                    )
                if sent:
                    return sent
            poster = _poster_frame(media)
            if poster:
                with poster.open("rb") as fh:
                    sent = _api(
                        "sendPhoto",
                        data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                        files={"photo": (poster.name, fh, "image/jpeg")},
                    )
                try:
                    poster.unlink()
                except OSError:
                    pass
                if sent:
                    return sent
            return None
        if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
            with media.open("rb") as fh:
                return _api(
                    "sendPhoto",
                    data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                    files={"photo": (media.name, fh, "image/jpeg")},
                )
        with media.open("rb") as fh:
            return _api(
                "sendDocument",
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                files={"document": (media.name, fh, "application/octet-stream")},
            )
    except Exception:  # noqa: BLE001
        logger.warning("tg_review: media send failed for %s", media, exc_info=True)
        return None


# --------------------------------------------------------------------------
# card
# --------------------------------------------------------------------------
def _short(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def build_caption(
    *,
    profile_label: str,
    schedule_label: str,
    account_labels: list[str],
    media_name: str,
    media_size_mb: float | None,
    public_url: str,
    copy_text: str,
    copy_in_caption: bool,
) -> str:
    """Header caption for the media message (≤ Telegram's 1024-char cap)."""
    lines = ["<b>⏳ Review before publish</b>", ""]
    lines.append(f"<b>Profile</b>: {html.escape(profile_label)}")
    lines.append(f"<b>When</b>: {html.escape(schedule_label)}")
    if account_labels:
        lines.append("<b>Accounts</b>:")
        lines.extend(f" • {html.escape(a)}" for a in account_labels)
    size = f" ({media_size_mb:.1f} MB)" if media_size_mb else ""
    lines.append(f"<b>Media</b>: {html.escape(media_name)}{size}")
    if public_url:
        lines.append(f'<a href="{html.escape(public_url, quote=True)}">open media</a>')
    if copy_in_caption and copy_text:
        lines.append("")
        lines.append("<b>Copy</b>")
        lines.append(html.escape(copy_text))
    return _short("\n".join(lines), CAPTION_LIMIT)


def build_copy_message(post_id: int | None, copy_text: str) -> str:
    return (
        f"<b>Copy for post {post_id}</b>\n"
        f"{html.escape(copy_text or '(no copy yet)')}\n\n"
        f"<i>{html.escape(HELP_LINE)}</i>"
    )


def notify_posts(
    *,
    cards: list[dict],
    db_path: Path | None = None,
) -> int:
    """Send one review card per entry in ``cards``; returns how many landed.

    Each card dict carries:
    ``post_id``, ``job_id``, ``campaign_id``, ``profile_id``, ``profile_label``,
    ``schedule_label``, ``account_labels`` (list[str]), ``copy_text``,
    ``media_path`` (str | None), ``public_url`` (str), ``source`` (str).
    """
    if not is_configured():
        logger.info("tg_review: not configured; skipping %d card(s)", len(cards))
        return 0

    sent = 0
    for card in cards:
        try:
            media = Path(card["media_path"]) if card.get("media_path") else None
            if media is not None and not media.exists():
                media = None
            name = media.name if media else (card.get("media_label") or "media unavailable")
            size_mb = (media.stat().st_size / (1024 * 1024)) if media else None
            copy_text = card.get("copy_text") or ""

            header = build_caption(
                profile_label=card.get("profile_label") or f"profile {card.get('profile_id')}",
                schedule_label=card.get("schedule_label") or "immediate",
                account_labels=list(card.get("account_labels") or []),
                media_name=name,
                media_size_mb=size_mb,
                public_url=card.get("public_url") or "",
                copy_text=copy_text,
                copy_in_caption=len(copy_text) <= 500,
            )

            for chat_id in _chat_ids():
                message = _send_media(chat_id, media, header)
                if message is None:
                    message = _send_text(chat_id, header)
                if message is None:
                    continue
                sent += 1
                _remember_message(
                    db_path=db_path, chat_id=chat_id,
                    message_id=int(message["message_id"]),
                    campaign_id=card.get("campaign_id"), post_id=card.get("post_id"),
                    job_id=card.get("job_id"), profile_id=card.get("profile_id"),
                    media_path=str(media) if media else "",
                    copy_text=copy_text,
                )
                # Long copy (or a video card, where the caption is the header)
                # gets its own message so the operator reads it in full.
                if len(copy_text) > 500:
                    extra = _send_text(chat_id, build_copy_message(card.get("post_id"), copy_text))
                    if extra:
                        _remember_message(
                            db_path=db_path, chat_id=chat_id,
                            message_id=int(extra["message_id"]),
                            campaign_id=card.get("campaign_id"), post_id=card.get("post_id"),
                            job_id=card.get("job_id"), profile_id=card.get("profile_id"),
                            media_path=str(media) if media else "",
                            copy_text=copy_text,
                        )
        except Exception:  # noqa: BLE001 — one bad card must not stop the rest
            logger.warning("tg_review: card failed: %s", card.get("post_id"), exc_info=True)
    return sent


def notify_async(*, cards: list[dict], db_path: Path | None = None) -> None:
    """Fire-and-forget wrapper: the publish request must not wait on Telegram."""
    def _run():
        try:
            notify_posts(cards=cards, db_path=db_path)
        except Exception:  # noqa: BLE001
            logger.warning("tg_review: async notify failed", exc_info=True)

    threading.Thread(target=_run, daemon=True, name="tg-review-notify").start()


# --------------------------------------------------------------------------
# reply handling
# --------------------------------------------------------------------------
def parse_command(text: str) -> tuple[str, str]:
    """``(action, payload)`` for one reply body.

    Anything that is not a recognised verb is copy replacement — the operator
    replying with the sentence they want published is the common case, so it
    must not need a keyword.
    """
    raw = (text or "").strip()
    head = raw.split(maxsplit=1)[0].upper() if raw else ""
    rest = raw.split(maxsplit=1)[1].strip() if len(raw.split(maxsplit=1)) > 1 else ""
    if head in {"OK", "👍", "✅"}:
        return "ack", ""
    if head in {"PAUSE", "HOLD"}:
        return "pause", ""
    if head in {"CANCEL", "STOP"}:
        return "cancel", ""
    if head == "SHOW":
        return "show", ""
    if head in {"TIME", "RESCHEDULE"}:
        return "time", rest
    if head in {"EDIT", "COPY", "TEXT"} and rest:
        return "edit", rest
    return "edit", raw


def apply_copy(post_id: int | None, new_copy: str, *, db_path: Path | None = None) -> int:
    """Replace the copy on the post's record *and* on every queued job payload.

    The publisher reads the text out of ``publish_jobs.payload_json`` (that is
    what the worker sends), while the UI reads ``campaign_posts.draft_json``.
    Both have to move or the edit would show in one place and not the other.
    """
    if post_id is None:
        return 0
    changed = 0
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT draft_json FROM campaign_posts WHERE id = ?", (int(post_id),)
        ).fetchone()
        if row is not None:
            try:
                draft = json.loads(row["draft_json"] or "{}")
            except ValueError:
                draft = {}
            draft["message"] = new_copy
            conn.execute(
                "UPDATE campaign_posts SET draft_json = ?, updated_at = datetime('now') WHERE id = ?",
                (json.dumps(draft, ensure_ascii=False), int(post_id)),
            )
            changed += 1

        marker = f'"campaignPostId": {int(post_id)}'
        jobs = conn.execute(
            "SELECT id, payload_json FROM publish_jobs WHERE payload_json LIKE ?",
            (f"%{marker}%",),
        ).fetchall()
        for job in jobs:
            try:
                payload = json.loads(job["payload_json"] or "{}")
            except ValueError:
                continue
            payload["message"] = new_copy
            if isinstance(payload.get("draft"), dict):
                payload["draft"]["message"] = new_copy
            conn.execute(
                "UPDATE publish_jobs SET payload_json = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), job["id"]),
            )
            changed += 1
        conn.commit()
    return changed


def _target_ids(job_id: int | None, *, db_path: Path | None) -> list[int]:
    if job_id is None:
        return []
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id FROM publish_job_targets WHERE job_id = ? AND status IN ('pending','retrying')",
                (int(job_id),),
            ).fetchall()
        return [int(r["id"]) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def handle_reply(
    message_id: int,
    text: str,
    *,
    db_path: Path | None = None,
    cancel_target=None,
    reschedule_target=None,
) -> str:
    """Apply one reply. Returns a human-readable outcome for the confirmation.

    ``cancel_target`` / ``reschedule_target`` are injected by the backend so
    this module stays free of job-runtime imports (and testable without them).
    """
    record = lookup_message(message_id, db_path=db_path)
    if not record:
        return ""  # not a reply to something we sent
    action, payload = parse_command(text)
    post_id = record.get("post_id")
    job_id = record.get("job_id")

    if action == "ack":
        result = f"✅ post {post_id} left as-is"
    elif action in {"pause", "cancel"}:
        targets = _target_ids(job_id, db_path=db_path)
        for target_id in targets:
            if cancel_target is not None:
                try:
                    cancel_target(target_id)
                except Exception:  # noqa: BLE001
                    logger.warning("tg_review: cancel target %s failed", target_id, exc_info=True)
        if action == "cancel" and post_id is not None:
            try:
                with _connect(db_path) as conn:
                    conn.execute(
                        "UPDATE campaign_posts SET status = 'cancelled', updated_at = datetime('now') WHERE id = ?",
                        (int(post_id),),
                    )
                    conn.commit()
            except Exception:  # noqa: BLE001
                pass
        verb = "cancelled" if action == "cancel" else "paused"
        result = f"⏸ post {post_id} {verb} ({len(targets)} target(s))"
    elif action == "time":
        targets = _target_ids(job_id, db_path=db_path)
        moved = 0
        for target_id in targets:
            if reschedule_target is not None:
                try:
                    reschedule_target(target_id, payload)
                    moved += 1
                except Exception:  # noqa: BLE001
                    logger.warning("tg_review: reschedule %s failed", target_id, exc_info=True)
        result = f"🕒 post {post_id} moved to {payload} ({moved}/{len(targets)} target(s))"
    elif action == "show":
        result = f"post {post_id} — copy:\n{record.get('copy_text') or '(none)'}"
    else:
        changed = apply_copy(post_id, payload, db_path=db_path)
        result = f"✏️ copy updated for post {post_id} ({changed} record(s)):\n{payload}"

    try:
        with _connect(db_path) as conn:
            conn.execute(
                "UPDATE tg_review_messages SET status = ? WHERE message_id = ?",
                (action, int(message_id)),
            )
            conn.commit()
    except Exception:  # noqa: BLE001
        pass
    return result


def _get_updates(offset: int) -> list[dict]:
    if requests is None or not _token():
        return []
    try:
        resp = requests.get(
            API_ROOT.format(token=_token(), method="getUpdates"),
            params={"offset": offset, "timeout": 20, "allowed_updates": '["message"]'},
            timeout=40,
        )
        if resp.status_code != 200:
            return []
        body = resp.json()
        return body.get("result") or [] if body.get("ok") else []
    except Exception:  # noqa: BLE001 — transient network issues are normal
        return []


def _loop(interval_seconds: int, db_path: Path | None, cancel_target, reschedule_target) -> None:
    offset = int(_get_state(db_path, "updates_offset", "0") or 0)
    while True:
        try:
            updates = _get_updates(offset)
            for update in updates:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                message = update.get("message") or {}
                reply_to = (message.get("reply_to_message") or {}).get("message_id")
                body = message.get("text")
                chat = str((message.get("chat") or {}).get("id") or "")
                if reply_to is None or not body:
                    continue
                if _chat_ids() and chat not in _chat_ids():
                    continue
                outcome = handle_reply(
                    int(reply_to), body, db_path=db_path,
                    cancel_target=cancel_target, reschedule_target=reschedule_target,
                )
                if outcome and chat:
                    _send_text(chat, outcome)
            if updates:
                _set_state(db_path, "updates_offset", str(offset))
        except Exception:  # noqa: BLE001 — the poller must never die
            logger.warning("tg_review: poller tick failed", exc_info=True)
        time.sleep(max(1, interval_seconds))


def start_poller(
    *,
    interval_seconds: int,
    db_path: Path | None,
    cancel_target=None,
    reschedule_target=None,
) -> bool:
    """Start the reply poller once. Returns True when it is running."""
    global _POLLER
    if not is_configured():
        return False
    with _POLLER_LOCK:
        if _POLLER is not None:
            return True
        _POLLER = threading.Thread(
            target=_loop,
            args=(interval_seconds, db_path, cancel_target, reschedule_target),
            daemon=True,
            name="tg-review-poller",
        )
        _POLLER.start()
    return True
