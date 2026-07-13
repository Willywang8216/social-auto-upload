"""SQLAlchemy repository layer for social-auto-upload (Phase 2).

Introduced in parallel with the legacy raw-``sqlite3`` data access: models and
repositories are added domain by domain, and the ORM is dialect-agnostic so the
same code runs on SQLite and PostgreSQL. Nothing here changes the behavior of
the existing backend yet.
"""

from __future__ import annotations

from .base import Base
# Import the model modules so every table is registered on ``Base.metadata``
# (needed for metadata.create_all and future Alembic autogenerate).
from . import identity_models, models  # noqa: F401
from .engine import (
    get_engine,
    get_sessionmaker,
    make_engine,
    reset_engine_cache,
    resolve_database_url,
    session_scope,
)

__all__ = [
    "Base",
    "get_engine",
    "get_sessionmaker",
    "make_engine",
    "reset_engine_cache",
    "resolve_database_url",
    "session_scope",
]
