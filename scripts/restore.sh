#!/usr/bin/env bash
# Offline disaster recovery, NOT a deployment rollback. No services are stopped
# or started automatically; --yes only skips the human prompt, never safety gates.
set -euo pipefail
umask 077
PROJECT_DIR="${SAU_PROJECT_DIR:-/home/will/social-auto-upload}"
BACKUP_DIR="${PROJECT_DIR}/backups"
REMOTE="Onedrive-Yahooforsub-Tao:Scripts-ssh-ssl-keys/socialupload/"
ARCHIVE_PATTERN='socialupload-backup-.*\.tar\.gz'
PYTHON="${SAU_RELEASE_PYTHON:-python3}"
SQLITE_BACKUP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/sqlite_backup.py"
# Preserve the existing restore scope; archives may additionally contain cookies,
# secrets or compose files, but those are not silently applied to a live release.
RESTORE_ITEMS=("conf.py" ".env" "db/database.db" "cookiesFile" "sau_frontend/dist")
MODE=interactive LOCAL_FILE="" AUTO_YES=0 VERIFY_ONLY=0 OFFLINE=0 LEGACY_ARCHIVE=0
usage() {
  cat <<'HELP'
Usage: restore.sh [--latest|--list|--file PATH] [--verify-only|--offline] [--yes]
  --verify-only / --dry-run   Verify checksums, SQLite and an isolated restore only
  --offline                  Confirm ALL web/worker/cron/CLI DB writers are stopped
  --yes                      Skip confirmation, not offline/lock/integrity checks
  --legacy-archive            Explicitly accept an old archive without checksums;
                             create and verify a new isolated SQLite snapshot first

A restore leaves services stopped and db/publish.pause in place. Never use this
as automatic rollback after publishing may have produced remote side effects.
HELP
}
while (( $# )); do
  case "$1" in
    --latest) MODE=latest; shift ;;
    --list) MODE=list; shift ;;
    --file) LOCAL_FILE="$2"; MODE=file; shift 2 ;;
    --yes) AUTO_YES=1; shift ;;
    --verify-only|--dry-run) VERIFY_ONLY=1; shift ;;
    --offline) OFFLINE=1; shift ;;
    --legacy-archive) LEGACY_ARCHIVE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown restore option: $1" >&2; exit 2 ;;
  esac
done
log() { printf '[restore] %s\n' "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }
command -v "$PYTHON" >/dev/null || die 'Python is required'
[[ -d "$PROJECT_DIR" ]] || die 'Project directory is missing'
if [[ "$MODE" != file ]]; then command -v rclone >/dev/null || die 'rclone is required'; fi
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/socialupload-restore.XXXXXX")"
trap 'rm -rf "$TEMP_DIR"' EXIT
list_remote_backups() {
  rclone lsf "$REMOTE" --files-only --format p | grep -E "^${ARCHIVE_PATTERN}$" | sort || true
}
if [[ "$MODE" == list ]]; then
  log "Available backups on $REMOTE"
  list_remote_backups
  exit 0
fi
if [[ "$MODE" == file ]]; then
  [[ -f "$LOCAL_FILE" ]] || die 'Local archive not found'
  DOWNLOAD_PATH="$LOCAL_FILE"
  ARCHIVE_NAME="$(basename "$LOCAL_FILE")"
else
  backups="$(list_remote_backups)"
  [[ -n "$backups" ]] || die 'No remote backups found'
  mapfile -t choices <<<"$backups"
  count=${#choices[@]}
  selection=$count
  if [[ "$MODE" == interactive ]]; then
    for index in "${!choices[@]}"; do printf '  %d. %s\n' "$((index + 1))" "${choices[$index]}"; done
    read -rp "Select backup [1-${count}] (default latest): " selection
    selection="${selection:-$count}"
  fi
  [[ "$selection" =~ ^[0-9]+$ ]] && (( selection >= 1 && selection <= count )) || die 'Invalid selection'
  ARCHIVE_NAME="${choices[$((selection - 1))]}"
  rclone copy "${REMOTE}${ARCHIVE_NAME}" "$TEMP_DIR"
  rclone copy "${REMOTE}${ARCHIVE_NAME}.sha256" "$TEMP_DIR" >/dev/null 2>&1 || true
  DOWNLOAD_PATH="${TEMP_DIR}/${ARCHIVE_NAME}"
fi
if [[ -f "${DOWNLOAD_PATH}.sha256" ]]; then
  # Do not feed untrusted checksum filenames to sha256sum -c.
  "$PYTHON" - "$DOWNLOAD_PATH" <<'PY'
import hashlib, pathlib, sys
path = pathlib.Path(sys.argv[1])
fields = pathlib.Path(str(path) + '.sha256').read_text(encoding='ascii').strip().split(maxsplit=1)
if len(fields) != 2 or fields[1] != path.name:
    raise SystemExit('Invalid archive checksum manifest')
digest = hashlib.sha256()
with path.open('rb') as stream:
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(block)
if digest.hexdigest() != fields[0]:
    raise SystemExit('Archive checksum mismatch')
PY
else
  (( LEGACY_ARCHIVE == 1 )) || die 'Archive checksum missing; use isolated verification and explicit --legacy-archive for an old backup'
fi
EXTRACT_DIR="$TEMP_DIR/extracted"
mkdir -p "$EXTRACT_DIR"
# Python 3.10 lacks the newer tar extraction filter. Explicitly reject traversal,
# links and devices BEFORE extracting any member into our empty private tempdir.
"$PYTHON" - "$DOWNLOAD_PATH" "$EXTRACT_DIR" <<'PY'
import pathlib, sys, tarfile
with tarfile.open(sys.argv[1], 'r:gz') as archive:
    members = archive.getmembers()
    for member in members:
        name = pathlib.PurePosixPath(member.name)
        if name.is_absolute() or '..' in name.parts or not (member.isfile() or member.isdir()):
            raise SystemExit('Unsafe archive member; extraction refused')
    archive.extractall(sys.argv[2], members=members)
PY
SNAPSHOT="$EXTRACT_DIR/db/database.db"
[[ -f "$SNAPSHOT" ]] || die 'Archive has no SQLite database'
if [[ ! -f "${SNAPSHOT}.sha256" ]]; then
  (( LEGACY_ARCHIVE == 1 )) || die 'Snapshot checksum is missing'
  log 'Legacy archive: no historical checksum assurance; verifying a new isolated online copy.'
  "$PYTHON" "$SQLITE_BACKUP" snapshot "$SNAPSHOT" "$TEMP_DIR/legacy/database.db"
  SNAPSHOT="$TEMP_DIR/legacy/database.db"
fi
"$PYTHON" "$SQLITE_BACKUP" verify "$SNAPSHOT" --restore-check
if (( VERIFY_ONLY == 1 )); then
  log 'Checksums/quick_check/isolated restore passed. No runtime files or services changed.'
  exit 0
fi
(( OFFLINE == 1 )) || die 'Restore requires --offline; --yes is not permission to replace a running DB'
[[ "$(uname -s)" == Linux ]] || die 'Offline writer checks require Linux'
for command in docker flock fuser tar sha256sum; do command -v "$command" >/dev/null || die "$command is required"; done
mkdir -p "$PROJECT_DIR/logs" "$BACKUP_DIR"
exec 9>"$PROJECT_DIR/logs/ci-watch.lock"
flock -n 9 || die 'Watcher/deploy/backup holds the release lock'
DB="$PROJECT_DIR/db/database.db"
[[ ! -L "$PROJECT_DIR/db" && ! -L "$DB" ]] || die 'Refusing symlinked database destination'
assert_offline() {
  local ids paths=() path rc
  ids="$(docker ps -q)" || die 'Cannot establish Docker writer state'
  if [[ -n "$ids" ]]; then
    local containers=()
    mapfile -t containers <<<"$ids"
    # Check EVERY running container with a mount containing this DB, not only
    # the standard web/worker names. Only mount paths (no secrets) are inspected.
    docker inspect --format '{{json .Mounts}}' "${containers[@]}" | "$PYTHON" -c '
import json, pathlib, sys
db = pathlib.Path(sys.argv[1]).resolve()
for line in sys.stdin:
    for mount in json.loads(line):
        source = pathlib.Path(mount["Source"]).resolve()
        if source == db or source in db.parents:
            raise SystemExit("A running container can access this database")
' "$DB" || die 'Stop all DB-sharing containers before restore'
  fi
  for path in "$DB" "$DB-wal" "$DB-shm" "$DB-journal"; do
    [[ ! -e "$path" ]] || paths+=("$path")
  done
  if (( ${#paths[@]} )); then
    if fuser "${paths[@]}" >/dev/null 2>&1; then
      die 'Database has open host handles; stop cron/CLI/other writers first'
    else
      rc=$?
      (( rc == 1 )) || die 'Cannot establish host writer state'
    fi
  fi
}
assert_offline
found_items=()
for item in "${RESTORE_ITEMS[@]}"; do
  [[ ! -e "$EXTRACT_DIR/$item" ]] || found_items+=("$item")
done
log "Offline restore scope: ${found_items[*]}"
if (( AUTO_YES == 0 )); then
  read -rp 'All writers must stay stopped; replace these files? [y/N] ' answer
  [[ "$answer" =~ ^[Yy]$ ]] || { log 'Cancelled'; exit 0; }
fi
[[ ! -L "$PROJECT_DIR/db/publish.pause" ]] || die 'Unknown pause symlink'
if [[ ! -e "$PROJECT_DIR/db/publish.pause" ]]; then
  (set -o noclobber; printf 'offline-restore\n' >"$PROJECT_DIR/db/publish.pause")
fi
# Safety archive preserves the original scope and location, but never tars the
# live SQLite file. Keep both the verified archive and original offline files.
SAFETY_NAME="pre-restore-$(date +%Y%m%d-%H%M%S).tar.gz"
SAFETY_PATH="$BACKUP_DIR/$SAFETY_NAME"
SAFETY_STAGE="$TEMP_DIR/safety"
mkdir -p "$SAFETY_STAGE"
safety_items=()
for item in "${found_items[@]}"; do
  [[ "$item" == db/database.db || ! -e "$PROJECT_DIR/$item" ]] || safety_items+=("$item")
done
snapshot_items=()
if [[ -f "$DB" ]]; then
  "$PYTHON" "$SQLITE_BACKUP" snapshot "$DB" "$SAFETY_STAGE/db/database.db"
  "$PYTHON" "$SQLITE_BACKUP" verify "$SAFETY_STAGE/db/database.db" --restore-check
  snapshot_items=(db/database.db db/database.db.sha256)
fi
if (( ${#safety_items[@]} + ${#snapshot_items[@]} )); then
  tar -czf "$SAFETY_PATH" -C "$PROJECT_DIR" "${safety_items[@]}" -C "$SAFETY_STAGE" "${snapshot_items[@]}"
  (cd "$BACKUP_DIR" && sha256sum "$SAFETY_NAME" >"${SAFETY_NAME}.sha256")
fi
assert_offline
# Move (not copy) the stopped original DB + sidecars out of the destination.
# restore() itself still refuses existing files, sidecars and unsafe destinations.
QUARANTINE="$(mktemp -d "$PROJECT_DIR/db/.pre-restore.XXXXXX")"
for path in "$DB" "$DB-wal" "$DB-shm" "$DB-journal" "$DB.sha256"; do
  [[ ! -e "$path" ]] || mv -- "$path" "$QUARANTINE/"
done
"$PYTHON" "$SQLITE_BACKUP" restore "$SNAPSHOT" "$DB" --offline
for item in "${found_items[@]}"; do
  [[ "$item" != db/database.db ]] || continue
  src="$EXTRACT_DIR/$item"; dst="$PROJECT_DIR/$item"
  [[ ! -L "$dst" ]] || die 'Refusing symlinked config/content destination'
  if [[ -d "$src" && -d "$dst" ]]; then rm -rf "$dst"; fi
  mkdir -p "$(dirname "$dst")"
  cp -a "$src" "$dst"
  if [[ "$item" == .env || "$item" == conf.py ]]; then chmod 600 "$dst"; fi
  log "Restored $item"
done
"$PYTHON" "$SQLITE_BACKUP" verify "$DB"
log "Offline restore verified. Safety archive: $SAFETY_PATH; original DB files: $QUARANTINE"
log 'Services remain stopped and claims paused. Review delivery side effects and migrate/verify the chosen image before any restart.'
