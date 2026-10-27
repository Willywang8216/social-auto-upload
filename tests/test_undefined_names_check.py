"""The undefined-name checker must catch what compileall cannot.

A missing `timedelta` import in the inbox watcher crashed the scan loop every
minute for days: the item was downloaded, the thumbnail built, and then the scan
died before anything was queued — with only a traceback in watch.log.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "check_undefined_names", REPO / "scripts" / "check_undefined_names.py"
)
checker = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(checker)


def _write(tmp: str, name: str, source: str) -> Path:
    path = Path(tmp) / name
    path.write_text(source, encoding="utf-8")
    return path


class CheckerTests(unittest.TestCase):
    def test_the_exact_watcher_bug_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "buggy.py", "from datetime import datetime, timezone\n"
                                         "x = datetime.now(timezone.utc) + timedelta(hours=8)\n")
            findings = checker.check_file(path)
            self.assertTrue(findings, "a missing timedelta import must be reported")
            self.assertEqual(findings[0][2], "timedelta")

    def test_correct_import_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "fine.py", "from datetime import datetime, timedelta, timezone\n"
                                          "x = datetime.now(timezone.utc) + timedelta(hours=8)\n")
            self.assertEqual(checker.check_file(path), [])

    def test_locals_parameters_and_comprehensions_are_not_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "scopes.py", "import os\n"
                                           "def f(a, b=1):\n"
                                           "    c = [x for x in range(a)]\n"
                                           "    with open(os.devnull) as fh:\n"
                                           "        return c, b, fh\n")
            self.assertEqual(checker.check_file(path), [])

    def test_syntax_errors_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "bad.py", "def f(:\n")
            findings = checker.check_file(path)
            self.assertTrue(findings)
            self.assertIn("SyntaxError", findings[0][2])

    def test_the_real_repo_is_clean(self):
        problems = []
        for path in checker.iter_targets(["myUtils", "sau_backend.py"]):
            problems.extend(checker.check_file(path))
        self.assertEqual(problems, [], f"undefined names: {problems}")


if __name__ == "__main__":
    unittest.main()
