"""Idempotent schema bootstrap for social-auto-upload.

Running this script is safe on a fresh database, on the legacy (`user_info`,
`file_records`) layout, and on the current layout that adds the `profiles` and
`accounts` tables. It never drops data; it only adds what is missing.

The Profile model represents a person/brand who can hold many accounts across
many platforms. The same Profile may own more than one account on the same
platform (e.g. two Medium accounts under one brand).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "database.db"


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


if __name__ == "__main__":
    bootstrap()
    print(f"OK schema ensured at {DB_PATH}")