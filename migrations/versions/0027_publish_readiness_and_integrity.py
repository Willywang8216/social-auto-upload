"""Add verified file facts and independent prerequisite/readiness projections.

Revision ID: 0027_publish_readiness_and_integrity
Revises: 0026_publish_alert_outbox

No legacy digest/size backfill and no queue/receipt changes. Downgrade removes
only the new projections; verified file facts remain for safe application
rollback (no SQLite table rebuild or destruction of executed verification).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0027_publish_readiness_and_integrity"
down_revision = "0026_publish_alert_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("file_records")}
    for name, datatype in (("sha256", "TEXT"), ("size_bytes", "INTEGER")):
        if name not in columns:
            op.execute(f"ALTER TABLE file_records ADD COLUMN {name} {datatype}")
    op.execute("""
        CREATE TABLE IF NOT EXISTS publish_dependency_states (
            scope_key TEXT PRIMARY KEY,
            config_fingerprint TEXT NOT NULL,
            kind TEXT,
            status TEXT,
            reason TEXT,
            blocked_until TEXT,
            last_checked_at TEXT,
            updated_at TEXT
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS publish_destination_readiness (
            account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
            workspace_id TEXT,
            provider TEXT,
            config_fingerprint TEXT,
            status TEXT,
            reason TEXT,
            evidence_json TEXT,
            checked_at TEXT,
            expires_at TEXT
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS publish_destination_readiness")
    op.execute("DROP TABLE IF EXISTS publish_dependency_states")
    # Deliberately keep nullable canonical facts. Older binaries ignore these
    # columns; removing them would discard evidence and may rebuild SQLite rows.
