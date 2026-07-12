"""google login (OIDC) transaction store

Server-side storage for in-flight Google OpenID Connect logins: the CSRF
``state``, the OIDC ``nonce``, and the PKCE ``code_verifier``, with a short TTL.
Keeping this server-side (rather than in a client cookie) means state is
one-time-use and cannot be replayed, and it works across multiple processes
(unlike an in-memory dict). Only a hash of ``state`` is stored.

Portable ``op.create_table`` (SQLite + PostgreSQL).

Revision ID: 0016_oauth_login_transactions
Revises: 0015_identity_and_workspaces
Create Date: 2026-07-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_oauth_login_transactions"
down_revision = "0015_identity_and_workspaces"
branch_labels = None
depends_on = None

_TS = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    op.create_table(
        "oauth_login_transactions",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("code_verifier", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("next_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_TS),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_oauth_login_transactions_expires_at", "oauth_login_transactions", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_oauth_login_transactions_expires_at", table_name="oauth_login_transactions")
    op.drop_table("oauth_login_transactions")
