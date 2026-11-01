"""Cheap validation after a target is claimed; no provider calls or restoration.

A missing/disabled destination is blocked, not confused with a database outage.
Media restoration and platform-specific credential refresh belong to the executor.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass(slots=True)
class PreFlightResult:
    ok: bool
    reason: str = ""
    checks: dict[str, bool] = field(default_factory=dict)
    category: str = "blocked"

    @classmethod
    def passed(cls, checks=None):
        return cls(True, checks=checks or {})

    @classmethod
    def failed(cls, reason, checks=None, *, category="blocked"):
        return cls(False, reason, checks or {}, category)


def run_pre_flight(*, account_ref: str, job_id: int, platform: str,
                   payload: dict, db_path: Path,
                   validate_credentials: bool = True) -> PreFlightResult:
    checks = {}
    # Cookie filenames/paths are a supported legacy reference, not a missing row.
    # The cookie executor validates/decrypts the actual file before using it.
    if account_ref.startswith("account:"):
        account = _resolve_account(account_ref, db_path)
        if account is None:
            return PreFlightResult.failed(f"Account not found: {account_ref}", {"account_exists": False})
        checks["account_exists"] = True
        if not _account_enabled(account):
            return PreFlightResult.failed(f"Account disabled: {account_ref}", {**checks, "account_enabled": False})
        checks["account_enabled"] = True
        config = account.config or {}
        # An explicitly configured fallback owns a separate provider credential.
        if validate_credentials:
            if config.get("_needsReconnect"):
                return PreFlightResult.failed(f"Account flagged for reconnect: {account_ref}", {**checks, "needs_reconnect": False})
            token_ok, reason = _check_token_validity(account, platform, config)
            checks["token_valid"] = token_ok
            if not token_ok:
                return PreFlightResult.failed(f"Token invalid for {account_ref}: {reason}", checks)
    elif not str(account_ref).strip():
        return PreFlightResult.failed("An account reference is required")

    if payload.get("campaignId"):
        ok, reason = _check_campaign_state(payload, db_path)
        checks["campaign_publishable"] = ok
        if not ok:
            return PreFlightResult.failed(f"Campaign not publishable for job {job_id}: {reason}", checks)
        # A recorded path may intentionally be offloaded. Do not reject it before
        # the executor can resolve exact storage mappings and restore the bytes.
        artifacts = payload.get("artifacts") or []
        if not isinstance(artifacts, list) or any(not isinstance(a, dict) for a in artifacts):
            return PreFlightResult.failed("Invalid artifact payload", checks, category="permanent")
    return PreFlightResult.passed(checks)


def _resolve_account(account_ref: str, db_path: Path):
    from myUtils import profiles
    try:
        ident = int(account_ref.split(":", 1)[1])
    except (ValueError, IndexError):
        return None
    try:
        return profiles.get_account(ident, db_path=db_path)
    except LookupError:
        return None
    # Busy/corrupt DB, decryption and I/O errors must reach the retry handler.


def _account_enabled(account) -> bool:
    return bool(getattr(account, "enabled", True))


def _check_token_validity(account, platform: str, config: dict) -> tuple[bool, str]:
    from myUtils import profiles
    if profiles.effective_auth_type(config, getattr(account, "auth_type", None), platform) == "cookie":
        return True, ""
    if platform not in {"tiktok", "youtube", "threads", "twitter", "reddit", "facebook", "instagram"}:
        return True, ""
    # Credential aliases/env indirection are resolved by individual publishers.
    if not config.get("accessToken"):
        return True, ""
    expiry = _parse_iso_datetime(str(config.get("accessTokenExpiresAt") or ""))
    if expiry is not None and expiry <= datetime.now(timezone.utc):
        if not config.get("refreshToken") and not config.get("refreshTokenEnv") and platform not in {"facebook", "instagram", "threads"}:
            return False, "access token expired and no refresh credential is configured"
    return True, ""


def _parse_iso_datetime(value: str):
    if not value or not str(value).strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    # Preserve the publisher's legacy convention: naive token expiries were
    # local wall time. New expiries have offsets; comparisons are always aware.
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


def _check_media_accessible(payload: dict, db_path: Path) -> tuple[bool, str]:
    """Compatibility helper: declared recoverable media may be offloaded."""
    for artifact in payload.get("artifacts") or []:
        if isinstance(artifact, dict) and any(artifact.get(k) for k in ("local_path", "public_url", "remote_path", "source_file_record_id")):
            return True, ""
    return False, "no recoverable artifact reference"


def _check_campaign_state(payload: dict, db_path: Path) -> tuple[bool, str]:
    campaign_id = payload.get("campaignId")
    if not campaign_id:
        return True, ""
    con = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True, timeout=15)
    try:
        row = con.execute("SELECT status FROM campaigns WHERE id=?", (int(campaign_id),)).fetchone()
    finally:
        con.close()
    if row is None:
        return False, f"campaign {campaign_id} not found"
    if row[0] in {"preparing", "needs_review", "draft", "cancelled", "failed"}:
        return False, f"campaign {campaign_id} is in '{row[0]}' state"
    return True, ""
