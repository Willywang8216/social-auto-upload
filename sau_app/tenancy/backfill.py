"""Legacy-workspace backfill (Phase 5, "backfill" step).

Turns the existing single-tenant installation into *tenant zero*: it ensures a
legacy user + legacy workspace exist, then assigns every existing tenant-owned
row to that workspace. Because there is exactly one pre-existing tenant, every
row belongs to the legacy workspace — so the backfill is a simple, idempotent
``UPDATE ... WHERE workspace_id IS NULL`` per table, with an orphan report that
proves no row was left unassigned.

Run it (after a database backup) with::

    SAU_LEGACY_OWNER_EMAIL=you@example.com \
    SAU_LEGACY_WORKSPACE_NAME="Your Workspace" \
    DATABASE_URL=sqlite:///db/database.db \
    python -m sau_app.tenancy.backfill            # add --dry-run to preview

The legacy workspace has no Google identity yet; the owner claims it on first
Google login (matched by the configured email + a one-time claim secret).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .tables import TENANT_TABLES

LEGACY_WORKSPACE_SLUG = "legacy"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(value: str) -> str:
    return _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-") or "workspace"


@dataclass
class BackfillReport:
    legacy_user_id: str
    legacy_workspace_id: str
    created_workspace: bool
    assigned: dict[str, int] = field(default_factory=dict)
    orphans: dict[str, int] = field(default_factory=dict)
    pre_counts: dict[str, int] = field(default_factory=dict)

    @property
    def has_orphans(self) -> bool:
        return any(v > 0 for v in self.orphans.values())

    def to_dict(self) -> dict:
        return {
            "legacy_user_id": self.legacy_user_id,
            "legacy_workspace_id": self.legacy_workspace_id,
            "created_workspace": self.created_workspace,
            "assigned": self.assigned,
            "orphans": self.orphans,
            "pre_counts": self.pre_counts,
            "has_orphans": self.has_orphans,
        }


def _table_exists(conn, table: str) -> bool:
    return conn.dialect.has_table(conn, table)


def _has_workspace_column(conn, table: str) -> bool:
    from sqlalchemy import inspect

    try:
        return any(c["name"] == "workspace_id" for c in inspect(conn).get_columns(table))
    except Exception:
        return False


def ensure_legacy_workspace(conn, *, owner_email: str, workspace_name: str) -> tuple[str, str, bool]:
    """Ensure the legacy user + workspace + owner membership exist (idempotent).

    Returns ``(user_id, workspace_id, created)``.
    """
    existing = conn.execute(
        text("SELECT id, created_by_user_id FROM workspaces WHERE slug = :slug"),
        {"slug": LEGACY_WORKSPACE_SLUG},
    ).fetchone()
    if existing is not None:
        return existing[1], existing[0], False

    # Reuse a user with this primary email if one already exists, else create.
    user_row = conn.execute(
        text("SELECT id FROM users WHERE primary_email = :email"), {"email": owner_email}
    ).fetchone()
    if user_row is not None:
        user_id = user_row[0]
    else:
        user_id = uuid.uuid4().hex
        conn.execute(
            text(
                "INSERT INTO users (id, primary_email, display_name, status) "
                "VALUES (:id, :email, :name, 'active')"
            ),
            {"id": user_id, "email": owner_email, "name": "Legacy Owner"},
        )

    workspace_id = uuid.uuid4().hex
    conn.execute(
        text(
            "INSERT INTO workspaces (id, name, slug, status, created_by_user_id) "
            "VALUES (:id, :name, :slug, 'active', :uid)"
        ),
        {"id": workspace_id, "name": workspace_name, "slug": LEGACY_WORKSPACE_SLUG, "uid": user_id},
    )
    conn.execute(
        text(
            "INSERT INTO workspace_members (workspace_id, user_id, role) "
            "VALUES (:ws, :uid, 'owner')"
        ),
        {"ws": workspace_id, "uid": user_id},
    )
    return user_id, workspace_id, True


def run_backfill(engine: Engine, *, owner_email: str, workspace_name: str, dry_run: bool = False) -> BackfillReport:
    with engine.begin() as conn:
        user_id, workspace_id, created = ensure_legacy_workspace(
            conn, owner_email=owner_email, workspace_name=workspace_name
        )
        report = BackfillReport(
            legacy_user_id=user_id, legacy_workspace_id=workspace_id, created_workspace=created
        )

        for table in TENANT_TABLES:
            if not _table_exists(conn, table) or not _has_workspace_column(conn, table):
                continue
            report.pre_counts[table] = conn.execute(
                text(f"SELECT COUNT(*) FROM {table}")
            ).scalar_one()
            null_before = conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE workspace_id IS NULL")
            ).scalar_one()
            if not dry_run and null_before:
                conn.execute(
                    text(f"UPDATE {table} SET workspace_id = :ws WHERE workspace_id IS NULL"),
                    {"ws": workspace_id},
                )
            report.assigned[table] = null_before

        # Orphan report: any tenant row still unassigned after the backfill.
        for table in TENANT_TABLES:
            if not _table_exists(conn, table) or not _has_workspace_column(conn, table):
                continue
            report.orphans[table] = conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE workspace_id IS NULL")
            ).scalar_one()

        if dry_run:
            conn.rollback()

    return report


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill existing rows into the legacy workspace.")
    parser.add_argument("--dry-run", action="store_true", help="preview without writing")
    parser.add_argument("--owner-email", default=os.environ.get("SAU_LEGACY_OWNER_EMAIL"))
    parser.add_argument(
        "--workspace-name",
        default=os.environ.get("SAU_LEGACY_WORKSPACE_NAME", "Legacy Workspace"),
    )
    args = parser.parse_args(argv)

    if not args.owner_email:
        parser.error("SAU_LEGACY_OWNER_EMAIL (or --owner-email) is required")

    from ..db import get_engine

    report = run_backfill(
        get_engine(),
        owner_email=args.owner_email,
        workspace_name=args.workspace_name,
        dry_run=args.dry_run,
    )
    print(json.dumps(report.to_dict(), indent=2))
    if report.has_orphans:
        print("ERROR: orphaned rows remain (workspace_id still NULL) — see report above.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
