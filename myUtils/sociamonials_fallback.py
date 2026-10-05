"""Sociamonials fallback publisher (Phase 4).

When a publish target has exhausted its retry budget, the worker can hand the
post to Sociamonials instead of marking it failed outright.  Sociamonials
already holds OAuth connections for most of the same brand accounts, so this
turns a dead X/Facebook/Bluesky target into a delivered one.

This module speaks the same REST surface the Sociamonials MCP wraps, because
the worker runs headless in a container where an MCP HTTP handshake would be
strictly worse.  Mapping to the MCP tool names:

========================================  ==================================
REST here                                 MCP tool
========================================  ==================================
``POST /api/v1/posts``                    ``create_social_post``
``POST /api/v1/media/uploads`` + PUT +    (no direct equivalent; the agent
``POST /api/v1/media/uploads/{id}/complete``  upload path)
``POST /api/v1/media/imports``            ``import_media_from_url``
profile map below                         ``list_social_profiles``
========================================  ==================================

The fallback is opt-in: it only fires when ``SAU_SOCIAMONIALS_FALLBACK`` is
truthy *and* ``SOCIAMONIALS_API_KEY`` is present.  It must never fire for a
target that is already classified non-retryable for a permanent reason (banned
subreddit, missing media, ...); use :func:`should_attempt_fallback` for that
guard.

Nothing here is imported at module import time that performs network I/O, and
every entry point accepts an injected ``session`` so tests never make a live
call.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

BASE_URL = "https://www.sociamonials.com"
POSTS_URL = f"{BASE_URL}/api/v1/posts"
MEDIA_UPLOADS_URL = f"{BASE_URL}/api/v1/media/uploads"
DEFAULT_WORKSPACE_ID = "26985"

ENABLED_ENV = "SAU_SOCIAMONIALS_FALLBACK"
API_KEY_ENV = "SOCIAMONIALS_API_KEY"
WORKSPACE_ENV = "SOCIAMONIALS_WORKSPACE_ID"
SECRETS_FILE_ENV = "SOCIAMONIALS_SECRETS_FILE"
TIMEOUT_ENV = "SAU_SOCIAMONIALS_TIMEOUT"

DEFAULT_TIMEOUT = 60.0

# Networks that only publish with media attached (Sociamonials validation).
_MEDIA_REQUIRED_NETWORKS = {"in", "tiktok", "yt", "pi"}

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".m4v"}

# Hosts that serve an HTML viewer rather than the raw bytes.  The Graph API and
# Sociamonials' fetch guard both reject these, so never hand one over.
_NON_DIRECT_HOSTS = {
    "drive.google.com",
    "docs.google.com",
    "photos.google.com",
    "www.google.com",
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
}

# SAU account id -> Sociamonials destination.  Mirrors the plan map, corrected
# with the full ``socialupload-groups.json`` profile refs so the EN and ZH
# Bluesky accounts do not collapse onto one profile.
SAU_ACCOUNT_TO_SOCIAMONIALS: dict[int, dict[str, Any]] = {
    # Facebook
    11: {"network": "fb", "profile_refs": ["19784|175913"], "name": "Nakedwill2"},
    64: {"network": "fb", "profile_refs": ["30186|255814"], "name": "Sexualwill"},
    # Instagram
    72: {"network": "in", "profile_refs": ["52817"], "name": "nakedwill8"},
    75: {"network": "in", "profile_refs": ["82894"], "name": "sexualwill8"},
    # X / Twitter
    123: {"network": "tw", "profile_refs": ["13425"], "name": "model_will"},
    124: {"network": "tw", "profile_refs": ["13426"], "name": "nudeweiwei"},
    77: {"network": "tw", "profile_refs": ["14100"], "name": "will_sexual"},
    # Bluesky (EN + ZH are distinct profiles)
    118: {"network": "blsk", "profile_refs": ["1280"], "name": "nakedwill.bsky.social"},
    119: {"network": "blsk", "profile_refs": ["1279"], "name": "nudeweiwei.bsky.social"},
    120: {"network": "blsk", "profile_refs": ["1281"], "name": "sexualwill.bsky.social"},
    121: {"network": "blsk", "profile_refs": ["1282"], "name": "nakedhappylife.bsky.social"},
    # Threads
    62: {"network": "thrd", "profile_refs": ["361"], "name": "nakedwill8"},
    42: {"network": "thrd", "profile_refs": ["1939"], "name": "sexualwill8"},
    # TikTok
    109: {"network": "tiktok", "profile_refs": ["3917"], "name": "Nakedwill"},
    # YouTube
    110: {"network": "yt", "profile_refs": ["13223"], "name": "itsnakedwill"},
}


class SociamonialsFallbackError(RuntimeError):
    """A fallback attempt failed; the caller should record the target failed."""


class SociamonialsNotConfigured(SociamonialsFallbackError):
    """The fallback is switched off or has no API key."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def get_api_key(env: Mapping[str, str] | None = None) -> str:
    """Return the API key from the environment, or the secrets file.

    The secrets-file fallback exists for local CLI use; the container is
    expected to receive ``SOCIAMONIALS_API_KEY`` through its environment.
    """
    environ = env if env is not None else os.environ
    key = str(environ.get(API_KEY_ENV) or "").strip()
    if key:
        return key
    secrets_path = str(environ.get(SECRETS_FILE_ENV) or "").strip()
    if not secrets_path and env is None:
        default_path = Path.home() / ".claude" / "secrets.json"
        secrets_path = str(default_path) if default_path.exists() else ""
    if secrets_path:
        try:
            payload = json.loads(Path(secrets_path).read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return str(payload.get(API_KEY_ENV) or "").strip()
        except (OSError, ValueError):
            logger.debug("could not read Sociamonials secrets file %s", secrets_path)
    return ""


def get_workspace_id(env: Mapping[str, str] | None = None) -> str:
    environ = env if env is not None else os.environ
    return str(environ.get(WORKSPACE_ENV) or DEFAULT_WORKSPACE_ID).strip()


def is_configured(env: Mapping[str, str] | None = None) -> bool:
    """True when an API key is available (the fallback can reach the API)."""
    return bool(get_api_key(env))


def is_enabled(env: Mapping[str, str] | None = None) -> bool:
    """True only when the opt-in flag is set *and* the API key is present."""
    environ = env if env is not None else os.environ
    return _truthy(environ.get(ENABLED_ENV)) and is_configured(environ)


def should_attempt_fallback(
    *,
    retryable: bool | None,
    attempts: int,
    max_attempts: int,
    enabled: bool | None = None,
) -> bool:
    """Guard for the worker: only after a genuine retry-budget exhaustion.

    ``retryable is False`` means the failure was classified permanent (banned
    subreddit, missing media, dead credential, ...).  Those must never be
    silently re-routed to a different account, so the fallback is skipped.
    """
    if retryable is False:
        return False
    if attempts < max_attempts:
        return False
    return is_enabled() if enabled is None else bool(enabled)


# --------------------------------------------------------------------------- #
# Mapping
# --------------------------------------------------------------------------- #

def _account_value(account: Any, key: str, default: Any = None) -> Any:
    if isinstance(account, Mapping):
        return account.get(key, default)
    return getattr(account, key, default)


def _normalise_entry(entry: Any) -> dict[str, Any] | None:
    """Accept a few operator-friendly config shapes and return one mapping."""
    if not isinstance(entry, Mapping):
        return None
    network = str(entry.get("network") or "").strip().lower()
    refs = entry.get("profile_refs")
    if refs is None and entry.get("profile_ref") is not None:
        refs = [entry.get("profile_ref")]
    if isinstance(refs, str):
        refs = [refs]
    refs = [str(ref).strip() for ref in (refs or []) if str(ref).strip()]
    if not refs:
        return None
    return {
        "network": network,
        "profile_refs": refs,
        "name": str(entry.get("name") or "").strip() or None,
    }


def _from_settings(settings: Any, account_id: int | None) -> dict[str, Any] | None:
    if not isinstance(settings, Mapping):
        return None
    raw = settings.get("sociamonials")
    if not isinstance(raw, Mapping):
        return None
    # shapes: {accountId: {...}} or {"accounts": {accountId: {...}}} or a single entry
    if "network" in raw:
        return _normalise_entry(raw)
    accounts = raw.get("accounts") if isinstance(raw.get("accounts"), Mapping) else raw
    if account_id is not None:
        found = accounts.get(account_id)
        if found is None:
            found = accounts.get(str(account_id))
        if isinstance(found, Mapping):
            merged = dict(found)
            merged.setdefault("network", raw.get("network"))
            return _normalise_entry(merged)
    return None


def resolve_mapping(
    account_id: int | None,
    *,
    platform: str | None = None,
    account: Any = None,
    settings: Any = None,
) -> dict[str, Any] | None:
    """Resolve an SAU account to ``{network, profile_refs, name}``.

    Precedence: per-account config, then profile settings, then the built-in
    map.  An explicit mapping must agree with the platform when both are known.
    """
    if account is None and account_id is not None:
        account = {"id": account_id}

    config = _account_value(account, "config", None) if account is not None else None
    if isinstance(config, Mapping):
        configured = _normalise_entry(config.get("sociamonials"))
        if configured:
            if not configured.get("network"):
                configured["network"] = _platform_network(platform) or configured["network"]
            return configured

    resolved = _from_settings(settings, account_id)
    if resolved:
        if not resolved.get("network"):
            resolved["network"] = _platform_network(platform) or resolved["network"]
        return resolved

    if account_id is not None:
        builtin = SAU_ACCOUNT_TO_SOCIAMONIALS.get(int(account_id))
        if builtin:
            entry = _normalise_entry(builtin)
            if entry and not entry.get("network"):
                entry["network"] = _platform_network(platform) or entry["network"]
            return entry
    return None


def _platform_network(platform: str | None) -> str | None:
    mapping = {
        "facebook": "fb",
        "twitter": "tw",
        "bluesky": "blsk",
        "threads": "thrd",
        "tiktok": "tiktok",
        "youtube": "yt",
        "instagram": "in",
        "linkedin": "ln",
        "pinterest": "pi",
    }
    return mapping.get(str(platform or "").strip().lower())


# --------------------------------------------------------------------------- #
# Media helpers
# --------------------------------------------------------------------------- #

def _has_media_suffix(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(tuple(_IMAGE_SUFFIXES | _VIDEO_SUFFIXES))


def is_direct_media_url(url: str | None) -> bool:
    """Best-effort check that ``url`` is a raw file URL, not a viewer page.

    The Graph API error this fallback exists to work around is exactly a
    non-direct URL, so a share/page link must never be treated as usable media.
    """
    raw = str(url or "").strip()
    if not raw.lower().startswith("https://"):
        return False
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if not host or host in _NON_DIRECT_HOSTS:
        return False
    if host.endswith(".google.com") or host.endswith(".googleusercontent.com"):
        # Drive viewer links; the raw download host (drive.usercontent.google.com)
        # is allowed through the suffix check below.
        if "usercontent" not in host:
            return False
    if "/file/d/" in parsed.path:
        return False
    return _has_media_suffix(raw)


def _verify_remote_media(url: str, session: Any, *, timeout: float) -> bool:
    """HEAD ``url`` and reject only a clear HTML/error response.

    A server that refuses HEAD (405/501) or cannot be reached is not proof the
    URL is bad, so it is allowed through; an explicit HTML content type or an
    outright error status is the only thing treated as a failure.
    """
    try:
        response = session.head(url, timeout=timeout, allow_redirects=True)
    except Exception:  # noqa: BLE001 - an unverifiable URL is not proof of anything
        logger.warning("could not HEAD media url %s for the fallback", url)
        return True
    status = int(getattr(response, "status_code", 0) or 0)
    if status in {405, 501}:
        return True
    if status >= 400 or status == 0:
        return False
    content_type = str(
        (getattr(response, "headers", {}) or {}).get("Content-Type") or ""
    ).lower()
    if content_type.startswith(("image/", "video/", "application/octet-stream")):
        return True
    return not content_type.startswith("text/html")


def _media_kind(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in _VIDEO_SUFFIXES:
        return "video"
    guessed = (mimetypes.guess_type(path)[0] or "").lower()
    return "video" if guessed.startswith("video/") else "image"


def collect_media(
    payload: Mapping[str, Any] | None, media_paths: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    """Normalise artifacts + explicit paths into ``{local_path, public_url, kind}``."""
    items: list[dict[str, Any]] = []
    artifacts = (payload or {}).get("artifacts") or []
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            continue
        local_path = str(artifact.get("local_path") or "").strip()
        public_url = str(artifact.get("public_url") or "").strip()
        role = ""
        meta = artifact.get("metadata")
        if isinstance(meta, Mapping):
            role = str(meta.get("role") or "").strip().lower()
        kind = "video" if role == "video" else ("image" if role == "image" else "")
        if not kind:
            kind = _media_kind(local_path or public_url)
        items.append(
            {"local_path": local_path or None, "public_url": public_url or None, "kind": kind}
        )
    if not items:
        for path in media_paths or []:
            text = str(path or "").strip()
            if text:
                items.append(
                    {"local_path": text, "public_url": None, "kind": _media_kind(text)}
                )
    return items


def _sha256(local_path: Path) -> str:
    digest = hashlib.sha256()
    with local_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _guess_mime(local_path: Path, kind: str) -> str:
    guessed = mimetypes.guess_type(local_path.name)[0]
    if guessed:
        return guessed
    return "video/mp4" if kind == "video" else "image/jpeg"


def _upload_local_media(
    session: Any,
    headers: Mapping[str, str],
    workspace_id: str,
    local_path: Path,
    *,
    idempotency_key: str | None,
    timeout: float,
) -> str:
    """Three-step upload; returns an ``asset://<id>`` reference."""
    kind = _media_kind(str(local_path))
    size_bytes = local_path.stat().st_size
    body: dict[str, Any] = {
        "filename": local_path.name,
        "mime_type": _guess_mime(local_path, kind),
        "size_bytes": size_bytes,
        "sha256": _sha256(local_path),
        "workspace_registration_id": _workspace_int(workspace_id),
    }
    if idempotency_key:
        body["idempotency_key"] = f"{idempotency_key}-media-{local_path.name}"

    response = session.post(MEDIA_UPLOADS_URL, json=body, headers=dict(headers), timeout=timeout)
    _raise_for_status(response, context="media upload grant")
    grant = _json_body(response)
    upload_id = grant.get("upload_id")
    if upload_id is None:
        raise SociamonialsFallbackError("media upload grant had no upload_id")

    if str(grant.get("mode") or "single").lower() == "multipart":
        _put_multipart(session, grant, local_path, timeout=timeout)
    else:
        upload_url = str(grant.get("upload_url") or "").strip()
        if not upload_url:
            raise SociamonialsFallbackError("media upload grant had no upload_url")
        # The pre-signed URL is its own bearer grant: no Authorization header.
        put = session.put(upload_url, data=local_path.read_bytes(), timeout=timeout)
        _raise_for_status(put, context="media upload PUT")

    complete_body: dict[str, Any] = {}
    if isinstance(grant.get("_parts"), list) and grant["_parts"]:
        complete_body["parts"] = grant["_parts"]
    complete = session.post(
        f"{MEDIA_UPLOADS_URL}/{upload_id}/complete",
        json=complete_body,
        headers=dict(headers),
        timeout=timeout,
    )
    _raise_for_status(complete, context="media upload complete")
    asset = _json_body(complete)
    asset_id = asset.get("asset_id")
    if asset_id is None:
        asset = asset.get("asset") if isinstance(asset.get("asset"), Mapping) else asset
        asset_id = asset.get("asset_id")
    if asset_id is None:
        raise SociamonialsFallbackError("media upload complete returned no asset_id")
    return f"asset://{asset_id}"


def _put_multipart(session: Any, grant: Mapping[str, Any], local_path: Path, *, timeout: float) -> None:
    """Split ``local_path`` into the grant's parts and PUT each one.

    The grant may carry the first batch of parts; the rest are fetched from the
    upload's parts endpoint.  Every PUT's ETag is kept for the complete call.
    """
    upload_id = grant.get("upload_id")
    part_size = int(grant.get("part_size_bytes") or 0)
    if not part_size:
        raise SociamonialsFallbackError("multipart upload grant had no part_size_bytes")
    data = local_path.read_bytes()
    total_parts = max(1, (len(data) + part_size - 1) // part_size)

    parts: dict[int, str] = {}

    def _put(part_number: int, url: str) -> None:
        start = (part_number - 1) * part_size
        response = session.put(url, data=data[start : start + part_size], timeout=timeout)
        _raise_for_status(response, context=f"media upload part {part_number}")
        etag = str((getattr(response, "headers", {}) or {}).get("ETag") or "").strip()
        if etag:
            parts[part_number] = etag

    for part in grant.get("parts") or []:
        if not isinstance(part, Mapping):
            continue
        number = int(part.get("part_number") or 0)
        url = str(part.get("url") or part.get("upload_url") or "").strip()
        if number and url:
            _put(number, url)

    for number in range(1, total_parts + 1):
        if number in parts:
            continue
        response = session.post(
            f"{MEDIA_UPLOADS_URL}/{upload_id}/parts",
            json={"part_number": number},
            timeout=timeout,
        )
        _raise_for_status(response, context=f"media upload parts {number}")
        payload = _json_body(response)
        candidates = payload.get("parts") if isinstance(payload.get("parts"), list) else [payload]
        for part in candidates:
            if not isinstance(part, Mapping):
                continue
            if int(part.get("part_number") or 0) == number:
                url = str(part.get("url") or part.get("upload_url") or "").strip()
                if url:
                    _put(number, url)
    if len(parts) < total_parts:
        raise SociamonialsFallbackError(
            f"multipart upload incomplete: {len(parts)}/{total_parts} parts"
        )
    # The complete call needs the ETags; keep them on the grant for the caller.
    grant["_parts"] = [{"part_number": n, "etag": parts[n]} for n in sorted(parts)]


# --------------------------------------------------------------------------- #
# Message composition
# --------------------------------------------------------------------------- #

def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, Mapping):
        pieces: list[str] = []
        for key in ("title", "summary", "description", "message", "body"):
            piece = _stringify(value.get(key))
            if piece:
                pieces.append(piece)
        for key, nested in value.items():
            if key in {"title", "summary", "description", "message", "body"}:
                continue
            if isinstance(nested, Mapping):
                for sub in nested.values():
                    piece = _stringify(sub)
                    if piece:
                        pieces.append(piece)
            elif isinstance(nested, list):
                for sub in nested:
                    piece = _stringify(sub)
                    if piece:
                        pieces.append(piece)
        hashtags = value.get("hashtags")
        if isinstance(hashtags, list):
            tags = " ".join(
                str(tag) if str(tag).startswith("#") else f"#{tag}" for tag in hashtags if str(tag).strip()
            )
            if tags:
                pieces.append(tags)
        return "\n\n".join(piece for piece in pieces if piece)
    if isinstance(value, (list, tuple)):
        return "\n\n".join(piece for piece in (_stringify(item) for item in value) if piece)
    return str(value).strip()


def _draft_from_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    draft = payload.get("draft") if isinstance(payload, Mapping) else None
    return draft if isinstance(draft, Mapping) else {}


def compose_message(payload: Mapping[str, Any], *, network: str) -> str:
    """Extract the publishable caption from the job payload."""
    payload = payload or {}
    draft = _draft_from_payload(payload)
    message = _stringify(draft.get("message") or payload.get("message"))
    if not message:
        message = _stringify(payload.get("brief") or payload.get("notes"))
    hashtags = draft.get("hashtags")
    if isinstance(hashtags, list) and hashtags:
        joined = " ".join(
            str(tag) if str(tag).startswith("#") else f"#{tag}"
            for tag in hashtags
            if str(tag).strip()
        )
        low = message.lower()
        missing = [tag for tag in joined.split() if tag.lower() not in low]
        if missing:
            message = f"{message}\n\n{' '.join(missing)}".strip()
    if network == "tw" and len(message) > 280:
        message = message[:279].rstrip() + "…"
    return message


def _network_options(payload: Mapping[str, Any], network: str) -> dict[str, Any]:
    draft = _draft_from_payload(payload)
    options: dict[str, Any] = {}
    title = _stringify(draft.get("title"))
    if network == "yt" and title:
        options["title"] = title
    if network == "reddit":
        options["title"] = title or None
    first_comment = _stringify(draft.get("firstComment"))
    return options


def _first_comment(payload: Mapping[str, Any]) -> str:
    draft = _draft_from_payload(payload)
    return _stringify(draft.get("firstComment"))


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #

def _timeout() -> float:
    try:
        return float(os.environ.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


def _workspace_int(workspace_id: Any) -> int | None:
    try:
        return int(str(workspace_id).strip())
    except (TypeError, ValueError):
        return None


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }


def _json_body(response: Any) -> dict[str, Any]:
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        body = None
    return body if isinstance(body, dict) else {}


def _raise_for_status(response: Any, *, context: str) -> None:
    status = int(getattr(response, "status_code", 0) or 0)
    if status and status < 400:
        return
    body = _json_body(response)
    detail = (
        body.get("error", {}).get("code")
        if isinstance(body.get("error"), Mapping)
        else body.get("error") or getattr(response, "text", "")
    )
    raise SociamonialsFallbackError(f"{context} failed (HTTP {status}): {detail}")


def _get_session(session: Any = None):
    if session is not None:
        return session
    import requests

    return requests.Session()


# --------------------------------------------------------------------------- #
# Publish
# --------------------------------------------------------------------------- #

def publish_via_sociamonials(
    *,
    platform: str,
    account: Any,
    payload: Mapping[str, Any],
    media_paths: Sequence[str] | None = None,
    target_id: int | None = None,
    settings: Any = None,
    session: Any = None,
    api_key: str | None = None,
    workspace_id: str | None = None,
    timeout: float | None = None,
    verify_media: bool = True,
) -> dict[str, Any]:
    """Publish one exhausted target through Sociamonials.

    Raises :class:`SociamonialsNotConfigured` when the fallback is off or has no
    key, and :class:`SociamonialsFallbackError` for any API/media failure.  The
    caller marks the target succeeded only when this returns.
    """
    key = (api_key if api_key is not None else get_api_key()).strip()
    if not key:
        raise SociamonialsNotConfigured("SOCIAMONIALS_API_KEY is not set")

    account_id = _account_value(account, "id", None)
    mapping = resolve_mapping(
        int(account_id) if account_id is not None else None,
        platform=platform,
        account=account,
        settings=settings,
    )
    if not mapping:
        raise SociamonialsFallbackError(
            f"no Sociamonials profile is mapped for account {account_id!r} ({platform})"
        )
    network = mapping["network"]
    platform_network = _platform_network(platform)
    if platform_network and network != platform_network:
        raise SociamonialsFallbackError(
            f"mapped network {network!r} does not match platform {platform!r}"
        )

    request_timeout = float(timeout if timeout is not None else _timeout())
    http = _get_session(session)
    headers = _headers(key)
    ws = str(workspace_id or get_workspace_id()).strip()

    message = compose_message(payload, network=network)
    if not message:
        raise SociamonialsFallbackError("payload contained no publishable message")

    body: dict[str, Any] = {
        "mode": "publish_now",
        "message": message,
        "networks": {network: {"profile_refs": list(mapping["profile_refs"])}},
    }
    if _workspace_int(ws) is not None:
        body["workspace_registration_id"] = _workspace_int(ws)
    first_comment = _first_comment(payload)
    if first_comment:
        body["first_comment"] = first_comment
    options = _network_options(payload, network)
    if options.get("title") and network == "yt":
        body["networks"][network]["title"] = options["title"]
    if target_id is not None:
        body["idempotency_key"] = f"sau-target-{target_id}"

    media = collect_media(payload, media_paths)
    warnings: list[str] = []
    if media:
        image_refs: list[str] = []
        video_ref: str | None = None
        for item in media:
            kind = item["kind"]
            reference: str | None = None
            public_url = item.get("public_url")
            if public_url and is_direct_media_url(public_url):
                if verify_media and not _verify_remote_media(public_url, http, timeout=request_timeout):
                    warnings.append(f"media url could not be verified: {public_url}")
                else:
                    reference = public_url
            if reference is None and item.get("local_path"):
                local = Path(str(item["local_path"]))
                if not local.exists():
                    raise SociamonialsFallbackError(f"local media missing: {local}")
                reference = _upload_local_media(
                    http,
                    headers,
                    ws,
                    local,
                    idempotency_key=f"sau-target-{target_id}" if target_id is not None else None,
                    timeout=request_timeout,
                )
            if reference is None:
                continue
            if kind == "video":
                video_ref = reference
            else:
                image_refs.append(reference)
        if video_ref:
            body["video_url"] = video_ref
            if image_refs:
                warnings.append("both video and image media were present; images were dropped")
        elif image_refs:
            body["image_urls"] = image_refs

    if network in _MEDIA_REQUIRED_NETWORKS and not (body.get("video_url") or body.get("image_urls")):
        raise SociamonialsFallbackError(
            f"{network} requires an image or video and no usable media was found"
        )

    response = http.post(POSTS_URL, json=body, headers=headers, timeout=request_timeout)
    _raise_for_status(response, context="create post")
    result = _json_body(response)
    result_warnings = result.get("warnings")
    if isinstance(result_warnings, list):
        warnings.extend(str(item) for item in result_warnings if str(item).strip())
    for warning in warnings:
        logger.warning("sociamonials fallback warning: %s", warning)
    requires_approval = bool(result.get("requires_approval"))
    if requires_approval:
        logger.warning(
            "sociamonials accepted the post but holds it for approval (post_id=%s)",
            result.get("post_id"),
        )
    return {
        "ok": True,
        "post_id": result.get("post_id"),
        "status": result.get("status"),
        "requires_approval": requires_approval,
        "warnings": warnings,
        "network": network,
        "profile_refs": list(mapping["profile_refs"]),
        "raw": result,
    }
