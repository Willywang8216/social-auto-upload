"""tiktok review persistence

Adds durable storage for TikTok OAuth requests and callback/webhook review
receipts so the review/demo flow survives process restarts.

Revision ID: 0003_tiktok_review_persistence
Revises: 0002_campaign_profiles_media
Create Date: 2026-05-05
"""
from __future__ import annotations

from alembic import op


revision = "0003_tiktok_review_persistence"
down_revision = "0002_campaign_profiles_media"
branch_labels = None
depends_on = None


CREATE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS tiktok_oauth_requests (
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
    """
    CREATE TABLE IF NOT EXISTS tiktok_review_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'received',
        account_id INTEGER,
        account_name TEXT,
        signature_verified INTEGER,
        signature_status TEXT,
        payload_json TEXT NOT NULL DEFAULT '{}',
        headers_json TEXT NOT NULL DEFAULT '{}',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        received_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE SET NULL
    )
    """,
)

CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_tiktok_oauth_requests_account ON tiktok_oauth_requests(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_tiktok_oauth_requests_status ON tiktok_oauth_requests(status)",
    "CREATE INDEX IF NOT EXISTS idx_tiktok_review_events_type ON tiktok_review_events(event_type)",
    "CREATE INDEX IF NOT EXISTS idx_tiktok_review_events_received_at ON tiktok_review_events(received_at)",
)

DROP_STATEMENTS = (
    "DROP TABLE IF EXISTS tiktok_review_events",
    "DROP TABLE IF EXISTS tiktok_oauth_requests",
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
