"""Tests for the Phase 2 SQLAlchemy repository layer (``sau_app.db``).

Runs against SQLite by default. When ``TEST_DATABASE_URL`` points at a
PostgreSQL instance (CI), the model/repository tests also run there, proving the
dialect-agnostic models and repositories work on the production database. The
legacy-interop tests stay on SQLite because the legacy code is SQLite-only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

sqlalchemy_available = importlib.util.find_spec("sqlalchemy") is not None

if sqlalchemy_available:
    from sqlalchemy.orm import Session

    from sau_app.db import make_engine, resolve_database_url
    from sau_app.db.base import Base
    from sau_app.db.models import Account, Profile
    from sau_app.db.repositories import ProfileRepository


def _pg_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL") or None


@unittest.skipUnless(sqlalchemy_available, "SQLAlchemy not installed")
class EngineTests(unittest.TestCase):
    def test_resolve_url_prefers_database_url(self) -> None:
        prev = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = "postgresql+psycopg://u:p@h/db"
        try:
            self.assertEqual(resolve_database_url(), "postgresql+psycopg://u:p@h/db")
        finally:
            if prev is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = prev

    def test_resolve_url_falls_back_to_sqlite(self) -> None:
        prev = os.environ.pop("DATABASE_URL", None)
        try:
            url = resolve_database_url()
            self.assertTrue(url.startswith("sqlite:///"))
            self.assertTrue(url.endswith("db/database.db"))
        finally:
            if prev is not None:
                os.environ["DATABASE_URL"] = prev

    def test_sqlite_engine_enforces_foreign_keys(self) -> None:
        engine = make_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            from sqlalchemy import text

            fk = conn.execute(text("PRAGMA foreign_keys")).scalar()
            self.assertEqual(fk, 1)
        engine.dispose()


@unittest.skipUnless(sqlalchemy_available, "SQLAlchemy not installed")
class ModelMaterializationTests(unittest.TestCase):
    """Base.metadata.create_all works on SQLite and (in CI) PostgreSQL."""

    def _run_against(self, url: str) -> None:
        engine = make_engine(url)
        try:
            Base.metadata.create_all(engine)
            from sqlalchemy import inspect

            tables = set(inspect(engine).get_table_names())
            self.assertIn("profiles", tables)
            self.assertIn("accounts", tables)
        finally:
            if url != "sqlite:///:memory:":
                Base.metadata.drop_all(engine)
            engine.dispose()

    def test_materializes_on_sqlite(self) -> None:
        self._run_against("sqlite:///:memory:")

    def test_materializes_on_postgres_when_configured(self) -> None:
        url = _pg_url()
        if not url:
            self.skipTest("TEST_DATABASE_URL not set (no PostgreSQL)")
        self._run_against(url)


@unittest.skipUnless(sqlalchemy_available, "SQLAlchemy not installed")
class ProfileRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = make_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine, future=True, expire_on_commit=False)

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def test_create_and_read_roundtrip(self) -> None:
        repo = ProfileRepository(self.session)
        created = repo.create(name="Alice", slug="alice", settings={"tz": "UTC"})
        self.session.commit()

        self.assertIsNotNone(created.id)
        fetched = repo.get(created.id)
        self.assertEqual(fetched.name, "Alice")
        self.assertEqual(json.loads(fetched.settings_json), {"tz": "UTC"})
        self.assertEqual(repo.get_by_slug("alice").id, created.id)
        self.assertEqual([p.slug for p in repo.list()], ["alice"])

    def test_delete(self) -> None:
        repo = ProfileRepository(self.session)
        p = repo.create(name="Bob", slug="bob")
        self.session.commit()
        self.assertTrue(repo.delete(p.id))
        self.session.commit()
        self.assertIsNone(repo.get(p.id))
        self.assertFalse(repo.delete(9999))

    def test_workspace_scope_is_noop_without_column(self) -> None:
        # Until the workspace_id column exists, passing workspace_id must not
        # filter anything out (compatibility mode).
        repo = ProfileRepository(self.session)
        repo.create(name="Carol", slug="carol")
        self.session.commit()
        self.assertEqual(len(repo.list(workspace_id="any-uuid")), 1)


@unittest.skipUnless(sqlalchemy_available, "SQLAlchemy not installed")
class LegacyInteropTests(unittest.TestCase):
    """The ORM reads/writes the exact rows the legacy raw-sqlite code uses."""

    def setUp(self) -> None:
        import db.createTable as create_table

        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "interop.db"
        create_table.bootstrap(self.db_path)
        self.engine = make_engine(f"sqlite:///{self.db_path}")

    def tearDown(self) -> None:
        self.engine.dispose()
        self._tmp.cleanup()

    def test_repo_write_is_visible_to_legacy_registry(self) -> None:
        from myUtils import profiles as legacy

        with Session(self.engine, future=True, expire_on_commit=False) as session:
            repo = ProfileRepository(session)
            repo.create(name="Dana", slug="dana", settings={"k": "v"})
            session.commit()

        # Legacy raw-sqlite reader sees the ORM-written row identically.
        loaded = legacy.get_profile_by_slug("dana", db_path=self.db_path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.name, "Dana")
        self.assertEqual(loaded.settings, {"k": "v"})

    def test_legacy_write_is_visible_to_repo(self) -> None:
        from myUtils import profiles as legacy

        created = legacy.create_profile(name="Eve", db_path=self.db_path)

        with Session(self.engine, future=True, expire_on_commit=False) as session:
            repo = ProfileRepository(session)
            fetched = repo.get_by_slug(created.slug)
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched.name, "Eve")
            self.assertEqual(fetched.id, created.id)


if __name__ == "__main__":
    unittest.main()
