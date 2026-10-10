#!/usr/bin/env bash
# Local CI watcher — build + deploy origin/main whenever it moves.
#
# Why this exists
# ---------------
# This repository is a FORK (of dreammis/social-auto-upload). GitHub disabled
# Actions on this specific fork after 495 runs — its own Actions page says:
#
#   "Workflows aren't being run on this fork because of its GitHub Actions
#    usage. A repository maintainer can re-enable them."
#
# Every push after commit d1319216 produces zero runs, dispatch returns
# HTTP 422 "Actions has been disabled for this repository", and the documented
# REST re-enable (PUT /actions/permissions enabled=true) returns 204 but does
# not clear it. It is repository-specific: sibling repos on the same account and
# other forks of the same parent still run Actions. See
# reports/ci-actions-reenable.md.
#
# Until a maintainer re-enables Actions in the GitHub UI, this cron job is the
# GitHub-independent replacement: it notices when origin/main changes, builds the
# app image from a clean checkout of that exact commit, and deploys it using the
# same procedure as scripts/deploy-local.sh.
#
# Safety properties
# -----------------
#   * Single-flight: flock(1) on logs/ci-watch.lock; an overlapping run exits
#     immediately, so two cron ticks can never race.
#   * Idempotent: if the image for the target commit already exists and the
#     running container already uses it, the watcher does nothing. If the image
#     exists but the container is not running it, it skips the (slow) build and
#     only redeploys.
#   * Dirty-worktree tolerant: it builds from a throwaway `git worktree` checked
#     out at origin/main, so uncommitted local changes are never shipped and
#     never block it.
#   * Read-only with respect to history: it only fetches and reads origin/main;
#     it never commits, checks out, resets, or rewrites anything.
#   * Verified: it checks the built image runs, waits for /healthz == 200, and
#     only then records the commit as deployed.
#   * Alerts Telegram on failure (best effort; creds read from .env).
#
# Usage:
#   scripts/ci-watch.sh                     # normal cron entry point
#   CI_WATCH_FORCE=1 scripts/ci-watch.sh    # ignore the deployed-state marker
#   CI_WATCH_DRY_RUN=1 scripts/ci-watch.sh  # report what would happen; change nothing
#   CI_WATCH_TEST_ALERT=1 scripts/ci-watch.sh # send one test alert, then exit
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG="${CI_WATCH_LOG:-$LOG_DIR/ci-watch.log}"
LOCK="$LOG_DIR/ci-watch.lock"
STATE="$LOG_DIR/ci-watch.state"

IMAGE_BASE="ghcr.io/willywang8216/social-auto-upload"
SERVICE="social-auto-upload"
HEALTH_URL="http://localhost:5409/healthz"

log() { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG"; }

# Send a Telegram failure alert if creds are present in .env. Never secrets are
# echoed; best effort by design — alerting must not mask the real failure.
notify_failure() {
  local subject="$1" body="$2"
  local token chat
  token="$(sed -n 's/^SAU_ALERT_TELEGRAM_BOT_TOKEN=//p' "$REPO_ROOT/.env" 2>/dev/null | tail -1 | tr -d '"'\''')"
  chat="$(sed -n 's/^SAU_ALERT_TELEGRAM_CHAT_ID=//p' "$REPO_ROOT/.env" 2>/dev/null | tail -1 | tr -d '"'\''')"
  chat="${chat%%,*}"
  if [[ -z "$token" || -z "$chat" ]]; then
    log "  (no Telegram creds in .env; failure recorded in this log only)"
    return 0
  fi
  if curl -fsS -m 20 -X POST "https://api.telegram.org/bot${token}/sendMessage" \
       --data-urlencode "chat_id=${chat}" \
       --data-urlencode "text=${subject}

${body}" >/dev/null 2>&1; then
    log "  failure alert delivered"
  else
    log "  failure alert delivery failed"
  fi
}

# ---------------------------------------------------------------------------
# Single-flight
# ---------------------------------------------------------------------------
exec 9>"$LOCK"
if ! flock -n 9; then
  log "another ci-watch run holds the lock; exiting"
  exit 0
fi

# Optional one-shot self-test of the alert channel (not used by cron).
if [[ "${CI_WATCH_TEST_ALERT:-0}" == "1" ]]; then
  log "sending a test alert (no action needed)"
  notify_failure "SAU ci-watch: test alert (no action needed)" \
    "One-off verification that ci-watch can reach the alert channel. Nothing is wrong."
  exit 0
fi

# ---------------------------------------------------------------------------
# Resolve origin/main
# ---------------------------------------------------------------------------
if ! GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new}" \
       git fetch --quiet origin main 2>>"$LOG"; then
  # Transient network failure: log but do not alert every two minutes.
  log "git fetch failed; will retry on the next tick"
  exit 0
fi

TARGET="$(git rev-parse origin/main)"
SHORT="${TARGET:0:7}"
IMAGE_SHA="${IMAGE_BASE}:commit-${SHORT}"
LAST=""
[[ -f "$STATE" ]] && LAST="$(cat "$STATE")"

log "origin/main=${SHORT} last_deployed=${LAST:0:7}"

DRY_RUN="${CI_WATCH_DRY_RUN:-0}"
FORCE="${CI_WATCH_FORCE:-0}"

if [[ "$LAST" == "$TARGET" && "$FORCE" != "1" ]]; then
  # State says done — but confirm the running container really is this image, so
  # a manual revert or a failed earlier deploy is corrected automatically.
  running_id="$(docker inspect -f '{{.Image}}' "$SERVICE" 2>/dev/null || true)"
  target_id="$(docker image inspect -f '{{.Id}}' "$IMAGE_SHA" 2>/dev/null || true)"
  if [[ -n "$running_id" && "$running_id" == "$target_id" ]]; then
    log "already deployed and running; nothing to do"
    exit 0
  fi
  log "state says deployed but the running container differs; re-deploying"
fi

if [[ "$DRY_RUN" == "1" ]]; then
  if docker image inspect "$IMAGE_SHA" >/dev/null 2>&1; then
    log "DRY RUN: image ${IMAGE_SHA} exists; would skip build and (re)deploy"
  else
    log "DRY RUN: would build ${IMAGE_SHA} and deploy"
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# Build (only when the image for this commit does not already exist)
# ---------------------------------------------------------------------------
BUILD_LOG="$LOG_DIR/ci-watch-build-${SHORT}.log"
WORKTREE=""
cleanup() {
  if [[ -n "$WORKTREE" && -d "$WORKTREE" ]]; then
    git worktree remove --force "$WORKTREE" >/dev/null 2>&1 || rm -rf "$WORKTREE"
  fi
}
trap cleanup EXIT

if docker image inspect "$IMAGE_SHA" >/dev/null 2>&1 && [[ "$FORCE" != "1" ]]; then
  log "image ${IMAGE_SHA} already exists; skipping build"
else
  git worktree prune >/dev/null 2>&1 || true
  WORKTREE="$(mktemp -d "${TMPDIR:-/tmp}/sau-ci-watch.XXXXXX")"
  if ! git worktree add --detach --quiet "$WORKTREE" "$TARGET" >>"$LOG" 2>&1; then
    log "ERROR: could not create a clean worktree for ${SHORT}"
    notify_failure "SAU ci-watch: worktree checkout failed for ${SHORT}" \
      "Could not check out ${TARGET} into a worktree. See ${LOG}."
    exit 1
  fi
  log "building ${IMAGE_SHA} from clean worktree (log: ${BUILD_LOG})"
  if ! docker build -f Dockerfile -t "$IMAGE_SHA" "$WORKTREE" >>"$BUILD_LOG" 2>&1; then
    log "ERROR: docker build failed for ${SHORT} (log: ${BUILD_LOG})"
    notify_failure "SAU ci-watch: image build failed for ${SHORT}" \
      "docker build failed for ${SHORT} (${TARGET}). See ${BUILD_LOG}."
    exit 1
  fi
  log "build finished for ${SHORT}"
fi

# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------
echo "==> tagging ${IMAGE_BASE}:latest" >>"$LOG"
docker tag "$IMAGE_SHA" "${IMAGE_BASE}:latest"

log "verifying the image runs"
if ! docker run --rm --entrypoint python "$IMAGE_SHA" -c \
     "import sys; sys.path.insert(0,'/app'); from myUtils import platform_limits as pl; print('    threads cap =', pl.media_max_mb('threads'))" \
     >>"$LOG" 2>&1; then
  log "ERROR: built image ${IMAGE_SHA} does not run"
  notify_failure "SAU ci-watch: built image for ${SHORT} is broken" \
    "The image built for ${SHORT} failed its import check. See ${LOG}."
  exit 1
fi

log "restarting ${SERVICE} (pull disabled so the local image is used)"

# ---------------------------------------------------------------------------
# Do not restart on top of in-flight publishes
# ---------------------------------------------------------------------------
# A restart kills the worker's running targets: the container is recreated, the
# in-flight ffmpeg/HTTP work is orphaned, and the rows stay 'running' forever
# because nothing is left to finish them. That is exactly what happened on
# 2026-10-10 when this script re-deployed at 10:42 and stranded six Sociamonials
# publishes for an hour.
#
# So wait for the queue to drain first. The lease/sweep will eventually recover a
# truly stuck target, but a publish in progress is far cheaper to wait for than to
# lose. Set SAU_CI_WATCH_MAX_WAIT_SECONDS to bound the wait (default 30 min);
# when the bound is hit the deploy proceeds and says so, because a permanently
# running target must not block security fixes forever.
MAX_WAIT="${SAU_CI_WATCH_MAX_WAIT_SECONDS:-1800}"
if [[ "$MAX_WAIT" =~ ^[0-9]+$ ]] && (( MAX_WAIT > 0 )); then
  DB="${SAU_DB_PATH:-/home/will/social-auto-upload/db/database.db}"
  waited=0
  while (( waited < MAX_WAIT )); do
    running=$(sqlite3 "$DB" "SELECT COUNT(*) FROM publish_job_targets WHERE status IN ('running','retrying');" 2>/dev/null || echo 0)
    [[ "$running" =~ ^[0-9]+$ ]] || running=0
    (( running == 0 )) && break
    if (( waited % 60 == 0 )); then
      log "waiting for ${running} in-flight target(s) before restart (${waited}s of ${MAX_WAIT}s)"
    fi
    sleep 15
    waited=$(( waited + 15 ))
  done
  if (( running != 0 )); then
    log "WARNING: ${running} target(s) still in flight after ${MAX_WAIT}s; deploying anyway"
    notify_failure "SAU ci-watch: deploying over ${running} in-flight target(s)" \
      "Waited ${MAX_WAIT}s for the queue to drain but ${running} target(s) are still running. The restart will strand them; they will need a resubmit."
  else
    log "queue drained; safe to restart"
  fi
fi

if ! docker compose up -d --force-recreate --pull never "$SERVICE" >>"$LOG" 2>&1; then
  log "ERROR: docker compose up failed for ${SHORT}"
  notify_failure "SAU ci-watch: deploy command failed for ${SHORT}" \
    "docker compose up failed for ${SHORT}. See ${LOG}."
  exit 1
fi

log "waiting for ${HEALTH_URL}"
code=""
for _ in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "$HEALTH_URL" || true)"
  [[ "$code" == "200" ]] && break
  sleep 2
done

if [[ "$code" != "200" ]]; then
  log "ERROR: healthz did not return 200 for ${SHORT} (last: ${code:-none})"
  notify_failure "SAU ci-watch: deploy unhealthy for ${SHORT}" \
    "healthz did not return 200 after deploying ${SHORT} (last: ${code:-none}). See ${LOG}."
  exit 1
fi

echo "$TARGET" > "$STATE"
running="$(docker ps --filter "name=${SERVICE}" --format '{{.Status}}')"
log "deployed ${SHORT} (healthz 200; container: ${running})"

# ---------------------------------------------------------------------------
# Reclaim disk: drop older commit-* images
# ---------------------------------------------------------------------------
# Every build produces a ~3 GB image tagged commit-<sha>, and nothing ever
# removed them, so the list grew without bound (11 images after one day). Layers
# are shared, but the newest layers are not, so this is a real leak on a 97 GB
# disk that also hosts a dozen other services.
#
# Keep the running image plus the newest KEEP_IMAGES commit tags so a rollback is
# one `docker tag` away, and delete the rest. Only tags produced by this script
# are touched: no other repo's images, no containers, no volumes. Failures are
# logged and ignored - cleanup must never turn a healthy deploy into a failure.
KEEP_IMAGES="${SAU_CI_WATCH_KEEP_IMAGES:-3}"
if [[ "$KEEP_IMAGES" =~ ^[0-9]+$ ]] && (( KEEP_IMAGES >= 1 )); then
  # Newest first; skip the first KEEP_IMAGES entries and remove the remainders.
  mapfile -t stale_tags < <(
    docker images "${IMAGE_BASE}" --format '{{.Tag}} {{.CreatedAt}}' \
      | grep -E '^commit-' \
      | sort -k2 -r \
      | awk '{print $1}' \
      | tail -n +$((KEEP_IMAGES + 1))
  )
  for tag in "${stale_tags[@]:-}"; do
    [[ -z "$tag" ]] && continue
    # Never remove the tag the current commit resolves to.
    [[ "$tag" == "commit-${SHORT}" ]] && continue
    if docker rmi "${IMAGE_BASE}:${tag}" >>"$LOG" 2>&1; then
      log "pruned old image ${IMAGE_BASE}:${tag}"
    else
      log "could not prune ${IMAGE_BASE}:${tag} (in use?); leaving it"
    fi
  done
  # Untagged intermediates from failed builds are always safe to drop.
  docker image prune -f --filter "dangling=true" >>"$LOG" 2>&1 || true
fi
