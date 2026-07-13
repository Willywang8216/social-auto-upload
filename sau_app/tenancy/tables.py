"""Single source of truth for the tenant-owned table set (Phase 5).

Mirrors ``migrations/versions/0017_workspace_id_expand.TENANT_TABLES`` — a test
asserts the two lists stay identical. Kept import-light (no package side effects)
so it is cheap to import from the backfill engine.
"""

from __future__ import annotations

# Every table that carries a ``workspace_id``. Excludes operator-level
# infrastructure (storage_backends) and the identity/session tables themselves.
TENANT_TABLES: tuple[str, ...] = (
    "user_info",
    "profiles",
    "accounts",
    "file_records",
    "media_groups",
    "media_group_items",
    "media_assets",
    "campaigns",
    "campaign_artifacts",
    "campaign_posts",
    "publish_jobs",
    "publish_job_targets",
    "publish_templates",
    "watermark_configs",
    "sheet_exports",
    "prepared_posts",
    "account_events",
    "video_analytics_videos",
    "video_analytics_snapshots",
    "analytics_sync_log",
    "tiktok_publish_status",
    "tiktok_review_events",
    "tiktok_oauth_requests",
    "reddit_oauth_requests",
    "youtube_oauth_requests",
    "meta_oauth_requests",
    "threads_oauth_requests",
    "twitter_oauth_requests",
)
