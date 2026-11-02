"""Credential state, safe provider verdicts and cross-process account CAS.

No provider I/O is performed here. Every mutation reads under BEGIN IMMEDIATE,
compares the credential revision AND identity/token fingerprint, and merges only
auth fields into the current encrypted config. Routing, enabled and tenancy
fields are never copied from a maintenance snapshot.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from myUtils import config_crypto
from myUtils.secret_redaction import redact_text

CREDENTIAL_KEYS = frozenset({
    "accessToken", "refreshToken", "metaUserAccessToken", "accessTokenEnv",
    "refreshTokenEnv", "metaUserAccessTokenEnv", "pageId", "igUserId",
    "threadUserId", "userId", "openId", "channelId", "redditUserName",
    "twitterUserId", "twitterAuthType", "redditAuthType", "clientIdEnv",
    "clientSecretEnv", "consumerKeyEnv", "consumerSecretEnv",
    'patreonUserId', 'patreonAuthType', 'campaignId',
})
FAILURE_KEYS = (
    "_maintenanceFailures", "_nextMaintenanceAttemptAt", "_lastMaintenanceError",
    "_lastMaintenanceAttemptAt", "_needsReconnect", "_reconnectAlertedAt",
    "_tokenSelfCheckFailures", "_lastTokenSelfCheckError", "_tokenSelfCheckError",
    "_nativeCredentialError", "_nativeCredentialWarning",
)
AUTH_METADATA_KEYS = frozenset({
    "accessTokenExpiresAt", "accessTokenExpiryMode", "accessTokenExpiresAtEpoch",
    "accessTokenUpdatedAt", "refreshTokenExpiresAt", "refreshTokenUpdatedAt",
    "metaUserAccessTokenExpiresAt", "metaTokenExpiryMode", "metaTokenExpiresAtEpoch",
    "dataAccessExpiresAt", "dataAccessExpiresAtEpoch", "metaUserDataAccessExpiresAt",
    "metaUserDataAccessExpiresAtEpoch", "connectedAt", "scope", "tokenType",
    "lastAutoRefreshAt", "lastManualRefreshAt", "lastConnectionCheckAt",
    "facebookPageName", "instagramUserName", "threadsUserName", "twitterUserName",
    "displayName", "avatarUrl", "channelTitle", "expiresIn", "refreshExpiresIn",
    'twitterDisplayName', 'patreonUserName',
    "_lastTokenSelfCheckAt", "_nativeCredentialState", "_nativeCredentialCheckedAt",
    "_nativeCredentialWarning",
})


class StaleCredentialUpdate(RuntimeError):
    def __init__(self):
        super().__init__("Account credentials or identity changed during authorization; reload and retry")


class CredentialError(RuntimeError):
    """A safe, structured verdict, never a provider URL/token-bearing message."""
    def __init__(self, message: str, *, code=None, subcode=None, status_code=None,
                 terminal: bool = False, kind: str = "unknown"):
        super().__init__(redact_text(message)[:300])
        self.error_code = code
        self.error_subcode = subcode
        self.status_code = status_code
        self.terminal = terminal
        self.kind = kind


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().replace(tzinfo=None).isoformat(timespec="seconds")


def parse_time(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _count(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def provider_error(error, *, status_code=None, invalid=False) -> CredentialError:
    """Classify Graph/Threads errors without storing their free-form message."""
    data = error if isinstance(error, dict) else {"code": error} if isinstance(error, str) else {}
    code = data.get("code") or data.get("error")
    subcode = data.get("error_subcode", data.get("subcode"))
    code_text = str(code or "")
    transient = data.get("is_transient") is True or status_code == 429 or (
        isinstance(status_code, int) and status_code >= 500
    ) or code_text in {"1", "2", "4", "17", "32", "341", "613"}
    terminal = not transient and (invalid or code_text in {
        "190", "102", "10", "200", "294", "299", "invalid_grant", "invalid_token",
        "refresh_token_revoked", "access_denied",
    })
    kind = "transient" if transient else ("permission" if code_text in {"10", "200", "294", "299", "access_denied"}
                                         else "credential" if terminal else "unknown")
    reason = {"transient": "Provider temporarily unavailable or rate limited; retry scheduled",
              "permission": "Provider rejected the required permissions; reconnect required",
              "credential": "Provider rejected the credential; reconnect required",
              "unknown": "Provider validation failed; credential validity is unknown"}[kind]
    if code is not None:
        reason += f" (code {str(code)[:40]}"
        if subcode is not None:
            reason += f", subcode {str(subcode)[:40]}"
        reason += ")"
    return CredentialError(reason, code=code, subcode=subcode, status_code=status_code,
                           terminal=terminal, kind=kind)


def response_payload(response, *, error_class=CredentialError) -> dict:
    """Read structured errors BEFORE raise_for_status discards their codes."""
    status = getattr(response, "status_code", None)
    try:
        payload = response.json()
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict) and payload.get("error"):
        verdict = provider_error(payload["error"], status_code=status)
        raise error_class(str(verdict), code=verdict.error_code, subcode=verdict.error_subcode,
                          status_code=status, terminal=verdict.terminal, kind=verdict.kind)
    if isinstance(status, int) and status >= 400:
        verdict = provider_error({}, status_code=status)
        raise error_class(str(verdict), status_code=status, kind=verdict.kind)
    response.raise_for_status()
    if not isinstance(payload, dict):
        raise error_class("Provider returned an unreadable response; retry scheduled", kind="transient")
    return payload


def classify_failure(exc: Exception) -> dict:
    if isinstance(exc, CredentialError):
        verdict = exc
    else:
        response = getattr(exc, "response", None)
        error = {}
        if response is not None:
            try:
                payload = response.json()
                error = payload.get("error") or {} if isinstance(payload, dict) else {}
            except Exception:
                pass
        if isinstance(error, str):
            error = {"code": error}
        elif not isinstance(error, dict):
            error = {}
        if not error and getattr(exc, "error_code", None):
            error = {"code": exc.error_code, "error_subcode": getattr(exc, "error_subcode", None)}
        status = getattr(exc, "status_code", None)
        if status is None and response is not None:
            status = getattr(response, "status_code", None)
        verdict = provider_error(error, status_code=status)
        if not error and status is None:
            # Unknown exceptions and transport errors are NOT credential verdicts.
            verdict = CredentialError("Credential check could not complete; retry scheduled", kind="transient")
    return {"terminal": verdict.terminal, "kind": verdict.kind, "reason": str(verdict),
            "code": verdict.error_code, "subcode": verdict.error_subcode,
            "httpStatus": verdict.status_code}


def safe_error(exc: Exception) -> str:
    if isinstance(exc, StaleCredentialUpdate):
        return str(exc)
    return classify_failure(exc)["reason"]


def fingerprint(account) -> str:
    config = account.config or {}
    payload = {
        "id": account.id, "profile": account.profile_id, "platform": account.platform,
        "workspace": getattr(account, "workspace_id", None), "authType": account.auth_type,
        "revision": _count(config.get("_credentialRevision")),
        "credentials": {key: config.get(key) for key in sorted(CREDENTIAL_KEYS)},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def credential_changes(before: dict, after: dict) -> dict:
    """Extract only intended auth changes from legacy helpers returning full configs."""
    allowed = CREDENTIAL_KEYS | AUTH_METADATA_KEYS
    return {key: value for key, value in after.items() if key in allowed and before.get(key) != value}


def clear_failure_markers(config: dict) -> None:
    for key in FAILURE_KEYS:
        config.pop(key, None)


def _merge_locked(conn, *, account_id: int, expected: str, changes: dict | None = None,
                  success=False, failure: dict | None = None, self_check=False,
                  backoff_base=1800, backoff_cap=21600, alert_state=None, advance_generation=True):
    from myUtils import profiles
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        return None
    account = profiles._row_to_account(row)
    if fingerprint(account) != expected:
        return None
    cfg = dict(account.config or {})
    patch = dict(changes or {})
    if set(patch) - (CREDENTIAL_KEYS | AUTH_METADATA_KEYS):
        raise ValueError("Credential update contains non-auth fields")
    credentials_changed = any(key in CREDENTIAL_KEYS and cfg.get(key) != value for key, value in patch.items())
    cfg.update(patch)
    now = iso_now()
    credential_write = success and (advance_generation or credentials_changed)
    if success:
        clear_failure_markers(cfg)
        if credential_write:
            cfg["_credentialRevision"] = _count(cfg.get("_credentialRevision")) + 1
        cfg["_nativeCredentialState"] = "verified"
        cfg["_nativeCredentialCheckedAt"] = now
        cfg["_lastTokenSelfCheckAt"] = now
    elif failure is not None:
        should_alert = bool(failure['terminal'] and not cfg.get('_reconnectAlertedAt'))
        previous_error = cfg.get('_nativeCredentialError')
        failures = min(_count(cfg.get("_maintenanceFailures")) + 1, 1000000)
        delay = min(backoff_base * (2 ** min(failures - 1, 16)), backoff_cap)
        cfg.update({
            "_maintenanceFailures": failures, "_lastMaintenanceError": failure["reason"],
            "_lastMaintenanceAttemptAt": now,
            "_nextMaintenanceAttemptAt": (utc_now() + timedelta(seconds=delay)).replace(tzinfo=None).isoformat(timespec="seconds"),
            "_nativeCredentialError": failure, "_nativeCredentialCheckedAt": now,
        })
        if cfg.get('_needsReconnect') and not failure['terminal'] and previous_error:
            cfg['_nativeCredentialError'] = previous_error
        if alert_state is not None:
            alert_state['needed'] = should_alert
        if self_check:
            cfg["_tokenSelfCheckFailures"] = _count(cfg.get("_tokenSelfCheckFailures")) + 1
            cfg["_lastTokenSelfCheckAt"] = now
        if failure["terminal"]:
            cfg["_needsReconnect"] = True
            cfg["_nativeCredentialState"] = "reconnect_required"
            cfg.setdefault("_reconnectAlertedAt", now)
        elif not cfg.get("_needsReconnect"):
            cfg["_nativeCredentialState"] = "warning"
        # Never fabricate an expiry. A transient failure also cannot erase an
        # earlier terminal verdict; only a verified success can do that.
    serialized = json.dumps(config_crypto.encrypt_config_secrets(cfg), ensure_ascii=False)
    # Read + compare + merge + write are all inside one SQLite write transaction.
    conn.execute(
        "UPDATE accounts SET config_json = ?, auth_type = ?, status = ?, last_checked_at = ? WHERE id = ?",
        (serialized, "oauth" if credential_write else account.auth_type,
         1 if success else account.status, now, account_id),
    )
    return profiles._row_to_account(conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone())


def commit_success(account, changes: dict, *, db_path, expected: str | None = None, advance_generation=True):
    from myUtils import profiles
    with profiles._connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = _merge_locked(conn, account_id=account.id, expected=expected or fingerprint(account),
                                changes=changes, success=True, advance_generation=advance_generation)
        conn.commit()
    return updated


def commit_validation(account, changes: dict, *, db_path, expected: str | None = None):
    """Save a verified check without invalidating an in-flight token exchange.

    A check that actually synchronizes a changed credential still advances the
    revision. Metadata-only checks preserve both revision and auth type. OAuth
    reauthorization uses commit_success's forced advance, including same tokens.
    """
    return commit_success(account, changes, db_path=db_path, expected=expected, advance_generation=False)


def commit_failure(account, exc: Exception, *, db_path, self_check=False,
                   backoff_base=1800, backoff_cap=21600, alert_state=None):
    from myUtils import profiles
    if isinstance(exc, StaleCredentialUpdate):
        return None
    with profiles._connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = _merge_locked(conn, account_id=account.id, expected=fingerprint(account),
                                failure=classify_failure(exc), self_check=self_check,
                                backoff_base=backoff_base, backoff_cap=backoff_cap, alert_state=alert_state)
        conn.commit()
    return updated


def require_saved(account):
    if account is None:
        raise StaleCredentialUpdate()
    return account


def native_state(account) -> dict:
    """Read-only facts; a future expiry/configuration alone is not validation."""
    cfg = account.config or {}
    is_meta = account.platform in {"facebook", "instagram"}
    def deadline(prefix, mode_key, epoch_key, data_key, data_epoch_key):
        raw = cfg.get(prefix) or ""
        mode = cfg.get(mode_key) or ("expiring" if raw else "unknown")
        return {"expiresAt": raw, "mode": mode, "expiresAtEpoch": cfg.get(epoch_key),
                "dataAccessExpiresAt": cfg.get(data_key) or "",
                "dataAccessExpiresAtEpoch": cfg.get(data_epoch_key)}
    token = deadline("accessTokenExpiresAt", "accessTokenExpiryMode", "accessTokenExpiresAtEpoch",
                     "dataAccessExpiresAt", "dataAccessExpiresAtEpoch")
    user = deadline("metaUserAccessTokenExpiresAt", "metaTokenExpiryMode", "metaTokenExpiresAtEpoch",
                    "metaUserDataAccessExpiresAt", "metaUserDataAccessExpiresAtEpoch") if is_meta else None
    present = bool(cfg.get("accessToken") or cfg.get("accessTokenEnv"))
    if account.platform == "facebook":
        present = present and bool(cfg.get("pageId"))
    elif account.platform == "instagram":
        present = present and bool(cfg.get("igUserId"))
    elif account.platform == "threads":
        present = present and bool(cfg.get("threadUserId") or cfg.get("userId"))
    now = utc_now()
    deadlines = []
    for item in (token, user):
        if item:
            if item["mode"] != "never":
                deadlines.append(parse_time(item["expiresAt"]))
            deadlines.append(parse_time(item["dataAccessExpiresAt"]))
    deadlines = [value for value in deadlines if value is not None]
    error = cfg.get("_nativeCredentialError")
    error = error if isinstance(error, dict) else {}
    last_checked = cfg.get("_nativeCredentialCheckedAt") or cfg.get("_lastTokenSelfCheckAt") or ""
    if cfg.get("_needsReconnect"):
        state, reason = "reconnect_required", error.get("reason") or "Native credentials require reconnect"
    elif deadlines and min(deadlines) <= now:
        state, reason = "expired", "A native token or data-access deadline has passed"
    elif not present:
        state, reason = "missing", "Native credentials or stable identity are not configured"
    elif cfg.get("_nativeCredentialState") == "warning" or cfg.get("_maintenanceFailures") or cfg.get("_tokenSelfCheckFailures"):
        state, reason = "warning", error.get("reason") or "Last native validation could not complete"
    elif deadlines and min(deadlines) <= now + timedelta(days=7):
        state, reason = "expiring", "A native token or data-access deadline is approaching"
    elif cfg.get("_nativeCredentialState") == "verified" and last_checked:
        state, reason = "valid", "Native credentials validated at the last check"
    else:
        state, reason = "unknown", "Native credentials configured; not yet validated"
    return {"state": state, "reason": reason, "lastCheckedAt": last_checked,
            "reconnectRequired": state in {"reconnect_required", "expired"},
            "configured": present, "token": token, "userToken": user,
            "providerCode": error.get("code"), "providerSubcode": error.get("subcode"),
            "providerHttpStatus": error.get("httpStatus")}


def publishing_route(account, native: dict) -> dict:
    cfg = account.config or {}
    provider = str(cfg.get("publishingProvider") or "direct").strip().lower()
    configured = bool(native["configured"])
    reason = "Native route configured" if configured else "Native route is missing credentials or identity"
    if provider == "sociamonials":
        from myUtils.sociamonials_fallback import prefers_sociamonials
        try:
            configured = prefers_sociamonials(account, platform=account.platform)
        except Exception:
            configured = False
        reason = "Managed route configured; provider connection and availability not validated" if configured else "Managed destination configuration is incomplete"
    elif provider != "direct":
        configured = False
        reason = "Unsupported publishing provider configuration"
    return {"provider": provider, "configured": configured, "enabled": bool(account.enabled),
            "state": "disabled" if not account.enabled else "configured" if configured else "missing",
            "validation": "not_checked" if provider != "direct" else native["state"],
            "availability": "unknown", "reason": reason}
