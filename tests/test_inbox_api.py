"""Focused unit tests for myUtils/inbox_ops.py.

Uses a scratch temp dir for SAU_INBOX / SAU_WATCH_STATE and reloads the module
so the module-level paths pick up the sandbox. No Flask import.
"""

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path


class InboxOpsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._inbox = Path(self._tmp.name) / "sau-inbox"
        self._inbox.mkdir()
        os.environ["SAU_INBOX"] = str(self._inbox)
        os.environ["SAU_WATCH_STATE"] = str(self._inbox / "state.json")
        self.ops = importlib.reload(importlib.import_module("myUtils.inbox_ops"))
        self._seed_state()

    def tearDown(self):
        self._tmp.cleanup()

    def _seed_state(self):
        state = {
            "version": 1,
            "processed": {},
            "ready": [
                {
                    "id": "item-1",
                    "persona": "nw",
                    "profileIds": [1],
                    "topic": "math demo",
                    "sfwFlag": True,
                    "kind": "video",
                    "sourcePath": "/tmp/x.mp4",
                    "thumbPath": None,
                    "brief": "A brief.",
                    "contentNote": "Note.",
                    "status": "ready",
                    "createdAt": "2026-09-17T00:00:00+00:00",
                    "needsTitleGeneration": True,
                    "needsFrameSelection": False,
                }
            ],
            "pending": [],
            "quarantined": [],
        }
        (self._inbox / "state.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )

    def _state(self) -> dict:
        return json.loads((self._inbox / "state.json").read_text(encoding="utf-8"))

    def test_list_items_returns_one_ready(self):
        items = self.ops.list_items()
        self.assertEqual(len(items["ready"]), 1)
        self.assertEqual(items["pending"], [])
        self.assertEqual(items["quarantined"], [])
        self.assertEqual(items["ready"][0]["id"], "item-1")

    def test_missing_state_file_yields_empty_lists(self):
        (self._inbox / "state.json").unlink()
        items = self.ops.list_items()
        self.assertEqual(items, {"ready": [], "pending": [], "quarantined": []})

    def test_corrupt_state_file_yields_empty_lists(self):
        (self._inbox / "state.json").write_text("{not json", encoding="utf-8")
        items = self.ops.list_items()
        self.assertEqual(items, {"ready": [], "pending": [], "quarantined": []})

    def test_approve_moves_item_to_processed(self):
        entry = self.ops.approve("item-1")
        self.assertEqual(entry["id"], "item-1")
        self.assertEqual(entry["status"], "processed")
        state = self._state()
        self.assertEqual(state["ready"], [])

    def test_approve_unknown_item_raises_lookup_error(self):
        with self.assertRaises(LookupError):
            self.ops.approve("does-not-exist")

    def test_reject_moves_item_to_quarantined(self):
        entry = self.ops.reject("item-1", reason="bad audio")
        self.assertEqual(entry["id"], "item-1")
        self.assertEqual(entry["status"], "quarantined")
        self.assertEqual(entry["reason"], "bad audio")
        state = self._state()
        self.assertEqual(state["ready"], [])
        self.assertEqual(len(state["quarantined"]), 1)
        self.assertEqual(state["quarantined"][0]["reason"], "bad audio")

    def test_reject_unknown_item_raises_lookup_error(self):
        with self.assertRaises(LookupError):
            self.ops.reject("does-not-exist", reason="nope")

    def test_item_payload_maps_fields(self):
        items = self.ops.list_items()
        payload = self.ops.item_payload(items["ready"][0])
        self.assertEqual(payload["id"], "item-1")
        self.assertEqual(payload["persona"], "nw")
        self.assertEqual(payload["profileIds"], [1])
        self.assertEqual(payload["topic"], "math demo")
        self.assertTrue(payload["needsTitleGeneration"])
        self.assertFalse(payload["needsFrameSelection"])


if __name__ == "__main__":
    unittest.main()