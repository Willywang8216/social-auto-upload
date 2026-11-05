"""Caller-level capability reads and backup media profiles, without live services."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from myUtils import preparation_context as context, media_compliance


def account(platform="tiktok", config=None):
    return SimpleNamespace(id=900, platform=platform, enabled=True, config=config or {})


def response(body):
    return SimpleNamespace(status_code=200, headers={}, json=lambda: body, text="", raise_for_status=lambda: None)


def test_fresh_creator_limit_tightens_stale_allowance_without_credentials_in_result():
    actor = account(config={"accessToken": "fixture-secret", "creator_info": {"max_video_post_duration_sec": 600}})
    http = Mock()
    http.post.return_value = response({"data": {"max_video_post_duration_sec": 75}, "error": {"code": "ok"}})
    result = context.account_constraints(actor, session=http)
    assert result["video_limits"]["max_seconds"] == 75
    assert result["capability_check"] == "fresh_creator"
    assert "fixture-secret" not in repr(result)
    assert http.post.call_args.kwargs["timeout"] == 45


def test_unknown_creator_lookup_retains_conservative_baseline():
    actor = account(config={"creator_info": {"max_video_post_duration_sec": 600}})
    http = Mock()
    result = context.account_constraints(actor, session=http)
    assert result["video_limits"]["max_seconds"] == 180
    http.post.assert_not_called()


def test_fresh_allowance_cannot_raise_an_existing_conservative_profile():
    actor = account(config={"accessToken": "fixture-only"})
    http = Mock()
    http.post.return_value = response({"data": {"max_video_post_duration_sec": 600}, "error": {"code": "ok"}})
    assert context.account_constraints(actor, session=http)["video_limits"]["max_seconds"] == 180


@pytest.mark.parametrize("actual,expected", [("wanted", 43200), ("different", 900)])
def test_youtube_allowance_requires_exact_owned_channel(actual, expected):
    actor = account("youtube", {"accessToken": "fixture-only", "channelId": "wanted",
                                "channelStatus": {"longUploadsStatus": "allowed"}})
    http = Mock()
    http.get.return_value = response({"items": [{"id": actual, "status": {"longUploadsStatus": "allowed"}}]})
    assert context.account_constraints(actor, session=http)["video_limits"]["max_seconds"] == expected
    assert http.get.call_args.kwargs["params"]["mine"] == "true"


def backup_verified():
    return {"requires_approval": False,
            "mapping": {"network": "fb", "profile_refs": ["fixture"], "workspace_id": "123"},
            "limits": {"policy_version": "fixture-v1", "hosted_video_max_bytes": 5_000_000,
                       "external_video_max_bytes": 1_024_000_000, "image_max_bytes": 1_000_000,
                       "max_images": 10, "video_max_seconds": 300}}


def test_configured_viable_backup_is_selected_before_preparation(monkeypatch):
    actor = account("facebook", {"accessToken": "fixture-secret", "sociamonials": {
        "network": "fb", "profile_refs": ["fixture"], "workspace_id": "123"}})
    monkeypatch.setattr(context.backup_preparation.adapter, "is_enabled", lambda: True)
    read = Mock(return_value=backup_verified())
    monkeypatch.setattr(context.backup_preparation.adapter, "verify_destination", read)
    request, verified = context.configure_request([actor], {"facebook"}, {})
    assert request["_backupCompatiblePlatforms"] == ["facebook"]
    assert request["_preparationLimits"]["facebook"]["max_bytes"] == 5_000_000
    assert request["_imagePreparationLimits"]["facebook"]["max_bytes"] == 1_000_000
    assert 900 in verified and "fixture-secret" not in repr(request)
    read.assert_called_once()


def test_unavailable_optional_backup_keeps_native_media_profile(monkeypatch):
    actor = account("facebook", {"sociamonials": {"network": "fb", "profile_refs": ["fixture"], "workspace_id": "123"}})
    monkeypatch.setattr(context.backup_preparation.adapter, "is_enabled", lambda: True)
    monkeypatch.setattr(context.backup_preparation.adapter, "verify_destination", Mock(side_effect=RuntimeError("Unavailable")))
    request, verified = context.configure_request([actor], {"facebook"}, {})
    assert verified == {} and request["_backupCompatiblePlatforms"] == []
    assert request["_preparationLimits"]["facebook"]["max_bytes"] == 4_000_000_000
    assert request["_backupPreparationWarnings"]["900"] == "Unavailable"


def test_unavailable_explicit_primary_is_not_replaced_by_native(monkeypatch):
    actor = account("facebook", {"publishingProvider": "sociamonials", "sociamonials": {
        "network": "fb", "profile_refs": ["fixture"], "workspace_id": "123"}})
    monkeypatch.setattr(context.backup_preparation.adapter, "is_enabled", lambda: False)
    with pytest.raises(media_compliance.MediaPreparationError, match="primary"):
        context.configure_request([actor], {"facebook"}, {})
