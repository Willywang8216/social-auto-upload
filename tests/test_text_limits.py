"""Unicode/field boundary regressions; no provider calls or copy regeneration."""
from __future__ import annotations

import pytest

from myUtils import content_rules, prepared_publishers as publishers, text_limits as text


@pytest.mark.parametrize("value,graphemes,utf16,utf8", [
    ("漢字", 2, 2, 6),
    ("e\u0301", 1, 2, 3),
    ("🙂", 1, 2, 4),
    ("🇹🇼", 1, 4, 8),
    ("👨‍👩‍👧‍👦", 1, 11, 25),
    ("👍🏽", 1, 4, 8),
    ("\r\n", 1, 2, 2),
])
def test_real_extended_graphemes_and_encoding_units(value, graphemes, utf16, utf8):
    assert text.grapheme_length(value) == graphemes
    assert text.utf16_length(value) == utf16
    assert text.utf8_length(value) == utf8


def test_youtube_description_is_5000_utf8_bytes_not_characters():
    boundary = "漢" * 1666 + "ab"
    assert text.message_length(boundary, "youtube") == 5000
    assert text.validate_message(boundary, "youtube") == boundary
    with pytest.raises(text.TextLimitError, match="utf8_bytes"):
        text.validate_message("漢" * 1667, "youtube")
    assert text.validate_message("🙂" * 1250, "youtube", field="description") == "🙂" * 1250
    with pytest.raises(text.TextLimitError):
        text.validate_message("🙂" * 1251, "youtube")
    assert text.validate_message("🙂" * 100, "youtube", field="title") == "🙂" * 100
    with pytest.raises(text.TextLimitError):
        text.validate_message("a" * 101, "youtube", field="title")


def test_youtube_tag_aggregate_counts_commas_and_quoted_spaces():
    assert text.youtube_tags_length([]) == 0
    assert text.youtube_tags_length(["two words", "漢字"]) == 14
    assert text.validate_youtube_tags(["a" * 494, "x y"]) == ["a" * 494, "x y"]
    with pytest.raises(text.TextLimitError, match="tag_characters"):
        text.validate_youtube_tags(["a" * 495, "x y"])


def test_tiktok_caption_boundary_uses_utf16_even_for_emoji():
    assert text.validate_message("🙂" * 1100, "tiktok", field="caption") == "🙂" * 1100
    with pytest.raises(text.TextLimitError, match="utf16_units"):
        text.validate_message("🙂" * 1101, "tiktok")
    with pytest.raises(publishers.PreparedPublishError, match="utf16_units") as error:
        publishers._validate_tiktok_video_artifact({}, message="🙂" * 1101, config={})
    assert error.value.retryable is False


def test_bluesky_checks_graphemes_and_secondary_byte_cap():
    boundary = "🇹🇼" * 300
    assert text.message_length(boundary, "bluesky") == 300
    assert text.validate_message(boundary, "bluesky") == boundary
    with pytest.raises(text.TextLimitError, match="graphemes"):
        text.validate_message(boundary + "a", "bluesky")
    assert text.validate_message("👨‍👩‍👧‍👦" * 120, "bluesky") == "👨‍👩‍👧‍👦" * 120
    with pytest.raises(text.TextLimitError, match="utf8_bytes"):
        text.validate_message("👨‍👩‍👧‍👦" * 121, "bluesky")
    assert text.validate_message("e\u0301" * 300, "bluesky") == "e\u0301" * 300


def test_x_weighted_cjk_emoji_nfc_and_urls():
    assert text.weighted_length("漢" * 140) == 280
    assert text.weighted_length("e\u0301") == 1  # count NFC, do NOT rewrite copy
    assert text.weighted_length("👨‍👩‍👧‍👦") == 2
    assert text.weighted_length("https://example.com/" + "a" * 500) == 23
    assert text.weighted_length("(https://example.com/path).") == 26
    boundary = "a" * 256 + " " + "https://example.com/" + "b" * 500
    assert text.validate_message(boundary, "twitter") == boundary
    with pytest.raises(text.TextLimitError):
        text.validate_message(boundary + "a", "twitter", max_length=279)


def test_limits_can_tighten_never_relax_and_unknown_is_not_unlimited():
    with pytest.raises(text.TextLimitError):
        text.validate_message("a" * 281, "twitter", max_length=500)
    with pytest.raises(text.TextLimitError):
        text.validate_message("a" * 51, "twitter", max_length=50)
    with pytest.raises(ValueError, match="No verified"):
        text.validate_message("hello", "unknown")
    for value in (-1, True, 1.5):
        with pytest.raises(ValueError):
            text.validate_message("hello", "twitter", max_length=value)
    with pytest.raises(ValueError, match="invalid Unicode"):
        text.validate_message("\ud83d", "tiktok")


@pytest.mark.parametrize("platform,source", [
    ("youtube", "漢" * 2000), ("tiktok", "🙂" * 1200),
    ("bluesky", "👨‍👩‍👧‍👦" * 150), ("twitter", "漢" * 180),
])
def test_ordinary_draft_truncation_is_grapheme_safe_and_measured(platform, source):
    result = text.truncate_message(source, platform)
    assert text.validate_message(result, platform) == result
    assert result.endswith("…")
    assert "\ud83d" not in result
    draft = content_rules.prepare_platform_draft(platform, {"message": source})
    assert draft["charCount"] == text.message_length(draft["message"], platform)
    text.validate_message(draft["message"], platform)


def test_strict_publishers_and_content_rules_preserve_exact_approved_copy():
    approved = {"message": "  e\u0301\n\nReal copy.  ", "hashtags": ["#original"], "title": "Original"}
    result = content_rules.prepare_platform_draft("twitter", approved, preserve_approved=True)
    assert result["message"] == approved["message"]
    assert result["hashtags"] == approved["hashtags"]
    assert publishers._enforce_message_limit(approved["message"], "twitter", strict=True) == approved["message"]
    row = content_rules.build_sheet_row(message=approved["message"], platform="twitter", preserve_approved=True)
    assert row["Message"] == approved["message"]
    with pytest.raises(publishers.PreparedPublishError):
        publishers._enforce_message_limit("漢" * 141, "twitter", payload={"_preserveApprovedText": True})
    with pytest.raises(text.TextLimitError):
        content_rules.prepare_platform_draft("youtube", {"message": "漢" * 1667}, preserve_approved=True)


def test_youtube_materialization_preserves_strict_fields_and_rejects_overflows():
    payload = {"_preserveApprovedText": True, "draft": {
        "title": "Exact title", "message": "  Exact description.  ", "tags": ["one", "two words"]}}
    fields = publishers.youtube_text_fields(payload)
    assert fields == {"title": "Exact title", "description": "  Exact description.  ", "tags": ["one", "two words"]}
    with pytest.raises(publishers.PreparedPublishError, match="utf8_bytes"):
        publishers.youtube_text_fields({"_preserveApprovedText": True,
            "draft": {"title": "T", "message": "漢" * 1667}})
    with pytest.raises(publishers.PreparedPublishError, match="tag_characters"):
        publishers.youtube_text_fields({"_preserveApprovedText": True,
            "draft": {"title": "T", "message": "Copy", "tags": ["a" * 495, "x y"]}})
    ordinary = publishers.youtube_text_fields({"draft": {"title": "T", "message": "漢" * 2000}})
    assert text.utf8_length(ordinary["description"]) <= 5000


def test_bluesky_recovery_does_not_append_or_truncate_hashtags():
    original = "e\u0301 " * 80
    payload = {"recoveryGeneration": "g", "draft": {"message": original, "hashtags": []}}
    assert publishers._bluesky_message_and_media(payload)[0] == original
    with pytest.raises(publishers.PreparedPublishError, match="review required"):
        publishers._bluesky_message_and_media({"_preserveApprovedText": True,
            "draft": {"message": "Approved", "hashtags": ["#new"]}})
