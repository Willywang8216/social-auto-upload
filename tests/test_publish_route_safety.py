"""Regression coverage for native-route holds and scoped X rejection handoffs."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from db import createTable
from myUtils import delivery, generic_delivery as journal, failure_classification as fc
from myUtils import pre_flight, profiles, token_state, x_auth
from myUtils import prepared_publishers as publishers


class Response:
    def __init__(self, status=402):
        self.status_code = status
        self.headers = {"Retry-After": "30"} if status == 429 else {}

    def json(self):
        return {"error": {"code": "CreditsDepleted", "message": "secret=do-not-record"}}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("provider URL contains access_token=do-not-record")


def rejected_media(*, status=402, text_only=False):
    payload = {"_telegramDeliveryKey": "17"}
    operations = journal.Operations(payload, "twitter")
    operations.plan(["tweet"] if text_only else ["media:0", "tweet"])
    if not text_only:
        operations.begin("media:0:initialize")
        operations.record("media:0:initialize", "media-id", provider_receipt=False)
        operations.begin("media:0:segment:0")
        operations.record("media:0:segment:0", "media-id", provider_receipt=False)
    with pytest.raises(publishers.PreparedPublishError) as raised:
        operations.request("tweet" if text_only else "media:0:finalize", lambda: Response(status))
    wrapped = publishers._raise_x_publish_error(raised.value, stage="media upload (OAuth 2.0)")
    return payload, operations, wrapped


@pytest.mark.parametrize("status", [401, 402, 429])
def test_real_rejection_survives_wrapper_and_scopes_every_media_effect(status):
    payload, _, error = rejected_media(status=status)
    original = copy.deepcopy(delivery.state(payload)["provider_operations"])
    assert fc.proven_non_delivery(error)
    assert error.details["httpStatus"] == status
    assert "do-not-record" not in str(error) + json.dumps(error.details)
    assert delivery.has_unresolved_submission(payload)
    assert journal.authorize_media_only_handoff(payload, error, target_id=17)
    assert journal.media_only_reconciled(delivery.state(payload), target_id=17)
    assert not journal.media_only_reconciled(delivery.state(payload), target_id=18)
    assert not delivery.has_unresolved_submission(payload)
    assert fc.backup_eligible(error, checkpoint=delivery.state(payload))
    assert delivery.state(payload)["provider_operations"] == original
    restored = {"_delivery": json.loads(json.dumps(delivery.state(payload)))}
    assert not delivery.has_unresolved_submission(restored)


def test_rejected_text_only_tweet_has_no_earlier_publication():
    payload, _, error = rejected_media(text_only=True)
    assert journal.authorize_media_only_handoff(payload, error, target_id=17)
    assert not delivery.has_unresolved_submission(payload)


@pytest.mark.parametrize("change", [
    "receipt", "native_receipt", "completed", "other_provider", "tweet_item",
    "legacy_effect", "processing", "changed_id", "changed_generation", "changed_proof",
])
def test_changed_or_publication_bearing_journal_never_uses_saved_handoff(change):
    payload, _, error = rejected_media()
    assert journal.authorize_media_only_handoff(payload, error, target_id=17)
    state = delivery.state(payload)
    items = state["provider_operations"]["twitter"]["items"]
    if change == "receipt":
        state["provider_receipt"] = {"provider": "twitter", "id": "tweet-id"}
    elif change == "native_receipt":
        state["native_provider_receipt"] = {"provider": "twitter", "id": "tweet-id"}
    elif change == "completed":
        state["completed_operations"] = {"tweet": "tweet-id"}
    elif change == "other_provider":
        state["provider_operations"]["facebook"] = {"items": {}}
    elif change == "tweet_item":
        items["tweet"] = {"id": "tweet-id", "status": "complete", "effect": "media_only"}
    elif change == "legacy_effect":
        items["media:0:initialize"].pop("effect")
    elif change == "processing":
        items["media:0:initialize"]["status"] = "processing"
    elif change == "changed_id":
        items["media:0:initialize"]["id"] = "different-media"
    elif change == "changed_generation":
        state["provider_operations"]["twitter"]["last_rejection"]["operation_generation"] = "a" * 32
    elif change == "changed_proof":
        state["backup_authorization"]["operation_receipts"]["twitter"]["media:0:initialize"]["no_publication"] = False
    assert not journal.media_only_reconciled(state, target_id=17)
    assert delivery.has_unresolved_submission(payload)
    assert not fc.backup_eligible(error, checkpoint=state)


def test_new_request_invalidates_scoped_rejection():
    payload, operations, error = rejected_media()
    assert journal.authorize_media_only_handoff(payload, error, target_id=17)
    operations.begin("tweet")
    assert delivery.has_unresolved_submission(payload)
    assert not journal.authorize_media_only_handoff(payload, error, target_id=17)


def test_untrusted_http_text_cannot_authorize_media_handoff():
    payload, _, _ = rejected_media()
    error = publishers._raise_x_publish_error(
        publishers.PreparedPublishError("HTTP 402 arbitrary message", retryable=False), stage="media upload")
    assert not fc.proven_non_delivery(error)
    assert not journal.authorize_media_only_handoff(payload, error, target_id=17)


def test_unknown_tweet_create_response_remains_protected():
    payload = {"_telegramDeliveryKey": "17"}
    operations = journal.Operations(payload, "twitter")
    operations.plan(["media:0", "tweet"])
    operations.begin("media:0")
    operations.record("media:0", "media-id", provider_receipt=False)
    def lost():
        raise TimeoutError("Response lost")
    with pytest.raises(delivery.DeliveryUnknown):
        operations.request("tweet", lost)
    assert delivery.has_unresolved_submission(payload)
    assert not journal.authorize_media_only_handoff(
        payload, publishers.PreparedPublishError("HTTP 402", details={"httpStatus": 402, "definitiveNonDelivery": True}), target_id=17)


@pytest.fixture
def account_db(tmp_path):
    path = tmp_path / "route-state.db"
    createTable.bootstrap(path)
    profile = profiles.create_profile("Isolated route", db_path=path)
    account = profiles.add_account(profile.id, platform="twitter", account_name="Fixture",
        auth_type="oauth", config={"twitterAuthType": "api", "accessToken": "fixture-access",
        "refreshToken": "fixture-refresh", "accessTokenExpiresAt": "2040-01-01T00:00:00+00:00"}, db_path=path)
    return path, account


@pytest.mark.parametrize("code,status", [("invalid_request", 400), ("invalid_client", 401), ("configuration_missing", None), ("", 400)])
def test_configuration_verdict_blocks_native_without_claiming_revocation(account_db, code, status):
    path, account = account_db
    error = x_auth.TwitterOAuthError("Safe OAuth failure", error_code=code, status_code=status)
    saved = token_state.commit_failure(account, error, db_path=path)
    assert saved.config["_nativeCredentialError"]["kind"] == "configuration"
    assert saved.config["_nativeCredentialState"] == "configuration_required"
    assert not saved.config.get("_needsReconnect")
    assert saved.config["accessToken"] == account.config["accessToken"]
    check = pre_flight.run_pre_flight(account_ref=f"account:{account.id}", job_id=1,
        platform="twitter", payload={}, db_path=path)
    assert not check.ok and check.checks["native_route_available"] is False
    managed = pre_flight.run_pre_flight(account_ref=f"account:{account.id}", job_id=1,
        platform="twitter", payload={}, db_path=path, validate_credentials=False)
    assert managed.ok
    assert not token_state.native_state(saved)["reconnectRequired"]


def test_old_invalid_request_warning_is_actionable_but_outage_is_not():
    legacy = {"_nativeCredentialError": {"kind": "unknown", "code": "invalid_request", "httpStatus": 400}}
    assert token_state.native_route_block_reason(legacy)
    legacy["_nativeCredentialError"]["httpStatus"] = 503
    assert not token_state.native_route_block_reason(legacy)


def test_budget_hold_survives_credential_validation_and_preserves_identity(account_db):
    path, account = account_db
    saved = token_state.commit_publishing_failure(account, token_state.provider_error({}, status_code=402), db_path=path)
    assert saved.config["_nativePublishingError"]["kind"] == "budget"
    assert "_needsReconnect" not in saved.config
    assert "_nativeCredentialError" not in saved.config
    assert saved.auth_type == account.auth_type and saved.enabled == account.enabled
    validated = token_state.commit_validation(saved, {}, db_path=path)
    assert token_state.native_state(validated)["state"] == "valid"
    assert token_state.native_route_block_reason(validated.config)
    assert token_state.publishing_route(validated, token_state.native_state(validated))["state"] == "blocked"
    delivered = token_state.commit_publication_success(validated, db_path=path)
    assert not token_state.native_route_block_reason(delivered.config)


def test_stale_budget_failure_cannot_poison_reconnected_account(account_db):
    path, account = account_db
    current = token_state.commit_success(account, {"accessToken": "new-fixture-access"}, db_path=path)
    assert token_state.commit_publishing_failure(account, token_state.provider_error({}, status_code=402), db_path=path) is None
    assert "_nativePublishingError" not in profiles.get_account(current.id, db_path=path).config


def test_actual_x_publish_budget_rejection_is_saved_without_touching_credentials(account_db, monkeypatch):
    path, account = account_db
    session = SimpleNamespace(post=lambda *args, **kwargs: Response(402))
    monkeypatch.setattr(publishers, "_get_session", lambda supplied=None: session)
    with pytest.raises(publishers.PreparedPublishError) as raised:
        publishers.publish_twitter_sync(account, {"message": "Original fixture copy", "_db_path": str(path)}, session=session)
    assert raised.value.details["definitiveNonDelivery"] is True
    saved = profiles.get_account(account.id, db_path=path)
    assert saved.config["_nativePublishingError"]["kind"] == "budget"
    assert saved.config["accessToken"] == "fixture-access"
    assert saved.config["refreshToken"] == "fixture-refresh"


def test_configuration_hold_survives_a_later_transport_failure(account_db):
    path, account = account_db
    saved = token_state.commit_failure(account, x_auth.TwitterOAuthError(
        "Safe request rejection", status_code=400, error_code="invalid_request"), db_path=path)
    later = token_state.commit_failure(saved, TimeoutError("Transport unavailable"), db_path=path)
    assert later.config["_nativeCredentialError"]["kind"] == "configuration"
    assert later.config["_nativeCredentialState"] == "configuration_required"
    assert "retry scheduled" in later.config["_lastMaintenanceError"]
    assert token_state.native_route_block_reason(later.config)
    repaired = token_state.commit_success(later, {"accessToken": "verified-new-token"}, db_path=path)
    assert not token_state.native_route_block_reason(repaired.config)


def test_native_oauth_budget_verdict_is_actionable_without_revocation():
    verdict = token_state.classify_failure(x_auth.TwitterOAuthError("Safe response", status_code=402))
    assert verdict["kind"] == "budget" and verdict["terminal"] is False
    assert token_state.native_route_block_reason({"_nativeCredentialError": verdict})


@pytest.mark.parametrize("operation", ["", "twitter:tweet", "sociamonials-create-post"])
def test_retained_native_proof_never_resolves_a_new_unknown_submission(operation):
    payload, _, error = rejected_media()
    assert journal.authorize_media_only_handoff(payload, error, target_id=17)
    delivery.checkpoint(payload, operation=operation, operation_generation="b" * 32, submission_started=True)
    assert journal.media_only_reconciled(delivery.state(payload), target_id=17)
    assert delivery.has_unresolved_submission(payload)
    assert not fc.backup_eligible(error, checkpoint=delivery.state(payload))
    assert not journal.authorize_media_only_handoff(payload, error, target_id=17)


def test_backup_rejection_keeps_native_proof_but_new_backup_fence_is_protected():
    payload, _, error = rejected_media()
    assert journal.authorize_media_only_handoff(payload, error, target_id=17)
    delivery.starting(payload, operation="sociamonials-create-post")
    delivery.checkpoint(payload, submission_started=False)
    assert journal.media_only_reconciled(delivery.state(payload), target_id=17)
    assert not delivery.has_unresolved_submission(payload)
    delivery.starting(payload, operation="sociamonials-create-post")
    assert delivery.has_unresolved_submission(payload)
    delivery.record_non_delivery(payload, provider="sociamonials", reason="Explicit rejection")
    assert not delivery.has_unresolved_submission(payload)
    delivery.starting(payload, operation="sociamonials-create-post")
    assert delivery.has_unresolved_submission(payload)


def test_stale_http_rejection_cannot_authorize_a_new_native_generation():
    payload, operations, old_error = rejected_media()
    with pytest.raises(publishers.PreparedPublishError) as raised:
        operations.request("media:0:finalize", lambda: Response(402))
    new_error = publishers._raise_x_publish_error(raised.value, stage="media upload")
    assert old_error.details["operationGeneration"] != new_error.details["operationGeneration"]
    assert not journal.authorize_media_only_handoff(payload, old_error, target_id=17)
    assert journal.authorize_media_only_handoff(payload, new_error, target_id=17)
