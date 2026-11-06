"""TikTok request intent and delivery interpretation, without credentials or I/O.

A publish_id is a tracking receipt, not a public network post ID. TikTok's
misspelled publicaly_available_post_id field is a list, populated after public
moderation. Preserve the mode/visibility at submission rather than infer it
from an account configuration that may have changed since then.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

PUBLIC = "PUBLIC_TO_EVERYONE"
PRIVACY_LEVELS = frozenset({PUBLIC, "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"})
_POST_ID = re.compile(r"[0-9]{1,32}")
_DEFINITE_INIT_ERRORS = frozenset({
    "invalid_params", "unaudited_client_can_only_post_to_private_accounts", "url_ownership_unverified",
    "scope_not_authorized", "access_token_invalid", "privacy_level_option_mismatch",
    "spam_risk_user_banned_from_posting", "spam_risk_too_many_posts", "rate_limit_exceeded",
})


def definite_init_rejection(status_code: int, error_code: str | None) -> bool:
    """Unknown business errors and HTTP 5xx cannot authorize another create."""
    return type(status_code) is int and (status_code in {401, 429} or (
        200 <= status_code < 500 and isinstance(error_code, str) and error_code in _DEFINITE_INIT_ERRORS))


def request_intent(config: Mapping, payload: Mapping) -> dict:
    direct = payload.get("tiktokDirectPost")
    if direct is not None and type(direct) is not bool:
        raise ValueError("TikTok per-post mode must be an explicit boolean")
    if type(direct) is bool:
        mode = "direct" if direct else "inbox"
    else:
        configured = str(config.get("publishMode") or "direct").strip().lower()
        if configured not in {"direct", "draft", "inbox"}:
            raise ValueError("TikTok publishing mode must be direct or an explicit inbox upload")
        mode = "direct" if configured == "direct" else "inbox"
    settings = payload.get("tiktokPostSettings")
    if settings is None:
        settings = {}
    if not isinstance(settings, Mapping):
        raise ValueError("TikTok per-post settings must be an object")
    if "consentChecked" in settings and settings["consentChecked"] is not True:
        raise ValueError("TikTok creator consent was not confirmed; review this post before publishing")
    privacy = None
    if mode == "direct":
        raw = settings.get("privacyLevel") if "privacyLevel" in settings else config.get("privacyLevel", "SELF_ONLY")
        if not isinstance(raw, str) or raw.strip().upper() not in PRIVACY_LEVELS:
            raise ValueError("TikTok requested privacy level is invalid")
        privacy = raw.strip().upper()
    return {"publish_mode": mode, "privacy_level": privacy}


def receipt_intent(receipt: Mapping, payload: Mapping) -> dict | None:
    """Only stored submission intent or explicit original per-post settings.

    Never consult current account defaults for an old receipt. Legacy inbox
    mode is explicit even without privacy; legacy direct receipts with no
    chosen visibility require reconciliation if no public IDs are returned.
    """
    if "publish_mode" in receipt:
        mode = receipt["publish_mode"]
        privacy = receipt.get("privacy_level")
        if (not isinstance(mode, str) or mode not in {"direct", "inbox"}
                or (mode == "direct" and (not isinstance(privacy, str) or privacy not in PRIVACY_LEVELS))):
            raise ValueError("TikTok receipt has invalid stored mode or visibility; reconcile it before retrying")
        return {"publish_mode": mode, "privacy_level": privacy}
    if payload.get("tiktokDirectPost") is False:
        return {"publish_mode": "inbox", "privacy_level": None}
    settings = payload.get("tiktokPostSettings") or {}
    if (payload.get("tiktokDirectPost") is True and isinstance(settings, Mapping)
            and settings.get("privacyLevel") in PRIVACY_LEVELS):
        return {"publish_mode": "direct", "privacy_level": settings["privacyLevel"]}
    return None


def public_post_ids(data: Mapping) -> list[str]:
    """Normalize the documented list, rejecting stringified lists/placeholders."""
    raw = data.get("publicaly_available_post_id", data.get("publicly_available_post_id", []))
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError("TikTok public post IDs are not a valid list; reconcile the stored receipt")
    result = []
    for value in raw:
        if type(value) not in {str, int}:
            raise ValueError("TikTok returned an invalid public post ID")
        text = str(value).strip()
        if not _POST_ID.fullmatch(text) or int(text) == 0:
            raise ValueError("TikTok returned an unusable public post ID")
        if text not in result:
            result.append(text)
    return result


def delivery_view(data: Mapping, intent: Mapping | None) -> dict:
    if not isinstance(data, Mapping):
        raise ValueError("TikTok status response is not an object")
    status = str(data.get("status") or "processing").strip().lower()
    ids = public_post_ids(data)
    view = {"provider_status": status, "public_post_ids": ids, "state": "pending"}
    if status == "failed":
        return {**view, "state": "failed", "reason": "TikTok confirmed the stored publication failed"}
    if status == "send_to_user_inbox":
        return {**view, "state": "needs_review", "reason": (
            "TikTok uploaded this video to the creator inbox, not the public feed. "
            "Open TikTok's inbox notification, review the original caption/settings in SAU, and finish posting. "
            "Do not resubmit this stored receipt."
        )}
    if status != "publish_complete":
        return {**view, "reason": "TikTok is still processing the stored publish receipt"}
    if intent is None:
        if ids:
            return {**view, "state": "delivered", "publication_visibility": "public"}
        return {**view, "state": "needs_review", "reason": (
            "TikTok completed an old receipt with no original visibility or public post ID. "
            "Check the creator account before approving another publication."
        )}
    if intent.get("publish_mode") == "inbox":
        return {**view, "state": "delivered", "publication_visibility": "public" if ids else "creator_selected"}
    privacy = intent.get("privacy_level")
    if privacy == PUBLIC:
        if not ids:
            return {**view, "reason": (
                "TikTok completed the public request but moderation has not returned a public post ID; "
                "keep polling this receipt, do not submit through another provider."
            )}
        return {**view, "state": "delivered", "publication_visibility": "public"}
    if ids:
        return {**view, "state": "needs_review", "reason": (
            "TikTok returned public IDs for a non-public request; verify visibility without resubmitting."
        )}
    if privacy not in PRIVACY_LEVELS:
        raise ValueError("TikTok receipt has no trustworthy original visibility")
    return {**view, "state": "delivered", "publication_visibility": privacy.lower()}
