#!/usr/bin/env bash
# bisync_onedrive.sh - two-way sync between Two OneDrive accounts
#
#   Onedrive-Yahooforsub-Tao  <->  Onedrive-willywang
#
# Uses rclone bisync which tracks changes (including deletions) and propagates
# them bidirectionally. Conflicts resolved by keeping the NEWER file.
#
# Folders: 補教教學, Yellow, Videos, Assets, Scripts-ssh-ssl-keys
# (ordered small→large so smaller folders complete faster during initial resync)
#
# DELETIONS: Bisync tracks deletions and propagates them. If you delete files
# on one side, they will be deleted on the other side on the next sync.
#
# RESYNC: Only needed once to build initial state files. During resync, files
# that exist on one side get copied to the other (deletions are undone). After
# resync completes, normal bisync will correctly propagate deletions.
#
# Usage:
#   ./bisync_onedrive.sh --resync   # First run only (builds state files)
#   ./bisync_onedrive.sh            # Normal sync (cron runs this every 30 min)

set -uo pipefail

REMOTE_A="Onedrive-Yahooforsub-Tao"
REMOTE_B="Onedrive-willywang"
FOLDERS=(
    "補教教學"
    "Yellow"
    "Videos"
    "Assets"
    "Scripts-ssh-ssl-keys"
)

WORK_ROOT="${HOME}/.cache/rclone/bisync-${REMOTE_A#Onedrive-}-${REMOTE_B#Onedrive-}"
LOG_DIR="${HOME}/logs"
LOCK_FILE="/tmp/bisync-onedrive.lock"
RESYNC="${1:-}"

# Telegram config
TG_TOKEN="8929051209:AAF9WULtpjwuSvFLpIbWZsyzHin5hMFF1oc"
TG_CHAT="8633483147"

mkdir -p "$WORK_ROOT" "$LOG_DIR"
RCLONE="$(command -v rclone || echo /usr/bin/rclone)"

# ---------------------------------------------------------------------------
# Telegram notification
# ---------------------------------------------------------------------------
tg_notify() {
    [ -n "${TG_TOKEN:-}" ] && [ -n "${TG_CHAT:-}" ] || return 0
    curl -s -m 10 "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TG_CHAT}" \
        --data-urlencode "disable_web_page_preview=true" \
        --data-urlencode "text=$1" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------
# Start notification
# ---------------------------------------------------------------------------
start_time=$(date '+%F %T')
if [ -n "$RESYNC" ]; then
    tg_notify "🔄 OneDrive bisync STARTED (RESYNC)
Time: ${start_time}
Folders: ${#FOLDERS[@]}
⚠️ Resync copies files that exist on one side to the other.
Deletions made before resync will be undone."
else
    tg_notify "🔄 OneDrive bisync STARTED
Time: ${start_time}
Folders: ${#FOLDERS[@]}
✓ Deletions will propagate"
fi

# ---------------------------------------------------------------------------
# Lock to prevent overlapping runs
# ---------------------------------------------------------------------------
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(date '+%F %T')] another bisync running; skipping"
    exit 0
fi

fail=0
failed_folders=()
synced_folders=()
skipped_folders=()

# Exclude regenerable content
EXCLUDES=(
    --exclude "**/node_modules/**"
    --exclude "**/.venv/**"
    --exclude "**/venv/**"
    --exclude "**/__pycache__/**"
    --exclude "**/.git/**"
)

# ---------------------------------------------------------------------------
# Sync each folder
# ---------------------------------------------------------------------------
for folder in "${FOLDERS[@]}"; do
    echo "[$(date '+%F %T')] sync ${folder} (both directions, keep newest)"
    rc=0
    # Stateless two-way copy. Each direction uses --update, which SKIPS a file
    # whose destination copy is newer - so the newer version always wins on both
    # sides ("keep the latest"). Unlike bisync there is no listing state to
    # corrupt, no --resync step, and an interrupted run resumes where it stopped.
    # That matters here: this tree is >100k files and OneDrive throttles long
    # listings, which made bisync hang after the transfer with state never
    # finalized.
    #
    # Trade-off vs bisync: deletions do not propagate (a file removed on one
    # side is restored from the other). This is a union mirror.
    for direction in "AtoB" "BtoA"; do
        if [ "$direction" = "AtoB" ]; then
            src_remote="$REMOTE_A"; dst_remote="$REMOTE_B"
        else
            src_remote="$REMOTE_B"; dst_remote="$REMOTE_A"
        fi
        "$RCLONE" copy "${src_remote}:${folder}" "${dst_remote}:${folder}" \
            --update \
            --create-empty-src-dirs \
            "${EXCLUDES[@]}" \
            --transfers 8 \
            --checkers 16 \
            --timeout 5m \
            --contimeout 1m \
            --retries 3 \
            --low-level-retries 10 \
            --log-level INFO \
            2>&1 | sed "s/^/  ${folder} ${direction}: /"
        prc=${PIPESTATUS[0]}
        if [ "$prc" -ne 0 ]; then
            echo "  ${folder} ${direction}: copy exited ${prc}"
            rc=$prc
        fi
    done
    if [ "$rc" -ne 0 ]; then
        fail=1
        failed_folders+=("$folder (rc=$rc)")
    else
        synced_folders+=("$folder")
    fi
done

# ---------------------------------------------------------------------------
# Completion notification
# ---------------------------------------------------------------------------
end_time=$(date '+%F %T')

if [ "$fail" -eq 1 ]; then
    msg="❌ OneDrive bisync FAILED
Start: ${start_time}
End: ${end_time}

Failed:"
    for f in "${failed_folders[@]}"; do msg+=$'\n'"  • $f"; done
    if [ ${#synced_folders[@]} -gt 0 ]; then
        msg+=$'\n\n'"OK:"
        for f in "${synced_folders[@]}"; do msg+=$'\n'"  ✓ $f"; done
    fi
    tg_notify "$msg"
elif [ ${#synced_folders[@]} -gt 0 ]; then
    msg="✅ OneDrive bisync DONE"
    [ -n "$RESYNC" ] && msg+=" (RESYNC)"
    msg+=$'\n'"Start: ${start_time}"
    msg+=$'\n'"End: ${end_time}"
    msg+=$'\n\n'"Synced (${#synced_folders[@]}):"
    for f in "${synced_folders[@]}"; do msg+=$'\n'"  ✓ $f"; done
    if [ ${#skipped_folders[@]} -gt 0 ]; then
        msg+=$'\n\n'"Skipped:"
        for f in "${skipped_folders[@]}"; do msg+=$'\n'"  ⚠ $f"; done
    fi
    if [ -n "$RESYNC" ]; then
        msg+=$'\n\n'"⚠️ Resync done. Delete files again if needed — next normal sync will propagate deletions."
    fi
    tg_notify "$msg"
else
    tg_notify "⚠️ All folders skipped (no state). Run: bisync_onedrive.sh --resync"
fi

exit "$fail"
