#!/usr/bin/env bash
# Ephemeral Linux source/venv/browser work; no production config or publisher.
set -euo pipefail
umask 077
REVISION="${1:?exact revision required}"
COMPONENT="${2:?frontend or backend-3.12/backend-3.13 required}"
[[ "$REVISION" =~ ^[0-9a-f]{40}$ ]]
[[ "$COMPONENT" =~ ^(frontend|backend-3\.12|backend-3\.13)$ ]]
[[ -d /git-objects && -d /output && -d /rules ]]
[[ ! -e /work/source ]]
export HOME=/work/home TMPDIR=/work/tmp TEMP=/work/tmp TMP=/work/tmp
export UV_CACHE_DIR=/work/uv-cache UV_PYTHON_INSTALL_DIR=/work/python
export PLAYWRIGHT_BROWSERS_PATH=/work/browsers
mkdir -p "$HOME" "$TMPDIR" /work/source
# Only immutable Git objects are mounted. No host config, hooks, credentials,
# runtime DB, media or original worktree metadata enter this private checkout.
git init --quiet /work/source
mkdir -p /work/source/.git/objects/info
printf '/git-objects\n' > /work/source/.git/objects/info/alternates
git -C /work/source update-ref refs/heads/verification "$REVISION"
git -C /work/source symbolic-ref HEAD refs/heads/verification
git -C /work/source reset --hard --quiet "$REVISION"
cd /work/source
[[ ! -e .env && ! -e .env.production && ! -e conf.py && ! -e db/database.db ]]
export SAU_RELEASE_RULES_ROOT=/rules
if [[ "$COMPONENT" == frontend ]]; then
  export SAU_RELEASE_KATEX_EXPORT=/output/katex
  bash scripts/release-gates.sh frontend --receipts /output
else
  version="${COMPONENT#backend-}"
  patch=3.12.14
  [[ "$version" != 3.13 ]] || patch=3.13.15
  uv python install "$patch"
  export UV_PYTHON_PREFERENCE=only-managed
  export SAU_RELEASE_KATEX_ROOT=/katex
  bash scripts/release-gates.sh backend --python "$version" --receipts /output
fi
printf 'verified component=%s revision=%s\n' "$COMPONENT" "$REVISION"
