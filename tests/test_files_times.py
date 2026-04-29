"""Tests for the schedule generator in utils.files_times."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from utils.files_times import generate_schedule_time_next_day


class GenerateScheduleTests(unittest.TestCase):
    def test_default_int_hours_still_work(self) -> None:
        schedule = generate_schedule_time_next_day(3, videos_per_day=1, daily_times=[10])
        self.assertEqual(len(schedule), 3)
        for item in schedule:
            self.assertEqual(item.hour, 10)
            self.assertEqual(item.minute, 0)

    def test_hh_mm_strings_from_frontend(self) -> None:
        # The Vue frontend sends entries like "10:00" / "14:30"
        schedule = generate_schedule_time_next_day(2, videos_per_day=2, daily_times=["10:00", "14:30"])
        self.assertEqual(len(schedule), 2)
        self.assertEqual((schedule[0].hour, schedule[0].minute), (10, 0))
        self.assertEqual((schedule[1].hour, schedule[1].minute), (14, 30))

    def test_schedule_starts_at_next_day_by_default(self) -> None:
        before = datetime.now()
        schedule = generate_schedule_time_next_day(1, videos_per_day=1, daily_times=["09:00"])
        # The first scheduled time must be on or after tomorrow midnight.
        tomorrow_midnight = (before + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        self.assertGreaterEqual(schedule[0], tomorrow_midnight)

    def test_invalid_string_rejected(self) -> None:
        with self.assertRaises((ValueError, TypeError)):
            generate_schedule_time_next_day(1, videos_per_day=1, daily_times=["not-a-time"])

    def test_too_many_videos_per_day_rejected(self) -> None:
        with self.assertRaises(ValueError):
            generate_schedule_time_next_day(2, videos_per_day=3, daily_times=["10:00"])

    def test_timestamps_flag_returns_ints(self) -> None:
        schedule = generate_schedule_time_next_day(1, videos_per_day=1, daily_times=["10:00"], timestamps=True)
        self.assertIsInstance(schedule[0], int)


if __name__ == "__main__":
    unittest.main()
