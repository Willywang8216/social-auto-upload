#!/usr/bin/env bash
# SAU media cache: move published assets off the VPS into Google Drive so the
# box stays lean, and RECORD where they went so the worker can pull them back
# when a scheduled post needs them.
# --min-age 10m is a mid-write guard only (NOT retention). Installed 2026-09-04.
# Cron runs as `will`, but the SAU container mounts videoFile/ as root, so most
# media is root-owned and rclone-as-will cannot unlink it. Therefore transfer is
# `rclone copy` and local deletion is a SEPARATE verified step that sudo-unlinks
# (see purge_verified_sources); the rclone config stays readable only to `will`.
#
# The loop this implements, end to end:
#   素材上傳 → 發布 → offload 到 GDrive（省空間）→ 到點抓回來發 → 發完清除
#
#   * files still needed LOCALLY are never touched: artifacts of in-flight
#     targets (running/retrying), of pending targets due within 30 minutes
#     (claim-race window), files with no file_records row (unrestoreable),
#     and videoFile/_library/** (tools/reddit_schedule.py enumerates it
#     straight off disk)
#   * everything else - INCLUDING future-scheduled posts - is moved, and
#     file_records is updated with the remote location + backend id so
#     myUtils.worker can download it back at claim time
#     (worker._resolve_file_path / worker._ensure_artifact_paths_local)
#   * the verified purge IS the delete: rclone copies the bytes up, then each
#     source is unlinked ONLY after `rclone check --size-only --one-way` agrees
#     the remote holds it, so "gone locally + present remotely" is the safe
#     signal we register on. A file whose copy can't be verified stays put.
#   * after a target succeeds its file falls out of the exclusion set and the
#     next run archives it back to Drive automatically.
#
# If anything needed to build the exclude list fails we skip the move entirely
# rather than risk deleting media the scheduler still needs.
set -uo pipefail
CONF=/home/will/.config/rclone/rclone.conf
SRC=/home/will/social-auto-upload
DST=GDrive-willywang8216:sau
DB=$SRC/db/database.db
LOG=$SRC/logs/offload.log
TGENV=/home/will/mailserver/monitor/.telegram_env
mkdir -p "$SRC/logs"
# Cron can overlap when a large transfer runs longer than 30 minutes. Keep the
# lock in a private, will-owned directory instead of a predictable /tmp path.
acquire_offload_lock() {
  local home_cache="$HOME/.cache"
  local lock_dir="${OFFLOAD_LOCK_DIR:-$home_cache/social-auto-upload-drive-offload}"
  local lock_file="$lock_dir/drive-offload.lock"
  if [ -L "$HOME" ] || [ -L "$home_cache" ]; then
    printf '[%s] refusing symlink home/cache lock parent\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$SRC/logs/offload.log"
    return 2
  fi
  mkdir -p "$home_cache" || return 2
  if [ ! -d "$home_cache" ] || [ "$(stat -c %u "$home_cache")" != "$(id -u)" ]; then
    printf '[%s] refusing unsafe cache lock parent: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$home_cache" >> "$SRC/logs/offload.log"
    return 2
  fi
  if [ ! -e "$lock_dir" ] && ! mkdir "$lock_dir" 2>/dev/null; then
    printf '[%s] could not create offload lock directory: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$lock_dir" >> "$SRC/logs/offload.log"
    return 2
  fi
  if [ -L "$lock_dir" ] || [ ! -d "$lock_dir" ] || [ "$(stat -c %u "$lock_dir")" != "$(id -u)" ]; then
    printf '[%s] refusing unsafe offload lock directory: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$lock_dir" >> "$SRC/logs/offload.log"
    return 2
  fi
  chmod 700 "$lock_dir" || return 2
  if [ -L "$lock_file" ]; then
    printf '[%s] refusing symlink offload lock: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$lock_file" >> "$SRC/logs/offload.log"
    return 2
  fi
  exec 9>>"$lock_file" || return 2
  if ! flock -n 9; then
    printf '[%s] offload skipped: another run holds the lock\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$SRC/logs/offload.log"
    return 1
  fi
}
if acquire_offload_lock; then
  :
else
  lock_rc=$?
  if [ "$lock_rc" -eq 1 ]; then
    exit 0
  fi
  exit 4
fi
# tiny log rotation
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
  tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
RCLONE_BIN=${RCLONE_BIN:-/usr/bin/rclone}
RC=("$RCLONE_BIN" --config "$CONF")
ts(){ date -u +%Y-%m-%dT%H:%M:%SZ; }
rc=0

preflight_storage_backends() {
  python3 - "$DB" "$DST" "$SRC" <<'PY'
import sqlite3
import sys
from pathlib import Path

db_path, dst, source = sys.argv[1:]
source = Path(source)
remote = dst.split(":", 1)[0]
conn = sqlite3.connect(db_path, timeout=15)
try:
    conn.execute("PRAGMA busy_timeout=15000")
    for root in ("videoFile", "uploads", "generated"):
        if not (source / root).is_dir():
            continue
        backend = conn.execute(
            "SELECT id FROM storage_backends WHERE provider='rclone' AND bucket=? AND endpoint=? AND enabled=1",
            (remote, f"sau/{root}"),
        ).fetchone()
        if backend is None:
            print(f"missing enabled rclone backend for {remote}:sau/{root}", file=sys.stderr)
            raise SystemExit(1)
finally:
    conn.close()
PY
}

register_local_generated() {
  python3 - "$DB" "$SRC" <<'PY'
import sqlite3, sys
from pathlib import Path

db_path, src = sys.argv[1], Path(sys.argv[2])
root = src / "generated"
if not root.is_dir():
    raise SystemExit(0)
conn = sqlite3.connect(db_path, timeout=15)
try:
    conn.execute("PRAGMA busy_timeout=15000")
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(src).as_posix()
        exists = conn.execute(
            "SELECT 1 FROM file_records WHERE file_path IN (?, ?)",
            (rel, "/app/" + rel),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO file_records (filename, filesize, file_path) VALUES (?, ?, ?)",
            (path.name, path.stat().st_size, rel),
        )
    conn.commit()
except Exception:
    conn.rollback()
    raise
finally:
    conn.close()
PY
}

escape_rclone_filter_path() {
  python3 - "$1" <<'PY'
import re, sys
print(re.sub(r"([\\*?\[\]{}])", r"\\\1", sys.argv[1]))
PY
}

verify_and_register_unreadable_source() {
  local source_root="$1"
  local remote_root="$2"
  local relative="$3"
  local source_file="$source_root/$relative"
  local before after staging escaped size
  [ -f "$source_file" ] && [ ! -L "$source_file" ] || return 1
  before=$(sudo -n stat -c '%d:%i:%s:%Y:%a:%u:%g' -- "$source_file") || return 1
  size=$(sudo -n stat -c '%s' -- "$source_file") || return 1
  staging=$(mktemp -d "${TMPDIR:-/tmp}/sau-offload-stage.XXXXXX") || return 1
  mkdir -p "$staging/$(dirname "$relative")" || { rm -rf "$staging"; return 1; }
  if ! sudo -n cat -- "$source_file" > "$staging/$relative"; then
    rm -rf "$staging"
    return 1
  fi
  if [ "$(stat -c '%s' -- "$staging/$relative")" != "$size" ]; then
    rm -rf "$staging"
    return 1
  fi
  escaped=$(escape_rclone_filter_path "$relative")
  if ! "${RC[@]}" copyto "$staging/$relative" "$remote_root/$relative" --stats-one-line >/dev/null 2>&1 || \
     ! "${RC[@]}" check "$staging" "$remote_root" --one-way --include "/$escaped" >/dev/null 2>&1; then
    rm -rf "$staging"
    return 1
  fi
  after=$(sudo -n stat -c '%d:%i:%s:%Y:%a:%u:%g' -- "$source_file") || { rm -rf "$staging"; return 1; }
  if [ "$before" != "$after" ]; then
    echo "[$(ts)] source changed during staged copy: $relative"
    rm -rf "$staging"
    return 1
  fi
  if ! register_verified_source "$source_root" "$relative" "$size"; then
    rm -rf "$staging"
    return 1
  fi
  if ! sudo -n rm -- "$source_file"; then
    echo "[$(ts)] could not unlink staged and registered source: $relative"
    rm -rf "$staging"
    return 1
  fi
  rm -rf "$staging"
  echo "[$(ts)] staged, verified and offloaded unreadable source: $relative ($size bytes)"
}

register_verified_source() {
  local source_root="$1"
  local relative="$2"
  local size="${3:-}"
  if [ -z "$size" ]; then
    size=$(stat -c%s -- "$source_root/$relative") || return 1
  fi
  python3 - "$DB" "$DST" "$source_root" "$relative" "$size" <<'PY'
import sqlite3
import sys
from pathlib import Path

db_path, dst, source_root, relative, size_raw = sys.argv[1:]
root = Path(source_root).name
remote = dst.split(":", 1)[0]
relative = Path(relative).as_posix()
size = int(size_raw)
if root not in {"videoFile", "uploads", "generated"}:
    raise SystemExit("unsupported source root")
conn = sqlite3.connect(db_path, timeout=15)
try:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("BEGIN IMMEDIATE")
    backend = conn.execute(
        "SELECT id FROM storage_backends WHERE provider='rclone' AND bucket=? AND endpoint=? AND enabled=1",
        (remote, f"sau/{root}"),
    ).fetchone()
    if backend is None:
        raise RuntimeError(f"missing backend for {remote}:sau/{root}")
    if root == "videoFile":
        candidates = (relative, f"videoFile/{relative}", f"/app/videoFile/{relative}")
    else:
        candidates = (f"{root}/{relative}", f"/app/{root}/{relative}")
    placeholders = ",".join("?" for _ in candidates)
    records = conn.execute(
        f"SELECT id, filesize, storage_key, storage_backend_id FROM file_records WHERE file_path IN ({placeholders})",
        candidates,
    ).fetchall()
    if not records:
        raise RuntimeError(f"no file_records row for {root}/{relative}")
    for row in records:
        # rclone check immediately before this transaction proved these bytes
        # exist remotely at the same size as this local source. Repair stale
        # legacy filesize values from that verified fact before unlinking.
        conn.execute(
            "UPDATE file_records SET filesize=?, storage_key=?, storage_backend_id=? WHERE id=?",
            (size, relative, backend["id"], row["id"]),
        )
    conn.commit()
except Exception:
    conn.rollback()
    raise
finally:
    conn.close()
PY
}

purge_verified_sources() {
  local source_root="$1"
  local remote_root="$2"
  local paths relative failed=0
  paths=$("${RC[@]}" lsf "$source_root" --recursive --files-only --min-age 10m --exclude-from "$EXCLUDES") || return 1
  while IFS= read -r relative; do
    [ -n "$relative" ] || continue
    case "$relative" in
      /*|..|../*|*/../*|*/..)
        echo "[$(ts)] refusing unsafe source path from rclone: $relative"
        failed=1
        continue
        ;;
    esac
    source_file="$source_root/$relative"
    if [ -L "$source_file" ] || [ ! -f "$source_file" ] || [[ "$(realpath -m -- "$source_file")" != "$(realpath -m -- "$source_root")"/* ]]; then
      echo "[$(ts)] refusing non-regular or escaping source path: $relative"
      failed=1
      continue
    fi
    if [ ! -r "$source_file" ]; then
      if ! verify_and_register_unreadable_source "$source_root" "$remote_root" "$relative"; then
        echo "[$(ts)] keeping unreadable source after failed staged verification: $relative"
        failed=1
      fi
      continue
    fi
    before_stat=$(stat -c '%d:%i:%s:%Y' -- "$source_file") || { failed=1; continue; }
    # Compare transfer roots so rclone can use content hashes when available.
    # Escaped includes make filename metacharacters literal; --one-way ignores
    # unrelated remote files without weakening verification of this source.
    escaped=$(escape_rclone_filter_path "$relative")
    if "${RC[@]}" check "$source_root" "$remote_root" --one-way \
         --include "/$escaped" >/dev/null 2>&1; then
      after_stat=$(stat -c '%d:%i:%s:%Y' -- "$source_file") || { failed=1; continue; }
      if [ "$before_stat" != "$after_stat" ]; then
        echo "[$(ts)] keeping $relative: source changed during remote verification"
        failed=1
        continue
      fi
      # Persist the exact restore mapping before deletion. If registration or
      # privileged unlink fails, the verified local source remains available.
      if ! register_verified_source "$source_root" "$relative"; then
        echo "[$(ts)] keeping $relative: verified remote copy could not be registered"
        failed=1
        continue
      fi
      sudo -n rm -- "$source_root/$relative" || { echo "[$(ts)] could not unlink verified source: $relative"; failed=1; }
    else
      echo "[$(ts)] keeping $relative: remote copy is missing or differs"
      failed=1
    fi
  done <<< "$paths"
  return "$failed"
}

# The rc of the run BEFORE this one, read back from the log (the block below
# appends this run's line last, so the second-newest is the previous run).
offload_previous_rc() {
  grep -o 'offload done rc=[0-9]*' "$LOG" 2>/dev/null | tail -2 | head -1 | sed 's/.*rc=//'
}

# Best-effort operator notice: page on failure and send exactly ONE recovery
# line when a failing run goes healthy again.
#
# Every message carries its own UTC timestamp because a "FAILED" line with no
# time is indistinguishable from a live failure once Telegram delivers it late:
# a stale alert for an already-fixed incident looked current and sent the
# operator chasing a run that had been clean for hours. The recovery line is
# what closes that loop — one message, on the failing -> healthy transition
# only, so it never becomes a heartbeat.
notify() {
  local current_rc="$1"
  local previous_rc="$2"
  [ -f "$TGENV" ] || return 0
  local text=""
  if [ "$current_rc" -ne 0 ]; then
    text="SAU->Drive offload FAILED (rc=$current_rc) at $(ts) on $(hostname). Check $LOG"
  elif [ -n "$previous_rc" ] && [ "$previous_rc" -ne 0 ]; then
    text="SAU->Drive offload RECOVERED (rc=0) at $(ts) on $(hostname) — the previous run (rc=$previous_rc) has cleared."
  fi
  [ -n "$text" ] || return 0
  . "$TGENV"
  curl -s -m 20 "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TG_CHAT}" \
    --data-urlencode "text=$text" >/dev/null 2>&1
  return 0
}

# Emit one rclone filter pattern per line for every file that must stay on
# local disk. Exclusion set (all anchored to the transfer root, glob
# metacharacters escaped so odd filenames can't widen the match):
#   1. artifacts of running/retrying targets - in-flight uploads read the
#      file from disk directly and cannot tolerate it vanishing mid-send
#   2. artifacts of pending targets due within DUE_SOON_MINUTES - the
#      claim-race window between this list and the actual rclone copy
#      (NULL/empty schedule_at means "claimable now", so those count as due)
#   3. files with no file_records row - nothing to restore from, moving
#      them would strand the pipeline (the bytes stay on Drive, but no
#      worker path can find them again)
#   4. videoFile/_library/** - tools/reddit_schedule.py enumerates that
#      directory straight off disk to build its payloads
# Everything else - including far-future scheduled posts - is safe to move:
# myUtils.worker downloads files back at claim time (worker._resolve_file_path
# for direct targets, worker._ensure_artifact_paths_local for campaign
# artifacts) and register_offloaded records the remote location right after
# the transfer.
# -i is load-bearing: without it docker exec closes stdin and python reads
# the heredoc as empty, which would report "0 exclusions" and move everything.
register_local_generated() {
  python3 - "$DB" "$SRC" <<'PY'
import sqlite3, sys
from pathlib import Path

db_path, src = sys.argv[1], Path(sys.argv[2])
root = src / "generated"
if not root.is_dir():
    raise SystemExit(0)
conn = sqlite3.connect(db_path, timeout=15)
try:
    conn.execute("PRAGMA busy_timeout=15000")
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(src).as_posix()
        exists = conn.execute(
            "SELECT 1 FROM file_records WHERE file_path IN (?, ?)",
            (rel, "/app/" + rel),
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO file_records (filename,filesize,file_path) VALUES (?,?,?)",
                (path.name, path.stat().st_size, rel),
            )
    conn.commit()
except Exception:
    conn.rollback()
    raise
finally:
    conn.close()
PY
}

pending_excludes() {
  docker exec -i social-auto-upload python3 - <<'PY'
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

IN_FLIGHT = ("running", "retrying")
DUE_SOON_MINUTES = 30
KEEP_LOCAL_PREFIXES = ("_library/",)  # under the videoFile root

# Same naive-UTC iso shape jobs._now_iso writes, so schedule_at compares
# chronologically as a plain string (normalize T vs space separators too).
now_utc = datetime.now(tz=timezone.utc).replace(tzinfo=None)
due_cutoff = (now_utc + timedelta(minutes=DUE_SOON_MINUTES)).isoformat(
    timespec="seconds"
).replace("T", " ")

seen = set()
conn = sqlite3.connect("/app/db/database.db")


def emit(rel: str) -> None:
    if not rel or rel in seen:
        return
    seen.add(rel)
    print("/" + re.sub(r"([\\*?\[\]{}])", r"\\\1", rel))


def emit_media_path(local_path: str) -> None:
    """Emit the transfer-root-relative part of an absolute/local media path."""
    for marker in ("/videoFile/", "/uploads/", "/generated/"):
        if marker in local_path:
            emit(local_path.split(marker, 1)[1])
            return


def emit_file_ref(ref: str) -> None:
    """Emit a target's file_ref (legacy direct targets store a path here;
    campaign targets store campaign_post:<id> and are skipped)."""
    ref = (ref or "").strip()
    if not ref or ref.startswith("campaign_post:"):
        return
    for marker in ("/videoFile/", "/uploads/", "/generated/"):
        if marker in ref:
            emit(ref.split(marker, 1)[1])
            return
    if ref.startswith(("videoFile/", "uploads/", "generated/")):
        emit(ref.split("/", 1)[1])
    else:
        emit(ref)  # legacy: path relative to the videoFile root


def emit_owed(sql: str, params: tuple = ()) -> None:
    """Emit every media path a set of targets still needs: file_ref, the
    payload's campaign artifacts, and twitter thread refs. Same coverage
    for in-flight and due-soon rows."""
    for payload_json, file_ref in conn.execute(sql, params):
        emit_file_ref(file_ref)
        try:
            data = json.loads(payload_json)
        except Exception:
            # Payloads are written by enqueue_job with json.dumps, so this
            # should not happen; if it does, skip rather than abort the
            # whole exclude build (the unrecorded-file walk below still
            # protects anything file_records knows nothing about).
            continue
        for artifact in data.get("artifacts") or []:
            local_path = artifact.get("local_path") or ""
            if local_path:
                emit_media_path(local_path)
        for ref in data.get("threadFileRefs") or []:
            if isinstance(ref, str) and ref:
                emit_media_path(ref)
                emit_file_ref(ref)


DUE_SOON_SQL = """
    SELECT j.payload_json, t.file_ref FROM publish_jobs j
    JOIN publish_job_targets t ON t.job_id = j.id
    WHERE t.status = 'pending'
      AND (t.schedule_at IS NULL OR t.schedule_at = ''
           OR replace(t.schedule_at, 'T', ' ') <= ?)
"""

# 1 + 2: in-flight targets, and pending targets inside the claim-race window.
emit_owed(
    """
    SELECT j.payload_json, t.file_ref FROM publish_jobs j
    JOIN publish_job_targets t ON t.job_id = j.id
    WHERE t.status IN (?, ?)
    """,
    IN_FLIGHT,
)
emit_owed(DUE_SOON_SQL, (due_cutoff,))

# 3 + 4: unrecorded files, and the _library tree the Reddit tool walks.
recorded = set()
for (file_path,) in conn.execute("SELECT file_path FROM file_records"):
    fp = (file_path or "").strip()
    if not fp:
        continue
    if fp.startswith("uploads/"):
        recorded.add(("uploads", fp[len("uploads/"):]))
    elif fp.startswith("generated/"):
        recorded.add(("generated", fp[len("generated/"):]))
    elif fp.startswith("videoFile/"):
        recorded.add(("videoFile", fp[len("videoFile/"):]))
    else:
        recorded.add(("videoFile", fp))

for root in ("videoFile", "uploads", "generated"):
    base = "/app/" + root
    if not os.path.isdir(base):
        continue
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), base)
            if root == "videoFile" and rel.startswith(KEEP_LOCAL_PREFIXES):
                emit(rel)
                continue
            if (root, rel) not in recorded:
                emit(rel)

conn.close()
# Sentinel: the bash side greps for this line, so an empty/short-circuited
# heredoc (which would mean "no exclusions" -> move EVERYTHING) is detected
# and the move is skipped instead. rclone treats it as a never-matching
# literal pattern.
print(f"# excludes={len(seen)}")
PY
}

{
  echo "[$(ts)] offload start"
  EXCLUDES=$(mktemp)
  if ! preflight_storage_backends; then
    echo "[$(ts)] SKIPPED offload: a required storage backend is not configured"
    rc=4
  elif ! register_local_generated; then
    echo "[$(ts)] SKIPPED offload: could not register generated local artifacts"
    rc=4
  elif ! pending_excludes > "$EXCLUDES" || ! grep -q '^# excludes=' "$EXCLUDES"; then
    echo "[$(ts)] SKIPPED offload: could not build the keep-local exclude list"
    rc=3
  else
    KEEP_COUNT=$(grep -vc '^#' "$EXCLUDES" || true)
    echo "[$(ts)] keeping ${KEEP_COUNT} file(s) local (in-flight / due soon / unrecorded / _library)"
    for d in videoFile uploads generated; do
      [ -d "$SRC/$d" ] || continue
      source_root="$SRC/$d"
      destination_root="$DST/$d"
      # Copy, verify and persist restore metadata before purging each root.
      # Do not skip the per-file verification pass when rclone reports a copy
      # error: it can still have copied readable siblings, while the purge path
      # stages root-owned unreadable files individually and fails closed per file.
      if ! "${RC[@]}" copy "$source_root" "$destination_root" --min-age 10m --exclude-from "$EXCLUDES" \
        --transfers 4 --checkers 8 --stats-one-line -v 2>&1; then
        echo "[$(ts)] copy reported errors under $source_root; checking each source before any unlink"
      fi
      if ! purge_verified_sources "$source_root" "$destination_root"; then
        echo "[$(ts)] could not remove all verified sources under $source_root"
        rc=1
      fi
    done
  fi
  rm -f "$EXCLUDES"
  echo "[$(ts)] offload done rc=$rc local videoFile=$(find "$SRC/videoFile" -type f 2>/dev/null | wc -l) uploads=$(find "$SRC/uploads" -type f 2>/dev/null | wc -l)"
} >> "$LOG" 2>&1
notify "$rc" "$(offload_previous_rc)"
exit "$rc"
