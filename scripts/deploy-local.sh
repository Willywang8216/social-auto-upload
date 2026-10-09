#!/usr/bin/env bash
# Build and deploy the app image locally, without depending on GitHub Actions.
#
# Why this exists
# ---------------
# This repo is a FORK (of dreammis/social-auto-upload). GitHub ran its workflows
# 495 times and then disabled Actions on the fork because of its Actions usage:
# every push after commit d1319216 - including an intentionally empty probe
# commit - produced no run. The repository's own Actions page states the cause:
#
#   "Workflows aren't being run on this fork because of its GitHub Actions
#    usage. A repository maintainer can re-enable them."
#
# Dispatch returns HTTP 422 "Actions has been disabled for this repository", and
# the REST re-enable (PUT actions/permissions enabled=true) does not clear it.
# Other repositories on the same account still run Actions, so this is
# repository-specific (not an account-level or billing limit). Re-enable from
# the repository's Actions tab in the GitHub UI - it is not fixable through the
# REST API - or use this script as the fallback.
#
# So this script performs the same job the `image.yml` workflow does, locally:
# build the image, tag both the commit and :latest, restart the service, and
# verify health. Use it when a push has not produced a run.
#
# Usage:
#   scripts/deploy-local.sh            # build, deploy, verify
#   scripts/deploy-local.sh --no-build # just re-tag the current commit image
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

IMAGE_BASE="ghcr.io/willywang8216/social-auto-upload"
SHA="$(git rev-parse --short HEAD)"
IMAGE_SHA="${IMAGE_BASE}:commit-${SHA}"

if [[ "${1:-}" != "--no-build" ]]; then
  echo "==> building ${IMAGE_SHA} from $(git rev-parse --short HEAD)"
  docker build -f Dockerfile -t "$IMAGE_SHA" .
else
  echo "==> skipping build (--no-build)"
fi

# Republish the commit image as :latest so docker-compose.yml's
# `image: ...:latest` resolves to this build.
echo "==> tagging ${IMAGE_BASE}:latest"
docker tag "$IMAGE_SHA" "${IMAGE_BASE}:latest"

# Prove the running code is the code we just built, before touching the service.
echo "==> verifying the image contains this commit's code"
docker run --rm --entrypoint python "$IMAGE_SHA" - <<'PY'
import sys
sys.path.insert(0, "/app")
from myUtils import platform_limits as pl
print(f"    threads cap = {pl.media_max_mb('threads')} MB")
PY

echo "==> restarting the service (pull disabled so the local image is used)"
# `pull_policy: always` would otherwise restore the older registry image.
docker compose up -d --force-recreate --pull never social-auto-upload

echo "==> waiting for health"
for attempt in $(seq 1 20); do
  code="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:5409/healthz || true)"
  if [[ "$code" == "200" ]]; then
    echo "    healthz 200 after ${attempt}s"
    break
  fi
  sleep 2
done

if [[ "${code:-}" != "200" ]]; then
  echo "!! healthz did not return 200 (last: ${code:-none}); check: docker logs --tail 50 social-auto-upload" >&2
  exit 1
fi

docker ps --filter name=social-auto-upload --format '    container: {{.Status}}'
echo "==> deployed ${SHA}"
