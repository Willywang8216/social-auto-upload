"""baseline schema

Captures the schema as it was when migrations were introduced. Mirrors
``db/createTable.py`` exactly so existing deployments — which already have
all of these tables — can stamp this revision without re-creating
anything.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-04-30
"""
from __future__ import annotations

from alembic import op


revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


CREATE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS user_info (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type INTEGER NOT NULL,
        filePath TEXT NOT NULL,
        userName TEXT NOT NULL,
        status INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS file_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        filesize REAL,
        upload_time DATETIME DEFAULT CURRENT_TIMESTAMP,
        file_path TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        slug TEXT NOT NULL UNIQUE,
        description TEXT DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
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
    """,
    """
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
    """,
    """
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
    """,
)


CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_accounts_profile ON accounts(profile_id)",
    "CREATE INDEX IF NOT EXISTS idx_accounts_platform ON accounts(platform)",
    "CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status)",
    "CREATE INDEX IF NOT EXISTS idx_publish_jobs_status ON publish_jobs(status)",
    "CREATE INDEX IF NOT EXISTS idx_publish_jobs_platform ON publish_jobs(platform)",
    "CREATE INDEX IF NOT EXISTS idx_publish_job_targets_job ON publish_job_targets(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_publish_job_targets_status ON publish_job_targets(status)",
    "CREATE INDEX IF NOT EXISTS idx_publish_job_targets_account ON publish_job_targets(account_ref)",
)


DROP_STATEMENTS = (
    "DROP TABLE IF EXISTS publish_job_targets",
    "DROP TABLE IF EXISTS publish_jobs",
    "DROP TABLE IF EXISTS accounts",
    "DROP TABLE IF EXISTS profiles",
    "DROP TABLE IF EXISTS file_records",
    "DROP TABLE IF EXISTS user_info",
)


def upgrade() -> None:
    op.execute("PRAGMA foreign_keys = ON")
    for statement in CREATE_STATEMENTS:
        op.execute(statement)
    for statement in CREATE_INDEXES:
        op.execute(statement)


def downgrade() -> None:
    # Best-effort downgrade. Real deployments should never run this — it
    # drops every table — but we provide the symmetric inverse so Alembic's
    # invariants hold.
    for statement in DROP_STATEMENTS:
        op.execute(statement)
