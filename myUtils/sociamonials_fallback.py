"""Bounded, fail-closed HTTP backup to an explicitly mapped Sociamonials profile.

No import-time I/O, automatic retries, caption rewriting, account substitution,
media re-preparation, or library deletion. The parent worker owns route budgets,
recovery generations and durable checkpoint persistence. A receipt is acceptance,
NOT delivery; only the exact destination's confirmed network post is success.

Public reference/OpenAPI verified read-only on 2026-11-05: validation is
POST /api/v1/posts with validate_only=true and strict=true, NOT an undocumented
/posts/validate endpoint. Validation does not fetch media, so local and external
byte verification below remains mandatory. No draft is created for preflight.
"""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import logging
import math
import mimetypes
import os
import re
import socket
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote, unquote, urlparse, urlunparse

from myUtils import delivery as delivery_state
from myUtils import media_pipeline, platform_limits
from myUtils import sociamonials_policy as policy

logger = logging.getLogger(__name__)
BASE_URL = "https://www.sociamonials.com"
POSTS_URL = f"{BASE_URL}/api/v1/posts"
# This is deliberately the documented dry-run surface, not a guessed endpoint.
POST_VALIDATE_URL = POSTS_URL
MEDIA_UPLOADS_URL = f"{BASE_URL}/api/v1/media/uploads"
MEDIA_ASSETS_URL = f"{BASE_URL}/api/v1/media/assets"
DEFAULT_WORKSPACE_ID = "26985"
ENABLED_ENV = "SAU_SOCIAMONIALS_FALLBACK"
API_KEY_ENV = "SOCIAMONIALS_API_KEY"
WORKSPACE_ENV = "SOCIAMONIALS_WORKSPACE_ID"
TEACHING_API_KEY_ENV = "SOCIAMONIALS_TEACHING_API_KEY"
TEACHING_WORKSPACE_ENV = "SOCIAMONIALS_TEACHING_WORKSPACE_ID"
DEFAULT_TEACHING_WORKSPACE_ID = "34293"
SECRETS_FILE_ENV = "SOCIAMONIALS_SECRETS_FILE"
TIMEOUT_ENV = "SAU_SOCIAMONIALS_TIMEOUT"
ASSET_READY_TIMEOUT_ENV = "SAU_SOCIAMONIALS_ASSET_READY_TIMEOUT"
DELIVERY_TIMEOUT_ENV = "SAU_SOCIAMONIALS_DELIVERY_TIMEOUT"
REUSE_ASSETS_ENV = "SAU_SOCIAMONIALS_REUSE_ASSETS"
DEFAULT_TIMEOUT = 60.0
DEFAULT_DELIVERY_TIMEOUT = 30.0
DEFAULT_ASSET_READY_TIMEOUT = 60.0
MAX_HTTP_REQUESTS = 96
MAX_HTTP_SECONDS = 300.0
MAX_POLL_REQUESTS = 12
MAX_UPLOAD_GRANTS = 20
MAX_UPLOAD_PARTS = 128
PART_URL_BATCH = 32
MAX_REUSE_DETAILS = 8
_MEDIA_REQUIRED_NETWORKS = {"in", "tiktok", "yt", "pi"}
NETWORK_MAX_MESSAGE_CHARS = dict(policy.NETWORK_MAX_MESSAGE_CHARS)
NETWORK_MAX_IMAGES = dict(policy.NETWORK_MAX_IMAGES)
NETWORK_MAX_VIDEO_SECONDS = {
    code: seconds for code, platform in policy.NETWORK_TO_PLATFORM.items()
    if (seconds := platform_limits.video_max_seconds(platform))
}
LINK_IN_MAIN_POST_FORBIDDEN_NETWORKS = frozenset({"tw"})
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".m4v"}
_CONTEXT_SUFFIXES = {".txt", ".wav", ".mp3", ".flac", ".srt", ".vtt", ".json", ".md"}
_CONTEXT_ROLES = {"audio", "transcript", "metadata", "support", "subtitle", "manifest", "prompt"}
_NON_DIRECT_HOSTS = {
    "drive.google.com", "docs.google.com", "photos.google.com", "drive.usercontent.google.com",
    "dropbox.com", "www.dropbox.com", "localhost", "127.0.0.1", "0.0.0.0",
}
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,79}")
_PLACEHOLDER_IDS = {"", "0", "null", "none", "false", "true", "unknown", "undefined", "pending", "n/a",
                    "delivered", "published", "failed", "processing", "draft", "scheduled", "queued", "accepted", "error", "success", "ok"}

# Existing operational mappings are preserved, not repaired by similar names.
# Live grants are checked on EVERY new publication. Per-account config wins.
_TEACHING = {"workspace_id": DEFAULT_TEACHING_WORKSPACE_ID, "api_key_env": TEACHING_API_KEY_ENV}
SAU_ACCOUNT_TO_SOCIAMONIALS: dict[int, dict[str, Any]] = {
    11: {"network": "fb", "profile_refs": ["19784|175913"], "name": "Nakedwill2"},
    64: {"network": "fb", "profile_refs": ["30186|255814"], "name": "Sexualwill"},
    72: {"network": "in", "profile_refs": ["52817"], "name": "nakedwill8"},
    75: {"network": "in", "profile_refs": ["82894"], "name": "sexualwill8"},
    123: {"network": "tw", "profile_refs": ["13425"], "name": "model_will"},
    124: {"network": "tw", "profile_refs": ["13426"], "name": "nudeweiwei"},
    77: {"network": "tw", "profile_refs": ["14100"], "name": "will_sexual"},
    103: {"network": "tw", "profile_refs": ["14099"], "name": "nakedhappylife"},
    118: {"network": "blsk", "profile_refs": ["1280"], "name": "nakedwill.bsky.social"},
    119: {"network": "blsk", "profile_refs": ["1279"], "name": "nudeweiwei.bsky.social"},
    120: {"network": "blsk", "profile_refs": ["1281"], "name": "sexualwill.bsky.social"},
    121: {"network": "blsk", "profile_refs": ["1282"], "name": "nakedhappylife.bsky.social"},
    62: {"network": "thrd", "profile_refs": ["361"], "name": "nakedwill8"},
    42: {"network": "thrd", "profile_refs": ["1939"], "name": "sexualwill8"},
    109: {"network": "tiktok", "profile_refs": ["3917"], "name": "Nakedwill"},
    110: {"network": "yt", "profile_refs": ["13223"], "name": "itsnakedwill"},
    43: {"network": "in", "profile_refs": ["70076"], "name": "supercool_bd", **_TEACHING},
    58: {"network": "fb", "profile_refs": ["26317|220114"], "name": "威威教學", **_TEACHING},
    61: {"network": "thrd", "profile_refs": ["1102"], "name": "supercool_bd", **_TEACHING},
    100: {"network": "tiktok", "profile_refs": ["5800"], "name": "weiwei_wang0", **_TEACHING},
    107: {"network": "tw", "profile_refs": ["17650"], "name": "willytutordev", **_TEACHING},
    108: {"network": "yt", "profile_refs": ["11020"], "name": "威威🌞", **_TEACHING},
    125: {"network": "blsk", "profile_refs": ["1278"], "name": "iamwillywang.bsky.social", **_TEACHING},
}


class SociamonialsFallbackError(RuntimeError):
    """Safe-to-report error, with explicit retryability and submission safety."""

    def __init__(self, message: str, *, retryable: bool = False, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.details = {"provider": "sociamonials", "retrySafe": True, **dict(details or {})}


class SociamonialsNotConfigured(SociamonialsFallbackError):
    """No authorized/configured route; never guess a different credential."""


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _testing() -> bool:
    return _truthy(os.environ.get("SAU_TESTING")) or "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _key_for_env(env_name: str, env: Mapping[str, str] | None = None) -> str:
    environ = os.environ if env is None else env
    key = str(environ.get(env_name) or "").strip()
    if key:
        return key
    secrets_path = str(environ.get(SECRETS_FILE_ENV) or "").strip()
    # Explicit files are supported in tests; implicit HOME secret reads are not.
    if not secrets_path and env is None and not _testing():
        default_path = Path.home() / ".claude" / "secrets.json"
        if default_path.is_file():
            secrets_path = str(default_path)
    if secrets_path:
        try:
            data = json.loads(Path(secrets_path).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return str(data.get(env_name) or "").strip()
        except (OSError, ValueError):
            logger.debug("Could not read the explicitly selected Sociamonials credential file")
    return ""


def get_api_key(env: Mapping[str, str] | None = None) -> str:
    return _key_for_env(API_KEY_ENV, env)


def get_teaching_api_key(env: Mapping[str, str] | None = None) -> str:
    return _key_for_env(TEACHING_API_KEY_ENV, env)


def get_workspace_id(env: Mapping[str, str] | None = None) -> str:
    return str((os.environ if env is None else env).get(WORKSPACE_ENV) or DEFAULT_WORKSPACE_ID).strip()


def get_teaching_workspace_id(env: Mapping[str, str] | None = None) -> str:
    return str((os.environ if env is None else env).get(TEACHING_WORKSPACE_ENV) or DEFAULT_TEACHING_WORKSPACE_ID).strip()


def resolve_credentials(mapping: Mapping[str, Any] | None, *, api_key: str | None = None,
                        workspace_id: str | None = None) -> tuple[str, str]:
    """Explicit key argument, otherwise ONLY the selected env/file key.

    A named api_key_env never falls through to the global key. Teaching's
    workspace cannot use the adult/default credential, even if it is present.
    """
    entry = mapping or {}
    env_name = str(entry.get("api_key_env") or "").strip()
    ws = str(workspace_id or entry.get("workspace_id") or "").strip()
    if not ws:
        ws = get_teaching_workspace_id() if env_name == TEACHING_API_KEY_ENV else get_workspace_id()
    if ws == DEFAULT_TEACHING_WORKSPACE_ID:
        if env_name and env_name != TEACHING_API_KEY_ENV:
            raise SociamonialsNotConfigured("Teaching workspace requires its isolated credential selector")
        env_name = TEACHING_API_KEY_ENV
    if env_name == TEACHING_API_KEY_ENV and ws != get_teaching_workspace_id():
        raise SociamonialsNotConfigured("Teaching credential cannot be routed to another workspace")
    key = str(api_key or "").strip() or _key_for_env(env_name or API_KEY_ENV)
    return key, ws


def is_configured(env: Mapping[str, str] | None = None) -> bool:
    return bool(get_api_key(env) or get_teaching_api_key(env))


def is_enabled(env: Mapping[str, str] | None = None) -> bool:
    return _truthy((os.environ if env is None else env).get(ENABLED_ENV)) and is_configured(env)


def should_attempt_fallback(*, retryable: bool | None, attempts: int, max_attempts: int,
                            enabled: bool | None = None) -> bool:
    """Legacy budget guard. The worker separately proves backup eligibility."""
    if retryable is False or attempts < max_attempts:
        return False
    return is_enabled() if enabled is None else bool(enabled)


def _account_value(account: Any, key: str, default: Any = None) -> Any:
    return account.get(key, default) if isinstance(account, Mapping) else getattr(account, key, default)


def _platform_network(platform: str | None) -> str | None:
    text = str(platform or "").strip().lower()
    if text == "google_business_profile":
        return "gmb"
    return next((network for network, name in policy.NETWORK_TO_PLATFORM.items() if name == text), None)


def _normalise_entry(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, Mapping):
        return None
    refs = entry.get("profile_refs", entry.get("profile_ref"))
    if isinstance(refs, (str, int)) and not isinstance(refs, bool):
        refs = [refs]
    if not isinstance(refs, (list, tuple)) or any(isinstance(ref, bool) or not isinstance(ref, (str, int)) for ref in refs):
        return None
    refs = [str(ref).strip() for ref in refs]
    if not refs or any(not ref or len(ref) > 128 or not re.fullmatch(r"[A-Za-z0-9_.:|@-]+", ref) for ref in refs):
        return None
    result = {"network": str(entry.get("network") or "").strip().lower(), "profile_refs": refs,
              "name": str(entry.get("name") or "").strip() or None}
    for key in ("workspace_id", "api_key_env"):
        if entry.get(key) is not None:
            text = str(entry[key]).strip()
            if not text:
                return None
            result[key] = text
    return result


def _from_settings(settings: Any, account_id: int | None) -> dict[str, Any] | None:
    if not isinstance(settings, Mapping) or not isinstance(settings.get("sociamonials"), Mapping):
        return None
    raw = settings["sociamonials"]
    if "network" in raw or "profile_refs" in raw or "profile_ref" in raw:
        return _normalise_entry(raw)
    accounts = raw.get("accounts") if isinstance(raw.get("accounts"), Mapping) else raw
    found = accounts.get(account_id, accounts.get(str(account_id))) if account_id is not None else None
    if not isinstance(found, Mapping):
        return None
    merged = dict(found)
    for key in ("network", "workspace_id", "api_key_env"):
        if key in raw:
            merged.setdefault(key, raw[key])
    return _normalise_entry(merged)


def resolve_mapping(account_id: int | None, *, platform: str | None = None, account: Any = None,
                    settings: Any = None) -> dict[str, Any] | None:
    """Account override, profile override, then unchanged historical mapping.

    A malformed EXPLICIT mapping is a denial, not permission to use a built-in.
    Live verification below prevents stale/banned/disconnected destinations.
    """
    config = _account_value(account, "config", {}) or {}
    if isinstance(config, Mapping) and "sociamonials" in config:
        entry = _normalise_entry(config["sociamonials"])
    else:
        entry = _from_settings(settings, account_id)
        if entry is None:
            raw = (settings or {}).get("sociamonials") if isinstance(settings, Mapping) else None
            accounts = raw.get("accounts", raw) if isinstance(raw, Mapping) else {}
            if isinstance(raw, Mapping) and ("network" in raw or str(account_id) in accounts or account_id in accounts):
                return None
            entry = _normalise_entry(SAU_ACCOUNT_TO_SOCIAMONIALS.get(account_id))
    if entry and not entry["network"]:
        entry["network"] = _platform_network(platform) or ""
    return entry


def prefers_sociamonials(account: Any, *, platform: str) -> bool:
    """Preserve explicit primary routing; never infer it from failed native auth."""
    config = _account_value(account, "config", {}) or {}
    if not isinstance(config, Mapping):
        raise SociamonialsNotConfigured("Invalid publishing route configuration")
    provider = str(config.get("publishingProvider") or "direct").strip().lower()
    if provider == "direct":
        return False
    if provider != "sociamonials":
        raise SociamonialsNotConfigured("Unsupported configured publishing provider")
    entry = _normalise_entry(config.get("sociamonials"))
    if not entry or entry["network"] != _platform_network(platform) or len(entry["profile_refs"]) != 1:
        raise SociamonialsNotConfigured("Primary Sociamonials route requires one explicit matching-platform destination")
    return True


def _bounded_timeout(value: Any, default: float, maximum: float = 60.0, *, zero: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    if not math.isfinite(number):
        number = default
    return max(0.0 if zero else 0.1, min(number, maximum))


def _timeout() -> float:
    return _bounded_timeout(os.environ.get(TIMEOUT_ENV), DEFAULT_TIMEOUT)


def _delivery_timeout() -> float:
    return _bounded_timeout(os.environ.get(DELIVERY_TIMEOUT_ENV), DEFAULT_DELIVERY_TIMEOUT, zero=True)


def _asset_ready_timeout() -> float:
    return _bounded_timeout(os.environ.get(ASSET_READY_TIMEOUT_ENV), DEFAULT_ASSET_READY_TIMEOUT, zero=True)


def _workspace_int(workspace_id: Any) -> int | None:
    if isinstance(workspace_id, bool):
        return None
    try:
        value = int(str(workspace_id).strip())
        return value if 0 < value < 2**63 else None
    except (TypeError, ValueError):
        return None


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def _json_body(response: Any) -> dict[str, Any]:
    try:
        body = response.json()
    except (ValueError, TypeError):
        return {}
    return body if isinstance(body, dict) else {}


def _data(body: Mapping[str, Any]) -> dict[str, Any]:
    return dict(body["data"]) if isinstance(body.get("data"), Mapping) else dict(body)


def _safe_code(value: Any, default: str = "provider_error") -> str:
    if not isinstance(value, str) or not _SAFE_CODE.fullmatch(value) or value.lower().startswith(("sm_agent_", "sma_", "sk-", "ghp_")):
        return default
    return value


def _rate_details(response: Any) -> dict[str, Any]:
    headers = {str(k).lower(): v for k, v in (getattr(response, "headers", {}) or {}).items()}
    result = {}
    for header, key in (("retry-after", "retryAfter"), ("x-ratelimit-reset", "rateLimitReset"),
                        ("ratelimit-reset", "rateLimitReset")):
        raw = str(headers.get(header) or "")
        if re.fullmatch(r"[0-9A-Za-z,: +./-]{1,128}", raw):
            result[key] = raw
    return result


def _raise_for_status(response: Any, *, context: str, side_effects: bool = True) -> None:
    status = int(getattr(response, "status_code", 0) or 0)
    if 200 <= status < 300:
        return
    error = _data(_json_body(response)).get("error")
    code = _safe_code(error.get("code") if isinstance(error, Mapping) else error)
    details = {"httpStatus": status, "providerCode": code, "stage": context,
               "retrySafe": not side_effects or (400 <= status < 500 and status not in {408, 409}),
               **_rate_details(response)}
    if isinstance(error, Mapping) and isinstance(error.get("errors"), Mapping):
        details["fields"] = [key for key in error["errors"] if isinstance(key, str) and _SAFE_CODE.fullmatch(key)][:8]
    # Never attach the raw response, error message, request, credential or URL.
    raise SociamonialsFallbackError(f"Sociamonials {context} rejected (HTTP {status}, code={code})",
                                   retryable=status == 429 or status >= 500 or status == 0, details=details)


_RATE_LOCK = threading.Lock()
_RATE_WINDOWS: dict[tuple[str, str], deque] = {}
_STORAGE_LOCK = threading.Lock()  # All pooled workspaces in this process, not one lock per workspace.


def _take_rate(key_digest: str, bucket: str, limit: int) -> None:
    now = time.monotonic()
    with _RATE_LOCK:
        if len(_RATE_WINDOWS) > 256:
            for key in list(_RATE_WINDOWS):
                if not _RATE_WINDOWS[key] or now - _RATE_WINDOWS[key][-1] >= 60:
                    del _RATE_WINDOWS[key]
        window = _RATE_WINDOWS.setdefault((key_digest, bucket), deque())
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= limit:
            raise SociamonialsFallbackError("Sociamonials credential rate budget reached; release worker concurrency",
                                           retryable=True, details={"httpStatus": 429, "retryAfter": max(1, math.ceil(60 - (now - window[0]))),
                                                                    "rateBucket": bucket, "retrySafe": True})
        window.append(now)


class _HTTP:
    """No automatic HTTP retry; finite calls and shared per-credential pacing."""

    def __init__(self, session: Any, key: str):
        self.session = session
        self.key_digest = hashlib.sha256(key.encode()).hexdigest()
        self.calls = 0
        self.agency = False
        self.grants = 0
        self.deadline = time.monotonic() + MAX_HTTP_SECONDS

    def _request(self, method: str, url: str, *, context: str = "request", side_effects: bool = False, **kwargs):
        remaining = self.deadline - time.monotonic()
        if self.calls >= MAX_HTTP_REQUESTS or remaining <= 0:
            raise SociamonialsFallbackError("Bounded backup HTTP/time budget reached; resume from its checkpoint",
                                           retryable=True, details={"retryAfter": 5, "stage": context, "retrySafe": True})
        provider = url.startswith(BASE_URL + "/api/v1/")
        bucket = "read" if provider and method == "get" else "write" if provider else "transfer"
        limit = {"read": 120, "write": 30, "transfer": 300}[bucket]
        if self.agency and bucket != "transfer":
            limit *= 2
        _take_rate(self.key_digest, bucket, limit)
        self.calls += 1
        kwargs.setdefault("allow_redirects", False)
        kwargs["timeout"] = min(_bounded_timeout(kwargs.get("timeout"), DEFAULT_TIMEOUT), remaining)
        try:
            return getattr(self.session, method)(url, **kwargs)
        except AssertionError:
            raise  # Missing contract mocks should fail loudly, not become retries.
        except Exception:
            raise SociamonialsFallbackError("Sociamonials transport response is unavailable; reconcile any started write",
                                           retryable=True, details={"stage": context, "retrySafe": not side_effects,
                                                                    "transportUnknown": True}) from None

    def get(self, url: str, **kwargs):
        return self._request("get", url, **kwargs)

    def post(self, url: str, **kwargs):
        kwargs.setdefault("side_effects", True)
        return self._request("post", url, **kwargs)

    def put(self, url: str, **kwargs):
        kwargs.setdefault("side_effects", True)
        return self._request("put", url, **kwargs)


def _get_session(session: Any = None):
    if session is not None:
        return session
    if _testing():
        raise SociamonialsNotConfigured("Tests must inject a verified mock HTTP session; external publishing is disabled")
    import requests
    result = requests.Session()
    result.trust_env = False
    return result


def _http(session: Any, key: str) -> _HTTP:
    return session if isinstance(session, _HTTP) else _HTTP(_get_session(session), key)


def _identity(http: _HTTP, headers: Mapping[str, str], ws: str, *, timeout: float,
              permissions: Sequence[str]) -> dict[str, Any]:
    response = http.get(f"{BASE_URL}/api/v1/me", headers=dict(headers), timeout=timeout, context="identity")
    _raise_for_status(response, context="identity", side_effects=False)
    body = _data(_json_body(response))
    agent, entitlement, workspaces = body.get("agent"), body.get("entitlement"), body.get("workspaces")
    if (not isinstance(agent, Mapping) or agent.get("status") != "active"
            or not isinstance(entitlement, Mapping) or entitlement.get("state") != "active"
            or not isinstance(workspaces, Mapping) or not isinstance(workspaces.get(ws), Mapping)):
        raise SociamonialsNotConfigured("Credential identity/entitlement does not prove access to the exact workspace")
    entry = dict(workspaces[ws])
    grants = entry.get("permissions")
    if not isinstance(grants, Mapping) or any(grants.get(name) is not True for name in permissions):
        raise SociamonialsNotConfigured("Credential lacks a required exact-workspace permission",
                                       details={"requiredPermissions": list(permissions)})
    http.agency = agent.get("type") == "agency"
    return entry


def _required_capabilities(payload: Mapping[str, Any] | None = None, *, intent: Mapping[str, Any] | None = None) -> set[str]:
    source = payload or {}
    required = set()
    if (intent or {}).get("message") or compose_message(source, network="fb"):
        required.add("text")
    media = collect_media(source)
    for item in media:
        if item["kind"] != "thumbnail":
            required.add(item["kind"])
    if len([item for item in media if item["kind"] == "image"]) > 1:
        required.add("carousel")
    if ((intent or {}).get("top_fields") or {}).get("first_comment") or _first_comment(source):
        required.add("first_comment")
    if ((intent or {}).get("top_fields") or {}).get("story"):
        required.add("story")
    return required


def _verify_route(http: _HTTP, headers: Mapping[str, str], mapping: Mapping[str, Any], ws: str,
                  *, platform: str, timeout: float, required: set[str]) -> dict[str, Any]:
    network = mapping.get("network")
    refs = mapping.get("profile_refs")
    if network != _platform_network(platform) or network not in policy.NETWORK_TO_PLATFORM or not isinstance(refs, list) or len(refs) != 1:
        raise SociamonialsNotConfigured("Backup requires one exact matching-platform profile, not guessed substitutions")
    if _workspace_int(ws) is None:
        raise SociamonialsNotConfigured("Backup requires an explicit usable workspace id")
    identity = _identity(http, headers, ws, timeout=timeout, permissions=("posts.read", "posts.create", "profiles.read"))
    assigned = identity.get("social_profiles")
    if (not isinstance(assigned, list) or len([row for row in assigned if isinstance(row, Mapping)
                                              and row.get("network") == network and row.get("profile_ref") == refs[0]]) != 1):
        raise SociamonialsNotConfigured("Credential has no exact network/profile-ref grant in this workspace")
    response = http.get(f"{BASE_URL}/api/v1/workspaces/{ws}/social-profiles", headers=dict(headers),
                        timeout=timeout, context="profile grants")
    _raise_for_status(response, context="profile grants", side_effects=False)
    body = _data(_json_body(response))
    if _workspace_int(body.get("workspace_registration_id")) != _workspace_int(ws):
        raise SociamonialsNotConfigured("Profile response belongs to another or unknown workspace")
    profiles = body.get("profiles")
    matches = [row for row in profiles or [] if isinstance(row, Mapping) and row.get("network") == network and row.get("profile_ref") == refs[0]]
    if len(matches) != 1:
        raise SociamonialsNotConfigured("Exact mapped profile is absent or ambiguous; do not replace it by name")
    profile = matches[0]
    capabilities = profile.get("capabilities")
    if (profile.get("connected") is not True or profile.get("agent_can_publish_to") is not True
            or "unavailable_reason" not in profile or profile["unavailable_reason"] is not None
            or not isinstance(capabilities, list) or any(not isinstance(cap, str) for cap in capabilities)
            or "mobile_app_handoff" in capabilities or profile.get("mobile_app_handoff") not in (None, False)):
        raise SociamonialsNotConfigured("Mapped profile is disconnected, unavailable, unauthorized or requires a mobile handoff")
    missing = required - set(capabilities)
    if missing:
        raise SociamonialsNotConfigured("Exact destination lacks required original-content capabilities",
                                       details={"missingCapabilities": sorted(missing)})
    grants = identity["permissions"]
    return {"verified": True,
            "mapping": {"network": network, "profile_refs": list(refs), "workspace_id": ws,
                        "api_key_env": mapping.get("api_key_env") or (TEACHING_API_KEY_ENV if ws == DEFAULT_TEACHING_WORKSPACE_ID else API_KEY_ENV)},
            "capabilities": sorted(set(capabilities)),
            "permissions": {name: grants.get(name) is True for name in ("assets.read", "assets.write", "posts.publish_direct")},
            "requires_approval": identity.get("requires_approval_for_posting") is True or grants.get("posts.publish_direct") is not True,
            "rate_limits": {"reads_per_minute": 240 if http.agency else 120, "writes_per_minute": 60 if http.agency else 30,
                            "media_transfers_per_minute": 300},
            "verified_at": delivery_state.utc_now()}


def _read_storage(http: Any, headers: Mapping[str, str], ws: str, *, timeout: float) -> dict[str, Any]:
    response = http.get(f"{BASE_URL}/api/v1/workspaces/{ws}/context", headers=dict(headers), timeout=timeout)
    _raise_for_status(response, context="storage context", side_effects=False)
    body = _data(_json_body(response))
    declared_ws = body.get("workspace_registration_id")
    if declared_ws is None and isinstance(body.get("workspace"), Mapping):
        declared_ws = body["workspace"].get("workspace_registration_id", body["workspace"].get("id"))
    if declared_ws is not None and _workspace_int(declared_ws) != _workspace_int(ws):
        raise SociamonialsFallbackError("Storage context is for another workspace", details={"storageUnknown": True})
    storage = body.get("storage")
    if not isinstance(storage, Mapping) or storage.get("status") != "ok":
        raise SociamonialsFallbackError("Workspace storage accounting is unavailable; verify quota/entitlement or reuse an exact ready asset",
                                       details={"storageUnknown": True})
    maximum = storage.get("max_upload_bytes")
    available = storage.get("available_bytes")
    if (not isinstance(maximum, Mapping) or not {"image", "video"} <= set(maximum)
            or any(value is not None and (type(value) is not int or value <= 0) for value in (maximum["image"], maximum["video"]))
            or type(storage.get("enforcement")) is not bool or type(storage.get("pooled")) is not bool
            or (not (type(available) is int and available >= 0) and not (available is None and storage.get("unmetered") is True))
            or storage.get("unconfigured") is True):
        raise SociamonialsFallbackError("Storage response is incomplete; confirm effective hosted limits and pooled quota before uploading",
                                       details={"storageUnknown": True})
    return {"status": "ok", "max_upload_bytes": dict(maximum), "available_bytes": available,
            "enforcement": storage["enforcement"], "pooled": storage["pooled"],
            "unmetered": storage.get("unmetered") is True,
            "ratchet_active": isinstance(storage.get("ratchet"), Mapping) and storage["ratchet"].get("active") is True,
            "requires_approval": isinstance(body.get("approval"), Mapping) and (
                body["approval"].get("credential_routes_through_approval") is True or body["approval"].get("client_requested_hold") is True)}


def verify_destination(*, platform: str, account: Any = None, account_id: int | None = None,
                       mapping: Mapping[str, Any] | None = None, settings: Any = None,
                       payload: Mapping[str, Any] | None = None, required_capabilities: Sequence[str] | None = None,
                       session: Any = None, api_key: str | None = None, workspace_id: str | None = None,
                       timeout: float | None = None, require_storage: bool = True) -> dict[str, Any]:
    """Read-only preparation gate returning verified mapping + conservative caps.

    No secrets/customer content are returned. Default require_storage=True fails
    closed on unknown accounting. False is ONLY useful to discover grants before
    proving an already-ready asset; it returns no hosted-video entitlement.
    This result is not a cached authorization token: publish always re-verifies.
    """
    account_id = _account_value(account, "id", account_id)
    entry = _normalise_entry(mapping) if mapping is not None else resolve_mapping(account_id, platform=platform, account=account, settings=settings)
    if not entry:
        raise SociamonialsNotConfigured("No explicit usable Sociamonials destination is mapped for this account")
    key, ws = resolve_credentials(entry, api_key=api_key, workspace_id=workspace_id)
    if not key:
        raise SociamonialsNotConfigured("The exact destination's selected Sociamonials credential is missing")
    http, headers = _http(session, key), _headers(key)
    budget = _bounded_timeout(timeout, _timeout())
    required = set(required_capabilities or ()) | _required_capabilities(payload)
    result = _verify_route(http, headers, entry, ws, platform=platform, timeout=budget, required=required)
    storage = _read_storage(http, headers, ws, timeout=budget) if require_storage else None
    result["storage"] = storage
    result["limits"] = policy.limits_for(entry["network"], storage)
    if storage:
        result["requires_approval"] = result["requires_approval"] or storage["requires_approval"]
    return result


def _media_kind(path: str) -> str:
    suffix = Path(urlparse(path).path).suffix.lower()
    return "image" if suffix in _IMAGE_SUFFIXES else "video" if suffix in _VIDEO_SUFFIXES else ""


def collect_media(payload: Mapping[str, Any] | None, media_paths: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Preserve all declared media in order; exclude only explicit context files."""
    items = []
    artifacts = (payload or {}).get("artifacts") or []
    if not isinstance(artifacts, (list, tuple)):
        raise SociamonialsFallbackError("Artifacts must be an ordered list")
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            raise SociamonialsFallbackError("Declared media is invalid; refusing a text-only replacement")
        metadata = artifact.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            raise SociamonialsFallbackError("Declared media metadata is invalid")
        local = str(artifact.get("local_path") or "").strip()
        public = str(artifact.get("public_url") or "").strip()
        role = str(metadata.get("role") or "").lower()
        artifact_kind = str(artifact.get("artifact_kind") or "").lower()
        suffix = Path(urlparse(local or public).path).suffix.lower()
        if (role in _CONTEXT_ROLES or suffix in _CONTEXT_SUFFIXES or "transcript" in artifact_kind or "audio" in artifact_kind
                or artifact_kind in {"support", "metadata", "context"} or artifact_kind.startswith(("support_", "metadata_"))):
            continue
        kind = _media_kind(local or public)
        if not kind or (role in {"video", "image"} and role != kind):
            raise SociamonialsFallbackError("Declared media is unsupported or mismatched; refusing a text-only replacement")
        if role in {"thumbnail", "cover"}:
            if kind != "image":
                raise SociamonialsFallbackError("A declared video thumbnail must be an image")
            kind = "thumbnail"
        items.append({"local_path": local or None, "public_url": public or None, "kind": kind,
                      "metadata": dict(metadata), "artifact": dict(artifact)})
    if not artifacts:
        for path in media_paths or []:
            value = str(path or "").strip()
            if Path(value).suffix.lower() in _CONTEXT_SUFFIXES:
                continue
            kind = _media_kind(value)
            if not kind:
                raise SociamonialsFallbackError("An explicit media path is unsupported")
            items.append({"local_path": value, "public_url": None, "kind": kind, "metadata": {}, "artifact": {}})
    return items


def _sha256(local_path: Path) -> str:
    digest = hashlib.sha256()
    try:
        expected_size = local_path.stat().st_size
        with local_path.open("rb") as handle:
            remaining = expected_size
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise SociamonialsFallbackError("Exact media was truncated during hashing")
                digest.update(chunk)
                remaining -= len(chunk)
            if handle.read(1) or local_path.stat().st_size != expected_size:
                raise SociamonialsFallbackError("Exact media grew or changed size during hashing")
    except OSError:
        raise SociamonialsFallbackError("Exact local media became unreadable; restore it before backup") from None
    return digest.hexdigest()


def _assert_media_size(network: str, size_bytes: int, *, kind: str = "video") -> None:
    cap = policy.IMAGE_MAX_BYTES if kind in {"image", "thumbnail"} else policy.HOSTED_VIDEO_MAX_BYTES
    if kind == "video":
        native = platform_limits.media_max_mb(policy.NETWORK_TO_PLATFORM.get(network))
        if native:
            cap = min(cap, int(native * 1_000_000))
    if type(size_bytes) is not int or not 0 < size_bytes <= cap:
        raise SociamonialsFallbackError("Exact local media exceeds the backup/native byte budget; prepare and schedule every compliant part",
                                       details={"mediaReviewRequired": True, "size_bytes": size_bytes, "max_bytes": cap})


def _assert_video_duration(network: str, local_path: str | None) -> None:
    if not local_path or not Path(local_path).is_file():
        raise SociamonialsFallbackError("Materialise the exact local video before backup validation")
    try:
        duration = media_pipeline.probe_video_duration(local_path)
    except Exception:
        raise SociamonialsFallbackError("Could not verify the local video's duration; preprocess it before backup") from None
    limit = NETWORK_MAX_VIDEO_SECONDS.get(network)
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise SociamonialsFallbackError("Local video duration could not be proven")
    if limit and duration > limit:
        raise SociamonialsFallbackError("Video duration exceeds the backup/native limit; schedule all prepared parts",
                                       details={"duration_seconds": duration, "max_seconds": limit})


def _checked_media(items: list[dict[str, Any]], network: str) -> list[dict[str, Any]]:
    for item in items:
        local = item.get("local_path")
        if not local or not Path(local).is_file():
            from myUtils.media_compliance import MediaPreparationRequired
            raise MediaPreparationRequired("Materialise the exact local media before backup; never use an unverified/oversized original URL")
        path = Path(local)
        size = path.stat().st_size
        _assert_media_size(network, size, kind=item["kind"])
        digest = _sha256(path)
        metadata, artifact = item["metadata"], item["artifact"]
        expected = metadata.get("sha256", artifact.get("sha256"))
        if expected is not None and expected != digest:
            raise SociamonialsFallbackError("Prepared media checksum changed; do not publish tampered bytes", details={"mediaReviewRequired": True})
        expected_size = metadata.get("size_bytes", artifact.get("size_bytes"))
        if expected_size is not None and expected_size != size:
            raise SociamonialsFallbackError("Prepared media byte count changed; do not publish tampered bytes")
        if item["kind"] == "video":
            _assert_video_duration(network, local)
        else:
            try:
                from PIL import Image
                with Image.open(path) as image:
                    width, height = image.size
                    if image.format not in {"JPEG", "PNG", "GIF", "WEBP"} or (item["kind"] == "thumbnail" and image.format not in {"JPEG", "PNG"}):
                        raise ValueError("Unsupported image encoding")
                    if width <= 0 or height <= 0 or width * height > policy.IMAGE_MAX_PIXELS or max(width, height) > policy.IMAGE_MAX_LONG_SIDE:
                        raise ValueError("Image geometry exceeds policy")
                    image.verify()
            except Exception:
                raise SociamonialsFallbackError("Image encoding/dimensions cannot be verified against the backup policy") from None
        item.update({"sha256": digest, "size_bytes": size})
    return items


def _normalize_public_url(url: str) -> str:
    parts = urlparse(str(url or "").strip())
    return urlunparse((parts.scheme, parts.netloc, quote(parts.path, safe="/%:@!$&'()*+;="), parts.params, parts.query, parts.fragment))


def is_direct_media_url(url: str | None) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
        host = (parsed.hostname or "").lower()
        if (parsed.scheme != "https" or not host or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.query or parsed.fragment or parsed.params
                or host in _NON_DIRECT_HOSTS or host.endswith((".localhost", ".local", ".internal"))
                or "/file/d/" in parsed.path or any(char in unquote(parsed.path) for char in ("\r", "\n", "\x00"))):
            return False
        try:
            if not ipaddress.ip_address(host).is_global:
                return False
        except ValueError:
            pass
        return _media_kind(parsed.path) != ""
    except ValueError:
        return False


def _resolve_public_host(host: str) -> None:
    """DNS guard; tests inject it. Reject any non-public address, not just localhost."""
    try:
        addresses = {row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError("Not public")
    except Exception:
        raise SociamonialsFallbackError("Media host cannot be verified as public HTTPS; use an immutable public CDN object") from None


def _verify_remote_media(url: str, session: Any, *, timeout: float, local_path: Path | None = None,
                         sha256: str | None = None, size_bytes: int | None = None, max_bytes: int | None = None,
                         immutable: bool = False) -> bool:
    """Stream a bounded HTTPS GET and prove equality to the exact LOCAL bytes.

    HEAD failures, redirects, credential/expiring URLs, mutable paths, unknown
    checksums and HTML responses are NEVER permission to publish. No credentials
    are sent to a public media host. This cannot be disabled by verify_media=False.
    """
    if not is_direct_media_url(url) or not immutable or not local_path or not local_path.is_file():
        return False
    if not isinstance(sha256, str) or not _SHA_RE.fullmatch(sha256) or type(size_bytes) is not int or type(max_bytes) is not int:
        return False
    if not 0 < size_bytes <= max_bytes or local_path.stat().st_size != size_bytes or _sha256(local_path) != sha256:
        return False
    _resolve_public_host(urlparse(url).hostname or "")
    response = None
    deadline = time.monotonic() + _bounded_timeout(timeout, DEFAULT_TIMEOUT)
    try:
        response = session.get(url, headers={"Accept-Encoding": "identity", "Authorization": None, "Cookie": None},
                               auth=(), cookies={}, timeout=timeout, stream=True, allow_redirects=False)
        if int(getattr(response, "status_code", 0) or 0) != 200:
            return False
        headers = {str(k).lower(): str(v) for k, v in (getattr(response, "headers", {}) or {}).items()}
        if headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
            return False
        kind = _media_kind(str(local_path))
        if not headers.get("content-type", "").lower().startswith(kind + "/"):
            return False
        if headers.get("content-length") and (not headers["content-length"].isdigit() or int(headers["content-length"]) != size_bytes):
            return False
        digest, received = hashlib.sha256(), 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            received += len(chunk)
            if received > size_bytes or received > max_bytes or time.monotonic() > deadline:
                return False
            digest.update(chunk)
        return received == size_bytes and digest.hexdigest() == sha256
    except SociamonialsFallbackError:
        raise
    except Exception:
        return False
    finally:
        if response is not None and callable(getattr(response, "close", None)):
            response.close()


def _external_reference(item: Mapping[str, Any], http: Any, *, timeout: float) -> str:
    url = _normalize_public_url(str(item.get("public_url") or ""))
    metadata = item.get("metadata") or {}
    digest = item["sha256"]
    immutable = (digest in unquote(urlparse(url).path).lower() or metadata.get("public_url_immutable") is True)
    # An explicit URL-specific checksum can tighten provenance, never replace
    # the computed local checksum with the raw original's checksum.
    if metadata.get("public_url_sha256", digest) != digest:
        immutable = False
    maximum = policy.EXTERNAL_VIDEO_MAX_BYTES if item["kind"] == "video" else policy.IMAGE_MAX_BYTES
    if not _verify_remote_media(url, http, timeout=timeout, local_path=Path(item["local_path"]),
                                sha256=digest, size_bytes=item["size_bytes"], max_bytes=maximum, immutable=immutable):
        raise SociamonialsFallbackError("Hosted media is unavailable and no stable public HTTPS URL proves the exact prepared bytes within its external limit",
                                       details={"mediaReviewRequired": True, "external_max_bytes": maximum})
    return url


def _asset_id(asset: Mapping[str, Any] | None) -> int | None:
    return _workspace_int(asset.get("asset_id")) if isinstance(asset, Mapping) else None


def _asset_preference(asset: Mapping[str, Any]) -> tuple[int, str, int]:
    return (0 if asset.get("starred") else 1, str(asset.get("created_at") or ""), _asset_id(asset) or 0)


def select_reusable_asset(assets: Sequence[Mapping[str, Any]] | None, *, filename: str = "", size_bytes: int,
                          kind: str, sha256: str | None = None, workspace_id: str | None = None,
                          exclude_ids: set[int] | None = None) -> Mapping[str, Any] | None:
    """Ready SHA-256 + exact size/kind/workspace identity; filename is NOT proof."""
    if not isinstance(sha256, str) or not _SHA_RE.fullmatch(sha256) or type(size_bytes) is not int or size_bytes <= 0:
        return None
    matches = []
    for asset in assets or []:
        if (not isinstance(asset, Mapping) or not _asset_id(asset) or asset.get("sha256") != sha256
                or type(asset.get("size_bytes")) is not int or asset["size_bytes"] != size_bytes
                or asset.get("media_type") != kind or asset.get("processing_status", asset.get("status")) != "ready"
                or asset.get("parent_asset_id") is not None or asset.get("deleted") is True
                or (exclude_ids and _asset_id(asset) in exclude_ids)
                or (workspace_id is not None and _workspace_int(asset.get("workspace_registration_id")) != _workspace_int(workspace_id))):
            continue
        matches.append(asset)
    return min(matches, key=_asset_preference) if matches else None


def _reuse_assets_enabled() -> bool:
    # Exact byte identity is safe and avoids filling pooled quota on retries.
    raw = os.environ.get(REUSE_ASSETS_ENV)
    return True if raw is None else _truthy(raw)


def _reuse_lookup_max_pages() -> int:
    try:
        return max(1, min(5, int(os.environ.get("SAU_SOCIAMONIALS_REUSE_MAX_PAGES") or 5)))
    except (TypeError, ValueError):
        return 5


def _asset_body(response: Any) -> dict[str, Any]:
    body = _data(_json_body(response))
    return dict(body["asset"]) if isinstance(body.get("asset"), Mapping) else body


def _lookup_reusable_asset(session: Any, headers: Mapping[str, str], workspace_id: str, *, filename: str,
                           size_bytes: int, kind: str, timeout: float, sha256: str | None = None) -> Mapping[str, Any] | None:
    if not _reuse_assets_enabled() or not sha256:
        return None
    details_read = 0
    for page in range(_reuse_lookup_max_pages()):
        response = session.get(MEDIA_ASSETS_URL, headers=dict(headers),
                               params={"workspace_registration_id": _workspace_int(workspace_id),
                                       "limit": 200, "offset": page * 200}, timeout=timeout)
        if int(getattr(response, "status_code", 0) or 0) == 403:
            return None  # Explicit lack of library browsing, not permission to skip quota.
        _raise_for_status(response, context="media reuse lookup", side_effects=False)
        rows = _data(_json_body(response)).get("assets")
        if not isinstance(rows, list):
            raise SociamonialsFallbackError("Library lookup returned an unrecognizable response; verify media identity before allocating more storage")
        for row in rows:
            if not isinstance(row, Mapping) or row.get("size_bytes") != size_bytes or row.get("media_type") != kind:
                continue
            if row.get("sha256") not in (None, sha256):
                continue
            asset_id = _asset_id(row)
            if not asset_id or details_read >= MAX_REUSE_DETAILS:
                continue
            # Full read proves workspace/provenance/hash; a list's name/size
            # alone never does, even if it is the oldest/starred candidate.
            full = session.get(f"{MEDIA_ASSETS_URL}/{asset_id}", headers=dict(headers), timeout=timeout)
            details_read += 1
            if int(getattr(full, "status_code", 0) or 0) == 404:
                continue
            _raise_for_status(full, context="media identity read", side_effects=False)
            match = select_reusable_asset([_asset_body(full)], filename=filename, size_bytes=size_bytes,
                                           kind=kind, sha256=sha256, workspace_id=workspace_id)
            if match:
                return match
        if len(rows) < 200:
            break
    return None


def _upload_checkpoint(payload: dict, digest: str, **changes: Any) -> dict[str, Any]:
    uploads = copy.deepcopy(delivery_state.state(payload).get("sociamonials_uploads") or {})
    current = {**uploads.get(digest, {}), **changes}
    uploads[digest] = current
    delivery_state.checkpoint(payload, sociamonials_uploads=uploads)
    return current


def _asset_receipt(asset: Mapping[str, Any], item: Mapping[str, Any], ws: str) -> dict[str, Any]:
    match = select_reusable_asset([asset], size_bytes=item["size_bytes"], kind=item["kind"], sha256=item["sha256"], workspace_id=ws)
    if not match:
        raise SociamonialsFallbackError("Provider media receipt does not prove exact ready bytes in this workspace",
                                       details={"mediaReviewRequired": True, "retrySafe": False})
    return {"asset_id": _asset_id(match), "workspace_id": _workspace_int(ws), "sha256": item["sha256"],
            "size_bytes": item["size_bytes"], "kind": item["kind"], "processing_status": "ready"}


def _saved_asset(http: Any, headers: Mapping[str, str], ws: str, item: Mapping[str, Any], payload: dict,
                 *, timeout: float) -> Mapping[str, Any] | None:
    saved = (delivery_state.state(payload).get("sociamonials_uploads") or {}).get(item["sha256"])
    if not isinstance(saved, Mapping):
        return None
    if (saved.get("workspace_id") != _workspace_int(ws) or saved.get("sha256") != item["sha256"]
            or saved.get("size_bytes") != item["size_bytes"] or saved.get("kind") != item["kind"]):
        raise SociamonialsFallbackError("Saved upload belongs to different bytes/workspace; reconcile it without a new upload", details={"retrySafe": False})
    asset_id = saved.get("asset_id") or saved.get("upload_id")
    if not _workspace_int(asset_id):
        return None
    response = http.get(f"{MEDIA_ASSETS_URL}/{asset_id}", headers=dict(headers), timeout=timeout)
    if int(getattr(response, "status_code", 0) or 0) == 404 and not saved.get("asset_id"):
        return None  # Reserved upload: replay the SAME grant or complete it, not allocate another.
    _raise_for_status(response, context="saved media receipt", side_effects=False)
    asset = _asset_body(response)
    state = asset.get("processing_status", asset.get("status"))
    if state in {"pending_upload", "completing"} and not saved.get("asset_id"):
        return None
    if state == "processing":
        asset = _wait_for_asset_ready(http, headers, asset_id, timeout=_asset_ready_timeout(), workspace_id=ws)
    receipt = _asset_receipt(asset, item, ws)
    _upload_checkpoint(payload, item["sha256"], **receipt, status="ready")
    return asset


def _guard_storage_for_upload(session: Any, headers: Mapping[str, str], workspace_id: str, *, local_path: Path,
                              size_bytes: int, db_path: Any = None, timeout: float) -> dict[str, Any]:
    """Read-only headroom gate. Never imports/calls sociamonials_reclaim."""
    storage = _read_storage(session, headers, workspace_id, timeout=timeout)
    cap = policy.limits_for("fb", storage)["hosted_video_max_bytes"] if _media_kind(str(local_path)) == "video" else policy.limits_for("fb", storage)["hosted_image_max_bytes"]
    if cap is None or size_bytes > cap:
        raise SociamonialsFallbackError("Effective hosted-media entitlement/byte ceiling refuses this upload",
                                       details={"providerCode": "hosted_media_limit", "max_bytes": cap})
    if storage["ratchet_active"] or (storage["available_bytes"] is not None and size_bytes > storage["available_bytes"]):
        raise SociamonialsFallbackError("Pooled storage has insufficient headroom; reuse exact media or a verified stable public URL (no automatic deletion)",
                                       details={"providerCode": "storage_quota_exceeded", "available_bytes": storage["available_bytes"], "size_bytes": size_bytes})
    return storage


def _transfer_url(url: Any) -> str:
    if not isinstance(url, str):
        raise SociamonialsFallbackError("Upload grant has no usable HTTPS transfer URL", details={"retrySafe": False})
    try:
        parsed = urlparse(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment or parsed.hostname.lower() in _NON_DIRECT_HOSTS):
            raise ValueError("Unsafe transfer URL")
        _resolve_public_host(parsed.hostname)
    except ValueError:
        raise SociamonialsFallbackError("Upload grant has an unsafe transfer URL; reconcile without exposing its signature", details={"retrySafe": False}) from None
    return url


def _guess_mime(local_path: Path, kind: str) -> str:
    if kind == "video" and local_path.suffix.lower() == ".m4v":
        return "video/mp4"
    guessed = mimetypes.guess_type(local_path.name)[0]
    allowed = {"image/jpeg", "image/png", "image/gif", "image/webp", "video/mp4", "video/quicktime", "video/webm"}
    if guessed not in allowed:
        raise SociamonialsFallbackError("Prepared media encoding is unsupported by the hosted upload transport")
    return guessed


class _FileSlice:
    """File-backed bounded request body; no read_bytes(), whole video, or giant part in RAM."""

    def __init__(self, handle: Any, size: int, *, deadline: float | None = None):
        self.handle, self.size, self.sent = handle, size, 0
        self.digest = hashlib.sha256()
        self.deadline = deadline

    def __len__(self):
        return self.size

    def tell(self):
        return self.sent

    def read(self, size: int = -1):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise SociamonialsFallbackError("Bounded media transfer time expired; reconcile the saved upload", details={"retrySafe": False})
        remaining = self.size - self.sent
        requested = min(remaining, 1024 * 1024, 1024 * 1024 if size is None or size < 0 else size)
        chunk = self.handle.read(requested)
        self.sent += len(chunk)
        self.digest.update(chunk)
        return chunk


def _put_bytes(session: Any, url: str, local_path: Path, *, start: int, size: int,
               timeout: float, mime_type: str) -> tuple[str, str]:
    url = _transfer_url(url)
    with local_path.open("rb") as handle:
        handle.seek(start)
        stream = _FileSlice(handle, size, deadline=getattr(session, "deadline", None))
        response = session.put(url, data=stream, headers={"Content-Type": mime_type, "Content-Length": str(size),
                                                        "Authorization": None, "Cookie": None},
                               auth=(), cookies={}, timeout=timeout, allow_redirects=False)
        _raise_for_status(response, context="media transfer", side_effects=True)
        if stream.sent != size:
            raise SociamonialsFallbackError("Media transfer did not consume the exact file slice", details={"retrySafe": False})
        etag = str((getattr(response, "headers", {}) or {}).get("ETag") or "").strip().strip('"')
        if not re.fullmatch(r"[A-Za-z0-9_./+=:-]{1,256}", etag):
            raise SociamonialsFallbackError("Media transfer returned no usable ETag; reconcile the saved upload", details={"retrySafe": False})
        return etag, stream.digest.hexdigest()


def _validate_grant(grant: Mapping[str, Any], *, ws: str, kind: str, size: int) -> None:
    if (not _workspace_int(grant.get("upload_id")) or _workspace_int(grant.get("workspace_registration_id")) != _workspace_int(ws)
            or grant.get("method") != "PUT" or grant.get("media_type") != kind or grant.get("mode") not in {"single", "multipart"}
            or type(grant.get("max_bytes")) is not int or size > grant["max_bytes"]):
        raise SociamonialsFallbackError("Upload grant does not authorize the exact workspace, kind and bytes", details={"retrySafe": False})
    try:
        expiry = datetime.fromisoformat(str(grant.get("expires_at") or "").replace("Z", "+00:00"))
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            raise ValueError("Expired")
    except (ValueError, TypeError):
        raise SociamonialsFallbackError("Saved upload grant expired or has no verified expiry; reconcile without allocating a new upload", details={"retrySafe": False}) from None
    required = grant.get("required_headers")
    if not isinstance(required, Mapping) or any(str(key).lower() != "content-type" for key in required):
        raise SociamonialsFallbackError("Upload grant requests unsupported headers; no credentials will be forwarded", details={"retrySafe": False})
    if grant["mode"] == "multipart":
        part_size, count = grant.get("part_size_bytes"), grant.get("part_count")
        if type(part_size) is not int or part_size <= 0 or type(count) is not int or not 1 <= count <= MAX_UPLOAD_PARTS or count != math.ceil(size / part_size):
            raise SociamonialsFallbackError("Multipart grant count/size exceeds the finite upload contract", details={"retrySafe": False})


def _put_multipart(session: Any, grant: dict[str, Any], local_path: Path, *, timeout: float,
                   headers: Mapping[str, str] | None = None, payload: dict | None = None,
                   digest: str | None = None) -> None:
    """Authenticated batched part grants; storage-confirmed resume; bounded streams."""
    size, part_size, count = local_path.stat().st_size, grant["part_size_bytes"], grant["part_count"]
    if not 1 <= count <= MAX_UPLOAD_PARTS or count != math.ceil(size / part_size):
        raise SociamonialsFallbackError("Multipart part count is invalid", details={"retrySafe": False})
    uploaded: dict[int, dict[str, Any]] = {}
    for first in range(1, count + 1, PART_URL_BATCH):
        response = session.post(f"{MEDIA_UPLOADS_URL}/{grant['upload_id']}/parts",
                                json={"from": first, "count": min(PART_URL_BATCH, count - first + 1)},
                                headers=dict(headers or {}), timeout=timeout)
        _raise_for_status(response, context="multipart grants", side_effects=False)
        body = _data(_json_body(response))
        parts, existing = body.get("parts"), body.get("uploaded", [])
        if not isinstance(parts, list) or not isinstance(existing, list) or len(parts) > PART_URL_BATCH or len(existing) > count:
            raise SociamonialsFallbackError("Multipart grant response is malformed or unbounded", details={"retrySafe": False})
        for part in existing:
            if not isinstance(part, Mapping):
                raise SociamonialsFallbackError("Stored multipart receipt is malformed", details={"retrySafe": False})
            number = part.get("part_number")
            expected = min(part_size, size - (number - 1) * part_size) if type(number) is int and 1 <= number <= count else None
            etag = str(part.get("etag") or "").strip().strip('"')
            if expected is None or part.get("size_bytes") != expected or not re.fullmatch(r"[A-Za-z0-9_./+=:-]{1,256}", etag):
                raise SociamonialsFallbackError("Stored part size/ETag cannot be verified", details={"retrySafe": False})
            saved_parts = ((delivery_state.state(payload or {}).get("sociamonials_uploads") or {}).get(digest, {}) or {}).get("parts", [])
            saved = next((p for p in saved_parts if p.get("part_number") == number), None)
            if saved and (saved.get("etag") != etag or saved.get("size_bytes") != expected):
                raise SociamonialsFallbackError("Storage part differs from its persisted exact-byte receipt", details={"retrySafe": False})
            uploaded[number] = {"part_number": number, "etag": etag, "size_bytes": expected}
        urls = {}
        for part in parts:
            if not isinstance(part, Mapping) or type(part.get("part_number")) is not int:
                raise SociamonialsFallbackError("Part grant is malformed", details={"retrySafe": False})
            number = part["part_number"]
            if not first <= number < min(first + PART_URL_BATCH, count + 1) or number in urls:
                raise SociamonialsFallbackError("Part grant numbering is duplicate/out of range", details={"retrySafe": False})
            urls[number] = _transfer_url(part.get("url", part.get("upload_url")))
        for number in range(first, min(first + PART_URL_BATCH, count + 1)):
            if number in uploaded:
                continue
            if number not in urls:
                raise SociamonialsFallbackError("Multipart grant omitted a required part", details={"retrySafe": False})
            length = min(part_size, size - (number - 1) * part_size)
            etag, part_hash = _put_bytes(session, urls[number], local_path, start=(number - 1) * part_size,
                                         size=length, timeout=timeout, mime_type=_guess_mime(local_path, _media_kind(str(local_path))))
            uploaded[number] = {"part_number": number, "etag": etag, "size_bytes": length, "sha256": part_hash}
            if payload is not None and digest:
                _upload_checkpoint(payload, digest, parts=[uploaded[n] for n in sorted(uploaded)], status="uploading")
    if set(uploaded) != set(range(1, count + 1)):
        raise SociamonialsFallbackError("Not every exact multipart piece was confirmed", details={"retrySafe": False})
    grant["_parts"] = [{"part_number": n, "etag": uploaded[n]["etag"]} for n in range(1, count + 1)]


def _wait_for_asset_ready(session: Any, headers: Mapping[str, str], asset_id: Any, *, timeout: float,
                          interval: float = 5.0, workspace_id: str | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + _bounded_timeout(timeout, DEFAULT_ASSET_READY_TIMEOUT, zero=True)
    last_status = "unknown"
    for attempt in range(MAX_POLL_REQUESTS):
        response = session.get(f"{MEDIA_ASSETS_URL}/{asset_id}", headers=dict(headers), timeout=min(30.0, max(0.1, deadline - time.monotonic())))
        if int(getattr(response, "status_code", 0) or 0) != 404:
            try:
                _raise_for_status(response, context="asset readiness", side_effects=False)
            except SociamonialsFallbackError as exc:
                if exc.retryable:
                    _pending("Asset processing/read is pending; resume the saved upload", **exc.details)
                raise
            asset = _asset_body(response)
            if _asset_id(asset) != _workspace_int(asset_id) or (workspace_id is not None and _workspace_int(asset.get("workspace_registration_id")) != _workspace_int(workspace_id)):
                raise SociamonialsFallbackError("Asset poll returned a different identity/workspace", details={"retrySafe": False})
            last_status = str(asset.get("processing_status", asset.get("status")) or "unknown")
            if last_status == "ready":
                return asset
            if last_status in {"failed", "error", "expired"}:
                raise SociamonialsFallbackError("Saved asset failed processing; retain its receipt for review", details={"asset_id": _workspace_int(asset_id), "retrySafe": False})
        remaining = deadline - time.monotonic()
        if remaining <= 0 or attempt + 1 == MAX_POLL_REQUESTS:
            break
        time.sleep(min(max(0.001, interval), remaining))
    _pending("Hosted asset is not ready; resume this receipt without reuploading", asset_id=_workspace_int(asset_id),
             asset_status=last_status, retryAfter=5, stage="asset_processing")


def _upload_local_media(session: Any, headers: Mapping[str, str], workspace_id: str, local_path: Path, *, timeout: float,
                         payload: dict | None = None, item: Mapping[str, Any] | None = None) -> str:
    payload = payload if payload is not None else {}
    item = item or {"sha256": _sha256(local_path), "size_bytes": local_path.stat().st_size, "kind": _media_kind(str(local_path))}
    digest, size, kind = item["sha256"], item["size_bytes"], item["kind"]
    if _sha256(local_path) != digest or local_path.stat().st_size != size:
        raise SociamonialsFallbackError("Local media changed after preflight; no upload will be started")
    saved = (delivery_state.state(payload).get("sociamonials_uploads") or {}).get(digest, {})
    if saved and (saved.get("workspace_id") != _workspace_int(workspace_id) or saved.get("sha256") != digest):
        raise SociamonialsFallbackError("Upload receipt workspace/bytes changed", details={"retrySafe": False})
    upload_key = f"sau-media-{workspace_id}-{digest}"
    mime_type = _guess_mime(local_path, kind)
    if isinstance(session, _HTTP):
        if session.grants >= MAX_UPLOAD_GRANTS:
            raise SociamonialsFallbackError("Bounded media-grant count reached; resume existing receipts", details={"retrySafe": False})
        session.grants += 1
    # Save BEFORE grant creation; a lost grant response replays the SAME key.
    _upload_checkpoint(payload, digest, workspace_id=_workspace_int(workspace_id), sha256=digest, size_bytes=size,
                       kind=kind, grant_key=upload_key, status=saved.get("status", "grant_started"))
    response = session.post(MEDIA_UPLOADS_URL,
                            json={"filename": local_path.name, "mime_type": mime_type, "size_bytes": size, "sha256": digest,
                                  "workspace_registration_id": _workspace_int(workspace_id), "idempotency_key": upload_key,
                                  "client_reference": upload_key}, headers=dict(headers), timeout=timeout)
    _raise_for_status(response, context="media upload grant")
    grant = _data(_json_body(response))
    # Persist the identity before validating advisory metadata/URLs. No bearer
    # grants or signatures are put into checkpoints, logs, or error details.
    if _workspace_int(grant.get("upload_id")):
        _upload_checkpoint(payload, digest, upload_id=_workspace_int(grant["upload_id"]))
    _validate_grant(grant, ws=workspace_id, kind=kind, size=size)
    if saved.get("upload_id") and saved["upload_id"] != _workspace_int(grant["upload_id"]):
        raise SociamonialsFallbackError("Idempotent upload replay returned a different reservation", details={"retrySafe": False})
    _upload_checkpoint(payload, digest, mode=grant["mode"], part_count=grant.get("part_count"),
                       part_size_bytes=grant.get("part_size_bytes"))
    # A completed transfer's checkpoint goes directly to complete. Crash before
    # checkpoint is safe: same single object is overwritten with identical bytes;
    # multipart uses storage's uploaded listing instead of sending parts twice.
    if saved.get("status") not in {"uploaded", "completing", "processing", "ready"}:
        if grant["mode"] == "multipart":
            _put_multipart(session, grant, local_path, timeout=timeout, headers=headers, payload=payload, digest=digest)
        else:
            etag, sent_hash = _put_bytes(session, grant.get("upload_url"), local_path, start=0, size=size,
                                          timeout=timeout, mime_type=mime_type)
            if sent_hash != digest:
                raise SociamonialsFallbackError("Transferred bytes differ from the preflight checksum", details={"retrySafe": False})
            _upload_checkpoint(payload, digest, etag=etag)
        if _sha256(local_path) != digest or local_path.stat().st_size != size:
            raise SociamonialsFallbackError("Local media changed during transfer; never create a replacement post", details={"retrySafe": False})
        _upload_checkpoint(payload, digest, status="uploaded")
    complete_parts = grant.get("_parts")
    if grant["mode"] == "multipart" and not complete_parts:
        previous = (delivery_state.state(payload).get("sociamonials_uploads") or {}).get(digest, {}).get("parts", [])
        complete_parts = [{"part_number": p["part_number"], "etag": p["etag"]} for p in previous]
        if len(complete_parts) != grant["part_count"]:
            raise SociamonialsFallbackError("Saved multipart completion is incomplete; reconcile its parts", details={"retrySafe": False})
    _upload_checkpoint(payload, digest, status="completing")
    complete = session.post(f"{MEDIA_UPLOADS_URL}/{grant['upload_id']}/complete",
                            json={"parts": complete_parts} if complete_parts else {}, headers=dict(headers), timeout=timeout)
    _raise_for_status(complete, context="media upload complete")
    asset = _asset_body(complete)
    asset_id = _asset_id(asset)
    if not asset_id or asset_id != _workspace_int(grant["upload_id"]):
        raise SociamonialsFallbackError("Upload completion has no matching asset receipt; reconcile the saved upload", details={"retrySafe": False})
    _upload_checkpoint(payload, digest, asset_id=asset_id, status="processing")
    if (asset.get("sha256") != digest or asset.get("size_bytes") != size or asset.get("media_type") != kind
            or _workspace_int(asset.get("workspace_registration_id")) != _workspace_int(workspace_id)):
        raise SociamonialsFallbackError("Server-computed upload checksum/bytes/type/workspace differ from the exact local media", details={"retrySafe": False})
    if asset.get("processing_status", asset.get("status")) != "ready":
        asset = _wait_for_asset_ready(session, headers, asset_id, timeout=_asset_ready_timeout(), workspace_id=workspace_id)
    receipt = _asset_receipt(asset, item, workspace_id)
    _upload_checkpoint(payload, digest, **receipt, status="ready")
    return f"asset://{asset_id}"


def _resolve_local_media(session: Any, headers: Mapping[str, str], workspace_id: str, local_path: Path, *, timeout: float,
                         db_path: Any = None, used_assets: list[dict[str, Any]] | None = None,
                         payload: dict | None = None, item: Mapping[str, Any] | None = None) -> str:
    payload = payload if payload is not None else {}
    item = item or {"sha256": _sha256(local_path), "size_bytes": local_path.stat().st_size,
                    "kind": _media_kind(str(local_path)), "local_path": str(local_path), "metadata": {}}
    if not _STORAGE_LOCK.acquire(timeout=0.1):
        _pending("Another pooled-quota upload is active; release worker concurrency", retryAfter=5, stage="storage_pool")
    try:
        _guard_storage_for_upload(session, headers, workspace_id, local_path=local_path, size_bytes=item["size_bytes"], timeout=timeout)
        reference = _upload_local_media(session, headers, workspace_id, local_path, timeout=timeout, payload=payload, item=item)
    finally:
        _STORAGE_LOCK.release()
    if used_assets is not None:
        saved = (delivery_state.state(payload).get("sociamonials_uploads") or {})[item["sha256"]]
        used_assets.append({key: saved[key] for key in ("asset_id", "workspace_id", "sha256", "size_bytes", "kind", "processing_status")})
    return reference


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value  # Exact whitespace, Unicode and links are approved copy too.
    if isinstance(value, Mapping):
        allowed = {"title", "summary", "description", "message", "body", "hashtags"}
        if set(value) - allowed:
            raise SociamonialsFallbackError("Structured draft contains unsupported copy fields; review instead of flattening settings into text")
        pieces = [_stringify(value.get(key)) for key in ("title", "summary", "description", "message", "body") if value.get(key) is not None]
        tags = value.get("hashtags")
        if tags is not None:
            if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
                raise SociamonialsFallbackError("Draft hashtags must be strings")
            pieces.append(" ".join(tag if tag.startswith("#") else "#" + tag for tag in tags if tag))
        return "\n\n".join(piece for piece in pieces if piece)
    raise SociamonialsFallbackError("Approved message must be text or an explicit structured draft")


def _draft_from_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return payload["draft"] if isinstance(payload.get("draft"), Mapping) else {}


def compose_message_with_links(payload: Mapping[str, Any], *, network: str) -> tuple[str, list[str]]:
    """Compatibility API: exact message, NO moved links and NO truncation.

    X restrictions are decided by strict provider validation against the ORIGINAL
    main post (Enterprise link allowance differs). No invented X first comment.
    """
    payload = payload or {}
    draft = _draft_from_payload(payload)
    raw = payload.get("message") if payload.get("message") is not None else draft.get("message")
    message = _stringify(raw)
    if not message:
        message = _stringify(payload.get("brief") if payload.get("brief") is not None else payload.get("notes"))
    tags = draft.get("hashtags")
    if isinstance(tags, list) and network != "yt":
        if any(not isinstance(tag, str) for tag in tags):
            raise SociamonialsFallbackError("Draft hashtags must be strings")
        missing = [tag if tag.startswith("#") else "#" + tag for tag in tags if tag and (tag if tag.startswith("#") else "#" + tag).lower() not in message.lower()]
        if missing:
            message += ("\n\n" if message else "") + " ".join(missing)
    return message, []


def compose_message(payload: Mapping[str, Any], *, network: str) -> str:
    return compose_message_with_links(payload, network=network)[0]


def _first_comment(payload: Mapping[str, Any]) -> str:
    draft = _draft_from_payload(payload)
    return _stringify(draft.get("firstComment", draft.get("first_comment", payload.get("first_comment", payload.get("firstComment")))))


def _network_options(payload: Mapping[str, Any], network: str, account_config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    try:
        media = collect_media(payload)
        main = [item for item in media if item["kind"] != "thumbnail"]
        return policy.map_intent(payload, network=network, message=compose_message(payload, network=network),
                                 account_config=account_config, media_kind=main[0]["kind"] if main else None,
                                 media_count=len(main))["network_options"]
    except policy.SociamonialsPolicyError as exc:
        raise SociamonialsFallbackError(str(exc), details=exc.details) from None


def _idempotency_key(target_id: Any, payload: dict, suffix: str | None) -> str:
    if type(target_id) is not int or not 0 < target_id < 2**63:
        raise SociamonialsFallbackError("A stable positive SAU target_id is required for backup idempotency")
    generation = delivery_state.state(payload).get("recovery_generation")
    if generation is not None and (not isinstance(generation, str) or not _SHA_RE.fullmatch(generation)):
        raise SociamonialsFallbackError("Invalid recovery generation; refusing an unstable post key")
    stable_suffix = "recovery-" + generation if generation is not None else ""
    if suffix and suffix != stable_suffix:
        raise SociamonialsFallbackError("Attempt-specific idempotency suffix is unsafe; use only the persisted recovery generation")
    key = f"sau-target-{target_id}" + (f"-{stable_suffix}" if stable_suffix else "")
    if len(key) > 120:
        raise SociamonialsFallbackError("Stable idempotency key exceeds the provider limit")
    return key


def _pending(message: str, **details: Any) -> None:
    error = delivery_state.DeliveryPending(message)
    error.retryable = True
    error.details = {"provider": "sociamonials", **details, "retrySafe": False}
    raise error


def _unknown(message: str, **details: Any) -> None:
    error = delivery_state.DeliveryUnknown(message)
    error.retryable = False
    error.details = {"provider": "sociamonials", **details, "retrySafe": False}
    raise error


def _usable_post_id(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return False
    text = str(value)
    if any(char in text for char in ("?", "#", "@")) or text.lower().startswith(("sm_agent_", "sma_", "bearer", "token=")):
        return False
    if re.fullmatch(r"-?\d+", text) and int(text) <= 0:
        return False
    return text.lower() not in _PLACEHOLDER_IDS and bool(re.fullmatch(r"[A-Za-z0-9_:/?.#%+=@|~-]{1,512}", text))


def _check_native_receipt(payload: dict, proof: Mapping[str, Any] | None, target_id: int | None) -> None:
    state = delivery_state.state(payload)
    receipt = state.get("provider_receipt")
    unrelated = isinstance(receipt, Mapping) and receipt.get("provider") != "sociamonials"
    started = delivery_state.has_unresolved_submission(payload)
    journals = {}
    raw = state.get("provider_operations") or {}
    if not isinstance(raw, Mapping):
        _unknown("Native operation journal is invalid; reconcile before backup")
    for provider, journal in raw.items():
        if provider == "sociamonials":
            continue
        if not isinstance(journal, Mapping) or not isinstance(journal.get("items", {}), Mapping):
            _unknown("Native operation receipts are invalid; reconcile before backup")
        if journal.get("items"):
            journals[provider] = journal["items"]
    if not unrelated and not started and not journals:
        return
    authorization = proof or state.get("backup_authorization")
    valid = (isinstance(authorization, Mapping) and authorization.get("definitive_rejection") is True
             and authorization.get("no_publication") is True and authorization.get("target_id") == target_id)
    if unrelated:
        valid = valid and authorization.get("provider") == receipt.get("provider") and authorization.get("receipt_id") == receipt.get("id")
    elif started and (not journals or state.get("operation")):
        valid = valid and isinstance(state.get("operation"), str) and bool(state["operation"]) and authorization.get("operation") == state["operation"]
    if not valid:
        _unknown("An existing native receipt or ambiguous submission owns this intent; reconcile it before authorizing backup")
    if journals:
        proofs = authorization.get("operation_receipts")
        if not isinstance(proofs, Mapping) or set(proofs) != set(journals):
            _unknown("Every native operation receipt needs explicit scoped proof of non-publication before backup")
        for provider, items in journals.items():
            verified_items = proofs[provider]
            if not isinstance(verified_items, Mapping) or set(verified_items) != set(items):
                _unknown("Native operation proof is incomplete; do not discard earlier side effects")
            for name, item in items.items():
                proof_item = verified_items[name]
                if (not isinstance(item, Mapping) or not isinstance(proof_item, Mapping)
                        or proof_item.get("id") != item.get("id") or proof_item.get("no_publication") is not True
                        or proof_item.get("definitive_rejection") is not True):
                    _unknown("Native operation non-publication proof does not match its original receipt")
    # Persist only the proof's scoped machine fields; never provider error text,
    # signatures or caller-supplied extra fields. Subsequent safe retries reuse it.
    saved_proof = {key: authorization[key] for key in ("target_id", "provider", "receipt_id", "operation", "definitive_rejection", "no_publication") if key in authorization}
    if journals:
        saved_proof["operation_receipts"] = {provider: {name: {key: authorization["operation_receipts"][provider][name].get(key)
            for key in ("id", "definitive_rejection", "no_publication")} for name in items} for provider, items in journals.items()}
    changes = {"backup_authorization": saved_proof}
    if unrelated:
        changes["native_provider_receipt"] = dict(receipt)
    delivery_state.checkpoint(payload, **changes)


def _strict_validate(http: Any, headers: Mapping[str, str], body: Mapping[str, Any], *, timeout: float,
                     payload: dict | None = None, route: Mapping[str, Any] | None = None) -> None:
    request = {**copy.deepcopy(body), "validate_only": True, "strict": True}
    response = http.post(POST_VALIDATE_URL, json=request, headers=dict(headers), timeout=timeout, side_effects=False)
    _raise_for_status(response, context="strict validation", side_effects=False)
    result = _data(_json_body(response))
    resolution = result.get("profile_resolution")
    network = next(iter(body["networks"]))
    refs = body["networks"][network]["profile_refs"]
    if _workspace_int(result.get("post_id")):
        if payload is not None and route is not None:
            delivery_state.accepted(payload, provider="sociamonials", receipt_id=str(result["post_id"]),
                network=network, profile_refs=list(refs), workspace_id=body["workspace_registration_id"],
                api_key_env=route["api_key_env"], idempotency_key=body["idempotency_key"], intent_mismatch=True)
        _unknown("Validation unexpectedly returned a post receipt; reconcile it without any further write")
    rows = resolution.get("resolved_profiles") if isinstance(resolution, Mapping) else None
    if (result.get("valid") is not True or result.get("validate_only") is not True or result.get("post_id") is not None
            or result.get("mode") != body["mode"] or not isinstance(resolution, Mapping) or resolution.get("mode") != "explicit"
            or resolution.get("preset") is not None or not isinstance(rows, list) or len(rows) != 1
            or not isinstance(rows[0], Mapping) or rows[0].get("network") != network or rows[0].get("profile_ref") != refs[0]
            or type(result.get("requires_approval")) is not bool
            or result.get("warnings") != [] or resolution.get("warnings") != []):
        raise SociamonialsFallbackError("Strict no-create validation did not confirm the original settings and exact destination; no media/post will be created",
                                       details={"intentReviewRequired": True, "validationUnknown": True})
    if result.get("requires_approval") is True:
        raise SociamonialsNotConfigured("Original destination requires provider approval; arrange approval instead of automatic backup writes",
                                       details={"requiresApproval": True, "intentReviewRequired": True})


def _delivery_view(body: Mapping[str, Any], *, post_id: Any, network: str, profile_refs: Sequence[str], workspace_id: Any) -> dict[str, Any]:
    unknown = {"state": "unknown", "delivered": None, "error": "Exact-destination delivery could not be proven", "platform_post_id": None}
    if (_workspace_int(body.get("post_id")) != _workspace_int(post_id)
            or _workspace_int(body.get("workspace_registration_id")) != _workspace_int(workspace_id)):
        return {**unknown, "details": {"receiptScopeMismatch": True}}
    networks = body.get("networks")
    if not isinstance(networks, Mapping) or not isinstance(networks.get(network), Mapping):
        return unknown
    detail = networks[network]
    profiles = detail.get("profiles")
    if not isinstance(profiles, list) or len(profile_refs) != 1:
        return unknown
    selected = [row for row in profiles if isinstance(row, Mapping) and row.get("profile_ref") == profile_refs[0]
                and row.get("network", network) == network]
    if len(selected) != 1:
        return unknown
    if set(networks) != {network} or len(profiles) != 1:
        return {**unknown, "state": "partial", "error": "Provider receipt includes unexpected destinations; reconcile partial publication"}
    profile = selected[0]
    state = str(profile.get("delivery_state") or "").lower()
    status = str(detail.get("delivery_status") or "").lower()
    native_id = profile.get("platform_post_id")
    if body.get("requires_approval") is True or (body.get("approved") is False and body.get("draft") is not True):
        return {**unknown, "state": "requires_approval", "error": "Provider holds this receipt for approval"}
    if profile.get("reason") in {"MOBILE_APP_SHARE", "APPROVAL_REQUIRED", "APPROVAL_HOLD"}:
        return {**unknown, "state": "requires_approval", "error": "Receipt requires a person/approval; never report automatic delivery"}
    if (detail.get("delivered") is True and status == "delivered" and state == "published"
            and ("delivered" not in profile or profile["delivered"] is True) and _usable_post_id(native_id)):
        return {"state": "delivered", "delivered": True, "error": "", "platform_post_id": str(native_id)}
    if _usable_post_id(native_id) or detail.get("delivered") is True or state == "published" or status == "delivered":
        return unknown  # Contradictory flags or an id without literal confirmation.
    if body.get("canceled") is True or state == "canceled":
        return {**unknown, "state": "failed", "delivered": False, "error": "Exact provider destination is canceled; retain its receipt"}
    if profile.get("reason") in {"MEDIA_DISPATCH_INTERRUPTED", "DELIVERY_UNKNOWN", "PUBLISHING_TIMEOUT"}:
        return {**unknown, "details": {"reason": profile["reason"]}}
    if status == "failed" and state == "needs_attention":
        return {**unknown, "state": "failed", "delivered": False,
                "error": "Provider reports final failure for the exact destination; inspect the retained receipt",
                "details": {"reason": _safe_code(profile.get("reason"), "delivery_failed")}}
    if status in {"pending", "processing"} and state in {"accepted", "preparing_media", "publishing"}:
        result = {**unknown, "state": "processing" if status == "processing" or state != "accepted" else "pending", "error": ""}
        next_check = profile.get("next_check_at_utc")
        if isinstance(next_check, str):
            try:
                datetime.fromisoformat(next_check.replace("Z", "+00:00"))
                result["details"] = {"next_check_at_utc": next_check}
            except ValueError:
                pass
        return result
    return unknown


def _wait_for_delivery(http: Any, headers: Mapping[str, str], post_id: Any, *, timeout: float, interval: float = 5.0,
                       network: str | None = None, profile_refs: Sequence[str] | None = None,
                       workspace_id: Any = None) -> dict[str, Any]:
    """Bounded receipt-only polling. A 429/5xx retains deadlines and releases concurrency."""
    last = {"state": "unknown", "delivered": None, "error": "Exact receipt destination is missing", "platform_post_id": None}
    if not _workspace_int(post_id) or network not in policy.NETWORK_TO_PLATFORM or not profile_refs or len(profile_refs) != 1 or not _workspace_int(workspace_id):
        return last
    deadline = time.monotonic() + _bounded_timeout(timeout, DEFAULT_DELIVERY_TIMEOUT, zero=True)
    for attempt in range(MAX_POLL_REQUESTS):
        try:
            response = http.get(f"{POSTS_URL}/{post_id}", headers=dict(headers), timeout=min(30.0, max(0.1, deadline - time.monotonic())))
            if int(getattr(response, "status_code", 0) or 0) == 404:
                return {**last, "details": {"httpStatus": 404}}
            _raise_for_status(response, context="delivery poll", side_effects=False)
            body = _data(_json_body(response))
            if isinstance(body.get("post"), Mapping):
                body = dict(body["post"])
            last = _delivery_view(body, post_id=post_id, network=network, profile_refs=profile_refs, workspace_id=workspace_id)
            last["details"] = {**dict(last.get("details") or {}), **_rate_details(response)}
        except SociamonialsFallbackError as exc:
            if exc.retryable:
                _pending("Provider delivery poll is deferred; keep the original receipt and release concurrency", **exc.details)
            raise
        except AssertionError:
            raise
        except Exception:
            return {"state": "unknown", "delivered": None, "error": "Provider receipt read was unavailable; do not recreate", "platform_post_id": None}
        if last["state"] in {"delivered", "failed", "partial", "requires_approval", "unknown"}:
            return last
        remaining = deadline - time.monotonic()
        if remaining <= 0 or attempt + 1 == MAX_POLL_REQUESTS:
            return last
        time.sleep(min(max(0.001, interval), remaining))
    return last


def _result(receipt: Mapping[str, Any], view: Mapping[str, Any]) -> dict[str, Any]:
    return {"ok": view.get("delivered") is True and _usable_post_id(view.get("platform_post_id")),
            "accepted": True, "provider": "sociamonials", "post_id": _workspace_int(receipt.get("id")),
            "platform_post_id": view.get("platform_post_id"), "status": view.get("state"),
            "network": receipt["network"], "profile_refs": list(receipt["profile_refs"]),
            "workspace_id": receipt["workspace_id"], "used_assets": receipt.get("used_assets") or [],
            "delivery_state": view.get("state"), "delivered": view.get("delivered"),
            "requires_approval": view.get("state") == "requires_approval", "delivery_error": view.get("error", ""),
            "details": dict(view.get("details") or {})}


def _resume_receipt(payload: dict, receipt: Mapping[str, Any], *, platform: str, session: Any,
                    api_key: str | None, timeout: float, delivery_timeout: float) -> dict[str, Any]:
    # The receipt, not current config or today's mapping, owns workspace/ref/key.
    ws = str(receipt.get("workspace_id") or "")
    network, refs, selector = receipt.get("network"), receipt.get("profile_refs"), receipt.get("api_key_env")
    if (not _workspace_int(receipt.get("id")) or not _workspace_int(ws) or network != _platform_network(platform)
            or not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], str) or not refs[0]
            or not isinstance(selector, str) or not selector):
        _unknown("Saved Sociamonials receipt lacks its original workspace/profile/credential scope; reconcile rather than guess a mapping")
    key, resolved_ws = resolve_credentials({"workspace_id": ws, "api_key_env": selector}, api_key=api_key)
    if not key or resolved_ws != ws:
        raise SociamonialsNotConfigured("Original receipt's isolated credential is unavailable; do not switch keys/workspaces")
    http, headers = _http(session, key), _headers(key)
    _identity(http, headers, ws, timeout=timeout, permissions=("posts.read",))
    view = _wait_for_delivery(http, headers, receipt["id"], timeout=delivery_timeout, network=network, profile_refs=refs, workspace_id=ws)
    if receipt.get("intent_mismatch") is True:
        view = {"state": "partial", "delivered": None, "error": "Stored provider receipt changed original intent; reconcile it", "platform_post_id": None}
    updated = {**receipt, "status": view["state"]}
    if view.get("platform_post_id"):
        updated["platform_post_id"] = view["platform_post_id"]
    delivery_state.checkpoint(payload, provider_receipt=updated)
    return _result(updated, view)


def publish_via_sociamonials(*, platform: str, account: Any, payload: Mapping[str, Any],
                             media_paths: Sequence[str] | None = None, target_id: int | None = None,
                             settings: Any = None, session: Any = None, db_path: Any = None,
                             api_key: str | None = None, workspace_id: str | None = None,
                             timeout: float | None = None, verify_media: bool = True,
                             delivery_timeout: float | None = None, idempotency_suffix: str | None = None,
                             native_failure_proof: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Publish exactly one prepared target, or resume its original scoped receipt.

    Stable API; verify_media is compatibility-only and bypasses NO safety check.
    New writes require target_id and a persisted recovery_generation (when used).
    Checkpoints store scoped receipts/hash identities, never secrets/signed URLs.
    Optional native_failure_proof (or _delivery.backup_authorization) must name
    target_id, provider, receipt_id, definitive_rejection=True, no_publication=True
    before an unrelated native receipt can be superseded; its evidence is kept.
    Any provider_operations journal also requires operation_receipts proofs for
    EVERY recorded item: {provider: {operation: {id, no_publication: True,
    definitive_rejection: True}}}. A missing/changed receipt always blocks.
    The parent must persist _save_checkpoint under its lease before side effects.
    """
    payload = payload if isinstance(payload, dict) else dict(payload)
    request_timeout = _bounded_timeout(timeout, _timeout())
    poll_timeout = _bounded_timeout(delivery_timeout, _delivery_timeout(), zero=True)
    receipt = delivery_state.receipt_for(payload, "sociamonials")
    if receipt is not None:
        return _resume_receipt(payload, receipt, platform=platform, session=session, api_key=api_key,
                               timeout=request_timeout, delivery_timeout=poll_timeout)
    current = delivery_state.state(payload)
    if delivery_state.has_unresolved_submission(payload) and current.get("operation") == "sociamonials-create-post":
        _unknown("An earlier backup create is ambiguous; retain its stable key and reconcile before any new write")
    key_id = _idempotency_key(target_id, payload, idempotency_suffix)
    _check_native_receipt(payload, native_failure_proof, target_id)
    if _account_value(account, "enabled", True) in (False, 0):
        raise SociamonialsNotConfigured("Disabled/deleted account cannot use a new backup publication")
    account_id = _account_value(account, "id")
    if type(account_id) is not int or account_id <= 0:
        raise SociamonialsNotConfigured("The original SAU account is missing/deleted; do not invent an account substitution")
    mapping = resolve_mapping(account_id, platform=platform, account=account, settings=settings)
    if not mapping:
        raise SociamonialsNotConfigured("No exact Sociamonials destination is mapped for this account; operational mappings need verification")
    key, ws = resolve_credentials(mapping, api_key=api_key, workspace_id=workspace_id)
    if not key:
        raise SociamonialsNotConfigured("The selected exact destination's Sociamonials credential is missing")
    network = mapping["network"]
    if network != _platform_network(platform) or len(mapping["profile_refs"]) != 1:
        raise SociamonialsNotConfigured("Backup needs one exact matching-platform destination")
    if not payload.get("artifacts") and media_paths:
        payload["artifacts"] = [{"local_path": path} for path in media_paths]
    media = collect_media(payload)
    for item in media:
        locator = urlparse(str(item.get("public_url") or ""))
        if locator.username is not None or locator.password is not None or locator.query or locator.fragment:
            raise SociamonialsFallbackError("Declared media has a credential/expiring locator; supply a credential-free stable URL before checkpointing")
    main, thumbnails = [item for item in media if item["kind"] != "thumbnail"], [item for item in media if item["kind"] == "thumbnail"]
    kinds = {item["kind"] for item in main}
    if len(kinds) > 1 or len([item for item in main if item["kind"] == "video"]) > 1:
        raise SociamonialsFallbackError("Mixed image/video or multiple videos require separately scheduled intents; nothing may be dropped")
    if thumbnails and (len(thumbnails) != 1 or kinds != {"video"}):
        raise SociamonialsFallbackError("A video cover needs exactly one video and one explicit thumbnail")
    if (payload.get("artifacts") or media_paths) and not main:
        raise SociamonialsFallbackError("Only unsupported/context media was supplied; refusing a text-only replacement")
    media_kind = next(iter(kinds), None)
    if network in _MEDIA_REQUIRED_NETWORKS and (not main or network == "yt" and media_kind != "video"):
        raise SociamonialsFallbackError("This destination requires original publishable media")
    if media_kind == "image" and len(main) > policy.limits_for(network)["max_images"]:
        raise SociamonialsFallbackError("Image count exceeds the backup limit; schedule ALL images without dropping extras")
    config = _account_value(account, "config", {}) or {}
    try:
        intent = policy.map_intent(payload, network=network, message=compose_message(payload, network=network),
                                   account_config=config, media_kind=media_kind, media_count=len(main))
    except policy.SociamonialsPolicyError as exc:
        raise SociamonialsFallbackError(str(exc), details=exc.details) from None
    if not intent["message"] and not main:
        raise SociamonialsFallbackError("Original intent contains no publishable text/media")
    http, headers = _http(session, key), _headers(key)
    verified = _verify_route(http, headers, mapping, ws, platform=platform, timeout=request_timeout,
                             required=_required_capabilities(payload, intent=intent))
    if verified["requires_approval"]:
        raise SociamonialsNotConfigured("Verified destination requires approval; no automatic backup media/post writes",
                                       details={"requiresApproval": True})
    route = verified["mapping"]
    prior_route = delivery_state.state(payload).get("sociamonials_route")
    if prior_route and any(prior_route.get(k) != route.get(k) for k in ("network", "profile_refs", "workspace_id", "api_key_env")):
        _unknown("Backup checkpoint owns a different original route; do not move its media/submission to a changed mapping")
    media = _checked_media(media, network)
    declared_identity = [(item["kind"], item["local_path"], item["sha256"], item["size_bytes"]) for item in media]
    from myUtils.publish_media_guard import ensure_single_payload
    # Final scheduled bytes must remain immutable. Neither this call nor a mock
    # verify_media flag permits skipping live profile grants/strict validation.
    ensure_single_payload(payload, platform=platform, account_config=config, backup_compatible=True)
    media = _checked_media(collect_media(payload), network)
    main, thumbnails = [item for item in media if item["kind"] != "thumbnail"], [item for item in media if item["kind"] == "thumbnail"]
    if [(item["kind"], item["local_path"], item["sha256"], item["size_bytes"]) for item in media] != declared_identity:
        raise SociamonialsFallbackError("The preparation guard changed scheduled byte/part identity; prepare and schedule all parts first")
    if intent["network_options"].get("shorts"):
        if media_kind != "video" or media_pipeline.probe_video_duration(main[0]["local_path"]) > 180:
            raise SociamonialsFallbackError("Original YouTube Shorts intent exceeds 180 seconds; do not silently publish a regular video")
    body = {"mode": "publish_now", "strict": True, "message": intent["message"],
            "workspace_registration_id": _workspace_int(ws),
            "networks": {network: {"profile_refs": list(mapping["profile_refs"]), **intent["network_options"]}},
            "idempotency_key": key_id, **intent["top_fields"]}
    # Per-image alt text cannot be collapsed to a single global alt_text unless
    # every original value is identical. A missing/different value is not dropped.
    alt_values = [item["metadata"].get("alt_text", item["artifact"].get("alt_text")) for item in main]
    if any(value is not None for value in alt_values):
        if media_kind != "image" or not all(isinstance(value, str) for value in alt_values) or any(value != alt_values[0] for value in alt_values) or network in {"in", "gmb", "yt", "tiktok"}:
            raise SociamonialsFallbackError("Original per-image accessibility text cannot be represented faithfully by this backup")
        if body.get("alt_text") is not None and body["alt_text"] != alt_values[0]:
            raise SociamonialsFallbackError("Global and per-image alt text conflict")
        body["alt_text"] = alt_values[0]
    if body.get("video_thumbnail_url") and not thumbnails:
        raise SociamonialsFallbackError("Original thumbnail URL has no exact local checksum-backed cover artifact")
    if thumbnails:
        if network not in policy.THUMBNAIL_NETWORKS:
            raise SociamonialsFallbackError("Original cover is unsupported on this backup network")
        reference = _external_reference(thumbnails[0], http, timeout=request_timeout)
        if body.get("video_thumbnail_url") and _normalize_public_url(body["video_thumbnail_url"]) != reference:
            raise SociamonialsFallbackError("Original cover URL differs from the exact thumbnail artifact")
        body["video_thumbnail_url"] = reference
    fingerprint = hashlib.sha256(json.dumps({"intent": body, "media": [{"sha256": item["sha256"], "size_bytes": item["size_bytes"], "kind": item["kind"]} for item in media]},
                                              sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    previous = delivery_state.state(payload).get("sociamonials_intent_sha256")
    if previous is not None and previous != fingerprint:
        _unknown("Original backup copy/options/media changed under a stable target key; reconcile before new writes")
    delivery_state.checkpoint(payload, sociamonials_route=route, sociamonials_idempotency_key=key_id,
                               sociamonials_intent_sha256=fingerprint)
    used_assets: list[dict[str, Any]] = []
    refs: list[str | None] = []
    for item in main:
        asset = _saved_asset(http, headers, ws, item, payload, timeout=request_timeout)
        if asset is None and verified["permissions"]["assets.read"]:
            asset = _lookup_reusable_asset(http, headers, ws, filename=Path(item["local_path"]).name,
                                           size_bytes=item["size_bytes"], kind=item["kind"], sha256=item["sha256"], timeout=request_timeout)
        if asset is not None:
            asset_receipt = _asset_receipt(asset, item, ws)
            _upload_checkpoint(payload, item["sha256"], **asset_receipt, status="ready")
            used_assets.append(asset_receipt)
            refs.append(f"asset://{asset_receipt['asset_id']}")
        else:
            refs.append(None)
    new_indices = [index for index, reference in enumerate(refs) if reference is None]
    storage = _read_storage(http, headers, ws, timeout=request_timeout) if new_indices else None
    if storage and storage["requires_approval"]:
        raise SociamonialsNotConfigured("Client/workspace holds this intent for approval; no automatic backup uploads", details={"requiresApproval": True})
    caps = policy.limits_for(network, storage)
    available = storage["available_bytes"] if storage else 0
    for index in new_indices:
        item = main[index]
        cap = caps["hosted_video_max_bytes"] if item["kind"] == "video" else caps["hosted_image_max_bytes"]
        fits = (verified["permissions"]["assets.write"] and cap is not None and item["size_bytes"] <= cap
                and not storage["ratchet_active"] and (available is None or item["size_bytes"] <= available))
        if not fits:
            # Separate external budget + exact stable URL. Never fall back to a
            # larger raw original, an unverified URL or a different destination.
            refs[index] = _external_reference(item, http, timeout=request_timeout)
        elif available is not None:
            available -= item["size_bytes"]  # Aggregate pooled cost before ANY grant.
    preview = copy.deepcopy(body)
    preview_refs = [ref or f"{BASE_URL}/sau-preflight/{item['sha256']}{'.mp4' if item['kind'] == 'video' else '.jpg'}" for ref, item in zip(refs, main)]
    if media_kind == "video":
        preview["video_url"] = preview_refs[0]
    elif media_kind == "image":
        preview["image_urls"] = preview_refs
    # Validation-only does not fetch placeholders and never creates them. The
    # final strict validation below uses real, server-verified ready references.
    _strict_validate(http, headers, preview, timeout=request_timeout, payload=payload, route=route)
    for index, item in enumerate(main):
        if refs[index] is not None:
            continue
        if item["kind"] == "video" and not verified["permissions"]["assets.read"]:
            raise SociamonialsNotConfigured("Hosted video readiness requires an asset-read grant before upload")
        try:
            refs[index] = _resolve_local_media(http, headers, ws, Path(item["local_path"]), timeout=request_timeout,
                                                payload=payload, item=item, used_assets=used_assets)
        except SociamonialsFallbackError as exc:
            # Only a definitive quota/entitlement rejection may select the
            # previously declared exact URL. Ambiguous writes NEVER change route.
            if exc.details.get("retrySafe") is True and exc.details.get("providerCode") in {"storage_quota_exceeded", "video_upload_not_enabled", "hosted_media_limit"}:
                refs[index] = _external_reference(item, http, timeout=request_timeout)
            else:
                raise
    if media_kind == "video":
        body["video_url"] = refs[0]
    elif media_kind == "image":
        body["image_urls"] = refs
    if main:
        _strict_validate(http, headers, body, timeout=request_timeout, payload=payload, route=route)
    # Last in-memory/local identity gate immediately before the only post write.
    for item in media:
        if Path(item["local_path"]).stat().st_size != item["size_bytes"] or _sha256(Path(item["local_path"])) != item["sha256"]:
            raise SociamonialsFallbackError("Scheduled media changed after upload; do not create a mismatched post")
    delivery_state.starting(payload, operation="sociamonials-create-post")
    try:
        response = http.post(POSTS_URL, json=body, headers=headers, timeout=request_timeout, context="create post")
    except SociamonialsFallbackError as exc:
        if exc.details.get("retrySafe") is True:
            # Local rate/budget rejection happens before HTTP; it must not leave
            # a false ambiguous-submission flag that prevents safe resumption.
            delivery_state.checkpoint(payload, submission_started=False)
        raise
    status = int(getattr(response, "status_code", 0) or 0)
    if 400 <= status < 500 and status not in {408, 409}:
        delivery_state.checkpoint(payload, submission_started=False)
    _raise_for_status(response, context="create post")
    result = _data(_json_body(response))
    post_id = _workspace_int(result.get("post_id"))
    if not post_id:
        _unknown("Provider create response has no usable post receipt; reconcile its stable idempotency key")
    delivery_state.accepted(payload, provider="sociamonials", receipt_id=str(post_id), network=network,
                            workspace_id=_workspace_int(ws), profile_refs=list(mapping["profile_refs"]), api_key_env=route["api_key_env"],
                            idempotency_key=key_id, used_assets=used_assets, intent_sha256=fingerprint)
    receipt = delivery_state.receipt_for(payload, "sociamonials")
    resolution = result.get("profile_resolution")
    wrong_resolution = False
    if resolution is not None:
        rows = resolution.get("resolved_profiles") if isinstance(resolution, Mapping) else None
        wrong_resolution = (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping)
                            or rows[0].get("network") != network or rows[0].get("profile_ref") != mapping["profile_refs"][0]
                            or resolution.get("mode") != "explicit" or resolution.get("preset") is not None or resolution.get("warnings") not in (None, []))
    if result.get("warnings") not in (None, []) or wrong_resolution or ("message" in result and result["message"] != body["message"]):
        receipt = {**receipt, "intent_mismatch": True, "status": "partial"}
        delivery_state.checkpoint(payload, provider_receipt=receipt)
        return _result(receipt, {"state": "partial", "delivered": None, "platform_post_id": None,
                                 "error": "Provider accepted with changed targeting/settings/copy; reconcile the original receipt"})
    if result.get("requires_approval") is True or result.get("status") == "pending_approval":
        view = {"state": "requires_approval", "delivered": None, "platform_post_id": None, "error": "Provider holds the accepted receipt for approval"}
    else:
        view = _wait_for_delivery(http, headers, post_id, timeout=poll_timeout, network=network,
                                  profile_refs=mapping["profile_refs"], workspace_id=ws)
    receipt = {**receipt, "status": view["state"]}
    if view.get("platform_post_id"):
        receipt["platform_post_id"] = view["platform_post_id"]
    delivery_state.checkpoint(payload, provider_receipt=receipt)
    return _result(receipt, view)
