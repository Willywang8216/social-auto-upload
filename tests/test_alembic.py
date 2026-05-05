"""Tests for the Alembic migration integration."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import db.createTable as create_table

try:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
except ModuleNotFoundError:  # pragma: no cover - environment-specific
    Config = None
    ScriptDirectory = None


@unittest.skipUnless(Config is not None, "alembic is not installed")
class AlembicBaselineTests(unittest.TestCase):
    """The baseline migration must agree with db/createTable.bootstrap()."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _table_set(self, db_path: Path) -> set[str]:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        # ``sqlite_sequence`` shows up after AUTOINCREMENT inserts, so
        # filter it out for a stable comparison.
        return {name for (name,) in rows if name != "sqlite_sequence"}

    def _current_head(self) -> str:
        cfg = Config(str(create_table.ALEMBIC_INI_PATH))
        return ScriptDirectory.from_config(cfg).get_current_head()

    def test_alembic_upgrade_creates_expected_tables(self) -> None:
        create_table._alembic_upgrade_head(self.db_path)
        tables = self._table_set(self.db_path)
        # alembic_version is created by Alembic itself.
        self.assertIn("alembic_version", tables)
        # Plus every business table the bootstrap defines.
        for expected in (
            "user_info",
            "file_records",
            "profiles",
            "accounts",
            "publish_jobs",
            "publish_job_targets",
            "media_groups",
            "media_group_items",
            "campaigns",
            "campaign_artifacts",
            "campaign_posts",
            "tiktok_oauth_requests",
            "reddit_oauth_requests",
            "youtube_oauth_requests",
            "tiktok_review_events",
            "account_events",
        ):
            self.assertIn(expected, tables)

    def test_bootstrap_and_upgrade_produce_the_same_tables(self) -> None:
        create_table.bootstrap(self.db_path)
        bootstrap_tables = self._table_set(self.db_path) - {"alembic_version"}

        upgrade_db = Path(self._tmp.name) / "upgrade.db"
        create_table._alembic_upgrade_head(upgrade_db)
        upgrade_tables = self._table_set(upgrade_db) - {"alembic_version"}

        self.assertEqual(bootstrap_tables, upgrade_tables)

    def test_bootstrap_stamps_alembic_head_version(self) -> None:
        create_table.bootstrap(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], self._current_head())

    def test_alembic_upgrade_is_idempotent(self) -> None:
        # Running upgrade head twice must succeed and not duplicate rows.
        create_table._alembic_upgrade_head(self.db_path)
        create_table._alembic_upgrade_head(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM alembic_version"
            ).fetchone()
        self.assertEqual(rows[0], 1)


if __name__ == "__main__":
    unittest.main()
