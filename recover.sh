#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

usage() {
  echo "Usage:"
  echo "  bash recover.sh --latest"
  echo "  bash recover.sh --remote <remote:path/file.tar.gz>"
  exit 1
}

REMOTE_SPEC=""
MODE="${1:-}"

if [[ "$MODE" == "--latest" ]]; then
  REMOTE_SPEC="$(python - <<'PY'
import json
import subprocess
import sys
from pathlib import Path

root_dir = Path.cwd()
config_path = root_dir / "db" / "profile_backup_config.json"
if not config_path.exists():
    raise SystemExit("db/profile_backup_config.json not found")

config = json.loads(config_path.read_text(encoding="utf-8"))
remote_name = str(config.get("remoteName") or "").strip()
remote_path = str(config.get("remotePath") or "").strip().strip("/")
if not remote_name:
    raise SystemExit("backup remoteName is not configured")

remote_dir_spec = f"{remote_name}:{remote_path}" if remote_path else f"{remote_name}:"
result = subprocess.run(
    ["rclone", "lsf", remote_dir_spec, "--files-only"],
    check=False,
    capture_output=True,
    text=True,
)
if result.returncode != 0:
    raise SystemExit(result.stderr.strip() or result.stdout.strip() or "rclone lsf failed")

filenames = sorted(
    [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().startswith("profiles-backup-") and line.strip().endswith(".tar.gz")
    ],
    reverse=True,
)
if not filenames:
    raise SystemExit("no backup archive found")

filename = filenames[0]
print(f"{remote_name}:{remote_path + '/' if remote_path else ''}{filename}")
PY
)"
elif [[ "$MODE" == "--remote" && $# -ge 2 ]]; then
  REMOTE_SPEC="$2"
else
  usage
fi

if ! command -v rclone >/dev/null 2>&1; then
  echo "rclone is required" >&2
  exit 1
fi

WORK_DIR="$(mktemp -d)"
ARCHIVE_PATH="$WORK_DIR/backup.tar.gz"
cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

echo "Downloading backup: $REMOTE_SPEC"
rclone copyto "$REMOTE_SPEC" "$ARCHIVE_PATH"

echo "Extracting backup into $ROOT_DIR"
tar -xzf "$ARCHIVE_PATH" -C "$ROOT_DIR"

echo "Recovery completed from: $REMOTE_SPEC"
