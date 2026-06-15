"""socialupload full schema — watermark configs, media assets, sheet exports, prepared posts

Adds the tables and columns needed for the full SocialUpload workflow:
batch upload, watermarking, content generation, sheet export, and publishing.

Revision ID: 0014_socialupload_full_schema
Revises: 0013_tiktok_publish_status
Create Date: 2026-06-15
"""
from __future__ import annotations

from alembic import op


revision = "0014_socialupload_full_schema"
down_revision = "0013_tiktok_publish_status"
branch_labels = None
depends_on = None


NEW_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS watermark_configs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile_id INTEGER,
        name TEXT NOT NULL DEFAULT 'default',
        watermark_type TEXT NOT NULL DEFAULT 'text',
        text TEXT DEFAULT '',
        image_path TEXT DEFAULT '',
        opacity REAL NOT NULL DEFAULT 0.3,
        scale REAL NOT NULL DEFAULT 0.15,
        margin INTEGER NOT NULL DEFAULT 24,
        randomize_position INTEGER NOT NULL DEFAULT 0,
        video_dynamic_position INTEGER NOT NULL DEFAULT 0,
        video_position_change_min_seconds INTEGER NOT NULL DEFAULT 1,
        video_position_change_max_seconds INTEGER NOT NULL DEFAULT 5,
        allowed_positions TEXT NOT NULL DEFAULT '["top_left","top_right","bottom_left","bottom_right"]',
        font_family TEXT DEFAULT '',
        font_size INTEGER DEFAULT 0,
        font_color TEXT DEFAULT 'white',
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS media_assets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        original_filename TEXT NOT NULL,
        media_type TEXT NOT NULL DEFAULT 'video',
        mime_type TEXT DEFAULT '',
        local_original_path TEXT DEFAULT '',
        local_processed_path TEXT DEFAULT '',
        rclone_remote_path TEXT DEFAULT '',
        public_url TEXT DEFAULT '',
        processed_public_url TEXT DEFAULT '',
        thumbnail_public_url TEXT DEFAULT '',
        duration_seconds REAL DEFAULT 0,
        width INTEGER DEFAULT 0,
        height INTEGER DEFAULT 0,
        file_size INTEGER DEFAULT 0,
        checksum TEXT DEFAULT '',
        upload_status TEXT NOT NULL DEFAULT 'pending',
        processing_status TEXT NOT NULL DEFAULT 'pending',
        transcript_text TEXT DEFAULT '',
        content_analysis_json TEXT NOT NULL DEFAULT '{}',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        file_record_id INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(file_record_id) REFERENCES file_records(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sheet_exports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id INTEGER,
        profile_id INTEGER,
        sheet_name TEXT NOT NULL DEFAULT '',
        spreadsheet_id TEXT DEFAULT '',
        spreadsheet_url TEXT DEFAULT '',
        exported_at DATETIME,
        row_count INTEGER DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        error_message TEXT DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE SET NULL,
        FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS prepared_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id INTEGER NOT NULL,
        media_group_id INTEGER,
        profile_id INTEGER,
        platform TEXT NOT NULL,
        account_id INTEGER,
        target_name TEXT DEFAULT '',
        message TEXT DEFAULT '',
        title TEXT DEFAULT '',
        description TEXT DEFAULT '',
        first_comment TEXT DEFAULT '',
        hashtags TEXT DEFAULT '',
        link TEXT DEFAULT '',
        image_urls TEXT DEFAULT '',
        video_url TEXT DEFAULT '',
        video_thumbnail_url TEXT DEFAULT '',
        alt_text TEXT DEFAULT '',
        post_preset TEXT DEFAULT '',
        category TEXT DEFAULT '',
        watermark_name TEXT DEFAULT '',
        hashtag_group TEXT DEFAULT '',
        cta_group TEXT DEFAULT '',
        story_flag INTEGER NOT NULL DEFAULT 0,
        pinterest_board TEXT DEFAULT '',
        scheduled_month TEXT DEFAULT '',
        scheduled_day TEXT DEFAULT '',
        scheduled_year TEXT DEFAULT '',
        scheduled_hour TEXT DEFAULT '',
        scheduled_minute TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'generated',
        validation_errors_json TEXT NOT NULL DEFAULT '[]',
        llm_raw_output_json TEXT NOT NULL DEFAULT '{}',
        char_count INTEGER DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE,
        FOREIGN KEY(media_group_id) REFERENCES media_groups(id) ON DELETE SET NULL,
        FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE SET NULL,
        FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE SET NULL
    )
    """,
)

PROFILE_COLUMNS = {
    "default_language": "default_language TEXT DEFAULT 'en'",
    "timezone": "timezone TEXT DEFAULT 'UTC'",
    "system_prompt": "system_prompt TEXT DEFAULT ''",
    "writing_style_prompt": "writing_style_prompt TEXT DEFAULT ''",
    "contact_details": "contact_details TEXT DEFAULT ''",
    "default_cta": "default_cta TEXT DEFAULT ''",
    "default_hashtags": "default_hashtags TEXT DEFAULT ''",
    "default_link": "default_link TEXT DEFAULT ''",
    "watermark_config_id": "watermark_config_id INTEGER REFERENCES watermark_configs(id) ON DELETE SET NULL",
    "google_sheet_folder_id": "google_sheet_folder_id TEXT DEFAULT ''",
}

MEDIA_GROUP_COLUMNS = {
    "profile_id": "profile_id INTEGER REFERENCES profiles(id) ON DELETE SET NULL",
    "group_type": "group_type TEXT DEFAULT 'mixed'",
    "content_theme": "content_theme TEXT DEFAULT ''",
    "user_notes": "user_notes TEXT DEFAULT ''",
    "status": "status TEXT NOT NULL DEFAULT 'draft'",
}

CAMPAIGN_COLUMNS = {
    "name": "name TEXT DEFAULT ''",
    "campaign_date": "campaign_date TEXT DEFAULT ''",
    "scheduling_mode": "scheduling_mode TEXT DEFAULT 'queue'",
    "default_schedule_start": "default_schedule_start DATETIME",
    "default_schedule_interval_minutes": "default_schedule_interval_minutes INTEGER DEFAULT 0",
    "randomize_minutes": "randomize_minutes INTEGER NOT NULL DEFAULT 0",
    "notes": "notes TEXT DEFAULT ''",
}

NEW_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_watermark_configs_profile ON watermark_configs(profile_id)",
    "CREATE INDEX IF NOT EXISTS idx_media_assets_upload_status ON media_assets(upload_status)",
    "CREATE INDEX IF NOT EXISTS idx_media_assets_processing_status ON media_assets(processing_status)",
    "CREATE INDEX IF NOT EXISTS idx_media_assets_media_type ON media_assets(media_type)",
    "CREATE INDEX IF NOT EXISTS idx_media_assets_file_record ON media_assets(file_record_id)",
    "CREATE INDEX IF NOT EXISTS idx_sheet_exports_campaign ON sheet_exports(campaign_id)",
    "CREATE INDEX IF NOT EXISTS idx_sheet_exports_profile ON sheet_exports(profile_id)",
    "CREATE INDEX IF NOT EXISTS idx_sheet_exports_status ON sheet_exports(status)",
    "CREATE INDEX IF NOT EXISTS idx_prepared_posts_campaign ON prepared_posts(campaign_id)",
    "CREATE INDEX IF NOT EXISTS idx_prepared_posts_platform ON prepared_posts(platform)",
    "CREATE INDEX IF NOT EXISTS idx_prepared_posts_status ON prepared_posts(status)",
    "CREATE INDEX IF NOT EXISTS idx_prepared_posts_account ON prepared_posts(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_prepared_posts_profile ON prepared_posts(profile_id)",
    "CREATE INDEX IF NOT EXISTS idx_media_groups_profile ON media_groups(profile_id)",
    "CREATE INDEX IF NOT EXISTS idx_media_groups_status ON media_groups(status)",
    "CREATE INDEX IF NOT EXISTS idx_campaigns_scheduling_mode ON campaigns(scheduling_mode)",
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
    # Create new tables first (for FK references)
    for statement in NEW_TABLES:
        op.execute(statement)
    # Add columns to existing tables
    for col_name, definition in PROFILE_COLUMNS.items():
        _maybe_add_column("profiles", col_name, definition)
    for col_name, definition in MEDIA_GROUP_COLUMNS.items():
        _maybe_add_column("media_groups", col_name, definition)
    for col_name, definition in CAMPAIGN_COLUMNS.items():
        _maybe_add_column("campaigns", col_name, definition)
    # Add indexes
    for statement in NEW_INDEXES:
        op.execute(statement)


def downgrade() -> None:
    # SQLite doesn't support DROP COLUMN; drop new tables only
    op.execute("DROP TABLE IF EXISTS prepared_posts")
    op.execute("DROP TABLE IF EXISTS sheet_exports")
    op.execute("DROP TABLE IF EXISTS media_assets")
    op.execute("DROP TABLE IF EXISTS watermark_configs")
