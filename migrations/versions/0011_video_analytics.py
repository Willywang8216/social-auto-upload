"""video analytics tables

Adds tables for tracking video performance metrics across platforms:
- video_analytics_videos: canonical video registry (one row per video per platform)
- video_analytics_snapshots: point-in-time metrics readings
- analytics_sync_log: tracks data sync jobs

Revision ID: 0011_video_analytics
Revises: 0010_publish_templates_and_post_files
Create Date: 2026-05-22
"""
from __future__ import annotations

from alembic import op


revision = "0011_video_analytics"
down_revision = "0010_publish_templates_and_post_files"
branch_labels = None
depends_on = None


CREATE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS video_analytics_videos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        platform TEXT NOT NULL,
        platform_video_id TEXT NOT NULL UNIQUE,
        file_record_id INTEGER,
        title TEXT DEFAULT '',
        description TEXT DEFAULT '',
        thumbnail_url TEXT DEFAULT '',
        published_at DATETIME,
        duration_seconds INTEGER DEFAULT 0,
        last_synced_at DATETIME,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE,
        FOREIGN KEY(file_record_id) REFERENCES file_records(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS video_analytics_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        platform TEXT NOT NULL,
        platform_video_id TEXT NOT NULL,
        file_record_id INTEGER,
        title TEXT DEFAULT '',
        thumbnail_url TEXT DEFAULT '',
        published_at DATETIME,
        snapshot_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        views INTEGER DEFAULT 0,
        likes INTEGER DEFAULT 0,
        comments INTEGER DEFAULT 0,
        shares INTEGER DEFAULT 0,
        watch_time_seconds INTEGER DEFAULT 0,
        engagement_rate REAL DEFAULT 0.0,
        raw_metrics_json TEXT NOT NULL DEFAULT '{}',
        FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE,
        FOREIGN KEY(file_record_id) REFERENCES file_records(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS analytics_sync_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        platform TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        videos_synced INTEGER DEFAULT 0,
        error_text TEXT,
        started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        finished_at DATETIME,
        FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
    )
    """,
)

CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_vav_account ON video_analytics_videos(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_vav_platform ON video_analytics_videos(platform)",
    "CREATE INDEX IF NOT EXISTS idx_vav_published ON video_analytics_videos(published_at)",
    "CREATE INDEX IF NOT EXISTS idx_vas_account ON video_analytics_snapshots(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_vas_platform ON video_analytics_snapshots(platform)",
    "CREATE INDEX IF NOT EXISTS idx_vas_video_id ON video_analytics_snapshots(platform_video_id)",
    "CREATE INDEX IF NOT EXISTS idx_vas_snapshot_at ON video_analytics_snapshots(snapshot_at)",
    "CREATE INDEX IF NOT EXISTS idx_vas_file_record ON video_analytics_snapshots(file_record_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_vas_unique_snapshot ON video_analytics_snapshots(account_id, platform_video_id, snapshot_at)",
    "CREATE INDEX IF NOT EXISTS idx_asl_account ON analytics_sync_log(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_asl_status ON analytics_sync_log(status)",
)

DROP_STATEMENTS = (
    "DROP TABLE IF EXISTS analytics_sync_log",
    "DROP TABLE IF EXISTS video_analytics_snapshots",
    "DROP TABLE IF EXISTS video_analytics_videos",
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
