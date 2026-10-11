"""``SAU_DB_PATH`` must actually isolate every module from the production DB.

The suite's ``conftest.py`` pins ``SAU_DB_PATH`` to a throwaway database, but
``myUtils.jobs`` / ``myUtils.campaigns`` / ``myUtils.profiles`` (and a dozen
other modules) bound ``Path(BASE_DIR)/db/database.db`` at import and ignored
the override. Any test that started a worker without an explicit ``db_path``
therefore drained the *production* queue: on 2026-10-11 a full test run claimed
live targets 5252-5255 and 5332, published 5252, and permanently failed the
rest with "cannot schedule new futures after interpreter shutdown".

``default_db_path()`` centralises the resolution so binding at import time
honours ``SAU_DB_PATH`` (same shape as ``db/createTable.py`` and the worker/CLI
tools), with production behaviour unchanged when the variable is unset.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def test_default_db_path_honours_the_override(monkeypatch) -> None:
    from utils import conf_defaults

    monkeypatch.setenv("SAU_DB_PATH", "/tmp/sau-isolation-probe.db")
    assert conf_defaults.default_db_path() == Path("/tmp/sau-isolation-probe.db")


def test_default_db_path_falls_back_to_the_app_db(monkeypatch) -> None:
    from utils import conf_defaults

    monkeypatch.delenv("SAU_DB_PATH", raising=False)
    assert conf_defaults.default_db_path() == Path(conf_defaults.BASE_DIR) / "db" / "database.db"


def test_suite_modules_point_at_the_pinned_database() -> None:
    """The conftest-pinned path must reach the queue/worker modules.

    Guards the exact regression: if a module can reach the live DB while the
    suite believes it is isolated, tests publish real posts.
    """

    pinned = os.environ.get("SAU_DB_PATH")
    # conftest.py always sets this for the suite; if it is missing the
    # isolation contract itself has been removed, which is a failure.
    assert pinned, "conftest did not pin SAU_DB_PATH"

    import myUtils.campaigns as campaigns
    import myUtils.jobs as jobs
    import myUtils.profiles as profiles

    for module in (jobs, campaigns, profiles):
        assert Path(module.DB_PATH).resolve() == Path(pinned).resolve(), (
            f"{module.__name__}.DB_PATH={module.DB_PATH} escapes the pinned "
            f"database {pinned}"
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
