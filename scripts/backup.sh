#!/usr/bin/env bash
# backup.sh - Back up social-auto-upload config, database, cookies, and frontend dist
#              to a local directory and a rclone remote, with rotation.
#
# Usage:
#   ./scripts/backup.sh            # run backup
#   ./scripts/backup.sh --dry-run  # show what would happen without doing it

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROJECT_DIR="/home/will/social-auto-upload"
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

log "Starting backup: ${ARCHIVE_NAME}"

# Verify required tools
for cmd in tar rclone; do
    command -v "$cmd" >/dev/null 2>&1 || die "'${cmd}' is not installed or not in PATH"
done

# Verify project directory
[[ -d "$PROJECT_DIR" ]] || die "Project directory not found: ${PROJECT_DIR}"

# Create local backup directory (and log file)
if [[ "$DRY_RUN" == false ]]; then
    mkdir -p "$BACKUP_DIR"
    touch "$LOG_FILE"
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
    tar -czf "$ARCHIVE_PATH" \
        -C "$PROJECT_DIR" \
        "${existing_items[@]}" \
        2>&1

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
    if rclone copy "$ARCHIVE_PATH" "$REMOTE" 2>&1; then
        log "Upload complete"
    else
        die "rclone upload failed"
    fi
fi

# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------
log "Rotating local backups (keeping ${KEEP} most recent)..."
rotate_backups "local"

log "Rotating remote backups (keeping ${KEEP} most recent)..."
rotate_backups "remote"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
if [[ "$DRY_RUN" == true ]]; then
    log "[dry-run] Would keep ${KEEP} most recent backups locally and on remote"
    log "=== DRY RUN COMPLETE - no changes were made ==="
else
    local_count=$(find "$BACKUP_DIR" -maxdepth 1 -name 'socialupload-backup-*.tar.gz' -type f 2>/dev/null | wc -l)
    log "Local backups: ${local_count}/${KEEP}"
fi

log "Backup finished successfully"
exit 0
