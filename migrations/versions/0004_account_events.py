"""account events

Adds a cross-platform audit/event table for account health checks and token
refresh operations so operator actions are queryable outside of ad-hoc config
inspection.

Revision ID: 0004_account_events
Revises: 0003_tiktok_review_persistence
Create Date: 2026-05-05
"""
from __future__ import annotations

from alembic import op


revision = "0004_account_events"
down_revision = "0003_tiktok_review_persistence"
branch_labels = None
depends_on = None


CREATE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS account_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER,
        profile_id INTEGER,
        platform TEXT NOT NULL,
        account_name TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'ok',
        summary TEXT NOT NULL DEFAULT '',
        error_text TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE SET NULL,
        FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE SET NULL
    )
    """,
)

CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_account_events_account ON account_events(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_account_events_profile ON account_events(profile_id)",
    "CREATE INDEX IF NOT EXISTS idx_account_events_platform ON account_events(platform)",
    "CREATE INDEX IF NOT EXISTS idx_account_events_action ON account_events(action)",
    "CREATE INDEX IF NOT EXISTS idx_account_events_created_at ON account_events(created_at)",
)

DROP_STATEMENTS = (
    "DROP TABLE IF EXISTS account_events",
)


def upgrade() -> None:
    op.execute("PRAGMA foreign_keys = ON")
    for statement in CREATE_STATEMENTS:
        op.execute(statement)
    for statement in CREATE_INDEXES:
        op.execute(statement)


def downgrade() -> None:
    for statement in DROP_STATEMENTS:
        op.execute(statement)
