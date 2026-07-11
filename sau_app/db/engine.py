"""Engine and session factory for the SQLAlchemy repository layer (Phase 2).

Resolves a database URL (``DATABASE_URL`` env wins; otherwise the legacy SQLite
file under ``BASE_DIR/db``) and builds a cached engine + ``sessionmaker``. Works
for both ``sqlite://`` and ``postgresql+psycopg://`` URLs so the same repository
code runs on today's SQLite and the production PostgreSQL target.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from utils.conf_defaults import BASE_DIR

_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None
_engine_url: str | None = None


def resolve_database_url() -> str:
    """Return the configured database URL.

    ``DATABASE_URL`` takes precedence (e.g. a PostgreSQL URL in production);
    otherwise fall back to the legacy SQLite file so nothing changes locally.
    """

    configured = (os.environ.get("DATABASE_URL") or "").strip()
    if configured:
        return configured
    legacy = Path(BASE_DIR) / "db" / "database.db"
    return f"sqlite:///{legacy.resolve()}"


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def make_engine(url: str | None = None, **kwargs) -> Engine:
    """Create a new engine for ``url`` (not cached).

    Applies SQLite-friendly defaults (thread sharing + ``PRAGMA foreign_keys``)
    so behavior matches the legacy ``sqlite3`` connections, which set
    ``PRAGMA foreign_keys = ON`` explicitly.
    """

    url = url or resolve_database_url()
    if _is_sqlite(url):
        kwargs.setdefault("connect_args", {"check_same_thread": False})
    engine = create_engine(url, future=True, **kwargs)

    if _is_sqlite(url):

        @event.listens_for(engine, "connect")
        def _enable_sqlite_fk(dbapi_connection, _record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.close()

    return engine


def get_engine() -> Engine:
    """Return the process-wide cached engine, rebuilding if the URL changed."""

    global _engine, _Session, _engine_url
    url = resolve_database_url()
    if _engine is None or _engine_url != url:
        if _engine is not None:
            _engine.dispose()
        _engine = make_engine(url)
        _Session = sessionmaker(bind=_engine, future=True, expire_on_commit=False)
        _engine_url = url
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    get_engine()
    assert _Session is not None
    return _Session


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session scope: commit on success, roll back on error."""

    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine_cache() -> None:
    """Drop the cached engine/sessionmaker (used by tests that switch URLs)."""

    global _engine, _Session, _engine_url
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None
    _engine_url = None
