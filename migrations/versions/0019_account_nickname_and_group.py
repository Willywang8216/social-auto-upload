"""accounts: add per-account nickname and account_group columns

Operators want to label accounts (e.g. "Acme Main", "Backup Brand") and bucket
them into groups (e.g. "Daily drivers", "Demos") so they can filter the
Accounts tab by group / platform / readiness. Today the only identity-bearing
field is the unique ``account_name``; this revision adds two free-form TEXT
columns that are nullable and default to the empty string so every existing row
continues to work without a backfill.

* ``nickname``: operator-chosen display name. Distinct from ``account_name``,
  which must stay unique per (profile_id, platform) and is used as the cookie
  file basename.
* ``account_group``: a free-form tag identifying the group an account belongs
  to. The new ``idx_accounts_group`` index keeps the group-filter queries
  cheap as the table grows.

Revision ID: 0019_account_nickname_and_group
Revises: 0018_telegram_multi_chat_ids
Create Date: 2026-08-21
"""
from __future__ import annotations

from alembic import op


revision = "0019_account_nickname_and_group"
down_revision = "0018_telegram_multi_chat_ids"
branch_labels = None
depends_on = None


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
        "accounts",
        "nickname",
        "nickname TEXT NOT NULL DEFAULT ''",
    )
    _maybe_add_column(
        "accounts",
        "account_group",
        "account_group TEXT NOT NULL DEFAULT ''",
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_accounts_group ON accounts(account_group)"
    )


def downgrade() -> None:
    # SQLite has no DROP COLUMN before 3.35; instead we leave the columns in
    # place and drop the index so a future cleanup can remove them. This keeps
    # the down-migration portable and safe.
    op.execute("DROP INDEX IF EXISTS idx_accounts_group")
