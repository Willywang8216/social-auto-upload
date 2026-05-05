"""meta oauth persistence

Adds durable storage for Meta OAuth state tokens so Facebook/Instagram connect
callbacks survive process restarts and map back to saved structured accounts.

Revision ID: 0007_meta_oauth_persistence
Revises: 0006_youtube_oauth_persistence
Create Date: 2026-05-05
"""
from __future__ import annotations

from alembic import op


revision = "0007_meta_oauth_persistence"
down_revision = "0006_youtube_oauth_persistence"
branch_labels = None
depends_on = None

CREATE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS meta_oauth_requests (
        state_token TEXT PRIMARY KEY,
        profile_id INTEGER,
        account_id INTEGER,
        account_name TEXT,
        platform TEXT NOT NULL,
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
    "CREATE INDEX IF NOT EXISTS idx_meta_oauth_requests_account ON meta_oauth_requests(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_meta_oauth_requests_status ON meta_oauth_requests(status)",
)

DROP_STATEMENTS = (
    "DROP TABLE IF EXISTS meta_oauth_requests",
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
