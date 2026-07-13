"""tenant expand: add nullable workspace_id to tenant-owned tables

The "expand" step of the multi-tenant migration (expand -> backfill ->
constrain). Adds a nullable ``workspace_id`` column to every tenant-owned table
so existing rows can be assigned to the legacy workspace by the backfill without
any destructive change. Constraints (NOT NULL, composite uniques, FKs, RLS)
follow in a later revision once the backfill has verified clean.

Portable and guarded: uses ``op.add_column`` (SQLite + PostgreSQL) and skips
columns that already exist, so re-runs and partially-migrated databases are
safe.

Revision ID: 0017_workspace_id_expand
Revises: 0016_oauth_login_transactions
Create Date: 2026-07-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_workspace_id_expand"
down_revision = "0016_oauth_login_transactions"
branch_labels = None
depends_on = None

# The tenant-owned tables that gain workspace ownership. Kept in sync with
# sau_app.tenancy.tables.TENANT_TABLES (a test asserts they match). Excludes
# operator-level infrastructure (storage_backends) and the identity tables.
TENANT_TABLES = (
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

# Tables that get a workspace_id index now (hot query paths). The rest are
# indexed in the constrain phase alongside their composite indexes.
_INDEXED = (
    "profiles",
    "accounts",
    "campaigns",
    "publish_jobs",
    "media_assets",
    "media_groups",
)


def _existing_columns(bind, table: str) -> set[str]:
    inspector = sa.inspect(bind)
    try:
        return {col["name"] for col in inspector.get_columns(table)}
    except Exception:  # table missing in some partial states
        return set()


def _existing_tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)
    for table in TENANT_TABLES:
        if table not in tables:
            continue
        if "workspace_id" in _existing_columns(bind, table):
            continue
        op.add_column(table, sa.Column("workspace_id", sa.String(36), nullable=True))
        if table in _INDEXED:
            op.create_index(f"ix_{table}_workspace_id", table, ["workspace_id"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)
    for table in TENANT_TABLES:
        if table not in tables:
            continue
        if "workspace_id" not in _existing_columns(bind, table):
            continue
        if table in _INDEXED:
            try:
                op.drop_index(f"ix_{table}_workspace_id", table_name=table)
            except Exception:
                pass
        op.drop_column(table, "workspace_id")
