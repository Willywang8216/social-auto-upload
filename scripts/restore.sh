#!/usr/bin/env bash
# restore.sh - Restore social-auto-upload from a backup archive
#
# Lists available backups from the rclone remote (or uses a local file),
# downloads and extracts the selected archive, then restores:
#   conf.py, .env, db/database.db, cookiesFile/, sau_frontend/dist/
#
# Before overwriting anything, a pre-restore safety snapshot is created
# in the local backups directory.
#
# Usage:
#   ./scripts/restore.sh                  # interactive: pick a backup
#   ./scripts/restore.sh --latest         # auto-select most recent backup
#   ./scripts/restore.sh --list           # list available backups and exit
#   ./scripts/restore.sh --file <path>    # restore from a local tar.gz
#   ./scripts/restore.sh --yes            # skip confirmation prompt
#
# Flags can be combined, e.g.: ./scripts/restore.sh --latest --yes

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROJECT_DIR="/home/will/social-auto-upload"
BACKUP_DIR="${PROJECT_DIR}/backups"
REMOTE="Onedrive-Yahooforsub-Tao:Scripts-ssh-ssl-keys/socialupload/"
ARCHIVE_PATTERN='socialupload-backup-.*\.tar\.gz'

# Items that a valid backup archive may contain (relative to PROJECT_DIR).
RESTORE_ITEMS=(
    "conf.py"
    ".env"
    "db/database.db"
    "cookiesFile"
    "sau_frontend/dist"
)

# ---------------------------------------------------------------------------
# Defaults (overridden by flags)
# ---------------------------------------------------------------------------
MODE="interactive"   # interactive | latest | list | file
LOCAL_FILE=""
AUTO_YES=false

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
usage() {
    cat <<'USAGE'
Usage: restore.sh [OPTIONS]

Restore social-auto-upload from a backup archive stored on rclone or a local
tar.gz file.

Options:
  --latest         Auto-select the most recent backup (no prompt)
  --list           List available backups and exit
  --file <path>    Restore from a local tar.gz file instead of rclone
  --yes            Skip the confirmation prompt before overwriting
  -h, --help       Show this help message
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --latest)
            MODE="latest"
            shift
            ;;
        --list)
            MODE="list"
            shift
            ;;
        --file)
            [[ $# -ge 2 ]] || { echo "Error: --file requires a path argument" >&2; exit 1; }
            MODE="file"
            LOCAL_FILE="$2"
            shift 2
            ;;
        --yes)
            AUTO_YES=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Error: unknown argument '$1'" >&2
            echo "Run '$0 --help' for usage." >&2
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()   { echo "[restore] $*"; }
warn()  { echo "[restore] WARNING: $*" >&2; }
die()   { echo "[restore] ERROR: $*" >&2; exit 1; }

confirm() {
    if [[ "$AUTO_YES" == true ]]; then
        return 0
    fi
    local prompt="${1:-Continue?}"
    read -rp "${prompt} [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]]
}

# Cleanup handler: remove temp directory on exit.
TEMP_DIR=""
cleanup() {
    if [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]]; then
        rm -rf "$TEMP_DIR"
    fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
command -v tar    >/dev/null 2>&1 || die "'tar' is not installed or not in PATH"
command -v rclone >/dev/null 2>&1 || die "'rclone' is not installed or not in PATH"
[[ -d "$PROJECT_DIR" ]]           || die "Project directory not found: ${PROJECT_DIR}"
mkdir -p "$BACKUP_DIR"

# ---------------------------------------------------------------------------
# List backups on remote
# ---------------------------------------------------------------------------
list_remote_backups() {
    # Returns a sorted list of backup filenames (newest last), one per line.
    # Prints nothing (and returns 0) if the remote path has no matching files.
    rclone lsf "$REMOTE" --files-only --format "p" 2>/dev/null \
        | grep -E "^${ARCHIVE_PATTERN}$" \
        | sort \
        || true
}

# ---------------------------------------------------------------------------
# MODE: list -- just print available backups and exit
# ---------------------------------------------------------------------------
if [[ "$MODE" == "list" ]]; then
    log "Available backups on ${REMOTE}"
    backups=$(list_remote_backups)
    if [[ -z "$backups" ]]; then
        log "(none found -- the remote path may not exist yet)"
        exit 0
    fi
    idx=0
    while IFS= read -r name; do
        idx=$(( idx + 1 ))
        # Extract timestamp portion for display: socialupload-backup-YYYYMMDD-HHMMSS.tar.gz
        ts=$(echo "$name" | grep -oE '[0-9]{8}-[0-9]{6}' || echo "unknown")
        printf "  %2d.  %s  (%s)\n" "$idx" "$name" "$ts"
    done <<< "$backups"
    log "Total: ${idx} backup(s)"
    exit 0
fi

# ---------------------------------------------------------------------------
# MODE: file -- restore from a local archive
# ---------------------------------------------------------------------------
if [[ "$MODE" == "file" ]]; then
    [[ -f "$LOCAL_FILE" ]] || die "File not found: ${LOCAL_FILE}"
    ARCHIVE_NAME="$(basename "$LOCAL_FILE")"
    log "Restoring from local file: ${LOCAL_FILE}"
fi

# ---------------------------------------------------------------------------
# MODE: interactive / latest -- select a backup from remote
# ---------------------------------------------------------------------------
if [[ "$MODE" == "interactive" || "$MODE" == "latest" ]]; then
    log "Listing backups on remote..."
    backups=$(list_remote_backups)

    if [[ -z "$backups" ]]; then
        die "No backups found on remote '${REMOTE}'. The path may not exist yet."
    fi

    # Build an indexed array for selection.
    mapfile -t backup_list <<< "$backups"
    backup_count=${#backup_list[@]}

    if [[ "$MODE" == "latest" ]]; then
        # Last entry after sort = most recent.
        SELECTED="${backup_list[$(( backup_count - 1 ))]}"
        log "Auto-selected latest backup: ${SELECTED}"
    else
        # Interactive: show numbered list, let user pick.
        echo ""
        log "Available backups:"
        for i in "${!backup_list[@]}"; do
            num=$(( i + 1 ))
            ts=$(echo "${backup_list[$i]}" | grep -oE '[0-9]{8}-[0-9]{6}' || echo "unknown")
            marker=""
            if (( num == backup_count )); then
                marker=" (latest)"
            fi
            printf "  %2d.  %s  (%s)%s\n" "$num" "${backup_list[$i]}" "$ts" "$marker"
        done
        echo ""

        default_num=$backup_count
        read -rp "Select backup [1-${backup_count}] (default: ${default_num} = latest): " selection
        selection="${selection:-$default_num}"

        # Validate input is a number in range.
        if ! [[ "$selection" =~ ^[0-9]+$ ]] || (( selection < 1 || selection > backup_count )); then
            die "Invalid selection: '${selection}'. Must be 1-${backup_count}."
        fi

        SELECTED="${backup_list[$(( selection - 1 ))]}"
        log "Selected: ${SELECTED}"
    fi

    ARCHIVE_NAME="$SELECTED"
fi

# ---------------------------------------------------------------------------
# Download the archive (if from remote)
# ---------------------------------------------------------------------------
if [[ "$MODE" != "file" ]]; then
    TEMP_DIR=$(mktemp -d -t socialupload-restore-XXXXXX)
    DOWNLOAD_PATH="${TEMP_DIR}/${ARCHIVE_NAME}"

    log "Downloading ${ARCHIVE_NAME} from remote..."
    if ! rclone copy "${REMOTE}${ARCHIVE_NAME}" "$TEMP_DIR" 2>&1; then
        die "Failed to download ${ARCHIVE_NAME} from ${REMOTE}"
    fi

    [[ -f "$DOWNLOAD_PATH" ]] || die "Download succeeded but file not found at ${DOWNLOAD_PATH}"

    archive_size=$(du -h "$DOWNLOAD_PATH" | cut -f1)
    log "Downloaded: ${DOWNLOAD_PATH} (${archive_size})"
else
    # For local files, still extract to a temp dir.
    TEMP_DIR=$(mktemp -d -t socialupload-restore-XXXXXX)
    DOWNLOAD_PATH="$LOCAL_FILE"
fi

# ---------------------------------------------------------------------------
# Extract archive
# ---------------------------------------------------------------------------
EXTRACT_DIR="${TEMP_DIR}/extracted"
mkdir -p "$EXTRACT_DIR"

log "Extracting archive..."
tar -xzf "$DOWNLOAD_PATH" -C "$EXTRACT_DIR" 2>&1 || die "Failed to extract archive (is it a valid tar.gz?)"

log "Archive contents:"
# Show top-level items in the extract dir.
find "$EXTRACT_DIR" -maxdepth 2 -mindepth 1 | sed "s|${EXTRACT_DIR}/|  |" | sort
echo ""

# ---------------------------------------------------------------------------
# Check what the archive actually contains
# ---------------------------------------------------------------------------
found_items=()
missing_items=()

for item in "${RESTORE_ITEMS[@]}"; do
    if [[ -e "${EXTRACT_DIR}/${item}" ]]; then
        found_items+=("$item")
    else
        missing_items+=("$item")
    fi
done

if (( ${#found_items[@]} == 0 )); then
    die "Archive contains none of the expected items. Is this the right backup?"
fi

if (( ${#missing_items[@]} > 0 )); then
    warn "Archive is missing these items (will be skipped): ${missing_items[*]}"
fi

log "Will restore: ${found_items[*]}"

# ---------------------------------------------------------------------------
# Confirm before overwriting
# ---------------------------------------------------------------------------
echo ""
log "This will overwrite the following in ${PROJECT_DIR}:"
for item in "${found_items[@]}"; do
    target="${PROJECT_DIR}/${item}"
    if [[ -e "$target" ]]; then
        echo "  [OVERWRITE] ${item}"
    else
        echo "  [CREATE]    ${item}"
    fi
done
echo ""

if ! confirm "Proceed with restore?"; then
    log "Restore cancelled by user."
    exit 0
fi

# ---------------------------------------------------------------------------
# Pre-restore safety backup
# ---------------------------------------------------------------------------
SAFETY_TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
SAFETY_NAME="pre-restore-${SAFETY_TIMESTAMP}.tar.gz"
SAFETY_PATH="${BACKUP_DIR}/${SAFETY_NAME}"

log "Creating pre-restore safety backup: ${SAFETY_NAME}"

safety_items=()
for item in "${found_items[@]}"; do
    if [[ -e "${PROJECT_DIR}/${item}" ]]; then
        safety_items+=("$item")
    fi
done

if (( ${#safety_items[@]} > 0 )); then
    tar -czf "$SAFETY_PATH" \
        -C "$PROJECT_DIR" \
        "${safety_items[@]}" \
        2>&1

    safety_size=$(du -h "$SAFETY_PATH" | cut -f1)
    log "Safety backup created: ${SAFETY_PATH} (${safety_size})"
else
    log "No existing files to back up (fresh install?)"
fi

# ---------------------------------------------------------------------------
# Restore files
# ---------------------------------------------------------------------------
log "Restoring files..."

restore_ok=true

for item in "${found_items[@]}"; do
    src="${EXTRACT_DIR}/${item}"
    dst="${PROJECT_DIR}/${item}"

    # For directories, ensure parent exists then replace.
    if [[ -d "$src" ]]; then
        # Remove old directory if it exists, then copy new one.
        if [[ -d "$dst" ]]; then
            rm -rf "$dst"
        fi
        mkdir -p "$(dirname "$dst")"
        cp -a "$src" "$dst"
        log "  Restored directory: ${item}/"
    elif [[ -f "$src" ]]; then
        mkdir -p "$(dirname "$dst")"
        cp -a "$src" "$dst"
        log "  Restored file: ${item}"
    else
        warn "  Skipping ${item}: not a regular file or directory"
    fi
done

# ---------------------------------------------------------------------------
# Post-restore verification
# ---------------------------------------------------------------------------
log "Verifying restore..."

verify_ok=true
for item in "${found_items[@]}"; do
    dst="${PROJECT_DIR}/${item}"
    if [[ ! -e "$dst" ]]; then
        warn "  MISSING after restore: ${item}"
        verify_ok=false
    fi
done

if [[ "$verify_ok" == true ]]; then
    log "All restored items verified."
else
    warn "Some items failed verification. You may need to restore manually from: ${SAFETY_PATH}"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
log "========================================="
log "  Restore complete"
log "========================================="
log "  Source:    ${ARCHIVE_NAME}"
log "  Restored:  ${found_items[*]}"
if (( ${#safety_items[@]} > 0 )); then
    log "  Safety:    ${SAFETY_PATH}"
    log "             (run restore.sh --file ${SAFETY_PATH} to undo)"
fi
log "========================================="

exit 0
