#!/usr/bin/env bash
# Sole production deployer: keep the existing */2 cron and flock path.
# Build/test exact origin/main, then call THAT candidate's guarded deploy helper.
# GitHub Actions only tests/publishes images; Watchtower stays disabled.
set -euo pipefail
umask 077
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG="${CI_WATCH_LOG:-$LOG_DIR/ci-watch.log}"
LOCK="$LOG_DIR/ci-watch.lock"
STATE="$LOG_DIR/ci-watch.state"
PYTHON="${SAU_RELEASE_PYTHON:-python3}"
IMAGE_BASE="ghcr.io/willywang8216/social-auto-upload"
SERVICE=social-auto-upload
WORKER=social-auto-upload-worker
log() { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG"; }

# Existing best-effort Telegram behavior. No token/chat/config value is logged.
notify_failure() {
  local subject="$1" body="$2" token chat
  token="$(sed -n 's/^SAU_ALERT_TELEGRAM_BOT_TOKEN=//p' "$REPO_ROOT/.env" 2>/dev/null | tail -1 | tr -d '\"'\''')"
  chat="$(sed -n 's/^SAU_ALERT_TELEGRAM_CHAT_ID=//p' "$REPO_ROOT/.env" 2>/dev/null | tail -1 | tr -d '\"'\''')"
  chat="${chat%%,*}"
  if [[ -z "$token" || -z "$chat" ]]; then
    log '  (no Telegram creds; failure recorded in this log only)'
    return 0
  fi
  if curl -fsS -m 20 -X POST "https://api.telegram.org/bot${token}/sendMessage" \
       --data-urlencode "chat_id=${chat}" --data-urlencode "text=${subject}

${body}" >/dev/null 2>&1; then
    log '  failure alert delivered'
  else
    log '  failure alert delivery failed'
  fi
}

exec 9>"$LOCK"
if ! flock -n 9; then log 'another watcher/deploy/restore holds the lock; exiting'; exit 0; fi
export SAU_RELEASE_LOCK_FD=9
if [[ "${CI_WATCH_TEST_ALERT:-0}" == 1 ]]; then
  notify_failure 'SAU ci-watch: test alert (no action needed)' 'One-off channel verification; no deployment.'
  exit 0
fi
# Dirty files and unpublished local history are blockers, not permission to
# silently ship origin/main over an operator's unknown local configuration/code.
if ! LOCAL_HEAD="$("$PYTHON" scripts/release_checks.py clean)"; then
  log 'local work is dirty/unknown; preserving it and deferring deployment'
  exit 1
fi
if [[ "${CI_WATCH_DRY_RUN:-0}" != 1 ]]; then
  if ! GIT_TERMINAL_PROMPT=0 git fetch --quiet origin main 2>>"$LOG"; then
    log 'git fetch failed; retrying next tick'
    exit 0
  fi
fi
TARGET="$(git rev-parse origin/main)"
SHORT="${TARGET:0:12}"
[[ "$TARGET" =~ ^[0-9a-f]{40}$ ]] || exit 1
if ! git merge-base --is-ancestor "$LOCAL_HEAD" "$TARGET"; then
  log 'local HEAD has unpublished/divergent history; refusing to overwrite it'
  exit 1
fi
LAST=""
[[ ! -f "$STATE" ]] || LAST="$(cat "$STATE")"
log "origin/main=${SHORT} last_deployed=${LAST:0:12}"
if [[ "${CI_WATCH_DRY_RUN:-0}" == 1 ]]; then
  log 'DRY RUN: would require clean exact-commit gates/image receipt, pause/drain, snapshot, migrate and verify both roles'
  exit 0
fi
if [[ -e "$REPO_ROOT/db/publish.pause" || -e "$LOG_DIR/release-staged.json" ]]; then
  log 'claims paused/staged: operator verification or recovery required; watcher will not release the pause'
  exit 0
fi
already_healthy() {
  local web_image worker_image
  web_image="$(docker inspect -f '{{.Image}}' "$SERVICE")" || return 1
  worker_image="$(docker inspect -f '{{.Image}}' "$WORKER")" || return 1
  [[ "$web_image" == "$worker_image" ]] || return 1
  "$PYTHON" scripts/release_checks.py verify --revision "$TARGET" \
    --receipts "$LOG_DIR/releases/gates/$TARGET" --image-id "$web_image" || return 1
  for name in "$SERVICE" "$WORKER"; do
    [[ "$(docker inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$name")" == "$TARGET" ]] || return 1
    [[ "$(docker inspect -f '{{.State.Health.Status}}' "$name")" == healthy ]] || return 1
  done
  docker exec "$SERVICE" python scripts/release_probe.py web --revision "$TARGET" || return 1
  docker exec "$WORKER" python scripts/release_probe.py worker --revision "$TARGET"
}
if [[ "$LAST" == "$TARGET" && "${CI_WATCH_FORCE:-0}" != 1 ]] && already_healthy >>"$LOG" 2>&1; then
  log 'recorded revision and both healthy running roles agree; nothing to do'
  exit 0
fi
BUILD_LOG="$LOG_DIR/ci-watch-build-${SHORT}.log"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/sau-ci-watch.XXXXXX")"
WORKTREE="$SCRATCH/source"
cleanup() {
  git -C "$REPO_ROOT" worktree remove --force "$WORKTREE" >/dev/null 2>&1 || true
  rm -rf "$SCRATCH"  # this invocation's own disposable checkout only
}
trap cleanup EXIT
git worktree add --detach --quiet "$WORKTREE" "$TARGET"
log "gating/building/deploying exact ${SHORT} (details: ${BUILD_LOG})"
if ! bash "$WORKTREE/scripts/deploy-local.sh" --runtime-root "$REPO_ROOT" --revision "$TARGET" >>"$BUILD_LOG" 2>&1; then
  log "candidate ${SHORT} failed; previous successful marker is unchanged"
  notify_failure "SAU ci-watch: release failed for ${SHORT}" \
    "Gates/build/drain/verification failed. Claims may be paused; inspect ${BUILD_LOG} and logs/releases before recovery. No database rollback was attempted."
  exit 1
fi
# deploy-local writes this same marker only AFTER activation is verified.
[[ "$(cat "$STATE")" == "$TARGET" ]] || { log 'successful revision marker mismatch'; exit 1; }
log "deployed ${SHORT}; both roles verified"

# Preserve the existing image/cache retention policy. The deploy helper keeps
# :previous as an explicit rollback reference; never delete other repositories.
KEEP_IMAGES="${SAU_CI_WATCH_KEEP_IMAGES:-3}"
if [[ "$KEEP_IMAGES" =~ ^[0-9]+$ ]] && (( KEEP_IMAGES >= 1 )); then
  mapfile -t stale_tags < <(
    docker images "$IMAGE_BASE" --format '{{.Tag}} {{.CreatedAt}}' \
      | grep -E '^commit-' | sort -k2 -r | awk '{print $1}' \
      | tail -n +$((KEEP_IMAGES + 1))
  )
  for tag in "${stale_tags[@]:-}"; do
    [[ -n "$tag" && "$tag" != "commit-${TARGET}" ]] || continue
    docker rmi "${IMAGE_BASE}:${tag}" >>"$LOG" 2>&1 || log "could not prune ${tag}; leaving it"
  done
  docker image prune -f --filter 'dangling=true' >>"$LOG" 2>&1 || true
fi
docker builder prune -f --keep-storage "${SAU_CI_WATCH_BUILD_CACHE:-3GB}" >>"$LOG" 2>&1 || true
