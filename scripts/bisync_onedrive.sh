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

mkdir -p "$WORK_ROOT" "$LOG_DIR"
RCLONE="$(command -v rclone || echo /usr/bin/rclone)"

# Serialise runs; a stuck rclone must not stack up behind the cron tick.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(date '+%F %T')] another bisync is running; skipping"
    exit 0
fi

fail=0
for folder in "${FOLDERS[@]}"; do
    slug="$(printf '%s' "$folder" | tr '/ ' '__' | tr -cd '[:alnum:]_-')"
    workdir="${WORK_ROOT}/${slug}"
    mkdir -p "$workdir"
    # Without a completed --resync there is no prior listing, so a plain bisync
    # can only fail. Skip quietly so the cron does not spam errors before the
    # first resync has been run.
    if [ -z "$RESYNC" ] && ! compgen -G "${workdir}/*.lst" > /dev/null; then
        echo "[$(date '+%F %T')] ${folder}: no prior state; run 'bisync_onedrive.sh --resync' first - skipping"
        continue
    fi
    echo "[$(date '+%F %T')] bisync ${folder} ${RESYNC:+(${RESYNC})}"
    "$RCLONE" bisync "${REMOTE_A}:${folder}" "${REMOTE_B}:${folder}" \
        --workdir "$workdir" \
        --resync-mode newer \
        --conflict-resolve newer \
        --create-empty-src-dirs \
        --max-delete 50 \
        --log-level INFO \
        ${RESYNC} 2>&1 | sed "s/^/  ${folder}: /"
    rc=${PIPESTATUS[0]}
    if [ "$rc" -ne 0 ]; then
        echo "  ${folder}: bisync exited ${rc}"
        fail=1
        # A failed/aborted run poisons the state; the next run needs a resync.
        if [ "${rc}" -eq 7 ] || [ "${rc}" -eq 9 ] || [ "${rc}" -eq 10 ]; then
            echo "  ${folder}: state needs 'bisync_onedrive.sh --resync'"
        fi
    fi
done
exit "$fail"
