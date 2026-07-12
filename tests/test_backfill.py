"""Phase 5: legacy-workspace backfill against synthetic data.

Verifies tenant-zero backfill semantics on a bootstrapped SQLite database:
every existing tenant row is assigned to the single legacy workspace, the
backfill is idempotent, dry-run writes nothing, and the orphan report catches
unassigned rows. (In CI this also runs against PostgreSQL.)
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

sqlalchemy_available = importlib.util.find_spec("sqlalchemy") is not None

if sqlalchemy_available:
    import db.createTable as create_table
    from sau_app.db import make_engine
    from sau_app.tenancy.backfill import run_backfill
    from sau_app.tenancy.tables import TENANT_TABLES


def _load_migration_tenant_tables() -> tuple[str, ...]:
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0017_workspace_id_expand.py"
    spec = importlib.util.spec_from_file_location("mig0017", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module.TENANT_TABLES


@unittest.skipUnless(sqlalchemy_available, "SQLAlchemy not installed")
class TenantTableListTests(unittest.TestCase):
    def test_matches_migration_list(self) -> None:
        self.assertEqual(tuple(TENANT_TABLES), tuple(_load_migration_tenant_tables()))


@unittest.skipUnless(sqlalchemy_available, "SQLAlchemy not installed")
class BackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "b.db"
        create_table.bootstrap(self.db_path)
        self._seed()
        self.engine = make_engine(f"sqlite:///{self.db_path}")

    def tearDown(self) -> None:
        self.engine.dispose()
        self._tmp.cleanup()

    def _seed(self) -> None:
        from myUtils import profiles as prof

        p = prof.create_profile(name="Brand", db_path=self.db_path)
        prof.add_account(profile_id=p.id, platform="douyin", account_name="a1", db_path=self.db_path)
        # A couple of direct inserts across other tenant tables.
        with sqlite3.connect(self.db_path) as c:
            c.execute("INSERT INTO file_records (filename, file_path) VALUES ('v.mp4','/tmp/v.mp4')")
            c.execute(
                "INSERT INTO publish_jobs (idempotency_key, platform, payload_json) "
                "VALUES ('k1','douyin','{}')"
            )
            c.commit()

    def _count_null(self, table: str) -> int:
        with sqlite3.connect(self.db_path) as c:
            return c.execute(f"SELECT COUNT(*) FROM {table} WHERE workspace_id IS NULL").fetchone()[0]

    def _count_all(self, table: str) -> int:
        with sqlite3.connect(self.db_path) as c:
            return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_backfill_assigns_all_rows_to_legacy_workspace(self) -> None:
        report = run_backfill(self.engine, owner_email="me@example.com", workspace_name="Mine")
        self.assertTrue(report.created_workspace)
        self.assertFalse(report.has_orphans)
        # Seeded tables now have zero NULL workspace_id.
        for table in ("profiles", "accounts", "file_records", "publish_jobs"):
            self.assertEqual(self._count_null(table), 0, table)
        # Every profile/account row carries the legacy workspace id.
        with sqlite3.connect(self.db_path) as c:
            ws = c.execute("SELECT DISTINCT workspace_id FROM profiles").fetchall()
        self.assertEqual([r[0] for r in ws], [report.legacy_workspace_id])
        # The legacy workspace + owner membership exist exactly once.
        with sqlite3.connect(self.db_path) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM workspaces WHERE slug='legacy'").fetchone()[0], 1)
            self.assertEqual(
                c.execute(
                    "SELECT role FROM workspace_members WHERE workspace_id=?",
                    (report.legacy_workspace_id,),
                ).fetchone()[0],
                "owner",
            )

    def test_backfill_is_idempotent(self) -> None:
        first = run_backfill(self.engine, owner_email="me@example.com", workspace_name="Mine")
        second = run_backfill(self.engine, owner_email="me@example.com", workspace_name="Mine")
        self.assertTrue(first.created_workspace)
        self.assertFalse(second.created_workspace)  # workspace reused
        self.assertEqual(first.legacy_workspace_id, second.legacy_workspace_id)
        # Second run assigns nothing (already assigned) and no duplicate workspace.
        self.assertEqual(sum(second.assigned.values()), 0)
        with sqlite3.connect(self.db_path) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM workspaces WHERE slug='legacy'").fetchone()[0], 1)

    def test_dry_run_writes_nothing(self) -> None:
        report = run_backfill(
            self.engine, owner_email="me@example.com", workspace_name="Mine", dry_run=True
        )
        # Rolled back: no legacy workspace persisted, rows still NULL, orphans reported.
        with sqlite3.connect(self.db_path) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM workspaces WHERE slug='legacy'").fetchone()[0], 0)
        self.assertGreater(self._count_null("profiles"), 0)
        self.assertTrue(report.has_orphans)

    def test_backfill_only_touches_null_rows(self) -> None:
        # Pre-assign one profile to a different workspace; backfill must not move it.
        with sqlite3.connect(self.db_path) as c:
            c.execute("UPDATE profiles SET workspace_id='other-ws' WHERE 1=1")
            c.commit()
        report = run_backfill(self.engine, owner_email="me@example.com", workspace_name="Mine")
        with sqlite3.connect(self.db_path) as c:
            ws = {r[0] for r in c.execute("SELECT DISTINCT workspace_id FROM profiles")}
        self.assertEqual(ws, {"other-ws"})  # untouched
        self.assertEqual(report.assigned.get("profiles", -1), 0)


if __name__ == "__main__":
    unittest.main()
