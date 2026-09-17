"""System-health aggregation for SAU.

Single entry point ``collect()`` that snaps a flat summary of the running
system:

    publish  - publish_job_targets / publish_jobs grouped by status
    inbox    - SAU-Inbox watcher state (ready/pending/quarantined counts)
    offload  - last ``offload_to_drive.sh`` run (timestamp, rc, files, healthy)
    digest   - last daily-digest send timestamp (digest.log)
    ts       - collection time in UTC ISO-8601

Every read is defensive: missing log files, an absent database, or an
empty environment degrade to safe defaults and ``collect()`` never raises.
The CLI in ``tools/system_status.py`` renders this dict as a human block,
JSON, or a Telegram status message.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from utils.conf_defaults import BASE_DIR

DEFAULT_DB_PATH = Path(BASE_DIR) / "db" / "database.db"

# Candidate offload-to-Drive log locations, in priority order. The first
# candidate that yields a parseable "offload done" line wins.
_OFFLOAD_CANDIDATES = [
    Path("/app/logs/offload.log"),
    Path("/home/will/social-auto-upload/logs/offload.log"),
    Path("C:/Users/willy/Documents/SAU/repo/logs/offload.log"),
]

_EMPTY_PUBLISH = {"targetsByStatus": {}, "jobsByStatus": {}}
_EMPTY_INBOX = {"ready": 0, "pending": 0, "quarantined": 0}
_EMPTY_OFFLOAD = {"lastRun": None, "exitCode": None, "localFiles": None, "healthy": None}
_EMPTY_DIGEST = {"lastSent": None}

_DIGEST_MARKER = "digest sent="
_OFFLOAD_MARKER = "offload done"
_OFFLOAD_TARGET_MARKER = "local videoFile="


# ---------------------------------------------------------------------------
# publish (sqlite)
# ---------------------------------------------------------------------------

def _collect_publish(db_path: Path | None) -> dict:
    out = {"targetsByStatus": {}, "jobsByStatus": {}}
    if db_path is None or not db_path.exists():
        return out
    try:
        with sqlite3.connect(str(db_path), timeout=5) as conn:
            out["targetsByStatus"] = {
                str(status): int(count)
                for status, count in conn.execute(
                    "SELECT status, COUNT(*) FROM publish_job_targets GROUP BY status"
                ).fetchall()
            }
            out["jobsByStatus"] = {
                str(status): int(count)
                for status, count in conn.execute(
                    "SELECT status, COUNT(*) FROM publish_jobs GROUP BY status"
                ).fetchall()
            }
    except sqlite3.Error:
        return {"targetsByStatus": {}, "jobsByStatus": {}}
    return out


# ---------------------------------------------------------------------------
# inbox (myUtils.inbox_ops)
# ---------------------------------------------------------------------------

def _collect_inbox() -> dict:
    counts = dict(_EMPTY_INBOX)
    try:
        import importlib

        from myUtils import inbox_ops as _io
        # inbox_ops computes INBOX_DIR / STATE_PATH at import time. Re-import
        # when the environment has moved them since (tests, changing config)
        # so list_items() reads the intended state file.
        want_inbox = os.environ.get("SAU_INBOX", "").strip()
        want_state = os.environ.get("SAU_WATCH_STATE", "").strip()
        if (want_inbox and os.path.normpath(str(_io.INBOX_DIR)) != os.path.normpath(want_inbox)) or (
            want_state and os.path.normpath(str(_io.STATE_PATH)) != os.path.normpath(want_state)
        ):
            _io = importlib.reload(_io)
        res = _io.list_items()
        for key in ("ready", "pending", "quarantined"):
            items = res.get(key) or []
            counts[key] = len(items)
    except Exception:
        # Missing/unreadable state is not a failure of status reporting.
        pass
    return counts


# ---------------------------------------------------------------------------
# offload + digest (log scanning)
# ---------------------------------------------------------------------------

def _offload_candidates() -> list[Path]:
    env_path = os.environ.get("SAU_OFFLOAD_LOG", "").strip()
    candidates = [Path(env_path)] if env_path else []
    candidates.extend(_OFFLOAD_CANDIDATES)
    return candidates


def _digest_candidates() -> list[Path]:
    """digest.log siblings of the offload candidates (same log dirs)."""
    seen: set[str] = set()
    out: list[Path] = []
    for cand in _offload_candidates():
        sibling = cand.parent / "digest.log"
        key = str(sibling)
        if key not in seen:
            seen.add(key)
            out.append(sibling)
    return out


def _last_matching_line(candidates: list[Path], marker: str) -> str | None:
    """Scan candidates for the last line containing ``marker``.

    Returns the line, or ``None`` when no candidate exists / is readable /
    contains the marker. Scanning stops at the first candidate that yields a
    match (highest-priority location wins).
    """
    for cand in candidates:
        try:
            if not cand.is_file():
                continue
            for line in reversed(
                cand.read_text(encoding="utf-8", errors="replace").splitlines()
            ):
                if not line:
                    continue
                if marker in line:
                    return line
            # File existed and was readable but had no match: keep looking.
        except (OSError, UnicodeDecodeError):
            continue
    return None


def _timestamp_prefix(line: str) -> str | None:
    prefix = line[:25].strip()
    return prefix or None


def _parse_offload(line: str | None) -> dict:
    offload = dict(_EMPTY_OFFLOAD)
    if not line:
        return offload
    offload["lastRun"] = _timestamp_prefix(line)
    rc_match = re.search(r"rc=(\d+)", line)
    exit_code = int(rc_match.group(1)) if rc_match else None
    offload["exitCode"] = exit_code
    files_match = re.search(r"local videoFile=(\d+)", line)
    offload["localFiles"] = int(files_match.group(1)) if files_match else None
    offload["healthy"] = (exit_code == 0) if exit_code is not None else None
    return offload


def _collect_offload() -> dict:
    line = _last_matching_line(_offload_candidates(), _OFFLOAD_MARKER)
    if line is None:
        return dict(_EMPTY_OFFLOAD)
    # A bare "offload done" without the file count is too thin to trust;
    # require the full marker (matches the existing MCP tool behaviour).
    if _OFFLOAD_TARGET_MARKER not in line:
        return dict(_EMPTY_OFFLOAD)
    return _parse_offload(line)


def _collect_digest() -> dict:
    line = _last_matching_line(_digest_candidates(), _DIGEST_MARKER)
    return {"lastSent": _timestamp_prefix(line) if line else None}


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def collect(db_path: str | Path | None = None) -> dict:
    """Collect a flat system-health snapshot.

    ``db_path`` may be a string/path to the publish SQLite file. When omitted,
    ``SAU_SYSTEM_DB`` env var is honoured, then the canonical
    ``db/database.db``. The function never raises: any read failure yields its
    safe default.
    """
    resolved_db: Path | None
    if db_path:
        resolved_db = Path(db_path)
    else:
        env_db = os.environ.get("SAU_SYSTEM_DB", "").strip()
        resolved_db = Path(env_db) if env_db else DEFAULT_DB_PATH

    result = {
        "publish": {},
        "inbox": {},
        "offload": {},
        "digest": {},
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        result["publish"] = _collect_publish(resolved_db)
    except Exception:
        result["publish"] = dict(_EMPTY_PUBLISH)
    try:
        result["inbox"] = _collect_inbox()
    except Exception:
        result["inbox"] = dict(_EMPTY_INBOX)
    try:
        result["offload"] = _collect_offload()
    except Exception:
        result["offload"] = dict(_EMPTY_OFFLOAD)
    try:
        result["digest"] = _collect_digest()
    except Exception:
        result["digest"] = dict(_EMPTY_DIGEST)
    return result