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
import re
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from myUtils import platform_limits

logger = logging.getLogger(__name__)

BASE_URL = "https://www.sociamonials.com"
POSTS_URL = f"{BASE_URL}/api/v1/posts"
MEDIA_UPLOADS_URL = f"{BASE_URL}/api/v1/media/uploads"
MEDIA_ASSETS_URL = f"{BASE_URL}/api/v1/media/assets"
DEFAULT_WORKSPACE_ID = "26985"

ENABLED_ENV = "SAU_SOCIAMONIALS_FALLBACK"
API_KEY_ENV = "SOCIAMONIALS_API_KEY"
WORKSPACE_ENV = "SOCIAMONIALS_WORKSPACE_ID"
SECRETS_FILE_ENV = "SOCIAMONIALS_SECRETS_FILE"
TIMEOUT_ENV = "SAU_SOCIAMONIALS_TIMEOUT"
ASSET_READY_TIMEOUT_ENV = "SAU_SOCIAMONIALS_ASSET_READY_TIMEOUT"
DELIVERY_TIMEOUT_ENV = "SAU_SOCIAMONIALS_DELIVERY_TIMEOUT"

# How long to wait for the platform to actually accept a post before calling
# the fallback done. A video hand-off can take a little while; a failed URL
# usually reports back quickly. 0 disables the check (accept-only semantics).
DEFAULT_DELIVERY_TIMEOUT = 60.0

DEFAULT_TIMEOUT = 60.0
# Hosted video is transcoded after the upload completes; attaching it before
# ``processing_status == "ready"`` is rejected with 422 validation_failed
# (asset_not_ready). Poll the asset for this long before giving up.
DEFAULT_ASSET_READY_TIMEOUT = 300.0

# Networks that only publish with media attached (Sociamonials validation).
_MEDIA_REQUIRED_NETWORKS = {"in", "tiktok", "yt", "pi"}

# Per-network caps, sourced from the single platform-limits table so the
# fallback cannot drift from the direct publishers. Sociamonials only surfaces
# an over-limit post as an opaque 422 at create time, so validate first.
NETWORK_MAX_VIDEO_SECONDS: dict[str, float] = {
    code: seconds
    for code, platform in platform_limits.NETWORK_TO_PLATFORM.items()
    if (seconds := platform_limits.video_max_seconds(platform))
}
NETWORK_MAX_MESSAGE_CHARS: dict[str, int] = {
    code: chars
    for code, platform in platform_limits.NETWORK_TO_PLATFORM.items()
    if (chars := platform_limits.message_max_chars(platform))
}
NETWORK_MAX_IMAGES: dict[str, int] = {
    code: count
    for code, platform in platform_limits.NETWORK_TO_PLATFORM.items()
    if (count := platform_limits.max_images(platform))
}

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
    # Account 103 (光光) publishes as nakedhappylife on X. Without this entry the
    # fallback raised "no Sociamonials profile is mapped", so every 103 target
    # failed outright once the X API credits ran out - even though Sociamonials
    # already holds a connected, publishable profile for the same handle.
    103: {"network": "tw", "profile_refs": ["14099"], "name": "nakedhappylife"},
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


def _assert_media_size(network: str, size_bytes: int) -> None:
    """Refuse media larger than the target network's file cap."""
    platform = platform_limits.NETWORK_TO_PLATFORM.get(network)
    max_mb = platform_limits.media_max_mb(platform) if platform else None
    if max_mb and size_bytes and size_bytes > max_mb * 1_000_000:
        raise SociamonialsFallbackError(
            f"{network} media {size_bytes / 1_000_000:.0f} MB exceeds the "
            f"{max_mb} MB limit"
        )


def _assert_video_duration(network: str, local_path: str | None) -> None:
    """Refuse a video longer than the target network allows.

    Only the local file can be probed, so a public-URL-only video is passed
    through and the network's own rejection (if any) still applies. A probe
    failure is not treated as a rejection.
    """
    max_seconds = NETWORK_MAX_VIDEO_SECONDS.get(network)
    if not max_seconds or not local_path:
        return
    path = Path(str(local_path))
    if not path.is_file():
        return
    try:
        from myUtils import media_pipeline

        duration = media_pipeline.probe_video_duration(str(path))
    except Exception as exc:  # noqa: BLE001 - a probe failure is not a rejection
        logger.warning("could not probe video duration for the fallback: %s", exc)
        return
    if duration and duration > max_seconds:
        raise SociamonialsFallbackError(
            f"{network} video duration {duration:.0f}s exceeds the "
            f"{int(max_seconds)}s limit"
        )


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
    timeout: float,
) -> str:
    """Three-step upload; returns an ``asset://<id>`` reference.

    No ``idempotency_key`` is sent: a repeated key with a *completed* multipart
    upload makes Sociamonials hand back the same closed session, whose part
    URLs then fail with ``NoSuchUpload``. Duplicate posts are already prevented
    by the post-level key; re-uploading a file costs only transient storage.
    """
    kind = _media_kind(str(local_path))
    size_bytes = local_path.stat().st_size
    body: dict[str, Any] = {
        "filename": local_path.name,
        "mime_type": _guess_mime(local_path, kind),
        "size_bytes": size_bytes,
        "sha256": _sha256(local_path),
        "workspace_registration_id": _workspace_int(workspace_id),
    }

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
    # A hosted video is transcoded asynchronously; it cannot be attached until
    # the asset reports ``processing_status == "ready"``. Images are ready at
    # once, so this is a single cheap GET for them.
    ready_timeout = _asset_ready_timeout()
    if ready_timeout > 0:
        _wait_for_asset_ready(session, headers, asset_id, timeout=ready_timeout)
    return f"asset://{asset_id}"


def _wait_for_asset_ready(
    session: Any,
    headers: Mapping[str, str],
    asset_id: Any,
    *,
    timeout: float,
    interval: float = 5.0,
) -> None:
    """Poll ``GET /api/v1/media/assets/{id}`` until the asset is attachable.

    Raises :class:`SociamonialsFallbackError` on a failed asset or when the
    readiness budget runs out, so the caller records a real failure instead of
    attaching a not-ready asset and getting an opaque 422.
    """
    import time as _time

    deadline = _time.monotonic() + max(0.0, float(timeout))
    last_status = "unknown"
    while True:
        try:
            response = session.get(
                f"{MEDIA_ASSETS_URL}/{asset_id}", headers=dict(headers), timeout=30
            )
            if int(getattr(response, "status_code", 0) or 0) == 200:
                body = _json_body(response)
                asset = body.get("asset") if isinstance(body.get("asset"), Mapping) else body
                last_status = str((asset or {}).get("processing_status") or "").strip().lower()
                if last_status == "ready":
                    return
                if last_status in {"failed", "error"}:
                    raise SociamonialsFallbackError(
                        f"media asset {asset_id} failed processing (status={last_status})"
                    )
            else:
                # A freshly-completed upload is briefly absent from the asset
                # index; keep polling rather than handing a not-ready asset to
                # the post call (which returns an opaque 422 asset_not_ready).
                logger.warning(
                    "sociamonials asset readiness poll HTTP %s for asset %s",
                    getattr(response, "status_code", "?"),
                    asset_id,
                )
        except SociamonialsFallbackError:
            raise
        except Exception as exc:  # noqa: BLE001 - a poll failure is not proof of anything
            logger.warning("sociamonials asset readiness poll failed: %s", exc)
        if _time.monotonic() >= deadline:
            raise SociamonialsFallbackError(
                f"media asset {asset_id} was not ready within {timeout:.0f}s "
                f"(status={last_status or 'unknown'})"
            )
        _time.sleep(interval)


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


_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)

# Networks whose main post must not contain a URL. Sociamonials' X/Twitter
# hand-off rejects a tweet body that carries a link, so any URL found in the
# composed X message is stripped out and carried in the follow-up reply
# (``first_comment``) instead. This is the single place that remembers the
# restriction; if Sociamonials ever lifts it, drop ``tw`` from this set and
# update the module docstring.
LINK_IN_MAIN_POST_FORBIDDEN_NETWORKS: frozenset[str] = frozenset({"tw"})


def _extract_urls(text: str) -> tuple[str, list[str]]:
    """Return ``(text_without_urls, urls)``, preserving the surrounding copy."""
    raw = text or ""
    urls = _URL_RE.findall(raw)
    if not urls:
        return raw.strip(), []
    cleaned = _URL_RE.sub("", raw)
    # Collapse the whitespace/blank lines the removal leaves behind.
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    # De-duplicate while keeping the original order.
    return cleaned, list(dict.fromkeys(urls))


def compose_message_with_links(
    payload: Mapping[str, Any], *, network: str
) -> tuple[str, list[str]]:
    """Compose the caption, returning ``(message, moved_links)``.

    For a network in :data:`LINK_IN_MAIN_POST_FORBIDDEN_NETWORKS` (X/Twitter)
    every URL is removed from the body and returned so the caller can carry it
    in the follow-up reply. The body is truncated to the platform limit *after*
    the links are removed, so a long URL cannot consume the whole post.
    """
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
    links: list[str] = []
    if network in LINK_IN_MAIN_POST_FORBIDDEN_NETWORKS:
        message, links = _extract_urls(message)
    limit = NETWORK_MAX_MESSAGE_CHARS.get(network)
    if limit and len(message) > limit:
        message = message[: limit - 1].rstrip() + "…"
    return message, links


def compose_message(payload: Mapping[str, Any], *, network: str) -> str:
    """Extract the publishable caption from the job payload."""
    message, _ = compose_message_with_links(payload, network=network)
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


def _delivery_timeout() -> float:
    try:
        return float(
            os.environ.get(DELIVERY_TIMEOUT_ENV) or DEFAULT_DELIVERY_TIMEOUT
        )
    except (TypeError, ValueError):
        return DEFAULT_DELIVERY_TIMEOUT


def _asset_ready_timeout() -> float:
    try:
        return float(
            os.environ.get(ASSET_READY_TIMEOUT_ENV) or DEFAULT_ASSET_READY_TIMEOUT
        )
    except (TypeError, ValueError):
        return DEFAULT_ASSET_READY_TIMEOUT


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
    error = body.get("error")
    if isinstance(error, Mapping):
        pieces = [str(error.get("code") or error.get("message") or "")]
        nested = error.get("errors")
        if isinstance(nested, Mapping):
            # Validation errors are keyed by the body field that failed
            # (image_urls, video_url, ...); keep them so a 422 is diagnosable.
            for field, messages in nested.items():
                pieces.append(f"{field}={messages}")
        elif isinstance(nested, list):
            pieces.extend(str(item) for item in nested[:4])
        detail = "; ".join(piece for piece in pieces if piece)
    else:
        detail = error or getattr(response, "text", "")
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
    delivery_timeout: float | None = None,
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

    warnings: list[str] = []
    message, moved_links = compose_message_with_links(payload, network=network)
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
    if moved_links:
        # X/Twitter cannot carry a link in the main post; put it in the
        # follow-up reply. Whether Sociamonials honours ``first_comment`` for
        # ``tw`` is not yet proven - the warning makes that visible if it is
        # ever rejected so the operator can fall back to link-free posts.
        moved = "\n".join(moved_links)
        first_comment = f"{first_comment}\n{moved}".strip() if first_comment else moved
        warnings.append(
            f"{network} forbids links in the main post; moved "
            f"{len(moved_links)} link(s) to the first comment/reply"
        )
    if first_comment:
        body["first_comment"] = first_comment
    options = _network_options(payload, network)
    if options.get("title") and network == "yt":
        body["networks"][network]["title"] = options["title"]
    if target_id is not None:
        body["idempotency_key"] = f"sau-target-{target_id}"

    media = collect_media(payload, media_paths)
    if media:
        image_refs: list[str] = []
        video_ref: str | None = None
        video_local_path: str | None = None
        for item in media:
            kind = item["kind"]
            reference: str | None = None
            local_path = item.get("local_path")
            public_url = item.get("public_url")
            # Prefer the prepared LOCAL file. The payload's public_url is often
            # the raw oversized original that just failed the direct publish;
            # re-attaching it is exactly how the Stonewall IG post got stuck.
            if local_path:
                local = Path(str(local_path))
                if local.exists():
                    _assert_media_size(network, local.stat().st_size)
                    reference = _upload_local_media(
                        http,
                        headers,
                        ws,
                        local,
                        timeout=request_timeout,
                    )
                elif not (public_url and is_direct_media_url(public_url)):
                    # A declared local artifact that is gone and no usable URL:
                    # nothing to attach.
                    raise SociamonialsFallbackError(f"local media missing: {local}")
            if reference is None and public_url and is_direct_media_url(public_url):
                if verify_media and not _verify_remote_media(public_url, http, timeout=request_timeout):
                    warnings.append(f"media url could not be verified: {public_url}")
                else:
                    reference = public_url
            if reference is None:
                continue
            if kind == "video":
                video_ref = reference
                video_local_path = str(item.get("local_path") or "") or None
            else:
                image_refs.append(reference)
        if video_ref:
            _assert_video_duration(network, video_local_path)
            body["video_url"] = video_ref
            if image_refs:
                warnings.append("both video and image media were present; images were dropped")
        elif image_refs:
            max_images = NETWORK_MAX_IMAGES.get(network)
            if max_images and len(image_refs) > max_images:
                warnings.append(
                    f"{network} accepts at most {max_images} images; "
                    f"dropped {len(image_refs) - max_images}"
                )
                image_refs = image_refs[:max_images]
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

    # Acceptance is not delivery. Sociamonials returns 200 as soon as the post
    # is queued and only discovers later that it cannot fetch the media (a
    # video URL it is refused, an R2 object not yet visible to it) - that has
    # already marked targets succeeded while nothing reached the platform. When
    # a post id comes back, poll it briefly and report the real delivery state.
    post_id = result.get("post_id")
    effective_delivery_timeout = (
        float(delivery_timeout)
        if delivery_timeout is not None
        else _delivery_timeout()
    )
    delivery = (
        _wait_for_delivery(
            http, headers, post_id, timeout=effective_delivery_timeout
        )
        if post_id and effective_delivery_timeout > 0
        else {"state": "unknown", "delivered": None, "error": ""}
    )

    return {
        "ok": True,
        "post_id": post_id,
        "status": result.get("status"),
        "requires_approval": requires_approval,
        "warnings": warnings,
        "network": network,
        "profile_refs": list(mapping["profile_refs"]),
        "delivery_state": delivery.get("state"),
        "delivered": delivery.get("delivered"),
        "delivery_error": delivery.get("error"),
        "raw": result,
    }


def _wait_for_delivery(
    http: Any,
    headers: dict[str, str],
    post_id: Any,
    *,
    timeout: float,
    interval: float = 5.0,
) -> dict[str, Any]:
    """Poll a created post until it is delivered, failed or ``timeout`` elapses.

    Returns ``{"state", "delivered", "error"}``. ``state`` is one of
    ``delivered``, ``failed``, ``pending`` (still queued when the budget ran
    out) or ``unknown`` (the status could not be read). Never raises: an
    unreadable status must not turn a successful submit into a fallback error.
    """
    import time as _time

    deadline = _time.monotonic() + max(0.0, float(timeout))
    last: dict[str, Any] = {"state": "unknown", "delivered": None, "error": ""}
    while True:
        try:
            response = http.get(
                f"{BASE_URL}/api/v1/posts/{post_id}", headers=headers, timeout=30
            )
            if response.status_code != 200:
                return last
            body = _json_body(response)
        except Exception as exc:  # noqa: BLE001 - status is advisory only
            logger.warning("sociamonials delivery poll failed: %s", exc)
            return last

        networks = body.get("networks")
        if isinstance(networks, dict) and networks:
            for network, detail in networks.items():
                if not isinstance(detail, dict):
                    continue
                status = str(detail.get("delivery_status") or "").strip().lower()
                delivered = bool(detail.get("delivered"))
                error = str(detail.get("error") or "").strip()
                if delivered or status == "delivered":
                    return {"state": "delivered", "delivered": True, "error": ""}
                if status in {"failed", "error"} or str(
                    detail.get("delivery_state") or ""
                ).strip().lower() == "needs_attention":
                    return {
                        "state": "failed",
                        "delivered": False,
                        "error": error[:400],
                    }
                last = {
                    "state": "pending",
                    "delivered": False,
                    "error": error[:400],
                }
        if _time.monotonic() >= deadline:
            return last
        _time.sleep(interval)
