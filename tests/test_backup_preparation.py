"""Fresh backup constraints are finite, credential-free and intent-preserving."""
from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from myUtils import backup_preparation as bridge, media_compliance, sociamonials_policy as policy


def verified(network="thrd", *, video_bytes=1_073_741_824, video_seconds=300, image_bytes=15_728_640):
    return {"requires_approval": False,
            "mapping": {"network": network, "profile_refs": ["fixture"], "workspace_id": "34293"},
            "limits": {"policy_version": "fixture-v1", "hosted_video_max_bytes": video_bytes,
                       "external_video_max_bytes": 1_024_000_000, "image_max_bytes": image_bytes,
                       "max_images": 10, "video_max_seconds": video_seconds}}


def test_live_constraints_tighten_but_never_raise_native_limits(monkeypatch):
    read = Mock(return_value=verified(video_bytes=2_000_000, video_seconds=120, image_bytes=1_000_000))
    monkeypatch.setattr(bridge.adapter, "verify_destination", read)
    account = SimpleNamespace(config={"accessToken": "fixture-secret-not-in-context"})
    context = bridge.resolve_constraints(platform="threads", account=account)
    assert context["video_limits"]["max_bytes"] == 2_000_000
    assert context["video_limits"]["max_seconds"] == 120
    assert context["image_limits"]["max_bytes"] == 1_000_000
    assert "fixture-secret-not-in-context" not in repr(context)
    context = bridge.constraints_from_verification(verified(video_bytes=9_000_000_000, video_seconds=900), platform="threads")
    assert context["video_limits"]["max_bytes"] == 1_000_000_000
    assert context["video_limits"]["max_seconds"] == 300
    read.assert_called_once()


@pytest.mark.parametrize("patch", [{"requires_approval": True}, {"requires_approval": None},
                                    {"mapping": {"network": "fb", "profile_refs": ["fixture"], "workspace_id": "34293"}}])
def test_unknown_or_wrong_destination_stops_preparation(patch):
    with pytest.raises(media_compliance.MediaPreparationError):
        bridge.constraints_from_verification({**verified(), **patch}, platform="threads")


def test_shorter_backup_caption_is_reported_without_mutating_original():
    context = bridge.constraints_from_verification(verified("tiktok", video_seconds=600), platform="tiktok")
    payload = {"message": "😀" * 76, "artifacts": [{"local_path": "fixture.mp4"}]}
    before = copy.deepcopy(payload)
    with pytest.raises(policy.SociamonialsPolicyError):
        bridge.validate_intent(payload, context=context, prepared=False)
    assert payload == before


def test_distinct_image_alt_text_is_not_collapsed():
    context = bridge.constraints_from_verification(verified(), platform="threads")
    payload = {"message": "Keep the caption", "artifacts": [
        {"local_path": "one.png", "metadata": {"alt_text": "First"}},
        {"local_path": "two.png", "metadata": {"alt_text": "Second"}}]}
    with pytest.raises(media_compliance.MediaPreparationError, match="alt text"):
        bridge.validate_intent(payload, context=context)


def test_unsupported_cover_is_reported_before_queueing():
    context = bridge.constraints_from_verification(verified(), platform="threads")
    payload = {"message": "Keep the caption", "artifacts": [
        {"local_path": "fixture.mp4"}, {"local_path": "cover.jpg", "metadata": {"role": "cover"}}]}
    with pytest.raises(media_compliance.MediaPreparationError, match="cover"):
        bridge.validate_intent(payload, context=context)


def test_unentitled_hosting_is_not_treated_as_unlimited():
    context = bridge.constraints_from_verification(verified(video_bytes=None), platform="threads")
    assert context["hosted_video_entitled"] is False
    assert context["video_limits"]["max_bytes"] == 1_000_000_000
    with pytest.raises(media_compliance.MediaPreparationError, match="No hosted"):
        bridge.validate_intent({"message": "Keep", "artifacts": [{"local_path": "fixture.mp4"}]}, context=context)


def test_unentitled_hosting_can_use_exact_verified_stable_url(monkeypatch):
    context = bridge.constraints_from_verification(verified(video_bytes=None), platform="threads")
    monkeypatch.setattr(bridge.adapter, "_checked_media", lambda items, _network: items)
    check = Mock(return_value="https://media.example/fixture.mp4")
    monkeypatch.setattr(bridge.adapter, "_external_reference", check)
    monkeypatch.setattr(bridge.adapter, "_http", lambda *args: object())
    payload = {"message": "Keep", "artifacts": [{"local_path": "fixture.mp4", "public_url": "https://media.example/fixture.mp4"}]}
    bridge.validate_intent(payload, context=context)
    check.assert_called_once()
