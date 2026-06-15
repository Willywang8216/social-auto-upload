"""Tests for content generator service."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from myUtils import content_generator


@pytest.fixture()
def tmp_db(tmp_path):
    db = tmp_path / "test.db"
    from db.createTable import bootstrap
    bootstrap(db)
    return db


class TestPlatformCharLimits:
    def test_twitter_limit(self):
        assert content_generator.PLATFORM_CHAR_LIMITS["twitter"] == 280

    def test_instagram_limit(self):
        assert content_generator.PLATFORM_CHAR_LIMITS["instagram"] == 2200

    def test_tiktok_limit(self):
        assert content_generator.PLATFORM_CHAR_LIMITS["tiktok"] == 150


class TestParseLLMResponse:
    def test_parse_json_object(self):
        raw = '{"message": "Hello world", "hashtags": ["#test"]}'
        result = content_generator.parse_llm_response(raw, "twitter")
        assert result["message"] == "Hello world"
        assert result["hashtags"] == ["#test"]

    def test_parse_markdown_fenced(self):
        raw = '```json\n{"message": "Test", "hashtags": ["#a", "#b"]}\n```'
        result = content_generator.parse_llm_response(raw, "twitter")
        assert result["message"] == "Test"
        assert result["hashtags"] == ["#a", "#b"]

    def test_parse_string_hashtags(self):
        raw = '{"message": "Test", "hashtags": "#a, #b, #c"}'
        result = content_generator.parse_llm_response(raw, "twitter")
        assert result["hashtags"] == ["#a", "#b", "#c"]

    def test_parse_plain_text_fallback(self):
        raw = "This is just plain text"
        result = content_generator.parse_llm_response(raw, "twitter")
        assert result["message"] == "This is just plain text"


class TestValidatePost:
    def test_twitter_within_limit(self):
        errors = content_generator.validate_post("twitter", {
            "message": "✨ Hello world #a #b #c",
            "hashtags": ["#a", "#b", "#c"],
        })
        # Should pass (no errors about char count or hashtag count)
        char_errors = [e for e in errors if "280" in e]
        assert len(char_errors) == 0

    def test_twitter_over_limit(self):
        long_msg = "✨ " + "x" * 300
        errors = content_generator.validate_post("twitter", {
            "message": long_msg,
            "hashtags": ["#a", "#b", "#c"],
        })
        assert any("280" in e for e in errors)

    def test_twitter_requires_three_hashtags(self):
        errors = content_generator.validate_post("twitter", {
            "message": "✨ Hello #a #b",
            "hashtags": ["#a", "#b"],
        })
        assert any("exactly 3 hashtags" in e for e in errors)

    def test_twitter_requires_emoji(self):
        errors = content_generator.validate_post("twitter", {
            "message": "Hello world #a #b #c",
            "hashtags": ["#a", "#b", "#c"],
        })
        assert any("emoji" in e for e in errors)

    def test_tiktok_over_limit(self):
        errors = content_generator.validate_post("tiktok", {
            "message": "x" * 200,
        })
        assert any("150" in e for e in errors)

    def test_valid_post_no_errors(self):
        errors = content_generator.validate_post("instagram", {
            "message": "Great post! #a #b",
        })
        assert errors == []


class TestBuildGenerationContext:
    def test_returns_system_and_user_prompts(self):
        profile = {
            "name": "Test Brand",
            "system_prompt": "You are a social media manager.",
            "writing_style_prompt": "Professional",
            "contact_details": "email@test.com",
            "default_cta": "Follow us!",
            "default_link": "https://example.com",
        }
        media_info = {"topic": "New product launch", "key_points": "Fast, reliable"}
        system_prompt, user_prompt = content_generator.build_generation_context(
            profile, media_info, "twitter"
        )
        assert "social media manager" in system_prompt
        assert "Test Brand" in user_prompt
        assert "New product launch" in user_prompt

    def test_fallback_for_unknown_platform(self):
        profile = {"name": "Brand"}
        system_prompt, user_prompt = content_generator.build_generation_context(
            profile, {}, "unknown_platform"
        )
        assert "unknown_platform" in user_prompt


class TestPreparedPostCRUD:
    def _create_campaign(self, tmp_db):
        """Helper: create a campaign row for FK reference."""
        import sqlite3
        with sqlite3.connect(tmp_db) as conn:
            cur = conn.execute(
                "INSERT INTO campaigns (profile_id, media_group_id, status) VALUES (1, 1, 'draft')"
            )
            conn.commit()
            return cur.lastrowid

    def test_create_and_get(self, tmp_db):
        cid = self._create_campaign(tmp_db)
        post = content_generator.create_prepared_post(
            campaign_id=cid,
            platform="twitter",
            message="Hello world",
            hashtags='["#test"]',
            char_count=11,
            db_path=tmp_db,
        )
        assert post.id > 0
        assert post.platform == "twitter"
        assert post.message == "Hello world"

        fetched = content_generator.get_prepared_post(post.id, db_path=tmp_db)
        assert fetched.id == post.id

    def test_list_by_campaign(self, tmp_db):
        cid = self._create_campaign(tmp_db)
        content_generator.create_prepared_post(
            campaign_id=cid, platform="twitter", message="t", db_path=tmp_db
        )
        content_generator.create_prepared_post(
            campaign_id=cid, platform="instagram", message="i", db_path=tmp_db
        )
        posts = content_generator.list_prepared_posts(campaign_id=cid, db_path=tmp_db)
        assert len(posts) == 2

    def test_update_post(self, tmp_db):
        cid = self._create_campaign(tmp_db)
        post = content_generator.create_prepared_post(
            campaign_id=cid, platform="twitter", message="old", db_path=tmp_db
        )
        updated = content_generator.update_prepared_post(
            post.id, message="new message", status="approved", db_path=tmp_db
        )
        assert updated.message == "new message"
        assert updated.status == "approved"

    def test_delete_post(self, tmp_db):
        cid = self._create_campaign(tmp_db)
        post = content_generator.create_prepared_post(
            campaign_id=cid, platform="twitter", message="del", db_path=tmp_db
        )
        content_generator.delete_prepared_post(post.id, db_path=tmp_db)
        with pytest.raises(ValueError, match="not found"):
            content_generator.get_prepared_post(post.id, db_path=tmp_db)

    def test_story_flag_roundtrip(self, tmp_db):
        cid = self._create_campaign(tmp_db)
        post = content_generator.create_prepared_post(
            campaign_id=cid, platform="instagram", story_flag=True, db_path=tmp_db
        )
        fetched = content_generator.get_prepared_post(post.id, db_path=tmp_db)
        assert fetched.story_flag is True


class TestCharacterCount:
    def test_count(self):
        # count_characters was removed as dead code; use len() directly
        assert len("hello") == 5
        assert len("") == 0


class TestSheetExcludedPlatforms:
    def test_excluded(self):
        assert "telegram" in content_generator.SHEET_EXCLUDED_PLATFORMS
        assert "patreon" in content_generator.SHEET_EXCLUDED_PLATFORMS
        assert "discord" in content_generator.SHEET_EXCLUDED_PLATFORMS

    def test_twitter_not_excluded(self):
        assert "twitter" not in content_generator.SHEET_EXCLUDED_PLATFORMS
