"""Keep actionable Meta diagnostics without secrets or unsafe blanket retries."""
from types import SimpleNamespace

import pytest

from myUtils import prepared_publishers as publishers


def response(error, status=400):
    def raise_error():
        if status >= 400:
            raise RuntimeError("synthetic HTTP error")
    return SimpleNamespace(status_code=status, headers={"Retry-After": "30"},
                           json=lambda: {"error": error}, text="", raise_for_status=raise_error)


@pytest.mark.parametrize("code,transient,retryable", [
    (1, True, True), (1, False, False), (1, None, False),
    (190, True, False), (190, False, False), (2, "true", False),
])
def test_meta_400_retryability_uses_explicit_boolean_except_revoked_token(code, transient, retryable):
    error = {"code": code, "error_subcode": 2207032, "type": "OAuthException",
             "message": "An unknown error occurred", "fbtrace_id": "trace-fixture",
             "error_user_msg": "access_token=SECRET password=HIDDEN"}
    if transient is not None:
        error["is_transient"] = transient
    with pytest.raises(publishers.PreparedPublishError) as caught:
        publishers._raise_for_status(response(error))
    exc = caught.value
    assert exc.retryable is retryable
    assert exc.details["providerErrorCode"] == str(code)
    assert exc.details["providerErrorSubcode"] == "2207032"
    assert exc.details["providerTraceId"] == "trace-fixture"
    assert exc.details["retryAfter"] == "30"
    assert "SECRET" not in repr(exc.details) and "HIDDEN" not in repr(exc.details)


@pytest.mark.parametrize("status", ["ERROR", "EXPIRED", "FAILED"])
def test_terminal_container_error_is_recorded_as_non_delivery(status):
    result = SimpleNamespace(status_code=200, headers={}, raise_for_status=lambda: None,
                             json=lambda: {"status": status, "error_message": "UNKNOWN"})
    http = SimpleNamespace(get=lambda *_args, **_kwargs: result)
    with pytest.raises(publishers.PreparedPublishError) as caught:
        publishers._wait_for_container_status(http, "fixture-container", "fixture-token", platform="threads")
    assert caught.value.retryable is False
    assert caught.value.details["containerStatus"] == status
    assert caught.value.details["definitiveNonDelivery"] is True
