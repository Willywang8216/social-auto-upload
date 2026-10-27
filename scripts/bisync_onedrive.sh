#!/usr/bin/env bash
# bisync_onedrive.sh - two-way sync between Two OneDrive accounts
#
#   Onedrive-Yahooforsub-Tao  <->  Onedrive-willywang
#
# Uses a union-copy approach (rclone copy --update in both directions) for
# file additions/modifications, PLUS snapshot-based deletion tracking so that
# deletions on either side propagate to the other.
#
# HOW IT WORKS:
#   1. Before sync: compare current file listings to the last saved snapshot
#      to detect files that were deleted since the last successful sync.
#   2. Propagate deletions: delete those files from the OTHER side.
#   3. Union copy: copy newer files in both directions (A→B, B→A with --update).
#   4. Save new snapshots of both sides for next time.
#
# BEHAVIOR:
#   - New/modified files: newer version wins (--update), synced both ways.
#   - Deleted files: deletion propagates to the other side.
#   - First run (no snapshot): just does union copy + saves snapshots.
#   - If sync fails mid-way: snapshots are NOT updated, so next run will
#     still correctly detect deletions since the last SUCCESSFUL sync.
#
# Folders: 補教教學, Yellow, Videos, Assets, Scripts-ssh-ssl-keys
#
# Usage:
#   ./bisync_onedrive.sh            # Normal sync (cron runs this every 30 min)
#   ./bisync_onedrive.sh --resync   # Force rebuild snapshots (no deletion propagation)

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
SNAPSHOT_DIR="${HOME}/.cache/rclone/bisync-snapshots"
LOG_DIR="${HOME}/logs"
LOCK_FILE="/tmp/bisync-onedrive.lock"
RESYNC="${1:-}"

# Telegram config
TG_TOKEN="8929051209:AAF9WULtpjwuSvFLpIbWZsyzHin5hMFF1oc"
TG_CHAT="8633483147"

mkdir -p "$WORK_ROOT" "$SNAPSHOT_DIR" "$LOG_DIR"
RCLONE="$(command -v rclone || echo /usr/bin/rclone)"

# ---------------------------------------------------------------------------
# Telegram notification
# ---------------------------------------------------------------------------
tg_notify() {
    [ -n "${TG_TOKEN:-}" ] && [ -n "${TG_CHAT}" ] || return 0
    curl -s -m 10 "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TG_CHAT}" \
        --data-urlencode "disable_web_page_preview=true" \
        --data-urlencode "text=$1" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------
# List all files on a remote (sorted, one relative path per line)
# Uses --fast-list to minimize API calls on large remotes.
# ---------------------------------------------------------------------------
list_remote() {
    local remote_path="$1"
    "$RCLONE" lsf "$remote_path" \
        --recursive \
        --files-only \
        --fast-list \
        --timeout 5m \
        --contimeout 1m \
        --retries 3 \
        --low-level-retries 10 \
        2>/dev/null | LC_ALL=C sort
}

# ---------------------------------------------------------------------------
# Propagate deletions: given a list of files deleted from one side,
# delete them from the other side.
# ---------------------------------------------------------------------------
propagate_deletions() {
    local deletions_file="$1"
    local dst_remote="$2"
    local folder="$3"
    local label="$4"
    local count=0

    if [ ! -s "$deletions_file" ]; then
        return 0
    fi

    count=$(wc -l < "$deletions_file")
    echo "  ${label}: propagating ${count} deletion(s) to ${dst_remote}:${folder}"

    while IFS= read -r file; do
        [ -n "$file" ] || continue
        "$RCLONE" deletefile "${dst_remote}:${folder}/${file}" \
            --timeout 2m \
            --contimeout 30s \
            --retries 2 \
            --low-level-retries 5 \
            2>/dev/null || true
    done < "$deletions_file"

    # Clean up empty directories left behind after deletions
    "$RCLONE" rmdirs "${dst_remote}:${folder}" \
        --leave-root \
        --timeout 5m \
        --contimeout 1m \
        --retries 2 \
        2>/dev/null || true

    echo "  ${label}: done propagating ${count} deletion(s)"
}

# ---------------------------------------------------------------------------
# Start notification
# ---------------------------------------------------------------------------
start_time=$(date '+%F %T')
if [ -n "$RESYNC" ]; then
    tg_notify "🔄 OneDrive bisync STARTED (RESYNC - rebuilding snapshots)
Time: ${start_time}
Folders: ${#FOLDERS[@]}
⚠️ Resync mode: snapshots will be rebuilt. No deletion propagation this run."
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
total_deletions=0

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
    echo "[$(date '+%F %T')] sync ${folder} (deletions + union mirror)"
    rc=0

    # Snapshot paths for this folder
    snap_a="${SNAPSHOT_DIR}/${folder}/A.list"
    snap_b="${SNAPSHOT_DIR}/${folder}/B.list"
    mkdir -p "${SNAPSHOT_DIR}/${folder}"

    # Temp files for this run
    tmp_dir=$(mktemp -d "/tmp/bisync-${folder//\//_}.XXXXXX")
    current_a="${tmp_dir}/current_A.list"
    current_b="${tmp_dir}/current_B.list"
    deleted_from_a="${tmp_dir}/deleted_from_A.list"
    deleted_from_b="${tmp_dir}/deleted_from_B.list"

    # -----------------------------------------------------------------------
    # Step 1: Detect deletions (skip if --resync or no previous snapshot)
    # -----------------------------------------------------------------------
    folder_deletions=0
    if [ -z "$RESYNC" ] && [ -f "$snap_a" ] && [ -f "$snap_b" ]; then
        echo "  ${folder}: detecting deletions since last sync..."

        # List current state of both sides
        list_remote "${REMOTE_A}:${folder}" > "$current_a"
        list_remote "${REMOTE_B}:${folder}" > "$current_b"

        # Find files that were in the snapshot but are now gone
        # comm -23 = lines only in file1 (snapshot) but not file2 (current)
        LC_ALL=C comm -23 "$snap_a" "$current_a" > "$deleted_from_a"
        LC_ALL=C comm -23 "$snap_b" "$current_b" > "$deleted_from_b"

        del_a_count=0
        del_b_count=0
        [ -s "$deleted_from_a" ] && del_a_count=$(wc -l < "$deleted_from_a")
        [ -s "$deleted_from_b" ] && del_b_count=$(wc -l < "$deleted_from_b")

        if [ "$del_a_count" -gt 0 ] || [ "$del_b_count" -gt 0 ]; then
            echo "  ${folder}: found ${del_a_count} deletion(s) on Tao, ${del_b_count} deletion(s) on willywang"

            # Propagate: files deleted from A → delete from B
            # But only if they still exist on B (check against current B listing)
            if [ -s "$deleted_from_a" ]; then
                # Filter: only delete from B if the file currently exists on B
                # (it might have already been deleted from both sides)
                LC_ALL=C comm -12 "$deleted_from_a" "$current_b" > "${deleted_from_a}.exists_on_b"
                propagate_deletions "${deleted_from_a}.exists_on_b" "$REMOTE_B" "$folder" "${folder} A→B del"
                rm -f "${deleted_from_a}.exists_on_b"
                folder_deletions=$((folder_deletions + del_a_count))
            fi

            # Propagate: files deleted from B → delete from A
            if [ -s "$deleted_from_b" ]; then
                LC_ALL=C comm -12 "$deleted_from_b" "$current_a" > "${deleted_from_b}.exists_on_a"
                propagate_deletions "${deleted_from_b}.exists_on_a" "$REMOTE_A" "$folder" "${folder} B→A del"
                rm -f "${deleted_from_b}.exists_on_a"
                folder_deletions=$((folder_deletions + del_b_count))
            fi
        else
            echo "  ${folder}: no deletions detected"
        fi
    else
        if [ -n "$RESYNC" ]; then
            echo "  ${folder}: RESYNC mode - skipping deletion detection, rebuilding snapshots"
        else
            echo "  ${folder}: no previous snapshot - first run, skipping deletion detection"
        fi
    fi
    total_deletions=$((total_deletions + folder_deletions))

    # -----------------------------------------------------------------------
    # Step 2: Union copy (both directions, keep newest)
    # -----------------------------------------------------------------------
    for direction in "AtoB" "BtoA"; do
        if [ "$direction" = "AtoB" ]; then
            src_remote="$REMOTE_A"; dst_remote="$REMOTE_B"
        else
            src_remote="$REMOTE_B"; dst_remote="$REMOTE_A"
        fi
        echo "  ${folder} ${direction}: copying newer files..."
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

    # -----------------------------------------------------------------------
    # Step 3: Save new snapshots (only if sync succeeded)
    # -----------------------------------------------------------------------
    if [ "$rc" -eq 0 ]; then
        echo "  ${folder}: saving snapshots..."
        list_remote "${REMOTE_A}:${folder}" > "$snap_a"
        list_remote "${REMOTE_B}:${folder}" > "$snap_b"
        synced_folders+=("$folder")
    else
        fail=1
        failed_folders+=("$folder (rc=$rc)")
        # Don't update snapshots - next run will retry
    fi

    # Clean up temp files
    rm -rf "$tmp_dir"
done

# ---------------------------------------------------------------------------
# Completion notification
# ---------------------------------------------------------------------------
end_time=$(date '+%F %T')

if [ "$fail" -eq 1 ]; then
    msg="❌ OneDrive bisync FAILED
Start: ${start_time}
End: ${end_time}
Deletions propagated: ${total_deletions}

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
    [ "$total_deletions" -gt 0 ] && msg+=$'\n'"🗑 Deletions propagated: ${total_deletions}"
    msg+=$'\n\n'"Synced (${#synced_folders[@]}):"
    for f in "${synced_folders[@]}"; do msg+=$'\n'"  ✓ $f"; done
    tg_notify "$msg"
else
    tg_notify "⚠️ All folders skipped. Check logs."
fi

exit "$fail"
