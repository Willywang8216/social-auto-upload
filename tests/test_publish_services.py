"""Shared callback regressions without importing the web application."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from alembic import command
from alembic.config import Config

from db import createTable as create_table
from myUtils import campaign_media_prep, campaigns, jobs, media_groups, profiles
from myUtils import publish_services as services


@pytest.fixture
def profile():
    return profiles.Profile(1, "Brand", "brand", settings={"systemPrompt": "Write as the creator."})


@pytest.fixture
def media_group():
    return media_groups.MediaGroup(1, "Launch", "")


@pytest.fixture(scope="module")
def schema_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("services-schema") / "schema.db"
    # Use the real bootstrap and migrations with in-memory configuration:
    # Windows' cp950 locale cannot decode this repository's UTF-8 ini file.
    with patch.object(create_table, "_stamp_alembic_head"):
        create_table.bootstrap(path)
    url = f"sqlite:///{path.resolve()}"
    cfg = Config(cmd_opts=SimpleNamespace(x=[f"url={url}"]))
    cfg.set_main_option("script_location", str(create_table._PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.stamp(cfg, create_table.RAW_SCHEMA_REVISION)
    command.upgrade(cfg, "head")
    return path


@pytest.fixture
def db_path(tmp_path, schema_db):
    path = tmp_path / "services.db"
    shutil.copyfile(schema_db, path)
    return path


def _video(artifact_id, *, metadata=None, **overrides):
    return dict(id=artifact_id, source_file_record_id=7, artifact_kind="remote_upload",
                local_path=f"/media/v{artifact_id}.mp4", public_url=f"https://cdn.example/v{artifact_id}.mp4",
                metadata=metadata or {"role": "video"}) | overrides


def _part(artifact_id, index, *, seconds=200.083, count=3, **overrides):
    return _video(artifact_id, metadata={"role": "video", "part_index": index,
                                       "part_count": count, "max_duration_seconds": seconds,
                                       "split_for": ["threads"]}, **overrides)


def test_selector_preserves_part_rounding_and_prefers_one_public_copy():
    items = [_video(1), _part(10, 1, public_url=None), _part(11, 2),
             _part(12, 3, seconds=200.048), _part(20, 1), _part(21, 2)]
    selected = services._select_videos_for_platform(items, "threads")
    assert [a["id"] for a in selected] == [20, 11, 12]
    assert [a["id"] for a in services._select_videos_for_platform(items, "youtube")] == [1]
    assert services._select_videos_for_platform([], "threads") == []
    assert services._select_videos_for_platform(items, "unknown") == [items[0]]
    groups = services._artifact_part_groups_for_platform(items, "threads")
    assert [[a["id"] for a in group] for group in groups] == [[20], [11], [12]]


def test_selector_largest_variant_and_full_public_url():
    items = [_video(1, public_url=None), _video(2),
             _video(3, metadata={"role": "video", "max_duration_seconds": 600}),
             _video(4, metadata={"role": "video", "max_duration_seconds": 140})]
    assert services._select_videos_for_platform(items, "instagram") == [items[2]]
    assert services._select_videos_for_platform(items, "twitter") == [items[3]]
    assert services._select_videos_for_platform(items[:2], "reddit") == [items[1]]


def test_context_artifacts_are_never_published_and_tiktok_keeps_raw():
    artifacts = [
        {"id": 1, "artifact_kind": "audio", "local_path": "/media/audio.wav"},
        {"id": 2, "artifact_kind": "transcript", "local_path": "/media/transcript.txt"},
        _video(3), _video(4, artifact_kind="raw_remote_upload"),
        _video(5, artifact_kind="watermarked_video"),
    ]
    for platform in ("telegram", "bluesky", "twitter", "tiktok", "nw_sw_blog"):
        selected = services._artifact_payloads_for_platform(artifacts, platform)
        assert all(a["artifact_kind"] not in {"audio", "transcript"} for a in selected)
        assert selected
        expected = 4 if platform == "tiktok" else (3 if services.platform_limits.video_max_seconds(platform) is None else 5)
        assert [a["id"] for a in selected] == [expected]


def test_multiple_sources_remain_one_post_group_and_images_pass_through():
    image = {"id": 99, "artifact_kind": "watermarked_image", "metadata": {"role": "image"}}
    artifacts = [_part(10, 1), _part(11, 2), _part(12, 3),
                 _video(80, source_file_record_id=8), image]
    groups = services._artifact_part_groups_for_platform(artifacts, "threads")
    assert len(groups) == 1
    assert {a["id"] for a in groups[0]} == {10, 11, 12, 80, 99}


# Golden hashes were checked against the original backend functions before
# extraction. They lock the entire prompts, not just a few policy keywords.
@pytest.mark.parametrize("platform,language,digest", [
    ("telegram", "", "bbf44bb55dc4c9dbe10e3f6cecd7ac1734c37337014f0d584517a19d09e7221d"),
    ("threads", "zh-TW", "13d20243cee53e40f9ec8040886c596909f5693d65f5711d592b0e069ebdb155"),
    ("nw_sw_blog", "en,zh-TW", "d571816deb981f3379016e37c3198d2cce66884d4738212ab0ff1e78f9369415"),
    ("twitter", "en", "8171d4625b4fbec4326991476bf195b161ecdfc51bc0a6d42226f276726aaa86"),
])
def test_existing_prompts_are_unchanged(profile, media_group, platform, language, digest):
    request = {"title": "Morning light", "notes": "Describe the moment honestly.",
               "contactDetails": "@creator", "cta": "Join me.",
               "_accountContext": "Account name: Creator", "_accountLanguage": language}
    context = {"transcriptText": "We watched the rain by the window.", "imageLocalPaths": ["frame.jpg"]}
    result = services._build_generation_prompt(platform, profile, media_group, request, context)
    assert hashlib.sha256(json.dumps(result, ensure_ascii=False).encode("utf-8")).hexdigest() == digest


def test_fallback_refuses_brief_labels_and_source_language(media_group):
    with pytest.raises(RuntimeError, match="brief is an instruction"):
        services._fallback_generated_draft("telegram", media_group,
                                           {"notes": "Write a thoughtful caption about the afternoon."}, {})
    with pytest.raises(RuntimeError, match="Account-language content generation"):
        services._fallback_generated_draft("telegram", media_group,
                                           {"_accountLanguage": "zh-TW"},
                                           {"transcriptText": "We watched the rain by the window."})
    draft = services._fallback_generated_draft("telegram", media_group,
                                              {"title": "I finally took the long way home.",
                                               "hashtags": ["walk"], "firstComment": "  Read more  "}, {})
    assert draft == {"message": "I finally took the long way home.", "hashtags": ["walk"], "firstComment": "Read more"}


def _ai_profile(profile):
    profile.settings["aiServices"] = [{"apiBaseUrl": "https://llm.invalid", "apiKey": "test-key", "model": "test-model"}]
    return profile


def test_platform_draft_uses_mock_llm_and_existing_language_guards(profile, media_group, monkeypatch, tmp_path):
    _ai_profile(profile)
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"mock image")
    message = "I waited by the window until the rain softened enough to walk home."
    generate = Mock(return_value=SimpleNamespace(parsed_json={"message": message}, content=""))
    monkeypatch.setattr(services.llm_client, "generate_chat_completion", generate)
    draft = services._generate_platform_draft("telegram", profile, media_group,
                                              {"_accountLanguage": "en"},
                                              {"imageLocalPaths": [str(image)]})
    assert draft["message"] == message
    assert generate.call_args.kwargs == {"api_base_url": "https://llm.invalid", "api_key": "test-key",
                                         "model": "test-model", "images": [str(image)]}
    with pytest.raises(RuntimeError, match="does not match the account language"):
        services._generate_platform_draft("telegram", profile, media_group, {"_accountLanguage": "zh-TW"}, {})
    with pytest.raises(ValueError, match="one audience language"):
        services._generate_platform_draft("twitter", profile, media_group, {"_accountLanguage": "en,zh-TW"}, {})
    with pytest.raises(RuntimeError, match="LLM is required"):
        services._generate_platform_draft("telegram", profile, media_group,
                                          {"_accountLanguage": "en", "useLlm": False}, {})


def test_llm_failure_cannot_publish_wrong_language_or_short_blog(profile, media_group, monkeypatch):
    _ai_profile(profile)
    generate = Mock(side_effect=RuntimeError("mock provider unavailable"))
    monkeypatch.setattr(services.llm_client, "generate_chat_completion", generate)
    with pytest.raises(RuntimeError, match="source-language fallback"):
        services._generate_platform_draft("telegram", profile, media_group, {"_accountLanguage": "zh-TW"},
                                          {"transcriptText": "I waited at the window until the rain stopped."})
    generate.side_effect = None
    generate.return_value = SimpleNamespace(parsed_json={"message": "I waited at the window until the rain stopped."}, content="")
    with pytest.raises(RuntimeError, match="blog article is too short"):
        services._generate_platform_draft("nw_sw_blog", profile, media_group, {}, {})


def test_account_context_language_nonce_and_humanizer_are_preserved(profile, media_group, monkeypatch):
    profile.settings["default_language"] = "en"
    account = profiles.Account(3, profile.id, "telegram", "Creator TW", "", config={"audienceLanguage": "zh-TW"})
    request = {"notes": "Original brief"}
    draft = {"message": "今天坐在窗邊聽雨，等天色亮一點再出門。"}
    generate = Mock(return_value=draft)
    humanize = Mock()
    monkeypatch.setattr(services, "_generate_platform_draft", generate)
    monkeypatch.setattr(services.humanize, "humanize_fields", humanize)
    monkeypatch.setattr(services.uuid, "uuid4", lambda: SimpleNamespace(hex="12345678abcdef"))
    assert services._generate_account_draft(account, profile, media_group, request, {}, regenerate=True) is draft
    extended = generate.call_args.args[3]
    assert request == {"notes": "Original brief"}
    assert extended["notes"] == "Original brief"
    assert extended["_accountLanguage"] == "zh-TW"
    assert "Account name: Creator TW" in extended["_accountContext"]
    assert extended["_accountContext"].endswith("Regeneration nonce: 12345678")
    humanize.assert_called_once_with(draft, language="zh-TW")
    account.config = {}
    assert services._account_audience_language(account, profile) == "en"
    humanize.side_effect = RuntimeError("humanizer unavailable")
    assert services._generate_account_draft(account, profile, media_group, request, {}) is draft


@pytest.mark.parametrize("origin", ["https://configured.example", ""])
def test_prepare_wrapper_uses_only_configured_origin(profile, db_path, monkeypatch, origin):
    monkeypatch.setenv("SAU_PUBLIC_APP_URL", "")
    monkeypatch.setenv("SAU_PUBLIC_BASE_URL", origin)
    prepare = Mock(return_value={"transcriptText": "prepared"})
    monkeypatch.setattr(campaign_media_prep, "prepare_campaign_media_artifacts", prepare)
    files, request = [{"file_record_id": 1}], {"watermark": ""}
    assert services._prepare_campaign_media_artifacts(12, profile, files, request,
                                                       selected_platforms={"telegram"}, db_path=db_path) == {"transcriptText": "prepared"}
    prepare.assert_called_once_with(12, profile, files, request, selected_platforms={"telegram"},
                                    db_path=db_path, public_base_url=origin or None)


def test_file_registration_uses_supplied_db_safe_roots_and_remote_lookup(db_path, tmp_path, monkeypatch):
    (tmp_path / "videoFile").mkdir()
    (tmp_path / "videoFile" / "clip_sfw.mp4").write_bytes(b"video")
    monkeypatch.setattr(campaign_media_prep, "BASE_DIR", tmp_path)
    fid = services._ensure_file_record_for_path("videoFile/clip_sfw.mp4", db_path=db_path)
    assert services._ensure_file_record_for_path("videoFile/clip_sfw.mp4", db_path=db_path) == fid
    with closing(sqlite3.connect(db_path)) as conn, conn:
        row = conn.execute("SELECT filename, filesize, file_path FROM file_records WHERE id = ?", (fid,)).fetchone()
        assert row == ("clip_sfw.mp4", 0.0, "videoFile/clip_sfw.mp4")
        remote_id = conn.execute("INSERT INTO file_records (filename, file_path, storage_cdn_url) "
                                 "VALUES ('remote_sfw.mp4', 'videoFile/_offloaded/remote_sfw.mp4', "
                                 "'https://cdn.example/remote_sfw.mp4')").lastrowid
    assert services._ensure_file_record_for_path("videoFile/_offloaded/remote_sfw.mp4", db_path=db_path) == remote_id
    with pytest.raises(ValueError, match="Unsafe media path"):
        services._ensure_file_record_for_path("../outside_sfw.mp4", db_path=db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM file_records").fetchone()[0] == 2


def test_job_payload_keeps_existing_public_shape():
    job = jobs.Job(4, "intent", 2, "telegram", {"draft": {"message": "A walk by the river."}},
                   "pending", 3, 1, 0, "2030-01-01", None, None)
    assert services._job_to_payload(job) == {
        "id": 4, "idempotencyKey": "intent", "platform": "telegram", "profileId": 2,
        "status": "pending", "totalTargets": 3, "completedTargets": 1, "failedTargets": 0,
        "createdAt": "2030-01-01", "startedAt": None, "finishedAt": None,
        "payload": job.payload, "title": "A walk by the river.",
    }


def test_review_cards_resolve_names_from_each_supplied_db_and_notify_async(tmp_path, schema_db, monkeypatch):
    from myUtils import tg_review
    notify = Mock()
    monkeypatch.setattr(tg_review, "notify_async", notify)
    monkeypatch.setenv("SAU_PUBLIC_APP_URL", "https://review.example")
    for label in ("First DB", "Second DB"):
        path = tmp_path / f"{label}.db"
        shutil.copyfile(schema_db, path)
        profile = profiles.create_profile(label, db_path=path)
        account = profiles.add_account(profile.id, "telegram", "shared", auth_type="api", nickname=label, db_path=path)
        group = media_groups.create_media_group("Review", db_path=path)
        campaign = campaigns.create_campaign(profile.id, group.id, db_path=path)
        payload = {"campaignId": campaign.id, "campaignPostId": 7, "draft": {"message": "Actual saved copy"},
                   "artifacts": [{"local_path": "/media/clip_pub.mp4", "public_url": "https://cdn.example/clip.mp4"}]}
        job = jobs.enqueue_job(jobs.JobSpec(platform="telegram", payload=payload, profile_id=profile.id,
                                            targets=[(f"account:{account.id}", "campaign_post:7", "2030-01-01T00:00:00")]), db_path=path)
        cards = services._tg_review_cards(jobs=[{}, {"id": -1}, {"id": job.id, "payload": {"message": "stale"}}], db_path=path)
        assert len(cards) == 1
        card = cards[0]
        assert card["account_labels"] == [f"telegram — {label}"]
        assert card["profile_label"] == f"{label} (#{profile.id})"
        assert card["copy_text"] == "Actual saved copy"
        assert card["schedule_label"] == "2030-01-01T00:00:00 UTC"
        assert card["media_label"] == "clip_pub.mp4"
        assert card["app_url"] == f"https://review.example/#/publish/queue?entity=mg-{group.id}"
        services._notify_tg_review(jobs=[{"id": job.id}], db_path=path)
        notify.assert_called_with(cards=cards, db_path=path)
        assert services._account_display_name("account:missing", db_path=path) == "account:missing"
        assert services._account_display_name("legacy-cookie.json", db_path=path) == "legacy-cookie.json"
    notify.reset_mock()
    services._notify_tg_review(jobs=[], db_path=path)
    notify.assert_not_called()
    notify.side_effect = RuntimeError("Telegram unavailable")
    services._notify_tg_review(jobs=[{"id": job.id}], db_path=path)  # Fail-soft.


def test_prepared_full_video_and_its_public_alias_beat_stale_raw_full():
    stale = _video(5501, artifact_kind="local", local_path="/media/extensionless-old-source")
    prepared = _video(5542, artifact_kind="watermarked_video", local_path="/media/prepared_pub.mp4", public_url=None)
    served = _video(5545, artifact_kind="local", local_path=prepared["local_path"], public_url="https://cdn.example/prepared.mp4")
    assert services._select_videos_for_platform([stale, prepared, served], "instagram") == [served]
    assert services._select_videos_for_platform([stale, prepared], "instagram") == [prepared]


def test_video_variant_must_fit_recorded_size_as_well_as_duration():
    oversized = _video(1, metadata={"role": "video", "max_duration_seconds": 600, "max_media_mb": 450})
    fitting = _video(2, metadata={"role": "video", "max_duration_seconds": 500, "max_media_mb": 200})
    assert services._select_videos_for_platform([oversized, fitting], "instagram") == [fitting]


def test_prepared_full_never_displaces_existing_platform_split():
    prepared = _video(1, artifact_kind="watermarked_video")
    parts = [_part(2, 1), _part(3, 2), _part(4, 3)]
    assert services._select_videos_for_platform([prepared, *parts], "threads") == parts
