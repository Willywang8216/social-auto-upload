"""Tests for platform content rules and sheet row mapping."""

from __future__ import annotations

import unittest
from datetime import datetime

from myUtils import content_rules


class ContentRulesTests(unittest.TestCase):
    def test_prepare_twitter_draft_enforces_emoji_and_three_hashtags(self) -> None:
        draft = content_rules.prepare_platform_draft(
            "twitter",
            {"message": "Launching the new clip", "hashtags": ["Launch"]},
        )
        self.assertTrue(draft["message"].startswith(content_rules.DEFAULT_EMOJI))
        self.assertEqual(len(draft["hashtags"]), 3)
        self.assertLessEqual(draft["charCount"], 280)

    def test_prepare_threads_requires_contact_and_cta(self) -> None:
        draft = content_rules.prepare_platform_draft(
            "threads",
            {"message": "Full story inside"},
            contact_details="contact@example.com",
            cta="Reply for details",
        )
        self.assertIn("contact@example.com", draft["message"])
        self.assertIn("Reply for details", draft["message"])

    def test_build_sheet_row_maps_schedule_and_story(self) -> None:
        row = content_rules.build_sheet_row(
            message="Hello",
            link="https://example.com",
            image_urls=["https://example.com/a.jpg", "https://example.com/b.jpg"],
            schedule=datetime(2026, 5, 2, 14, 30),
            story=True,
            post_preset="Brand preset",
        )
        self.assertEqual(row["ImageURL"], "https://example.com/a.jpg,https://example.com/b.jpg")
        self.assertEqual(row["Month(1-12)"], "5")
        self.assertEqual(row["Story(YorN)"], "Y")
        self.assertEqual(row["PostPreset"], "Brand preset")

    def test_sheet_row_rejects_image_and_video_together(self) -> None:
        with self.assertRaises(ValueError):
            content_rules.build_sheet_row(
                message="Hello",
                image_urls=["https://example.com/a.jpg"],
                video_url="https://example.com/a.mp4",
            )


if __name__ == "__main__":
    unittest.main()
