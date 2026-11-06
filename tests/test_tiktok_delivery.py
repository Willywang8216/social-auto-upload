"""Offline TikTok intent/receipt regressions; no live accounts or provider calls."""
from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.test_prepared_publishers import _FakeResponse as Response, _RecordingSession as Session
from myUtils import delivery, failure_classification as fc, jobs, profiles, tiktok_delivery as tt, worker
from myUtils import prepared_publishers as pp
from db import createTable


@pytest.fixture
def account():
    return SimpleNamespace(id=901, config={"accessToken": "synthetic-token", "privacyLevel": tt.PUBLIC})


@pytest.fixture
def payload():
    return {"message": "An original caption kept exactly as approved.", "tiktokDirectPost": True,
            "tiktokPostSettings": {"privacyLevel": tt.PUBLIC, "consentChecked": True,
                                  "disableComment": True, "disableDuet": False, "disableStitch": True,
                                  "contentDisclosure": {"enabled": True, "yourBrand": True, "brandedContent": False}},
            "artifacts": [{"public_url": "https://cdn.invalid/prepared.mp4", "artifact_kind": "video"}]}


def creator(privacy=None):
    return Response({"data": {"creator_username": "fixture_creator", "max_video_post_duration_sec": 600,
                              "privacy_level_options": privacy or [tt.PUBLIC, "SELF_ONLY"]},
                     "error": {"code": "ok"}})


def accepted():
    return Response({"data": {"publish_id": "tracking-receipt"}, "error": {"code": "ok"}})


def test_direct_video_uses_only_documented_schema_and_retains_intent(account, payload):
    before = copy.deepcopy(payload)
    session = Session([creator(), accepted()])
    result = pp.publish_tiktok_sync(account, payload, session=session)
    assert session.calls[-1][1] == pp.TIKTOK_VIDEO_INIT_URL
    request = result["request"]
    assert set(request) == {"source_info", "post_info"}
    assert request["post_info"]["privacy_level"] == tt.PUBLIC
    assert request["post_info"]["title"] == before["message"]
    assert request["post_info"]["disable_comment"] is True
    assert request["post_info"]["disable_duet"] is False
    assert request["post_info"]["disable_stitch"] is True
    assert request["post_info"]["brand_organic_toggle"] is True
    assert request["post_info"]["brand_content_toggle"] is False
    receipt = delivery.receipt_for(payload, "tiktok")
    assert receipt["publish_mode"] == "direct" and receipt["privacy_level"] == tt.PUBLIC
    assert receipt["status"] == "processing"
    assert "synthetic-token" not in json.dumps(receipt)


def test_explicit_inbox_video_does_not_require_publish_scope_or_drop_original_copy(account, payload):
    payload["tiktokDirectPost"] = False
    account.config["scope"] = "video.upload"
    original = copy.deepcopy(payload)
    session = Session([accepted()])
    result = pp.publish_tiktok_sync(account, payload, session=session)
    assert len(session.calls) == 1
    assert session.calls[0][1] == pp.TIKTOK_INBOX_VIDEO_INIT_URL
    assert result["request"] == {"source_info": {"source": "PULL_FROM_URL", "video_url": original["artifacts"][0]["public_url"]}}
    assert payload["message"] == original["message"]
    assert payload["tiktokPostSettings"] == original["tiktokPostSettings"]
    assert delivery.receipt_for(payload, "tiktok")["publish_mode"] == "inbox"


def test_sandbox_public_request_cannot_become_private(account, payload, monkeypatch):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "sb-synthetic-client")
    session = Session([])
    with pytest.raises(pp.PreparedPublishError) as error:
        pp.publish_tiktok_sync(account, payload, session=session)
    assert not error.value.retryable and error.value.details["nativePublicRestriction"] is True
    assert fc.backup_eligible(error.value, checkpoint=delivery.state(payload))
    assert session.calls == [] and payload["tiktokPostSettings"]["privacyLevel"] == tt.PUBLIC
    assert not delivery.receipt_for(payload, "tiktok")


def test_account_privacy_restriction_is_not_a_backup_bypass(account, payload):
    session = Session([creator(["SELF_ONLY"])])
    with pytest.raises(pp.PreparedPublishError) as error:
        pp.publish_tiktok_sync(account, payload, session=session)
    assert not fc.backup_eligible(error.value)
    assert len(session.calls) == 1 and not delivery.state(payload)


@pytest.mark.parametrize("status", [200, 403])
def test_unaudited_init_rejection_has_scoped_proof_and_can_use_verified_backup(account, payload, status):
    session = Session([creator(), Response({"error": {"code": "unaudited_client_can_only_post_to_private_accounts"}}, status_code=status)])
    saved = []
    payload["_save_checkpoint"] = lambda state: saved.append(copy.deepcopy(state))
    with pytest.raises(pp.PreparedPublishError) as error:
        pp.publish_tiktok_sync(account, payload, session=session)
    assert error.value.details["nativePublicRestriction"] is True
    assert fc.backup_eligible(error.value, checkpoint=saved[-1])
    assert not delivery.has_unresolved_submission({"_delivery": saved[-1]})
    assert not saved[-1].get("provider_receipt")
    assert saved[-1]["definitive_non_delivery"]["operation_generation"] == saved[-1]["operation_generation"]


@pytest.mark.parametrize("failure", [TimeoutError("synthetic lost response"), Response({}), Response(status_code=503)])
def test_lost_or_missing_init_receipt_never_initializes_again(account, payload, failure):
    session = Session([creator(), failure])
    with pytest.raises(delivery.DeliveryUnknown):
        pp.publish_tiktok_sync(account, payload, session=session)
    assert delivery.has_unresolved_submission(payload)
    resumed = Session([])
    with pytest.raises(delivery.DeliveryUnknown):
        pp.publish_tiktok_sync(account, payload, session=resumed)
    assert resumed.calls == []


def test_http_200_status_business_error_is_not_completion():
    session = Session([Response({"error": {"code": "access_token_invalid", "message": "invalid credential"}})])
    with pytest.raises(pp.PreparedPublishError) as error:
        pp.fetch_tiktok_publish_status("synthetic", "tracking-receipt", session=session)
    assert not error.value.retryable
    assert not error.value.details.get("definitiveNonDelivery")


def test_confirmed_domain_rejection_can_use_exact_local_bytes(account, payload, tmp_path, monkeypatch):
    path = tmp_path / "prepared.mp4"
    path.write_bytes(b"synthetic-video-bytes")
    payload["artifacts"][0]["local_path"] = str(path)
    monkeypatch.setattr(pp.media_pipeline, "probe_video_duration", lambda *_: 6)
    session = Session([creator(), Response({"error": {"code": "url_ownership_unverified"}}),
                       Response({"data": {"publish_id": "tracking-file", "upload_url": "https://upload.invalid/prepared"}}), Response()])
    result = pp.publish_tiktok_sync(account, payload, session=session)
    assert result["request"]["source_info"]["source"] == "FILE_UPLOAD"
    assert "post_mode" not in result["request"]
    assert session.calls[-1][0] == "PUT" and session.calls[-1][2]["data"] == path.read_bytes()
    assert delivery.receipt_for(payload, "tiktok")["id"] == "tracking-file"


@pytest.mark.parametrize("bad", ["['123']", "123", [True], ["delivered"], [0], [{"id": "123"}]])
def test_public_ids_never_accept_stringified_lists_or_placeholders(bad):
    with pytest.raises(ValueError):
        tt.public_post_ids({"publicaly_available_post_id": bad})


def test_public_id_list_is_ordered_unique_and_structured():
    assert tt.public_post_ids({"publicaly_available_post_id": ["123", 456, "123"]}) == ["123", "456"]


@pytest.fixture
def poll(account, payload, monkeypatch):
    delivery.accepted(payload, provider="tiktok", receipt_id="tracking-receipt", publish_mode="direct",
                      privacy_level=tt.PUBLIC, creator_username="fixture_creator")
    status = Mock()
    monkeypatch.setattr(pp, "_ensure_tiktok_access_token", lambda *_: ("synthetic-token", None))
    monkeypatch.setattr(pp, "fetch_tiktok_publish_status", status)
    upsert = Mock()
    monkeypatch.setattr(jobs, "upsert_tiktok_publish_status", upsert)
    publish = Mock(side_effect=AssertionError("Receipt polling must never initialize another post"))
    monkeypatch.setattr(pp, "publish_tiktok_sync", publish)
    target = SimpleNamespace(job_id=1)
    def run(data):
        status.return_value = {"data": data, "error": {"code": "ok"}}
        return asyncio.run(worker._publish_prepared_tiktok("tiktok", payload, target, account=account, account_file=None))
    return run, upsert, publish


def test_public_completion_waits_for_moderation_then_retains_exact_network_ids(poll, account, payload):
    run, upsert, publish = poll
    account.config["privacyLevel"] = "SELF_ONLY"  # Account drift cannot rewrite original receipt intent.
    with pytest.raises(delivery.DeliveryPending, match="moderation"):
        run({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": []})
    receipt = delivery.receipt_for(payload, "tiktok")
    assert receipt["status"] == "awaiting_public_moderation" and receipt["privacy_level"] == tt.PUBLIC
    assert upsert.call_args.kwargs["post_id"] is None
    run({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": ["123456789", "987654321"]})
    receipt = delivery.receipt_for(payload, "tiktok")
    assert receipt["status"] == "delivered" and receipt["publication_visibility"] == "public"
    assert receipt["public_post_ids"] == ["123456789", "987654321"]
    assert upsert.call_args.kwargs["post_id"] == "123456789"
    assert upsert.call_args.kwargs["platform_url"] == "https://www.tiktok.com/@fixture_creator/video/123456789"
    publish.assert_not_called()


def test_intentional_private_completion_is_labelled_private(poll, payload):
    run, upsert, _ = poll
    payload["_delivery"]["provider_receipt"]["privacy_level"] = "SELF_ONLY"
    run({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": []})
    receipt = delivery.receipt_for(payload, "tiktok")
    assert receipt["status"] == "delivered" and receipt["publication_visibility"] == "self_only"
    assert upsert.call_args.kwargs["post_id"] is None


def test_legacy_unknown_visibility_is_review_not_a_second_post(poll, payload):
    run, _, publish = poll
    payload["_delivery"]["provider_receipt"].pop("publish_mode")
    payload["_delivery"]["provider_receipt"].pop("privacy_level")
    payload.pop("tiktokPostSettings")
    with pytest.raises(delivery.DeliveryUnknown, match="original visibility"):
        run({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": []})
    assert delivery.receipt_for(payload, "tiktok")["id"] == "tracking-receipt"
    publish.assert_not_called()


def test_inbox_handoff_requires_creator_action_without_reposting(poll, payload):
    run, _, publish = poll
    payload["_delivery"]["provider_receipt"].update(publish_mode="inbox", privacy_level=None)
    with pytest.raises(delivery.DeliveryUnknown, match="creator inbox"):
        run({"status": "SEND_TO_USER_INBOX", "publicaly_available_post_id": []})
    assert delivery.receipt_for(payload, "tiktok")["status"] == "needs_review"
    publish.assert_not_called()


def test_inbox_review_and_its_actionable_alert_are_durable(tmp_path, monkeypatch):
    path = tmp_path / "inbox.db"
    createTable.bootstrap(path)
    profile = profiles.create_profile("Inbox fixture", db_path=path)
    account = profiles.add_account(profile.id, "tiktok", "Inbox fixture", auth_type="oauth",
        config={"accessToken": "synthetic-token", "openId": "synthetic-open-id"}, db_path=path)
    job = jobs.enqueue_job(jobs.JobSpec(platform="tiktok", profile_id=profile.id,
        payload={"message": "The original caption stays available for the creator's final edit.", "tiktokDirectPost": False},
        targets=[(f"account:{account.id}", "campaign_post:1", None)]), db_path=path)
    monkeypatch.setattr(pp, "_ensure_tiktok_access_token", lambda *_: ("synthetic-token", None))
    monkeypatch.setattr(pp, "fetch_tiktok_publish_status", lambda *a, **k: {"data": {"status": "SEND_TO_USER_INBOX"}})
    async def execute(platform, data, target):
        delivery.accepted(data, provider="tiktok", receipt_id="inbox-receipt", publish_mode="inbox", privacy_level=None)
        await worker._publish_prepared_tiktok(platform, data, target, account=account, account_file=None)
    instance = worker.PublishWorker(execute, db_path=path,
        config=worker.WorkerConfig(poll_interval=.001, max_concurrent=1))
    asyncio.run(instance.drain())
    target = jobs.list_targets(job.id, db_path=path)[0]
    assert target.status == "needs_review" and target.checkpoint["provider_receipt"]["id"] == "inbox-receipt"
    with closing(sqlite3.connect(path)) as conn:
        subject, body = conn.execute("SELECT subject,body FROM publish_alert_outbox").fetchone()
    assert "needs_review" in subject and "creator inbox" in body and "Do not resubmit" in body
    assert "synthetic-token" not in body


@pytest.mark.parametrize("status,code", [(200, "internal_error"), (400, "unknown_business_error"), (503, "invalid_params")])
def test_uncertain_business_or_server_error_cannot_authorize_backup(account, payload, status, code):
    session = Session([creator(), Response({"error": {"code": code}}, status_code=status)])
    with pytest.raises(delivery.DeliveryUnknown):
        pp.publish_tiktok_sync(account, payload, session=session)
    assert delivery.has_unresolved_submission(payload)
    assert not delivery.state(payload).get("definitive_non_delivery")
    assert not fc.backup_eligible(delivery.DeliveryUnknown("uncertain"), checkpoint=delivery.state(payload))


@pytest.mark.parametrize("privacy", [None, "", False, 123, {"value": tt.PUBLIC}])
def test_invalid_explicit_privacy_never_falls_back_to_an_account_default(account, payload, privacy):
    payload["tiktokPostSettings"]["privacyLevel"] = privacy
    session = Session([])
    with pytest.raises(pp.PreparedPublishError, match="privacy"):
        pp.publish_tiktok_sync(account, payload, session=session)
    assert session.calls == []


@pytest.mark.parametrize("bad_receipt", [{"publish_mode": []}, {"publish_mode": "direct", "privacy_level": {}}])
def test_invalid_receipt_intent_requires_reconciliation(bad_receipt):
    with pytest.raises(ValueError):
        tt.receipt_intent(bad_receipt, {})


def test_provider_confirmed_failure_finalizes_now_without_backup_or_24h_polling(tmp_path, monkeypatch):
    path = tmp_path / "confirmed-failure.db"
    createTable.bootstrap(path)
    profile = profiles.create_profile("Failure fixture", db_path=path)
    account = profiles.add_account(profile.id, "tiktok", "Failure fixture", auth_type="oauth",
        config={"accessToken": "synthetic-token", "openId": "synthetic-open-id"}, db_path=path)
    job = jobs.enqueue_job(jobs.JobSpec(platform="tiktok", profile_id=profile.id,
        payload={"message": "The original caption remains unchanged after a provider failure.", "tiktokDirectPost": True},
        targets=[(f"account:{account.id}", "campaign_post:1", None)]), db_path=path)
    monkeypatch.setattr(pp, "_ensure_tiktok_access_token", lambda *_: ("synthetic-token", None))
    monkeypatch.setattr(pp, "fetch_tiktok_publish_status", lambda *a, **k: {
        "data": {"status": "FAILED", "fail_reason": "video_format_check_failed"}})
    backup = Mock(side_effect=AssertionError("A failed receipt must not be reposted automatically"))
    monkeypatch.setattr(worker.sociamonials_fallback, "publish_via_sociamonials", backup)
    async def execute(platform, data, target):
        delivery.accepted(data, provider="tiktok", receipt_id="failed-receipt", publish_mode="direct", privacy_level=tt.PUBLIC)
        await worker._publish_prepared_tiktok(platform, data, target, account=account, account_file=None)
    instance = worker.PublishWorker(execute, db_path=path,
        config=worker.WorkerConfig(poll_interval=.001, max_concurrent=1))
    asyncio.run(instance.drain())
    target = jobs.list_targets(job.id, db_path=path)[0]
    assert target.status == "failed" and target.next_attempt_at is None and target.finished_at is not None
    assert target.checkpoint["provider_receipt"]["id"] == "failed-receipt"
    assert target.checkpoint["provider_receipt"]["terminal_failure"] is True
    assert target.checkpoint["publish_routes"]["native"]["outcome"] == "failed"
    backup.assert_not_called()
    with closing(sqlite3.connect(path)) as conn:
        subject, body = conn.execute("SELECT subject,body FROM publish_alert_outbox").fetchone()
        assert conn.execute("SELECT COUNT(*) FROM publish_alert_outbox").fetchone()[0] == 1
    assert "Publish failed" in subject and "video_format_check_failed" in body
    assert "synthetic-token" not in body
