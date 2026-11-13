"""Explicit option aliases are immutable approved values, not precedence hints."""
import pytest

from myUtils import prepared_publishers, publish_admission


@pytest.mark.parametrize("where", ["payload", "draft", "artifact", "metadata"])
@pytest.mark.parametrize("field", ["firstComment", "first_comment"])
def test_first_comment_mapper_preserves_all_supported_alias_locations(where, field):
    payload = {"message": "Approved post", "_preserveApprovedText": True,
               "artifacts": [{"artifact_kind": "image", "local_path": "offline.png", "metadata": {}}]}
    target = payload if where == "payload" else payload.setdefault("draft", {}) if where == "draft" else payload["artifacts"][0] if where == "artifact" else payload["artifacts"][0]["metadata"]
    target[field] = "  Exact approved comment.  "
    publish_admission.validate_intent(payload, platform="facebook")
    assert prepared_publishers._first_comment_text(payload) == "  Exact approved comment.  "


def test_conflicting_first_comment_aliases_are_not_silently_dropped():
    payload = {"message": "Approved", "firstComment": "One approved value", "draft": {"first_comment": "A different approved value"}}
    with pytest.raises(publish_admission.AdmissionError, match="conflicting approved aliases"):
        publish_admission.validate_intent(payload, platform="facebook")
    with pytest.raises(prepared_publishers.PreparedPublishError, match="conflicting approved aliases"):
        prepared_publishers._first_comment_text(payload)


def test_identical_first_comment_aliases_are_allowed():
    payload = {"message": "Approved", "firstComment": "Same exact value", "draft": {"first_comment": "Same exact value"}}
    publish_admission.validate_intent(payload, platform="facebook")
    assert prepared_publishers._first_comment_text(payload) == "Same exact value"


@pytest.mark.parametrize("field", ["altText", "alt_text"])
def test_bluesky_image_alias_mapping_keeps_default_and_per_image_values(field):
    payload = {"message": "Approved", "draft": {field: "Approved default"}, "artifacts": [
        {"artifact_kind": "image", "local_path": "offline-a.png", "metadata": {field: "First image"}},
        {"artifact_kind": "image", "local_path": "offline-b.png", "metadata": {field: "Second image"}},
        {"artifact_kind": "image", "local_path": "offline-c.png"}]}
    publish_admission.validate_intent(payload, platform="bluesky")
    assert [image["alt_text"] for image in prepared_publishers._extract_media(payload)["images"]] == ["First image", "Second image", "Approved default"]


@pytest.mark.parametrize("conflict", ["default", "image", "duplicate"])
def test_alt_text_conflicts_refuse_before_provider_work(conflict):
    payload = {"message": "Approved", "altText": "Default", "artifacts": [{"artifact_kind": "image", "local_path": "offline.png"}]}
    if conflict == "default":
        payload["draft"] = {"alt_text": "Different default"}
    elif conflict == "image":
        payload["artifacts"][0].update(altText="One", metadata={"alt_text": "Different"})
    else:
        payload["artifacts"].append({"artifact_kind": "image", "local_path": "offline.png", "alt_text": "Different image value"})
    with pytest.raises(publish_admission.AdmissionError, match="conflicting approved"):
        publish_admission.validate_intent(payload, platform="bluesky")
    with pytest.raises(prepared_publishers.PreparedPublishError, match="conflicting approved"):
        prepared_publishers._extract_media(payload)


@pytest.mark.parametrize("field", ["altText", "alt_text"])
def test_native_x_does_not_claim_unimplemented_image_alt_mapping(field):
    payload = {"message": "Approved", "artifacts": [{"artifact_kind": "image", field: "Approved accessibility text"}]}
    with pytest.raises(publish_admission.AdmissionError, match="cannot preserve"):
        publish_admission.validate_intent(payload, platform="twitter", effective_provider="direct")
    publish_admission.validate_intent(payload, platform="twitter", effective_provider="sociamonials")


def test_empty_native_options_mean_no_operation_and_remain_in_approved_intent():
    payload = {"message": "Approved", "firstComment": "", "altText": "",
               "artifacts": [{"artifact_kind": "image", "local_path": "offline.png"}]}
    publish_admission.validate_intent(payload, platform="twitter", effective_provider="direct")
    assert payload["firstComment"] == payload["altText"] == ""
    assert prepared_publishers._first_comment_text(payload) == ""
