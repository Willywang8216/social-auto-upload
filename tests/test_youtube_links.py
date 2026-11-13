"""YouTube creator-only links, generation filtering and no-write publish guards."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from myUtils import content_generator, content_rules, prepared_publishers, profiles, publish_services, youtube_links
from myUtils import sociamonials_policy

LINKS = [
    "https://www.patreon.com/NakedWill",
    "https://www.instagram.com/itsmenakedwill",
    "https://www.facebook.com/1251025951420035",
    "https://www.threads.com/@itsmenakedwill",
]


@pytest.mark.parametrize("url", LINKS + [
    "https://threads.net/@creator", "threads.com/@creator", "instagram.com/creator",
    "https://www.facebook.com/profile.php?id=12345", "https://www.patreon.com/c/Creator",
])
def test_direct_allowed_provider_profiles(url):
    assert youtube_links.is_allowed_link(url)
    youtube_links.validate_text("Follow " + url)


@pytest.mark.parametrize("url", [
    "https://linktr.ee/nakedwill", "linktr.ee/nakedwill", "https://linktree.com/creator",
    "https://onlyfans.com/creator", "onlyfans.com/creator", "https://justfor.fans/creator",
    "https://justforfans.com/creator", "https://fansly.com/creator",
    "https://nakedwill.com", "nakedwill.com", "https://contacts.nakedwill.com",
    "https://other.example/nudity", "https://www.patreon.com/posts/nudity-123",
    "https://www.patreon.com/nudity", "https://www.patreon.com/nu%64ity",
    "https://www.instagram.com/nsfw-content", "https://www.instagram.com/p/xyz",
    "https://www.facebook.com/reel/123", "https://www.threads.com/@creator/post/xyz",
    "https://instagram.com.attacker.example/creator", "https://fakeinstagram.com/creator",
    "https://instagram.com@attacker.example/creator", "https://user:secret@instagram.com/creator",
    "https://instagram.com/creator?redirect=https://onlyfans.com/creator",
    "https://www.facebook.com/profile.php?id=123&next=https://onlyfans.com/creator",
    "https://www.facebook.com/l.php?u=https://fansly.com/creator",
    "https://www.instagram.com/creator#nsfw", "http://instagram.com/creator",
    "//instagram.com/creator", "will@nakedwill.com", "https://www.instagram.com:443/creator",
])
def test_disallowed_links_are_removed_and_rejected(url):
    assert not youtube_links.is_allowed_link(url)
    with pytest.raises(youtube_links.YouTubeLinkPolicyError):
        youtube_links.validate_text("Read this: " + url)
    cleaned = youtube_links.sanitize_text("Opening sentence.\nRead this: " + url + "\nClosing sentence.")
    youtube_links.validate_text(cleaned)
    assert url not in cleaned
    assert "Opening sentence." in cleaned and "Closing sentence." in cleaned


def test_markdown_bare_links_contact_labels_and_heading_preservation():
    text = "Opening.\n\nWhat I get into:\nA useful point.\n\n[More](https://linktr.ee/nakedwill)\nWebsite: nakedwill.com\nEmail: will@nakedwill.com\nTwitter / X: @model_will\nInstagram: @oldhandle\nContact: " + LINKS[0]
    cleaned = youtube_links.sanitize_text(text, allowed_links=LINKS)
    assert "What I get into:" in cleaned and "A useful point." in cleaned
    assert "[More](" not in cleaned
    assert "Website:" not in cleaned and "Email:" not in cleaned and "Twitter" not in cleaned
    assert "@oldhandle" not in cleaned and LINKS[0] in cleaned
    youtube_links.validate_text(cleaned, allowed_links=LINKS)


def test_profile_override_never_changes_general_brand_links():
    profile = profiles.Profile(1, "NW", "nw", settings={youtube_links.SETTING: LINKS},
                               contact_details="Linktree: https://linktr.ee/nakedwill", default_link="https://linktr.ee/nakedwill")
    before = copy.deepcopy(profile.to_dict())
    assert youtube_links.links_for_profile(profile) == LINKS
    assert profile.to_dict() == before
    with pytest.raises(youtube_links.YouTubeLinkPolicyError):
        youtube_links.links_for_profile({"settings": {youtube_links.SETTING: ["https://fansly.com/creator"]}})
    assert youtube_links.links_for_profile({"settings": {youtube_links.SETTING: []}, "default_link": LINKS[0]}) == []


def test_sanitize_all_draft_fields_is_pure_idempotent_and_creator_only():
    raw = {field: "Real content. https://linktr.ee/nakedwill" for field in youtube_links.TEXT_FIELDS}
    raw.update(message="The face has muscles. https://www.instagram.com/someoneelse", hashtags=["face", "https://fansly.com/creator"], charCount=10)
    before = copy.deepcopy(raw)
    clean = youtube_links.sanitize_draft(raw, allowed_links=LINKS, add_footer=True)
    assert raw == before
    assert clean == youtube_links.sanitize_draft(clean, allowed_links=LINKS, add_footer=True)
    assert clean["contactDetails"] == youtube_links.creator_footer(LINKS)
    assert set(youtube_links.links_in_text(clean["description"])) == set(LINKS)
    assert "someoneelse" not in clean["message"] and clean["hashtags"] == ["face"]
    assert clean["charCount"] == len(clean["message"].encode("utf-8"))


def test_recovery_does_not_silently_rewrite_approved_links():
    raw = {"message": "Original body", "description": "Original https://linktr.ee/nakedwill"}
    before = copy.deepcopy(raw)
    with pytest.raises(youtube_links.YouTubeLinkPolicyError):
        content_rules.prepare_platform_draft("youtube", raw, preserve_approved=True)
    assert raw == before
    clean = content_rules.prepare_platform_draft("youtube", raw)
    assert "linktr.ee" not in clean["description"]
    other = content_rules.prepare_platform_draft("facebook", raw)
    assert other["description"] == raw["description"]


def test_both_generation_prompt_paths_exclude_unsafe_profile_defaults():
    profile = profiles.Profile(1, "NW", "nw", settings={youtube_links.SETTING: LINKS, "systemPrompt": "Write warmly. Link to https://linktr.ee/nakedwill"},
                               default_link="https://linktr.ee/nakedwill", contact_details="https://contacts.nakedwill.com")
    group = SimpleNamespace(name="Morning light")
    prompts = publish_services._build_generation_prompt("youtube", profile, group,
        {"notes": "Discuss mornings; more at https://fansly.com/creator", "contactDetails": "https://linktr.ee/nakedwill"}, {})
    legacy = content_generator.build_generation_context(profile.to_dict(), {"topic": "Morning"}, "youtube", {})
    for system, user in (prompts, legacy):
        assert "YOUTUBE EXTERNAL-LINK POLICY" in user
        assert "https://linktr.ee" not in system + user and "https://fansly.com" not in system + user
        for link in LINKS:
            assert link in user
    assert profile.default_link == "https://linktr.ee/nakedwill"


def test_llm_and_humanizer_cannot_reintroduce_unsafe_links(monkeypatch):
    profile = profiles.Profile(1, "NW", "nw", settings={youtube_links.SETTING: LINKS,
        "aiServices": [{"apiBaseUrl": "https://llm.invalid", "apiKey": "fixture", "model": "fixture"}]})
    group = SimpleNamespace(name="Morning light")
    generate = Mock(return_value=SimpleNamespace(parsed_json={"message": "I watched the rain and waited for the light. https://fansly.com/creator",
        "description": "A quiet morning. https://linktr.ee/nakedwill"}, content=""))
    monkeypatch.setattr(publish_services.llm_client, "generate_chat_completion", generate)
    def humanize(draft, **kwargs):
        draft["description"] += " https://onlyfans.com/creator"
    monkeypatch.setattr(publish_services.humanize, "humanize_fields", humanize)
    account = profiles.Account(110, 1, "youtube", "YT", "")
    result = publish_services._generate_account_draft(account, profile, group, {}, {})
    youtube_links.validate_draft(result, allowed_links=LINKS)
    assert set(youtube_links.links_in_text(result["description"])) == set(LINKS)


@pytest.mark.parametrize("field", ["message", "description", "title", "firstComment", "contactDetails", "cta", "altText"])
def test_native_text_guard_is_read_only_for_every_surface(field):
    payload = {"draft": {"message": "Original body", "title": "Original title", field: "https://linktr.ee/nakedwill"}}
    before = copy.deepcopy(payload)
    with pytest.raises(prepared_publishers.PreparedPublishError) as raised:
        prepared_publishers.youtube_text_fields(payload)
    assert raised.value.retryable is False
    assert raised.value.details["stage"] == "external_link_policy"
    assert payload == before


def test_native_guard_runs_before_token_or_upload_http_calls():
    session = Mock()
    account = SimpleNamespace(config={"channelId": "fixture-channel", youtube_links.SETTING: LINKS})
    payload = {"draft": {"title": "Original", "message": "https://linktr.ee/nakedwill"}, "artifacts": []}
    with pytest.raises(prepared_publishers.PreparedPublishError, match="YouTube"):
        prepared_publishers.publish_youtube_sync(account, payload, session=session)
    assert not session.mock_calls
    with pytest.raises(prepared_publishers.PreparedPublishError):
        prepared_publishers.youtube_text_fields({"draft": {"message": "https://www.instagram.com/someoneelse"}}, allowed_links=LINKS)


def test_backup_overrides_cannot_bypass_youtube_link_policy():
    payload = {"draft": {"title": "Original"}, "networkOptions": {"yt": {"description": "https://fansly.com/creator"}}}
    with pytest.raises(sociamonials_policy.SociamonialsPolicyError, match="link policy"):
        sociamonials_policy.map_intent(payload, network="yt", message="Original description", media_kind="video", media_count=1)
    clean = {"draft": {"title": "Original", "description": youtube_links.creator_footer(LINKS)}}
    result = sociamonials_policy.map_intent(clean, network="yt", message="Original body", media_kind="video", media_count=1)
    assert result["network_options"]["description"] == clean["draft"]["description"]


def test_prospective_parser_filters_but_approved_validator_is_read_only():
    raw = json.dumps({"message": "Original", "description": "https://onlyfans.com/creator"})
    assert "onlyfans.com" not in content_generator.parse_llm_response(raw, "youtube")["description"]
    assert "onlyfans.com" in content_generator.parse_llm_response(raw, "facebook")["description"]
    approved = json.loads(raw)
    before = copy.deepcopy(approved)
    with pytest.raises(prepared_publishers.PreparedPublishError, match="YouTube"):
        content_generator.validate_post("youtube", approved)
    assert approved == before


def test_account_override_and_profile_fallback_are_shared_by_generation_and_publish(monkeypatch):
    profile = profiles.Profile(1, "NW", "nw", settings={youtube_links.SETTING: LINKS})
    account = profiles.Account(110, 1, "youtube", "Fixture YT", "", config={"channelId": "fixture-channel"})
    monkeypatch.setattr(profiles, "get_profile", lambda *args, **kwargs: profile)
    assert youtube_links.links_for_account(account) == LINKS
    session = Mock()
    with pytest.raises(prepared_publishers.PreparedPublishError):
        prepared_publishers.publish_youtube_sync(account, {"draft": {"message": "https://www.instagram.com/othercreator"}}, session=session)
    assert not session.mock_calls
    override = ["https://www.patreon.com/SecondBrand"]
    account.config[youtube_links.SETTING] = override
    assert youtube_links.links_for_account(account, profile=profile) == override
    _, prompt = content_generator.build_generation_context(profile.to_dict(), {}, "youtube", {"_youtubeAllowedLinks": override})
    assert override[0] in prompt and LINKS[0] not in prompt
    monkeypatch.setattr(publish_services, "_generate_platform_draft", Mock(return_value={"message": "A real morning at the window."}))
    monkeypatch.setattr(publish_services.humanize, "humanize_fields", Mock())
    result = publish_services._generate_account_draft(account, profile, SimpleNamespace(name="Morning"), {}, {})
    assert override[0] in result["message"] and LINKS[0] not in result["message"]
    assert publish_services._generate_platform_draft.call_args.args[3]["_youtubeAllowedLinks"] == override


@pytest.mark.parametrize("allowed_links", [LINKS, None])
def test_final_mapped_title_never_publishes_a_truncated_profile_url(allowed_links):
    payload = {"draft": {"message": "Original body", "title": "A" * 70 + " " + LINKS[1]}}
    with pytest.raises(prepared_publishers.PreparedPublishError, match="link"):
        prepared_publishers.youtube_text_fields(payload, allowed_links=allowed_links)


def test_backup_final_tags_are_subject_to_the_same_link_policy():
    for field in ("tags", "hashtags"):
        payload = {"draft": {"title": "Original", field: ["https://fansly.com/creator"]}}
        with pytest.raises(sociamonials_policy.SociamonialsPolicyError, match="link policy"):
            sociamonials_policy.map_intent(payload, network="yt", message="Original", media_kind="video", media_count=1)
    payload = {"draft": {"title": "Original"}, "networkOptions": {"yt": {"tags": ["https://fansly.com/creator"]}}}
    with pytest.raises(sociamonials_policy.SociamonialsPolicyError, match="link policy"):
        sociamonials_policy.map_intent(payload, network="yt", message="Original", media_kind="video", media_count=1)


def test_footer_has_reserved_utf8_budget_and_plain_headings_are_preserved():
    raw = {"message": "漢" * 1700, "description": "漢" * 1700, "charCount": 1}
    clean = youtube_links.sanitize_draft(raw, allowed_links=LINKS, add_footer=True)
    for field in ("message", "description"):
        assert len(clean[field].encode("utf-8")) <= 5000
        assert clean[field].endswith(youtube_links.creator_footer(LINKS))
    assert youtube_links.sanitize_text("X: independent variable\nLINE: a straight path\nEmail: preferences") == "X: independent variable\nLINE: a straight path\nEmail: preferences"


@pytest.mark.parametrize("watermark,expected", [
    ({"text": "linktr.ee/nakedwill"}, True),
    ({"text": "https://fansly.com/creator"}, True),
    ({"text": LINKS[0]}, False), ({"text": "NakedWill"}, False), ({}, False),
])
def test_youtube_omits_only_disallowed_link_watermarks(watermark, expected):
    from myUtils import video_artifacts
    assert video_artifacts.youtube_requires_link_free_media({"_appliedWatermarkSpec": watermark}) is expected


def test_youtube_media_uses_selected_preoverlay_content_while_x_keeps_watermark(tmp_path, monkeypatch):
    from myUtils import video_artifacts as artifacts
    source, marked = tmp_path / "selected-with-intro.mp4", tmp_path / "marked.mp4"
    source.write_bytes(b"selected-content-with-intro-and-outro")
    marked.write_bytes(b"same-content-with-linktree-overlay")
    calls = []
    def prepare(path, *args, **kwargs):
        calls.append((kwargs["platform"], path))
        return [SimpleNamespace(path=path, metadata={"sha256": "fixture-hash"})]
    monkeypatch.setattr(artifacts.media_compliance, "prepare_video", prepare)
    monkeypatch.setattr(artifacts.media_compliance, "validate_video", Mock())
    monkeypatch.setattr(artifacts.media_pipeline, "build_campaign_workspace", lambda *args: tmp_path)
    monkeypatch.setattr(artifacts, "_artifact_row", lambda part, **kwargs: {"local_path": str(part.path), "metadata": kwargs["metadata"]})
    before = source.read_bytes(), marked.read_bytes()
    result = artifacts.prepare_and_register_video(campaign_id=1, media_file={}, source_path=source, publish_path=marked,
        selected_platforms={"youtube", "twitter"}, request_data={"_appliedWatermarkSpec": {"text": "linktr.ee/nakedwill"}},
        upload_to_remote=False, db_path=tmp_path / "unused.db", register=False)
    assert dict(calls) == {"twitter": marked, "youtube": source}
    assert result["by_platform"]["youtube"][0]["metadata"]["unwatermarked"] is True
    assert result["by_platform"]["twitter"][0]["metadata"]["unwatermarked"] is False
    assert before == (source.read_bytes(), marked.read_bytes())


def test_youtube_cover_uses_preoverlay_image_while_x_keeps_watermark(tmp_path, monkeypatch):
    from myUtils import video_artifacts as artifacts
    source, marked = tmp_path / "selected.jpg", tmp_path / "marked.jpg"
    calls = []
    def prepare(paths, *args, **kwargs):
        calls.append((kwargs["platform"], paths))
        return [SimpleNamespace(path=path, metadata={"sha256": "fixture-hash", "prepared_for": [kwargs["platform"]]}) for path in paths]
    monkeypatch.setattr(artifacts.image_compliance, "prepare_images", prepare)
    monkeypatch.setattr(artifacts.media_pipeline, "build_campaign_workspace", lambda *args: tmp_path)
    monkeypatch.setattr(artifacts, "_artifact_row", lambda part, **kwargs: {"local_path": str(part.path), "metadata": kwargs["metadata"]})
    result = artifacts.prepare_and_register_images(campaign_id=1, images=[{"source_path": source, "publish_path": marked, "media_file": {}}],
        selected_platforms={"youtube", "twitter"}, request_data={"_appliedWatermarkSpec": {"text": "linktr.ee/nakedwill"}},
        upload_to_remote=False, db_path=tmp_path / "unused.db", register=False, video_present=True)
    assert dict(calls) == {"twitter": [marked], "youtube": [source]}
    assert result["by_platform"]["youtube"][0]["metadata"]["unwatermarked"] is True
