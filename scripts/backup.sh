#!/usr/bin/env bash
# backup.sh - Back up social-auto-upload config, database, cookies, and frontend dist
#              to a local directory and a rclone remote, with rotation.
#
# Usage:
#   ./scripts/backup.sh            # run backup
#   ./scripts/backup.sh --dry-run  # show what would happen without doing it

set -euo pipefail
umask 077

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROJECT_DIR="${SAU_PROJECT_DIR:-/home/will/social-auto-upload}"
PYTHON="${SAU_RELEASE_PYTHON:-python3}"
SQLITE_BACKUP="${PROJECT_DIR}/scripts/sqlite_backup.py"
BACKUP_DIR="${PROJECT_DIR}/backups"
LOG_FILE="${BACKUP_DIR}/backup.log"
REMOTE="Onedrive-Yahooforsub-Tao:Scripts-ssh-ssl-keys/socialupload/"
KEEP=5
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE_NAME="socialupload-backup-${TIMESTAMP}.tar.gz"
ARCHIVE_PATH="${BACKUP_DIR}/${ARCHIVE_NAME}"
DRY_RUN=false

# Files and directories to back up (paths relative to PROJECT_DIR).
# Items that don't exist at runtime are silently skipped.
BACKUP_ITEMS=(
    "conf.py"
    ".env"
    "db/database.db"
    "cookies"
    "cookiesFile"
    "secrets"
    "sau_frontend/dist"
    "docker-compose.yml"
)

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        -h|--help)
            cat <<'HELP'
Usage: backup.sh [--dry-run]

Back up social-auto-upload config, database, cookies, and frontend build
to a local directory and rclone remote, keeping the 5 most recent copies.

Options:
  --dry-run   Show what would happen without performing any actions
  -h, --help  Show this help message

Backed-up items:
  conf.py, .env, db/database.db, cookiesFile/, sau_frontend/dist/
HELP
            exit 0
            ;;
        *)
            echo "Error: unknown argument '${arg}'" >&2
            echo "Run '${0} --help' for usage." >&2
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg"
    if [[ "$DRY_RUN" == false ]]; then
        echo "$msg" >> "$LOG_FILE"
    fi
}

die() {
    log "ERROR: $*" >&2
    exit 1
}

# rotate_backups <local|remote>
#   Keep only the $KEEP most recent backup archives matching the naming
#   pattern in the given location.
rotate_backups() {
    local target="$1"

    if [[ "$target" == "local" ]]; then
        local count
        count=$(find "$BACKUP_DIR" -maxdepth 1 -name 'socialupload-backup-*.tar.gz' -type f 2>/dev/null | wc -l)
        if (( count > KEEP )); then
            local to_delete
            to_delete=$(find "$BACKUP_DIR" -maxdepth 1 -name 'socialupload-backup-*.tar.gz' -type f \
                        | sort \
                        | head -n "$(( count - KEEP ))")
            while IFS= read -r f; do
                [[ -z "$f" ]] && continue
                log "Rotating local backup: $(basename "$f")"
                if [[ "$DRY_RUN" == false ]]; then
                    rm -f "$f"
                    rm -f "${f}.sha256"
                fi
            done <<< "$to_delete"
        else
            log "Local rotation: ${count} backup(s), nothing to remove"
        fi
    else
        # Remote rotation via rclone lsf
        local remote_files
        remote_files=$(rclone lsf "$REMOTE" --files-only --format "p" 2>/dev/null \
                       | grep '^socialupload-backup-.*\.tar\.gz$' \
                       | sort) || true

        local rcount=0
        if [[ -n "$remote_files" ]]; then
            rcount=$(echo "$remote_files" | wc -l)
        fi

        if (( rcount > KEEP )); then
            local to_delete_remote
            to_delete_remote=$(echo "$remote_files" | head -n "$(( rcount - KEEP ))")
            while IFS= read -r f; do
                [[ -z "$f" ]] && continue
                log "Rotating remote backup: ${f}"
                if [[ "$DRY_RUN" == false ]]; then
                    rclone deletefile "${REMOTE}${f}" 2>&1 || log "WARNING: failed to delete remote file ${f}"
                    rclone deletefile "${REMOTE}${f}.sha256" 2>&1 || log "WARNING: no remote checksum sidecar for ${f}"
                fi
            done <<< "$to_delete_remote"
        else
            log "Remote rotation: ${rcount} backup(s), nothing to remove"
        fi
    fi
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
if [[ "$DRY_RUN" == true ]]; then
    log "=== DRY RUN MODE - no changes will be made ==="
fi


# Verify required tools
for cmd in tar rclone sha256sum flock "$PYTHON"; do
    command -v "$cmd" >/dev/null 2>&1 || die "'${cmd}' is not installed or not in PATH"
done

# Verify project directory
[[ -d "$PROJECT_DIR" ]] || die "Project directory not found: ${PROJECT_DIR}"

# Create local backup directory (and log file)
if [[ "$DRY_RUN" == false ]]; then
    mkdir -p "$BACKUP_DIR"
    touch "$LOG_FILE"
fi
log "Starting backup: ${ARCHIVE_NAME}"
[[ -f "${PROJECT_DIR}/db/database.db" ]] || die "Required database is missing"
TEMP_DIR=""
cleanup() { [[ -z "$TEMP_DIR" ]] || rm -rf "$TEMP_DIR"; }
trap cleanup EXIT
if [[ "$DRY_RUN" == false ]]; then
    mkdir -p "${PROJECT_DIR}/logs"
    exec 9>"${PROJECT_DIR}/logs/ci-watch.lock"
    flock -n 9 || die "Deploy/restore/backup lock is held; retry later"
fi

# ---------------------------------------------------------------------------
# Collect existing backup items (skip missing optional ones)
# ---------------------------------------------------------------------------
existing_items=()
missing_items=()

for item in "${BACKUP_ITEMS[@]}"; do
    full_path="${PROJECT_DIR}/${item}"
    if [[ -e "$full_path" ]]; then
        existing_items+=("$item")
    else
        missing_items+=("$item")
    fi
done

if (( ${#existing_items[@]} == 0 )); then
    die "No backup items found - nothing to back up"
fi

if (( ${#missing_items[@]} > 0 )); then
    log "WARNING: skipping missing items: ${missing_items[*]}"
fi

log "Backing up: ${existing_items[*]}"

# ---------------------------------------------------------------------------
# Create archive
# ---------------------------------------------------------------------------
if [[ "$DRY_RUN" == true ]]; then
    log "[dry-run] Would create archive: ${ARCHIVE_NAME} containing: ${existing_items[*]}"
else
    TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/sau-backup.XXXXXX")
    mkdir -p "${TEMP_DIR}/db"
    "$PYTHON" "$SQLITE_BACKUP" snapshot "${PROJECT_DIR}/db/database.db" "${TEMP_DIR}/db/database.db"
    "$PYTHON" "$SQLITE_BACKUP" verify "${TEMP_DIR}/db/database.db" --restore-check
    non_db_items=()
    for item in "${existing_items[@]}"; do
        [[ "$item" == "db/database.db" ]] || non_db_items+=("$item")
    done
    # Never archive the live DB or WAL directly. Only this online snapshot is
    # included; credential/config bytes and the existing remote are unchanged.
    tar -czf "${ARCHIVE_PATH}.tmp" -C "$PROJECT_DIR" "${non_db_items[@]}" \
        -C "$TEMP_DIR" db/database.db db/database.db.sha256
    mv "${ARCHIVE_PATH}.tmp" "$ARCHIVE_PATH"
    (cd "$BACKUP_DIR" && sha256sum "$ARCHIVE_NAME" >"${ARCHIVE_NAME}.sha256")

    archive_size=$(du -h "$ARCHIVE_PATH" | cut -f1)
    log "Archive created: ${ARCHIVE_PATH} (${archive_size})"
fi

# ---------------------------------------------------------------------------
# Upload to remote
# ---------------------------------------------------------------------------
if [[ "$DRY_RUN" == true ]]; then
    log "[dry-run] Would upload ${ARCHIVE_NAME} to ${REMOTE}"
else
    log "Uploading to remote: ${REMOTE}"
    if rclone copy "$ARCHIVE_PATH" "$REMOTE" 2>&1 && rclone copy "${ARCHIVE_PATH}.sha256" "$REMOTE" 2>&1; then
        log "Upload complete"
        # Purge local archive after successful upload
        log "Removing local archive: ${ARCHIVE_NAME}"
        rm -f "$ARCHIVE_PATH" "${ARCHIVE_PATH}.sha256"
    else
        die "rclone upload failed"
    fi
fi

# ---------------------------------------------------------------------------
# Rotation (remote only — local archives are deleted after upload)
# ---------------------------------------------------------------------------
log "Rotating remote backups (keeping ${KEEP} most recent)..."
rotate_backups "remote"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
if [[ "$DRY_RUN" == true ]]; then
    log "[dry-run] Would keep ${KEEP} most recent backups on remote"
    log "=== DRY RUN COMPLETE - no changes were made ==="
else
    local_count=$(find "$BACKUP_DIR" -maxdepth 1 -name 'socialupload-backup-*.tar.gz' -type f 2>/dev/null | wc -l)
    log "Local backups remaining: ${local_count}"
fi

log "Backup finished successfully"
exit 0
