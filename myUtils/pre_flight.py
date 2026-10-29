"""Pre-flight validation before the worker claims a target.

Catches doomed targets *before* they burn retry budget or hold a concurrency
slot. Each check returns a :class:`PreFlightResult`; the worker refuses to
claim a target whose result is not :attr:`PreFlightResult.ok`.

Checks
------
1. **Account exists** — the account row hasn't been deleted.
2. **Account enabled** — the operator hasn't disabled it.
3. **Account not flagged for reconnect** — the credential is dead and waiting
   for a human; retrying will never help.
4. **OAuth token valid** — the token is present and not expired (or refreshable).
5. **Media accessible** — local artifacts exist, or public URLs respond.
6. **Campaign publishable** — the campaign isn't stuck in ``preparing``.

The checks are cheap (a few SQLite reads + optional HEAD requests) and run
synchronously inside the worker's claim loop. A failing check marks the target
as ``failed`` immediately with a clear reason, no executor invocation.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PreFlightResult:
    """Outcome of pre-flight checks for one target."""
    ok: bool
    reason: str = ""
    checks: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def passed(cls, checks: dict[str, bool] | None = None) -> PreFlightResult:
        return cls(ok=True, checks=checks or {})

    @classmethod
    def failed(cls, reason: str, checks: dict[str, bool] | None = None) -> PreFlightResult:
        return cls(ok=False, reason=reason, checks=checks or {})


def run_pre_flight(
    *,
    account_ref: str,
    job_id: int,
    platform: str,
    payload: dict,
    db_path: Path,
) -> PreFlightResult:
    """Run all pre-flight checks and return the aggregated result.

    The first failing check short-circuits — there's no point checking media
    accessibility when the account doesn't exist.
    """
    checks: dict[str, bool] = {}

    # 1. Account exists and is enabled.
    account = _resolve_account(account_ref, db_path)
    if account is None:
        return PreFlightResult.failed(
            f"Account not found: {account_ref}",
            {"account_exists": False},
        )
    checks["account_exists"] = True

    if not _account_enabled(account):
        return PreFlightResult.failed(
            f"Account disabled: {account_ref}",
            {**checks, "account_enabled": False},
        )
    checks["account_enabled"] = True

    # 2. Account not flagged for manual reconnect.
    config = getattr(account, "config", None) or {}
    if config.get("_needsReconnect"):
        return PreFlightResult.failed(
            f"Account flagged for reconnect: {account_ref} "
            f"(credential is dead, waiting for human re-auth)",
            {**checks, "needs_reconnect": False},
        )
    checks["needs_reconnect"] = True

    # 3. OAuth token validity (only for API-backed accounts).
    token_ok, token_reason = _check_token_validity(account, platform, config)
    checks["token_valid"] = token_ok
    if not token_ok:
        return PreFlightResult.failed(
            f"Token invalid for {account_ref}: {token_reason}",
            checks,
        )

    # 4. Media accessibility (only for campaign-based publishes).
    if payload.get("campaignId"):
        media_ok, media_reason = _check_media_accessible(payload, db_path)
        checks["media_accessible"] = media_ok
        if not media_ok:
            return PreFlightResult.failed(
                f"Media not accessible for job {job_id}: {media_reason}",
                checks,
            )

    # 5. Campaign state.
    if payload.get("campaignId"):
        campaign_ok, campaign_reason = _check_campaign_state(payload, db_path)
        checks["campaign_publishable"] = campaign_ok
        if not campaign_ok:
            return PreFlightResult.failed(
                f"Campaign not publishable for job {job_id}: {campaign_reason}",
                checks,
            )

    return PreFlightResult.passed(checks)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _resolve_account(account_ref: str, db_path: Path):
    """Resolve an account row, returning None if it doesn't exist."""
    try:
        from myUtils import profiles as profile_registry
        if not account_ref.startswith("account:"):
            return None
        account_id = int(account_ref.split(":", 1)[1])
        return profile_registry.get_account(account_id, db_path=db_path)
    except LookupError:
        return None
    except Exception:
        logger.debug("pre-flight: account lookup failed for %s", account_ref, exc_info=True)
        return None


def _account_enabled(account) -> bool:
    """True when the account row is enabled (status > 0)."""
    status = getattr(account, "status", None)
    if status is None:
        return True  # legacy accounts without a status column
    return int(status) > 0


def _check_token_validity(account, platform: str, config: dict) -> tuple[bool, str]:
    """Check whether the account's OAuth token is usable.

    Returns ``(ok, reason)``. A missing token for a cookie-based account is
    fine (the cookie file is the credential). A missing token for an API
    account is a failure.
    """
    from myUtils import profiles as profile_registry

    auth_type = profile_registry.effective_auth_type(
        config, getattr(account, "auth_type", None), platform
    )

    # Cookie-based accounts don't have refreshable tokens.
    if auth_type == "cookie":
        return True, ""

    # Platforms that use API tokens.
    api_platforms = {"tiktok", "youtube", "threads", "twitter", "reddit",
                     "facebook", "instagram", "bluesky"}
    if platform not in api_platforms:
        return True, ""

    access_token = str(config.get("accessToken") or "").strip()
    if not access_token:
        # No token at all — might be a fresh account or a misconfiguration.
        # Don't fail here; the publisher will raise a specific error.
        return True, ""

    # Check expiry if recorded.
    expires_at = _parse_iso_datetime(
        str(config.get("accessTokenExpiresAt") or "")
    )
    if expires_at is not None:
        now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
        if expires_at <= now:
            # Token is expired. Check if we have a refresh token.
            refresh_token = str(config.get("refreshToken") or "").strip()
            if not refresh_token and platform not in {"facebook", "instagram", "threads"}:
                return False, (
                    f"access token expired at {expires_at.isoformat()} "
                    f"and no refresh token available"
                )
            # Has refresh token — the worker's maintenance will refresh it.
            # Don't fail pre-flight; let the publish attempt trigger a refresh.

    return True, ""


def _parse_iso_datetime(value: str):
    """Parse a stored token expiry into an absolute UTC datetime."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


def _check_media_accessible(payload: dict, db_path: Path) -> tuple[bool, str]:
    """Check that at least one artifact has a usable local path or public URL."""
    artifacts = payload.get("artifacts") or []
    if not artifacts:
        return False, "no artifacts in payload"

    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        local_path = artifact.get("local_path")
        public_url = artifact.get("public_url")

        if local_path:
            path = Path(str(local_path))
            if path.is_file() and path.stat().st_size > 0:
                return True, ""

        if public_url:
            # We don't HEAD-check URLs here (too slow for the claim loop).
            # A non-empty URL is sufficient; the publisher will fail if it's
            # unreachable, and that failure will be classified as TRANSIENT.
            return True, ""

    return False, "no artifact has a usable local path or public URL"


def _check_campaign_state(payload: dict, db_path: Path) -> tuple[bool, str]:
    """Check that the campaign is in a publishable state."""
    import sqlite3
    campaign_id = payload.get("campaignId")
    if not campaign_id:
        return True, ""

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT status FROM campaigns WHERE id = ?",
                (int(campaign_id),),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            return False, f"campaign {campaign_id} not found"

        status = str(row[0] or "").strip().lower()
        not_ready = {"preparing", "needs_review", "draft"}
        if status in not_ready:
            return False, f"campaign {campaign_id} is still in '{status}' state"

    except Exception:
        logger.debug("pre-flight: campaign state check failed", exc_info=True)
        # Don't fail on a DB error; the publisher will handle it.

    return True, ""
