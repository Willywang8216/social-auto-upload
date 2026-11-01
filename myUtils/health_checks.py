"""Read-only readiness probes shared by the web and release tooling."""
from pathlib import Path
import sqlite3


def database_ready(db_path: Path) -> tuple[str, bool]:
    path = Path(db_path)
    if not path.is_file():
        return "uninitialized", False
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        con.execute("SELECT id FROM publish_jobs LIMIT 1").fetchall()
        columns = {row[1] for row in con.execute("PRAGMA table_info(publish_job_targets)")}
        if not {"lease_token", "checkpoint_json", "next_attempt_at"}.issubset(columns):
            return "migration_required", False
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        root = Path(__file__).resolve().parents[1]
        cfg = Config(str(root / "alembic.ini"))
        cfg.set_main_option("script_location", str(root / "migrations"))
        head = ScriptDirectory.from_config(cfg).get_current_head()
        rows = con.execute("SELECT version_num FROM alembic_version").fetchall()
        return ("ok", True) if rows == [(head,)] else ("migration_required", False)
    finally:
        con.close()
