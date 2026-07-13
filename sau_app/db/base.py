"""SQLAlchemy 2.x declarative base for the repository layer (Phase 2).

These models are dialect-agnostic (generic column types) so the same schema
materializes on both SQLite (today) and PostgreSQL (the production target).
They map the *existing* tables column-for-column — the repository layer reads
and writes through them in parallel with the legacy raw-``sqlite3`` code while
domains are migrated one at a time. No behavior of the legacy code changes.

Alembic still owns migrations via the hand-written revisions under
``migrations/versions/``; this metadata is **not** wired into Alembic
autogenerate yet (that begins with the new identity/workspace tables in a later
phase), so it cannot drift the existing schema.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# A stable naming convention so future Alembic autogenerate emits deterministic,
# cross-dialect constraint names.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
