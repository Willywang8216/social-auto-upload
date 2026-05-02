"""campaign/profile/media schema additions

Adds structured profile/account configuration plus the core tables needed for
media grouping and campaign preparation.

Revision ID: 0002_campaign_profiles_media
Revises: 0001_baseline
Create Date: 2026-05-01
"""
from __future__ import annotations

from alembic import op


revision = "0002_campaign_profiles_media"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


CREATE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS media_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        notes TEXT DEFAULT '',
        primary_video_file_id INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(primary_video_file_id) REFERENCES file_records(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS media_group_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        media_group_id INTEGER NOT NULL,
        file_record_id INTEGER NOT NULL,
        role TEXT NOT NULL DEFAULT 'attachment',
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(media_group_id, file_record_id),
        FOREIGN KEY(media_group_id) REFERENCES media_groups(id) ON DELETE CASCADE,
        FOREIGN KEY(file_record_id) REFERENCES file_records(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS campaigns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile_id INTEGER NOT NULL,
        media_group_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft',
        selected_account_ids_json TEXT NOT NULL DEFAULT '[]',
        sheet_spreadsheet_id TEXT,
        sheet_title TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        prepared_at DATETIME,
        published_at DATETIME,
        last_error TEXT,
        FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
        FOREIGN KEY(media_group_id) REFERENCES media_groups(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS campaign_artifacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id INTEGER NOT NULL,
        source_file_record_id INTEGER,
        artifact_kind TEXT NOT NULL,
        local_path TEXT,
        public_url TEXT,
        remote_path TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE,
        FOREIGN KEY(source_file_record_id) REFERENCES file_records(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS campaign_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id INTEGER NOT NULL,
        platform TEXT NOT NULL,
        account_ids_json TEXT NOT NULL DEFAULT '[]',
        draft_json TEXT NOT NULL DEFAULT '{}',
        sheet_row_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'draft',
        last_published_job_id INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE,
        FOREIGN KEY(last_published_job_id) REFERENCES publish_jobs(id) ON DELETE SET NULL
    )
    """,
)


CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_accounts_enabled ON accounts(enabled)",
    "CREATE INDEX IF NOT EXISTS idx_media_groups_primary_video ON media_groups(primary_video_file_id)",
    "CREATE INDEX IF NOT EXISTS idx_media_group_items_group ON media_group_items(media_group_id)",
    "CREATE INDEX IF NOT EXISTS idx_media_group_items_file ON media_group_items(file_record_id)",
    "CREATE INDEX IF NOT EXISTS idx_campaigns_profile ON campaigns(profile_id)",
    "CREATE INDEX IF NOT EXISTS idx_campaigns_media_group ON campaigns(media_group_id)",
    "CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status)",
    "CREATE INDEX IF NOT EXISTS idx_campaign_artifacts_campaign ON campaign_artifacts(campaign_id)",
    "CREATE INDEX IF NOT EXISTS idx_campaign_artifacts_kind ON campaign_artifacts(artifact_kind)",
    "CREATE INDEX IF NOT EXISTS idx_campaign_posts_campaign ON campaign_posts(campaign_id)",
    "CREATE INDEX IF NOT EXISTS idx_campaign_posts_platform ON campaign_posts(platform)",
    "CREATE INDEX IF NOT EXISTS idx_campaign_posts_status ON campaign_posts(status)",
)


DROP_STATEMENTS = (
    "DROP TABLE IF EXISTS campaign_posts",
    "DROP TABLE IF EXISTS campaign_artifacts",
    "DROP TABLE IF EXISTS campaigns",
    "DROP TABLE IF EXISTS media_group_items",
    "DROP TABLE IF EXISTS media_groups",
)


def _column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    rows = bind.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


def _maybe_add_column(table_name: str, column_name: str, definition: str) -> None:
    if column_name in _column_names(table_name):
        return
    op.execute(f"ALTER TABLE {table_name} ADD COLUMN {definition}")


def upgrade() -> None:
    op.execute("PRAGMA foreign_keys = ON")
    _maybe_add_column(
        "profiles",
        "settings_json",
        "settings_json TEXT NOT NULL DEFAULT '{}'",
    )
    _maybe_add_column(
        "accounts",
        "auth_type",
        "auth_type TEXT NOT NULL DEFAULT 'cookie'",
    )
    _maybe_add_column(
        "accounts",
        "config_json",
        "config_json TEXT NOT NULL DEFAULT '{}'",
    )
    _maybe_add_column(
        "accounts",
        "enabled",
        "enabled INTEGER NOT NULL DEFAULT 1",
    )
    for statement in CREATE_STATEMENTS:
        op.execute(statement)
    for statement in CREATE_INDEXES:
        op.execute(statement)


def downgrade() -> None:
    # Best-effort downgrade. SQLite column removal is intentionally omitted
    # here; we drop only the tables introduced by this revision.
    for statement in DROP_STATEMENTS:
        op.execute(statement)
