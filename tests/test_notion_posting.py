"""Tests for myUtils.notion_posting (density-aware Notion scheduling)."""

from __future__ import annotations

import unittest
from datetime import datetime

from myUtils import notion_posting as np


class SelectRowsTests(unittest.TestCase):
    def _rows(self, n_nw=1, n_sw=0):
        rows = []
        for i in range(n_nw):
            rows.append({"page_id": f"nw-{i}", "title": f"NW piece {i}", "brand": "nw",
                         "content": "body"})
        for i in range(n_sw):
            rows.append({"page_id": f"sw-{i}", "title": f"SW piece {i}", "brand": "sw",
                         "content": "body"})
        return rows

    def test_single_row_lands_in_publishing_window(self):
        slots = np.select_rows_to_schedule(
            self._rows(1, 0), None, now=datetime(2026, 9, 17, 6, 0, 0))
        self.assertEqual(len(slots), 1)
        self.assertTrue(slots[0].scheduled_at.startswith("2026-09-17T13:00"))
        self.assertEqual(slots[0].account_id, 112)
        self.assertEqual(slots[0].profile_id, 1)

    def test_two_rows_same_brand_use_consecutive_days(self):
        slots = np.select_rows_to_schedule(
            self._rows(2, 0), None, now=datetime(2026, 9, 17, 6, 0, 0))
        days = [s.scheduled_at[:10] for s in slots]
        self.assertEqual(days, ["2026-09-17", "2026-09-18"])

    def test_two_brands_same_day_are_staggered(self):
        slots = np.select_rows_to_schedule(
            self._rows(1, 1), None, now=datetime(2026, 9, 17, 6, 0, 0))
        times = sorted(s.scheduled_at for s in slots)
        self.assertEqual(len(times), 2)
        # same day, but 5 minutes apart
        self.assertEqual(times[0][:10], times[1][:10])
        hhmm_a = int(times[0][11:13]) * 60 + int(times[0][14:16])
        hhmm_b = int(times[1][11:13]) * 60 + int(times[1][14:16])
        self.assertEqual(hhmm_b - hhmm_a, np.DEFAULT_STAGGER_MINUTES)

    def test_never_in_the_past(self):
        # now is after the publishing window today -> roll to tomorrow
        slots = np.select_rows_to_schedule(
            self._rows(1, 0), None, now=datetime(2026, 9, 17, 20, 0, 0))
        self.assertEqual(slots[0].scheduled_at[:10], "2026-09-18")

    def test_existing_bookings_are_respected(self):
        booked = {112: ["2026-09-17T13:00:00", "2026-09-18T13:00:00"]}
        slots = np.select_rows_to_schedule(
            self._rows(1, 0), booked, now=datetime(2026, 9, 17, 6, 0, 0))
        self.assertEqual(slots[0].scheduled_at[:10], "2026-09-19")

    def test_missing_brand_raises(self):
        with self.assertRaises(ValueError):
            np.select_rows_to_schedule(
                [{"page_id": "x", "title": "t", "content": "b"}], None,
                now=datetime(2026, 9, 17, 6, 0, 0))

    def test_build_job_spec_shape(self):
        slots = np.select_rows_to_schedule(
            self._rows(1, 0), None, now=datetime(2026, 9, 17, 6, 0, 0))
        spec = np.build_job_spec(slots[0], "MDX BODY")
        self.assertEqual(spec["platform"], "nw_sw_blog")
        self.assertEqual(spec["profile_id"], 1)
        self.assertEqual(spec["payload"]["message"], "MDX BODY")
        self.assertEqual(spec["targets"][0][0], "account:112")
        self.assertTrue(spec["targets"][0][2].startswith("2026-09-17T13:00"))
        self.assertEqual(spec["idempotency_key"], "notion-nw-0")


if __name__ == "__main__":
    unittest.main()
