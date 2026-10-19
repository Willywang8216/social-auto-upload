#!/usr/bin/env bash
# bisync_onedrive.sh - two-way sync of the shared folders between Two OneDrive
# remotes, so a change on either side lands on the other.
#
#   Onedrive-Yahooforsub-Tao  <->  Onedrive-willywang
#
# Synced folders: Scripts-ssh-ssl-keys, Assets, Videos, Yellow, 補教教學
#
# rclone "sync" is one-way and would delete the other side's new files, so this
# uses "bisync". Conflicts are resolved by keeping the NEWER copy
# (--conflict-resolve newer / --resync-mode newer), matching "keep the latest".
#
# First run (or after changing the folder list) MUST resync to build the state:
#     scripts/bisync_onedrive.sh --resync
# After that, run it plain (cron does this every 30 minutes).
#
# Safety: --max-delete stops a run that would remove a suspicious number of
# files, and a lock prevents overlapping runs.
#
# Telegram notifications: sends start/finish/error messages to the configured
# chat so you know when sync runs and if anything goes wrong.

set -uo pipefail

REMOTE_A="Onedrive-Yahooforsub-Tao"
REMOTE_B="Onedrive-willywang"
FOLDERS=(
    "Scripts-ssh-ssl-keys"
    "Assets"
    "Videos"
    "Yellow"
    "補教教學"
)

WORK_ROOT="${HOME}/.cache/rclone/bisync-${REMOTE_A#Onedrive-}-${REMOTE_B#Onedrive-}"
LOG_DIR="${HOME}/logs"
LOCK_FILE="/tmp/bisync-onedrive.lock"
RESYNC="${1:-}"

# Telegram notification config (read from the environment or .env; never
# commit the token - this repository is public).
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "${REPO_DIR}/.env" ]; then
    TG_TOKEN="${TG_TOKEN:-$(grep -m1 '^SAU_BISYNC_TG_TOKEN=' "${REPO_DIR}/.env" | cut -d= -f2-)}"
    TG_CHAT="${TG_CHAT:-$(grep -m1 '^SAU_BISYNC_TG_CHAT=' "${REPO_DIR}/.env" | cut -d= -f2-)}"
fi
TG_TOKEN="${TG_TOKEN:-${TELEGRAM_BOT_TOKEN:-${TG_BOT_TOKEN:-}}}"
TG_CHAT="${TG_CHAT:-${TELEGRAM_CHAT_ID:-${TG_CHAT_ID:-}}}"

mkdir -p "$WORK_ROOT" "$LOG_DIR"
RCLONE="$(command -v rclone || echo /usr/bin/rclone)"

# ---------------------------------------------------------------------------
# Telegram notification helper
# ---------------------------------------------------------------------------
tg_notify() {
    local message="$1"
    [ -n "${TG_TOKEN:-}" ] && [ -n "${TG_CHAT:-}" ] || return 0
    curl -s -m 10 "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TG_CHAT}" \
        --data-urlencode "disable_web_page_preview=true" \
        --data-urlencode "text=${message}" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------
# Send start notification
# ---------------------------------------------------------------------------
start_time=$(date '+%F %T')
if [ -n "$RESYNC" ]; then
    tg_notify "🔄 OneDrive bisync STARTED (RESYNC mode)
Time: ${start_time}
Folders: ${#FOLDERS[@]}
Mode: Two-way sync, newer file wins"
else
    tg_notify "🔄 OneDrive bisync STARTED
Time: ${start_time}
Folders: ${#FOLDERS[@]}
Mode: Two-way sync, newer file wins"
fi

# Serialise runs; a stuck rclone must not stack up behind the cron tick.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(date '+%F %T')] another bisync is running; skipping"
    tg_notify "⚠️ OneDrive bisync SKIPPED
Another instance is already running"
    exit 0
fi

fail=0
failed_folders=()
synced_folders=()
skipped_folders=()

# Regenerable dependency/cache trees are excluded: node_modules and virtualenvs
# are 100k+ tiny files of reinstallable content, they dominate the transfer and
# make OneDrive throttle (the first attempt hung mid-listing). Remove these
# --exclude lines if you genuinely want an exact byte mirror including them.
EXCLUDES=(
    --exclude "**/node_modules/**"
    --exclude "**/.venv/**"
    --exclude "**/venv/**"
    --exclude "**/__pycache__/**"
    --exclude "**/.git/**"
)

for folder in "${FOLDERS[@]}"; do
    slug="$(printf '%s' "$folder" | tr '/ ' '__' | tr -cd '[:alnum:]_-')"
    workdir="${WORK_ROOT}/${slug}"
    mkdir -p "$workdir"
    # Without a completed --resync there is no prior listing, so a plain bisync
    # can only fail. Skip quietly so the cron does not spam errors before the
    # first resync has been run.
    if [ -z "$RESYNC" ] && ! compgen -G "${workdir}/*.lst" > /dev/null; then
        echo "[$(date '+%F %T')] ${folder}: no prior state; run 'bisync_onedrive.sh --resync' first - skipping"
        skipped_folders+=("$folder")
        continue
    fi
    echo "[$(date '+%F %T')] bisync ${folder} ${RESYNC:+(${RESYNC})}"
    "$RCLONE" bisync "${REMOTE_A}:${folder}" "${REMOTE_B}:${folder}" \
        --workdir "$workdir" \
        --resync-mode newer \
        --conflict-resolve newer \
        --create-empty-src-dirs \
        --max-delete 50 \
        "${EXCLUDES[@]}" \
        --transfers 4 \
        --checkers 8 \
        --timeout 5m \
        --contimeout 1m \
        --retries 3 \
        --low-level-retries 10 \
        --log-level INFO \
        ${RESYNC} 2>&1 | sed "s/^/  ${folder}: /"
    rc=${PIPESTATUS[0]}
    if [ "$rc" -ne 0 ]; then
        echo "  ${folder}: bisync exited ${rc}"
        fail=1
        failed_folders+=("$folder (rc=$rc)")
        # A failed/aborted run poisons the state; the next run needs a resync.
        if [ "${rc}" -eq 7 ] || [ "${rc}" -eq 9 ] || [ "${rc}" -eq 10 ]; then
            echo "  ${folder}: state needs 'bisync_onedrive.sh --resync'"
        fi
    else
        synced_folders+=("$folder")
    fi
done

# ---------------------------------------------------------------------------
# Send completion notification
# ---------------------------------------------------------------------------
end_time=$(date '+%F %T')
duration=$(( $(date -d "$end_time" +%s 2>/dev/null || echo 0) - $(date -d "$start_time" +%s 2>/dev/null || echo 0) ))

if [ "$fail" -eq 1 ]; then
    # Error notification
    error_msg="❌ OneDrive bisync FAILED
Start: ${start_time}
End: ${end_time}

Failed folders:"
    for f in "${failed_folders[@]}"; do
        error_msg+="
  • ${f}"
    done
    if [ ${#synced_folders[@]} -gt 0 ]; then
        error_msg+="

Synced OK:"
        for f in "${synced_folders[@]}"; do
            error_msg+="
  ✓ ${f}"
        done
    fi
    error_msg+="

Check log: ~/logs/bisync-onedrive.log"
    tg_notify "$error_msg"
else
    # Success notification
    if [ ${#synced_folders[@]} -gt 0 ]; then
        success_msg="✅ OneDrive bisync COMPLETED"
        [ -n "$RESYNC" ] && success_msg+=" (RESYNC)"
        success_msg+="
Start: ${start_time}
End: ${end_time}

Synced folders (${#synced_folders[@]}):"
        for f in "${synced_folders[@]}"; do
            success_msg+="
  ✓ ${f}"
        done
        if [ ${#skipped_folders[@]} -gt 0 ]; then
            success_msg+="

Skipped (no state):"
            for f in "${skipped_folders[@]}"; do
                success_msg+="
  ⚠ ${f}"
            done
        fi
        tg_notify "$success_msg"
    else
        # All skipped
        tg_notify "⚠️ OneDrive bisync: All folders skipped (no prior state)
Run: bisync_onedrive.sh --resync"
    fi
fi

exit "$fail"
