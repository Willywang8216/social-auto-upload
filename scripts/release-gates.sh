#!/usr/bin/env bash
# One gate implementation for CI, image publishing, the watcher and manual release.
# Tests run in a disposable source-only worktree, never beside production .env/data.
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${SAU_RELEASE_PYTHON:-python3}"
MODE="${1:-all}"
[[ $# == 0 ]] || shift
VERSION=""
RECEIPTS=""
while (( $# )); do
  case "$1" in
    --python) VERSION="$2"; shift 2 ;;
    --receipts) RECEIPTS="$2"; shift 2 ;;
    *) echo "usage: release-gates.sh [all|backend|frontend|verify] [--python 3.10|3.12] --receipts DIR" >&2; exit 2 ;;
  esac
done
[[ "$MODE" =~ ^(all|backend|frontend|verify)$ ]] || exit 2
REVISION="$("$PYTHON" "$ROOT/scripts/release_checks.py" --root "$ROOT" clean)"
RECEIPTS="${RECEIPTS:-$ROOT/logs/releases/gates/$REVISION}"
mkdir -p "$RECEIPTS"
RECEIPTS="$(cd "$RECEIPTS" && pwd)"
if [[ "$MODE" == verify ]]; then
  exec "$PYTHON" "$ROOT/scripts/release_checks.py" --root "$ROOT" verify --revision "$REVISION" --receipts "$RECEIPTS"
fi
[[ "$(uname -s)" == Linux ]] || { echo 'Release gates require Linux (Python 3.10 and 3.12).' >&2; exit 1; }
command -v uv >/dev/null
[[ "$(uv --version)" == 'uv 0.12.7'* ]] || { echo 'Use the pinned release uv 0.12.7.' >&2; exit 1; }
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/sau-release-gates.XXXXXX")"
TREE="$SCRATCH/source"
cleanup() {
  git -C "$ROOT" worktree remove --force "$TREE" >/dev/null 2>&1 || true
  rm -rf "$SCRATCH"  # only the directory made by this invocation
}
trap cleanup EXIT
# No shell config is sourced and no ignored runtime files enter this checkout.
git -C "$ROOT" worktree add --detach --quiet "$TREE" "$REVISION"
[[ ! -e "$TREE/.env" && ! -e "$TREE/conf.py" && ! -e "$TREE/db/database.db" ]]
cp "$TREE/conf.example.py" "$TREE/conf.py"
# A fresh HOME plus an allowlisted environment prevents shell/.netrc/cloud
# credentials and production configuration from reaching tests or npm scripts.
mkdir -p "$SCRATCH/home"
run_clean() {
  env -i PATH="$PATH" HOME="$SCRATCH/home" LANG=C.UTF-8 \
    TEMP="$SCRATCH" TMP="$SCRATCH" TMPDIR="$SCRATCH" \
    PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$TREE" \
    SAU_TESTING=1 APP_ENV=testing SAU_PROCESS_ROLE=web AWS_EC2_METADATA_DISABLED=true \
    UV_NO_ENV_FILE=1 UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$SCRATCH/venv}" "$@"
}
cd "$TREE"
backend() {
  local version="$1"
  export UV_PROJECT_ENVIRONMENT="$SCRATCH/venv-$version"
  run_clean uv sync --frozen --python "$version" --extra web --group dev --no-install-project
  local python="$UV_PROJECT_ENVIRONMENT/bin/python"
  run_clean "$python" -m patchright install chromium
  run_clean "$python" -X utf8 scripts/release_checks.py static
  run_clean "$python" -X utf8 -m pytest tests/ -q --basetemp "$SCRATCH/pytest-$version"
  run_clean "$python" scripts/release_checks.py clean --revision "$REVISION" >/dev/null
  run_clean "$python" scripts/release_checks.py record --revision "$REVISION" --receipts "$RECEIPTS" --component "backend-$version"
}
frontend() {
  [[ "$(node --version)" == v22.21.1 ]] || { echo 'Use release Node 22.21.1.' >&2; exit 1; }
  (cd sau_frontend && run_clean npm ci && run_clean npm test && run_clean npm run build)
  run_clean "$PYTHON" scripts/release_checks.py clean --revision "$REVISION" >/dev/null
  run_clean "$PYTHON" scripts/release_checks.py record --revision "$REVISION" --receipts "$RECEIPTS" --component frontend
}
case "$MODE" in
  all) backend 3.10; backend 3.12; frontend ;;
  backend) [[ "$VERSION" == 3.10 || "$VERSION" == 3.12 ]] || exit 2; backend "$VERSION" ;;
  frontend) frontend ;;
esac
"$PYTHON" "$ROOT/scripts/release_checks.py" --root "$ROOT" clean --revision "$REVISION" >/dev/null
if [[ "$MODE" == all ]]; then
  "$PYTHON" scripts/release_checks.py verify --revision "$REVISION" --receipts "$RECEIPTS"
fi
