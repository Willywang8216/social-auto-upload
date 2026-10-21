"""Tests for the "publish unless I object" countdown.

Operator rule (2026-10-21): notify a day ahead; if nothing happens, publish as
it is. The dangerous parts are (a) publishing something the operator removed or
that is still inside its grace window, and (b) double-submitting when two
scheduler ticks overlap. Both are pinned here.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from myUtils import inbox_autopublish as ap


def _entry(**overrides) -> dict:
    entry = {
        "id": "both:demo:2026-10-21T01:00:00+00:00",
        "persona": "both",
        "profileIds": [1, 3],
        "topic": "Demo clip",
        "sfwFlag": "sfw",
        "kind": "video",
        "remotePath": "both/video/demo__sfw.mp4",
        "createdAt": "2026-10-21T01:00:00+00:00",
        "status": "ready",
    }
    entry.update(overrides)
    return entry


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)

    def _plan(self, entries: list[dict], **kwargs) -> dict:
        state = {"ready": entries}
        return ap.plan(state, self.now, grace_hours=kwargs.get("grace", 24),
                       remind_hours=kwargs.get("remind", 2),
                       max_attempts=kwargs.get("attempts", 3))

    def test_fresh_item_is_armed_with_a_24h_deadline(self):
        entry = _entry(createdAt=(self.now - timedelta(minutes=5)).isoformat())
        result = self._plan([entry])
        self.assertEqual(len(result["armed"]), 1)
        self.assertFalse(result["publish"])
        deadline = datetime.fromisoformat(entry["autoPublishAt"])
        self.assertAlmostEqual((deadline - self.now).total_seconds() / 3600, 24, delta=0.2)

    def test_item_inside_the_window_is_not_published(self):
        entry = _entry(createdAt=(self.now - timedelta(hours=2)).isoformat(),
                       autoPublishAt=(self.now + timedelta(hours=22)).isoformat())
        result = self._plan([entry])
        self.assertFalse(result["publish"])
        self.assertFalse(result["armed"])

    def test_item_past_the_deadline_is_published(self):
        entry = _entry(autoPublishAt=(self.now - timedelta(minutes=1)).isoformat())
        self.assertEqual(len(self._plan([entry])["publish"]), 1)

    def test_reminder_fires_inside_the_reminder_window_once(self):
        entry = _entry(autoPublishAt=(self.now + timedelta(hours=1)).isoformat())
        result = self._plan([entry])
        self.assertEqual(len(result["remind"]), 1)
        entry["autoReminderSent"] = self.now.isoformat()
        self.assertFalse(self._plan([entry])["remind"])

    def test_item_already_claimed_is_skipped(self):
        entry = _entry(autoPublishAt=(self.now - timedelta(hours=1)).isoformat(),
                       autoPublishState="publishing")
        self.assertFalse(self._plan([entry])["publish"])

    def test_item_without_a_drive_path_is_ignored(self):
        entry = _entry(remotePath="", autoPublishAt=(self.now - timedelta(hours=1)).isoformat())
        self.assertFalse(self._plan([entry])["publish"])

    def test_attempt_budget_exhaustion_stops_retrying(self):
        entry = _entry(autoPublishAt=(self.now - timedelta(hours=1)).isoformat(),
                       autoPublishAttempts=3)
        result = self._plan([entry])
        self.assertFalse(result["publish"])
        self.assertEqual(len(result["gave_up"]), 1)


class StartAtTests(unittest.TestCase):
    def test_publish_date_pins_the_day_at_2100_taipei(self):
        now = datetime(2026, 10, 21, 4, 0, tzinfo=timezone.utc)  # 12:00 Taipei
        value = ap.start_at_for({"publishDate": "2026-10-24"}, now)
        self.assertEqual(value, "2026-10-24T21:00:00")

    def test_past_publish_date_falls_back_to_the_next_slot(self):
        now = datetime(2026, 10, 25, 4, 0, tzinfo=timezone.utc)
        value = ap.start_at_for({"publishDate": "2026-10-24"}, now)
        self.assertEqual(value, "2026-10-25T12:15:00")


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "state.json"

    def _write(self, entries: list[dict]) -> None:
        self.state_path.write_text(json.dumps({
            "version": 1, "processed": {}, "ready": entries,
            "pending": [], "quarantined": [],
        }, ensure_ascii=False), encoding="utf-8")

    def test_successful_publish_clears_the_queue_item(self):
        entry = _entry(autoPublishAt="2020-01-01T00:00:00+00:00")
        self._write([entry])
        sent = []
        with patch.object(ap, "submit", return_value=(True, '{"code": 200, "data": {"campaignIds": [42]}}')), \
             patch.object(ap, "notify", side_effect=lambda t, b: sent.append(t) or True):
            code = ap.run(self.state_path, grace_hours=24, remind_hours=0, max_attempts=3, dry_run=False)
        self.assertEqual(code, 0)
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state["ready"], [])
        self.assertIn("drive:both/video/demo__sfw.mp4", state["processed"])
        self.assertTrue(any("已自動排程" in title for title in sent))

    def test_failed_publish_keeps_the_item_and_counts_the_attempt(self):
        entry = _entry(autoPublishAt="2020-01-01T00:00:00+00:00")
        self._write([entry])
        with patch.object(ap, "submit", return_value=(False, "HTTP 500: nope")), \
             patch.object(ap, "notify", return_value=True):
            code = ap.run(self.state_path, grace_hours=24, remind_hours=0, max_attempts=3, dry_run=False)
        self.assertEqual(code, 1)
        state = json.loads(self.state_path.read_text())
        self.assertEqual(len(state["ready"]), 1)
        self.assertEqual(state["ready"][0]["autoPublishAttempts"], 1)
        self.assertNotIn("autoPublishState", state["ready"][0])

    def test_rejected_item_is_never_published(self):
        self._write([])  # operator rejected it -> gone from ready
        with patch.object(ap, "submit") as submit:
            ap.run(self.state_path, grace_hours=24, remind_hours=0, max_attempts=3, dry_run=False)
        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
