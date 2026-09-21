"""telegram review cards: message <-> post mapping and poller state

The operator reviews a queued post in Telegram before it goes out: the bot
sends one card per campaign post (media + the exact copy + where it will go)
and the operator replies to that card to change the copy, pause it or move it.

Two tables back that:

* ``tg_review_messages`` maps a Telegram ``message_id`` back to the
  ``campaign_posts`` row (and its job) the card described. A reply carries only
  the message it replied to, so this mapping is what makes "reply to the card"
  address a post. ``status`` records the last action taken from that message.
* ``tg_review_state`` is a tiny key/value store for the ``getUpdates`` offset,
  so a restart does not re-read (and re-apply) old replies.

Both are additive and nullable-friendly: with no Telegram configured the
feature no-ops and the tables simply stay empty.

Revision ID: 0020_telegram_review_cards
Revises: 0019_account_nickname_and_group
Create Date: 2026-09-21
"""
from __future__ import annotations

from alembic import op


revision = "0020_telegram_review_cards"
down_revision = "0019_account_nickname_and_group"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("PRAGMA foreign_keys = ON")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tg_review_messages (
            message_id INTEGER PRIMARY KEY,
            chat_id TEXT NOT NULL DEFAULT '',
            campaign_id INTEGER,
            post_id INTEGER,
            job_id INTEGER,
            profile_id INTEGER,
            media_path TEXT NOT NULL DEFAULT '',
            copy_text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tg_review_posts "
        "ON tg_review_messages(post_id)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tg_review_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_tg_review_posts")
    op.execute("DROP TABLE IF EXISTS tg_review_messages")
    op.execute("DROP TABLE IF EXISTS tg_review_state")
