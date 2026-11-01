"""Durable publisher leases, checkpoints, delayed retries and worker presence.

Additive only: no existing states, schedules, counters or deliveries are
rewritten. The immutable enqueue-intent hash lets explicit idempotency keys
reject a different intent even after workers update payloads or users reschedule.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_publish_reliability"
down_revision = "0022_sociamonials_delivered_assets"
branch_labels = None
depends_on = None


_TARGET_COLUMNS = (
    "lease_owner", "lease_token", "lease_expires_at", "next_attempt_at",
    "error_category", "checkpoint_json", "delivery_state",
)


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("publish_job_targets")}
    for name in _TARGET_COLUMNS:
        if name not in columns:
            op.add_column(
                "publish_job_targets",
                sa.Column(name, sa.Text(), nullable=name != "checkpoint_json",
                          server_default=sa.text("'{}'") if name == "checkpoint_json" else None),
            )
    job_columns = {column["name"] for column in sa.inspect(bind).get_columns("publish_jobs")}
    if "intent_hash" not in job_columns:
        op.add_column("publish_jobs", sa.Column("intent_hash", sa.Text(), nullable=True))
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_heartbeats (
            worker_id TEXT PRIMARY KEY,
            heartbeat_at TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'publisher',
            draining INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_pjt_retry_due "
        "ON publish_job_targets(status, next_attempt_at, schedule_at, id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_pjt_lease_expiry "
        "ON publish_job_targets(status, lease_expires_at, account_ref)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_pjt_retry_due")
    op.execute("DROP INDEX IF EXISTS idx_pjt_lease_expiry")
    op.execute("DROP TABLE IF EXISTS worker_heartbeats")
    op.drop_column("publish_jobs", "intent_hash")
    for name in reversed(_TARGET_COLUMNS):
        op.drop_column("publish_job_targets", name)
