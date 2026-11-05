"""Central failure taxonomy for the publish pipeline.

Every publish failure is classified into one of three categories so the worker
can make smart decisions about retry, fallback, and alerting instead of
treating a dead OAuth token the same as a network timeout.

Categories
----------
TRANSIENT   — The failure is expected to resolve on its own. Retry with
              backoff. Examples: network timeout, HTTP 5xx, rate limit (429),
              platform temporarily unavailable.

PERMANENT   — The native failure will not resolve by retrying. Stop native
              retries; a separately verified backup is governed by
              backup_eligible, not this category alone.

RECOVERABLE — The failure can be fixed by an automated action (token refresh,
              media restore, re-host). Attempt recovery once, then retry the
              publish. Examples: expired OAuth token (refresh first), missing
              local artifact (restore from remote), stale cookie.

The classification is determined by :func:`classify_failure`, which inspects
the exception type, message, and HTTP status code. Publishers can also raise
``PreparedPublishError(retryable=False)`` to signal PERMANENT directly.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any


class FailureCategory(Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    RECOVERABLE = "recoverable"


# Aliases for readability at call sites.
TRANSIENT = FailureCategory.TRANSIENT
PERMANENT = FailureCategory.PERMANENT
RECOVERABLE = FailureCategory.RECOVERABLE


class ClassifiedFailure(Exception):
    """An exception with an explicit failure category.

    Publishers and the worker use this to communicate *why* a target failed,
    not just *that* it failed. The ``retryable`` property on
    ``PreparedPublishError`` is the legacy version of this; new code should
    use ``ClassifiedFailure`` directly.
    """

    def __init__(
        self,
        message: str,
        category: FailureCategory = TRANSIENT,
        *,
        platform: str = "",
        account_ref: str = "",
        details: dict[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.platform = platform
        self.account_ref = account_ref
        self.details = details or {}
        if cause is not None and isinstance(cause, BaseException):
            self.__cause__ = cause

    @property
    def is_retryable(self) -> bool:
        return self.category in {TRANSIENT, RECOVERABLE}

    @property
    def is_recoverable(self) -> bool:
        return self.category is RECOVERABLE


# ---------------------------------------------------------------------------
# Pattern-based classification heuristics
# ---------------------------------------------------------------------------

# Patterns that indicate a dead credential requiring human re-auth.
_CREDENTIAL_DEAD_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"HTTP 401", re.IGNORECASE),
    re.compile(r"Unauthorized", re.IGNORECASE),
    re.compile(r"session (?:key is )?(?:malformed|invalidated|expired)", re.IGNORECASE),
    re.compile(r"invalid_grant", re.IGNORECASE),
    re.compile(r"refresh_token_revoked", re.IGNORECASE),
    re.compile(r"credentials? (?:are )?(?:invalid|revoked|expired)", re.IGNORECASE),
    re.compile(r"re-?authoris", re.IGNORECASE),
    re.compile(r"re-?authoriz", re.IGNORECASE),
    re.compile(r"Please log in", re.IGNORECASE),
    re.compile(r"access_denied", re.IGNORECASE),
    re.compile(r"Error validating access token", re.IGNORECASE),
)

# Patterns that indicate the account row is gone.
_ACCOUNT_GONE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Account not found", re.IGNORECASE),
    re.compile(r"account (?:does not exist|deleted|disabled)", re.IGNORECASE),
)

_ACCOUNT_BANNED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:user|account)[_ -](?:is |has been )?(?:banned|suspended)\b", re.IGNORECASE),
    re.compile(r"\bbanned[_ -](?:user|account)\b", re.IGNORECASE),
)

# Patterns that indicate missing or invalid media.
_MEDIA_INVALID_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"artifacts? were supplied but none is", re.IGNORECASE),
    re.compile(r"no (?:usable |supported )?(?:media|image|video)", re.IGNORECASE),
    re.compile(r"(?:video|media) (?:file )?(?:is missing|not found|does not exist)", re.IGNORECASE),
    re.compile(r"exceeds the \d+ MB limit", re.IGNORECASE),
    re.compile(r"duration \d+s exceeds the \d+s limit", re.IGNORECASE),
    re.compile(r"Unsupported .+ media type", re.IGNORECASE),
    re.compile(r"requires a video artifact", re.IGNORECASE),
    re.compile(r"requires video media", re.IGNORECASE),
)

# Patterns that indicate a content/policy rejection (permanent).
_CONTENT_POLICY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"content[- ]guard", re.IGNORECASE),
    re.compile(r"does not match account language", re.IGNORECASE),
    re.compile(r"placeholder/generic copy", re.IGNORECASE),
    re.compile(r"community guidelines", re.IGNORECASE),
    re.compile(r"content policy", re.IGNORECASE),
)

# Patterns that indicate a recoverable state (refresh/restore can fix it).
_RECOVERABLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"MediaRestoreError", re.IGNORECASE),
    re.compile(r"artifact.*not.*materiali[sz]ed", re.IGNORECASE),
    re.compile(r"local media missing", re.IGNORECASE),
    re.compile(r"token (?:expired|stale).*refresh", re.IGNORECASE),
    re.compile(r"cookie (?:已失效|invalid|expired)", re.IGNORECASE),
)

# Patterns that indicate transient issues (network, rate limit, server error).
_TRANSIENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"HTTP (?:5\d\d)", re.IGNORECASE),
    re.compile(r"HTTP 429", re.IGNORECASE),
    re.compile(r"HTTP 408", re.IGNORECASE),
    re.compile(r"timeout", re.IGNORECASE),
    re.compile(r"connection (?:refused|reset|aborted|timed out)", re.IGNORECASE),
    re.compile(r"temporarily unavailable", re.IGNORECASE),
    re.compile(r"rate limit", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"cancelled mid-run", re.IGNORECASE),
)

# Patterns for operator-disabled platforms (permanent by policy).
_DISABLED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"disabled by the operator", re.IGNORECASE),
    re.compile(r"direct publish disabled", re.IGNORECASE),
    re.compile(r"publishing is disabled", re.IGNORECASE),
)


def classify_failure(
    exc: BaseException,
    *,
    platform: str = "",
    message: str | None = None,
) -> ClassifiedFailure:
    """Inspect an exception and return a :class:`ClassifiedFailure`.

    The classification priority is:
    1. Already-classified exceptions pass through unchanged.
    2. ``PreparedPublishError(retryable=False)`` → PERMANENT.
    3. Pattern matching on the error message.
    4. Default: TRANSIENT (conservative — retry rather than discard).
    """
    text = message or f"{type(exc).__name__}: {exc}"

    # 1. Already classified.
    if isinstance(exc, ClassifiedFailure):
        return exc

    # 2. Legacy retryable flag on PreparedPublishError.
    retryable = getattr(exc, "retryable", None)
    if retryable is False:
        return ClassifiedFailure(
            text, PERMANENT, platform=platform, cause=exc,
            details=getattr(exc, "details", None),
        )

    # 3. Pattern matching, in priority order.
    if _matches_any(text, _ACCOUNT_GONE_PATTERNS):
        return ClassifiedFailure(text, PERMANENT, platform=platform, cause=exc)

    if _matches_any(text, _ACCOUNT_BANNED_PATTERNS):
        return ClassifiedFailure(text, PERMANENT, platform=platform, cause=exc)

    if _matches_any(text, _DISABLED_PATTERNS):
        return ClassifiedFailure(text, PERMANENT, platform=platform, cause=exc)

    if _matches_any(text, _CONTENT_POLICY_PATTERNS):
        return ClassifiedFailure(text, PERMANENT, platform=platform, cause=exc)

    if _matches_any(text, _MEDIA_INVALID_PATTERNS):
        return ClassifiedFailure(text, PERMANENT, platform=platform, cause=exc)

    # A restore error or explicitly refreshable expiry is recoverable unless
    # the publisher marked it non-retryable. A revoked refresh token still
    # wins: no amount of retrying can repair it.
    if _matches_any(text, _RECOVERABLE_PATTERNS) and not re.search(
        r"invalid_grant|refresh_token_revoked|re-?authori[sz]", text, re.IGNORECASE
    ):
        return ClassifiedFailure(text, RECOVERABLE, platform=platform, cause=exc)

    if _matches_any(text, _CREDENTIAL_DEAD_PATTERNS):
        return ClassifiedFailure(text, PERMANENT, platform=platform, cause=exc)

    if _matches_any(text, _RECOVERABLE_PATTERNS):
        return ClassifiedFailure(text, RECOVERABLE, platform=platform, cause=exc)

    if _matches_any(text, _TRANSIENT_PATTERNS):
        return ClassifiedFailure(text, TRANSIENT, platform=platform, cause=exc)

    # 4. Default: transient (safe — retry rather than discard).
    return ClassifiedFailure(text, TRANSIENT, platform=platform, cause=exc)


def _matches_any(text: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(p.search(text) for p in patterns)


# ---------------------------------------------------------------------------
# Convenience helpers for the worker
# ---------------------------------------------------------------------------

def is_permanent(exc: BaseException, message: str | None = None) -> bool:
    """Quick check: is this failure permanent (no retry possible)?"""
    return classify_failure(exc, message=message).category is PERMANENT


def is_recoverable(exc: BaseException, message: str | None = None) -> bool:
    """Quick check: can automated recovery fix this?"""
    return classify_failure(exc, message=message).category is RECOVERABLE


def failure_summary(exc: BaseException, message: str | None = None) -> dict:
    """Return a structured summary for logging/alerting."""
    classified = classify_failure(exc, message=message)
    return {
        "category": classified.category.value,
        "message": str(classified),
        "retryable": classified.is_retryable,
        "recoverable": classified.is_recoverable,
        "platform": classified.platform,
    }


def proven_non_delivery(exc: BaseException) -> bool:
    """A response rejection is evidence; arbitrary error text is not."""
    details = getattr(exc, "details", None) or {}
    if isinstance(details, dict) and details.get("definitiveNonDelivery") is True:
        return True
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if type(status) is not int:
        return False
    if status in {401, 402, 429}:
        return True
    return status == 403 and _matches_any(str(exc), _CREDENTIAL_DEAD_PATTERNS)


def backup_eligible(exc: BaseException, *, checkpoint: dict | None = None) -> bool:
    """Native retryability is NOT backup eligibility.

    Only a separate verified connection to the same destination may repair a
    native credential/provider failure. This predicate grants no account access;
    the adapter must still verify grants, media and every intent setting.
    """
    text = f"{type(exc).__name__}: {exc}"
    state = {} if checkpoint is None else checkpoint
    details = getattr(exc, "details", None) or {}
    if not isinstance(state, dict) or state.get("checkpoint_invalid"):
        return False
    if (state.get("provider_receipt") or state.get("telegramCompletedByDelivery")
            or state.get("completed_operations") or state.get("provider_operations")):
        return False
    from myUtils import delivery
    if isinstance(exc, delivery.RouteExhausted) and isinstance(details, dict) and details.get("provider") == "native":
        routes = state.get("publish_routes") or {}
        if not isinstance(routes, dict) or not isinstance(routes.get("native", {}), dict):
            return False
        prior_error = routes.get("native", {}).get("error", "")
        if not isinstance(prior_error, str):
            return False
        text += " " + prior_error
    if any(_matches_any(text, patterns) for patterns in (
            _ACCOUNT_GONE_PATTERNS, _ACCOUNT_BANNED_PATTERNS, _DISABLED_PATTERNS, _CONTENT_POLICY_PATTERNS,
            _MEDIA_INVALID_PATTERNS)):
        return False
    if re.search(r"banned|user_banned|restricted by|community violation|deleted account|"
                 r"content policy|does not match|missing (?:media|file)|requires human approval", text, re.I):
        return False
    if isinstance(details, dict) and details.get("retrySafe") is False:
        return False
    credential_failure = (_matches_any(text, _CREDENTIAL_DEAD_PATTERNS)
                          or bool(re.search(r"token.{0,100}expired|reconnect (?:required|the account)|"
                                            r"requires? reconnection|media\.write|scope.not.authorized", text, re.I)))
    definite = proven_non_delivery(exc)
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    provider_budget_failure = (status_code == 402 or bool(re.search(
        r"HTTP 402|CreditsDepleted|insufficient (?:provider )?credits|credit balance exhausted", text, re.I)))
    rejected_auth = credential_failure and status_code in {401, 403}
    rejected_budget = provider_budget_failure and status_code == 402
    persisted_rejection = not delivery.has_unresolved_submission({"_delivery": state})
    exhausted_native = (isinstance(exc, delivery.RouteExhausted)
                        and isinstance(details, dict) and details.get("provider") == "native")
    if state.get("submission_started") and not (definite or rejected_auth or rejected_budget or persisted_rejection):
        return False
    if credential_failure or provider_budget_failure or definite or exhausted_native:
        return True
    return classify_failure(exc).category in {TRANSIENT, RECOVERABLE}
