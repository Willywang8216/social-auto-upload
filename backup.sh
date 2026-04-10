#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

python - <<'PY'
from pathlib import Path

from conf import BASE_DIR
from utils.account_registry import ensure_account_tables
from utils.profile_pipeline import ensure_profile_tables, run_profile_backup
from utils.publish_jobs import ensure_publish_job_tables

base_dir = Path(BASE_DIR)
db_path = base_dir / "db" / "database.db"
db_path.parent.mkdir(parents=True, exist_ok=True)

ensure_account_tables(db_path)
ensure_profile_tables(db_path)
ensure_publish_job_tables(db_path)

result = run_profile_backup(base_dir, db_path)
print(f"Backup completed: {result['remoteSpec']}")
PY
