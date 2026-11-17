#!/usr/bin/env bash
# Host controller only: fixed jobs execute in an immutable operations container.
set -euo pipefail
umask 077
CODE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(realpath -e "${SAU_VPS_ROOT:-$CODE_ROOT}")"
JOB="${1:?usage: vps-ops.sh JOB}"
[[ $# == 1 && "$(uname -s)" == Linux ]]
case "$JOB" in
  watcher|inbox-monitor|pipeline-monitor|inbox-archive|inbox-autopublish|vault-credentials|vault-llm-stt) ;;
  *) echo 'Unsupported operations job.' >&2; exit 2 ;;
esac
for tool in docker flock realpath; do command -v "$tool" >/dev/null; done
OPS="$ROOT/ops/container-jobs"
IMAGE="$(cat "$OPS/image-id.txt")"
[[ "$IMAGE" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$(docker image inspect --format '{{index .Config.Labels "io.sau.operations-tools"}}' "$IMAGE")" == 1 ]]
BASE="$(docker image inspect --format '{{index .Config.Labels "io.sau.runtime-image"}}' "$IMAGE")"
# An operational overlay is bound to the existing production pair, not a
# second publisher or an unapproved application/schema rollout.
[[ "$BASE" == "$(docker inspect --format '{{.Image}}' social-auto-upload)" \
   && "$BASE" == "$(docker inspect --format '{{.Image}}' social-auto-upload-worker)" ]]
mkdir -p "$OPS/locks"
[[ ! -L "$OPS/locks" && ! -L "$OPS/locks/$JOB.lock" ]]
exec 9>>"$OPS/locks/$JOB.lock"
if ! flock -n 9; then echo "ops $JOB skipped: previous run still owns the lock"; exit 0; fi
NAME="sau-ops-$JOB"
if docker container inspect "$NAME" >/dev/null 2>&1; then
  echo "Existing $NAME: reconcile the original container, do not replay." >&2
  exit 75
fi
MOUNTS=()
case "$JOB" in
  watcher|inbox-monitor|pipeline-monitor|inbox-archive|inbox-autopublish)
    MOUNTS+=(--mount "type=bind,src=$ROOT/ops/inbox,dst=/app/sau-inbox") ;;
esac
case "$JOB" in
  pipeline-monitor|inbox-archive|vault-credentials|vault-llm-stt|inbox-autopublish)
    MOUNTS+=(--mount "type=bind,src=$ROOT/.env,dst=/app/.env,readonly") ;;
esac
case "$JOB" in
  pipeline-monitor|inbox-archive|vault-credentials)
    MOUNTS+=(--mount "type=bind,src=$ROOT/conf.py,dst=/app/conf.py,readonly"
             --mount "type=bind,src=$ROOT/db,dst=/app/db"
             --mount "type=bind,src=$ROOT/logs,dst=/app/logs") ;;
esac
case "$JOB" in
  vault-credentials)
    MOUNTS+=(--mount "type=bind,src=$ROOT/cookies,dst=/app/cookies,readonly"
             --mount "type=bind,src=$ROOT/cookiesFile,dst=/app/cookiesFile,readonly") ;;
esac
case "$JOB" in
  watcher|inbox-archive) CONFIG="$OPS/config/drive" ;;
  vault-credentials|vault-llm-stt) CONFIG="$OPS/config/onedrive" ;;
  *) CONFIG="" ;;
esac
if [[ -n "$CONFIG" ]]; then
  [[ -f "$CONFIG/rclone.conf" && ! -L "$CONFIG" && ! -L "$CONFIG/rclone.conf" ]]
  # A dedicated parent is writable for rclone's atomic OAuth-token refresh.
  # It contains only the selected remote, never the operator's full config.
  MOUNTS+=(--mount "type=bind,src=$CONFIG,dst=/run/rclone")
fi
ARGS=("$JOB")
[[ "${SAU_OPS_CHECK_ONLY:-0}" != 1 ]] || ARGS+=(--check)
printf 'ops started job=%s image=%s at=%s\n' "$JOB" "$IMAGE" "$(date -u +%FT%TZ)"
set +e
docker run --rm --name "$NAME" --label io.sau.ops=cron --label "io.sau.job=$JOB" \
  --read-only --no-healthcheck --init --user 1000:1000 --pids-limit 128 --cpus 1 \
  --memory 1g --memory-swap 1g --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,nodev,size=256m,mode=1777 \
  --network 1panel-network --add-host host.docker.internal:host-gateway \
  --log-driver local --log-opt max-size=10m --log-opt max-file=2 \
  "${MOUNTS[@]}" "$IMAGE" "${ARGS[@]}"
RC=$?
set -e
printf 'ops finished job=%s rc=%s image=%s at=%s\n' "$JOB" "$RC" "$IMAGE" "$(date -u +%FT%TZ)"
exit "$RC"
