"""The archive note must catch up once the archived campaign actually publishes.

The cron archives at scheduling time (so the operator's drop box stays clean),
which means the first note says "scheduled, not published yet". When the
campaign finishes, the note has to say so, and the ledger must record that it
was refreshed exactly once.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from myUtils import inbox_archive

from tests.test_inbox_archive import _campaign, _db


class RefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "db.sqlite"
        self.ledger = Path(self.tmp.name) / "ledger.json"
        _db(self.db)
        self._ledger_patch = patch.object(inbox_archive, "LEDGER", self.ledger)
        self._ledger_patch.start()
        self.addCleanup(self._ledger_patch.stop)

    def _write_ledger(self, published_at_archive: bool) -> None:
        self.ledger.write_text(json.dumps({
            "both/video/Clip__nsfw.mp4": {
                "archived": "Clip_NSFW_20261022.mp4",
                "campaign_id": 9,
                "published_at_archive": published_at_archive,
            }
        }), encoding="utf-8")

    def test_note_is_refreshed_once_the_campaign_published(self):
        _campaign(self.db, campaign_id=9, filename="Clip__nsfw.mp4",
                  title="Clip", statuses=["succeeded", "succeeded"])
        self._write_ledger(published_at_archive=False)
        ok = type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()
        with patch.object(inbox_archive, "_rclone", return_value=ok) as mocked:
            errors = inbox_archive.refresh_published_notes(self.db)
        self.assertEqual(errors, [])
        self.assertEqual(mocked.call_count, 1)
        ledger = json.loads(self.ledger.read_text())
        self.assertTrue(ledger["both/video/Clip__nsfw.mp4"]["published_at_archive"])

        # second pass must not touch Drive again
        with patch.object(inbox_archive, "_rclone", return_value=ok) as mocked2:
            inbox_archive.refresh_published_notes(self.db)
        self.assertEqual(mocked2.call_count, 0)

    def test_nothing_happens_while_the_campaign_is_only_scheduled(self):
        _campaign(self.db, campaign_id=9, filename="Clip__nsfw.mp4",
                  title="Clip", statuses=["pending"])
        self._write_ledger(published_at_archive=False)
        with patch.object(inbox_archive, "_rclone", return_value=None) as mocked:
            errors = inbox_archive.refresh_published_notes(self.db)
        self.assertEqual(errors, [])
        self.assertEqual(mocked.call_count, 0)

    def test_failed_refresh_is_reported_and_retried_next_run(self):
        _campaign(self.db, campaign_id=9, filename="Clip__nsfw.mp4",
                  title="Clip", statuses=["succeeded"])
        self._write_ledger(published_at_archive=False)
        bad = type("Result", (), {"returncode": 1, "stderr": "boom", "stdout": ""})()
        with patch.object(inbox_archive, "_rclone", return_value=bad):
            errors = inbox_archive.refresh_published_notes(self.db)
        self.assertEqual(len(errors), 1)
        ledger = json.loads(self.ledger.read_text())
        self.assertFalse(ledger["both/video/Clip__nsfw.mp4"]["published_at_archive"])


if __name__ == "__main__":
    unittest.main()
