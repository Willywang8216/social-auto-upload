"""Independent durable publish-failure notification outbox.

Revision ID: 0026_publish_alert_outbox
Revises: 0025_file_record_path_indexes

Additive only: notification retries do not change publish jobs or targets.
Timestamps are supplied by the outbox as fixed-width, aware UTC ISO strings.
Sent rows remain as deduplication receipts; failures have no terminal retry cap.
"""
from __future__ import annotations

from alembic import op

revision = "0026_publish_alert_outbox"
down_revision = "0025_file_record_path_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS publish_alert_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE CHECK (length(event_key) BETWEEN 1 AND 512),
            subject TEXT NOT NULL CHECK (length(subject) BETWEEN 1 AND 256),
            body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 16384),
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'sending', 'sent')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            next_attempt_at TEXT NOT NULL,
            lease_token TEXT,
            lease_expires_at TEXT,
            created_at TEXT NOT NULL,
            sent_at TEXT,
            last_error TEXT CHECK (
                last_error IS NULL OR last_error IN
                    ('sender_did_not_acknowledge', 'sender_raised')
            ),
            CHECK (
                (state = 'sending' AND lease_token IS NOT NULL
                    AND lease_expires_at IS NOT NULL AND sent_at IS NULL)
                OR (state = 'pending' AND lease_token IS NULL
                    AND lease_expires_at IS NULL AND sent_at IS NULL)
                OR (state = 'sent' AND lease_token IS NULL
                    AND lease_expires_at IS NULL AND sent_at IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_publish_alert_outbox_due "
        "ON publish_alert_outbox(state, next_attempt_at, id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_publish_alert_outbox_lease "
        "ON publish_alert_outbox(state, lease_expires_at, id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS publish_alert_outbox")
