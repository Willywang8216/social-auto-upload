"""Durable, workspace-scoped publish submission receipts.

Revision ID: 0024_publish_submissions
Revises: 0023_publish_reliability

The receipt is committed in the same transaction as its preparing campaigns.
A non-null scope keeps legacy (no workspace) submissions unique too: SQLite's
nullable composite UNIQUE semantics must not permit duplicate legacy retries.
"""
from __future__ import annotations

from alembic import op

revision = "0024_publish_submissions"
down_revision = "0023_publish_reliability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS publish_submissions (
            scope TEXT NOT NULL,
            submission_id TEXT NOT NULL,
            workspace_id TEXT,
            request_hash TEXT NOT NULL,
            request_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (scope, submission_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS publish_submissions")
