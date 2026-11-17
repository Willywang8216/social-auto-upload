"""Add Teaching repurpose tasks, immutable kits and delivery journals.

Revision ID: 0028_teaching_repurpose
Revises: 0027_publish_readiness_and_integrity
No existing articles, jobs, receipts, budgets or reservations are changed.
"""
from alembic import op
from myUtils.repurpose.store import SCHEMA

revision = '0028_teaching_repurpose'
down_revision = '0027_publish_readiness_and_integrity'
branch_labels = None
depends_on = None


def upgrade():
    for statement in SCHEMA:
        op.execute(statement)


def downgrade():
    # Retain immutable material and intent evidence; old binaries ignore these tables.
    pass
