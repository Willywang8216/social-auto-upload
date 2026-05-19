"""publish templates + campaign post file subsets

Adds the `publish_templates` table for saving Publish Center settings as
reusable templates, and a nullable `file_record_ids_json` column on
`campaign_posts` so a post can be pinned to a subset of the campaign's
media files. The subset enables the Publish Center to split a single
media group into multiple posts (e.g. for single-media platforms that
require staggered N-posts-5-minutes-apart fan-out).

Revision ID: 0010_publish_templates_and_post_files
Revises: 0009_twitter_oauth_persistence
Create Date: 2026-05-19
"""
from __future__ import annotations

from alembic import op


revision = "0010_publish_templates_and_post_files"
down_revision = "0009_twitter_oauth_persistence"
branch_labels = None
depends_on = None


CREATE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS publish_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        slug TEXT NOT NULL UNIQUE,
        description TEXT DEFAULT '',
        config_json TEXT NOT NULL DEFAULT '{}',
        included_settings_json TEXT NOT NULL DEFAULT '[]',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
)

CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_publish_templates_slug ON publish_templates(slug)",
)

DROP_STATEMENTS = (
    "DROP TABLE IF EXISTS publish_templates",
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
    for statement in CREATE_STATEMENTS:
        op.execute(statement)
    for statement in CREATE_INDEXES:
        op.execute(statement)
    _maybe_add_column(
        "campaign_posts",
        "file_record_ids_json",
        "file_record_ids_json TEXT",
    )


def downgrade() -> None:
    for statement in DROP_STATEMENTS:
        op.execute(statement)
    # SQLite cannot easily drop a column; the nullable column is left in
    # place on downgrade. Behaviour is unchanged for callers that ignore it.
