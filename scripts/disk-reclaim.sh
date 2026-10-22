#!/usr/bin/env bash
# Daily disk safety net for the SAU host.
#
# Why: on 2026-10-21 the 97 GB disk reached 99%. The two silent consumers were
# (a) the Docker BuildKit build cache, which `docker image prune` never touches
# and which every local ci-watch build grew, and (b) old `commit-<sha>` images
# from repeated deploys. A full disk breaks SQLite writes and ffmpeg, and the
# resulting failures look like unrelated bugs.
#
# ci-watch now prunes the build cache after each deploy; this runs regardless so
# builds made outside ci-watch (manual docker build, other repos) cannot fill the
# disk either. It only removes cache and dangling/untagged layers - never a
# tagged image in use, never a container, never a volume.
set -uo pipefail
LOG=/home/will/social-auto-upload/logs/disk-reclaim.log
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

{
  echo "[$(ts)] reclaim start"
  before=$(df --output=avail -BG / | tail -1 | tr -dc '0-9')
  docker builder prune -f --keep-storage "${SAU_BUILD_CACHE_KEEP:-3GB}" 2>&1 | tail -2
  docker image prune -f --filter "dangling=true" 2>&1 | tail -1
  after=$(df --output=avail -BG / | tail -1 | tr -dc '0-9')
  used=$(df --output=pcent / | tail -1 | tr -dc '0-9')
  echo "[$(ts)] reclaim done free=${after}G (was ${before}G) used=${used}%"
} >>"$LOG" 2>&1
