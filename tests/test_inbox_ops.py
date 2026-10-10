"""SAU-Inbox state access: path resolution and stale-item handling.

The inbox path used to be derived from BASE_DIR alone, which made the host and
the container disagree: on the host BASE_DIR is /home/will/social-auto-upload, so
the inbox resolved to a directory that does not exist and list_items() reported
zero ready items while 304 were actually queued in /home/will/sau-inbox. Reading
the same state as the container is the only correct behaviour.
"""

import importlib
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from myUtils import inbox_ops

PRODUCTION_INBOX = Path("/home/will/sau-inbox")


class InboxDirResolutionTests(unittest.TestCase):
    """Call the resolver directly.

    Reloading the module here would mutate shared import state for every other
    test in the suite (it passed alone and failed in the full run), so the pure
    function is exercised instead - which is also the part that can be wrong.
    """

    def test_explicit_inbox_env_wins(self):
        with patch.dict(os.environ, {"SAU_INBOX": "/tmp/explicit-inbox"}, clear=False):
            self.assertEqual(
                str(inbox_ops._resolve_inbox_dir()), "/tmp/explicit-inbox"
            )

    def test_explicit_state_path_sets_the_dir(self):
        env = {"SAU_WATCH_STATE": "/tmp/elsewhere/state.json"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("SAU_INBOX", None)
            self.assertEqual(str(inbox_ops._resolve_inbox_dir()), "/tmp/elsewhere")

    def test_the_known_absolute_inbox_is_preferred_over_base_dir(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SAU_INBOX", None)
            os.environ.pop("SAU_WATCH_STATE", None)
            resolved = inbox_ops._resolve_inbox_dir()
        if (PRODUCTION_INBOX / "state.json").is_file():
            self.assertEqual(resolved, PRODUCTION_INBOX)
            # Specifically NOT the project-relative path that does not exist and
            # silently reported zero ready items.
            self.assertNotEqual(resolved, inbox_ops.BASE_DIR / "sau-inbox")

    def test_module_level_paths_are_consistent(self):
        self.assertEqual(inbox_ops.STATE_PATH, inbox_ops.INBOX_DIR / "state.json")


class StaleReadyItemTests(unittest.TestCase):
    """A 'ready' item whose source is gone is processed work, not queue."""

    def test_the_state_shape_exposes_the_lists_the_health_check_reports(self):
        lists = inbox_ops.list_items()
        for key in ("ready", "pending", "quarantined"):
            with self.subTest(key=key):
                self.assertIn(key, lists)
                self.assertIsInstance(lists[key], (list, dict))

    def test_a_ready_item_with_a_missing_source_is_detectable(self):
        # The classifier used to clear the stale queue: sourcePath set but the
        # file is gone. This pins that predicate so the 304-item cleanup can be
        # reasoned about rather than re-derived.
        items = inbox_ops.list_items().get("ready") or []
        stale = [
            i for i in items
            if i.get("sourcePath") and not Path(i["sourcePath"]).exists()
        ]
        # After the cleanup there should be none; if the watcher re-introduces
        # them the count is what the operator needs to see.
        self.assertEqual(len(stale), 0, f"{len(stale)} stale ready item(s) present")


if __name__ == "__main__":
    unittest.main()
