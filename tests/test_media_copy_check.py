"""Tests for myUtils.media_copy_check.

The important property is not what it flags, it is what it does when anything
goes wrong: this is an advisory gate in front of publishing, so every failure
must come back as ``checked=False`` and leave the post alone.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from myUtils import media_copy_check as check


def _fake_result(payload=None, content=""):
    return SimpleNamespace(parsed_json=payload, content=content)


class CopyTextTests(unittest.TestCase):
    def test_reads_message_then_title_then_description(self):
        self.assertEqual(check._copy_text({"message": "m"}), "m")
        self.assertEqual(check._copy_text({"title": "t"}), "t")
        self.assertEqual(check._copy_text({"description": "d"}), "d")

    def test_accepts_a_plain_string(self):
        self.assertEqual(check._copy_text("  hello  "), "hello")

    def test_ignores_empty_and_non_mapping(self):
        self.assertEqual(check._copy_text({"message": "   "}), "")
        self.assertEqual(check._copy_text(None), "")
        self.assertEqual(check._copy_text(42), "")


class CheckCopyAgainstMediaTests(unittest.TestCase):
    def test_no_media_is_unchecked_and_never_calls_the_model(self):
        with patch.object(check.llm_client, "generate_chat_completion") as call:
            result = check.check_copy_against_media(media=None, copy={"message": "hi"})
        self.assertEqual(result, {"checked": False, "contradicts": False, "reason": ""})
        call.assert_not_called()

    def test_empty_copy_is_unchecked(self):
        with patch.object(check.llm_client, "generate_chat_completion") as call:
            result = check.check_copy_against_media(media="/tmp/a.jpg", copy={"message": ""})
        self.assertFalse(result["checked"])
        call.assert_not_called()

    def test_a_clear_contradiction_is_reported(self):
        with patch.object(
            check.llm_client, "generate_chat_completion",
            return_value=_fake_result({"contradicts": True, "reason": "copy describes a beach; media is a studio shot"}),
        ) as call:
            result = check.check_copy_against_media(
                media="/tmp/a.jpg", copy={"message": "sunset at the beach"}, platform="bluesky"
            )
        self.assertEqual(result["checked"], True)
        self.assertEqual(result["contradicts"], True)
        self.assertIn("beach", result["reason"])
        # The image is actually handed to the model, and JSON is requested.
        self.assertEqual(call.call_args[1]["images"], ["/tmp/a.jpg"])
        self.assertTrue(call.call_args[1]["response_json"])

    def test_a_match_is_not_flagged(self):
        with patch.object(check.llm_client, "generate_chat_completion",
                          return_value=_fake_result({"contradicts": False, "reason": ""})):
            result = check.check_copy_against_media(media="/tmp/a.jpg", copy={"message": "hi"})
        self.assertEqual(result, {"checked": True, "contradicts": False, "reason": ""})

    def test_a_model_failure_leaves_the_post_alone(self):
        with patch.object(check.llm_client, "generate_chat_completion",
                          side_effect=RuntimeError("endpoint down")):
            result = check.check_copy_against_media(media="/tmp/a.jpg", copy={"message": "hi"})
        self.assertEqual(result, {"checked": False, "contradicts": False, "reason": ""})

    def test_unparseable_output_is_unchecked(self):
        with patch.object(check.llm_client, "generate_chat_completion",
                          return_value=_fake_result(None, content="not json at all")):
            result = check.check_copy_against_media(media="/tmp/a.jpg", copy={"message": "hi"})
        self.assertFalse(result["checked"])

    def test_a_json_string_body_is_still_accepted(self):
        with patch.object(check.llm_client, "generate_chat_completion",
                          return_value=_fake_result(None, content=json.dumps({"contradicts": True, "reason": "r"}))):
            result = check.check_copy_against_media(media="/tmp/a.jpg", copy={"message": "hi"})
        self.assertTrue(result["contradicts"])

    def test_the_reason_is_truncated(self):
        with patch.object(check.llm_client, "generate_chat_completion",
                          return_value=_fake_result({"contradicts": True, "reason": "x" * 500})):
            result = check.check_copy_against_media(media="/tmp/a.jpg", copy={"message": "hi"})
        self.assertLessEqual(len(result["reason"]), 300)

    def test_a_missing_contradicts_key_defaults_to_no_flag(self):
        """A model that answers with prose must not be read as a contradiction."""
        with patch.object(check.llm_client, "generate_chat_completion",
                          return_value=_fake_result({"reason": "looks fine"})):
            result = check.check_copy_against_media(media="/tmp/a.jpg", copy={"message": "hi"})
        self.assertEqual(result["checked"], True)
        self.assertFalse(result["contradicts"])


class KillSwitchTests(unittest.TestCase):
    def test_disabled_by_env_means_no_call(self):
        with patch.dict("os.environ", {"SAU_COPY_MEDIA_CHECK": "0"}), \
                patch.object(check.llm_client, "generate_chat_completion") as call:
            result = check.check_copy_against_media(media="/tmp/a.jpg", copy={"message": "hi"})
        self.assertFalse(result["checked"])
        call.assert_not_called()

    def test_enabled_by_default_and_by_other_values(self):
        for value in ("", "1", "true", "yes"):
            with patch.dict("os.environ", {"SAU_COPY_MEDIA_CHECK": value}), \
                    patch.object(check.llm_client, "generate_chat_completion",
                                 return_value=_fake_result({"contradicts": False})):
                self.assertEqual(
                    check.check_copy_against_media(media="/tmp/a.jpg", copy={"message": "hi"})["checked"],
                    True,
                    f"value={value!r} should keep the check on",
                )


class ReviewNoteTests(unittest.TestCase):
    def test_uses_the_reason_when_present(self):
        self.assertEqual(check.review_note({"reason": "wrong person"}), "文案與媒體內容可能不符：wrong person")

    def test_falls_back_when_the_reason_is_empty(self):
        self.assertEqual(check.review_note({}), "文案與媒體內容可能不符")


if __name__ == "__main__":
    unittest.main()
