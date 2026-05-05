"""reddit oauth persistence

Adds durable storage for Reddit OAuth state tokens so the callback survives
process restarts and validates the expected saved account.

Revision ID: 0005_reddit_oauth_persistence
Revises: 0004_account_events
Create Date: 2026-05-05
"""
from __future__ import annotations

from alembic import op


revision = "0005_reddit_oauth_persistence"
down_revision = "0004_account_events"
branch_labels = None
depends_on = None


CREATE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS reddit_oauth_requests (
        state_token TEXT PRIMARY KEY,
        profile_id INTEGER,
        account_id INTEGER,
        account_name TEXT,
        redirect_uri TEXT NOT NULL,
        scopes_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'started',
        requested_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        completed_at DATETIME,
        error_text TEXT,
        result_json TEXT NOT NULL DEFAULT '{}',
        FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE SET NULL,
        FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE SET NULL
    )
    """,
)

CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_reddit_oauth_requests_account ON reddit_oauth_requests(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_reddit_oauth_requests_status ON reddit_oauth_requests(status)",
)

DROP_STATEMENTS = (
    "DROP TABLE IF EXISTS reddit_oauth_requests",
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
