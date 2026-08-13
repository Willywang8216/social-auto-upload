"""telegram multi-chat-ids documentation

``accounts.config_json`` continues to be a free-form JSON blob; this revision
records the new ``chatIds`` / ``chatTitles`` keys alongside the legacy
``chatId`` / ``telegramChatTitle`` keys. Operators reading the migration log
will see that accounts may now carry multiple Telegram targets per bot.

No schema change. Pure documentation marker so ``alembic upgrade head`` keeps
advancing after the application code in :mod:`myUtils.prepared_publishers`
learns about :func:`_telegram_resolve_chat_ids`.

Revision ID: 0018_telegram_multi_chat_ids
Revises: 0017_workspace_id_expand
Create Date: 2026-08-08
"""
from __future__ import annotations

from alembic import op


revision = "0018_telegram_multi_chat_ids"
down_revision = "0017_workspace_id_expand"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No-op. Documented in the migration message + module docstring. ``accounts``
    # already stores ``config_json`` as TEXT, so the new ``chatIds`` (list) and
    # ``chatTitles`` (list) keys are added by the application layer; nothing
    # changes at the SQL layer.
    pass


def downgrade() -> None:
    # No-op. Reverse compatibility is handled at the read sites
    # (``_telegram_resolve_chat_ids`` falls back to ``chatId``); there is
    # nothing to undo at the schema layer.
    pass
