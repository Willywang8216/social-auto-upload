from __future__ import annotations

import os
from pathlib import Path

try:
    import conf as _conf
except ModuleNotFoundError:  # pragma: no cover - environment-specific
    _conf = None

_BASE_DIR_DEFAULT = Path(__file__).resolve().parent.parent

BASE_DIR = Path(getattr(_conf, 'BASE_DIR', _BASE_DIR_DEFAULT))


def default_db_path() -> Path:
    """Resolve the SQLite database path, honouring ``SAU_DB_PATH``.

    ``SAU_DB_PATH`` is the documented override (used by ``db/createTable.py``,
    the worker CLI, the migration and the ``scripts/*`` tools). Modules bind
    ``DB_PATH`` at import time, so the override must be set before the module
    is first imported — which is exactly what the test suite's ``conftest.py``
    does. Without this, modules silently defaulted to the production database
    and a full test run published and failed live targets (see 2026-10-11: the
    suite claimed production jobs 5252-5255 and 5332).
    """
    override = str(os.environ.get("SAU_DB_PATH", "") or "").strip()
    if override:
        return Path(override)
    return Path(BASE_DIR) / "db" / "database.db"


XHS_SERVER = getattr(_conf, 'XHS_SERVER', 'http://127.0.0.1:11901')
LOCAL_CHROME_PATH = getattr(_conf, 'LOCAL_CHROME_PATH', '')
LOCAL_CHROME_HEADLESS = getattr(_conf, 'LOCAL_CHROME_HEADLESS', True)
DEBUG_MODE = getattr(_conf, 'DEBUG_MODE', True)
YT_PROXY = getattr(_conf, 'YT_PROXY', None)
REDDIT_PROXY = getattr(_conf, 'REDDIT_PROXY', None)
