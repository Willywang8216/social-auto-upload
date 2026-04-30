"""Regression tests for the conf.py backfill.

These cover the production crash where a stripped-down user-mounted
``conf.py`` triggered ``ImportError: cannot import name 'BASE_DIR'``
the moment ``sau_backend.py`` started up.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import ModuleType

from conf_defaults import _EXPECTED_ATTRS, apply_conf_defaults


class ConfBackfillTests(unittest.TestCase):
    """``apply_conf_defaults`` makes a partial ``conf`` module whole."""

    def setUp(self) -> None:
        self._saved_conf = sys.modules.get("conf")

    def tearDown(self) -> None:
        # Restore whatever ``conf`` was before the test so other tests
        # don't see a fabricated module.
        if self._saved_conf is None:
            sys.modules.pop("conf", None)
        else:
            sys.modules["conf"] = self._saved_conf

    def test_partial_conf_gets_missing_attrs_backfilled(self) -> None:
        # Simulate the production-bug shape: a ``conf`` module that
        # defines only one attribute and is missing ``BASE_DIR`` etc.
        partial = ModuleType("conf")
        partial.LOCAL_CHROME_PATH = "/usr/bin/chrome"  # explicit override
        sys.modules["conf"] = partial

        apply_conf_defaults()

        # Explicit user value preserved.
        self.assertEqual(partial.LOCAL_CHROME_PATH, "/usr/bin/chrome")
        # Missing attrs filled in from the defaults module.
        for attr in _EXPECTED_ATTRS:
            self.assertTrue(
                hasattr(partial, attr),
                f"expected ``conf.{attr}`` after backfill",
            )
        self.assertIsInstance(partial.BASE_DIR, Path)
        self.assertIsInstance(partial.LOCAL_CHROME_HEADLESS, bool)

    def test_complete_conf_is_unchanged(self) -> None:
        complete = ModuleType("conf")
        complete.BASE_DIR = Path("/tmp/custom-base")
        complete.XHS_SERVER = "http://xhs.example.com"
        complete.LOCAL_CHROME_PATH = "/opt/chrome"
        complete.LOCAL_CHROME_HEADLESS = False
        complete.DEBUG_MODE = False
        sys.modules["conf"] = complete

        apply_conf_defaults()

        self.assertEqual(complete.BASE_DIR, Path("/tmp/custom-base"))
        self.assertEqual(complete.XHS_SERVER, "http://xhs.example.com")
        self.assertEqual(complete.LOCAL_CHROME_PATH, "/opt/chrome")
        self.assertFalse(complete.LOCAL_CHROME_HEADLESS)
        self.assertFalse(complete.DEBUG_MODE)

    def test_idempotent(self) -> None:
        partial = ModuleType("conf")
        sys.modules["conf"] = partial
        apply_conf_defaults()
        first_base_dir = partial.BASE_DIR
        apply_conf_defaults()
        self.assertEqual(partial.BASE_DIR, first_base_dir)


if __name__ == "__main__":
    unittest.main()
