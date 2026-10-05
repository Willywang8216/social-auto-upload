"""Tests for the Alembic migration integration."""

from __future__ import annotations

import os
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

    def _alembic_config(self) -> "Config":
        """Build an Alembic config that works from any cwd.

        ``alembic.ini`` carries a relative ``script_location = migrations``,
        which Alembic resolves against the process cwd. Pinning it to the
        repo-absolute path keeps these tests honest regardless of where they
        are invoked from - the same fix ``db.createTable`` applies in
        production.
        """
        cfg = Config(str(create_table.ALEMBIC_INI_PATH))
        migrations_dir = create_table._PROJECT_ROOT / "migrations"
        if migrations_dir.is_dir():
            cfg.set_main_option("script_location", str(migrations_dir))
        return cfg

    def _current_head(self) -> str:
        cfg = self._alembic_config()
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
            "meta_oauth_requests",
            "threads_oauth_requests",
            "tiktok_review_events",
            "account_events",
            "video_analytics_videos",
            "video_analytics_snapshots",
            "analytics_sync_log",
            "storage_backends",
            "tiktok_publish_status",
            "watermark_configs",
            "media_assets",
            "sheet_exports",
            "prepared_posts",
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

    def test_raw_schema_revision_is_valid_ancestor_of_head(self) -> None:
        # bootstrap() stamps RAW_SCHEMA_REVISION and then upgrades to head, so
        # the raw revision must exist and be head or an ancestor of it —
        # otherwise the follow-up ``alembic upgrade head`` would be invalid.
        cfg = self._alembic_config()
        script = ScriptDirectory.from_config(cfg)
        raw = create_table.RAW_SCHEMA_REVISION
        head = script.get_current_head()
        # The revision resolves (raises if unknown).
        self.assertIsNotNone(script.get_revision(raw))
        # raw is head, or head is reachable by walking down from head to raw.
        ancestors = {rev.revision for rev in script.walk_revisions(base="base", head=head)}
        self.assertIn(raw, ancestors)

    def test_bootstrap_then_upgrade_matches_head(self) -> None:
        # After bootstrap(), the DB is stamped at head (today RAW == head; once
        # newer migrations exist, bootstrap applies them up to head).
        create_table.bootstrap(self.db_path)
        head = self._current_head()
        with sqlite3.connect(self.db_path) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        self.assertEqual(version, head)

    def test_bootstrap_applies_migrations_from_any_working_directory(self) -> None:
        """bootstrap() must not depend on the process cwd.

        ``alembic.ini`` declares ``script_location = migrations``, a relative
        path Alembic resolves against the *current working directory*. Any
        caller whose cwd was not the repo root raised inside the (swallowed)
        config load in ``_stamp_alembic_head`` and stamped the raw revision
        without running the later migrations — leaving the schema without
        ``profiles.workspace_id`` / ``accounts.workspace_id`` and failing later
        at runtime with "no such column: workspace_id". CI hit exactly this:
        ``uv run pytest`` from a different cwd produced a schema the tests
        then rejected.
        """
        original = Path.cwd()
        try:
            os.chdir(self._tmp.name)
            create_table.bootstrap(self.db_path)
        finally:
            os.chdir(original)

        with sqlite3.connect(self.db_path) as conn:
            account_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(accounts)")
            }
            profile_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(profiles)")
            }
            version = conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
        self.assertIn("workspace_id", account_cols)
        self.assertIn("workspace_id", profile_cols)
        self.assertEqual(version, self._current_head())


if __name__ == "__main__":
    unittest.main()
