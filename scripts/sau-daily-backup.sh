#!/usr/bin/env bash
# sau-daily-backup.sh - Daily backup for social-auto-upload
# Backs up to both:
#   1. Onedrive-Yahooforsub-Tao:Scripts-ssh-ssl-keys/socialupload/ (config + DB + cookies)
#   2. Onedrive-Yahooforsub-Tao:備份/ (full project archive)
#   3. share.iamwillywang.com (if SHARE_TOKEN is set)
#
# Self-expires after 5 runs by removing its own crontab entry.

set -euo pipefail

RUN_COUNT_FILE="/home/will/social-auto-upload/backups/.daily_run_count"
MAX_RUNS=5

# Initialize or increment run count
if [[ -f "$RUN_COUNT_FILE" ]]; then
    count=$(cat "$RUN_COUNT_FILE")
else
    count=0
fi
count=$((count + 1))
echo "$count" > "$RUN_COUNT_FILE"

echo "[$(date)] === Daily SAU backup run ${count}/${MAX_RUNS} ==="

# 1. Run the existing SAU backup script (DB + cookies + config → OneDrive)
/home/will/social-auto-upload/scripts/backup.sh 2>&1

# 2. Full project archive → OneDrive 備份
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_NAME="socialupload-full-${TIMESTAMP}.tar.gz"
cd /home/will
tar -czf "/tmp/${BACKUP_NAME}" \
  --exclude='social-auto-upload/node_modules' \
  --exclude='social-auto-upload/.git' \
  --exclude='social-auto-upload/sau_frontend/node_modules' \
  --exclude='social-auto-upload/backups' \
  social-auto-upload/ 2>/dev/null

SIZE=$(du -h "/tmp/${BACKUP_NAME}" | cut -f1)
echo "[$(date)] Full archive: ${BACKUP_NAME} (${SIZE})"

rclone copy "/tmp/${BACKUP_NAME}" 'Onedrive-Yahooforsub-Tao:備份/' 2>&1
echo "[$(date)] Uploaded to OneDrive 備份"

rm -f "/tmp/${BACKUP_NAME}"

# 3. Upload to share.iamwillywang.com (R2 storage via tgstate)
SHARE_URL="http://localhost:8001"
SHARE_PASSWORD="sau-backup-2026"
SHARE_COOKIE="/tmp/sau-share-cookie.txt"

# Login to get session cookie
login_resp=$(curl -s -X POST "${SHARE_URL}/api/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"password\":\"${SHARE_PASSWORD}\"}" \
  -c "$SHARE_COOKIE" 2>/dev/null)

if echo "$login_resp" | grep -q '"ok"'; then
    # Create a small critical backup (DB + cookies + config) for share
    CRITICAL_NAME="sau-critical-${TIMESTAMP}.tar.gz"
    tar -czf "/tmp/${CRITICAL_NAME}" \
      -C /home/will/social-auto-upload \
      conf.py .env db/database.db cookies cookiesFile docker-compose.yml 2>/dev/null

    upload_resp=$(curl -s -X POST "${SHARE_URL}/api/upload" \
      -b "$SHARE_COOKIE" \
      -F "file=@/tmp/${CRITICAL_NAME}" 2>/dev/null)

    if echo "$upload_resp" | grep -q 'download_path'; then
        dl_path=$(echo "$upload_resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('download_path',''))" 2>/dev/null)
        echo "[$(date)] Uploaded to share.iamwillywang.com → https://share.iamwillywang.com${dl_path}"
    else
        echo "[$(date)] WARNING: share upload failed: ${upload_resp}"
    fi

    rm -f "/tmp/${CRITICAL_NAME}" "$SHARE_COOKIE"
else
    echo "[$(date)] WARNING: share login failed: ${login_resp}"
    rm -f "$SHARE_COOKIE"
fi

# 4. Self-expire after MAX_RUNS
if (( count >= MAX_RUNS )); then
    echo "[$(date)] Reached ${MAX_RUNS} runs — removing daily backup cron entry"
    crontab -l 2>/dev/null | grep -v "sau-daily-backup.sh" | crontab -
    rm -f "$RUN_COUNT_FILE"
fi

echo "[$(date)] === Daily backup complete ==="
