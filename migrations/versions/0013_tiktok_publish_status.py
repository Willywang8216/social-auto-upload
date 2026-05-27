"""tiktok publish status tracking

Adds a tiktok_publish_status table to track post-publish lifecycle
(processing, published, failed) by polling TikTok's status/fetch API.

Revision ID: 0013_tiktok_publish_status
Revises: 0012_storage_backends
Create Date: 2026-05-27
"""
from __future__ import annotations

from alembic import op


revision = "0013_tiktok_publish_status"
down_revision = "0012_storage_backends"
branch_labels = None
depends_on = None


CREATE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS tiktok_publish_status (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        publish_id TEXT NOT NULL UNIQUE,
        job_id TEXT,
        account_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'processing',
        fail_reason TEXT,
        post_id TEXT,
        platform_url TEXT,
        polled_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
)

CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_tps_job ON tiktok_publish_status(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_tps_account ON tiktok_publish_status(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_tps_status ON tiktok_publish_status(status)",
)


def upgrade() -> None:
    op.execute("PRAGMA foreign_keys = ON")
    for statement in CREATE_STATEMENTS:
        op.execute(statement)
    for statement in CREATE_INDEXES:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tiktok_publish_status")
