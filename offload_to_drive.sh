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
# tiny log rotation
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
  tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
RCLONE_BIN=${RCLONE_BIN:-/usr/bin/rclone}
RC=("$RCLONE_BIN" --config "$CONF")
ts(){ date -u +%Y-%m-%dT%H:%M:%SZ; }
rc=0

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
    # rclone check needs DIRECTORIES on both sides: passing the file itself as
    # the destination aborts with "is a file not a directory", which silently
    # disabled this whole purge (every rc=1 offload). Compare the two roots
    # with an include scoped to this one file instead. --one-way because the
    # only property we need is "the bytes we still hold are safely on Drive".
    if "${RC[@]}" check "$source_root" "$remote_root" --size-only --one-way \
         --include "/$relative" >/dev/null 2>&1; then
      # One un-unlinkable file must not block cleanup of every other one, so
      # record the failure and keep going.
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
    if "/videoFile/" in local_path:
        emit(local_path.split("/videoFile/", 1)[1])
    elif "/uploads/" in local_path:
        emit(local_path.split("/uploads/", 1)[1])


def emit_file_ref(ref: str) -> None:
    """Emit a target's file_ref (legacy direct targets store a path here;
    campaign targets store campaign_post:<id> and are skipped)."""
    ref = (ref or "").strip()
    if not ref or ref.startswith("campaign_post:"):
        return
    for marker in ("/videoFile/", "/uploads/"):
        if marker in ref:
            emit(ref.split(marker, 1)[1])
            return
    if ref.startswith("videoFile/"):
        emit(ref[len("videoFile/"):])
    elif ref.startswith("uploads/"):
        emit(ref[len("uploads/"):])
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

# Remember where the offloaded files went. Runs against the SQLite file the
# container mounts, and only records a path the remote actually has while the
# local copy is gone — i.e. exactly the files this run (or an earlier one)
# successfully moved. Exits non-zero when files are missing locally but could
# NOT be registered (remote listing failed / storage_backends row absent):
# a silent skip there would leave moved files unrestorable, so it must page
# the operator instead.
register_offloaded() {
  python3 - "$DB" "$DST" "$CONF" "$SRC" <<'PY'
import sqlite3
import subprocess
import sys
from pathlib import Path

db_path, dst, conf, src = sys.argv[1:5]
src = Path(src)

# file_records.file_path -> (offload root, key within that root, local file)
def locate(file_path: str):
    if file_path.startswith("uploads/"):
        return "uploads", file_path[len("uploads/"):], src / "uploads" / file_path[len("uploads/"):]
    if file_path.startswith("generated/"):
        return "generated", file_path[len("generated/"):], src / "generated" / file_path[len("generated/"):]
    if file_path.startswith("videoFile/"):
        return "videoFile", file_path[len("videoFile/"):], src / "videoFile" / file_path[len("videoFile/"):]
    return "videoFile", file_path, src / "videoFile" / file_path


def remote_index(root: str):
    """Remote file list for one root, or None when rclone itself failed."""
    proc = subprocess.run(
        ["rclone", "--config", conf, "lsf", "-R", "--files-only", f"{dst}/{root}"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT id, file_path, storage_key FROM file_records").fetchall()

by_root = {"videoFile": set(), "uploads": set(), "generated": set()}
for row in rows:
    root, key, local = locate(row["file_path"] or "")
    if local.exists() or not key:
        continue
    by_root.setdefault(root, set()).add(key)

registered = 0
unregisterable = 0
for root, keys in by_root.items():
    if not keys:
        continue
    present = remote_index(root)
    if not present:
        print(f"ERROR: remote listing failed for {dst}/{root} "
              f"while {len(keys)} local file(s) are missing", file=sys.stderr)
        unregisterable += len(keys)
        continue
    backend_row = conn.execute(
        "SELECT id FROM storage_backends WHERE provider='rclone' AND bucket=? AND endpoint=?",
        (dst.split(":", 1)[0], f"sau/{root}"),
    ).fetchone()
    if backend_row is None:
        print(f"ERROR: no storage_backends row for {dst.split(':', 1)[0]} "
              f"sau/{root}; {len(keys)} moved file(s) cannot be registered",
              file=sys.stderr)
        unregisterable += len(keys)
        continue
    backend_id = backend_row[0]
    for row in rows:
        r_root, key, local = locate(row["file_path"] or "")
        if r_root != root or not key or key not in present or local.exists():
            continue
        if row["storage_key"] == key and row["storage_key"] is not None:
            continue  # already registered by an earlier run
        conn.execute(
            "UPDATE file_records SET storage_key=?, storage_backend_id=? WHERE id=?",
            (key, backend_id, row["id"]),
        )
        registered += 1
conn.commit()
print(f"registered {registered} offloaded file(s) in file_records")
if unregisterable:
    sys.exit(1)
PY
}

{
  echo "[$(ts)] offload start"
  EXCLUDES=$(mktemp)
  if pending_excludes > "$EXCLUDES" && grep -q '^# excludes=' "$EXCLUDES"; then
          KEEP_COUNT=$(grep -vc '^#' "$EXCLUDES" || true)
    echo "[$(ts)] keeping ${KEEP_COUNT} file(s) local (in-flight / due soon / unrecorded / _library)"
    for d in videoFile uploads; do
      [ -d "$SRC/$d" ] || continue
      source_root="$SRC/$d"
      destination_root="$DST/$d"
      if [ "$d" = videoFile ]; then
        rclone_source="$SRC/videoFile"
        rclone_destination="$DST/videoFile"
      else
        rclone_source="$SRC/uploads"
        rclone_destination="$DST/uploads"
      fi
      # copy, never move: this cron runs as `will` but the SAU container writes
      # videoFile/ as root, so a move aborts with "permission denied" on every
      # container-written source (which is most of them). Deletion is the
      # separate, verified step below.
      if "${RC[@]}" copy "$rclone_source" "$rclone_destination" --min-age 10m --exclude-from "$EXCLUDES" \
        --transfers 4 --checkers 8 --stats-one-line -v 2>&1; then
        if ! purge_verified_sources "$source_root" "$destination_root"; then
          echo "[$(ts)] could not remove all verified sources under $source_root"
          rc=1
        fi
      else
        rc=$?
      fi
    done
    if ! register_offloaded 2>&1; then
      echo "[$(ts)] WARNING: could not register offloaded files in file_records"
      rc=4
    fi
  else
    echo "[$(ts)] SKIPPED offload: could not build the keep-local exclude list"
    rc=3
  fi
  rm -f "$EXCLUDES"
  echo "[$(ts)] offload done rc=$rc local videoFile=$(find "$SRC/videoFile" -type f 2>/dev/null | wc -l) uploads=$(find "$SRC/uploads" -type f 2>/dev/null | wc -l)"
} >> "$LOG" 2>&1
notify "$rc" "$(offload_previous_rc)"
exit "$rc"
