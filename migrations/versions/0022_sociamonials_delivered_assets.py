"""sociamonials delivered assets: the progressive-upload reclaim ledger

The Sociamonials workspace storage allowance is a pooled, hard ceiling on the
reusable media library.  X publishes through the Sociamonials fallback, and
every fallback upload stays in that library forever, so the allowance fills and
the next upload is refused with ``storage_quota_exceeded``.

The operator's rule is to add an asset, publish it, and only then add the next
one.  This table is the record that makes that safe: after a target is
transitioned to ``succeeded`` the worker writes the ``asset_id`` the fallback
attached, so a later sweep knows the asset's post actually delivered and can
delete it once no ``pending``/``retrying``/``running`` target still needs it.

It is additive: with no recorded delivery the reclaim tool keeps every asset.

Revision ID: 0022_sociamonials_delivered_assets
Revises: 0021_schedule_read_indexes
Create Date: 2026-10-11
"""
from __future__ import annotations

from alembic import op


revision = "0022_sociamonials_delivered_assets"
down_revision = "0021_schedule_read_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sociamonials_delivered_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL,
            workspace_registration_id INTEGER NOT NULL DEFAULT 0,
            filename TEXT NOT NULL DEFAULT '',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            media_type TEXT NOT NULL DEFAULT '',
            job_id INTEGER,
            target_id INTEGER,
            post_id INTEGER,
            network TEXT NOT NULL DEFAULT '',
            delivered_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reclaimed_at DATETIME,
            UNIQUE(workspace_registration_id, asset_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_sm_delivered_filename "
        "ON sociamonials_delivered_assets(filename)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_sm_delivered_filename")
    op.execute("DROP TABLE IF EXISTS sociamonials_delivered_assets")
