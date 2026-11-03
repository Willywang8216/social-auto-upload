"""The container's data bind mount must never supply migration code."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

from db import createTable
from myUtils import db_bootstrap

ROOT = Path(__file__).resolve().parents[1]


def test_source_bootstrap_facade_preserves_canonical_api():
    assert db_bootstrap is createTable
    assert db_bootstrap.bootstrap is createTable.bootstrap
    assert db_bootstrap.DB_PATH == ROOT / "db" / "database.db"
    assert callable(db_bootstrap.main)


def test_image_bootstrap_ignores_shadowing_data_directory(tmp_path):
    image = tmp_path / "image"
    image.mkdir()
    shutil.copytree(ROOT / "myUtils", image / "myUtils", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "migrations", image / "migrations", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(ROOT / "alembic.ini", image / "alembic.ini")
    # The same COPY operation as Dockerfile. No second maintained implementation.
    shutil.copyfile(ROOT / "db" / "createTable.py", image / "myUtils" / "db_bootstrap.py")
    data = image / "db"
    data.mkdir()
    (data / "__init__.py").write_text("raise AssertionError('data-mounted package was imported')\n")
    (data / "createTable.py").write_text("raise AssertionError('stale host bootstrap was executed')\n")
    database = data / "database.db"
    env = {**os.environ, "SAU_TESTING": "1", "SAU_DB_PATH": str(database),
           "PYTHONPATH": os.pathsep.join((str(image), str(ROOT)))}
    result = subprocess.run([sys.executable, "-X", "utf8", "-m", "myUtils.db_bootstrap"],
                            cwd=image, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(database)
    try:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0025_file_record_path_indexes"
        assert "lease_token" in {row[1] for row in conn.execute("PRAGMA table_info(publish_job_targets)")}
    finally:
        conn.close()
