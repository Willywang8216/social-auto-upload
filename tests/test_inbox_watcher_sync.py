"""The versioned watcher copy must match the live deployment, and stay lintable.

`/home/will/sau-inbox/` is where cron runs the watcher from and it is not a git
repository. The repo keeps a copy in `sau_inbox/` so the code has history, review
and CI coverage; a missing `timedelta` import in that file crashed every scan for
days with nothing failing a build. These tests make drift and that bug class loud.
"""

from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VERSIONED = REPO / "sau_inbox"
LIVE = Path(os.environ.get("SAU_INBOX", "/home/will/sau-inbox"))
FILES = ("watch.py", "thumb.py", "manual_platforms.py", "gen_title.py", "inbox_monitor.py")

_spec = importlib.util.spec_from_file_location(
    "check_undefined_names", REPO / "scripts" / "check_undefined_names.py"
)
checker = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(checker)


class InboxWatcherVersionedTests(unittest.TestCase):
    def test_the_versioned_copy_exists(self):
        for name in FILES:
            self.assertTrue((VERSIONED / name).is_file(), f"missing sau_inbox/{name}")

    def test_no_undefined_names(self):
        """The bug that started this: `timedelta` used but not imported."""
        problems = []
        for name in FILES:
            problems.extend(checker.check_file(VERSIONED / name))
        self.assertEqual(problems, [], f"undefined names: {problems}")

    def test_every_file_compiles(self):
        for name in FILES:
            source = (VERSIONED / name).read_text(encoding="utf-8")
            compile(source, str(VERSIONED / name), "exec")

    @unittest.skipUnless(LIVE.is_dir(), "live inbox directory not present")
    def test_the_live_deployment_matches_the_versioned_copy(self):
        drifted = []
        for name in FILES:
            live = LIVE / name
            if not live.is_file():
                drifted.append(f"{name}: missing on the host")
                continue
            if live.read_text(encoding="utf-8") != (VERSIONED / name).read_text(encoding="utf-8"):
                drifted.append(f"{name}: live file differs from sau_inbox/{name}")
        self.assertEqual(
            drifted,
            [],
            "the live watcher and the versioned copy have drifted; "
            "sync them (see sau_inbox/README.md): " + "; ".join(drifted),
        )


if __name__ == "__main__":
    unittest.main()
