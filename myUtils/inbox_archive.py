"""Archive published inbox media back into the Drive archive, automatically.

Why this exists
---------------
`docs/gdrive-filename-convention.md` §10 says every piece of inbox media must end
up in `sau/published/videoFile/` renamed to ``<Title>_<SFW|NSFW>_<YYYYMMDD>`` with
a same-name ``.txt`` summary beside it. Until 2026-10-21 that step was **manual**:
an agent had to remember to run it after each publish, so `SAU-Inbox/` quietly
filled with already-published sources (the 2026-10-20 session found 404 objects
there, most of them long published).

This module makes it a background job:

  1. read the watcher's queue (`/home/will/sau-inbox/state.json`),
  2. for each item whose Drive object is still in the inbox, look up a campaign
     that published the same media,
  3. if that campaign finished (every target terminal, at least one succeeded),
     move the source to `sau/published/videoFile/<Title>_<RATING>_<date>.<ext>`
     and write the same-name ``.txt`` (summary + the exact per-platform copy),
  4. drop the item from the queue and mark it processed.

Nothing is ever deleted: a failure leaves the file where it is and reports.

Run it from cron every 15 minutes::

    python -m myUtils.inbox_archive --dry-run
    python -m myUtils.inbox_archive
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from myUtils import content_rating

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DB = Path(os.environ.get("SAU_DB_PATH") or (REPO / "db" / "database.db"))
DEFAULT_STATE = Path(os.environ.get("SAU_WATCH_STATE") or "/home/will/sau-inbox/state.json")
REMOTE = os.environ.get("SAU_INBOX_DRIVE_REMOTE", "GDrive-willywang8216")
INBOX_ROOT = os.environ.get("SAU_INBOX_DRIVE_ROOT", "SAU-Inbox").strip("/")
ARCHIVE_DIR = f"{REMOTE}:sau/published/videoFile"

# Targets in these states are finished. Anything else means the media may still
# be needed (pending/running/retrying) or is deliberately parked (needs_review
# is per-target only; the campaign status carries that).
TERMINAL_TARGETS = {"succeeded", "cancelled", "failed"}
MEDIA_SUFFIXES = {
    ".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi",
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
}
SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


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


def _rclone(args: Iterable[str], *, timeout: int = 900) -> subprocess.CompletedProcess:
    config = os.environ.get("SAU_INBOX_RCLONE_CONFIG", "/home/will/.config/rclone/rclone.conf")
    command = ["rclone", "--config", config, "--tpslimit", "2", "--tpslimit-burst", "2",
               "--retries", "6", "--low-level-retries", "20", *args]
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def safe_name(text: str) -> str:
    cleaned = SAFE_NAME_RE.sub("-", str(text or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:180] or "untitled"


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "processed": {}, "ready": [], "pending": [], "quarantined": []}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def published_media(db_path: Path, *, include_scheduled: bool = False) -> dict[str, dict]:
    """basename -> most recent finished campaign that used it.

    A campaign counts as finished when it has at least one target, no target is
    still live, and at least one target succeeded. With ``include_scheduled`` a
    campaign that has been fully scheduled (targets exist, none live-failed) also
    counts: the operator's drop box should only hold media the pipeline has not
    taken responsibility for yet. The basename is what the
    watcher and the inbox route both keep (the staged copy is
    ``videoFile/<original name>``), so it is the join key between the queue
    entry and the campaign without storing extra metadata.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT c.id AS campaign_id,
               c.status AS campaign_status,
               c.metadata_json AS metadata_json,
               fr.filename AS filename,
               MIN(CASE WHEN t.status = 'succeeded' THEN t.schedule_at END) AS first_success,
               MIN(t.schedule_at) AS first_schedule,
               MAX(t.schedule_at) AS last_schedule,
               COUNT(t.id) AS targets,
               SUM(CASE WHEN t.status IN ('succeeded', 'cancelled', 'failed') THEN 1 ELSE 0 END) AS terminal,
               SUM(CASE WHEN t.status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded
        FROM campaigns c
        JOIN media_group_items mgi ON mgi.media_group_id = c.media_group_id
        JOIN file_records fr ON fr.id = mgi.file_record_id
        JOIN campaign_posts cp ON cp.campaign_id = c.id
        JOIN publish_job_targets t ON t.file_ref = 'campaign_post:' || cp.id
        WHERE c.status NOT IN ('draft', 'preparing', 'needs_review')
        GROUP BY c.id, fr.filename
        """
    ).fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        if not row["targets"]:
            continue
        finished = row["terminal"] == row["targets"] and bool(row["succeeded"])
        scheduled = row["targets"] > 0
        if not finished and not (include_scheduled and scheduled):
            continue
        name = Path(str(row["filename"] or "")).name
        # Uploads made through the web UI (and the old `_inbox_cache` path) are
        # stored as ``<32-hex uuid>_<original name>``; the Drive inbox keeps the
        # original name only, so strip the prefix or every such item looks
        # unpublished forever (92 objects on 2026-10-21).
        name = re.sub(r"^[0-9a-f]{32}_", "", name, flags=re.IGNORECASE)
        if not name:
            continue
        candidate = {
            "campaign_id": row["campaign_id"],
            "status": row["campaign_status"],
            "filename": name,
            "first_success": row["first_success"],
            "first_schedule": row["first_schedule"],
            "last_schedule": row["last_schedule"],
            "succeeded": row["succeeded"],
            "published": finished,
            "metadata": _parse_metadata(row["metadata_json"]),
        }
        current = out.get(name)
        if current is None or int(candidate["campaign_id"]) > int(current["campaign_id"]):
            out[name] = candidate
    conn.close()
    return out


def _parse_metadata(raw: str | None) -> dict:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def campaign_copy(campaign_id: int, db_path: Path, limit: int = 12) -> list[tuple[str, str, str]]:
    """(platform, account, message) for the campaign, one row per account."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """
        SELECT cp.platform,
               COALESCE(a.account_name, t.account_ref),
               json_extract(cp.draft_json, '$.message')
        FROM campaign_posts cp
        LEFT JOIN publish_job_targets t ON t.file_ref = 'campaign_post:' || cp.id
        LEFT JOIN accounts a ON 'account:' || a.id = t.account_ref
        WHERE cp.campaign_id = ?
        GROUP BY cp.platform, t.account_ref
        ORDER BY cp.platform, t.account_ref
        LIMIT ?
        """,
        (campaign_id, limit),
    ).fetchall()
    conn.close()
    return [(r[0], r[1], r[2] or "") for r in rows]


def archive_name(remote_path: str, entry: dict, campaign: dict) -> tuple[str, str]:
    """(archived filename, publish date YYYYMMDD) for one inbox object."""
    source = Path(remote_path)
    title = str(campaign["metadata"].get("title") or entry.get("topic") or entry.get("title") or "").strip()
    if not title:
        title = source.stem.split("__")[0] or source.stem
    rating = content_rating.rating_for_filename(entry.get("remotePath") or source.name).upper()
    when = (
        campaign.get("first_success")
        or campaign.get("first_schedule")
        or campaign.get("last_schedule")
        or entry.get("publishDate")
        or datetime.now().strftime("%Y-%m-%d")
    )
    date_token = str(when)[:10].replace("-", "")
    return f"{safe_name(title)}_{rating}_{date_token}{source.suffix}", date_token


def build_note(remote_path: str, entry: dict, campaign: dict, archived: str, db_path: Path) -> str:
    lines = [
        f"# {campaign['metadata'].get('title') or entry.get('topic') or Path(remote_path).stem}",
        "",
        f"Archived file: {archived}",
        f"Source: {INBOX_ROOT}/{remote_path}",
        f"Rating: {content_rating.rating_for_filename(entry.get('remotePath') or archived).upper()}",
        f"Campaign: {campaign['campaign_id']} "
        + (f"({campaign['succeeded']} target(s) succeeded)"
           if campaign.get("published")
           else "(scheduled, not published yet)"),
        f"Profiles: {entry.get('profileIds')}",
        "",
        "## Brief used for generation",
        "",
        str(campaign["metadata"].get("notes") or entry.get("brief") or "").strip() or "(none)",
        "",
        "## Per-platform copy that was queued",
        "",
    ]
    for platform, account, message in campaign_copy(int(campaign["campaign_id"]), db_path):
        lines.extend([f"### {platform} — {account}", "", message.strip(), ""])
    return "\n".join(lines)


def _drive_objects() -> list[str]:
    """Every media object currently sitting in the Drive inbox."""
    listed = _rclone(["lsjson", f"{REMOTE}:{INBOX_ROOT}", "--recursive", "--files-only"],
                     timeout=300)
    if listed.returncode != 0:
        raise RuntimeError(f"rclone lsjson failed: {listed.stderr.strip()[-300:]}")
    try:
        entries = json.loads(listed.stdout or "[]")
    except ValueError as exc:
        raise RuntimeError(f"rclone returned invalid JSON: {exc}") from exc
    out = []
    for entry in entries:
        path = str(entry.get("Path") or entry.get("Name") or "").replace("\\", "/")
        if not path or Path(path).suffix.lower() not in MEDIA_SUFFIXES:
            continue
        if path.count("/") < 2:
            continue  # only <persona>/<kind>/<file>
        out.append(path)
    return out


def plan_archives(db_path: Path, state_path: Path, *, from_drive: bool = False,
                  include_scheduled: bool = False) -> list[dict]:
    state = load_state(state_path)
    published = published_media(db_path, include_scheduled=include_scheduled)
    plan: list[dict] = []
    for bucket in ("ready", "pending", "quarantined"):
        for entry in state.get(bucket, []):
            remote_path = str(entry.get("remotePath") or "").strip()
            if not remote_path or Path(remote_path).suffix.lower() not in MEDIA_SUFFIXES:
                continue
            campaign = published.get(Path(remote_path).name)
            if campaign is None:
                continue
            archived, date_token = archive_name(remote_path, entry, campaign)
            plan.append({
                "bucket": bucket,
                "remote_path": remote_path,
                "archived": archived,
                "campaign_id": campaign["campaign_id"],
                "note": build_note(remote_path, entry, campaign, archived, db_path),
                "entry_id": entry.get("id"),
            })
    if not from_drive:
        return plan

    # Reconcile the whole inbox against published campaigns, independent of
    # state.json: media whose queue entry was lost, hand-submitted, or archived
    # before this module existed still clutters the operator's Drive folder.
    queued = {item["remote_path"] for item in plan}
    for remote_path in _drive_objects():
        if remote_path in queued:
            continue
        campaign = published.get(Path(remote_path).name)
        if campaign is None:
            continue
        archived, _ = archive_name(remote_path, {}, campaign)
        plan.append({
            "bucket": "drive",
            "remote_path": remote_path,
            "archived": archived,
            "campaign_id": campaign["campaign_id"],
            "note": build_note(remote_path, {}, campaign, archived, db_path),
            "entry_id": None,
        })
    return plan


def apply_archive(item: dict, state_path: Path, *, dry_run: bool = False) -> str | None:
    """Move one object and write its note. Returns an error string or None."""
    source = f"{REMOTE}:{INBOX_ROOT}/{item['remote_path']}"
    dest = f"{ARCHIVE_DIR}/{item['archived']}"
    if dry_run:
        return None
    moved = _rclone(["moveto", source, dest])
    if moved.returncode != 0:
        return f"move failed for {item['remote_path']}: {moved.stderr.strip()[-300:]}"
    note_path = Path("/tmp") / f"sau-inbox-archive-{os.getpid()}-{abs(hash(item['archived']))}.txt"
    note_path.write_text(item["note"], encoding="utf-8")
    try:
        copied = _rclone(["copyto", str(note_path), f"{dest}.txt"])
        if copied.returncode != 0:
            return f"note upload failed for {item['archived']}: {copied.stderr.strip()[-300:]}"
    finally:
        note_path.unlink(missing_ok=True)

    state = load_state(state_path)
    removed = False
    for bucket in ("ready", "pending", "quarantined"):
        keep = []
        for entry in state.get(bucket, []):
            same = (
                str(entry.get("remotePath") or "") == item["remote_path"]
                or (item.get("entry_id") and entry.get("id") == item["entry_id"])
            )
            if same:
                removed = True
                continue
            keep.append(entry)
        state[bucket] = keep
    if removed:
        state.setdefault("processed", {})[f"drive:{item['remote_path']}"] = (
            datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        )
        save_state(state_path, state)
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Archive published inbox media to the Drive archive")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=25, help="max objects per run")
    ap.add_argument("--include-scheduled", action="store_true",
                    help="also archive media that is scheduled but not published yet")
    ap.add_argument("--from-drive", action="store_true",
                    help="also reconcile objects that are no longer in state.json")
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"database not found: {args.db}", file=sys.stderr)
        return 2
    plan = plan_archives(args.db, args.state, from_drive=args.from_drive,
                         include_scheduled=args.include_scheduled)
    if not plan:
        print("nothing to archive")
        return 0
    print(f"{len(plan)} published inbox object(s) to archive")
    errors: list[str] = []
    for item in plan[: args.limit]:
        print(f"  {item['remote_path']} -> {item['archived']} (campaign {item['campaign_id']})")
        if args.dry_run:
            continue
        error = apply_archive(item, args.state)
        if error:
            print(f"    ERROR: {error}", file=sys.stderr)
            errors.append(error)
    if errors:
        _alert(errors)
        return 1
    return 0


def _alert(errors: list[str]) -> None:
    try:
        _load_dotenv()
        from myUtils import ops_alerts

        ops_alerts.send_ops_alert(
            subject="⚠️ inbox archive 失敗（Drive 收件匣會堆積）",
            body="\n".join(errors[:5]),
        )
    except Exception:  # noqa: BLE001 - alerting must never mask the failure code
        pass


if __name__ == "__main__":
    raise SystemExit(main())
