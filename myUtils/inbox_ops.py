"""SAU-Inbox watcher state access (read + atomic update).

The phone-first flow drops files into GDrive ``SAU-Inbox``; the host
``watch.py`` syncs them to ``/home/will/sau-inbox`` and writes a JSON state
file. This module lets the backend / MCP / UI read that state and approve or
reject a ready item.

Paths:
* ``INBOX_DIR`` — where the watcher lives. Defaults to ``/app/sau-inbox``
  (the path the production compose mounts host ``/home/will/sau-inbox``
  onto). Falls back to the project-relative ``sau-inbox`` for local dev.
* ``STATE_PATH`` — ``<INBOX_DIR>/state.json`` (mirrors ``SAU_WATCH_STATE``).

State shape (v1, matches the watcher):
    {
      "version": 1,
      "processed": { "<abs path>": "<iso>" },
      "ready": [ {id, persona, profileIds, topic, sfwFlag, kind, sourcePath,
                   thumbPath, brief, contentNote, status: "ready", createdAt} ],
      "pending": [...], "quarantined": [...]
    }
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from myUtils import profiles as profile_registry

BASE_DIR = Path(__file__).resolve().parent.parent

INBOX_DIR = Path(os.environ.get("SAU_INBOX", BASE_DIR / "sau-inbox"))
STATE_PATH = Path(os.environ.get(
    "SAU_WATCH_STATE", str(INBOX_DIR / "state.json")))

# Media directories the watcher scans: <INBOX_DIR>/<persona>/<video|img>/.
KIND_DIRS = ("video", "img")

PERSONA_PROFILE_ID = {"nw": 1, "sw": 3, "teaching": 4, "msl": 10}

_lock = threading.Lock()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"version": 1, "processed": {}, "ready": [], "pending": [], "quarantined": []}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def find_item(state: dict, item_id: str, status: str = "ready") -> dict | None:
    return next((i for i in state.get(status, []) if str(i.get("id")) == str(item_id)),
                None)


def source_path_for(entry: dict) -> Path:
    """Absolute path of the media file for a ready entry."""
    raw = entry.get("sourcePath") or ""
    p = Path(raw)
    if p.is_absolute():
        return p
    persona = entry.get("persona")
    kind = entry.get("kind")
    return INBOX_DIR / persona / (kind or "") / p.name


def _scan_dir_for(path: Path) -> Path | None:
    """Locate a media file under the inbox (any persona/kind dir), tolerant of
    the watcher's key format (which stores the path under the persona dir)."""
    if path.is_file() or (path.is_absolute() and str(path).startswith(str(INBOX_DIR))):
        return path if path.is_file() else None
    if path.is_file():
        return path
    # Match across persona/video|img by basename when direct path is missing.
    name = path.name
    if INBOX_DIR.is_dir():
        for persona_dir in INBOX_DIR.iterdir():
            if not persona_dir.is_dir() or persona_dir.name.startswith("."):
                continue
            for kind in KIND_DIRS:
                cand = persona_dir / kind / name
                if cand.is_file():
                    return cand
    return None


class InboxItemNotFound(LookupError):
    """No such ready/pending item in the inbox state."""


class InboxStateConflict(ValueError):
    """The ready item moved between read and write (best-effort concurrency)."""


def list_items(status: str | None = None) -> dict:
    """Return {ready, pending, quarantined} lists (or a single status)."""
    state = load_state()
    if status:
        return {"items": state.get(status, [])}
    return {k: state.get(k, []) for k in ("ready", "pending", "quarantined")}


def _mutate(item_id: str, *, to_status: str, metadata: dict | None = None) -> dict:
    """Move a ready item to another list (or drop it), atomically."""
    state = load_state()
    entry = find_item(state, item_id, "ready")
    if entry is None:
        raise InboxItemNotFound(f"Inbox item not found: {item_id}")
    state["ready"] = [i for i in state["ready"] if str(i.get("id")) != str(item_id)]
    meta = dict(metadata or {})
    if to_status in ("pending", "quarantined"):
        entry = dict(entry)
        entry.update({"status": to_status, "at": utcnow(), **(meta or {})})
        state.setdefault(to_status, []).append(entry)
    else:
        # to_status == "processed": record it under processed by id and keep a
        # readable trail in the list for the UI.
        entry = dict(entry)
        entry.update({"status": "processed", **meta})
        state.setdefault("processed_items", []).append(entry)
    save_state(state)
    return entry


@dataclass
class InboxItem:
    """A ready/pending listing row for UI/MCP surfaces."""

    id: str
    persona: str
    profile_ids: list[int]
    topic: str
    sfw_flag: str
    kind: str
    source_path: str
    brief: str
    content_note: str
    status: str
    created_at: str
    thumb_path: str | None = None
    thumb_kind: str | None = None


def item_payload(entry: dict) -> dict:
    return {
        "id": entry.get("id"),
        "persona": entry.get("persona"),
        "profileIds": entry.get("profileIds") or [],
        "topic": entry.get("topic") or "",
        "sfwFlag": entry.get("sfwFlag"),
        "kind": entry.get("kind"),
        "sourcePath": entry.get("sourcePath"),
        "thumbPath": entry.get("thumbPath"),
        "thumbKind": entry.get("thumbKind"),
        "brief": entry.get("brief") or "",
        "contentNote": entry.get("contentNote"),
        "status": entry.get("status"),
        "createdAt": entry.get("createdAt"),
        "needsTitleGeneration": bool(entry.get("needsTitleGeneration")),
        "needsFrameSelection": bool(entry.get("needsFrameSelection")),
    }


def approve(item_id: str) -> dict:
    """Mark a ready item as approved (caller enqueues the publish)."""
    return _mutate(item_id, to_status="processed", metadata={"approvedAt": utcnow()})


def reject(item_id: str, reason: str | None = None) -> dict:
    """Move a ready item to the quarantined list."""
    return _mutate(item_id, to_status="quarantined",
                   metadata={"reason": reason or "rejected", "rejectedAt": utcnow()})