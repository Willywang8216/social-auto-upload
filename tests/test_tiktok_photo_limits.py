"""TikTok photo fields are not video captions; all providers are mocked."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from myUtils import prepared_publishers as pp, publish_media_guard as guard, text_limits as text
from tests.test_prepared_publishers import _FakeResponse as Response, _RecordingSession as Session


def photo_payload(title="Approved photo title", message="Approved description"):
    return {
        "_preserveApprovedText": True,
        "message": message,
        "tiktokDirectPost": True,
        "tiktokPostSettings": {
            "title": title, "privacyLevel": "PUBLIC_TO_EVERYONE", "consentChecked": True,
        },
        "artifacts": [{"public_url": "https://cdn.example/photo.jpg", "artifact_kind": "image"}],
    }


def photo_session():
    return Session([
        Response({"data": {"privacy_level_options": ["PUBLIC_TO_EVERYONE"]}}),
        Response({"data": {"publish_id": "synthetic-photo-receipt"}, "error": {"code": "ok"}}),
    ])


@pytest.mark.parametrize("field,limit", [("photo_title", 90), ("photo_description", 4000)])
def test_photo_fields_count_utf16_and_cannot_relax(field, limit):
    exact = "🙂" * (limit // 2)
    assert text.validate_message(exact, "tiktok", field=field) == exact
    with pytest.raises(text.TextLimitError, match="utf16_units"):
        text.validate_message(exact + "a", "tiktok", field=field, max_length=limit + 100)
    with pytest.raises(text.TextLimitError):
        text.validate_message(exact, "tiktok", field=field, max_length=limit - 1)


def test_photo_description_4000_is_accepted_without_changing_video_policy(monkeypatch):
    monkeypatch.setenv("SAU_TIKTOK_VERIFIED_URL_PREFIXES", "https://cdn.example/")
    payload = photo_payload(title="🙂" * 45, message="🙂" * 2000)
    original = copy.deepcopy(payload)
    session = photo_session()
    account = SimpleNamespace(config={"accessToken": "synthetic", "privacyLevel": "PUBLIC_TO_EVERYONE"})
    result = pp.publish_tiktok_sync(account, payload, session=session)
    info = result["request"]["post_info"]
    assert info["title"] == original["tiktokPostSettings"]["title"]
    assert info["description"] == original["message"]
    assert session.calls[-1][1] == pp.TIKTOK_CONTENT_INIT_URL
    assert payload["message"] == original["message"]
    with pytest.raises(text.TextLimitError):
        text.validate_message(original["message"], "tiktok", field="caption")


@pytest.mark.parametrize("title,message", [("🙂" * 45 + "x", "Description"), ("Title", "🙂" * 2000 + "x")])
def test_approved_photo_overflow_never_submits_or_rewrites(title, message, monkeypatch):
    monkeypatch.setenv("SAU_TIKTOK_VERIFIED_URL_PREFIXES", "https://cdn.example/")
    payload = photo_payload(title, message)
    original = copy.deepcopy(payload)
    session = photo_session()
    account = SimpleNamespace(config={"accessToken": "synthetic", "privacyLevel": "PUBLIC_TO_EVERYONE"})
    with pytest.raises(pp.PreparedPublishError) as error:
        pp.publish_tiktok_sync(account, payload, session=session)
    assert error.value.retryable is False
    assert all(call[1] != pp.TIKTOK_CONTENT_INIT_URL for call in session.calls)
    assert payload["message"] == original["message"]
    assert payload["tiktokPostSettings"] == original["tiktokPostSettings"]


def test_prequeue_photo_fields_use_the_same_contract_and_fallback_title():
    payload = photo_payload(title="🙂" * 45, message="🙂" * 2000)
    guard._validate_approved_text(payload, "tiktok", has_media=True)
    payload["tiktokPostSettings"]["title"] += "x"
    with pytest.raises((text.TextLimitError, pp.PreparedPublishError)):
        guard._validate_approved_text(payload, "tiktok", has_media=True)
    payload = photo_payload(title="", message="a" * 91 + "\nApproved body")
    with pytest.raises((text.TextLimitError, pp.PreparedPublishError)):
        guard._validate_approved_text(payload, "tiktok", has_media=True)


def test_prequeue_video_with_cover_keeps_video_caption_limit():
    payload = photo_payload(title="a" * 91, message="a" * 2200)
    payload["artifacts"].append({"local_path": "synthetic.mp4", "artifact_kind": "video"})
    payload["artifacts"][0]["metadata"] = {"role": "cover"}
    guard._validate_approved_text(payload, "tiktok", has_media=True)
    payload["message"] += "a"
    with pytest.raises(text.TextLimitError):
        guard._validate_approved_text(payload, "tiktok", has_media=True)


def test_photo_1080p_policy_preserves_full_frame_and_original_bytes(tmp_path):
    from PIL import Image
    from myUtils import image_compliance as images, media_compliance as media

    source = tmp_path / "wide.jpg"
    image = Image.new("RGB", (1920, 1080), "blue")
    image.paste("red", (0, 0, 160, 1080))
    image.paste("green", (1760, 0, 1920, 1080))
    image.save(source)
    original = media.sha256_file(source)
    with pytest.raises(media.MediaPreparationError):
        images.validate_image(source, platform="tiktok")
    prepared = images.prepare_image(source, tmp_path / "outputs", platform="tiktok")
    info = images.validate_image(prepared.path, platform="tiktok", expected_sha256=prepared.metadata["sha256"])
    assert max(info["width"], info["height"]) <= 1080
    assert abs(info["width"] / info["height"] - 1920 / 1080) < 0.01
    assert media.sha256_file(source) == original
    with Image.open(prepared.path) as final:
        left = final.getpixel((20, final.height // 2))
        right = final.getpixel((final.width - 21, final.height // 2))
        assert left[0] > 180 and left[2] < 60
        assert right[1] > 90 and right[0] < 60
    tightened = images.effective_limits("tiktok", limits={"max_width": 4096, "max_height": 4096})
    assert tightened["max_width"] == tightened["max_height"] == 1080
