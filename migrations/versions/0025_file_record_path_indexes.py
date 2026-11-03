"""Index the file_records path and basename lookups.

Revision ID: 0025_file_record_path_indexes
Revises: 0024_publish_submissions

Every media lookup in the publish path resolves a file_records row by
file_path, or by basename, and neither column carried an index. On the
production database that made each lookup a full scan of file_records.
Measured over 60 randomly sampled references: resolving them by exact path
took 21.4 ms and the entity ``file_path = ? OR filename = ?`` lookup took
11.4 ms. With these two indexes the same work takes 0.46 ms and 0.67 ms, so
46x and 17x faster.
"""
from __future__ import annotations

from alembic import op


revision = "0025_file_record_path_indexes"
down_revision = "0024_publish_submissions"
branch_labels = None
depends_on = None


_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_fr_file_path ON file_records(file_path)",
    "CREATE INDEX IF NOT EXISTS idx_fr_filename ON file_records(filename)",
)


def upgrade() -> None:
    # Additive only, so an existing deployment upgrades without a table rewrite.
    for statement in _INDEXES:
        op.execute(statement)


def downgrade() -> None:
    for name in ("idx_fr_file_path", "idx_fr_filename"):
        op.execute(f"DROP INDEX IF EXISTS {name}")
