"""Idempotent schema bootstrap for social-auto-upload.

Running this script is safe on a fresh database, on the legacy
(``user_info``, ``file_records``) layout, and on the current layout that
adds the Profile / job-runtime tables. It never drops data; it only adds
what is missing.

There are two ways into this module:

* ``python db/createTable.py`` — invokes ``alembic upgrade head`` against
  the canonical ``db/database.db`` file, which is now the supported
  production path. New schema work is added as a new migration under
  ``migrations/versions/`` rather than by editing the table definitions
  in this file.

* ``bootstrap(db_path)`` — the Python-level entry point used by the test
  suite to spin up a throwaway DB without paying the cost of an Alembic
  config load. It runs the raw-SQL ``CREATE TABLE IF NOT EXISTS`` chain
  defined here AND stamps the result with Alembic's current head, so the
  resulting database is indistinguishable from one bootstrapped via
  ``alembic upgrade``. The two paths must therefore always agree on the
  schema; ``tests/test_alembic.py`` enforces that.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from myUtils.env_loader import load_repo_env

load_repo_env()

DB_PATH = Path(__file__).resolve().parent / "database.db"

# Alembic config lives at the workspace root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI_PATH = _PROJECT_ROOT / "alembic.ini"


LEGACY_USER_INFO = """
CREATE TABLE IF NOT EXISTS user_info (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type INTEGER NOT NULL,
    filePath TEXT NOT NULL,
    userName TEXT NOT NULL,
    status INTEGER DEFAULT 0
)
"""

FILE_RECORDS = """
CREATE TABLE IF NOT EXISTS file_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    filesize REAL,
    upload_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    file_path TEXT
)
"""

PROFILES = """
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    settings_json TEXT NOT NULL DEFAULT '{}',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

ACCOUNTS = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    account_name TEXT NOT NULL,
    cookie_path TEXT NOT NULL,
    auth_type TEXT NOT NULL DEFAULT 'cookie',
    config_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    status INTEGER NOT NULL DEFAULT 0,
    last_checked_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(profile_id, platform, account_name),
    FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
)
"""

PUBLISH_JOBS = """
CREATE TABLE IF NOT EXISTS publish_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    profile_id INTEGER,
    platform TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    total_targets INTEGER NOT NULL DEFAULT 0,
    completed_targets INTEGER NOT NULL DEFAULT 0,
    failed_targets INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    started_at DATETIME,
    finished_at DATETIME,
    FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE SET NULL
)
"""

PUBLISH_JOB_TARGETS = """
CREATE TABLE IF NOT EXISTS publish_job_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    account_ref TEXT NOT NULL,
    file_ref TEXT NOT NULL,
    schedule_at DATETIME,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    started_at DATETIME,
    finished_at DATETIME,
    UNIQUE(job_id, account_ref, file_ref),
    FOREIGN KEY(job_id) REFERENCES publish_jobs(id) ON DELETE CASCADE
)
"""

MEDIA_GROUPS = """
CREATE TABLE IF NOT EXISTS media_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    notes TEXT DEFAULT '',
    primary_video_file_id INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(primary_video_file_id) REFERENCES file_records(id) ON DELETE SET NULL
)
"""

MEDIA_GROUP_ITEMS = """
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
"""

CAMPAIGNS = """
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
"""

CAMPAIGN_ARTIFACTS = """
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
"""

CAMPAIGN_POSTS = """
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
"""

TIKTOK_OAUTH_REQUESTS = """
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
"""

REDDIT_OAUTH_REQUESTS = """
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
"""

YOUTUBE_OAUTH_REQUESTS = """
CREATE TABLE IF NOT EXISTS youtube_oauth_requests (
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
"""

META_OAUTH_REQUESTS = """
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
"""

THREADS_OAUTH_REQUESTS = """
CREATE TABLE IF NOT EXISTS threads_oauth_requests (
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
"""

TIKTOK_REVIEW_EVENTS = """
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
"""

ACCOUNT_EVENTS = """
CREATE TABLE IF NOT EXISTS account_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    profile_id INTEGER,
    platform TEXT NOT NULL,
    account_name TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ok',
    summary TEXT NOT NULL DEFAULT '',
    error_text TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE SET NULL,
    FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE SET NULL
)
"""

VIDEO_ANALYTICS_VIDEOS = """
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
"""

VIDEO_ANALYTICS_SNAPSHOTS = """
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
"""

ANALYTICS_SYNC_LOG = """
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
"""

STORAGE_BACKENDS = """
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
"""

TIKTOK_PUBLISH_STATUS = """
CREATE TABLE IF NOT EXISTS tiktok_publish_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publish_id TEXT NOT NULL UNIQUE,
    job_id TEXT,
    account_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'processing',
    fail_reason TEXT,
    post_id TEXT,
    platform_url TEXT,
    polled_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

WATERMARK_CONFIGS = """
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
"""

MEDIA_ASSETS = """
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
"""

SHEET_EXPORTS = """
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
"""

PREPARED_POSTS = """
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
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_accounts_profile ON accounts(profile_id)",
    "CREATE INDEX IF NOT EXISTS idx_accounts_platform ON accounts(platform)",
    "CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status)",
    "CREATE INDEX IF NOT EXISTS idx_accounts_enabled ON accounts(enabled)",
    "CREATE INDEX IF NOT EXISTS idx_publish_jobs_status ON publish_jobs(status)",
    "CREATE INDEX IF NOT EXISTS idx_publish_jobs_platform ON publish_jobs(platform)",
    "CREATE INDEX IF NOT EXISTS idx_publish_job_targets_job ON publish_job_targets(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_publish_job_targets_status ON publish_job_targets(status)",
    "CREATE INDEX IF NOT EXISTS idx_publish_job_targets_account ON publish_job_targets(account_ref)",
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
    "CREATE INDEX IF NOT EXISTS idx_tiktok_oauth_requests_account ON tiktok_oauth_requests(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_tiktok_oauth_requests_status ON tiktok_oauth_requests(status)",
    "CREATE INDEX IF NOT EXISTS idx_reddit_oauth_requests_account ON reddit_oauth_requests(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_reddit_oauth_requests_status ON reddit_oauth_requests(status)",
    "CREATE INDEX IF NOT EXISTS idx_youtube_oauth_requests_account ON youtube_oauth_requests(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_youtube_oauth_requests_status ON youtube_oauth_requests(status)",
    "CREATE INDEX IF NOT EXISTS idx_meta_oauth_requests_account ON meta_oauth_requests(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_meta_oauth_requests_status ON meta_oauth_requests(status)",
    "CREATE INDEX IF NOT EXISTS idx_threads_oauth_requests_account ON threads_oauth_requests(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_threads_oauth_requests_status ON threads_oauth_requests(status)",
    "CREATE INDEX IF NOT EXISTS idx_tiktok_review_events_type ON tiktok_review_events(event_type)",
    "CREATE INDEX IF NOT EXISTS idx_tiktok_review_events_received_at ON tiktok_review_events(received_at)",
    "CREATE INDEX IF NOT EXISTS idx_account_events_account ON account_events(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_account_events_profile ON account_events(profile_id)",
    "CREATE INDEX IF NOT EXISTS idx_account_events_platform ON account_events(platform)",
    "CREATE INDEX IF NOT EXISTS idx_account_events_action ON account_events(action)",
    "CREATE INDEX IF NOT EXISTS idx_account_events_created_at ON account_events(created_at)",
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
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_sb_default ON storage_backends(is_default) WHERE is_default = 1",
    "CREATE INDEX IF NOT EXISTS idx_tps_job ON tiktok_publish_status(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_tps_account ON tiktok_publish_status(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_tps_status ON tiktok_publish_status(status)",
    "CREATE INDEX IF NOT EXISTS idx_fr_storage_key ON file_records(storage_key)",
    "CREATE INDEX IF NOT EXISTS idx_fr_storage_backend ON file_records(storage_backend_id)",
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
]


REQUIRED_COLUMNS = {
    "profiles": {
        "settings_json": "settings_json TEXT NOT NULL DEFAULT '{}'",
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
    },
    "accounts": {
        "auth_type": "auth_type TEXT NOT NULL DEFAULT 'cookie'",
        "config_json": "config_json TEXT NOT NULL DEFAULT '{}'",
        "enabled": "enabled INTEGER NOT NULL DEFAULT 1",
    },
    "file_records": {
        "storage_backend_id": "storage_backend_id INTEGER REFERENCES storage_backends(id) ON DELETE SET NULL",
        "storage_key": "storage_key TEXT",
        "storage_cdn_url": "storage_cdn_url TEXT",
        "local_cleaned_at": "local_cleaned_at DATETIME",
    },
    "campaign_artifacts": {
        "storage_backend_id": "storage_backend_id INTEGER REFERENCES storage_backends(id) ON DELETE SET NULL",
    },
    "media_groups": {
        "profile_id": "profile_id INTEGER REFERENCES profiles(id) ON DELETE SET NULL",
        "group_type": "group_type TEXT DEFAULT 'mixed'",
        "content_theme": "content_theme TEXT DEFAULT ''",
        "user_notes": "user_notes TEXT DEFAULT ''",
        "status": "status TEXT NOT NULL DEFAULT 'draft'",
    },
    "campaigns": {
        "name": "name TEXT DEFAULT ''",
        "campaign_date": "campaign_date TEXT DEFAULT ''",
        "scheduling_mode": "scheduling_mode TEXT DEFAULT 'queue'",
        "default_schedule_start": "default_schedule_start DATETIME",
        "default_schedule_interval_minutes": "default_schedule_interval_minutes INTEGER DEFAULT 0",
        "randomize_minutes": "randomize_minutes INTEGER NOT NULL DEFAULT 0",
        "notes": "notes TEXT DEFAULT ''",
    },
}


def _existing_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


def _ensure_required_columns(conn: sqlite3.Connection) -> None:
    for table_name, columns in REQUIRED_COLUMNS.items():
        existing = _existing_columns(conn, table_name)
        for column_name, definition in columns.items():
            if column_name in existing:
                continue
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {definition}")


def _stamp_alembic_head(db_path: Path) -> None:
    """Best-effort: mark the DB as up-to-date with Alembic head.

    We avoid actually running migrations from inside ``bootstrap`` because
    spinning up an Alembic config is expensive and the test suite calls
    this function on every test setup. Instead we directly write the
    current head revision into ``alembic_version`` so any later
    ``alembic upgrade`` is a no-op.

    Alembic might not be installed in extremely minimal environments
    (running tests against a stripped-down sandbox); we degrade
    gracefully there.
    """

    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
    except ImportError:
        return

    try:
        cfg = Config(str(ALEMBIC_INI_PATH))
        head_rev = ScriptDirectory.from_config(cfg).get_current_head()
    except Exception:
        # Couldn't load the config (e.g. ini missing in a packaged wheel);
        # leaving the version table empty just means a future
        # ``alembic upgrade head`` will run the baseline migration, which
        # is itself idempotent.
        return
    if head_rev is None:
        return

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS alembic_version "
            "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        existing = conn.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO alembic_version (version_num) VALUES (?)",
                (head_rev,),
            )
        elif existing[0] != head_rev:
            # A real migration would normally do this; we just keep the
            # row in sync so the bootstrap path doesn't drift behind.
            conn.execute(
                "UPDATE alembic_version SET version_num = ?",
                (head_rev,),
            )
        conn.commit()


def bootstrap(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        cursor = conn.cursor()
        cursor.execute(LEGACY_USER_INFO)
        cursor.execute(FILE_RECORDS)
        cursor.execute(PROFILES)
        cursor.execute(ACCOUNTS)
        cursor.execute(PUBLISH_JOBS)
        cursor.execute(PUBLISH_JOB_TARGETS)
        cursor.execute(MEDIA_GROUPS)
        cursor.execute(MEDIA_GROUP_ITEMS)
        cursor.execute(CAMPAIGNS)
        cursor.execute(CAMPAIGN_ARTIFACTS)
        cursor.execute(CAMPAIGN_POSTS)
        cursor.execute(TIKTOK_OAUTH_REQUESTS)
        cursor.execute(REDDIT_OAUTH_REQUESTS)
        cursor.execute(YOUTUBE_OAUTH_REQUESTS)
        cursor.execute(META_OAUTH_REQUESTS)
        cursor.execute(THREADS_OAUTH_REQUESTS)
        cursor.execute(TIKTOK_REVIEW_EVENTS)
        cursor.execute(ACCOUNT_EVENTS)
        cursor.execute(VIDEO_ANALYTICS_VIDEOS)
        cursor.execute(VIDEO_ANALYTICS_SNAPSHOTS)
        cursor.execute(ANALYTICS_SYNC_LOG)
        cursor.execute(STORAGE_BACKENDS)
        cursor.execute(TIKTOK_PUBLISH_STATUS)
        cursor.execute(WATERMARK_CONFIGS)
        cursor.execute(MEDIA_ASSETS)
        cursor.execute(SHEET_EXPORTS)
        cursor.execute(PREPARED_POSTS)
        _ensure_required_columns(conn)
        for statement in INDEXES:
            cursor.execute(statement)
        # Seed default storage backend from env vars if present
        import os
        do_key = os.environ.get("DO_SPACES_KEY", "")
        if do_key:
            cursor.execute(
                """
                INSERT OR IGNORE INTO storage_backends
                (slug, label, provider, bucket, region, endpoint, access_key, secret_key, cdn_url, is_default)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    "default",
                    "Default DO Spaces",
                    "do_spaces",
                    os.environ.get("DO_SPACES_BUCKET", "sau-media"),
                    os.environ.get("DO_SPACES_REGION", "sgp1"),
                    f"https://{os.environ.get('DO_SPACES_REGION', 'sgp1')}.digitaloceanspaces.com",
                    do_key,
                    os.environ.get("DO_SPACES_SECRET", ""),
                    os.environ.get("DO_SPACES_CDN_URL", ""),
                ),
            )
        conn.commit()
    _stamp_alembic_head(db_path)


def _alembic_upgrade_head(db_path: Path = DB_PATH) -> None:
    """Run ``alembic upgrade head`` against the given DB file.

    This is what ``python db/createTable.py`` invokes — the supported
    production path. Tests use the lighter ``bootstrap()`` path above.
    """

    from alembic import command
    from alembic.config import Config

    db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = Config(str(ALEMBIC_INI_PATH))
    cfg.set_main_option(
        "sqlalchemy.url", f"sqlite:///{db_path.resolve()}"
    )
    command.upgrade(cfg, "head")


if __name__ == "__main__":
    target = Path(os.environ.get("SAU_DB_PATH", DB_PATH))
    _alembic_upgrade_head(target)
    print(f"OK alembic upgraded {target} to head")
