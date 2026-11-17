#!/usr/bin/env bash
# Docker is the only backend/test runtime. Host shell only controls Docker.
set -euo pipefail
umask 077
CODE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(realpath -e "${SAU_VPS_ROOT:-$CODE_ROOT}")"
REVISION="${1:?usage: vps-gates.sh REVISION COMPONENT IMAGE_ID RULES_ROOT [OUTPUT]}"
COMPONENT="${2:?frontend or backend-3.12/backend-3.13 required}"
IMAGE="${3:?immutable verification image ID required}"
RULES="${4:?versioned private rules root required}"
OUTPUT="${5:-$ROOT/ops/verification/$REVISION/receipts}"
[[ "$(uname -s)" == Linux && "$REVISION" =~ ^[0-9a-f]{40}$ ]]
[[ "$COMPONENT" =~ ^(frontend|backend-3\.12|backend-3\.13)$ ]]
[[ "$IMAGE" =~ ^sha256:[0-9a-f]{64}$ ]]
for tool in git docker realpath awk; do command -v "$tool" >/dev/null; done
[[ "$(docker image inspect --format '{{index .Config.Labels "io.sau.verification-tools"}}' "$IMAGE")" == 1 ]]
OBJECTS="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)/objects"
git -C "$ROOT" cat-file -e "$REVISION^{commit}"
RULES="$(realpath -e "$RULES")"
[[ -f "$RULES/humanizer/scripts/check_tells.py" && -f "$RULES/weiwei-senpai-writing/SKILL.md" ]]
OUTPUT="$(realpath -m "$OUTPUT")"
[[ "$OUTPUT" == "$ROOT/ops/verification/"* && "$RULES" == "$ROOT/ops/toolchains/rules/"* ]]
mkdir -p "$OUTPUT"
[[ "$(realpath -e "$OUTPUT")" == "$OUTPUT" ]]
# Reserve the same host headroom; tmpfs avoids new permanent test copies.
[[ "$(awk '/MemAvailable:/{print $2}' /proc/meminfo)" -ge 10485760 ]]
[[ "$(df -B1 --output=avail "$ROOT" | tail -1 | tr -d ' ')" -ge 2147483648 ]]
NAME="sau-gates-${REVISION:0:12}-${COMPONENT//./-}"
if docker container inspect "$NAME" >/dev/null 2>&1; then
  echo "Existing container $NAME: inspect its original result, do not resubmit." >&2
  exit 75
fi
MOUNTS=(--mount "type=bind,src=$OBJECTS,dst=/git-objects,readonly"
        --mount "type=bind,src=$RULES,dst=/rules,readonly"
        --mount "type=bind,src=$OUTPUT,dst=/output")
if [[ "$COMPONENT" != frontend ]]; then
  [[ -f "$OUTPUT/katex/katex.min.js" && -f "$OUTPUT/frontend.json" ]]
  MOUNTS+=(--mount "type=bind,src=$OUTPUT/katex,dst=/katex,readonly")
fi
docker run --detach --name "$NAME" --label io.sau.ops=verification \
  --label "io.sau.source-revision=$REVISION" --label "io.sau.component=$COMPONENT" \
  --read-only --no-healthcheck --init --pids-limit 256 --cpus 2 \
  --cap-drop ALL --security-opt no-new-privileges \
  --memory 6g --memory-swap 6g --tmpfs /work:rw,nosuid,nodev,size=4g,uid=1000,gid=1000,mode=0700 \
  --user 1000:1000 "${MOUNTS[@]}" "$IMAGE" "$REVISION" "$COMPONENT"
printf 'Started %s. Use docker wait/logs/inspect; archive output before removing this exact labeled container.\n' "$NAME"
