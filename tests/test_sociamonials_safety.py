"""Fail-closed backup safety, exact receipt identity and finite restart behavior."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import Mock

import pytest
from PIL import Image

from myUtils import delivery, publish_media_guard
from myUtils import sociamonials_fallback as sm
from myUtils import sociamonials_policy as policy
from tests.test_sociamonials_fallback import (
    Account, ContractSession, Response, asset_for, image, isolate, media_payload,
    profile, publish, storage, video,
)

_REAL_RESOLVER = sm._resolve_public_host


def assert_no_publication(session):
    assert not session.calls_for("POST", "/media/uploads")
    assert not session.calls_for("PUT")
    assert not session.calls_for("POST", "/posts", create=True)


@pytest.mark.parametrize("changes", [
    {"connected": False}, {"connected": "true"}, {"connected": 1}, {"connected": None},
    {"agent_can_publish_to": False}, {"agent_can_publish_to": "true"}, {"agent_can_publish_to": 1},
    {"unavailable_reason": "not_publishable_via_api"}, {"capabilities": []},
    {"capabilities": ["text", "video", "mobile_app_handoff"]}, {"mobile_app_handoff": True},
])
def test_live_profile_denial_is_never_bypassed_by_verify_media_false(video, changes):
    session = ContractSession(profiles=[profile(**changes)])
    with pytest.raises(sm.SociamonialsNotConfigured):
        publish(session, payload=media_payload(video), verify_media=False)
    assert_no_publication(session)


def test_missing_unavailable_reason_is_not_null_permission(video):
    row = profile()
    row.pop("unavailable_reason")
    session = ContractSession(profiles=[row])
    with pytest.raises(sm.SociamonialsNotConfigured):
        publish(session, payload=media_payload(video), verify_media=False)
    assert_no_publication(session)


@pytest.mark.parametrize("rows", [[profile(ref="other", name="will_sexual")], [profile(network="fb")], [profile(), profile()]])
def test_same_name_wrong_network_or_duplicate_ref_is_not_a_verified_mapping(rows):
    session = ContractSession(profiles=rows)
    with pytest.raises(sm.SociamonialsNotConfigured):
        publish(session)
    assert_no_publication(session)


@pytest.mark.parametrize("permission", ["posts.create", "posts.read", "profiles.read"])
def test_missing_exact_workspace_permission_blocks_all_writes(permission):
    session = ContractSession()
    session.permissions.pop(permission)
    with pytest.raises(sm.SociamonialsNotConfigured):
        publish(session)
    assert_no_publication(session)


class SparseIdentitySession(ContractSession):
    """The live identity summary can omit assignments; profile flags decide."""
    summary = []

    def _wrap(self, body):
        if "agent" in body and "workspaces" in body:
            for entry in body["workspaces"].values():
                if self.summary == "omitted":
                    entry.pop("social_profiles", None)
                else:
                    entry["social_profiles"] = copy.deepcopy(self.summary)
        return super()._wrap(body)


@pytest.mark.parametrize("summary", [[], None, {}, "omitted"])
def test_sparse_identity_summary_needs_fresh_exact_profile_authorization(summary):
    session = SparseIdentitySession(profiles=[profile()])
    session.summary = summary
    result = sm.verify_destination(platform="twitter", account=Account(),
        payload={"message": "Approved original"}, session=session, api_key="fixture-only")
    assert result["verified"] is True and result["mapping"]["profile_refs"] == ["14100"]
    assert len(session.calls_for("GET", "/social-profiles")) == 1
    assert_no_publication(session)


@pytest.mark.parametrize("rows", [[], [profile(ref="other")], [profile(agent_can_publish_to=False)],
    [profile(connected=False)], [profile(), profile()]])
def test_empty_identity_summary_is_never_a_wildcard_grant(rows):
    session = SparseIdentitySession(profiles=rows)
    with pytest.raises(sm.SociamonialsNotConfigured):
        publish(session)
    assert len(session.calls_for("GET", "/social-profiles")) == 1
    assert_no_publication(session)


@pytest.mark.parametrize("permission", ["posts.create", "posts.read", "profiles.read"])
def test_exact_profile_true_cannot_override_missing_workspace_permission(permission):
    session = SparseIdentitySession(profiles=[profile()])
    session.permissions.pop(permission)
    with pytest.raises(sm.SociamonialsNotConfigured):
        publish(session)
    assert not session.calls_for("GET", "/social-profiles")
    assert_no_publication(session)


@pytest.mark.parametrize("platform,account_id,network,kind", [
    ("tiktok", 109, "tiktok", "video"), ("instagram", 72, "in", "image")])
def test_media_caption_does_not_require_standalone_text_capability(video, image, platform, account_id, network, kind):
    source = video if kind == "video" else image
    payload = media_payload(source, message="Approved caption")
    session = SparseIdentitySession(profiles=[profile(network,
        sm.SAU_ACCOUNT_TO_SOCIAMONIALS[account_id]["profile_refs"][0], capabilities=[kind, "title"])])
    result = sm.verify_destination(platform=platform, account=Account(account_id, platform),
        payload=payload, session=session, api_key="fixture-only")
    assert result["verified"] is True and payload["draft"]["message"] == "Approved caption"
    assert_no_publication(session)


def test_media_only_profile_still_cannot_authorize_a_standalone_text_post():
    session = SparseIdentitySession(profiles=[profile(capabilities=["video"])])
    with pytest.raises(sm.SociamonialsNotConfigured, match="capabilities"):
        publish(session)
    assert_no_publication(session)


def test_caption_support_does_not_remove_explicit_required_capabilities(video):
    session = SparseIdentitySession(profiles=[profile(capabilities=["video"])])
    with pytest.raises(sm.SociamonialsNotConfigured, match="capabilities"):
        sm.verify_destination(platform="twitter", account=Account(), payload=media_payload(video),
            required_capabilities=["text"], session=session, api_key="fixture-only")
    assert_no_publication(session)


def test_current_explicit_facebook_alias_is_verified_without_changing_numeric_maps():
    mapped = {"sociamonials": {"network": "fb", "profile_refs": ["30186|255814"]}}
    session = ContractSession(profiles=[profile("fb", "30186|255814")])
    result = publish(session, platform="facebook", account=Account(11, "facebook", mapped))
    assert result["profile_refs"] == ["30186|255814"]
    assert sm.SAU_ACCOUNT_TO_SOCIAMONIALS[11]["profile_refs"] == ["19784|175913"]
    assert sm.resolve_mapping(64)["profile_refs"] == result["profile_refs"]


def test_explicit_missing_key_cannot_fall_back_to_default_or_home(monkeypatch):
    monkeypatch.setenv(sm.API_KEY_ENV, "default-fixture")
    monkeypatch.setattr(Path, "home", lambda: (_ for _ in ()).throw(AssertionError("HOME secrets forbidden")))
    assert sm.get_api_key({}) == ""
    assert sm.resolve_credentials({"api_key_env": "MISSING_BACKUP_KEY", "workspace_id": "26985"}) == ("", "26985")
    session = ContractSession()
    with pytest.raises(sm.SociamonialsNotConfigured):
        publish(session, account=Account(config={"sociamonials": {"network": "tw", "profile_ref": "14100", "api_key_env": "MISSING_BACKUP_KEY"}}), api_key=None)
    assert session.calls == []
    monkeypatch.delenv(sm.API_KEY_ENV)
    assert sm.get_api_key() == ""


def test_tests_without_injected_session_are_never_live():
    with pytest.raises(sm.SociamonialsNotConfigured, match="Tests must inject"):
        publish(None)


@pytest.mark.parametrize("key_env,ws", [(sm.API_KEY_ENV, "34293"), (sm.TEACHING_API_KEY_ENV, "26985")])
def test_teaching_credentials_and_workspace_cannot_be_crossed(monkeypatch, key_env, ws):
    monkeypatch.setenv(sm.API_KEY_ENV, "adult")
    monkeypatch.setenv(sm.TEACHING_API_KEY_ENV, "teaching")
    with pytest.raises(sm.SociamonialsNotConfigured):
        sm.resolve_credentials({"api_key_env": key_env, "workspace_id": ws})


@pytest.mark.parametrize("mutation", ["valid-string", "missing-dry-run", "preset", "wrong-ref", "extra-ref", "warnings", "wrong-mode"])
def test_strict_no_create_validation_must_prove_original_exact_selection(image, mutation):
    def override(method, url, kw):
        if method == "POST" and url == sm.POSTS_URL and kw["json"].get("validate_only"):
            body = kw["json"]
            result = {"valid": True, "validate_only": True, "mode": "publish_now", "requires_approval": False,
                      "profile_resolution": {"mode": "explicit", "preset": None, "resolved_profiles": [{"network": "in", "profile_ref": "52817"}], "warnings": []},
                      "warnings": []}
            if mutation == "valid-string": result["valid"] = "true"
            if mutation == "missing-dry-run": result.pop("validate_only")
            if mutation == "preset": result["profile_resolution"]["mode"] = "post_preset"
            if mutation == "wrong-ref": result["profile_resolution"]["resolved_profiles"][0]["profile_ref"] = "other"
            if mutation == "extra-ref": result["profile_resolution"]["resolved_profiles"].append({"network": "in", "profile_ref": "other"})
            if mutation == "warnings": result["warnings"] = ["setting ignored; customer caption must not be logged"]
            if mutation == "wrong-mode": result["mode"] = "draft"
            assert body["message"] == "Approved original" and body["strict"] is True
            return Response(result)
    session = ContractSession(override=override)
    with pytest.raises(sm.SociamonialsFallbackError, match="Strict no-create"):
        publish(session, platform="instagram", account=Account(72, "instagram"), payload=media_payload(image), verify_media=False)
    assert_no_publication(session)


def test_x_original_links_are_validated_not_moved_and_enterprise_may_allow_them():
    original = "Original link https://example.org/article"
    rejected = ContractSession(override=lambda method, url, kw: Response({"error": {"code": "x_link_not_enabled"}}, 422)
        if method == "POST" and url == sm.POSTS_URL and kw["json"].get("validate_only") else None)
    with pytest.raises(sm.SociamonialsFallbackError):
        publish(rejected, payload={"message": original})
    assert_no_publication(rejected)
    assert rejected.calls_for("POST", "/posts")[0][2]["json"]["message"] == original
    enterprise = ContractSession()
    publish(enterprise, payload={"message": original})
    assert enterprise.last_body["message"] == original and "first_comment" not in enterprise.last_body


@pytest.mark.parametrize("network,maximum", list(policy.NETWORK_MAX_MESSAGE_CHARS.items()))
def test_backup_caption_ceiling_is_separate_from_native_and_never_truncates(network, maximum):
    assert policy.validate_text("x" * maximum, network) == "x" * maximum
    with pytest.raises(policy.SociamonialsPolicyError):
        policy.validate_text("x" * (maximum + 1), network)


def test_unicode_counts_utf16_graphemes_x_weights_and_youtube_bytes():
    assert policy.validate_text("\U0001f600" * 75, "tiktok") == "\U0001f600" * 75
    with pytest.raises(policy.SociamonialsPolicyError): policy.validate_text("\U0001f600" * 76, "tiktok")
    assert policy.validate_text("e\u0301" * 300, "blsk") == "e\u0301" * 300
    assert policy.validate_text("\u4f60" * 140, "tw") == "\u4f60" * 140
    with pytest.raises(policy.SociamonialsPolicyError): policy.validate_text("\u4f60" * 141, "tw")
    assert policy.validate_text("\u4f60" * 1666 + "ab", "yt") == "\u4f60" * 1666 + "ab"
    with pytest.raises(policy.SociamonialsPolicyError): policy.validate_text("\u4f60" * 1667, "yt")
    with pytest.raises(policy.SociamonialsPolicyError): policy.validate_text("\ud800", "fb")


@pytest.mark.parametrize("field,value", [("reply_control", "mentioned"), ("post_preset", "default"), ("privacy", "friends"), ("watermark", "default"), ("unknown_setting", True)])
def test_unsupported_generic_intent_blocks_before_upload(video, field, value):
    session = ContractSession()
    payload = media_payload(video)
    payload["networkOptions"] = {"tw": {field: value}}
    with pytest.raises(sm.SociamonialsFallbackError): publish(session, payload=payload)
    assert_no_publication(session)


@pytest.mark.parametrize("platform,account_id", [("twitter", 77), ("youtube", 110), ("tiktok", 109), ("pinterest", 999)])
def test_undocumented_first_comment_is_not_invented(video, platform, account_id):
    config = {"sociamonials": {"network": "pi", "profile_ref": "board-fixture"}, "boardId": "original-board"} if platform == "pinterest" else {}
    payload = media_payload(video)
    payload["draft"]["firstComment"] = "Original first comment"
    session = ContractSession()
    with pytest.raises(sm.SociamonialsFallbackError, match="first_comment"):
        publish(session, platform=platform, account=Account(account_id, platform, config), payload=payload)
    assert_no_publication(session)


@pytest.mark.parametrize("privacy,visibility", [("PUBLIC_TO_EVERYONE", "public"), ("SELF_ONLY", "private"), ("MUTUAL_FOLLOW_FRIENDS", "friends"), ("FOLLOWER_OF_CREATOR", "followers")])
def test_tiktok_original_privacy_interactions_and_ai_options_survive(video, privacy, visibility):
    payload = media_payload(video, message="Original TikTok caption")
    payload["tiktokDirectPost"] = True
    payload["tiktokPostSettings"] = {"privacyLevel": privacy, "disableComment": True, "disableDuet": False,
                                     "disableStitch": True, "aiGeneratedContent": True, "contentDisclosure": {"enabled": False}}
    session = ContractSession()
    publish(session, platform="tiktok", account=Account(109, "tiktok"), payload=payload)
    options = session.last_body["networks"]["tiktok"]
    assert options["visibility"] == visibility
    assert options["allow_comments"] is False and options["allow_duet"] is True and options["allow_stitch"] is False
    assert options["ai_generated_content"] is True and options["promotes_your_brand"] is False
    assert session.last_body["message"] == "Original TikTok caption"


def test_tiktok_frontend_disclosures_and_per_post_direct_override(video):
    payload = media_payload(video)
    payload["tiktokDirectPost"] = True
    payload["tiktokPostSettings"] = {"title": "Reviewed original", "privacyLevel": "PUBLIC_TO_EVERYONE", "allowComment": False,
        "allowDuet": False, "allowStitch": False, "contentDisclosureEnabled": True, "yourBrand": True,
        "brandedContent": True, "consentChecked": True}
    session = ContractSession()
    publish(session, platform="tiktok", account=Account(109, "tiktok", {"publishMode": "draft", "privacyLevel": "SELF_ONLY"}), payload=payload)
    options = session.last_body["networks"]["tiktok"]
    assert session.last_body["message"] == "Reviewed original" and options["visibility"] == "public"
    assert options["promotes_your_brand"] and options["branded_content"] and not options["allow_comments"]


@pytest.mark.parametrize("intent", [{"tiktokDirectPost": False}, {"networkOptions": {"tiktok": {"publishMode": "MEDIA_UPLOAD"}}},
    {"tiktokPostSettings": {"allowDuet": "false"}}, {"tiktokPostSettings": {"visibility": None}},
    {"tiktokPostSettings": {"disableDuet": True, "allowDuet": True}}])
def test_tiktok_inbox_null_or_conflicting_settings_are_not_changed_to_public_direct(video, intent):
    session = ContractSession()
    payload = {**media_payload(video), **intent}
    with pytest.raises(sm.SociamonialsFallbackError): publish(session, platform="tiktok", account=Account(109, "tiktok"), payload=payload)
    assert_no_publication(session)


def test_creator_restrictions_are_not_silently_flipped(video):
    session = ContractSession()
    config = {"creatorInfo": {"data": {"duet_disabled": True}}}
    with pytest.raises(sm.SociamonialsFallbackError):
        publish(session, platform="tiktok", account=Account(109, "tiktok", config), payload=media_payload(video))
    assert_no_publication(session)


def test_youtube_title_description_tags_and_flags_are_preserved(video):
    session = ContractSession()
    payload = media_payload(video, message="Original description")
    payload["draft"].update(title="T" * 100, tags=["original", "two words"])
    payload["networkOptions"] = {"yt": {"visibility": "unlisted", "notifySubscribers": False,
        "madeForKids": True, "containsSyntheticMedia": True, "shorts": False}}
    publish(session, platform="youtube", account=Account(110, "youtube", {"privacyStatus": "private"}), payload=payload)
    options = session.last_body["networks"]["yt"]
    assert options["title"] == "T" * 100 and options["description"] == "Original description"
    assert options["tags"] == ["original", "two words"] and options["visibility"] == "unlisted"
    assert options["notify_subscribers"] is False and options["made_for_kids"] is True and options["synthetic_media"] is True


@pytest.mark.parametrize("intent", [{"draft": {"message": "Original", "title": "T" * 101}}, {"networkOptions": {"yt": {"license": "creativeCommon"}}}])
def test_youtube_incompatible_title_or_settings_stop_before_upload(video, intent):
    session = ContractSession()
    payload = {**media_payload(video), **intent}
    with pytest.raises(sm.SociamonialsFallbackError): publish(session, platform="youtube", account=Account(110, "youtube"), payload=payload)
    assert_no_publication(session)


@pytest.mark.parametrize("duration,allowed", [(180, True), (181, False)])
def test_youtube_shorts_intent_is_not_silently_turned_into_regular_video(video, monkeypatch, duration, allowed):
    monkeypatch.setattr(sm.media_pipeline, "probe_video_duration", lambda path: duration)
    payload = media_payload(video)
    payload["networkOptions"] = {"yt": {"shorts": True, "title": "Original title"}}
    session = ContractSession()
    if allowed:
        assert publish(session, platform="youtube", account=Account(110, "youtube"), payload=payload)["ok"]
    else:
        with pytest.raises(sm.SociamonialsFallbackError): publish(session, platform="youtube", account=Account(110, "youtube"), payload=payload)
        assert_no_publication(session)


def test_facebook_and_instagram_formats_are_explicit(image, video):
    fb = ContractSession()
    payload = media_payload(video)
    payload["postType"] = "reel"
    publish(fb, platform="facebook", account=Account(64, "facebook"), payload=payload)
    assert fb.last_body["networks"]["fb"]["reel"] is True and fb.last_body["story"] is False
    ig = ContractSession()
    payload = media_payload(video)
    payload["networkOptions"] = {"in": {"postType": "reel", "shareToFeed": False}}
    publish(ig, platform="instagram", account=Account(72, "instagram"), payload=payload)
    assert ig.last_body["networks"]["in"]["reels_only"] is True
    story = ContractSession()
    payload = media_payload(image)
    payload["postType"] = "story"
    publish(story, platform="facebook", account=Account(64, "facebook"), payload=payload)
    assert story.last_body["story"] is True and "reel" not in story.last_body["networks"]["fb"]


@pytest.mark.parametrize("options", [{"postType": "grid", "shareToFeed": False}, {"postType": "video", "reelsOnly": True}])
def test_incompatible_instagram_original_format_is_not_changed(video, options):
    session = ContractSession()
    payload = media_payload(video)
    payload["networkOptions"] = {"in": options}
    with pytest.raises(sm.SociamonialsFallbackError): publish(session, platform="instagram", account=Account(72, "instagram"), payload=payload)
    assert_no_publication(session)


def test_media_guard_is_backup_compatible_and_its_failure_prevents_grants(video, monkeypatch):
    guard = Mock(side_effect=sm.SociamonialsFallbackError("Final prepared bytes failed validation"))
    monkeypatch.setattr(publish_media_guard, "ensure_single_payload", guard)
    session = ContractSession()
    with pytest.raises(sm.SociamonialsFallbackError): publish(session, payload=media_payload(video), verify_media=False)
    assert guard.call_args.kwargs["backup_compatible"] is True
    assert_no_publication(session)


def test_context_artifacts_are_not_uploaded_and_mixed_media_cannot_be_dropped(image, video, tmp_path):
    payload = media_payload(video)
    for suffix in ("txt", "wav"):
        path = tmp_path / ("support." + suffix)
        path.write_bytes(b"internal-only")
        payload["artifacts"].append({"local_path": str(path), "metadata": {"role": "support"}})
    session = ContractSession()
    publish(session, payload=payload)
    assert len(session.calls_for("PUT")) == 1
    assert all(not call[2].get("json", {}).get("filename", "").endswith((".txt", ".wav")) for call in session.calls)
    mixed = media_payload(video)
    mixed["artifacts"] += media_payload(image)["artifacts"]
    rejected = ContractSession()
    with pytest.raises(sm.SociamonialsFallbackError, match="Mixed"): publish(rejected, payload=mixed)
    assert_no_publication(rejected)


@pytest.mark.parametrize("mode", ["multiple-videos", "excess-images", "only-context"])
def test_every_declared_part_is_required_not_just_first(video, image, tmp_path, mode):
    session = ContractSession()
    if mode == "multiple-videos":
        payload = media_payload(video)
        payload["artifacts"] *= 2
    elif mode == "excess-images":
        payload = media_payload(image)
        payload["artifacts"] *= 5
    else:
        path = tmp_path / "support.txt"
        path.write_text("Internal", encoding="utf-8")
        payload = {"message": "Original", "artifacts": [{"local_path": str(path)}]}
    with pytest.raises(sm.SociamonialsFallbackError): publish(session, payload=payload)
    assert_no_publication(session)


@pytest.mark.parametrize("changes", [{"status": "unavailable"}, {"available_bytes": None}, {"max_upload_bytes": {}}, {"enforcement": None}, {"pooled": None}])
def test_unknown_storage_fails_closed_before_any_upload(image, changes):
    session = ContractSession(storage_block=storage(**changes))
    with pytest.raises(sm.SociamonialsFallbackError) as caught: publish(session, payload=media_payload(image))
    assert caught.value.details["storageUnknown"] is True
    assert_no_publication(session)


def test_proven_ready_exact_asset_needs_no_unknown_storage_allocation(image):
    session = ContractSession(assets=[asset_for(image, asset_id=555)], storage_block=storage(status="unavailable"))
    result = publish(session, payload=media_payload(image))
    assert result["ok"] and session.last_body["image_urls"] == ["asset://555"]
    assert not session.calls_for("POST", "/media/uploads") and not session.calls_for("GET", "/context")


def test_pooled_headroom_counts_all_new_images_before_first_grant(image, tmp_path):
    second = tmp_path / "second.png"
    Image.new("RGB", (16, 16), "blue").save(second)
    payload = media_payload(image)
    payload["artifacts"] += media_payload(second)["artifacts"]
    session = ContractSession(storage_block=storage(available_bytes=image.stat().st_size))
    with pytest.raises(sm.SociamonialsFallbackError): publish(session, payload=payload)
    assert_no_publication(session)


@pytest.mark.parametrize("hosted", [None, 1])
def test_hosted_rejection_prefers_only_same_hash_stable_external_video(video, hosted):
    url = f"https://cdn.example/immutable/{sm._sha256(video)}.mp4"
    session = ContractSession(storage_block=storage(max_upload_bytes={"image": policy.IMAGE_MAX_BYTES, "video": hosted}))
    session.public[url] = Response(headers={"Content-Type": "video/mp4", "Content-Length": str(video.stat().st_size)}, content=video.read_bytes())
    result = publish(session, payload=media_payload(video, public_url=url), verify_media=False)
    assert result["ok"] and session.last_body["video_url"] == url
    assert not session.calls_for("PUT") and not session.calls_for("POST", "/media/uploads")
    external = [call for call in session.calls if call[1] == url]
    assert external and not external[0][2]["headers"].get("Authorization")


@pytest.mark.parametrize("fault", ["mutable", "different-hash", "html", "redirect", "too-many-bytes", "timeout"])
def test_uncertain_external_bytes_stop_without_oversized_original_fallback(video, fault):
    url = f"https://cdn.example/{sm._sha256(video)}.mp4" if fault != "mutable" else "https://cdn.example/mutable.mp4"
    response = Response(headers={"Content-Type": "video/mp4"}, content=video.read_bytes())
    if fault == "different-hash": response.content = b"x" * video.stat().st_size
    if fault == "html": response.headers["Content-Type"] = "text/html"
    if fault == "redirect": response.status_code = 302
    if fault == "too-many-bytes": response.content += b"extra"
    def override(method, target, kw):
        if fault == "timeout" and target == url: raise OSError("credential/signature not to be logged")
    session = ContractSession(storage_block=storage(available_bytes=0), override=override)
    session.public[url] = response
    with pytest.raises(sm.SociamonialsFallbackError): publish(session, payload=media_payload(video, public_url=url), verify_media=False)
    assert_no_publication(session)


@pytest.mark.parametrize("url", ["https://user:password@cdn.example/a.mp4", "https://cdn.example/a.mp4?token=secret", "https://127.0.0.1/a.mp4",
    "https://10.0.0.1/a.mp4", "https://169.254.169.254/a.mp4", "https://[::1]/a.mp4", "https://drive.google.com/uc/a.mp4", "https://cdn.example/a.mp4#fragment"])
def test_credential_expiring_private_or_viewer_urls_are_not_direct(url):
    assert not sm.is_direct_media_url(url)


def test_dns_guard_rejects_private_address_without_fetch(monkeypatch):
    monkeypatch.setattr(sm.socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("10.0.0.1", 443))])
    with pytest.raises(sm.SociamonialsFallbackError): _REAL_RESOLVER("cdn.example")


def test_missing_local_original_never_uses_unverified_public_url(video):
    payload = media_payload(video, public_url=f"https://cdn.example/{sm._sha256(video)}.mp4")
    video.unlink()
    session = ContractSession()
    from myUtils.media_compliance import MediaPreparationRequired
    with pytest.raises(MediaPreparationRequired): publish(session, payload=payload, verify_media=False)
    assert_no_publication(session)


def test_hosted_and_external_byte_boundaries_are_distinct():
    sm._assert_media_size("yt", 1_073_741_824)
    with pytest.raises(sm.SociamonialsFallbackError): sm._assert_media_size("yt", 1_073_741_825)
    caps = policy.limits_for("yt", storage(max_upload_bytes={"image": None, "video": 2_000_000_000}))
    assert caps["hosted_video_max_bytes"] == 1_073_741_824 and caps["external_video_max_bytes"] == 1_024_000_000


def post_view(*, delivered=True, native_id="native-post-1", ref="14100", network="tw", **changes):
    return {"post_id": 999, "workspace_registration_id": 26985, "approved": True,
            "networks": {network: {"delivered": delivered, "delivery_status": "delivered",
                "profiles": [{"profile_ref": ref, "delivery_state": "published", "platform_post_id": native_id}]}}, **changes}


def view_result(body):
    session = ContractSession(override=lambda method, url, kw: Response(body))
    return sm._wait_for_delivery(session, {}, 999, timeout=0, network="tw", profile_refs=["14100"], workspace_id=26985)


@pytest.mark.parametrize("delivered", ["true", 1, False, None])
def test_delivery_requires_literal_true_not_truthiness_or_status(delivered):
    assert view_result(post_view(delivered=delivered))["delivered"] is not True


@pytest.mark.parametrize("native_id", [None, "", "null", "0", "unknown", " ", False, True, -1, "-1"])
def test_generic_receipt_or_unusable_provider_id_is_not_delivery(native_id):
    assert view_result(post_view(native_id=native_id))["delivered"] is not True


@pytest.mark.parametrize("body", [post_view(ref="other"), post_view(network="fb"), post_view(workspace_registration_id=34293), post_view(post_id=998)])
def test_delivered_other_network_profile_workspace_or_receipt_is_not_success(body):
    assert view_result(body)["state"] == "unknown"


def test_per_destination_success_and_partial_outcomes_are_distinct():
    assert view_result(post_view())["delivered"] is True
    body = post_view()
    body["networks"]["tw"]["profiles"].append({"profile_ref": "other", "delivery_state": "needs_attention", "platform_post_id": None})
    result = view_result(body)
    assert result["state"] == "partial" and result["delivered"] is not True


def test_post_level_receipt_id_or_network_only_boolean_never_substitutes_provider_post_id():
    body = post_view()
    body["networks"]["tw"]["profiles"][0].pop("platform_post_id")
    assert view_result(body)["delivered"] is not True
    body["networks"]["tw"]["profiles"] = []
    assert view_result(body)["delivered"] is not True


@pytest.mark.parametrize("status", [429, 503])
def test_poll_rate_deadlines_are_not_swallowed_and_never_recreate(status):
    session = ContractSession()
    payload = {"message": "Original"}
    publish(session, payload=payload)
    session.override = lambda method, url, kw: Response({"error": {"code": "rate_limited"}}, status,
        {"Retry-After": "120", "X-RateLimit-Reset": "1999999999"}) if method == "GET" and "/posts/" in url else None
    with pytest.raises(delivery.DeliveryPending) as caught: publish(session, payload=payload)
    assert caught.value.details["retryAfter"] == "120" and caught.value.details["rateLimitReset"] == "1999999999"
    assert delivery.receipt_for(payload, "sociamonials")["id"] == "999"
    assert len(session.calls_for("POST", "/posts", create=True)) == 1


def test_receipt_resume_owns_original_scope_even_after_config_changes(monkeypatch):
    monkeypatch.setenv(sm.API_KEY_ENV, "original-fixture")
    session = ContractSession()
    payload = {"message": "Original"}
    publish(session, payload=payload, api_key=None)
    changed = Account(config={"sociamonials": {"network": "tw", "profile_ref": "17650", "workspace_id": "34293", "api_key_env": sm.TEACHING_API_KEY_ENV}})
    before = len(session.calls)
    result = publish(session, payload=payload, account=changed, api_key=None)
    assert result["workspace_id"] == 26985 and result["profile_refs"] == ["14100"]
    calls = session.calls[before:]
    assert len(calls) == 2 and all(call[0] == "GET" for call in calls)
    assert all(call[2]["headers"]["Authorization"] == "Bearer original-fixture" for call in calls)


def test_unscoped_legacy_receipt_blocks_instead_of_guessing_current_mapping():
    receipt = {"provider": "sociamonials", "id": "999", "submitted_at": delivery.utc_now()}
    payload = {"message": "Original", "_delivery": {"provider_receipt": receipt}}
    session = ContractSession()
    with pytest.raises(delivery.DeliveryUnknown): publish(session, payload=payload)
    assert delivery.receipt_for(payload, "sociamonials") == receipt and session.calls == []


@pytest.mark.parametrize("proof", [None, {"target_id": 4321, "provider": "twitter", "receipt_id": "wrong", "definitive_rejection": True, "no_publication": True}])
def test_native_receipt_cannot_be_overwritten_without_scoped_parent_proof(proof):
    native = {"provider": "twitter", "id": "native-receipt", "submitted_at": delivery.utc_now()}
    payload = {"message": "Original", "_delivery": {"provider_receipt": native, "submission_started": True}}
    session = ContractSession()
    with pytest.raises(delivery.DeliveryUnknown): publish(session, payload=payload, native_failure_proof=proof)
    assert delivery.state(payload)["provider_receipt"] == native and session.calls == []


def test_parent_proven_definitive_native_rejection_preserves_original_receipt():
    native = {"provider": "twitter", "id": "native-receipt", "submitted_at": delivery.utc_now()}
    proof = {"target_id": 4321, "provider": "twitter", "receipt_id": "native-receipt", "definitive_rejection": True, "no_publication": True}
    payload = {"message": "Original", "_delivery": {"provider_receipt": native, "submission_started": True}}
    session = ContractSession()
    assert publish(session, payload=payload, native_failure_proof=proof)["ok"]
    assert delivery.state(payload)["native_provider_receipt"] == native
    assert delivery.state(payload)["provider_receipt"]["provider"] == "sociamonials"


@pytest.mark.parametrize("status", [500, 503])
def test_ambiguous_create_is_not_retry_safe_and_does_not_create_again(status):
    session = ContractSession(override=lambda method, url, kw: Response({"error": {"code": "server_error"}}, status)
        if method == "POST" and url == sm.POSTS_URL and not kw["json"].get("validate_only") else None)
    payload = {"message": "Original"}
    with pytest.raises(sm.SociamonialsFallbackError) as caught: publish(session, payload=payload)
    assert caught.value.retryable is True and caught.value.details["retrySafe"] is False
    with pytest.raises(delivery.DeliveryUnknown): publish(session, payload=payload)
    assert len(session.calls_for("POST", "/posts", create=True)) == 1


@pytest.mark.parametrize("suffix", ["a1-timestamp", "a2-timestamp", "random"])
def test_transient_attempts_cannot_invent_new_idempotency_suffix(suffix):
    session = ContractSession()
    with pytest.raises(sm.SociamonialsFallbackError): publish(session, idempotency_suffix=suffix)
    assert session.calls == []


@pytest.mark.parametrize("generation", [[], "short", "A" * 64])
def test_invalid_generation_blocks_before_any_http(generation):
    session = ContractSession()
    with pytest.raises(sm.SociamonialsFallbackError): publish(session, payload={"message": "Original", "_delivery": {"recovery_generation": generation}})
    assert session.calls == []


def test_crash_after_grant_replays_same_reservation_not_new_upload(image):
    session = ContractSession()
    payload = media_payload(image)
    def fence(state):
        if any(saved.get("upload_id") for saved in state.get("sociamonials_uploads", {}).values()):
            raise delivery.LeaseLost("Injected crash after saved grant identity")
    payload["_save_checkpoint"] = fence
    with pytest.raises(delivery.LeaseLost): publish(session, payload=payload)
    payload.pop("_save_checkpoint")
    result = publish(session, payload=payload)
    assert result["ok"] and len(session.grant_keys) == 1
    grants = [call for call in session.calls_for("POST", "/media/uploads") if call[1] == sm.MEDIA_UPLOADS_URL]
    assert len(grants) == 2 and grants[0][2]["json"]["idempotency_key"] == grants[1][2]["json"]["idempotency_key"]
    assert len(session.calls_for("PUT")) == 1


@pytest.mark.parametrize("field,value", [("sha256", "0" * 64), ("size_bytes", 1), ("workspace_registration_id", 34293), ("media_type", "video")])
def test_complete_server_computed_receipt_must_match_exact_local_bytes(image, field, value):
    def override(method, url, kw):
        if method == "POST" and url.endswith("/complete"):
            row = asset_for(image)
            row[field] = value
            return Response(row)
    session = ContractSession(override=override)
    payload = media_payload(image)
    with pytest.raises(sm.SociamonialsFallbackError): publish(session, payload=payload)
    assert not session.calls_for("POST", "/posts", create=True)
    assert delivery.state(payload)["sociamonials_uploads"][sm._sha256(image)]["asset_id"] == 77


def test_single_and_multipart_reads_are_bounded_not_whole_file(image):
    with image.open("rb") as handle:
        stream = sm._FileSlice(handle, image.stat().st_size)
        assert stream.read(10**12) == image.read_bytes()
    # Large virtual file: even an unbounded caller read is capped at 1 MiB.
    fake = Mock()
    fake.read.side_effect = lambda count: b"x" * count
    stream = sm._FileSlice(fake, 1_000_000_000)
    assert len(stream.read(-1)) == 1024 * 1024 and len(stream.read(10**12)) == 1024 * 1024


def test_credential_rate_budget_is_shared_and_returns_deadline(monkeypatch):
    monkeypatch.setattr(sm.time, "monotonic", lambda: 0)
    for _ in range(30): sm._take_rate("same-credential", "write", 30)
    with pytest.raises(sm.SociamonialsFallbackError) as caught: sm._take_rate("same-credential", "write", 30)
    assert caught.value.details["retryAfter"] == 60 and caught.value.retryable
    sm._take_rate("isolated-teaching-credential", "write", 30)


def test_no_automatic_reclaim_or_deletion_even_when_storage_full(image):
    from myUtils import sociamonials_reclaim
    reclaim = Mock(side_effect=AssertionError("Deletion forbidden"))
    original = sociamonials_reclaim.ensure_headroom
    sociamonials_reclaim.ensure_headroom = reclaim
    try:
        session = ContractSession(storage_block=storage(available_bytes=0))
        with pytest.raises(sm.SociamonialsFallbackError): publish(session, payload=media_payload(image))
        reclaim.assert_not_called()
        assert all(call[0] != "DELETE" for call in session.calls)
    finally:
        sociamonials_reclaim.ensure_headroom = original


def test_provider_errors_redact_credentials_signatures_and_customer_copy(caplog):
    response = Response({"error": {"code": "validation_failed", "message": "Bearer live-secret https://x/a?signature=secret customer copy",
        "errors": {"message": ["customer copy live-secret"], "video_url": ["https://x/a?signature=secret"]}}}, 422)
    with pytest.raises(sm.SociamonialsFallbackError) as caught: sm._raise_for_status(response, context="create post")
    report = str(caught.value) + json.dumps(caught.value.details) + caplog.text
    assert "live-secret" not in report and "signature" not in report and "customer copy" not in report
    assert set(caught.value.details["fields"]) == {"message", "video_url"} and not hasattr(caught.value, "response")


@pytest.mark.parametrize("changes", [{"requires_approval": True}, {"approved": False}])
def test_approval_hold_never_becomes_success_from_conflicting_delivery_flags(changes):
    result = view_result(post_view(**changes))
    assert result["state"] == "requires_approval" and result["delivered"] is not True


def test_mobile_share_and_interrupted_dispatch_are_review_not_delivery_or_final_failure():
    body = post_view()
    body["networks"]["tw"]["profiles"][0]["reason"] = "MOBILE_APP_SHARE"
    assert view_result(body)["state"] == "requires_approval"
    body = post_view(delivered=False, native_id=None)
    body["networks"]["tw"]["delivery_status"] = "failed"
    body["networks"]["tw"]["profiles"][0].update(delivery_state="needs_attention", reason="MEDIA_DISPATCH_INTERRUPTED")
    assert view_result(body)["state"] == "unknown"


@pytest.mark.parametrize("value", ["published", "processing", "scheduled", "success", "sm_agent_secret", "https://example.org/post?token=secret"])
def test_status_words_and_credential_locators_are_not_usable_platform_receipts(value):
    assert view_result(post_view(native_id=value))["delivered"] is not True


def test_signed_media_locator_is_refused_before_preparation_checkpoint(video, monkeypatch):
    payload = media_payload(video, public_url="https://cdn.example/a.mp4?signature=secret")
    guard = Mock()
    monkeypatch.setattr(publish_media_guard, "ensure_single_payload", guard)
    snapshots = []
    payload["_save_checkpoint"] = lambda state: snapshots.append(copy.deepcopy(state))
    session = ContractSession()
    with pytest.raises(sm.SociamonialsFallbackError): publish(session, payload=payload)
    guard.assert_not_called()
    assert_no_publication(session)
    assert "signature" not in json.dumps(snapshots)


def test_same_path_rewrite_of_scheduled_media_cannot_replace_original_bytes(image, monkeypatch):
    payload = media_payload(image)
    def mutate(working, **kwargs):
        Image.new("RGB", (16, 16), "blue").save(image)
        working["artifacts"][0]["metadata"].update(sha256=sm._sha256(image), size_bytes=image.stat().st_size)
    monkeypatch.setattr(publish_media_guard, "ensure_single_payload", mutate)
    session = ContractSession()
    with pytest.raises(sm.SociamonialsFallbackError, match="scheduled byte/part identity"):
        publish(session, payload=payload)
    assert_no_publication(session)


@pytest.mark.parametrize("key", ["message", "title", "description", "firstComment", "altText"])
def test_literal_null_text_cannot_be_silently_replaced_by_transport_defaults(video, key):
    payload = media_payload(video)
    if key == "message":
        payload["draft"]["message"] = "null"
    else:
        payload["networkOptions"] = {"yt": {key: "null"}}
    session = ContractSession()
    with pytest.raises(sm.SociamonialsFallbackError):
        publish(session, platform="youtube", account=Account(110, "youtube"), payload=payload)
    assert_no_publication(session)


def test_unexpected_receipt_from_validation_is_retained_and_stops_every_further_write(image):
    session = ContractSession(override=lambda method, url, kw: Response({"post_id": 321})
        if method == "POST" and url == sm.POSTS_URL and kw["json"].get("validate_only") else None)
    payload = media_payload(image)
    with pytest.raises(delivery.DeliveryUnknown): publish(session, payload=payload)
    receipt = delivery.receipt_for(payload, "sociamonials")
    assert receipt["id"] == "321" and receipt["profile_refs"] == ["14100"] and receipt["intent_mismatch"] is True
    assert_no_publication(session)


def test_native_operation_items_need_individual_proof_and_proofs_remain_secret_free():
    journal = {"twitter": {"items": {"media:0": {"id": "native-media-1", "status": "complete"}}}}
    payload = {"message": "Original", "_delivery": {"provider_operations": copy.deepcopy(journal)}}
    session = ContractSession()
    proof = {"target_id": 4321, "definitive_rejection": True, "no_publication": True}
    with pytest.raises(delivery.DeliveryUnknown): publish(session, payload=payload, native_failure_proof=proof)
    assert session.calls == [] and delivery.state(payload)["provider_operations"] == journal
    proof["operation_receipts"] = {"twitter": {"media:0": {"id": "native-media-1", "definitive_rejection": True, "no_publication": True}}}
    proof["api_key"] = "must-not-be-checkpointed"
    assert publish(session, payload=payload, native_failure_proof=proof)["ok"]
    assert delivery.state(payload)["provider_operations"] == journal
    assert "must-not-be-checkpointed" not in json.dumps(delivery.state(payload))


def test_missing_etag_cannot_complete_or_create_an_unverified_upload(image):
    def override(method, url, kw):
        if method == "PUT":
            while kw["data"].read(8192):
                pass
            return Response({}, headers={})
    session = ContractSession(override=override)
    with pytest.raises(sm.SociamonialsFallbackError, match="ETag"):
        publish(session, payload=media_payload(image))
    assert not session.calls_for("POST", "/complete") and not session.calls_for("POST", "/posts", create=True)


def test_multipart_grant_is_finite_and_invalid_count_never_starts_transfer(video):
    def override(method, url, kw):
        if method == "POST" and url == sm.MEDIA_UPLOADS_URL:
            return Response({"upload_id": 77, "workspace_registration_id": 26985, "mode": "multipart", "method": "PUT",
                "media_type": "video", "max_bytes": policy.HOSTED_VIDEO_MAX_BYTES, "required_headers": {},
                "expires_at": "2099-01-01T00:00:00Z", "part_size_bytes": 1, "part_count": sm.MAX_UPLOAD_PARTS + 1})
    session = ContractSession(override=override)
    with pytest.raises(sm.SociamonialsFallbackError, match="finite upload"):
        publish(session, payload=media_payload(video))
    assert not session.calls_for("PUT") and not session.calls_for("POST", "/posts", create=True)


def test_lower_actual_hosted_cap_and_approval_are_read_before_media_writes(image):
    session = ContractSession(storage_block=storage(max_upload_bytes={"image": 1, "video": policy.HOSTED_VIDEO_MAX_BYTES}))
    with pytest.raises(sm.SociamonialsFallbackError): publish(session, payload=media_payload(image))
    assert_no_publication(session)
    held = ContractSession()
    held.approval = True
    with pytest.raises(sm.SociamonialsNotConfigured): publish(held, payload=media_payload(image))
    assert_no_publication(held)


def test_processing_poll_request_count_and_http_wall_budget_are_finite(monkeypatch):
    session = ContractSession(delivery_state="processing")
    result = publish(session, delivery_timeout=60)
    assert result["delivery_state"] == "processing" and result["ok"] is False
    assert len(session.calls_for("GET", "/posts/")) == sm.MAX_POLL_REQUESTS
    clock = [0.0]
    monkeypatch.setattr(sm.time, "monotonic", lambda: clock[0])
    http = sm._HTTP(ContractSession(), "budget-fixture")
    clock[0] = sm.MAX_HTTP_SECONDS + 1
    with pytest.raises(sm.SociamonialsFallbackError) as caught: http.get(sm.POSTS_URL)
    assert caught.value.retryable and caught.value.details["retrySafe"] is True


def test_provider_api_secret_cannot_be_echoed_as_an_error_code():
    response = Response({"error": {"code": "sm_agent_secret"}}, 401)
    with pytest.raises(sm.SociamonialsFallbackError) as caught: sm._raise_for_status(response, context="identity", side_effects=False)
    assert "sm_agent_secret" not in str(caught.value) + json.dumps(caught.value.details)


def test_scoped_worker_rejection_resolves_sticky_native_start_without_a_receipt():
    payload = {"message": "Keep original text"}
    delivery.starting(payload, operation="native-create-post")
    delivery.record_non_delivery(payload, provider="native", reason="Verified HTTP 401 response")
    assert payload["_delivery"]["submission_started"] is True
    session = ContractSession()
    result = publish(session, payload=payload)
    assert result["delivered"] is True
    assert len(session.calls_for("POST", "/posts", create=True)) == 1


def test_ambiguous_native_start_without_receipt_never_creates_backup():
    payload = {"message": "Keep original text"}
    delivery.starting(payload, operation="native-create-post")
    session = ContractSession()
    with pytest.raises(delivery.DeliveryUnknown):
        publish(session, payload=payload)
    assert_no_publication(session)


def test_null_image_hosting_entitlement_never_requests_an_upload_grant(image):
    session = ContractSession(storage_block=storage(max_upload_bytes={"image": None, "video": policy.HOSTED_VIDEO_MAX_BYTES}))
    with pytest.raises(sm.SociamonialsFallbackError):
        publish(session, payload=media_payload(image))
    assert_no_publication(session)
