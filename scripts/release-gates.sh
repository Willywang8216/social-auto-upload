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
    *) echo "usage: release-gates.sh [all|backend|frontend|verify|image] [--python 3.12|3.13] --receipts DIR" >&2; exit 2 ;;
  esac
done
[[ "$MODE" =~ ^(all|backend|frontend|verify|image)$ ]] || exit 2
REVISION="$("$PYTHON" "$ROOT/scripts/release_checks.py" --root "$ROOT" clean)"
RECEIPTS="${RECEIPTS:-$ROOT/logs/releases/gates/$REVISION}"
mkdir -p "$RECEIPTS"
RECEIPTS="$(cd "$RECEIPTS" && pwd)"
if [[ "$MODE" == verify ]]; then
  exec "$PYTHON" "$ROOT/scripts/release_checks.py" --root "$ROOT" verify --revision "$REVISION" --receipts "$RECEIPTS"
fi
[[ "$(uname -s)" == Linux ]] || { echo 'Release gates require Linux (Python 3.12 and 3.13).' >&2; exit 1; }
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
# Image CVE evidence and policy gate. It needs a built image, so it takes an
# explicit reference and skips cleanly when the image or the scanner is absent:
# a machine without docker must not fail an otherwise complete release.
image_scan() {
  local image="${SAU_SCAN_IMAGE:-}"
  if [[ -z "$image" ]]; then
    echo 'image-scan: SAU_SCAN_IMAGE is unset; skipping the image CVE scan.' >&2
    return 0
  fi
  SAU_SCAN_OUT="$RECEIPTS/image-cve-scan.json" \
    bash "$ROOT/scripts/audit/image_cve_scan.sh" "$image" "$RECEIPTS/image-cve-scan.json"
}

case "$MODE" in
  all) backend 3.12; backend 3.13; frontend; image_scan ;;
  backend) [[ "$VERSION" == 3.12 || "$VERSION" == 3.13 ]] || exit 2; backend "$VERSION" ;;
  frontend) frontend ;;
  image) image_scan ;;
esac
"$PYTHON" "$ROOT/scripts/release_checks.py" --root "$ROOT" clean --revision "$REVISION" >/dev/null
if [[ "$MODE" == all ]]; then
  "$PYTHON" scripts/release_checks.py verify --revision "$REVISION" --receipts "$RECEIPTS"
fi
