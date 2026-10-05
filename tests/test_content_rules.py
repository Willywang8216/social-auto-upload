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

    def test_prepare_threads_trims_to_500_characters(self) -> None:
        # Threads API rejects text longer than 500 characters (code 100).
        draft = content_rules.prepare_platform_draft(
            "threads",
            {"message": "x" * 900},
            contact_details="c@example.com",
            cta="Reply",
        )
        self.assertLessEqual(len(draft["message"]), 500)
        self.assertEqual(draft["charCount"], len(draft["message"]))

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

    def test_tiktok_draft_and_sheet_row_use_different_limits(self) -> None:
        message = 'x' * 500
        draft = content_rules.prepare_platform_draft('tiktok', {'message': message})
        self.assertEqual(len(draft['message']), 500)
        row = content_rules.build_sheet_row(message=draft['message'], platform='tiktok')
        self.assertEqual(len(row['Message']), 150)


class NormalizeDraftFieldsTests(unittest.TestCase):
    def test_plain_message_is_untouched(self) -> None:
        draft = content_rules.normalize_draft_fields({"message": "Just a caption"})
        self.assertEqual(draft["message"], "Just a caption")
        self.assertNotIn("title", draft)

    def test_labeled_message_is_stripped_of_labels(self) -> None:
        raw = (
            "Title: Taipei Stonewall\n\n"
            "Summary: A short summary\n\n"
            "Description: The real caption body. #tag"
        )
        draft = content_rules.normalize_draft_fields({"message": raw})
        self.assertEqual(draft["title"], "Taipei Stonewall")
        self.assertNotIn("Title:", draft["message"])
        self.assertNotIn("Summary:", draft["message"])
        self.assertNotIn("Description:", draft["message"])
        self.assertIn("The real caption body. #tag", draft["message"])

    def test_dict_message_extracts_description(self) -> None:
        draft = content_rules.normalize_draft_fields(
            {
                "message": {
                    "title": "A title",
                    "summary": "A summary",
                    "description": "The body",
                    "hashtags": ["#one"],
                    "firstComment": "First!",
                }
            }
        )
        self.assertEqual(draft["title"], "A title")
        self.assertIn("The body", draft["message"])
        self.assertTrue(draft["message"].startswith("A title"))
        self.assertEqual(draft["hashtags"], ["#one"])
        self.assertEqual(draft["firstComment"], "First!")

    def test_stringified_dict_message_is_parsed(self) -> None:
        raw = "{'title': 'A title', 'summary': 'A summary', 'description': 'The body'}"
        draft = content_rules.normalize_draft_fields({"message": raw})
        self.assertEqual(draft["title"], "A title")
        self.assertIn("The body", draft["message"])
        self.assertNotIn("{'", draft["message"])

    def test_chinese_labeled_message_is_stripped(self) -> None:
        raw = "標題：台北石牆\n\n摘要：一段摘要\n\n描述：真正的內容。"
        draft = content_rules.normalize_draft_fields({"message": raw})
        self.assertEqual(draft["title"], "台北石牆")
        self.assertNotIn("標題：", draft["message"])
        self.assertNotIn("摘要：", draft["message"])
        self.assertNotIn("描述：", draft["message"])
        self.assertIn("真正的內容。", draft["message"])

    def test_prepare_platform_draft_has_no_field_labels(self) -> None:
        raw = "Title: Taipei Stonewall\n\nDescription: A clean caption for Facebook."
        draft = content_rules.prepare_platform_draft("facebook", {"message": raw})
        self.assertNotIn("Title:", draft["message"])
        self.assertIn("A clean caption", draft["message"])
        self.assertEqual(draft["title"], "Taipei Stonewall")


if __name__ == "__main__":
    unittest.main()
