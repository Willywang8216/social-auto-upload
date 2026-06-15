"""storage backends for remote media storage

Adds a storage_backends table for S3-compatible storage configurations
(DO Spaces, AWS S3, MinIO). Adds storage tracking columns to file_records
and campaign_artifacts so user-uploaded media can be stored remotely.

Revision ID: 0012_storage_backends
Revises: 0011_video_analytics
Create Date: 2026-05-24
"""
from __future__ import annotations

import os

from alembic import op


revision = "0012_storage_backends"
down_revision = "0011_video_analytics"
branch_labels = None
depends_on = None


CREATE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS storage_backends (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT NOT NULL UNIQUE,
        label TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL DEFAULT 'do_spaces',
        bucket TEXT NOT NULL,
        region TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        access_key TEXT NOT NULL,
        secret_key TEXT NOT NULL,
        cdn_url TEXT NOT NULL DEFAULT '',
        is_default INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
)

CREATE_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_sb_default ON storage_backends(is_default) WHERE is_default = 1",
)

ALTER_STATEMENTS = (
    "ALTER TABLE file_records ADD COLUMN storage_backend_id INTEGER REFERENCES storage_backends(id) ON DELETE SET NULL",
    "ALTER TABLE file_records ADD COLUMN storage_key TEXT",
    "ALTER TABLE file_records ADD COLUMN storage_cdn_url TEXT",
    "ALTER TABLE file_records ADD COLUMN local_cleaned_at DATETIME",
    "ALTER TABLE campaign_artifacts ADD COLUMN storage_backend_id INTEGER REFERENCES storage_backends(id) ON DELETE SET NULL",
)

CREATE_NEW_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_fr_storage_key ON file_records(storage_key)",
    "CREATE INDEX IF NOT EXISTS idx_fr_storage_backend ON file_records(storage_backend_id)",
)


def upgrade() -> None:
    op.execute("PRAGMA foreign_keys = ON")
    for statement in CREATE_STATEMENTS:
        op.execute(statement)
    for statement in CREATE_INDEXES:
        op.execute(statement)
    for statement in ALTER_STATEMENTS:
        try:
            op.execute(statement)
        except Exception:
            pass  # column already exists
    for statement in CREATE_NEW_INDEXES:
        op.execute(statement)

    # Seed default backend from env vars if credentials are present
    key = os.environ.get("DO_SPACES_KEY", "")
    if key:
        from sqlalchemy import text
        op.execute(
            text("""
            INSERT OR IGNORE INTO storage_backends
            (slug, label, provider, bucket, region, endpoint, access_key, secret_key, cdn_url, is_default)
            VALUES (:slug, :label, :provider, :bucket, :region, :endpoint, :access_key, :secret_key, :cdn_url, 1)
            """).bindparams(
                slug="default",
                label="Default DO Spaces",
                provider="do_spaces",
                bucket=os.environ.get("DO_SPACES_BUCKET", "sau-media"),
                region=os.environ.get("DO_SPACES_REGION", "sgp1"),
                endpoint=f"https://{os.environ.get('DO_SPACES_REGION', 'sgp1')}.digitaloceanspaces.com",
                access_key=key,
                secret_key=os.environ.get("DO_SPACES_SECRET", ""),
                cdn_url=os.environ.get("DO_SPACES_CDN_URL", ""),
            )
        )


def downgrade() -> None:
    # SQLite doesn't support DROP COLUMN before 3.35.0; drop and recreate
    # is too destructive. Leave the columns in place.
    op.execute("DROP TABLE IF EXISTS storage_backends")
