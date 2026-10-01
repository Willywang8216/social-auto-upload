"""Add composite indexes for schedule and publish-entity read paths."""
from __future__ import annotations

from alembic import op


revision = "0021_schedule_read_indexes"
down_revision = "0020_telegram_review_cards"
branch_labels = None
depends_on = None


_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_pjt_schedule_status_job "
    "ON publish_job_targets(schedule_at, status, job_id)",
    "CREATE INDEX IF NOT EXISTS idx_pjt_job_account "
    "ON publish_job_targets(job_id, account_ref)",
    "CREATE INDEX IF NOT EXISTS idx_pjt_job_file "
    "ON publish_job_targets(job_id, file_ref)",
    "CREATE INDEX IF NOT EXISTS idx_pj_profile_platform_id "
    "ON publish_jobs(profile_id, platform, id)",
    "CREATE INDEX IF NOT EXISTS idx_campaigns_profile_group "
    "ON campaigns(profile_id, media_group_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_mgi_group_sort "
    "ON media_group_items(media_group_id, sort_order, id)",
)


def upgrade() -> None:
    # CREATE INDEX IF NOT EXISTS is supported by both SQLite and PostgreSQL.
    # Keep these additive so existing deployments can upgrade without a table
    # rewrite or a destructive migration.
    for statement in _INDEXES:
        op.execute(statement)


def downgrade() -> None:
    for name in (
        "idx_pjt_schedule_status_job",
        "idx_pjt_job_account",
        "idx_pjt_job_file",
        "idx_pj_profile_platform_id",
        "idx_campaigns_profile_group",
        "idx_mgi_group_sort",
    ):
        op.execute(f"DROP INDEX IF EXISTS {name}")
