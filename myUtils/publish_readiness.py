"""Stored, exact-destination authorization evidence; never a provider probe.

GET callers use destination_readiness. Only a caller which has just performed
an actual read-only provider verification may call record_destination_readiness.
The latter accepts normalized facts, not raw responses, credentials or ready flags.
Neither function creates a database or schema. Proof is invalidated by credential,
identity, tenant, profile policy or effective-route changes.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from myUtils import account_identity, config_crypto, token_state

DEFAULT_MAX_AGE_SECONDS = 900
_REASONS = {
    "configured_unverified": "Destination configured; fresh exact authorization evidence is required",
    "configuration_required": "Destination configuration or credential selection is incomplete",
    "unsupported_provider": "Configured publishing provider is unsupported",
    "disabled": "Destination is disabled",
    "native_hold": "Native publishing is held; repair native configuration or budget",
    "revoked": "Native authorization requires reconnect",
    "expired": "Native authorization deadline has passed",
    "dependency_cooldown": "The exact destination dependency is in a durable cooldown",
    "evidence_expired": "Stored destination verification has expired",
    "configuration_changed": "Destination configuration changed since verification",
    "evidence_invalid": "Stored destination verification does not prove exact authorization",
    "readiness_unavailable": "Stored destination readiness is unavailable",
    "ready": "Fresh exact-destination authorization verified",
}


def _now():
    return datetime.now(timezone.utc)


def _time(value):
    return token_state.parse_time(value)


def _stamp(value):
    parsed = _time(value)
    return parsed.isoformat(timespec="seconds") if parsed else None


def _value(account, key, default=None):
    return account_identity._value(account, key, default)


def _config(account):
    config = account_identity._config(account)
    if config is None:
        raise ValueError("Invalid destination configuration")
    return config_crypto.decrypt_config_secrets(dict(config))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _refs(values):
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("Exact destination references are required")
    refs = [str(v) for v in values if isinstance(v, (str, int)) and not isinstance(v, bool)]
    if len(refs) != len(values) or any(not re.fullmatch(r"[A-Za-z0-9_.:|@-]{1,128}", v) for v in refs):
        raise ValueError("Invalid destination reference")
    return refs


def _caps(values):
    if not isinstance(values, (list, tuple)) or any(
            not isinstance(v, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", v) for v in values):
        raise ValueError("Invalid destination capabilities")
    return sorted(set(values))


def _context(account, settings):
    config = _config(account)
    platform = account_identity._platform(account)
    provider = str(config.get("publishingProvider") or "direct").strip().lower()
    settings = settings if settings is not None else {}
    if not isinstance(settings, Mapping):
        raise ValueError("Invalid profile policy")
    identity, selected, configured = None, {}, False
    if provider == "sociamonials":
        from myUtils import sociamonials_fallback as managed
        # Use the publisher's existing route and isolated teaching selector.
        try:
            managed.prefers_sociamonials({"config": config}, platform=platform)
            mapping = managed.resolve_mapping(_value(account, "id"), platform=platform,
                                              account={"config": config}, settings=settings)
            key, workspace = managed.resolve_credentials(mapping)
            if managed._workspace_int(workspace) is None:
                raise ValueError("Invalid exact provider workspace")
        except managed.SociamonialsNotConfigured as exc:
            raise ValueError("Invalid exact provider credential selection") from exc
        selector = mapping.get("api_key_env") or (
            managed.TEACHING_API_KEY_ENV if workspace == managed.DEFAULT_TEACHING_WORKSPACE_ID else managed.API_KEY_ENV)
        if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}', selector):
            raise ValueError('Invalid exact provider credential selector')
        identity = {"network": mapping["network"], "profile_refs": _refs(mapping["profile_refs"]),
                    "workspace_id": str(workspace), "api_key_env": selector}
        selected = {"selector": selector, "credentialDigest": _digest(key)}
        configured = bool(key)
    elif provider == "direct":
        external = account_identity.external_account_identity({"platform": platform, "config": config})
        if external:
            identity = {"platform": external[0], "id": _refs([external[1]])[0]}
        # Include both explicit native material and its effective env selection.
        selected = {k: config.get(k) for k in token_state.CREDENTIAL_KEYS if k in config}
        selected["env"] = {str(v): os.environ.get(str(v), "") for k, v in config.items()
                           if k.endswith("Env") and isinstance(v, str)}
        selected["cookiePathDigest"] = _digest(_value(account, "cookie_path", ""))
        if platform in {"facebook", "instagram", "threads"}:
            # These introspectors use the existing global app selector. App
            # rotation must invalidate proofs just like access-token rotation.
            from myUtils import meta_auth, threads_auth
            auth = threads_auth if platform == "threads" else meta_auth
            try:
                selected["introspectionApp"] = {
                    "id": auth._required_env(auth.CLIENT_ID_ENV),
                    "secretDigest": _digest(auth._required_env(auth.CLIENT_SECRET_ENV)),
                }
            except token_state.CredentialError:
                selected["introspectionApp"] = None
        configured = bool(config.get("accessToken") or config.get("accessTokenEnv")
                          or config.get("botToken") or config.get("botTokenEnv")
                          or config.get("password") or _value(account, "cookie_path"))
        from myUtils import native_destinations
        if platform in native_destinations.SUPPORTED_PLATFORMS:
            identity, credential_selection, configured = native_destinations.configured_destination(platform, config)
            selected["nativeDestinationCredentials"] = credential_selection
    required = _caps(config.get("requiredCapabilities", []))
    policy = settings.get("publishing", {})
    if isinstance(policy, Mapping):
        required = sorted(set(required) | set(_caps(policy.get("requiredCapabilities", []))))
    # Exclude historical check/display/error metadata: those facts cannot grant
    # authorization or invalidate a live proof merely when a label is edited.
    route_config = {k: v for k, v in config.items() if k not in (token_state.AUTH_METADATA_KEYS - {'scope', 'tokenType'})
                    and not k.startswith("_")}
    fingerprint = _digest({"version": 1, "accountId": _value(account, "id"),
                           "workspaceId": _value(account, "workspace_id"),
                           "profileId": _value(account, "profile_id"), "provider": provider,
                           "identity": identity, "selection": selected,
                           "config": route_config, "profilePolicy": dict(settings)})
    return {"provider": provider, "identity": identity, "configured": configured,
            "config": config, "fingerprint": fingerprint, "required": required}


def config_fingerprint(account, settings=None):
    """Opaque digest of effective route/credentials/exact identity/profile policy."""
    return _context(account, settings)["fingerprint"]


def destination_scope_key(account, settings=None):
    """Dependency writers use this tenant-and-exact-destination scope key."""
    context = _context(account, settings)
    return "destination:" + _digest({"workspace": _value(account, "workspace_id"),
                                    "provider": context["provider"], "identity": context["identity"],
                                    "account": _value(account, "id")})


def _native_health(account, config):
    native = token_state.native_state(SimpleNamespace(config=config, platform=account_identity._platform(account)))
    hold = bool(token_state.native_route_block_reason(config))
    state = native["state"]
    error = config.get('_nativeCredentialError')
    if config.get('_nativeCredentialState') == 'reconnect_required' or isinstance(error, dict) and str(error.get('code')).lower() in {'invalid_grant', 'invalid_token', 'token_revoked'}:
        state = 'reconnect_required'
    platform = account_identity._platform(account)
    from myUtils import native_destinations
    if platform in native_destinations.SUPPORTED_PLATFORMS:
        _, _, configured = native_destinations.configured_destination(platform, config)
        native["configured"] = configured
        if state == "missing" and configured:
            state = "unknown"
    # Generic token checks do not prove destination identity/grants/capabilities.
    return {"state": state, "configured": bool(native["configured"]),
            "reconnectRequired": bool(native["reconnectRequired"]) or state == 'reconnect_required', "held": hold,
            "reasonCode": "native_hold" if hold else state,
            "reason": "Native publishing is held" if hold else {
                "valid": "Native credentials checked historically; exact destination proof is separate",
                "unknown": "Native credentials configured; exact authorization unverified",
            }.get(state, "Native credential state: " + state),
            "checkedAt": _stamp(native["lastCheckedAt"]),
            "expiresAt": _stamp(native["token"]["expiresAt"])}


def _result(context, native, code, *, row=None, evidence=None, next_attempt=None):
    phase = "ready" if code == "ready" else "waiting_dependency" if code == "dependency_cooldown" else "blocked_intent" if code in {
        "disabled", "revoked", "native_hold", "configuration_required", "unsupported_provider", "expired"} else "configured_unverified"
    return {"provider": context["provider"], "phase": phase, "status": phase,
            "ready": code == "ready", "reasonCode": code, "reason": _REASONS[code],
            "checkedAt": _stamp(row["checked_at"]) if row else None,
            "expiresAt": _stamp(row["expires_at"]) if row else None,
            "evidenceFresh": code == "ready", "nativeHealth": native,
            "destinationFingerprint": context.get("fingerprint"),
            "nextAttemptAt": _stamp(next_attempt), "evidence": evidence if code == "ready" else None}


def _validate_proof(context, proof, *, max_age_seconds):
    if not isinstance(proof, Mapping) or proof.get("verified") is not True or proof.get("live_check") is not True:
        raise ValueError("Fresh provider verification facts are required")
    if proof.get("source") != "provider_verification" or proof.get("requires_approval") is not False:
        raise ValueError("Direct authorization without approval must be proved")
    if proof.get('config_fingerprint') != context['fingerprint']:
        raise ValueError('Verification does not bind the effective credential/configuration selection')
    checked = _time(proof.get("verified_at"))
    now = _now()
    if checked is None or checked > now or (now - checked).total_seconds() >= max_age_seconds:
        raise ValueError("Provider verification is stale or has an invalid timestamp")
    identity = proof.get("mapping") if context["provider"] == "sociamonials" else proof.get("identity")
    if not isinstance(identity, Mapping) or any(identity.get(k) != v for k, v in (context["identity"] or {}).items()) or not context["identity"]:
        raise ValueError("Provider proof does not match the exact configured destination")
    permissions = proof.get("permissions")
    required_permissions = ("posts.read", "posts.create", "profiles.read", "posts.publish_direct") if context["provider"] == "sociamonials" else ("posts.create",)
    if not isinstance(permissions, Mapping) or any(permissions.get(p) is not True for p in required_permissions):
        raise ValueError("Exact destination publishing grants are not proved")
    capabilities = _caps(proof.get("capabilities"))
    if not capabilities or "mobile_app_handoff" in capabilities or not set(context["required"]) <= set(capabilities):
        raise ValueError("Required original-content capabilities are not proved")
    # Persist an allowlist only. Never free-form provider responses or credentials.
    limits = proof.get("limits") or {}
    limit_fields = {'max_video_bytes', 'hosted_video_max_bytes', 'external_video_max_bytes',
                    'image_max_bytes', 'hosted_image_max_bytes', 'image_max_pixels', 'image_max_long_side',
                    'max_images', 'video_max_seconds', 'message_max_length', 'title_max_length',
                    'reads_per_minute', 'writes_per_minute', 'media_transfers_per_minute'}
    safe_limits = {k: v for k, v in limits.items() if k in limit_fields
                   and type(v) in (int, float) and math.isfinite(v) and v >= 0} if isinstance(limits, Mapping) else {}
    expires = checked + timedelta(seconds=max_age_seconds)
    declared = _time(proof.get("expires_at"))
    if proof.get('expires_at') is not None and declared is None:
        raise ValueError('Invalid explicit verification expiry')
    if declared:
        expires = min(expires, declared)
    if expires <= now:
        raise ValueError("Provider verification has expired")
    return {"verified": True, "live_check": True, "source": "provider_verification",
            'config_fingerprint': context['fingerprint'],
            "requires_approval": False, "mapping" if context["provider"] == "sociamonials" else "identity": dict(context["identity"]),
            "permissions": {p: True for p in required_permissions}, "capabilities": capabilities,
            "limits": safe_limits, "verified_at": checked.isoformat(), "expires_at": expires.isoformat()}


def _read_connection(db_path, *, writable=False):
    # mode=rw also refuses absent files; schema is owned exclusively by Alembic.
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + ("?mode=rw" if writable else "?mode=ro"),
                           uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    return conn


def _settings(conn, account, settings):
    if settings is not None:
        return settings
    row = conn.execute("SELECT settings_json FROM profiles WHERE id = ? AND workspace_id IS ?",
                       (_value(account, "profile_id"), _value(account, "workspace_id"))).fetchone()
    return account_identity.json_object(row[0] or "{}") if row else {}


def _project(account, context, native, row, dependency=None):
    code = "configured_unverified"
    if not _value(account, "enabled", True):
        code = "disabled"
    elif context["provider"] not in {"direct", "sociamonials"}:
        code = "unsupported_provider"
    elif context["provider"] == "direct" and native["held"]:
        code = "native_hold"
    elif context["provider"] == "direct" and native["reconnectRequired"]:
        code = "expired" if native["state"] == "expired" else "revoked"
    elif not context["configured"]:
        code = "configuration_required"
    else:
        if dependency and dependency["config_fingerprint"] == context["fingerprint"] and dependency["status"] in {"blocked", "cooldown", "waiting_dependency", "held"}:
            until = _time(dependency["blocked_until"])
            if until and until > _now():
                return _result(context, native, "dependency_cooldown", row=row, next_attempt=until)
        if row:
            if row["workspace_id"] != _value(account, "workspace_id") or row["provider"] != context["provider"] or row["config_fingerprint"] != context["fingerprint"]:
                code = "configuration_changed"
            elif row["status"] != "ready":
                code = "evidence_invalid"
            elif not _time(row["expires_at"]) or _time(row["expires_at"]) <= _now():
                code = "evidence_expired"
            else:
                try:
                    evidence = account_identity.json_object(row["evidence_json"])
                    evidence = _validate_proof(context, evidence, max_age_seconds=DEFAULT_MAX_AGE_SECONDS)
                    if _time(evidence["verified_at"]) != _time(row["checked_at"]):
                        raise ValueError("Evidence timestamp mismatch")
                    return _result(context, native, "ready", row=row, evidence=evidence)
                except (ValueError, TypeError):
                    code = "evidence_invalid"
    return _result(context, native, code, row=row)


def destination_readiness(account, settings=None, *, db_path):
    """Truthful read-only projection; absent database/table is never created."""
    context = {"provider": "direct", "fingerprint": None}
    native = None
    conn = None
    try:
        config = _config(account)
        context["provider"] = str(config.get("publishingProvider") or "direct").lower()
        native = _native_health(account, config)
        conn = _read_connection(db_path)
        conn.execute('BEGIN')
        current_account = conn.execute('SELECT * FROM accounts WHERE id = ? AND workspace_id IS ?',
                                       (_value(account, 'id'), _value(account, 'workspace_id'))).fetchone()
        if not current_account:
            return _result(context, native, 'configuration_changed')
        settings = _settings(conn, account, settings)
        context = _context(account, settings)
        current = _context(current_account, _settings(conn, current_account, None))
        if current['fingerprint'] != context['fingerprint']:
            return _result(current, _native_health(current_account, current['config']), 'configuration_changed')
        account, context = current_account, current
        native = _native_health(account, context['config'])
        row = conn.execute("SELECT * FROM publish_destination_readiness WHERE account_id = ? AND workspace_id IS ?",
                           (_value(account, "id"), _value(account, "workspace_id"))).fetchone()
        try:
            dependency = conn.execute("SELECT * FROM publish_dependency_states WHERE scope_key = ?",
                                      (destination_scope_key(account, settings),)).fetchone()
        except sqlite3.OperationalError:
            dependency = None
        return _project(account, context, native, row, dependency)
    except (sqlite3.Error, OSError):
        return _result(context, native, "readiness_unavailable")
    except (ValueError, TypeError, KeyError):
        return _result(context, native, "configuration_required")
    finally:
        if conn:
            conn.close()


def record_destination_readiness(account, settings=None, *, proof, provider, db_path,
                                 max_age_seconds=DEFAULT_MAX_AGE_SECONDS):
    """Record normalized fresh verification after caller's actual provider read.

    Proof requires verified/live_check=True, source='provider_verification',
    exact mapping (managed) or identity (native), explicit publishing permissions,
    capabilities, requires_approval=False and verified_at. No ready=True shortcut.
    Capture config_fingerprint(account, settings) BEFORE the provider read and
    include it in proof['config_fingerprint']; rotation during the read is denied.
    The current account and profile are rechecked under a writer transaction.
    Stale/foreign-account proofs and absent tables fail without changing anything.
    """
    if not math.isfinite(float(max_age_seconds)) or not 0 < float(max_age_seconds) <= DEFAULT_MAX_AGE_SECONDS:
        raise ValueError("Verification age must be between 0 and 900 seconds")
    conn = _read_connection(db_path, writable=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM accounts WHERE id = ? AND workspace_id IS ?",
                           (_value(account, "id"), _value(account, "workspace_id"))).fetchone()
        if not row:
            raise ValueError("Destination ownership changed during verification")
        effective_settings = _settings(conn, row, None)
        context = _context(account, effective_settings if settings is None else settings)
        current = _context(row, effective_settings)
        if context["fingerprint"] != current["fingerprint"] or provider != current["provider"] or not current["configured"]:
            raise ValueError("Destination configuration changed during verification")
        evidence = _validate_proof(current, proof, max_age_seconds=float(max_age_seconds))
        conn.execute("""INSERT INTO publish_destination_readiness
            (account_id,workspace_id,provider,config_fingerprint,status,reason,evidence_json,checked_at,expires_at)
            VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET
            workspace_id=excluded.workspace_id,provider=excluded.provider,config_fingerprint=excluded.config_fingerprint,
            status=excluded.status,reason=excluded.reason,evidence_json=excluded.evidence_json,
            checked_at=excluded.checked_at,expires_at=excluded.expires_at""",
            (_value(row, "id"), _value(row, "workspace_id"), provider, current["fingerprint"], "ready", "ready",
             json.dumps(evidence, sort_keys=True), evidence["verified_at"], evidence["expires_at"]))
        conn.commit()
    finally:
        conn.close()
    return destination_readiness(account, settings, db_path=db_path)
