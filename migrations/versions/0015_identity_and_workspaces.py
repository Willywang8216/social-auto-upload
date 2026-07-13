"""multi-tenant identity and workspace tables

Adds the authentication/tenancy foundation for the multi-user conversion:
users, auth_identities (keyed by provider + provider_subject — the Google
``sub``, never email), workspaces, workspace_members, and server-side sessions.

This is the first migration authored with portable ``op.create_table`` (rather
than the SQLite-raw ``op.execute`` used by 0001-0014), so it runs on both
SQLite and PostgreSQL. It is also the first revision *past* the raw
``CREATE TABLE`` block in ``db/createTable.py`` — ``bootstrap()`` stamps the raw
revision (0014) and then applies this one via ``alembic upgrade head`` (see the
D-8 fix), so a freshly bootstrapped database gains these tables.

Revision ID: 0015_identity_and_workspaces
Revises: 0014_socialupload_full_schema
Create Date: 2026-07-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_identity_and_workspaces"
down_revision = "0014_socialupload_full_schema"
branch_labels = None
depends_on = None

_UUID = sa.String(36)
_TS = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("PRAGMA foreign_keys = ON")

    op.create_table(
        "users",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("primary_email", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_TS),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=_TS),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "auth_identities",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "user_id",
            _UUID,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_subject", sa.Text(), nullable=False),
        sa.Column("email_at_provider", sa.Text(), nullable=True),
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("claims_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_TS),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=_TS),
        sa.UniqueConstraint("provider", "provider_subject", name="uq_auth_identities_provider_subject"),
    )
    op.create_index("ix_auth_identities_user_id", "auth_identities", ["user_id"])

    op.create_table(
        "workspaces",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column(
            "created_by_user_id",
            _UUID,
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_TS),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=_TS),
    )

    op.create_table(
        "workspace_members",
        sa.Column(
            "workspace_id",
            _UUID,
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            _UUID,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_TS),
    )
    op.create_index("ix_workspace_members_user_id", "workspace_members", ["user_id"])

    op.create_table(
        "sessions",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "user_id",
            _UUID,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "active_workspace_id",
            _UUID,
            sa.ForeignKey("workspaces.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_TS),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("csrf_secret", sa.Text(), nullable=False),
        sa.Column("user_agent_hash", sa.Text(), nullable=True),
        sa.Column("ip_prefix", sa.Text(), nullable=True),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_workspace_members_user_id", table_name="workspace_members")
    op.drop_table("workspace_members")
    op.drop_table("workspaces")
    op.drop_index("ix_auth_identities_user_id", table_name="auth_identities")
    op.drop_table("auth_identities")
    op.drop_table("users")
