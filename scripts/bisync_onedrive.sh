#!/usr/bin/env bash
# bisync_onedrive.sh - two-way sync with ACTIVE MODIFICATION DETECTION
#
#   Onedrive-Yahooforsub-Tao  <->  Onedrive-willywang
#
# CRITICAL BEHAVIOR:
#   - Before and during sync, monitor for active file modifications
#   - If modifications detected → ABORT IMMEDIATELY, wait for next hour
#   - This prevents restoring deleted files while user is actively making changes
#   - Only sync when both sides are STABLE (no active modifications)
#
# HOW IT WORKS:
#   1. Pre-flight check: compare listings taken 5 seconds apart
#      - If different → user is actively modifying → ABORT
#   2. Snapshot-based deletion tracking: detect and propagate deletions
#   3. Union copy: sync newer files both directions
#   4. During sync: monitor for modifications every 30 seconds
#      - If detected → ABORT immediately
#   5. Save snapshots only if sync completed without interruptions
#
# Usage:
#   ./bisync_onedrive.sh            # Normal sync (cron runs this every hour)
#   ./bisync_onedrive.sh --resync   # Force rebuild snapshots (skip modification checks)

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

SNAPSHOT_DIR="${HOME}/.cache/rclone/bisync-snapshots"
LOG_DIR="${HOME}/logs"
LOCK_FILE="/tmp/bisync-onedrive.lock"
RESYNC="${1:-}"

# Telegram config
TG_TOKEN="8929051209:AAF9WULtpjwuSvFLpIbWZsyzHin5hMFF1oc"
TG_CHAT="8633483147"

mkdir -p "$SNAPSHOT_DIR" "$LOG_DIR"
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
# Quick file listing (for modification detection)
# Returns: file count and a hash of file names (fast, not full listing)
# ---------------------------------------------------------------------------
quick_state() {
    local remote_path="$1"
    # Get file count + first/last 100 files as a quick fingerprint
    {
        rclone lsf "$remote_path" --recursive --files-only --fast-list \
            --timeout 2m --contimeout 30s 2>/dev/null | \
        awk 'BEGIN{c=0} {c++; if(c<=100 || c%1000==0) print} END{print "TOTAL:"c}'
    } | md5sum | cut -d' ' -f1
}

# ---------------------------------------------------------------------------
# Full file listing (for snapshots and deletion detection)
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
# Check for active modifications: take two quick snapshots 5 seconds apart
# Returns 0 if stable, 1 if modifications detected
# ---------------------------------------------------------------------------
check_stability() {
    local folder="$1"
    local state1_a state1_b state2_a state2_b
    
    echo "  ${folder}: checking for active modifications..."
    
    # First snapshot
    state1_a=$(quick_state "${REMOTE_A}:${folder}")
    state1_b=$(quick_state "${REMOTE_B}:${folder}")
    
    # Wait 5 seconds
    sleep 5
    
    # Second snapshot
    state2_a=$(quick_state "${REMOTE_A}:${folder}")
    state2_b=$(quick_state "${REMOTE_B}:${folder}")
    
    # Compare
    if [ "$state1_a" != "$state2_a" ] || [ "$state1_b" != "$state2_b" ]; then
        echo "  ${folder}: ⚠️ ACTIVE MODIFICATIONS DETECTED - aborting"
        return 1
    fi
    
    echo "  ${folder}: ✓ stable (no active modifications)"
    return 0
}

# ---------------------------------------------------------------------------
# Propagate deletions
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

    # Clean up empty directories
    "$RCLONE" rmdirs "${dst_remote}:${folder}" \
        --leave-root \
        --timeout 5m \
        --contimeout 1m \
        --retries 2 \
        2>/dev/null || true

    echo "  ${label}: done propagating ${count} deletion(s)"
}

# ---------------------------------------------------------------------------
# Monitor for modifications during sync (runs in background)
# Checks every 30 seconds, writes to a flag file if modifications detected
# ---------------------------------------------------------------------------
monitor_modifications() {
    local folder="$1"
    local abort_flag="$2"
    local initial_state_a initial_state_b
    
    initial_state_a=$(quick_state "${REMOTE_A}:${folder}")
    initial_state_b=$(quick_state "${REMOTE_B}:${folder}")
    
    while true; do
        sleep 30
        
        # Check if abort flag already set
        [ -f "$abort_flag" ] && exit 0
        
        current_a=$(quick_state "${REMOTE_A}:${folder}")
        current_b=$(quick_state "${REMOTE_B}:${folder}")
        
        if [ "$initial_state_a" != "$current_a" ] || [ "$initial_state_b" != "$current_b" ]; then
            echo "  ${folder}: ⚠️ MODIFICATIONS DETECTED DURING SYNC - signaling abort"
            touch "$abort_flag"
            exit 0
        fi
    done
}

# ---------------------------------------------------------------------------
# Start notification
# ---------------------------------------------------------------------------
start_time=$(date '+%F %T')
if [ -n "$RESYNC" ]; then
    tg_notify "🔄 OneDrive bisync STARTED (RESYNC - rebuilding snapshots)
Time: ${start_time}
Folders: ${#FOLDERS[@]}
⚠️ Resync mode: skipping modification checks"
else
    tg_notify "🔄 OneDrive bisync STARTED
Time: ${start_time}
Folders: ${#FOLDERS[@]}
✓ Will abort if active modifications detected"
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
aborted=0
failed_folders=()
synced_folders=()
aborted_folders=()
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
    echo "[$(date '+%F %T')] sync ${folder}"
    rc=0
    folder_aborted=0

    # Snapshot paths
    snap_a="${SNAPSHOT_DIR}/${folder}/A.list"
    snap_b="${SNAPSHOT_DIR}/${folder}/B.list"
    mkdir -p "${SNAPSHOT_DIR}/${folder}"

    # Temp files
    tmp_dir=$(mktemp -d "/tmp/bisync-${folder//\//_}.XXXXXX")
    abort_flag="${tmp_dir}/ABORT"

    # -----------------------------------------------------------------------
    # Step 1: Pre-flight stability check (skip if --resync)
    # -----------------------------------------------------------------------
    if [ -z "$RESYNC" ]; then
        if ! check_stability "$folder"; then
            echo "  ${folder}: ABORTED - user is actively making changes"
            folder_aborted=1
            aborted=1
            aborted_folders+=("$folder")
            rm -rf "$tmp_dir"
            continue
        fi
    fi

    # -----------------------------------------------------------------------
    # Step 2: Detect deletions (skip if --resync or no previous snapshot)
    # -----------------------------------------------------------------------
    folder_deletions=0
    if [ -z "$RESYNC" ] && [ -f "$snap_a" ] && [ -f "$snap_b" ]; then
        echo "  ${folder}: detecting deletions since last sync..."

        # List current state
        list_remote "${REMOTE_A}:${folder}" > "${tmp_dir}/current_a.list"
        list_remote "${REMOTE_B}:${folder}" > "${tmp_dir}/current_b.list"

        # Find deletions
        LC_ALL=C comm -23 "$snap_a" "${tmp_dir}/current_a.list" > "${tmp_dir}/deleted_a.list"
        LC_ALL=C comm -23 "$snap_b" "${tmp_dir}/current_b.list" > "${tmp_dir}/deleted_b.list"

        del_a_count=0
        del_b_count=0
        [ -s "${tmp_dir}/deleted_a.list" ] && del_a_count=$(wc -l < "${tmp_dir}/deleted_a.list")
        [ -s "${tmp_dir}/deleted_b.list" ] && del_b_count=$(wc -l < "${tmp_dir}/deleted_b.list")

        if [ "$del_a_count" -gt 0 ] || [ "$del_b_count" -gt 0 ]; then
            echo "  ${folder}: found ${del_a_count} deletion(s) on Tao, ${del_b_count} on willywang"

            # Propagate deletions (only if file still exists on other side)
            if [ -s "${tmp_dir}/deleted_a.list" ]; then
                LC_ALL=C comm -12 "${tmp_dir}/deleted_a.list" "${tmp_dir}/current_b.list" > "${tmp_dir}/del_a_exists_on_b.list"
                propagate_deletions "${tmp_dir}/del_a_exists_on_b.list" "$REMOTE_B" "$folder" "${folder} A→B del"
                folder_deletions=$((folder_deletions + del_a_count))
            fi

            if [ -s "${tmp_dir}/deleted_b.list" ]; then
                LC_ALL=C comm -12 "${tmp_dir}/deleted_b.list" "${tmp_dir}/current_a.list" > "${tmp_dir}/del_b_exists_on_a.list"
                propagate_deletions "${tmp_dir}/del_b_exists_on_a.list" "$REMOTE_A" "$folder" "${folder} B→A del"
                folder_deletions=$((folder_deletions + del_b_count))
            fi
        else
            echo "  ${folder}: no deletions detected"
        fi
    else
        if [ -n "$RESYNC" ]; then
            echo "  ${folder}: RESYNC mode - skipping deletion detection"
        else
            echo "  ${folder}: no previous snapshot - first run"
        fi
    fi
    total_deletions=$((total_deletions + folder_deletions))

    # -----------------------------------------------------------------------
    # Step 3: Start modification monitor (background, skip if --resync)
    # -----------------------------------------------------------------------
    monitor_pid=""
    if [ -z "$RESYNC" ]; then
        monitor_modifications "$folder" "$abort_flag" &
        monitor_pid=$!
    fi

    # -----------------------------------------------------------------------
    # Step 4: Union copy (both directions)
    # -----------------------------------------------------------------------
    for direction in "AtoB" "BtoA"; do
        # Check if abort was signaled
        if [ -f "$abort_flag" ]; then
            echo "  ${folder}: ⚠️ ABORTING - modifications detected during sync"
            rc=99
            folder_aborted=1
            aborted=1
            break
        fi

        if [ "$direction" = "AtoB" ]; then
            src_remote="$REMOTE_A"; dst_remote="$REMOTE_B"
        else
            src_remote="$REMOTE_B"; dst_remote="$REMOTE_A"
        fi
        
        echo "  ${folder} ${direction}: syncing..."
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
    # Step 5: Stop monitor and check for abort
    # -----------------------------------------------------------------------
    if [ -n "$monitor_pid" ]; then
        kill "$monitor_pid" 2>/dev/null || true
        wait "$monitor_pid" 2>/dev/null || true
    fi

    if [ -f "$abort_flag" ]; then
        echo "  ${folder}: ⚠️ ABORTED - modifications detected"
        folder_aborted=1
        aborted=1
        aborted_folders+=("$folder")
        rc=99
    fi

    # -----------------------------------------------------------------------
    # Step 6: Save snapshots (only if sync succeeded and not aborted)
    # -----------------------------------------------------------------------
    if [ "$rc" -eq 0 ] && [ "$folder_aborted" -eq 0 ]; then
        echo "  ${folder}: saving snapshots..."
        list_remote "${REMOTE_A}:${folder}" > "$snap_a"
        list_remote "${REMOTE_B}:${folder}" > "$snap_b"
        synced_folders+=("$folder")
    elif [ "$folder_aborted" -eq 0 ]; then
        fail=1
        failed_folders+=("$folder (rc=$rc)")
    fi

    rm -rf "$tmp_dir"
done

# ---------------------------------------------------------------------------
# Completion notification
# ---------------------------------------------------------------------------
end_time=$(date '+%F %T')

if [ "$aborted" -eq 1 ]; then
    msg="⚠️ OneDrive bisync ABORTED (modifications detected)
Start: ${start_time}
End: ${end_time}
Deletions propagated: ${total_deletions}

Aborted folders (user active):"
    for f in "${aborted_folders[@]}"; do msg+=$'\n'"  • $f"; done
    if [ ${#synced_folders[@]} -gt 0 ]; then
        msg+=$'\n\n'"Completed:"
        for f in "${synced_folders[@]}"; do msg+=$'\n'"  ✓ $f"; done
    fi
    msg+=$'\n\n'"Will retry next hour."
    tg_notify "$msg"
elif [ "$fail" -eq 1 ]; then
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

# Exit with special code if aborted (so cron knows it was intentional)
if [ "$aborted" -eq 1 ]; then
    exit 99
fi

exit "$fail"
