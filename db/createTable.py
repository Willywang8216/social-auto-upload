"""Idempotent schema bootstrap for social-auto-upload.

Running this script is safe on a fresh database, on the legacy
(``user_info``, ``file_records``) layout, and on the current layout that
adds the Profile / job-runtime tables. It never drops data; it only adds
what is missing.

There are two ways into this module:

* ``python db/createTable.py`` — invokes ``alembic upgrade head`` against
  the canonical ``db/database.db`` file, which is now the supported
  production path. New schema work is added as a new migration under
  ``migrations/versions/`` rather than by editing the table definitions
  in this file.

* ``bootstrap(db_path)`` — the Python-level entry point used by the test
  suite to spin up a throwaway DB without paying the cost of an Alembic
  config load. It runs the raw-SQL ``CREATE TABLE IF NOT EXISTS`` chain
  defined here AND stamps the result with Alembic's current head, so the
  resulting database is indistinguishable from one bootstrapped via
  ``alembic upgrade``. The two paths must therefore always agree on the
  schema; ``tests/test_alembic.py`` enforces that.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "database.db"

# Alembic config lives at the workspace root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI_PATH = _PROJECT_ROOT / "alembic.ini"


LEGACY_USER_INFO = """
CREATE TABLE IF NOT EXISTS user_info (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type INTEGER NOT NULL,
    filePath TEXT NOT NULL,
    userName TEXT NOT NULL,
    status INTEGER DEFAULT 0
)
"""

FILE_RECORDS = """
CREATE TABLE IF NOT EXISTS file_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    filesize REAL,
    upload_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    file_path TEXT
)
"""

PROFILES = """
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

ACCOUNTS = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    account_name TEXT NOT NULL,
    cookie_path TEXT NOT NULL,
    status INTEGER NOT NULL DEFAULT 0,
    last_checked_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(profile_id, platform, account_name),
    FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
)
"""

PUBLISH_JOBS = """
CREATE TABLE IF NOT EXISTS publish_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    profile_id INTEGER,
    platform TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    total_targets INTEGER NOT NULL DEFAULT 0,
    completed_targets INTEGER NOT NULL DEFAULT 0,
    failed_targets INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    started_at DATETIME,
    finished_at DATETIME,
    FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE SET NULL
)
"""

PUBLISH_JOB_TARGETS = """
CREATE TABLE IF NOT EXISTS publish_job_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    account_ref TEXT NOT NULL,
    file_ref TEXT NOT NULL,
    schedule_at DATETIME,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    started_at DATETIME,
    finished_at DATETIME,
    UNIQUE(job_id, account_ref, file_ref),
    FOREIGN KEY(job_id) REFERENCES publish_jobs(id) ON DELETE CASCADE
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_accounts_profile ON accounts(profile_id)",
    "CREATE INDEX IF NOT EXISTS idx_accounts_platform ON accounts(platform)",
    "CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status)",
    "CREATE INDEX IF NOT EXISTS idx_publish_jobs_status ON publish_jobs(status)",
    "CREATE INDEX IF NOT EXISTS idx_publish_jobs_platform ON publish_jobs(platform)",
    "CREATE INDEX IF NOT EXISTS idx_publish_job_targets_job ON publish_job_targets(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_publish_job_targets_status ON publish_job_targets(status)",
    "CREATE INDEX IF NOT EXISTS idx_publish_job_targets_account ON publish_job_targets(account_ref)",
]


def _stamp_alembic_head(db_path: Path) -> None:
    """Best-effort: mark the DB as up-to-date with Alembic head.

    We avoid actually running migrations from inside ``bootstrap`` because
    spinning up an Alembic config is expensive and the test suite calls
    this function on every test setup. Instead we directly write the
    current head revision into ``alembic_version`` so any later
    ``alembic upgrade`` is a no-op.

    Alembic might not be installed in extremely minimal environments
    (running tests against a stripped-down sandbox); we degrade
    gracefully there.
    """

    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
    except ImportError:
        return

    try:
        cfg = Config(str(ALEMBIC_INI_PATH))
        head_rev = ScriptDirectory.from_config(cfg).get_current_head()
    except Exception:
        # Couldn't load the config (e.g. ini missing in a packaged wheel);
        # leaving the version table empty just means a future
        # ``alembic upgrade head`` will run the baseline migration, which
        # is itself idempotent.
        return
    if head_rev is None:
        return

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS alembic_version "
            "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        existing = conn.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO alembic_version (version_num) VALUES (?)",
                (head_rev,),
            )
        elif existing[0] != head_rev:
            # A real migration would normally do this; we just keep the
            # row in sync so the bootstrap path doesn't drift behind.
            conn.execute(
                "UPDATE alembic_version SET version_num = ?",
                (head_rev,),
            )
        conn.commit()


def bootstrap(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        cursor = conn.cursor()
        cursor.execute(LEGACY_USER_INFO)
        cursor.execute(FILE_RECORDS)
        cursor.execute(PROFILES)
        cursor.execute(ACCOUNTS)
        cursor.execute(PUBLISH_JOBS)
        cursor.execute(PUBLISH_JOB_TARGETS)
        for statement in INDEXES:
            cursor.execute(statement)
        conn.commit()
    _stamp_alembic_head(db_path)


def _alembic_upgrade_head(db_path: Path = DB_PATH) -> None:
    """Run ``alembic upgrade head`` against the given DB file.

    This is what ``python db/createTable.py`` invokes — the supported
    production path. Tests use the lighter ``bootstrap()`` path above.
    """

    from alembic import command
    from alembic.config import Config

    db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = Config(str(ALEMBIC_INI_PATH))
    cfg.set_main_option(
        "sqlalchemy.url", f"sqlite:///{db_path.resolve()}"
    )
    command.upgrade(cfg, "head")


if __name__ == "__main__":
    target = Path(os.environ.get("SAU_DB_PATH", DB_PATH))
    _alembic_upgrade_head(target)
    print(f"OK alembic upgraded {target} to head")