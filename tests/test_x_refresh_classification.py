"""Constrained X refresh diagnostics distinguish bad requests from retryable transport."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from myUtils import prepared_publishers as publishers, x_auth


@pytest.mark.parametrize("status", [200, 400])
@pytest.mark.parametrize("code", ["invalid_grant", "invalid_token", "refresh_token_revoked", "invalid_request", "invalid_client"])
def test_safe_refresh_codes_survive_without_raw_provider_descriptions(monkeypatch, status, code):
    monkeypatch.setenv("X_CLIENT_ID", "fixture-client")
    monkeypatch.setenv("X_CLIENT_SECRET", "fixture-secret")
    response = SimpleNamespace(status_code=status, ok=status < 400,
        json=lambda: {"error": code, "error_description": "refresh_token=do-not-record-this"})
    with pytest.raises(x_auth.TwitterOAuthError) as raised:
        x_auth.refresh_access_token(refresh_token="do-not-record-this", session=SimpleNamespace(post=lambda *a, **k: response))
    assert raised.value.error_code == code
    assert raised.value.status_code == status
    assert "do-not-record-this" not in str(raised.value)


@pytest.mark.parametrize("code,status,retryable,reconnect,classification", [
    ("invalid_grant", 400, False, True, "credential_rejected"),
    ("invalid_token", 400, False, True, "credential_rejected"),
    ("refresh_token_revoked", 400, False, True, "credential_rejected"),
    ("invalid_request", 400, False, False, "request_rejected"),
    ("invalid_client", 401, False, False, "request_rejected"),
    ("unauthorized_client", 400, False, False, "request_rejected"),
    ("unsupported_grant_type", 400, False, False, "request_rejected"),
    ("configuration_missing", None, False, False, "request_rejected"),
    ("", 400, False, False, "request_rejected"),
    ("", 429, True, False, "transient"),
    ("", 503, True, False, "transient"),
    ("", None, True, False, "transient"),
])
def test_refresh_wrapper_retains_safe_cause_and_stops_persistent_request_retries(code, status, retryable, reconnect, classification):
    config = {"twitterAuthType": "api", "accessToken": "old-fixture", "refreshToken": "fixture-refresh",
              "accessTokenExpiresAt": "2020-01-01T00:00:00+00:00"}
    error = x_auth.TwitterOAuthError("Credential-free failure", status_code=status, error_code=code)
    callback = Mock()
    with patch.object(publishers, "refresh_twitter_token_single_flight", side_effect=error):
        with pytest.raises(publishers.PreparedPublishError) as raised:
            publishers._maybe_refresh_twitter_token(config, on_refresh_failure=callback)
    assert raised.value.retryable is retryable
    assert raised.value.details == {"stage": "oauth_refresh", "status": status, "code": code,
                                   "reconnectRequired": reconnect, "classification": classification}
    assert "fixture-refresh" not in str(raised.value) + json.dumps(raised.value.details)
    if classification == "request_rejected":
        assert "temporarily" not in str(raised.value) and "reconnect this account" not in str(raised.value)
    assert callback.call_count == 1
    assert "_needsReconnect" not in config


def test_missing_client_id_is_actionable_and_makes_no_http_request(monkeypatch):
    monkeypatch.delenv("X_CLIENT_ID", raising=False)
    session = Mock()
    with pytest.raises(x_auth.TwitterOAuthError) as raised:
        x_auth.refresh_access_token(refresh_token="fixture-refresh", session=session)
    assert raised.value.error_code == "configuration_missing"
    assert not session.mock_calls


def test_refresh_configuration_classification_is_shared_with_persisted_state():
    from myUtils import token_state
    error = x_auth.TwitterOAuthError("Safe OAuth rejection", status_code=400, error_code="invalid_request")
    assert error.classification == "request_rejected"
    verdict = token_state.classify_failure(error)
    assert verdict["kind"] == "configuration" and verdict["terminal"] is False
    assert verdict["code"] == "invalid_request" and verdict["httpStatus"] == 400
