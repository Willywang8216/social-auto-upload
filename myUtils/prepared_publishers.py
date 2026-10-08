"""Prepared-campaign publishers for API-driven platforms."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus, unquote, urlparse, urlunparse

logger = logging.getLogger(__name__)

from myUtils import media_pipeline
from myUtils import platform_limits
from myUtils import tiktok_auth

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - environment-specific
    requests = None

# Telethon is imported lazily: it is only needed when an account publishes to
# Telegram with its own MTProto user session (config["mtproto"]). Keeping the
# import optional lets non-Telegram installs / test runs stay light.
try:  # pragma: no cover - exercised where telethon is installed
    from telethon import TelegramClient, functions, types
    from telethon.sessions import StringSession
except ModuleNotFoundError:  # pragma: no cover - environment-specific
    TelegramClient = None
    functions = None
    types = None
    StringSession = None

TELEGRAM_API_ROOT = "https://api.telegram.org/bot{token}/{method}"
BLUESKY_API_ROOT = "https://bsky.social/xrpc"
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_SUBMIT_URL = "https://oauth.reddit.com/api/submit"
REDDIT_ME_URL = "https://oauth.reddit.com/api/v1/me"
REDDIT_MEDIA_LEASE_URL = "https://oauth.reddit.com/api/media/asset.json"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_RESUMABLE_UPLOAD_URL = (
    "https://www.googleapis.com/upload/youtube/v3/videos"
    "?uploadType=resumable&part=snippet,status"
)
YOUTUBE_PLAYLIST_INSERT_URL = (
    "https://www.googleapis.com/youtube/v3/playlistItems?part=snippet"
)
YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
FACEBOOK_GRAPH_ROOT = "https://graph.facebook.com/v25.0"
THREADS_GRAPH_ROOT = "https://graph.threads.net/v1.0"
THREADS_MAX_TEXT_CHARS = platform_limits.message_max_chars("threads") or 500
# Threads video posts are capped at 5 minutes / 1 GB (Instagram Reels, by
# contrast, allow up to 15 minutes). Overshoot is accepted at container
# creation and only fails later as an opaque container ``ERROR``, so probe the
# local artifact and fail fast with an actionable message.
THREADS_MAX_VIDEO_SECONDS = platform_limits.video_max_seconds("threads") or 300.0
THREADS_MAX_VIDEO_BYTES = (platform_limits.media_max_mb("threads") or 1024) * 1_000_000
THREADS_ALLOWED_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v"}
TIKTOK_API_ROOT = "https://open.tiktokapis.com"
TIKTOK_CREATOR_INFO_URL = f"{TIKTOK_API_ROOT}/v2/post/publish/creator_info/query/"
TIKTOK_VIDEO_INIT_URL = f"{TIKTOK_API_ROOT}/v2/post/publish/video/init/"
TIKTOK_CONTENT_INIT_URL = f"{TIKTOK_API_ROOT}/v2/post/publish/content/init/"
TIKTOK_STATUS_FETCH_URL = f"{TIKTOK_API_ROOT}/v2/post/publish/status/fetch/"
TIKTOK_MAX_PULL_FROM_URL_BYTES = (platform_limits.media_max_mb("tiktok") or 4096) * 1_000_000
TIKTOK_MIN_VIDEO_SECONDS = 3.0
TIKTOK_MAX_VIDEO_SECONDS = int(platform_limits.video_max_seconds("tiktok") or 3600)
TIKTOK_MAX_CAPTION_CHARS = platform_limits.message_max_chars("tiktok") or 2200
TIKTOK_ALLOWED_VIDEO_SUFFIXES = {".mp4", ".webm"}


def _enforce_message_limit(message: str, platform: str) -> str:
    """Trim a caption to the platform's hard message limit.

    Generation already trims, but a prepared/import/API payload arrives with a
    finished draft and skips that path; the publisher is the last line of
    defence. Returns the message unchanged when the platform has no cap.
    """
    limit = platform_limits.message_max_chars(platform)
    text = str(message or "")
    if limit is None or len(text) <= limit:
        return text
    logger.warning(
        "%s message is %d chars, over the %d-char limit; truncating",
        platform,
        len(text),
        limit,
    )
    return text[: limit - 1].rstrip() + "…"


def _enforce_video_limits(local_path: str, platform: str) -> None:
    """Fail fast when a local video breaks the platform's size/duration caps.

    The media-prep shrink is best-effort and only runs on the Publish Center
    import path; a prepared/API payload can reach a publisher unchecked. Probe
    the local artifact here so the error names the real limit instead of an
    opaque platform rejection later. A probe/size failure is not a rejection.
    """
    path = Path(str(local_path or ""))
    if not path.is_file():
        return
    size_limit_mb = platform_limits.media_max_mb(platform)
    if size_limit_mb:
        try:
            size_bytes = path.stat().st_size
        except OSError:
            size_bytes = 0
        if size_bytes > size_limit_mb * 1_000_000:
            raise PreparedPublishError(
                f"{platform} video {size_bytes / 1_000_000:.0f} MB exceeds the "
                f"{size_limit_mb} MB limit"
            )
    max_seconds = platform_limits.video_max_seconds(platform)
    if max_seconds:
        try:
            duration = media_pipeline.probe_video_duration(str(path))
        except Exception:  # noqa: BLE001 - a probe failure is not a rejection
            duration = None
        if duration and duration > max_seconds:
            raise PreparedPublishError(
                f"{platform} video duration {duration:.0f}s exceeds the "
                f"{max_seconds:.0f}s limit"
            )


def _tiktok_verified_url_prefixes() -> list[str]:
    """Return configured verified URL prefixes for TikTok PULL_FROM_URL."""
    raw = str(os.environ.get("SAU_TIKTOK_VERIFIED_URL_PREFIXES") or "").strip()
    if not raw:
        return []
    return [p.strip().rstrip("/") + "/" for p in raw.split(",") if p.strip()]


def _tiktok_validate_pull_from_url(public_url: str) -> str:
    """Validate that a public URL is under a verified TikTok prefix.

    Returns the URL if valid, raises PreparedPublishError otherwise.
    """
    if not public_url:
        raise PreparedPublishError(
            "TikTok Direct Post requires a public_url for PULL_FROM_URL. "
            "The media must be uploaded to server/storage first."
        )
    prefixes = _tiktok_verified_url_prefixes()
    if not prefixes:
        raise PreparedPublishError(
            "TikTok Direct Post requires SAU_TIKTOK_VERIFIED_URL_PREFIXES to be configured. "
            "Set it to your verified domain(s), comma-separated."
        )
    url_lower = public_url.lower().rstrip("/")
    for prefix in prefixes:
        if url_lower.startswith(prefix.lower().rstrip("/")):
            return public_url
    raise PreparedPublishError(
        f"TikTok Direct Post URL '{public_url[:80]}...' does not match any verified prefix. "
        f"Configure SAU_TIKTOK_VERIFIED_URL_PREFIXES with your verified domain(s)."
    )


class PreparedPublishError(RuntimeError):
    """Raised when a prepared publish cannot be completed."""

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.details = details or {}
        self.retryable = retryable


def _first_comment_text(payload: dict) -> str:
    """Return the trimmed first-comment text, or empty string."""
    draft = payload.get("draft") or {}
    text = str(draft.get("firstComment") or payload.get("firstComment") or "").strip()
    return text


def _try_post_first_comment(
    *,
    platform: str,
    post_id: str | None,
    text: str,
    poster,
    logger=None,
) -> None:
    """Best-effort: post a follow-up comment via ``poster(post_id, text)``.

    A failure is logged but does not propagate, so a flaky comments API
    cannot fail the main publish. ``poster`` must accept ``(post_id, text)``
    and may return any value.
    """
    if not (post_id and text):
        return
    try:
        poster(post_id, text)
    except Exception as exc:  # noqa: BLE001
        if logger is not None:
            try:
                logger("first_comment_failed", platform=platform, post_id=post_id, error=str(exc))
            except Exception:  # noqa: BLE001
                pass
        else:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "first-comment post failed on %s for post %s: %s",
                platform, post_id, exc,
            )


def _get_session(session=None):
    if session is not None:
        return session
    if requests is None:
        raise RuntimeError("requests is required for prepared publishers")
    return requests.Session()


_ACCESS_TOKEN_RE = re.compile(r"(access_token=)[^&\s\"']+", re.IGNORECASE)


def _redact_tokens(text: str) -> str:
    """Strip access_token values so they never reach logs, errors, or the UI."""
    return _ACCESS_TOKEN_RE.sub(r"\1<redacted>", text or "")


def _raise_for_status(response) -> None:
    if not hasattr(response, "raise_for_status"):
        return
    try:
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        status = getattr(response, "status_code", "?")
        # Only surface the body detail for actual error responses (>=400); for
        # 2xx we re-raise the original library exception so callers don't get
        # a misleading "HTTP 200: …" string from a non-fatal parse step.
        if isinstance(status, int) and status < 400:
            raise
        detail = ""
        try:
            body = response.json()
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                bits = [str(err.get("message") or "").strip()]
                for key in ("code", "error_subcode", "type"):
                    value = err.get(key)
                    if value not in (None, ""):
                        bits.append(f"{key}={value}")
                detail = " ".join(b for b in bits if b)
            elif body:
                detail = str(body)[:300]
        except Exception:  # noqa: BLE001
            detail = (getattr(response, "text", "") or "")[:300]
        # Collapse whitespace (Meta messages contain newlines) and redact tokens
        # so the access_token in the request URL never leaks into logs/UI.
        detail = _redact_tokens(" ".join(str(detail).split()))
        # Ordinary client errors are usually permanent input/auth/configuration
        # failures. Retrying them burns the target's budget and can repeat a
        # side effect after the provider has already accepted an earlier step.
        # Keep rate limits, timeouts, and server errors retryable.
        retryable = not (
            isinstance(status, int)
            and 400 <= status < 500
            and status not in {408, 425, 429}
        )
        if detail:
            raise PreparedPublishError(
                f"HTTP {status}: {detail}", retryable=retryable
            ) from None
        raise PreparedPublishError(
            _redact_tokens(str(exc)), retryable=retryable
        ) from None


    try:
        body = response.json()
    except Exception:
        body = {}
    if isinstance(body, dict) and body.get("ok") is False:
        description = str(body.get("description") or "Telegram API rejected the request")
        description = _redact_tokens(description)
        raise PreparedPublishError(f"Telegram API error: {description}")


def _raise_tiktok_error(response) -> None:
    """Raise a PreparedPublishError with TikTok-specific error details."""
    try:
        body = response.json()
        error = body.get("error") or {}
        code = str(error.get("code") or "").strip()
        message = str(error.get("message") or "").strip()
    except Exception:  # noqa: BLE001
        code = ""
        message = ""
    if code == "invalid_params":
        raise PreparedPublishError(
            f"TikTok API error (invalid_params): {message or 'The post parameters are invalid'}",
            retryable=False,
        )
    if code == "unaudited_client_can_only_post_to_private_accounts":
        raise PreparedPublishError(
            "TikTok app is in development mode — can only post to private accounts. "
            "Submit your app for review at developers.tiktok.com or set your TikTok account to private for testing.",
            retryable=False,
        )
    if code == "url_ownership_unverified":
        raise PreparedPublishError(
            "TikTok requires domain ownership verification for PULL_FROM_URL. "
            "Using FILE_UPLOAD mode instead."
        )
    if message:
        raise PreparedPublishError(
            f"TikTok API error ({code}): {message}",
            retryable=code not in {"invalid_params"},
        )
    response.raise_for_status()


def _config_value(config: dict[str, Any], key: str, *, default_env: str | None = None) -> Any:
    direct = config.get(key)
    if direct not in (None, ""):
        return direct
    env_name = config.get(f"{key}Env")
    if env_name:
        return os.environ.get(str(env_name), "")
    if default_env:
        return os.environ.get(default_env, "")
    return ""


def _payload_message(payload: dict) -> str:
    return str(payload.get("message") or payload.get("draft", {}).get("message", "")).strip()


def _message_title(payload: dict, *, fallback: str = "Campaign post") -> str:
    raw = _payload_message(payload) or fallback
    return raw.splitlines()[0].strip()[:100] or fallback


def _response_payload(response):
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return {}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith('Z'):
            value = value[:-1] + '+00:00'
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _token_expiry_from_payload(payload: dict, key: str) -> str:
    seconds = payload.get(key)
    try:
        seconds_int = int(seconds)
    except (TypeError, ValueError):
        return ''
    return (_utc_now() + timedelta(seconds=seconds_int)).replace(microsecond=0).isoformat()


# --- X (Twitter) OAuth 2.0 token refresh -------------------------------------
#
# X rotates (invalidates) the refresh token on every successful refresh, so two
# concurrent refreshes of the same account leave it holding a dead token: the
# slower request is rejected, and a stale config write can clobber the winner's
# freshly rotated token. The backend maintenance thread, the worker's
# maintenance tick and the publish path can all refresh the same account, so
# they share one process-wide lock keyed by the account id. The lock is held
# across the re-read, the network refresh and the persist, which makes the
# sequence atomic and lets a loser reuse the winner's token instead of burning
# the old one.
_TWITTER_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_TWITTER_REFRESH_LOCKS_GUARD = threading.Lock()


def twitter_refresh_lock(account_id: object = None) -> threading.Lock:
    """Return the process-wide single-flight lock for one X account."""
    key = str(account_id) if account_id is not None else "__x_default__"
    with _TWITTER_REFRESH_LOCKS_GUARD:
        lock = _TWITTER_REFRESH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _TWITTER_REFRESH_LOCKS[key] = lock
    return lock


def _parse_token_expiry(value: str | None) -> datetime | None:
    """Parse a stored token expiry into an absolute UTC datetime.

    Writers historically stamped tokens with ``datetime.now()`` (naive server
    local) while readers assumed naive meant UTC, which shifted a 2-hour X token
    by the server's UTC offset (up to 8h here) and let it expire before the
    proactive refresh noticed. Treat a naive legacy value as server-local so its
    absolute instant is recovered; a naive-UTC legacy value is then read as up
    to one offset earlier, i.e. it only ever looks *staler*, which refreshes
    early rather than late and is therefore safe.
    """
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
        # datetime.astimezone() on a naive value attaches the local timezone.
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


def _x_access_token_stale(config: dict[str, Any], *, skew_seconds: int = 300) -> bool:
    """True when the X OAuth 2.0 access token is missing, unknown or near expiry."""
    access_token = str(config.get("accessToken") or "").strip()
    if not access_token:
        return True
    expires_at = _parse_token_expiry(config.get("accessTokenExpiresAt"))
    if expires_at is None:
        # No trustworthy expiry: refresh to establish one rather than let a
        # 2-hour token lapse silently.
        return True
    return expires_at <= (_utc_now() + timedelta(seconds=skew_seconds))


def _apply_twitter_token_payload(config: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Merge an X token response into ``config`` using one UTC time base."""
    updated = dict(config)
    access_token = str(result.get("access_token") or "").strip()
    if access_token:
        updated["accessToken"] = access_token
    refresh_token = str(result.get("refresh_token") or "").strip()
    if refresh_token:
        updated["refreshToken"] = refresh_token
    if result.get("scope"):
        updated["scope"] = result["scope"]
    if result.get("token_type"):
        updated["tokenType"] = result["token_type"]
    expires_in = result.get("expires_in")
    if expires_in not in (None, ""):
        updated["accessTokenExpiresAt"] = _token_expiry_from_payload(
            {"expires_in": expires_in}, "expires_in"
        )
    updated["accessTokenUpdatedAt"] = _utc_now().replace(microsecond=0).isoformat()
    me = result.get("me") or {}
    user_data = me.get("data", me) if isinstance(me, dict) else {}
    if isinstance(user_data, dict):
        if user_data.get("id"):
            updated["twitterUserId"] = str(user_data["id"])
        if user_data.get("username"):
            updated["twitterUserName"] = str(user_data["username"])
        if user_data.get("name"):
            updated["twitterDisplayName"] = str(user_data["name"])
        if user_data.get("profile_image_url"):
            updated["avatarUrl"] = str(user_data["profile_image_url"])
    return updated


def _read_account_config(account_id: object, db_path: object = None) -> dict[str, Any] | None:
    """Read the authoritative account config, or ``None`` when unavailable."""
    if account_id is None:
        return None
    try:
        from myUtils import profiles as profile_registry

        kwargs = {"db_path": db_path} if db_path else {}
        account = profile_registry.get_account(int(account_id), **kwargs)
        return dict(account.config or {})
    except Exception:  # noqa: BLE001 - fall back to the caller's copy
        return None


def refresh_twitter_token_single_flight(
    config: dict[str, Any],
    *,
    account_id: object = None,
    db_path: object = None,
    session=None,
    skew_seconds: int = 300,
    persist=None,
    extra_fields: dict[str, Any] | None = None,
    clear_markers: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Refresh the X token at most once per account, even under concurrency.

    Returns ``(config, refreshed)``. The read-check-refresh-persist sequence
    runs under :func:`twitter_refresh_lock`, so a caller that loses the race
    re-reads the winner's rotated token and skips the network call. ``persist``
    is invoked inside the lock; callers that need their own write semantics
    pass a callable.
    """
    lock = twitter_refresh_lock(account_id)
    with lock:
        latest = _read_account_config(account_id, db_path)
        merged = {**config, **latest} if latest is not None else dict(config)
        if not _x_access_token_stale(merged, skew_seconds=skew_seconds):
            return merged, False
        result = refresh_twitter_access_token(merged, session=session)
        updated = _apply_twitter_token_payload(merged, result)
        if clear_markers:
            for marker in (
                "_needsReconnect", "_reconnectAlertedAt", "_maintenanceFailures",
                "_nextMaintenanceAttemptAt", "_lastMaintenanceError",
                "_lastMaintenanceAttemptAt",
            ):
                updated.pop(marker, None)
        if extra_fields:
            updated.update(extra_fields)
        if persist is not None:
            persist(updated)
        return updated, True


def _apply_tiktok_token_payload(config: dict[str, Any], token_payload: dict, user_info: dict | None = None) -> dict[str, Any]:
    next_config = dict(config)
    access_token = str(token_payload.get('access_token') or '').strip()
    refresh_token = str(token_payload.get('refresh_token') or '').strip()
    if access_token:
        next_config['accessToken'] = access_token
    if refresh_token:
        next_config['refreshToken'] = refresh_token
    if token_payload.get('open_id'):
        next_config['openId'] = token_payload.get('open_id')
    if token_payload.get('scope'):
        next_config['scope'] = token_payload.get('scope')
    next_config['accessTokenUpdatedAt'] = _utc_now().replace(microsecond=0).isoformat()
    access_expires_at = _token_expiry_from_payload(token_payload, 'expires_in')
    refresh_expires_at = _token_expiry_from_payload(token_payload, 'refresh_expires_in')
    if access_expires_at:
        next_config['accessTokenExpiresAt'] = access_expires_at
    if refresh_expires_at:
        next_config['refreshTokenExpiresAt'] = refresh_expires_at
    if user_info:
        user = user_info.get('data', {}).get('user', {})
        if user.get('display_name'):
            next_config['displayName'] = user['display_name']
        if user.get('avatar_url'):
            next_config['avatarUrl'] = user['avatar_url']
        if user.get('open_id'):
            next_config['openId'] = user['open_id']
    return next_config


def _is_tiktok_access_token_stale(config: dict[str, Any], *, skew_seconds: int = 300) -> bool:
    access_token = str(config.get('accessToken') or '').strip()
    if not access_token:
        return True
    expires_at = _parse_iso_datetime(str(config.get('accessTokenExpiresAt') or ''))
    if expires_at is None:
        return False
    return expires_at <= (_utc_now() + timedelta(seconds=skew_seconds))


def _ensure_tiktok_access_token(config: dict[str, Any], *, session=None) -> tuple[str, dict[str, Any] | None]:
    access_token = str(_config_value(config, 'accessToken') or '').strip()
    refresh_token = str(_config_value(config, 'refreshToken') or '').strip()
    if access_token and not _is_tiktok_access_token_stale(config):
        return access_token, None
    if not refresh_token:
        if access_token:
            return access_token, None
        raise PreparedPublishError('TikTok publish requires accessToken or a refreshable TikTok connection')
    http = _get_session(session)
    token_payload = tiktok_auth.refresh_access_token(refresh_token=refresh_token, session=http)
    next_config = _apply_tiktok_token_payload(config, token_payload)
    return str(next_config.get('accessToken') or ''), next_config


def _normalize_public_url(url: str) -> str:
    """Percent-encode characters a platform's media fetcher rejects.

    Stored artifacts written before the CDN encoding fix (and any generated key
    with a literal space, e.g. ``SFW clip.mp4``) otherwise reach the Graph /
    Threads / TikTok fetch guard as an invalid URL and fail with an opaque
    "Unable to fetch" 400. Only the unsafe characters are encoded, so an
    already-encoded URL is unchanged.
    """
    text = str(url or "").strip()
    if not text:
        return text
    parts = urlparse(text)
    if not parts.scheme or not parts.netloc:
        return text
    return urlunparse(
        (
            parts.scheme,
            parts.netloc,
            quote(parts.path, safe="/%:@!$&'()*+,;="),
            parts.params,
            quote(parts.query, safe="=&%?+:;,/"),
            quote(parts.fragment, safe=""),
        )
    )


def _extract_media(payload: dict) -> dict[str, list[dict[str, str]]]:
    items_by_key: dict[str, dict[str, str]] = {}
    for artifact in payload.get("artifacts", []) or []:
        local_path = artifact.get("local_path") or ""
        public_url = _normalize_public_url(artifact.get("public_url") or "")
        key = local_path or public_url
        if not key:
            continue
        existing = items_by_key.get(key)
        if existing is None:
            existing = {
                "local_path": local_path,
                "public_url": public_url,
                "artifact_kind": artifact.get("artifact_kind", ""),
            }
            items_by_key[key] = existing
        elif public_url and not existing.get("public_url"):
            existing["public_url"] = public_url

    images: list[dict[str, str]] = []
    videos: list[dict[str, str]] = []
    for item in items_by_key.values():
        probe = item.get("local_path") or item.get("public_url") or ""
        suffix = Path(probe).suffix.lower()
        kind = item.get("artifact_kind", "")
        if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"} or "image" in kind:
            images.append(item)
        elif suffix in {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"} or "video" in kind:
            videos.append(item)
    return {"images": images, "videos": videos}


def _telegram_caption_chunks(message: str) -> tuple[str, str]:
    if len(message) <= 1024:
        return message, ""
    return message[:1024], message


def _telegram_chat_id_list(value: Any) -> list[str]:
    """Normalise a Telegram chat-id input (list, csv, scalar) to a list of non-empty strings.

    Empty inputs collapse to ``[]``. Whitespace is trimmed. Used by both the
    publisher and the live validator so the precedence rules stay in one
    place.
    """
    if value is None:
        return []
    if isinstance(value, list):
        candidates: list[Any] = value
    elif isinstance(value, str):
        candidates = value.split(",")
    else:
        candidates = [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if item is None:
            continue
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _telegram_resolve_chat_ids(config: dict[str, Any], payload: dict | None = None) -> list[str]:
    """Resolve the ordered list of Telegram chat ids to publish to.

    Precedence:
      1. ``payload.draft.chatIds`` (per-publish override, list or csv)
      2. ``config.chatIds`` (account-level default, list or csv)
      3. ``config.chatId`` (legacy single id, wrapped in a one-element list)
    """
    payload = payload or {}
    draft = payload.get("draft") or {}
    return (
        _telegram_chat_id_list(draft.get("chatIds"))
        or _telegram_chat_id_list(config.get("chatIds"))
        or _telegram_chat_id_list(config.get("chatId"))
    )


def validate_telegram_config_live(config: dict[str, Any], *, session=None) -> dict:
    if _telegram_is_mtproto(config):
        return _validate_telegram_mtproto_config_live(config)

    token = str(_config_value(config, "botToken", default_env="TELEGRAM_BOT_TOKEN") or "").strip()
    chat_ids = _telegram_resolve_chat_ids(config)
    if not token:
        raise PreparedPublishError("Telegram validation requires botToken or botTokenEnv")
    if not chat_ids:
        raise PreparedPublishError("Telegram validation requires chatId or chatIds")

    http = _get_session(session)
    bot_response = http.post(
        TELEGRAM_API_ROOT.format(token=token, method="getMe"),
        data={},
        timeout=120,
    )
    _raise_for_status(bot_response)
    chat_payloads: list[dict[str, Any]] = []
    for chat_id in chat_ids:
        chat_response = http.post(
            TELEGRAM_API_ROOT.format(token=token, method="getChat"),
            data={"chat_id": chat_id},
            timeout=120,
        )
        _raise_for_status(chat_response)
        chat_payloads.append({"chatId": chat_id, "result": _response_payload(chat_response)})
    bot_payload = _response_payload(bot_response)
    # Keep the legacy ``chat`` key (singular) so existing readers see the
    # first chat as if it were the only one.
    return {
        "bot": bot_payload,
        "chats": chat_payloads,
        "chat": chat_payloads[0]["result"] if chat_payloads else {},
    }


def _validate_telegram_mtproto_config_live(config: dict[str, Any]) -> dict:
    """Validate an MTProto (user-account) Telegram config without a server call.

    The StringSession encodes the login, so a cheap validation is: every
    required key present, and — when a string session is provided — that it
    round-trips through Telethon's ``StringSession`` constructor. Full live
    connectivity checks happen on the first real publish.
    """
    if TelegramClient is None:
        raise PreparedPublishError(
            "Telegram MTProto validation requires the 'telethon' package to be installed"
        )
    chat_ids = _telegram_resolve_chat_ids(config)
    if not chat_ids:
        raise PreparedPublishError("Telegram MTProto validation requires chatId or chatIds")
    api_id_raw = str(_config_value(config, "apiId") or "").strip()
    api_hash = str(_config_value(config, "apiHash") or "").strip()
    if not api_id_raw or not api_hash:
        raise PreparedPublishError("Telegram MTProto validation requires apiId and apiHash in config")
    try:
        api_id = int(api_id_raw)
    except (TypeError, ValueError) as exc:
        raise PreparedPublishError(f"Telegram MTProto apiId must be an integer, got {api_id_raw!r}") from exc
    session_string = _telegram_mtproto_session_string(config)
    session_obj = StringSession(session_string)
    return {
        "mode": "mtproto",
        "api_id": api_id,
        "has_session": bool(str(session_obj.save())),
        "chats": chat_ids,
    }


def _publish_telegram_to_one(
    http,
    *,
    token: str,
    chat_id: str,
    message: str,
    caption: str,
    overflow_message: str,
    parse_mode: str | None,
    silent: str,
    disable_preview: str,
    media: dict[str, list[dict[str, str]]],
    completed_operations: set[str] | None = None,
) -> dict[str, Any]:
    """Send the given payload to a single Telegram chat and return a status dict.

    Mirrors the original single-chat logic from ``publish_telegram_sync`` but
    is chat-id-parameterised so the fan-out wrapper can reuse it.
    """
    attachments = [*media["videos"], *media["images"]]
    responses: list[Any] = []
    errors: list[str] = []
    retry_safe = True
    completed = completed_operations if completed_operations is not None else set()

    def _post(operation_key: str, method: str, **kwargs):
        nonlocal retry_safe
        if f"ambiguous:{operation_key}" in completed:
            retry_safe = False
            raise PreparedPublishError(
                f"Telegram {operation_key} may already have been accepted; reconcile delivery before retrying",
                retryable=False,
            )
        if operation_key in completed:
            return None
        try:
            response = http.post(TELEGRAM_API_ROOT.format(token=token, method=method), **kwargs)
        except Exception:
            retry_safe = False
            completed.add(f"ambiguous:{operation_key}")
            raise
        try:
            _raise_for_status(response)
        except PreparedPublishError as exc:
            status = int(getattr(response, "status_code", 0) or 0)
            if status >= 500 or status == 0:
                retry_safe = False
                completed.add(f"ambiguous:{operation_key}")
            # Telegram's per-chat validation failures are safe to retry after
            # the operator fixes the destination; preserve that fan-out
            # contract even though the shared HTTP helper classifies ordinary
            # 4xx responses as permanent for API publishers.
            if 400 <= status < 500 and status != 429 and exc.retryable is False:
                raise PreparedPublishError(str(exc), details=exc.details, retryable=True) from exc
            raise
        try:
            body = response.json()
            if isinstance(body, dict) and body.get("ok") is False:
                description = _redact_tokens(str(body.get("description") or "Telegram API rejected the request"))
                status = int(getattr(response, "status_code", 0) or 0)
                if status != 429:
                    retry_safe = False
                    completed.add(f"ambiguous:{operation_key}")
                raise PreparedPublishError(
                    f"Telegram API error: {description}", retryable=status == 429
                )
        except PreparedPublishError:
            raise
        except Exception:
            pass
        responses.append(response)
        completed.add(operation_key)
        return response

    try:
        if not attachments:
            data = {
                "chat_id": chat_id,
                "text": message,
                "disable_notification": silent,
                "disable_web_page_preview": disable_preview,
            }
            if parse_mode:
                data["parse_mode"] = parse_mode
            _post("message", "sendMessage", data=data, timeout=120)
        elif len(attachments) == 1:
            item = attachments[0]
            is_video = item in media["videos"]
            method = "sendVideo" if is_video else "sendPhoto"
            field_name = "video" if is_video else "photo"
            data = {
                "chat_id": chat_id,
                "caption": caption,
                "disable_notification": silent,
            }
            if parse_mode:
                data["parse_mode"] = parse_mode
            local_path = item.get("local_path")
            if local_path:
                with Path(local_path).open("rb") as handle:
                    _post("media", method, data=data, files={field_name: (Path(local_path).name, handle)}, timeout=600)
            else:
                data[field_name] = item.get("public_url")
                _post("media", method, data=data, timeout=120)
            if overflow_message and "media" in completed:
                _post(
                    "overflow",
                    "sendMessage",
                    data={
                        "chat_id": chat_id,
                        "text": overflow_message,
                        "disable_notification": silent,
                        "disable_web_page_preview": disable_preview,
                        **({"parse_mode": parse_mode} if parse_mode else {}),
                    },
                    timeout=120,
                )
        elif len(attachments) > 1:
            media_payload = []
            files = {}
            open_files = []
            try:
                for index, item in enumerate(attachments):
                    is_video = item in media["videos"]
                    local_path = item.get("local_path")
                    media_entry = {
                        "type": "video" if is_video else "photo",
                        "media": item.get("public_url") or f"attach://media{index}",
                    }
                    if index == 0 and caption:
                        media_entry["caption"] = caption
                        if parse_mode:
                            media_entry["parse_mode"] = parse_mode
                    media_payload.append(media_entry)
                    if local_path:
                        handle = Path(local_path).open("rb")
                        open_files.append(handle)
                        files[f"media{index}"] = (Path(local_path).name, handle)
                _post(
                    "sendMediaGroup",
                    data={
                        "chat_id": chat_id,
                        "disable_notification": silent,
                        "media": json.dumps(media_payload, ensure_ascii=False),
                    },
                    files=files,
                    timeout=600,
                )
            finally:
                for handle in open_files:
                    handle.close()
            if overflow_message and "album" in completed:
                _post(
                    "overflow",
                    "sendMessage",
                    data={
                        "chat_id": chat_id,
                        "text": overflow_message,
                        "disable_notification": silent,
                        "disable_web_page_preview": disable_preview,
                        **({"parse_mode": parse_mode} if parse_mode else {}),
                    },
                    timeout=120,
                )
    except PreparedPublishError as exc:
        errors.append(str(exc))
        if exc.retryable is False:
            retry_safe = False
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))
        retry_safe = False

    return {
        "chatId": chat_id,
        "ok": not errors,
        "errors": errors,
        "responses": responses,
        "retrySafe": retry_safe,
        "completedOperations": sorted(completed),
    }


def _telegram_is_mtproto(config: dict[str, Any]) -> bool:
    """True when a Telegram account publishes as the user (MTProto) not a bot.

    Detected from an explicit ``mtproto: true`` / ``authMode: user`` flag, or
    (for accounts configured through the UI form, which has no flag) from the
    presence of apiId/apiHash credentials. botToken-only accounts stay on the
    Bot API path.
    """
    if bool(config.get("mtproto")):
        return True
    if str(config.get("authMode") or "").strip().lower() == "user":
        return True
    api_id = str(_config_value(config, "apiId") or "").strip()
    api_hash = str(_config_value(config, "apiHash") or "").strip()
    return bool(api_id and api_hash)


def publish_telegram_sync(account, payload: dict, *, session=None) -> list[Any]:
    config = account.config or {}
    artifacts = payload.get("artifacts") or []
    media = _extract_media(payload)
    attachments = [*media["videos"], *media["images"]]
    if artifacts and not attachments:
        raise PreparedPublishError("Telegram media artifacts were supplied but none is a supported image/video")
    if len(attachments) > 10:
        raise PreparedPublishError("Telegram supports at most 10 media items per album")
    if len(attachments) > 1 and any(item not in media["images"] for item in attachments):
        raise PreparedPublishError("Telegram media groups support image albums only; split video and mixed-media jobs")
    for item in attachments:
        local_path = str(item.get("local_path") or "").strip()
        public_url = str(item.get("public_url") or "").strip()
        if local_path:
            path = Path(local_path)
            if not path.is_file() or path.stat().st_size <= 0:
                raise PreparedPublishError(f"Telegram media file is missing or empty: {path}")
        elif not public_url:
            raise PreparedPublishError("Telegram media artifact has no usable local path or public URL")
    if _telegram_is_mtproto(config):
        return _publish_telegram_mtproto_sync(account, payload, session=session)

    token = str(_config_value(config, "botToken", default_env="TELEGRAM_BOT_TOKEN") or "").strip()
    chat_ids = _telegram_resolve_chat_ids(config, payload)
    if not token:
        raise PreparedPublishError("Telegram publish requires botToken or botTokenEnv")
    if not chat_ids:
        raise PreparedPublishError("Telegram publish requires chatId or chatIds")

    http = _get_session(session)
    message = _payload_message(payload)
    caption, overflow_message = _telegram_caption_chunks(message)
    parse_mode = str(config.get("parseMode") or "").strip() or None
    silent = "true" if bool(config.get("silent", False)) else "false"
    disable_preview = "true" if bool(config.get("disableWebPreview", False)) else "false"
    media = _extract_media(payload)

    delivery_key = str(payload.get("_telegramDeliveryKey") or "default")
    stored_completion = payload.get("telegramCompletedByDelivery")
    if not isinstance(stored_completion, dict):
        stored_completion = {}
    completed_by_chat = stored_completion.get(delivery_key)
    if not isinstance(completed_by_chat, dict):
        completed_by_chat = {}

    results: list[dict[str, Any]] = []
    for chat_id in chat_ids:
        completed = completed_by_chat.get(chat_id)
        if not isinstance(completed, list):
            completed = []
        result = _publish_telegram_to_one(
            http,
            token=token,
            chat_id=chat_id,
            message=message,
            caption=caption,
            overflow_message=overflow_message,
            parse_mode=parse_mode,
            silent=silent,
            disable_preview=disable_preview,
            media=media,
            completed_operations=set(completed),
        )
        results.append(result)
        completed_by_chat[chat_id] = sorted(
            set(completed) | set(result.get("completedOperations", []))
        )

    payload.setdefault("telegramCompletedByDelivery", {})[delivery_key] = completed_by_chat
    failures = [item for item in results if not item["ok"]]
    if failures:
        successful = [item for item in results if item["ok"]]
        retry_safe = all(item.get("retrySafe", True) for item in failures)
        summary = {
            "platform": "telegram",
            "partial": bool(successful),
            "retrySafe": retry_safe,
            "chats": [
                {
                    "chatId": item["chatId"],
                    "ok": item["ok"],
                    "retrySafe": item.get("retrySafe", True),
                    "errors": item["errors"],
                }
                for item in results
            ],
        }
        joined = "; ".join(
            f"{item['chatId']}: {'; '.join(item['errors']) or 'unknown error'}"
            for item in failures
        )
        partial_label = "partial" if successful else "all"
        raise PreparedPublishError(
            f"Telegram publish failed for {partial_label} delivery ({len(successful)} succeeded, {len(failures)} failed): {joined}",
            details=summary,
            retryable=retry_safe,
        )
    return results


def _telegram_mtproto_session_string(config: dict[str, Any]) -> str:
    """Resolve the Telethon StringSession for a user-account Telegram publish.

    Precedence: ``config.sessionString`` (direct) -> ``config.sessionStringEnv``
    (named env var) -> ``config.sessionFile`` (path to a .session file).
    """
    session_string = str(config.get("sessionString") or "").strip()
    if session_string:
        return session_string
    env_name = str(config.get("sessionStringEnv") or "").strip()
    if env_name:
        value = os.environ.get(env_name, "")
        if value:
            return str(value).strip()
    session_file = str(config.get("sessionFile") or "").strip()
    if session_file:
        from telethon.sessions import StringSession

        return StringSession.read_file(session_file)
    raise PreparedPublishError(
        "Telegram MTProto publish requires sessionString / sessionStringEnv "
        "(a Telethon StringSession) or sessionFile"
    )


def _publish_telegram_mtproto_one(
    client,
    *,
    chat_id: str,
    message: str,
    caption: str,
    overflow_message: str,
    silent: bool,
    media: dict[str, list[dict[str, str]]],
    completed_operations: set[str] | None = None,
) -> dict[str, Any]:
    """Send the payload to a single chat over MTProto (as the user account).

    Mirror of ``_publish_telegram_to_one`` for the Bot API, but driven through
    Telethon so posts appear from the logged-in user rather than a bot.
    """
    from telethon.tl.types import InputMessagesFilterEmpty  # noqa: F401

    attachments = [*media["videos"], *media["images"]]
    errors: list[str] = []
    retry_safe = True
    completed = completed_operations if completed_operations is not None else set()

    async def _send():
        nonlocal retry_safe
        peer = await client.get_input_entity(chat_id)
        if not attachments:
            text = message if not caption or caption == message else caption
            if "message" not in completed:
                try:
                    await client.send_message(peer, text, silent=silent)
                    completed.add("message")
                except Exception:
                    retry_safe = False
                    raise
            if overflow_message and overflow_message != text and "overflow" not in completed:
                try:
                    await client.send_message(peer, overflow_message, silent=silent)
                    completed.add("overflow")
                except Exception:
                    retry_safe = False
                    raise
            return

        first = attachments[0]
        first_path = first.get("local_path") or first.get("public_url")
        if not first_path:
            errors.append("MTProto publish requires a local_path or public_url per media item")
            return
        if "media" not in completed:
            try:
                remaining = attachments[1:]
                if len(attachments) == 1:
                    await client.send_file(peer, first_path, caption=caption or None, silent=silent)
                elif all(item in media["images"] for item in attachments):
                    paths = [item.get("local_path") or item.get("public_url") for item in attachments]
                    await client.send_file(peer, [path for path in paths if path], caption=caption or None, silent=silent)
                else:
                    await client.send_file(peer, first_path, caption=caption or None, silent=silent)
                    for item in remaining:
                        item_path = item.get("local_path") or item.get("public_url")
                        if item_path:
                            await client.send_file(peer, item_path, silent=silent)
                completed.add("media")
            except Exception:
                retry_safe = False
                raise
        if overflow_message and "overflow" not in completed:
            try:
                await client.send_message(peer, overflow_message, silent=silent)
                completed.add("overflow")
            except Exception:
                retry_safe = False
                raise

    try:
        client.loop.run_until_complete(_send())
    except PreparedPublishError as exc:
        errors.append(str(exc))
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))

    return {
        "chatId": chat_id,
        "ok": not errors,
        "errors": errors,
        "retrySafe": retry_safe,
        "completedOperations": sorted(completed),
        "mode": "mtproto",
    }


def _publish_telegram_mtproto_sync(account, payload: dict, *, session=None) -> list[Any]:
    """Publish to Telegram chat(s) as the account's own Telegram user.

    Uses Telethon with an api_id/api_hash + StringSession (see config) so the
    post is attributed to the personal account rather than a bot.
    """
    if TelegramClient is None:
        raise PreparedPublishError(
            "Telegram MTProto publish requires the 'telethon' package to be installed"
        )
    config = account.config or {}
    chat_ids = _telegram_resolve_chat_ids(config, payload)
    if not chat_ids:
        raise PreparedPublishError("Telegram MTProto publish requires chatId or chatIds")

    api_id_raw = str(_config_value(config, "apiId") or "").strip()
    api_hash = str(_config_value(config, "apiHash") or "").strip()
    if not api_id_raw:
        raise PreparedPublishError("Telegram MTProto publish requires apiId in config")
    if not api_hash:
        raise PreparedPublishError("Telegram MTProto publish requires apiHash in config")
    try:
        api_id = int(api_id_raw)
    except (TypeError, ValueError) as exc:
        raise PreparedPublishError(f"Telegram MTProto apiId must be an integer, got {api_id_raw!r}") from exc

    session_string = _telegram_mtproto_session_string(config)
    message = _payload_message(payload)
    caption, overflow_message = _telegram_caption_chunks(message)
    silent = bool(config.get("silent", False))
    media = _extract_media(payload)

    delivery_key = str(payload.get("_telegramDeliveryKey") or "default")
    stored_completion = payload.get("telegramCompletedByDelivery")
    if not isinstance(stored_completion, dict):
        stored_completion = {}
    completed_by_chat = stored_completion.get(delivery_key)
    if not isinstance(completed_by_chat, dict):
        completed_by_chat = {}
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    client.start()
    try:
        results: list[dict[str, Any]] = []
        for chat_id in chat_ids:
            completed = completed_by_chat.get(chat_id)
            if not isinstance(completed, list):
                completed = []
            result = _publish_telegram_mtproto_one(
                client,
                chat_id=chat_id,
                message=message,
                caption=caption,
                overflow_message=overflow_message,
                silent=silent,
                media=media,
                completed_operations=set(completed),
            )
            results.append(result)
            completed_by_chat[chat_id] = sorted(set(completed) | set(result.get("completedOperations", [])))
        payload.setdefault("telegramCompletedByDelivery", {})[delivery_key] = completed_by_chat
        failures = [item for item in results if not item["ok"]]
        if failures:
            successful = [item for item in results if item["ok"]]
            retry_safe = all(item.get("retrySafe", True) for item in failures)
            details = {
                "platform": "telegram",
                "partial": bool(successful),
                "retrySafe": retry_safe,
                "chats": [
                    {"chatId": item["chatId"], "ok": item["ok"], "retrySafe": item.get("retrySafe", True), "errors": item["errors"]}
                    for item in results
                ],
            }
            raise PreparedPublishError(
                f"Telegram MTProto delivery failed ({len(successful)} succeeded, {len(failures)} failed)",
                details=details,
                retryable=retry_safe,
            )
        return results
    finally:
        client.disconnect()


def validate_discord_config_live(config: dict[str, Any], *, session=None) -> dict:
    webhook_url = str(_config_value(config, "webhookUrl") or "").strip()
    if not webhook_url:
        raise PreparedPublishError("Discord validation requires webhookUrl or webhookUrlEnv")
    http = _get_session(session)
    response = http.get(webhook_url, timeout=120)
    _raise_for_status(response)
    return _response_payload(response)


def publish_discord_sync(account, payload: dict, *, session=None) -> list[Any]:
    config = account.config or {}
    webhook_url = str(_config_value(config, "webhookUrl") or "").strip()
    if not webhook_url:
        raise PreparedPublishError("Discord publish requires webhookUrl or webhookUrlEnv")

    artifacts = payload.get("artifacts") or []
    media = _extract_media(payload)
    all_media = [*media["videos"], *media["images"]]
    if artifacts and not all_media:
        raise PreparedPublishError("Discord media artifacts were supplied but none is a supported image/video")
    if len(all_media) > 10:
        raise PreparedPublishError("Discord webhook supports at most 10 files per post")

    message = _payload_message(payload) or _message_title(payload)
    content_lines = [message] if message else []
    embeds = []
    files: dict[str, tuple[str, Any]] = {}
    open_files = []
    try:
        for index, item in enumerate(all_media):
            local_path = str(item.get("local_path") or "").strip()
            public_url = str(item.get("public_url") or "").strip()
            if local_path:
                path = Path(local_path)
                if not path.is_file() or path.stat().st_size <= 0:
                    raise PreparedPublishError(f"Discord media file is missing or empty: {path}")
                handle = path.open("rb")
                open_files.append(handle)
                files[f"files[{index}]"] = (path.name, handle)
            elif public_url.lower().startswith(("https://", "http://")):
                if item in media["images"]:
                    embeds.append({"image": {"url": public_url}})
                else:
                    embeds.append({"url": public_url})
            else:
                raise PreparedPublishError("Discord media artifact has no valid public URL or local file")

        payload_json = {"content": "\n".join(content_lines).strip()}
        if embeds:
            payload_json["embeds"] = embeds
        if files:
            response = _get_session(session).post(
                webhook_url,
                data={"payload_json": json.dumps(payload_json, ensure_ascii=False)},
                files=files,
                timeout=600,
            )
        else:
            response = _get_session(session).post(webhook_url, json=payload_json, timeout=120)
        _raise_for_status(response)
        return [_response_payload(response)]
    finally:
        for handle in open_files:
            handle.close()


def validate_facebook_config_live(config: dict[str, Any], *, session=None) -> dict:
    page_id = str(config.get("pageId") or "").strip()
    access_token = str(_config_value(config, "accessToken") or "").strip()
    if not page_id:
        raise PreparedPublishError("Facebook validation requires pageId")
    if not access_token:
        raise PreparedPublishError("Facebook validation requires accessToken or accessTokenEnv")
    http = _get_session(session)
    response = http.get(
        f"{FACEBOOK_GRAPH_ROOT}/{page_id}",
        params={"fields": "id,name", "access_token": access_token},
        timeout=120,
    )
    _raise_for_status(response)
    return _response_payload(response)


def _check_meta_token_not_expired(config: dict[str, Any], platform: str) -> None:
    """Raise early if the Meta user access token is expired."""
    from datetime import datetime, timezone
    expires_at = str(config.get("metaUserAccessTokenExpiresAt") or config.get("accessTokenExpiresAt") or "").strip()
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at)
            now = datetime.now(timezone.utc) if exp.tzinfo else datetime.now()
            if now >= exp:
                raise PreparedPublishError(
                    f"{platform} token expired at {expires_at}. Reconnect the account via OAuth."
                )
        except (ValueError, TypeError):
            pass


def _is_meta_token_stale(config: dict[str, Any], *, skew_seconds: int = 300) -> bool:
    """Return True if the Meta user access token is missing or expires within skew_seconds."""
    expires_at_str = str(config.get("metaUserAccessTokenExpiresAt") or config.get("accessTokenExpiresAt") or "").strip()
    if not expires_at_str:
        return False  # no expiry tracked → assume ok
    expires_at = _parse_iso_datetime(expires_at_str)
    if expires_at is None:
        return False
    return expires_at <= (_utc_now() + timedelta(seconds=skew_seconds))


def _is_youtube_refresh_token_expired(config: dict[str, Any]) -> bool:
    """Return True if the YouTube refresh token itself has expired.

    Google OAuth apps in ``Testing`` publishing status issue refresh tokens that
    expire after 7 days. Apps moved to ``In production`` get non-expiring refresh
    tokens. We capture ``refresh_token_expires_in`` from the initial exchange as
    ``refreshTokenExpiresAt`` so we can detect the cliff and surface a clear
    "reconnect required" instead of letting publishing fail with a confusing
    ``invalid_grant`` error at the next publish.
    """
    expires_at = str(config.get("refreshTokenExpiresAt") or "").strip()
    if not expires_at:
        return False  # non-expiring (production mode) — assume ok
    parsed = _parse_iso_datetime(expires_at)
    if parsed is None:
        return False
    return parsed <= _utc_now()


def _maybe_refresh_meta_token(config: dict[str, Any], platform: str, *, session=None) -> dict[str, Any]:
    """Refresh the Meta user access token for Facebook or Instagram if stale.

    Uses the long-lived token exchange endpoint (fb_exchange_token) which returns
    a new long-lived token valid for 60 days. Stores metaUserAccessToken and
    metaUserAccessTokenExpiresAt in the config for future checks.
    """
    from myUtils import meta_auth as _meta_auth
    meta_user_token = str(config.get("metaUserAccessToken") or "").strip()
    if not meta_user_token:
        return config  # no user token to refresh

    if not _is_meta_token_stale(config):
        return config  # still valid

    try:
        if platform == "instagram":
            refreshed = _meta_auth.refresh_instagram_user_token(access_token=meta_user_token, session=session)
        else:
            refreshed = _meta_auth.exchange_for_long_lived_token(access_token=meta_user_token, session=session)
    except Exception:
        return config  # best-effort: keep using the old token

    if not refreshed or not refreshed.get("access_token"):
        return config

    updated = dict(config)
    updated["metaUserAccessToken"] = refreshed["access_token"]
    expires_in = refreshed.get("expires_in")
    if expires_in not in (None, ""):
        updated["metaUserAccessTokenExpiresAt"] = _token_expiry_from_payload(
            {"expires_in": expires_in}, "expires_in"
        )
    updated["accessTokenUpdatedAt"] = _utc_now().replace(microsecond=0).isoformat()
    return updated


def _rederive_meta_page_token(config: dict[str, Any], platform: str, *, session=None) -> dict[str, Any]:
    """Re-derive the page-level access_token from the current user token.

    Page tokens inherit the lifecycle of the user token that minted them, so
    whenever the user token rotates we must fetch a fresh page token via
    /me/accounts. Updates ``config["accessToken"]`` in place and returns the
    updated dict. Returns the config unchanged on any failure (best-effort).
    """
    from myUtils import meta_auth as _meta_auth

    user_token = str(config.get("metaUserAccessToken") or "").strip()
    if not user_token:
        return config
    try:
        payload = _meta_auth.fetch_managed_pages(access_token=user_token, session=session)
    except Exception:
        return config
    pages = payload.get("data", []) if isinstance(payload, dict) else []
    if not isinstance(pages, list) or not pages:
        return config

    target = None
    if platform == "facebook":
        wanted = str(config.get("pageId") or "").strip()
        if wanted:
            target = next((p for p in pages if str(p.get("id") or "") == wanted), None)
        if target is None:
            target = pages[0]
    else:  # instagram
        wanted_ig = str(config.get("igUserId") or "").strip()
        for page in pages:
            ig = page.get("instagram_business_account") if isinstance(page, dict) else None
            if not isinstance(ig, dict):
                continue
            if wanted_ig and str(ig.get("id") or "") == wanted_ig:
                target = page
                break
            if not wanted_ig and target is None:
                target = page

    if target is None:
        return config

    updated = dict(config)
    page_token = str(target.get("access_token") or "").strip()
    if page_token:
        updated["accessToken"] = page_token
    if platform == "instagram" and target.get("id"):
        updated["pageId"] = str(target["id"])
    return updated


_OAUTH_RETRY_SUBCODES = {460, 463, 467}
_OAUTH_RETRY_CODES = {190}


def _is_recoverable_oauth_error(exc: Exception) -> bool:
    """Return True if a PreparedPublishError looks like a token that can be rotated."""
    text = str(exc) if exc else ""
    if "HTTP 401" not in text and "HTTP 400" not in text:
        return False
    for sub in _OAUTH_RETRY_SUBCODES:
        if f"error_subcode={sub}" in text:
            return True
    for code in _OAUTH_RETRY_CODES:
        if f"code={code}" in text:
            return True
    if "Token has been expired or revoked" in text:
        return True
    if "invalid_grant" in text or "Invalid OAuth" in text:
        return True
    return False


def _maybe_refresh_facebook_token(config: dict[str, Any], *, session=None) -> dict[str, Any]:
    """Refresh Facebook user token if expired or about to expire."""
    return _maybe_refresh_meta_token(config, "facebook", session=session)


def _maybe_refresh_instagram_token(config: dict[str, Any], *, session=None) -> dict[str, Any]:
    """Refresh Instagram user token if expired or about to expire."""
    return _maybe_refresh_meta_token(config, "instagram", session=session)


def publish_facebook_sync(account, payload: dict, *, session=None) -> dict[str, Any]:
    config = dict(account.config or {})
    prior_user_token = str(config.get("metaUserAccessToken") or "").strip()
    prior_page_token = str(config.get("accessToken") or "").strip()
    config = _maybe_refresh_facebook_token(config, session=session)
    # Re-derive page-level access_token when:
    #   (a) the user token rotated, or
    #   (b) the page token is missing/empty (e.g. from a partial OAuth callback).
    # Page tokens inherit the user token's lifecycle, so without this publishing
    # silently breaks ~60 days after connect.
    if (config.get("metaUserAccessToken") != prior_user_token) or not prior_page_token:
        config = _rederive_meta_page_token(config, "facebook", session=session)
    _check_meta_token_not_expired(config, "Facebook")
    page_id = str(config.get("pageId") or "").strip()
    access_token = str(_config_value(config, "accessToken") or "").strip()
    if not page_id:
        raise PreparedPublishError("Facebook publish requires pageId")
    if not access_token:
        raise PreparedPublishError("Facebook publish requires accessToken or accessTokenEnv")

    http = _get_session(session)
    message = _enforce_message_limit(_payload_message(payload), "facebook")
    title = _message_title(payload)
    artifacts = payload.get("artifacts") or []
    media = _extract_media(payload)
    if artifacts and not (media["videos"] or media["images"]):
        raise PreparedPublishError("Facebook media artifacts were supplied but none is a supported image/video")
    if len(media["videos"]) > 1:
        raise PreparedPublishError("Facebook publishing supports one video per post")
    if media["videos"]:
        _enforce_video_limits(str(media["videos"][0].get("local_path") or ""), "facebook")
    if len(media["images"]) > 10:
        raise PreparedPublishError("Facebook photo attachments support at most ten images per post")

    def _do_post():
        results = []
        def _post(edge: str, *, data: dict, files=None, timeout=120):
            response = http.post(
                f"{FACEBOOK_GRAPH_ROOT}/{page_id}/{edge}",
                data=data,
                files=files,
                timeout=timeout,
            )
            _raise_for_status(response)
            body = _response_payload(response)
            results.append(body)
            return body

        if media["videos"]:
            item = media["videos"][0]
            data = {"access_token": access_token, "description": message, "title": title}
            local_path = item.get("local_path")
            public_url = item.get("public_url")
            if public_url:
                data["file_url"] = public_url
                _post("videos", data=data, timeout=600)
            elif local_path:
                with Path(local_path).open("rb") as handle:
                    _post("videos", data=data, files={"source": (Path(local_path).name, handle)}, timeout=1800)
            else:
                raise PreparedPublishError("Facebook video publish requires a public_url or local_path")
            return results

        if len(media["images"]) > 1:
            attached_media = []
            for item in media["images"][:10]:
                data = {"access_token": access_token, "published": "false"}
                local_path = item.get("local_path")
                public_url = item.get("public_url")
                if public_url:
                    data["url"] = public_url
                    body = _post("photos", data=data)
                elif local_path:
                    with Path(local_path).open("rb") as handle:
                        body = _post("photos", data=data, files={"source": (Path(local_path).name, handle)}, timeout=600)
                else:
                    raise PreparedPublishError("Facebook image publish requires a public_url or local_path")
                media_id = body.get("id")
                if not media_id:
                    raise PreparedPublishError("Facebook photo upload did not return an id")
                attached_media.append({"media_fbid": media_id})

            _post(
                "feed",
                data={
                    "access_token": access_token,
                    "message": message,
                    "attached_media": json.dumps(attached_media, ensure_ascii=False),
                },
            )
            return results

        if media["images"]:
            item = media["images"][0]
            data = {"access_token": access_token, "caption": message}
            local_path = item.get("local_path")
            public_url = item.get("public_url")
            if public_url:
                data["url"] = public_url
                _post("photos", data=data)
            elif local_path:
                with Path(local_path).open("rb") as handle:
                    _post("photos", data=data, files={"source": (Path(local_path).name, handle)}, timeout=600)
            else:
                raise PreparedPublishError("Facebook image publish requires a public_url or local_path")
            return results

        _post("feed", data={"access_token": access_token, "message": message})
        first_comment = _first_comment_text(payload)
        if first_comment:
            last = results[-1] if results else {}
            post_id = last.get("post_id") or last.get("id")

            def _poster(pid, text):
                response = http.post(
                    f"{FACEBOOK_GRAPH_ROOT}/{pid}/comments",
                    data={"access_token": access_token, "message": text},
                    timeout=60,
                )
                _raise_for_status(response)
                results.append(_response_payload(response))
            _try_post_first_comment(platform="facebook", post_id=post_id, text=first_comment, poster=_poster)
        return results

    try:
        results = _do_post()
    except PreparedPublishError as exc:
        if not _is_recoverable_oauth_error(exc):
            raise
        # Single retry: force a user-token + page-token rotation then try again.
        config = _maybe_refresh_facebook_token(config, session=session)
        config = _rederive_meta_page_token(config, "facebook", session=session)
        access_token = str(config.get("accessToken") or access_token).strip()
        results = _do_post()

    return {"results": results, "updated_config": config}


def _instagram_create_container(http, ig_user_id: str, access_token: str, data: dict) -> str:
    response = http.post(
        f"{FACEBOOK_GRAPH_ROOT}/{ig_user_id}/media",
        data={**data, "access_token": access_token},
        timeout=120,
    )
    _raise_for_status(response)
    payload = _response_payload(response)
    container_id = payload.get("id")
    if not container_id:
        raise PreparedPublishError("Instagram media creation did not return an id")
    return str(container_id)


def _wait_for_container_status(
    http,
    container_id: str,
    access_token: str,
    *,
    platform: str,
    timeout: float = 180.0,
    interval: float = 2.0,
) -> None:
    """Poll a created media container until it is FINISHED (or PUBLISHED).

    Meta (Instagram) and Threads video / carousel containers take a few
    seconds to fetch and transcode the remote media URL. Publishing before
    the container reaches ``FINISHED`` fails with Instagram error 9007
    "Media ID is not available" or Threads error code 24 ("resource does
    not exist"). This helper polls the container status — Instagram exposes
    it as ``status_code``, Threads as ``status`` — and surfaces the
    container's ``error_message`` when it lands in an ERROR state.

    The wait is generous on purpose: Meta transcoding routinely exceeded the old
    90s ceiling, so threads and instagram runs failed with "not ready after 90s"
    and only succeeded on a retry - or not at all. A slow container is normal
    here, and waiting costs far less than a dead target.
    """
    root, field, requested_fields = _container_status_field(platform)
    deadline = time.monotonic() + timeout
    last_status = "UNKNOWN"
    last_body: dict[str, Any] = {}
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # Bound each request so a slow/hung GET cannot push the total wait past
        # the caller's deadline.
        response = http.get(
            f"{root}/{container_id}",
            params={"fields": requested_fields, "access_token": access_token},
            timeout=min(30.0, max(1.0, remaining)),
        )
        _raise_for_status(response)
        body = _response_payload(response) or {}
        last_body = body
        status = str(body.get(field) or body.get("status") or "").upper()
        last_status = status
        if status in ("FINISHED", "PUBLISHED"):
            return
        if status in ("ERROR", "EXPIRED", "FAILED"):
            # Meta only returns a field when it is requested, so the poll asks
            # for ``error_message`` explicitly (see _container_status_field).
            # Prefer it, then Instagram's human-readable ``status``; ignore the
            # bare status enum so we never raise "failed to process: ERROR".
            detail = str(body.get("error_message") or "").strip()
            if not detail:
                raw_status = str(body.get("status") or "").strip()
                if raw_status.upper() not in ("", "ERROR", "EXPIRED", "FAILED", "UNKNOWN"):
                    detail = raw_status
            if detail.upper() in ("", "UNKNOWN"):
                detail = (
                    f"no diagnostic detail from Meta (container status {status}); "
                    "check the media against the platform's duration/size limits"
                )
            raise PreparedPublishError(
                f"{platform.title()} container {container_id} failed to process: {detail}"
            )
        # Never sleep past the deadline; cap the nap to the time remaining.
        sleep_for = min(interval, max(0.0, deadline - time.monotonic()))
        if sleep_for <= 0:
            break
        time.sleep(sleep_for)
    detail = last_body.get("error_message") or last_status
    raise PreparedPublishError(
        f"{platform.title()} container {container_id} not ready after {int(timeout)}s "
        f"(last status {last_status})"
    )


def _container_status_field(platform: str) -> tuple[str, str, str]:
    """Status endpoint root, status field name, and fields to request.

    The requested-fields string matters: Meta only returns a field when it is
    asked for. The previous code requested just the status enum, so Threads'
    ``error_message`` was never present in the response and a failed container
    surfaced as a bare "ERROR" or an empty string - swallowing Meta's real
    reason. Instagram's diagnostic lives in the human-readable ``status``
    field, so request that alongside ``status_code``.
    """
    if platform == "instagram":
        return FACEBOOK_GRAPH_ROOT, "status_code", "status_code,status"
    return THREADS_GRAPH_ROOT, "status", "status,error_message"


def validate_instagram_config_live(config: dict[str, Any], *, session=None) -> dict:
    ig_user_id = str(config.get("igUserId") or "").strip()
    access_token = str(_config_value(config, "accessToken") or "").strip()
    if not ig_user_id:
        raise PreparedPublishError("Instagram validation requires igUserId")
    if not access_token:
        raise PreparedPublishError("Instagram validation requires accessToken or accessTokenEnv")
    http = _get_session(session)
    response = http.get(
        f"{FACEBOOK_GRAPH_ROOT}/{ig_user_id}",
        params={"fields": "id,username", "access_token": access_token},
        timeout=120,
    )
    _raise_for_status(response)
    return _response_payload(response)


def publish_instagram_sync(account, payload: dict, *, session=None) -> dict:
    config = dict(account.config or {})
    prior_user_token = str(config.get("metaUserAccessToken") or "").strip()
    prior_page_token = str(config.get("accessToken") or "").strip()
    config = _maybe_refresh_instagram_token(config, session=session)
    if (config.get("metaUserAccessToken") != prior_user_token) or not prior_page_token:
        config = _rederive_meta_page_token(config, "instagram", session=session)
    _check_meta_token_not_expired(config, "Instagram")
    ig_user_id = str(config.get("igUserId") or "").strip()
    access_token = str(_config_value(config, "accessToken") or "").strip()
    if not ig_user_id:
        raise PreparedPublishError("Instagram publish requires igUserId")
    if not access_token:
        raise PreparedPublishError("Instagram publish requires accessToken or accessTokenEnv")

    http = _get_session(session)
    message = _enforce_message_limit(_payload_message(payload), "instagram")
    artifacts = payload.get("artifacts") or []
    media = _extract_media(payload)
    if artifacts and not (media["videos"] or media["images"]):
        raise PreparedPublishError("Instagram media artifacts were supplied but none is a supported image/video")
    if len(media["videos"]) > 1:
        raise PreparedPublishError("Instagram publish accepts one video per post")
    if len(media["images"]) > 10:
        raise PreparedPublishError("Instagram carousel accepts at most ten images")
    if media["videos"]:
        _enforce_video_limits(str(media["videos"][0].get("local_path") or ""), "instagram")

    def _do_post():
        if media["videos"]:
            public_url = media["videos"][0].get("public_url") or ""
            if not public_url:
                raise PreparedPublishError("Instagram video publish requires a public_url")
            container_id = _instagram_create_container(
                http,
                ig_user_id,
                access_token,
                {"media_type": "REELS", "video_url": public_url, "caption": message},
            )
        elif len(media["images"]) > 1:
            child_ids = []
            for item in media["images"][:10]:
                public_url = item.get("public_url") or ""
                if not public_url:
                    raise PreparedPublishError("Instagram carousel publish requires public image URLs")
                child_ids.append(
                    _instagram_create_container(
                        http,
                        ig_user_id,
                        access_token,
                        {"image_url": public_url, "is_carousel_item": "true"},
                    )
                )
            container_id = _instagram_create_container(
                http,
                ig_user_id,
                access_token,
                {"media_type": "CAROUSEL", "children": ",".join(child_ids), "caption": message},
            )
        elif media["images"]:
            public_url = media["images"][0].get("public_url") or ""
            if not public_url:
                raise PreparedPublishError("Instagram image publish requires a public_url")
            container_id = _instagram_create_container(
                http,
                ig_user_id,
                access_token,
                {"image_url": public_url, "caption": message},
            )
        else:
            raise PreparedPublishError("Instagram publish requires at least one image or video")

        _wait_for_container_status(http, container_id, access_token, platform="instagram")
        publish_response = http.post(
            f"{FACEBOOK_GRAPH_ROOT}/{ig_user_id}/media_publish",
            data={"creation_id": container_id, "access_token": access_token},
            timeout=120,
        )
        _raise_for_status(publish_response)
        return {"container_id": container_id, "publish": _response_payload(publish_response)}

    try:
        post_result = _do_post()
    except PreparedPublishError as exc:
        if not _is_recoverable_oauth_error(exc):
            raise
        config = _maybe_refresh_instagram_token(config, session=session)
        config = _rederive_meta_page_token(config, "instagram", session=session)
        access_token = str(config.get("accessToken") or access_token).strip()
        post_result = _do_post()

    publish_body = post_result.get("publish", {}) if isinstance(post_result, dict) else {}
    first_comment = _first_comment_text(payload)
    if first_comment:
        media_id = publish_body.get("id") if isinstance(publish_body, dict) else None

        def _poster(pid, text):
            response = http.post(
                f"{FACEBOOK_GRAPH_ROOT}/{pid}/comments",
                data={"message": text, "access_token": access_token},
                timeout=60,
            )
            _raise_for_status(response)
        _try_post_first_comment(platform="instagram", post_id=media_id, text=first_comment, poster=_poster)
    return {"container_id": post_result.get("container_id", ""), "publish": publish_body, "updated_config": config}


def _threads_create_container(http, user_id: str, access_token: str, data: dict) -> str:
    response = http.post(
        f"{THREADS_GRAPH_ROOT}/{user_id}/threads",
        data={**data, "access_token": access_token},
        timeout=120,
    )
    _raise_for_status(response)
    payload = _response_payload(response)
    container_id = payload.get("id")
    if not container_id:
        raise PreparedPublishError("Threads media creation did not return an id")
    return str(container_id)


def _validate_threads_video_artifact(item: dict[str, Any]) -> None:
    """Fail fast when a Threads video obviously exceeds Meta's limits.

    Threads caps video posts at 5 minutes / 1 GB. Meta accepts a container for
    an over-limit video and only later flips it to ``ERROR`` with an opaque
    ``error_message: UNKNOWN``, burning a retry cycle for every attempt. When
    the artifact has a locally visible path we can probe it up front and raise
    an actionable error instead. Probing is best-effort: a remote-only artifact
    or an ffprobe failure must never block a publish that Meta might accept.
    """
    local_path = str(item.get("local_path") or "").strip()
    probe = local_path or str(item.get("public_url") or "")
    suffix = Path(probe).suffix.lower()
    if suffix and suffix not in THREADS_ALLOWED_VIDEO_SUFFIXES:
        raise PreparedPublishError(
            f"Threads video publish supports MP4/MOV (got {suffix})"
        )
    if not local_path:
        return
    source = Path(local_path).expanduser()
    if not source.is_file():
        return
    file_size = source.stat().st_size
    if file_size > THREADS_MAX_VIDEO_BYTES:
        raise PreparedPublishError(
            f"Threads video size {file_size / 1_000_000:.0f} MB exceeds the "
            f"{platform_limits.media_max_mb('threads')} MB limit"
        )
    try:
        duration_seconds = media_pipeline.probe_video_duration(source)
    except Exception as exc:  # noqa: BLE001 - probing must not block publishing
        logger.warning("Could not probe Threads video duration for %s: %s", source, exc)
        return
    if duration_seconds > THREADS_MAX_VIDEO_SECONDS:
        raise PreparedPublishError(
            f"Threads video duration {duration_seconds:.0f}s exceeds the "
            f"{int(THREADS_MAX_VIDEO_SECONDS)}s limit; re-encode a shorter cut "
            "before publishing (Instagram Reels allow up to 15 minutes, which is "
            "why the same file may publish there)"
        )


def validate_threads_config_live(config: dict[str, Any], *, session=None) -> dict:
    user_id = str(config.get("threadUserId") or config.get("userId") or "").strip()
    access_token = str(_config_value(config, "accessToken") or "").strip()
    if not user_id:
        raise PreparedPublishError("Threads validation requires threadUserId")
    if not access_token:
        raise PreparedPublishError("Threads validation requires accessToken or accessTokenEnv")
    http = _get_session(session)
    response = http.get(
        f"{THREADS_GRAPH_ROOT}/{user_id}",
        params={"fields": "id,username", "access_token": access_token},
        timeout=120,
    )
    _raise_for_status(response)
    return _response_payload(response)


def _maybe_refresh_threads_token(config: dict[str, Any], *, session=None) -> dict[str, Any]:
    """Refresh Threads long-lived token if expired or about to expire."""
    from myUtils import threads_auth as _threads_auth
    # Resolve through _config_value so an env-backed ``accessTokenEnv`` account
    # is refreshed too, not silently skipped because the literal key is empty.
    access_token = str(_config_value(config, "accessToken") or "").strip()
    if not access_token:
        return config
    expires_at = str(config.get("accessTokenExpiresAt") or "").strip()
    if expires_at:
        from datetime import datetime, timedelta, timezone
        try:
            exp = datetime.fromisoformat(expires_at)
            now = datetime.now(timezone.utc) if exp.tzinfo else datetime.now()
            if now < exp - timedelta(seconds=300):
                return config
        except (ValueError, TypeError):
            pass
    try:
        refreshed = _threads_auth.refresh_long_lived_token(access_token=access_token, session=session)
    except Exception:
        return config
    if not refreshed or not refreshed.get("access_token"):
        return config
    updated = dict(config)
    updated["accessToken"] = refreshed["access_token"]
    expires_in = refreshed.get("expires_in")
    if expires_in not in (None, ""):
        updated["accessTokenExpiresAt"] = _token_expiry_from_payload(
            {"expires_in": expires_in}, "expires_in"
        )
    updated["accessTokenUpdatedAt"] = _utc_now().replace(microsecond=0).isoformat()
    return updated


def publish_threads_sync(account, payload: dict, *, session=None) -> dict:
    config = dict(account.config or {})
    config = _maybe_refresh_threads_token(config, session=session)
    user_id = str(config.get("threadUserId") or config.get("userId") or "").strip()
    access_token = str(_config_value(config, "accessToken") or "").strip()
    if not user_id:
        raise PreparedPublishError("Threads publish requires threadUserId")
    if not access_token:
        raise PreparedPublishError("Threads publish requires accessToken or accessTokenEnv")

    http = _get_session(session)
    message = _payload_message(payload)
    # Threads caps post text at 500 characters and returns
    # `HTTP 500: Param text must be at most 500 characters long` when
    # exceeded. Truncate to the platform limit (mirrors the TikTok caption
    # truncation) so a long auto-generated caption fails locally before the
    # API call and the post still goes out.
    if len(message) > THREADS_MAX_TEXT_CHARS:
        logger.warning(
            "Threads text exceeds %d characters; truncating %d -> %d",
            THREADS_MAX_TEXT_CHARS, len(message), THREADS_MAX_TEXT_CHARS,
        )
        message = message[:THREADS_MAX_TEXT_CHARS]
    artifacts = payload.get("artifacts") or []
    media = _extract_media(payload)
    if artifacts and not (media["videos"] or media["images"]):
        raise PreparedPublishError("Threads media artifacts were supplied but none is a supported image/video")
    if len(media["videos"]) > 1:
        raise PreparedPublishError("Threads accepts one video per post")
    if len(media["images"]) > 10:
        raise PreparedPublishError("Threads carousel accepts at most ten images")

    if media["videos"]:
        public_url = media["videos"][0].get("public_url") or ""
        if not public_url:
            raise PreparedPublishError("Threads video publish requires a public_url")
        _validate_threads_video_artifact(media["videos"][0])
        container_id = _threads_create_container(
            http,
            user_id,
            access_token,
            {"media_type": "VIDEO", "video_url": public_url, "text": message},
        )
    elif len(media["images"]) > 1:
        child_ids = []
        for item in media["images"][:10]:
            public_url = item.get("public_url") or ""
            if not public_url:
                raise PreparedPublishError("Threads carousel publish requires public image URLs")
            child_ids.append(
                _threads_create_container(
                    http,
                    user_id,
                    access_token,
                    {"media_type": "IMAGE", "image_url": public_url, "is_carousel_item": "true"},
                )
            )
        container_id = _threads_create_container(
            http,
            user_id,
            access_token,
            {"media_type": "CAROUSEL", "children": ",".join(child_ids), "text": message},
        )
    elif media["images"]:
        public_url = media["images"][0].get("public_url") or ""
        if not public_url:
            raise PreparedPublishError("Threads image publish requires a public_url")
        container_id = _threads_create_container(
            http,
            user_id,
            access_token,
            {"media_type": "IMAGE", "image_url": public_url, "text": message},
        )
    else:
        container_id = _threads_create_container(
            http,
            user_id,
            access_token,
            {"media_type": "TEXT", "text": message},
        )

    # Text-only posts publish instantly; media (video/image/carousel)
    # containers need a moment to fetch/transcode the remote URL.
    if media["videos"] or media["images"]:
        _wait_for_container_status(http, container_id, access_token, platform="threads")

    publish_response = http.post(
        f"{THREADS_GRAPH_ROOT}/{user_id}/threads_publish",
        data={"creation_id": container_id, "access_token": access_token},
        timeout=120,
    )
    _raise_for_status(publish_response)
    return {"container_id": container_id, "publish": _response_payload(publish_response), "updated_config": config}


def validate_reddit_config_live(config: dict[str, Any], *, session=None) -> dict:
    http = _get_session(session)
    access_token = _reddit_access_token(config, session=http)
    user_agent = str(
        _config_value(config, "userAgent")
        or f"social-auto-upload/0.1 ({config.get('accountName', 'sau')})"
    ).strip()
    response = http.get(
        REDDIT_ME_URL,
        headers={"Authorization": f"Bearer {access_token}", "User-Agent": user_agent},
        timeout=120,
    )
    _raise_for_status(response)
    return {"access_token": access_token, "me": _response_payload(response)}


def validate_youtube_config_live(config: dict[str, Any], *, session=None) -> dict:
    channel_id = str(config.get("channelId") or "").strip()
    if not channel_id:
        raise PreparedPublishError("YouTube validation requires channelId")
    http = _get_session(session)
    access_token = _google_access_token(config, session=http)
    response = http.get(
        YOUTUBE_CHANNELS_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"part": "id,snippet", "id": channel_id},
        timeout=120,
    )
    _raise_for_status(response)
    return {"access_token": access_token, "channel": _response_payload(response)}


def query_tiktok_creator_info(config: dict[str, Any], *, access_token: str | None = None, session=None) -> dict:
    resolved_access_token = str(access_token or _config_value(config, "accessToken") or "").strip()
    if not resolved_access_token:
        raise PreparedPublishError("TikTok creator info query requires accessToken or accessTokenEnv")
    http = _get_session(session)
    response = http.post(
        TIKTOK_CREATOR_INFO_URL,
        headers={
            "Authorization": f"Bearer {resolved_access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json={},
        timeout=120,
    )
    _raise_for_status(response)
    # TikTok can return HTTP 200 with a business-level error object.
    _raise_tiktok_error(response)
    payload = _response_payload(response)
    return payload


def _validate_tiktok_video_artifact(item: dict[str, Any], *, message: str, config: dict[str, Any]) -> None:
    if len(message) > TIKTOK_MAX_CAPTION_CHARS:
        raise PreparedPublishError(f"TikTok caption exceeds {TIKTOK_MAX_CAPTION_CHARS} characters for direct publishing")

    public_url = str(item.get("public_url") or "").strip()
    local_path = str(item.get("local_path") or "").strip()

    if not public_url and not local_path:
        raise PreparedPublishError("TikTok video publish requires either a public_url or a local_path")

    if local_path:
        source = Path(local_path).expanduser().resolve()
        if not source.exists():
            raise PreparedPublishError(f"TikTok video artifact not found: {source}")

        suffix = source.suffix.lower()
        if suffix not in TIKTOK_ALLOWED_VIDEO_SUFFIXES:
            raise PreparedPublishError("TikTok video publish currently supports only MP4 or WebM artifacts")

        file_size = source.stat().st_size
        if file_size > TIKTOK_MAX_PULL_FROM_URL_BYTES:
            raise PreparedPublishError(
                "TikTok video uploads support up to "
                f"{platform_limits.media_max_mb('tiktok')} MB"
            )

        duration_seconds = media_pipeline.probe_video_duration(source)
        if duration_seconds < TIKTOK_MIN_VIDEO_SECONDS:
            raise PreparedPublishError(f"TikTok videos must be at least {TIKTOK_MIN_VIDEO_SECONDS} seconds")
        # Check against creator_info's max_video_post_duration_sec if available,
        # otherwise fall back to the hardcoded maximum.
        creator_max = config.get("_tiktok_max_video_duration_sec")
        max_sec = creator_max if creator_max and creator_max > 0 else TIKTOK_MAX_VIDEO_SECONDS
        if duration_seconds > max_sec:
            raise PreparedPublishError(f"TikTok video duration ({duration_seconds:.0f}s) exceeds the limit ({max_sec:.0f}s)")

        cover_timestamp_raw = config.get("videoCoverTimestampMs")
        if cover_timestamp_raw not in (None, ""):
            cover_timestamp_ms = int(cover_timestamp_raw)
            if cover_timestamp_ms < 0:
                raise PreparedPublishError("TikTok video_cover_timestamp_ms must be >= 0")
            if cover_timestamp_ms > int(duration_seconds * 1000):
                raise PreparedPublishError("TikTok video_cover_timestamp_ms must not exceed the video duration")


def _validate_tiktok_photo_payload(public_urls: list[str], *, message: str) -> None:
    if len(message) > TIKTOK_MAX_CAPTION_CHARS:
        raise PreparedPublishError(f"TikTok caption exceeds {TIKTOK_MAX_CAPTION_CHARS} characters for direct publishing")
    if len(public_urls) > 35:
        raise PreparedPublishError("TikTok photo publish supports up to 35 images")

TIKTOK_FILE_UPLOAD_CHUNK_SIZE = 5 * 1024 * 1024  # 5 MB minimum per TikTok docs
TIKTOK_FILE_UPLOAD_MAX_CHUNK_SIZE = 64 * 1024 * 1024  # 64 MB maximum per TikTok docs


def _tiktok_chunk_plan(file_size: int) -> tuple[int, int]:
    """Return ``(chunk_size, total_chunk_count)`` for a TikTok FILE_UPLOAD.

    TikTok computes ``total_chunk_count = floor(video_size / chunk_size)`` and
    merges any trailing bytes into the final chunk (which may exceed
    ``chunk_size``, up to 128 MB). Using ``ceil`` here declares one chunk too
    many, so the init call is rejected with
    ``invalid_params: The total chunk count is invalid``. A file of at most
    64 MB fits in a single chunk; a larger file must use multiple chunks, so the
    chunk size is capped at half the file size to keep ``total_chunk_count >= 2``.
    """
    if file_size <= 0:
        raise PreparedPublishError(
            "TikTok FILE_UPLOAD requires a non-empty video", retryable=False
        )
    if file_size <= TIKTOK_FILE_UPLOAD_MAX_CHUNK_SIZE:
        return file_size, 1
    chunk_size = min(TIKTOK_FILE_UPLOAD_MAX_CHUNK_SIZE, file_size // 2)
    return chunk_size, file_size // chunk_size


def _tiktok_file_upload(
    http,
    video_path: Path,
    upload_url: str,
    chunk_size: int,
    total_chunks: int,
) -> None:
    """Upload a video file to TikTok via chunked PUT to the presigned upload_url.

    The first ``total_chunks - 1`` requests carry exactly ``chunk_size`` bytes;
    the final request carries every remaining byte. This mirrors TikTok's
    documented layout, where the trailing remainder is merged into the last
    chunk rather than sent as its own (sub-5 MB) chunk.
    """
    import mimetypes

    total_size = video_path.stat().st_size
    mime_type = mimetypes.guess_type(video_path.name)[0] or "video/mp4"

    with video_path.open("rb") as fh:
        offset = 0
        for index in range(total_chunks):
            if index == total_chunks - 1:
                chunk = fh.read()  # final chunk absorbs the trailing bytes
            else:
                chunk = fh.read(chunk_size)
            if not chunk:
                break
            chunk_len = len(chunk)
            start = offset
            end = offset + chunk_len - 1
            headers = {
                "Content-Type": mime_type,
                "Content-Length": str(chunk_len),
                "Content-Range": f"bytes {start}-{end}/{total_size}",
            }
            for attempt in range(3):
                resp = http.put(upload_url, headers=headers, data=chunk, timeout=300)
                if resp.status_code in (200, 201, 206):
                    break
                if resp.status_code >= 500 and attempt < 2:
                    import time
                    time.sleep(2 ** attempt)
                    continue
                _raise_for_status(resp)
            else:
                _raise_for_status(resp)
            offset += chunk_len


def publish_tiktok_sync(account, payload: dict, *, session=None) -> dict:
    config = dict(account.config or {})
    artifacts = payload.get("artifacts") or []
    media = _extract_media(payload)
    payload_direct_post = payload.get("tiktokDirectPost")
    if isinstance(payload_direct_post, bool):
        publish_mode = "direct" if payload_direct_post else "draft"
    else:
        publish_mode = str(config.get("publishMode") or "direct").strip().lower()
    if media["images"] and not media["videos"] and publish_mode != "direct":
        raise PreparedPublishError(
            "TikTok photo posts require Direct Post; change the per-post mode "
            "or remove this photo destination.",
            retryable=False,
        )
    http = _get_session(session)
    access_token, updated_config = _ensure_tiktok_access_token(config, session=http)
    if artifacts and not (media["videos"] or media["images"]):
        raise PreparedPublishError("TikTok media artifacts were supplied but none is a supported image/video")
    if len(media["videos"]) > 1:
        raise PreparedPublishError("TikTok supports one video per post")
    if len(media["images"]) > 35:
        raise PreparedPublishError("TikTok photo posts support at most 35 images")
    creator_info = query_tiktok_creator_info(config, access_token=access_token, session=http)
    # Store creator_info's max duration for video validation
    ci_data = creator_info.get("data") or creator_info
    max_dur = ci_data.get("max_video_post_duration_sec")
    if max_dur:
        config["_tiktok_max_video_duration_sec"] = int(max_dur)
    if updated_config is not None:
        updated_config = _apply_tiktok_token_payload(updated_config, {'access_token': access_token}, creator_info)
    message = _payload_message(payload)
    # Per-publish override has precedence over the account-level default.
    post_mode = "DIRECT_POST" if publish_mode == "direct" else "MEDIA_UPLOAD"
    # Per-post TikTok settings from the frontend override account-level config.
    # This is required for TikTok audit compliance: privacy, interactions, and
    # content disclosure must be chosen per-post by the user.
    tt_settings = payload.get("tiktokPostSettings") or {}
    privacy_level = str(
        tt_settings.get("privacyLevel")
        or config.get("privacyLevel")
        or "SELF_ONLY"
    ).strip().upper()

    # Sandbox/development apps (client_key starts with "sb") can ONLY use SELF_ONLY
    client_key = os.environ.get("TIKTOK_CLIENT_KEY", "")
    if client_key.startswith("sb") and privacy_level != "SELF_ONLY":
        privacy_level = "SELF_ONLY"

    if media["videos"]:
        video_item = media["videos"][0]
        _validate_tiktok_video_artifact(video_item, message=message, config=config)
        public_url = video_item.get("public_url") or ""
        local_path = str(video_item.get("local_path") or "").strip()

        # Use reviewed title from TikTokPostSettings if present, otherwise fall back to message
        reviewed_title = str(tt_settings.get("title") or "").strip()
        post_title = reviewed_title[:TIKTOK_MAX_CAPTION_CHARS] if reviewed_title else message[:TIKTOK_MAX_CAPTION_CHARS]

        post_info = {
            "title": post_title,
            "privacy_level": privacy_level,
            "disable_duet": bool(tt_settings.get("disableDuet", config.get("disableDuet", False))),
            "disable_comment": bool(tt_settings.get("disableComment", config.get("disableComment", False))),
            "disable_stitch": bool(tt_settings.get("disableStitch", config.get("disableStitch", False))),
        }
        if config.get("videoCoverTimestampMs") not in (None, ""):
            post_info["video_cover_timestamp_ms"] = int(config["videoCoverTimestampMs"])

        # Commercial content disclosure (TikTok audit compliance).
        # "Branded Content" takes precedence when both are selected.
        content_disclosure = tt_settings.get("contentDisclosure")
        if isinstance(content_disclosure, dict) and content_disclosure.get("enabled"):
            post_info["brand_content_toggle"] = bool(content_disclosure.get("brandedContent"))
            post_info["brand_organic_toggle"] = bool(content_disclosure.get("yourBrand"))

        # Prefer PULL_FROM_URL when we have a public URL — avoids chunk-size
        # issues with small files and is more reliable than FILE_UPLOAD.
        # Fall back to FILE_UPLOAD if domain ownership is not verified.
        pull_from_url_attempted = False
        if public_url:
            pull_from_url_attempted = True
            request_body = {
                "post_info": post_info,
                "source_info": {
                    "source": "PULL_FROM_URL",
                    "video_url": public_url,
                },
                "post_mode": post_mode,
            }
            response = http.post(
                TIKTOK_VIDEO_INIT_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=request_body,
                timeout=120,
            )
            if response.status_code == 200:
                _raise_tiktok_error(response)
                return {
                    "creator_info": creator_info,
                    "publish": _response_payload(response),
                    "request": request_body,
                    "updated_config": updated_config,
                    "access_token": access_token,
                }
            # Check if it's a domain verification error — fall through to FILE_UPLOAD
            try:
                resp_json = response.json()
                error_obj = resp_json.get("error", {}) or {}
                error_code = str(error_obj.get("code") or "").strip()
                logger.info("TikTok PULL_FROM_URL error: code=%s, status=%s, body=%s",
                         error_code, response.status_code, resp_json)
                if error_code == "url_ownership_unverified":
                    logger.warning("TikTok PULL_FROM_URL failed (domain not verified), falling back to FILE_UPLOAD")
                else:
                    _raise_tiktok_error(response)
            except PreparedPublishError:
                raise
            except Exception as e:
                logger.warning("Failed to parse TikTok error response: %s", e)
                _raise_tiktok_error(response)

        if local_path and Path(local_path).expanduser().resolve().is_file():
            # FILE_UPLOAD for local files (non-Direct-Post or no public URL)
            video_path = Path(local_path).expanduser().resolve()
            file_size = video_path.stat().st_size
            # TikTok declares chunk_size in [5MB, 64MB] and computes
            # total_chunk_count = floor(video_size / chunk_size), merging the
            # trailing remainder into the final chunk (up to 128 MB).
            chunk_size, total_chunks = _tiktok_chunk_plan(file_size)
            request_body = {
                "post_info": post_info,
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": file_size,
                    "chunk_size": chunk_size,
                    "total_chunk_count": total_chunks,
                },
                "post_mode": post_mode,
            }
            response = http.post(
                TIKTOK_VIDEO_INIT_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=request_body,
                timeout=120,
            )
            if response.status_code != 200:
                _raise_tiktok_error(response)
            resp_data = response.json()
            resp_data_inner = resp_data.get("data") or resp_data
            upload_url = resp_data_inner.get("upload_url") or ""
            if not upload_url:
                raise PreparedPublishError("TikTok FILE_UPLOAD init did not return an upload_url")
            _tiktok_file_upload(http, video_path, upload_url, chunk_size, total_chunks)
            return {
                "creator_info": creator_info,
                "publish": resp_data,
                "request": request_body,
                "updated_config": updated_config,
                "access_token": access_token,
            }
        else:
            # Fallback to PULL_FROM_URL when we only have a public URL
            request_body = {
                "post_info": post_info,
                "source_info": {
                    "source": "PULL_FROM_URL",
                    "video_url": public_url,
                },
                "post_mode": post_mode,
            }
            response = http.post(
                TIKTOK_VIDEO_INIT_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=request_body,
                timeout=120,
            )
            if response.status_code != 200:
                _raise_tiktok_error(response)
            return {
                "creator_info": creator_info,
                "publish": _response_payload(response),
                "request": request_body,
                "updated_config": updated_config,
                "access_token": access_token,
            }

    if media["images"]:
        if post_mode != "DIRECT_POST":
            raise PreparedPublishError(
                "TikTok photo posts require Direct Post; change the per-post mode "
                "or remove this photo destination.",
                retryable=False,
            )
        public_urls = [item.get("public_url") or "" for item in media["images"][:35]]
        if not all(public_urls):
            raise PreparedPublishError("TikTok photo publish requires public image URLs")
        # Validate all URLs against verified prefixes for Direct Post
        if post_mode == "DIRECT_POST":
            for url in public_urls:
                _tiktok_validate_pull_from_url(url)
        _validate_tiktok_photo_payload(public_urls, message=message)
        # Use reviewed title from TikTokPostSettings if present
        reviewed_title = str(tt_settings.get("title") or "").strip()
        photo_title = reviewed_title if reviewed_title else _message_title(payload)

        photo_post_info = {
            "title": photo_title[:TIKTOK_MAX_CAPTION_CHARS],
            "description": message[:TIKTOK_MAX_CAPTION_CHARS],
            "privacy_level": privacy_level,
            "disable_comment": bool(tt_settings.get("disableComment", config.get("disableComment", False))),
            "disable_duet": bool(tt_settings.get("disableDuet", config.get("disableDuet", False))),
            "disable_stitch": bool(tt_settings.get("disableStitch", config.get("disableStitch", False))),
            "auto_add_music": bool(config.get("autoAddMusic", True)),
        }
        # Commercial content disclosure for photo posts (TikTok audit compliance).
        content_disclosure = tt_settings.get("contentDisclosure")
        if isinstance(content_disclosure, dict) and content_disclosure.get("enabled"):
            photo_post_info["brand_content_toggle"] = bool(content_disclosure.get("brandedContent"))
            photo_post_info["brand_organic_toggle"] = bool(content_disclosure.get("yourBrand"))
        request_body = {
            "post_info": photo_post_info,
            "source_info": {
                "source": "PULL_FROM_URL",
                "photo_images": public_urls,
                "photo_cover_index": int(config.get("photoCoverIndex", 0) or 0),
            },
            "post_mode": post_mode,
            "media_type": "PHOTO",
        }
        response = http.post(
            TIKTOK_CONTENT_INIT_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=request_body,
            timeout=120,
        )
        if response.status_code >= 400:
            _raise_tiktok_error(response)
        return {
            "creator_info": creator_info,
            "publish": _response_payload(response),
            "request": request_body,
            "updated_config": updated_config,
            "access_token": access_token,
        }

    raise PreparedPublishError("TikTok publish requires at least one video or image")


def fetch_tiktok_publish_status(access_token: str, publish_id: str, *, session=None) -> dict:
    """Poll TikTok publish status for a given publish_id.

    Returns the parsed JSON response from TikTok's status/fetch endpoint.
    Typical response fields: status, fail_reason, publicaly_available_post_id,
    platform_url, etc.
    """
    http = _get_session(session)
    resp = http.post(
        TIKTOK_STATUS_FETCH_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={"publish_id": publish_id},
        timeout=60,
    )
    _raise_for_status(resp)
    return resp.json()


def refresh_reddit_access_token(config: dict[str, Any], *, session=None) -> dict:
    client_id = str(_config_value(config, "clientId", default_env="REDDIT_CLIENT_ID") or "").strip()
    client_secret = str(_config_value(config, "clientSecret", default_env="REDDIT_CLIENT_SECRET") or "").strip()
    refresh_token = str(_config_value(config, "refreshToken", default_env="REDDIT_REFRESH_TOKEN") or "").strip()
    user_agent = str(
        _config_value(config, "userAgent")
        or f"social-auto-upload/0.1 ({config.get('accountName', 'sau')})"
    ).strip()
    if not client_id or not client_secret or not refresh_token:
        raise PreparedPublishError(
            "Reddit refresh requires clientId/clientSecret/refreshToken or their env references"
        )
    http = _get_session(session)
    token_response = http.post(
        REDDIT_TOKEN_URL,
        auth=(client_id, client_secret),
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        headers={"User-Agent": user_agent},
        timeout=120,
    )
    _raise_for_status(token_response)
    token_payload = token_response.json()
    access_token = token_payload.get("access_token")
    if not access_token:
        raise PreparedPublishError("Reddit token response did not include access_token")
    me_response = http.get(
        REDDIT_ME_URL,
        headers={"Authorization": f"Bearer {access_token}", "User-Agent": user_agent},
        timeout=120,
    )
    _raise_for_status(me_response)
    return {
        "access_token": str(access_token),
        "expires_in": token_payload.get("expires_in"),
        "scope": token_payload.get("scope", ""),
        "me": _response_payload(me_response),
    }


def _reddit_access_token(config: dict[str, Any], *, session=None) -> str:
    client_id = str(_config_value(config, "clientId", default_env="REDDIT_CLIENT_ID") or "").strip()
    client_secret = str(_config_value(config, "clientSecret", default_env="REDDIT_CLIENT_SECRET") or "").strip()
    refresh_token = str(_config_value(config, "refreshToken", default_env="REDDIT_REFRESH_TOKEN") or "").strip()
    user_agent = str(
        _config_value(config, "userAgent")
        or f"social-auto-upload/0.1 ({config.get('accountName', 'sau')})"
    ).strip()
    if not client_id or not client_secret or not refresh_token:
        raise PreparedPublishError(
            "Reddit publish requires clientId/clientSecret/refreshToken or their env references"
        )
    http = _get_session(session)
    response = http.post(
        REDDIT_TOKEN_URL,
        auth=(client_id, client_secret),
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        headers={"User-Agent": user_agent},
        timeout=120,
    )
    _raise_for_status(response)
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise PreparedPublishError("Reddit token response did not include access_token")
    return str(token)


# ---------------------------------------------------------------------------
# X / Twitter (API v2 with OAuth 1.0a)
# ---------------------------------------------------------------------------

X_API_ROOT = "https://api.x.com"
X_MEDIA_UPLOAD_URL = f"{X_API_ROOT}/2/media/upload"
X_MEDIA_INITIALIZE_URL = f"{X_MEDIA_UPLOAD_URL}/initialize"
X_TWEET_URL = f"{X_API_ROOT}/2/tweets"
X_ME_URL = f"{X_API_ROOT}/2/users/me"


def _x_oauth1_signature(
    *,
    method: str,
    url: str,
    params: dict[str, str],
    consumer_secret: str,
    token_secret: str,
) -> str:
    """Build an OAuth 1.0a HMAC-SHA1 signature for a Twitter API request."""
    sorted_params = sorted(params.items())
    param_string = "&".join(
        f"{quote_plus(k, safe='')}={quote_plus(v, safe='')}"
        for k, v in sorted_params
    )
    base_string = "&".join(
        [
            method.upper(),
            quote_plus(url, safe=""),
            quote_plus(param_string, safe=""),
        ]
    )
    signing_key = f"{quote_plus(consumer_secret, safe='')}&{quote_plus(token_secret, safe='')}"
    sig = hmac.new(
        signing_key.encode("utf-8"),
        base_string.encode("utf-8"),
        hashlib.sha1,
    )
    return base64.b64encode(sig.digest()).decode("utf-8")


def _x_auth_header(
    *,
    method: str,
    url: str,
    consumer_key: str,
    token: str,
    consumer_secret: str,
    token_secret: str,
    oauth_params_extra: dict[str, str] | None = None,
    signature_params_extra: dict[str, str] | None = None,
) -> str:
    nonce = secrets.token_hex(16)
    timestamp = str(int(time.time()))
    oauth_params: dict[str, str] = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp,
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    if oauth_params_extra:
        oauth_params.update(oauth_params_extra)
    signature = _x_oauth1_signature(
        method=method,
        url=url,
        params={**oauth_params, **(signature_params_extra or {})},
        consumer_secret=consumer_secret,
        token_secret=token_secret,
    )
    oauth_params["oauth_signature"] = signature
    header_parts = [
        f'{quote_plus(k, safe="")}="{quote_plus(v, safe="")}"'
        for k, v in sorted(oauth_params.items())
        if not k.startswith("oauth_signature_method")
    ]
    header_parts.append('oauth_signature_method="HMAC-SHA1"')
    return "OAuth " + ", ".join(header_parts)


def _x_media_upload(*, file_path: str, access_token: str, session=None) -> str:
    """Upload media via X API v2 using the same OAuth2 account as the tweet."""
    http = _get_session(session)
    source = Path(file_path)
    file_size = source.stat().st_size
    mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    media_category = "tweet_video" if mime_type.startswith("video/") else "tweet_image"
    auth_headers = {"Authorization": f"Bearer {access_token}"}
    # The v2 media API uses POST initialize, per-id append/finalize and a v2
    # status endpoint; the legacy command-style endpoint is OAuth1-only.
    init = http.post(
        X_MEDIA_INITIALIZE_URL,
        headers={**auth_headers, "Content-Type": "application/json"},
        json={"media_type": mime_type, "total_bytes": file_size, "media_category": media_category},
        timeout=120,
    )
    _raise_for_status(init)
    init_data = init.json().get("data") or {}
    media_id = str(init_data.get("id") or "")
    if not media_id:
        raise PreparedPublishError("X media initialize response did not include a media id")

    segment_bytes = 4 * 1024 * 1024
    with source.open("rb") as fh:
        segment_index = 0
        while chunk := fh.read(segment_bytes):
            append = http.post(
                f"{X_MEDIA_UPLOAD_URL}/{media_id}/append",
                headers=auth_headers,
                data={"segment_index": str(segment_index)},
                files={"media": (source.name, chunk, mime_type)},
                timeout=600,
            )
            _raise_for_status(append)
            segment_index += 1

    finalize = http.post(
        f"{X_MEDIA_UPLOAD_URL}/{media_id}/finalize",
        headers=auth_headers,
        timeout=120,
    )
    _raise_for_status(finalize)
    processing = (finalize.json().get("data") or {}).get("processing_info") or {}
    deadline = time.monotonic() + 600
    while processing:
        state = str(processing.get("state") or "").lower()
        if state == "succeeded":
            break
        if state == "failed":
            error = processing.get("error") or {}
            raise PreparedPublishError(f"X media processing failed: {error.get('message') or error}")
        delay = max(float(processing.get("check_after_secs") or 2), 1)
        if time.monotonic() + delay >= deadline:
            raise PreparedPublishError("X media processing did not finish within 600 seconds")
        time.sleep(delay)
        status = http.get(
            X_MEDIA_UPLOAD_URL,
            headers=auth_headers,
            params={"command": "STATUS", "media_id": media_id},
            timeout=120,
        )
        _raise_for_status(status)
        processing = (status.json().get("data") or {}).get("processing_info") or {}
    return media_id


def _twitter_refresh_requires_reconnect(exc: Exception) -> bool:
    """Return True only when X explicitly rejects the saved OAuth credential."""
    error_code = str(getattr(exc, "error_code", "") or "").strip().lower()
    if error_code in {"invalid_grant", "invalid_token", "refresh_token_revoked"}:
        return True
    response = getattr(exc, "response", None)
    if response is None:
        return False
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        return False
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code") or error.get("error") or "").strip().lower()
    else:
        code = str(error or "").strip().lower()
    return code in {"invalid_grant", "invalid_token", "refresh_token_revoked"}


def _normalized_platform_auth(config: dict[str, Any], platform: str) -> str:
    """Return canonical auth mode for an API publisher account."""
    from myUtils.profiles import effective_auth_type
    return effective_auth_type(config, None, platform)


def _maybe_refresh_twitter_token(
    config: dict[str, Any],
    *,
    session=None,
    on_refresh=None,
    account_id: object = None,
    db_path: object = None,
) -> dict[str, Any]:
    """Refresh OAuth 2.0 token and immediately persist a rotated refresh token.

    Runs the read-check-refresh-persist sequence under the per-account
    single-flight lock so the single-use refresh token is never burned by a
    concurrent refresh. ``on_refresh`` receives the freshly rotated config
    inside the lock (see :func:`refresh_twitter_token_single_flight`).
    """
    refresh_token = str(config.get("refreshToken") or "").strip()
    if config.get("_needsReconnect"):
        raise PreparedPublishError(
            "X account requires reconnection; reconnect it before publishing",
            retryable=False,
        )
    if not refresh_token or _normalized_platform_auth(config, "twitter") != "api":
        return config
    # Fast path: still valid, plenty of margin. The authoritative re-read under
    # the single-flight lock repeats this check, so a caller that lost a race
    # never burns the old refresh token.
    if not _x_access_token_stale(config):
        return config
    try:
        updated, _refreshed = refresh_twitter_token_single_flight(
            config,
            account_id=account_id,
            db_path=db_path,
            session=session,
            persist=on_refresh,
            clear_markers=True,
        )
        return updated
    except PreparedPublishError:
        # Includes the shared helper's definitive "reconnect required" signal.
        raise
    except Exception as exc:
        reconnect_required = _twitter_refresh_requires_reconnect(exc)
        if reconnect_required and on_refresh is not None:
            failed_config = dict(config)
            failed_config["_needsReconnect"] = True
            failed_config["_lastMaintenanceError"] = "X OAuth 2.0 refresh token was rejected; reconnect required"
            failed_config["_lastMaintenanceAttemptAt"] = _utc_now().replace(microsecond=0).isoformat()
            on_refresh(failed_config)
        message = (
            "X OAuth 2.0 refresh token was rejected; reconnect this account"
            if reconnect_required
            else "X OAuth 2.0 token refresh failed temporarily; retry later"
        )
        raise PreparedPublishError(message, retryable=not reconnect_required) from exc


def _raise_x_publish_error(exc: Exception, *, stage: str) -> PreparedPublishError:
    """Attach a safe X request-stage label without retaining request secrets."""
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    error_text = str(exc)
    if status is None:
        status_match = re.search(r"\bHTTP\s+(\d{3})\b", error_text, re.IGNORECASE)
        if status_match:
            status = int(status_match.group(1))
    code = str(getattr(exc, "error_code", "") or "").strip()
    if not code and response is not None:
        try:
            body = response.json()
            errors = body.get("errors") if isinstance(body, dict) else None
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                candidate = str(errors[0].get("code") or "").strip()
                code = candidate if candidate.isdigit() else ""
        except Exception:  # noqa: BLE001
            pass
    if not code:
        code_match = re.search(r"['\\\"]code['\\\"]\s*:\s*['\\\"]?(\d{1,6})", error_text)
        if code_match:
            code = code_match.group(1)
    message = f"X {stage} request failed"
    if isinstance(status, int):
        message += f" (HTTP {status})"
    if code:
        message += f" code={code}"
    retryable = not (_twitter_refresh_requires_reconnect(exc) or str(status) == "401")
    return PreparedPublishError(message, details={"stage": stage, "status": status, "code": code}, retryable=retryable)


def publish_twitter_sync(account, payload: dict, *, session=None) -> dict[str, Any]:
    """Publish a tweet with optional media via the X API v2.

    Returns ``{"results": [...], "updated_config": {...}}`` so the caller can
    persist refreshed tokens back to the database.
    """
    config = dict(account.config or {})
    config.setdefault("twitterAuthType", getattr(account, "auth_type", ""))

    # A rotated refresh token is single-use; persist it immediately so any later
    # media/tweet failure does not strand the account on the already-dead token.
    def _persist_refreshed(updated_config: dict[str, Any]) -> None:
        from myUtils import profiles as profile_registry
        account_id = getattr(account, "id", None)
        if account_id is None:
            return
        # Write to the database the account was read from, not the process
        # default — see myUtils.worker._persist_rotated_config.
        raw_db_path = (payload or {}).get("_db_path")
        kwargs = {"db_path": Path(raw_db_path)} if raw_db_path else {}
        profile_registry.update_account(
            account_id, config=updated_config, auth_type="oauth", **kwargs
        )

    config = _maybe_refresh_twitter_token(
        config,
        session=session,
        on_refresh=_persist_refreshed,
        account_id=getattr(account, "id", None),
        db_path=(payload or {}).get("_db_path"),
    )

    # Check if we have OAuth 2.0 token (from PKCE flow)
    oauth2_token = str(config.get("accessToken") or "").strip()
    has_oauth1 = all(_twitter_oauth1_credentials(config))

    if payload.get("campaignId") and not (payload.get("artifacts") or []):
        raise PreparedPublishError("Prepared X campaign has no media artifact; refusing text-only publication")
    if not oauth2_token and not has_oauth1:
        raise PreparedPublishError(
            "Twitter publish requires either OAuth 2.0 tokens (via Connect button) or OAuth 1.0a credentials"
        )

    http = _get_session(session)
    message = _enforce_message_limit(_payload_message(payload), "twitter")
    artifacts = payload.get("artifacts") or []
    media = _extract_media(payload)
    media_items = media["images"][:4] + media["videos"][:1]
    if media["images"] and media["videos"]:
        raise PreparedPublishError("X API posts cannot mix images and video; split the campaign into separate posts")
    if len(media["images"]) > 4 or len(media["videos"]) > 1:
        raise PreparedPublishError("X API posts support up to four images or one video per post")
    if artifacts and not media_items:
        raise PreparedPublishError(
            "Twitter media artifacts were supplied but none is a supported image/video"
        )
    for artifact in artifacts:
        local_path = str(artifact.get("local_path") or "").strip()
        probe = local_path or str(artifact.get("public_url") or "")
        kind = str(artifact.get("artifact_kind") or "").lower()
        declared_media = "image" in kind or "video" in kind or bool(probe)
        if declared_media and not any(
            (item.get("local_path") or item.get("public_url")) == probe
            for item in media_items
        ):
            raise PreparedPublishError("Twitter received an unsupported or unclassified media artifact")
    for item in media_items:
        local_path = str(item.get("local_path") or "").strip()
        if not local_path:
            raise PreparedPublishError(
                "Twitter API media publishing requires a local media path; "
                "public URLs alone are not uploaded by the X API"
            )
        path = Path(local_path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise PreparedPublishError(
                f"Twitter API media file is missing or empty: {path}"
            )

    upload_items = media["images"] or media["videos"]

    # Upload media with the account's OAuth2 user token so media and tweet
    # requests are authenticated as the same profile identity.
    media_ids = []
    if upload_items:
        if not oauth2_token:
            raise PreparedPublishError(
                "X media upload requires an OAuth2 user token with media.write; reconnect via OAuth2",
                retryable=False,
            )
        if "media.write" not in set(str(config.get("scope") or "").split()):
            raise PreparedPublishError(
                "X media upload requires the media.write scope; reconnect the account and grant it",
                retryable=False,
            )
        if media["videos"]:
            for item in upload_items:
                _enforce_video_limits(str(item.get("local_path") or ""), "twitter")
        for item in upload_items:
            local_path = item.get("local_path")
            try:
                mid = _x_media_upload(file_path=local_path, access_token=oauth2_token, session=http)
            except Exception as exc:
                raise _raise_x_publish_error(exc, stage="media upload (OAuth 2.0)") from exc
            if not mid:
                raise PreparedPublishError("X media upload returned no media ID")
            media_ids.append(mid)
        if len(media_ids) != len(upload_items):
            raise PreparedPublishError("X media upload did not attach every requested media item")

    # Create tweet (v2 endpoint)
    tweet_data: dict[str, Any] = {"text": message}
    if media_ids:
        tweet_data["media"] = {"media_ids": media_ids}

    headers = _twitter_auth_headers(config, method="POST", url=X_TWEET_URL)
    headers["Content-Type"] = "application/json"
    try:
        resp = http.post(
            X_TWEET_URL,
            headers=headers,
            data=json.dumps(tweet_data),
            timeout=120,
        )
        _raise_for_status(resp)
    except Exception as exc:
        raise _raise_x_publish_error(exc, stage="tweet creation (OAuth 2.0)") from exc
    return {"results": [_response_payload(resp)], "updated_config": config}


def _twitter_oauth1_credentials(config: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return ``(api_key, api_key_secret, access_token, access_token_secret)``.

    Media upload only exists on the v1.1 endpoint, which speaks OAuth 1.0a, so
    these four values are needed even for accounts that authenticate to the v2
    write endpoints with an OAuth 2.0 bearer token.

    Per-account values come first because one deployment can drive several X
    accounts and each needs its own user context — an access token minted for
    account A cannot attach media to a tweet posted as account B. The
    ``oauth1*`` keys are the per-account home for them: the plain
    ``accessToken`` key already holds the OAuth 2.0 token, so reusing it here
    would shadow one credential set with the other. Env stays as the
    single-account fallback it has always been.
    """
    api_key = str(_config_value(config, "oauth1ApiKey", default_env="X_API_KEY") or "").strip()
    api_key_secret = str(
        _config_value(config, "oauth1ApiKeySecret", default_env="X_API_KEY_SECRET") or ""
    ).strip()
    access_token = str(
        _config_value(config, "oauth1AccessToken", default_env="X_ACCESS_TOKEN") or ""
    ).strip()
    access_token_secret = str(
        _config_value(config, "oauth1AccessTokenSecret", default_env="X_ACCESS_TOKEN_SECRET") or ""
    ).strip()
    return api_key, api_key_secret, access_token, access_token_secret


def _twitter_auth_headers(config: dict[str, Any], *, method: str, url: str) -> dict[str, str]:
    """Return Authorization headers for Twitter, preferring OAuth 2.0 over 1.0a."""
    # OAuth 2.0 Bearer token (from OAuth PKCE flow stored in config)
    oauth2_token = str(config.get("accessToken") or "").strip()
    if oauth2_token:
        return {"Authorization": f"Bearer {oauth2_token}"}

    # OAuth 1.0a (per-account oauth1* keys, else env)
    api_key, api_key_secret, access_token, access_token_secret = _twitter_oauth1_credentials(config)

    if all([api_key, api_key_secret, access_token, access_token_secret]):
        return {
            "Authorization": _x_auth_header(
                method=method, url=url,
                consumer_key=api_key, token=access_token,
                consumer_secret=api_key_secret, token_secret=access_token_secret,
            )
        }

    raise PreparedPublishError(
        "Twitter requires either OAuth 2.0 tokens (via Connect button) or OAuth 1.0a credentials "
        "(oauth1ApiKey, oauth1ApiKeySecret, oauth1AccessToken, oauth1AccessTokenSecret)"
    )


def validate_twitter_config_live(config: dict[str, Any], *, session=None) -> dict:
    """Validate Twitter/X API credentials by fetching the authenticated user's info."""
    http = _get_session(session)
    headers = _twitter_auth_headers(config, method="GET", url=X_ME_URL)
    resp = http.get(X_ME_URL, headers=headers, timeout=120)
    _raise_for_status(resp)
    return _response_payload(resp)


def refresh_twitter_access_token(config: dict[str, Any], *, session=None) -> dict:
    """Refresh a Twitter OAuth 2.0 access token using the stored refresh token."""
    from myUtils import x_auth as _x_auth

    refresh_token = str(config.get("refreshToken") or "").strip()
    if not refresh_token:
        raise PreparedPublishError("Twitter refresh requires a refreshToken (re-authorize via Connect button)")

    http = _get_session(session)
    token_payload = _x_auth.refresh_access_token(refresh_token=refresh_token, session=http)
    access_token = str(token_payload.get("access_token") or "")
    if not access_token:
        raise PreparedPublishError("Twitter token response did not include access_token")

    # The token endpoint has ALREADY rotated the refresh token — the old one is
    # now dead at Twitter. Fetching profile metadata is a nice-to-have; a failure
    # here must never propagate, or the caller discards the freshly-issued tokens
    # and the refresh chain snaps permanently (invalid_grant on every future
    # refresh — the root cause of accounts silently dying).
    user_info: dict = {}
    if access_token:
        try:
            user_info = _x_auth.fetch_user_info(access_token=access_token, session=http)
        except Exception:  # noqa: BLE001
            user_info = {}

    return {
        "access_token": access_token,
        "refresh_token": token_payload.get("refresh_token") or refresh_token,
        "expires_in": token_payload.get("expires_in"),
        "scope": token_payload.get("scope", ""),
        "token_type": token_payload.get("token_type", "bearer"),
        "me": user_info,
    }


# Content-category post flairs. Subreddits that require a flair (r/NudistMen)
# publish a fixed set of categories, and the right one is a property of the
# image, not of the destination. These are the labels the operator named; the
# ids Reddit uses are per-subreddit GUIDs, so config stores label -> id and the
# classifier picks the label.
REDDIT_CONTENT_FLAIRS = ("Selfie", "In Nature", "At Home", "Food & Drink")

# Keyword -> flair label. Ordered most-specific first; the first hit wins. The
# patterns are deliberately conservative: an unmatched image falls through to
# "At Home" (the neutral bucket) rather than guessing wrong, because a wrong
# flair is a moderation problem while the wrong-but-plausible one is not.
_REDDIT_FLAIR_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Food & Drink", (
        "food", "drink", "coffee", "tea", "wine", "beer", "cocktail", "cake",
        "dinner", "breakfast", "lunch", "brunch", "meal", "kitchen", "cook",
        "eat", "pizza", "dessert", "restaurant", "cafe", "barbecue", "bbq",
    )),
    ("In Nature", (
        "nature", "wood", "forest", "outdoor", "beach", "sea", "ocean",
        "river", "lake", "mountain", "garden", "park", "field", "trail",
        "camp", "meadow", "sunset", "sunrise", "hike", "rock", "sand",
        "verdant", "autumn", "snow", "water", "river", "jungle", "pine",
    )),
    ("Selfie", (
        "selfie", "portrait", "mirror", "gaze", "face", "smile", "mask",
        "feline", "kitsune", "fox", "masked", "reflection", "pensive",
        "contemplat",
    )),
)

# Used when nothing matches: the least-assertive category, valid everywhere the
# operator listed it.
REDDIT_DEFAULT_FLAIR = "At Home"


def _reddit_content_flair_label(*sources: Any) -> str:
    """Choose a content-category flair label from the media/payload text.

    Inspects the topic/brief/title/filename text an operator or the pipeline
    already attached. Returns one of :data:`REDDIT_CONTENT_FLAIRS`; falls back
    to :data:`REDDIT_DEFAULT_FLAIR` when nothing matches, and "" when the text
    is empty so the caller can decide to send no flair at all.
    """
    haystack = " ".join(
        str(value or "").lower() for value in sources if value
    )
    if not haystack.strip():
        return ""
    for label, keywords in _REDDIT_FLAIR_HINTS:
        for keyword in keywords:
            if keyword in haystack:
                return label
    return REDDIT_DEFAULT_FLAIR


def _reddit_prefers_self_post(payload: dict, config: dict, subreddit: str) -> bool:
    """Whether this subreddit must receive a self post instead of a link post.

    Subreddits that whitelist link domains reject a foreign URL with
    ``SUBMIT_VALIDATION_LINK_WHITELIST``; the same content is accepted as a self
    post. Accepts either a list of subreddits or a boolean:

    * ``draft.selfPostSubreddits`` / ``config.selfPostSubreddits``: names,
    * ``draft.selfPost`` / ``config.selfPost``: applies to every destination.
    """
    draft = payload.get("draft") if isinstance(payload.get("draft"), dict) else {}
    for source in (draft, config):
        names = source.get("selfPostSubreddits")
        if isinstance(names, str):
            names = [part.strip() for part in names.split(",") if part.strip()]
        if isinstance(names, list):
            lowered = {str(name).strip().lower().lstrip("r/") for name in names}
            if subreddit.strip().lower().lstrip("r/") in lowered:
                return True
    for source in (draft, config):
        if source.get("selfPost") is True:
            return True
    return False


def _reddit_flair_id(payload: dict, config: dict, subreddit: str) -> str:
    """Resolve the post-flair id for one subreddit, or "" when unset.

    Subreddits that require a flair (r/NudistMen) reject the submit with
    ``SUBMIT_VALIDATION_FLAIR_REQUIRED`` and flair ids do not transfer between
    subreddits, so the accepted shapes are:

    * ``{"NudistMen": {"Selfie": "<id>", "In Nature": "<id>"}}`` — a
      per-subreddit label map, chosen by the image's content;
    * ``{"NudistMen": "<id>"}`` — one fixed flair for that subreddit;
    * ``draft.flairId`` / ``config.flairId`` — one flat id applied to every
      destination (only correct for a single-subreddit publish).

    An unmapped subreddit returns "" so the caller submits without a flair and
    Reddit's own validation error names the missing field, rather than us
    guessing a flair the community does not use.
    """
    draft = payload.get("draft") if isinstance(payload.get("draft"), dict) else {}
    for source in (draft, config):
        mapping = source.get("flairIds")
        if isinstance(mapping, dict):
            for key in (subreddit, f"r/{subreddit}", subreddit.lower()):
                value = mapping.get(key)
                if value in (None, ""):
                    continue
                if isinstance(value, dict):
                    # Content-category map: pick the label from the media text.
                    label = _reddit_content_flair_label(
                        draft.get("topic"),
                        payload.get("title"),
                        payload.get("brief"),
                        draft.get("message"),
                        payload.get("fileName"),
                        payload.get("fileRef"),
                        [a.get("local_path") or a.get("public_url")
                         for a in (payload.get("artifacts") or [])
                         if isinstance(a, dict)],
                    )
                    chosen = value.get(label) if label else None
                    if chosen in (None, ""):
                        # Fall back to the default label, then to any single
                        # entry so a one-flair subreddit still publishes.
                        chosen = value.get(REDDIT_DEFAULT_FLAIR)
                    if chosen in (None, "") and len(value) == 1:
                        chosen = next(iter(value.values()))
                    if chosen in (None, ""):
                        return ""
                    return str(chosen).strip()
                return str(value).strip()
    for source in (draft, config):
        flat = source.get("flairId")
        if flat not in (None, ""):
            return str(flat).strip()
    return ""


def _reddit_upload_image(http, headers: dict[str, str], image: dict[str, str]) -> str:
    """Upload one image to Reddit's own media host; return the URL to submit with.

    Reddit image posts have to point at an asset Reddit has ingested, not at an
    external host. Posting our own storage URL instead would make this the only
    self-hosted link in a feed of i.redd.it images — which is exactly the shape
    the target subs' promotion rules go after, and it survives even when the
    title and body are clean.

    Three steps: request a lease, POST the bytes to the S3 endpoint it hands
    back, then give the resulting S3 object URL to /api/submit with kind=image.
    Reddit fetches that URL, processes the image and serves it from i.redd.it.

    The URL returned here must be the raw S3 one. Submitting the eventual
    i.redd.it/<id> URL fails: Reddit fetches it while validating the post, the
    CDN does not have the object yet, and the submit is rejected as an invalid
    image URL.
    """
    local_path = str(image.get("local_path") or "")
    if not local_path:
        raise PreparedPublishError("Reddit image upload requires a local_path")
    path = Path(local_path)
    if not path.is_file():
        raise PreparedPublishError(f"Reddit image upload: missing file {local_path}")
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"

    lease = http.post(
        REDDIT_MEDIA_LEASE_URL,
        headers=headers,
        data={"filepath": path.name, "mimetype": mime},
        timeout=120,
    )
    _raise_for_status(lease)
    payload = lease.json() or {}
    args = payload.get("args") or {}
    action = str(args.get("action") or "")
    if not action:
        raise PreparedPublishError("Reddit media lease returned no upload action")
    # Reddit hands back a protocol-relative URL ("//host/path").
    if action.startswith("//"):
        action = f"https:{action}"
    fields = {
        str(field.get("name")): str(field.get("value"))
        for field in (args.get("fields") or [])
        if field.get("name")
    }

    with open(path, "rb") as handle:
        upload = http.post(
            action,
            data=fields,
            files={"file": (path.name, handle, mime)},
            timeout=600,
        )
    _raise_for_status(upload)

    match = re.search(r"<Location>(.*?)</Location>", upload.text or "")
    if not match:
        raise PreparedPublishError("Reddit media upload returned no Location")
    return unquote(match.group(1).strip())


def _reddit_image_post_url(websocket_url: str, timeout: float = 20.0) -> str:
    """Best-effort read of the permalink Reddit pushes after an image submit.

    An image submission answers with a websocket_url and a user_submitted_page
    URL rather than the post's fullname, so this socket is the only way to learn
    the permalink. The post is created whether or not this succeeds, so every
    failure path here returns "" instead of raising.
    """
    if not websocket_url:
        return ""
    try:  # pragma: no cover - depends on websocket-client being installed
        import websocket
    except ImportError:
        return ""
    socket = None
    try:  # pragma: no cover - network dependent
        socket = websocket.create_connection(websocket_url, timeout=timeout)
        socket.settimeout(timeout)
        while True:
            message = json.loads(socket.recv())
            payload = message.get("payload") or {}
            redirect = payload.get("redirect") or payload.get("url")
            if redirect:
                return str(redirect)
            if message.get("type") in {"error", "upload_error", "failed"}:
                return ""
    except Exception:  # noqa: BLE001 - the post already exists; this is only the permalink
        return ""
    finally:
        if socket is not None:
            try:
                socket.close()
            except Exception:  # noqa: BLE001
                pass


def publish_reddit_sync(account, payload: dict, *, session=None) -> list[Any]:
    config = dict(account.config or {})
    config.setdefault("accountName", getattr(account, "account_name", "sau"))
    # Check payload draft for subreddits override, then fall back to account config
    draft = payload.get("draft") or {}
    subreddits = draft.get("subreddits") or config.get("subreddits") or []
    if isinstance(subreddits, str):
        subreddits = [s.strip() for s in subreddits.split(",") if s.strip()]
    if not isinstance(subreddits, list) or not subreddits:
        raise PreparedPublishError("Reddit publish requires a non-empty subreddits array")

    http = _get_session(session)
    access_token = _reddit_access_token(config, session=http)
    user_agent = str(
        _config_value(config, "userAgent")
        or f"social-auto-upload/0.1 ({config.get('accountName', 'sau')})"
    ).strip()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": user_agent,
    }
    message = _enforce_message_limit(_payload_message(payload), "reddit")
    artifacts = payload.get("artifacts") or []
    media = _extract_media(payload)
    if artifacts and not (media["videos"] or media["images"]):
        raise PreparedPublishError("Reddit media artifacts were supplied but none is a supported image/video")
    if len(media["videos"]) > 1:
        raise PreparedPublishError("Reddit supports one video link per post")
    if len(media["images"]) > 1:
        raise PreparedPublishError("Reddit API image publishing supports one image per post")
    # Reddit cannot host video itself, so a video payload keeps the link-post
    # contract it always had (and video wins when both are present). An image
    # payload, by contrast, must become a native image post: submitting the
    # storage URL as a link made every Reddit submission the one self-hosted
    # link in a feed of i.redd.it images — a promotion signal that survives even
    # a clean title and body, and what the target subs' rules target.
    public_url = ""
    image = None
    # A native image upload needs the file locally; when it is missing we fall
    # back to a self post (never a link post to our own storage, which is the
    # promotion signal the native path exists to avoid).
    force_self_post = False
    if media["videos"]:
        # A video with no public URL is still publishable: Reddit accepts it as
        # a self post whose body carries the message (and the URL when one is
        # present). Link-whitelisted subs route through _reddit_prefers_self_post
        # below; every other sub takes the link branch only when a URL actually
        # exists. Raising here defeated both paths and refused valid posts.
        public_url = media["videos"][0].get("public_url") or ""
    elif media["images"]:
        image = media["images"][0]
        if not (image.get("local_path") or ""):
            # No local copy to ingest into Reddit's media host. A self post
            # carrying the image's public URL is still valid, so fall back to
            # that instead of refusing the publish outright.
            public_url = str(image.get("public_url") or "")
            image = None
            force_self_post = True
    elif artifacts:
        raise PreparedPublishError("Reddit media artifacts were supplied but none is a supported image/video")
    title = _message_title(payload)
    results = []
    for subreddit in subreddits:
        native_url = ""
        if image is not None:
            # One lease per subreddit: the asset is consumed by the submit that
            # references it. A local file is guaranteed here (an image without
            # one fell back to a self post above).
            native_url = _reddit_upload_image(http, headers, image)
        data = {
            "api_type": "json",
            "sr": subreddit,
            "title": title[:300],
            "resubmit": "false",
            "sendreplies": "true",
        }
        # Some subreddits (r/NudistMen among them) reject every submit with
        # SUBMIT_VALIDATION_FLAIR_REQUIRED unless a post flair is attached, and
        # flair ids are per-subreddit. Accept either a mapping
        # ``{"<subreddit>": "<flair_id>"}`` or one flat id, and leave an
        # unmapped subreddit unflaired so the service's own error surfaces
        # rather than posting under the wrong flair.
        flair_id = _reddit_flair_id(payload, config, subreddit)
        if flair_id:
            data["flair_id"] = flair_id
        # Some subreddits restrict link posts to a whitelist of domains
        # (r/NudistMen allows only imgur/blogspot/youtube/... - not an arbitrary
        # CDN). A link post is then refused with SUBMIT_VALIDATION_LINK_WHITELIST
        # even when the media is fine, so those subreddits take the video as a
        # self post with the URL in the body instead, which the same rules allow.
        # Configure per subreddit, or globally, via account config.
        prefer_self_post = _reddit_prefers_self_post(payload, config, subreddit)
        if native_url:
            data["kind"] = "image"
            data["url"] = native_url
        elif public_url and not (prefer_self_post or force_self_post):
            data["kind"] = "link"
            data["url"] = public_url
        else:
            data["kind"] = "self"
            # Carry the URL in the body when we have one. The self branch is
            # only reached with a URL when the subreddit prefers self posts, so
            # this keeps the link instead of silently dropping it; without a URL
            # the message alone is a valid post.
            data["text"] = f"{message}\n\n{public_url}" if public_url else message
        response = http.post(REDDIT_SUBMIT_URL, headers=headers, data=data, timeout=120)
        _raise_for_status(response)
        body = response.json()
        errors = body.get("json", {}).get("errors", [])
        if errors:
            codes = {
                str(error[0]).strip()
                for error in errors
                if isinstance(error, (list, tuple)) and error
            }
            permanent_codes = {
                "SUBMIT_VALIDATION_FLAIR_REQUIRED",
                "NO_IMAGES",
                "SUBREDDIT_NOTALLOWED_BANNED",
            }
            raise PreparedPublishError(
                f"Reddit submit failed for r/{subreddit}: {errors}",
                retryable=not bool(codes & permanent_codes),
            )
        # An image submit answers with a websocket_url instead of the post's
        # fullname; read the permalink off it, best effort.
        ws_url = str((body.get("json", {}).get("data") or {}).get("websocket_url") or "")
        if ws_url:
            post_url = _reddit_image_post_url(ws_url)
            if post_url:
                body.setdefault("json", {}).setdefault("data", {})["post_url"] = post_url
        results.append(body)
    return results


def refresh_youtube_access_token(config: dict[str, Any], *, session=None) -> dict:
    channel_id = str(config.get("channelId") or "").strip()
    if not channel_id:
        raise PreparedPublishError("YouTube refresh requires channelId")
    http = _get_session(session)
    client_id = str(_config_value(config, "clientId", default_env="YT_CLIENT_ID") or "").strip()
    client_secret = str(_config_value(config, "clientSecret", default_env="YT_CLIENT_SECRET") or "").strip()
    refresh_token = str(_config_value(config, "refreshToken") or "").strip()
    if not client_id or not client_secret or not refresh_token:
        raise PreparedPublishError(
            "YouTube refresh requires clientId/clientSecret/refreshToken"
        )
    token_response = http.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=120,
    )
    _raise_for_status(token_response)
    token_payload = token_response.json()
    access_token = str(token_payload.get("access_token") or "")
    if not access_token:
        raise PreparedPublishError("Google token response did not include access_token")
    channel_response = http.get(
        YOUTUBE_CHANNELS_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"part": "id,snippet", "id": channel_id},
        timeout=120,
    )
    _raise_for_status(channel_response)
    return {
        "access_token": access_token,
        "expires_in": token_payload.get("expires_in"),
        "channel": _response_payload(channel_response),
    }


def _google_access_token(config: dict[str, Any], *, session=None) -> str:
    client_id = str(_config_value(config, "clientId", default_env="YT_CLIENT_ID") or "").strip()
    client_secret = str(_config_value(config, "clientSecret", default_env="YT_CLIENT_SECRET") or "").strip()
    refresh_token = str(_config_value(config, "refreshToken") or "").strip()
    if not client_id or not client_secret or not refresh_token:
        raise PreparedPublishError(
            "YouTube publish requires clientId/clientSecret/refreshToken"
        )

    http = _get_session(session)
    response = http.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=120,
    )
    _raise_for_status(response)
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise PreparedPublishError("Google token response did not include access_token")
    return str(token)


def _verify_youtube_visibility(http, access_token: str, video_id: str, requested: str) -> None:
    """Confirm the uploaded video really has the requested visibility.

    Only ``public``/``unlisted`` are verified (``private`` is the default and
    nothing to check). Raises :class:`PreparedPublishError` when YouTube reports
    a different value, so an API-project lock cannot silently hide the video.
    """
    requested = str(requested or "").strip().lower()
    if requested not in {"public", "unlisted"}:
        return
    try:
        response = http.get(
            "https://www.googleapis.com/youtube/v3/videos",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"id": video_id, "part": "status"},
            timeout=60,
        )
    except Exception as exc:  # noqa: BLE001 - a read-back failure is not a publish failure
        logger.warning("YouTube visibility check failed for %s: %s", video_id, exc)
        return
    if int(getattr(response, "status_code", 0) or 0) != 200:
        return
    try:
        items = (response.json() or {}).get("items") or []
    except Exception:  # noqa: BLE001
        return
    actual = ""
    if items and isinstance(items[0], dict):
        actual = str((items[0].get("status") or {}).get("privacyStatus") or "").lower()
    if actual and actual != requested:
        raise PreparedPublishError(
            f"YouTube uploaded {video_id} as '{actual}' but '{requested}' was requested "
            "(an unaudited API project is often locked to private; use the browser "
            "uploader or make the API project audited)",
            retryable=False,
        )
    logger.info("YouTube visibility verified: %s -> %s", video_id, actual or requested)


def publish_youtube_sync(account, payload: dict, *, session=None) -> dict:
    config = dict(account.config or {})
    channel_id = str(config.get("channelId") or "").strip()
    if not channel_id:
        raise PreparedPublishError("YouTube publish requires channelId")

    media = _extract_media(payload)
    if not media["videos"]:
        raise PreparedPublishError(
            "YouTube publishing requires a video artifact; the selected campaign media contains no video.",
            retryable=False,
        )
    if len(media["videos"]) > 1:
        raise PreparedPublishError(
            "YouTube accepts one video per upload; split the campaign into separate targets",
            retryable=False,
        )
    if not media["videos"][0].get("local_path"):
        raise PreparedPublishError("YouTube publish requires a local video artifact", retryable=False)
    video_path = Path(media["videos"][0]["local_path"])
    if not video_path.is_file() or video_path.stat().st_size <= 0:
        raise PreparedPublishError(
            f"YouTube video artifact is missing or empty: {video_path}",
            retryable=False,
        )

    http = _get_session(session)
    access_token = _google_access_token(config, session=http)
    headers = {"Authorization": f"Bearer {access_token}"}

    # Extract metadata from draft and config
    draft = payload.get("draft") or {}

    # Title: use draft.title if set, otherwise first line of message
    raw_title = str(draft.get("title") or "").strip()
    if not raw_title:
        raw_message = str(draft.get("message") or payload.get("message", "")).strip()
        lines = [l.strip() for l in raw_message.split("\n") if l.strip()]
        for line in lines:
            if not line.startswith("publish-center") and not line.startswith("campaign"):
                raw_title = line
                break
        if not raw_title and lines:
            raw_title = lines[0]
    title = raw_title[:100] or "Untitled"

    # Description
    raw_message = str(draft.get("message") or payload.get("message", "")).strip()
    message_lines = raw_message.split("\n")
    if message_lines and message_lines[0].strip() == raw_title:
        description = "\n".join(message_lines[1:]).strip()
    else:
        description = raw_message
    first_comment = str(draft.get("firstComment") or "").strip()
    if first_comment and first_comment not in description:
        description = f"{description}\n\n{first_comment}" if description else first_comment
    description = _enforce_message_limit(description, "youtube")

    # Tags
    tags = draft.get("hashtags") or draft.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    tags = [t.lstrip("#") for t in tags if t]

    # Build snippet
    snippet = {
        "title": title,
        "description": description[:5000],
        "channelId": channel_id,
        "categoryId": str(config.get("categoryId") or "22"),
    }
    if tags:
        snippet["tags"] = tags[:500]
    if config.get("defaultLanguage"):
        snippet["defaultLanguage"] = config["defaultLanguage"]
    if config.get("defaultAudioLanguage"):
        snippet["defaultAudioLanguage"] = config["defaultAudioLanguage"]

    # Build status
    status = {
        "privacyStatus": str(config.get("privacyStatus") or "private"),
        "selfDeclaredMadeForKids": bool(config.get("madeForKids", False)),
        "embeddable": bool(config.get("embeddable", True)),
        "publicStatsViewable": bool(config.get("publicStatsViewable", True)),
    }
    if config.get("license"):
        status["license"] = config["license"]
    if config.get("publishAt"):
        status["publishAt"] = config["publishAt"]
        status["privacyStatus"] = "private"  # Required for scheduled
    if config.get("containsSyntheticMedia"):
        status["containsSyntheticMedia"] = True

    # Build recording details
    recording_details = {}
    if config.get("recordingDate"):
        recording_details["recordingDate"] = config["recordingDate"]
    if config.get("locationDescription"):
        recording_details["locationDescription"] = config["locationDescription"]
    if config.get("latitude") and config.get("longitude"):
        recording_details["location"] = {
            "latitude": float(config["latitude"]),
            "longitude": float(config["longitude"]),
        }

    # Build metadata
    metadata = {"snippet": snippet, "status": status}
    if recording_details:
        metadata["recordingDetails"] = recording_details

    logger.info("YouTube upload: title=%r, tags=%d, privacy=%s", title, len(tags), status["privacyStatus"])

    init_response = http.post(
        YOUTUBE_RESUMABLE_UPLOAD_URL,
        headers={
            **headers,
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(video_path.stat().st_size),
            "X-Upload-Content-Type": mimetypes.guess_type(video_path.name)[0] or "video/mp4",
        },
        data=json.dumps(metadata),
        timeout=120,
    )
    _raise_for_status(init_response)
    upload_url = init_response.headers.get("Location")
    if not upload_url:
        raise PreparedPublishError("YouTube resumable upload did not return a Location header")

    with video_path.open("rb") as handle:
        upload_response = http.put(
            upload_url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": mimetypes.guess_type(video_path.name)[0] or "video/mp4",
            },
            data=handle,
            timeout=1800,
        )
    _raise_for_status(upload_response)
    result = upload_response.json()
    video_id = result.get("id")
    # "Succeeded" must mean visible at the requested visibility. An unaudited
    # API project is routinely force-locked to private, which silently hides
    # the post while the target reports success (the Stonewall video did exactly
    # this). Verify and fail loudly instead.
    if video_id:
        _verify_youtube_visibility(http, access_token, str(video_id), status["privacyStatus"])

    # Upload thumbnail if available
    thumbnail_path = _find_thumbnail(media, video_path)
    if thumbnail_path and result.get("id"):
        try:
            _upload_youtube_thumbnail(http, access_token, result["id"], thumbnail_path)
            logger.info("YouTube thumbnail uploaded: %s", thumbnail_path.name)
        except Exception as exc:
            logger.warning("YouTube thumbnail upload failed: %s", exc)

    # Add to playlist if configured
    playlist_id = str(config.get("playlistId") or "").strip()
    if playlist_id and result.get("id"):
        playlist_response = http.post(
            YOUTUBE_PLAYLIST_INSERT_URL,
            headers={
                **headers,
                "Content-Type": "application/json; charset=UTF-8",
            },
            data=json.dumps(
                {
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {
                            "kind": "youtube#video",
                            "videoId": result["id"],
                        },
                    }
                }
            ),
            timeout=120,
        )
        _raise_for_status(playlist_response)
    return result


def _find_thumbnail(media: dict, video_path: Path) -> Path | None:
    """Find a thumbnail image for the video."""
    # Check if there's an image artifact
    for img in media.get("images", []):
        if img.get("local_path"):
            p = Path(img["local_path"])
            if p.exists():
                return p
    # Check for thumbnail with same name as video
    for ext in [".png", ".jpg", ".jpeg"]:
        thumb = video_path.with_suffix(ext)
        if thumb.exists():
            return thumb
    return None


def _upload_youtube_thumbnail(http, access_token: str, video_id: str, thumbnail_path: Path) -> None:
    """Upload a thumbnail for a YouTube video."""
    url = f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={video_id}&uploadType=media"
    with thumbnail_path.open("rb") as f:
        response = http.post(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "image/png",
            },
            data=f.read(),
            timeout=60,
        )
    _raise_for_status(response)


# ---- Patreon ----

PATREON_IDENTITY_URL = "https://www.patreon.com/api/oauth2/v2/identity"


def validate_patreon_config_live(config: dict[str, Any], *, session=None) -> dict:
    """Validate Patreon account config via the OAuth identity endpoint.

    Since Patreon's public API v2 does not support post creation,
    validation is limited to checking that the OAuth token is valid.
    """
    access_token = str(_config_value(config, "accessToken") or "").strip()
    if not access_token:
        raise PreparedPublishError("Patreon validation requires accessToken or accessTokenEnv")
    http = _get_session(session)
    response = http.get(
        PATREON_IDENTITY_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"fields[user]": "full_name,url"},
        timeout=120,
    )
    _raise_for_status(response)
    return _response_payload(response)


# ---- Teaching Blog (GitHub Contents API) ----

GITHUB_API_ROOT = "https://api.github.com"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from Markdown text. Returns (metadata_dict, body_without_frontmatter)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fm_text = parts[1].strip()
    body = parts[2].strip()
    meta: dict[str, Any] = {}
    for line in fm_text.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip().strip('"').strip("'")
    return meta, body


def _slugify_title(title: str) -> str:
    """Turn a title into a filesystem-safe slug."""
    import re as _re
    slug = _re.sub(r"[^a-z0-9]+", "-", title.lower().strip()).strip("-")
    return slug or "post"


def publish_teaching_blog_sync(
    account,
    payload: dict,
    *,
    session=None,
) -> list[dict[str, Any]]:
    """Publish a Markdown post to a GitHub repo via the Contents API."""
    config = account.config or {}
    owner = str(_config_value(config, "repoOwner") or "").strip()
    repo = str(_config_value(config, "repoName") or "").strip()
    branch = str(_config_value(config, "branch") or "main").strip()
    content_dir = str(_config_value(config, "contentDir") or "content/posts").strip("/")
    token = str(_config_value(config, "githubToken", default_env="SAU_TEACHING_BLOG_GITHUB_TOKEN") or "").strip()

    if not owner:
        raise PreparedPublishError("Teaching Blog account requires repoOwner in config")
    if not repo:
        raise PreparedPublishError("Teaching Blog account requires repoName in config")
    if not token:
        raise PreparedPublishError("Teaching Blog account requires githubToken or githubTokenEnv")

    message = _payload_message(payload)
    if not message:
        raise PreparedPublishError("Teaching Blog publish requires a message (Markdown body)")

    title_from_draft = str((payload.get("draft") or {}).get("title") or "").strip()
    frontmatter, _body = _parse_frontmatter(message)
    title = title_from_draft or frontmatter.get("title") or _message_title(payload)
    slug = _slugify_title(title)

    # Build frontmatter if not already present
    if not message.startswith("---"):
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lines = [
            "---",
            f"title: \"{title}\"",
            f"date: {date_str}",
            "---",
            "",
            message,
        ]
        message = "\n".join(lines)

    file_path = f"{content_dir}/{slug}.md"
    content_b64 = base64.b64encode(message.encode("utf-8")).decode("ascii")

    http = _get_session(session)
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Check if file already exists (to get SHA for update)
    existing_sha = None
    get_url = f"{GITHUB_API_ROOT}/repos/{owner}/{repo}/contents/{file_path}"
    get_resp = http.get(get_url, headers=headers, params={"ref": branch}, timeout=30)
    if get_resp.status_code == 200:
        existing_sha = get_resp.json().get("sha")

    # PUT (create or update)
    put_body: dict[str, Any] = {
        "message": f"publish: {title}",
        "content": content_b64,
        "branch": branch,
    }
    if existing_sha:
        put_body["sha"] = existing_sha

    put_resp = http.put(get_url, headers=headers, json=put_body, timeout=30)
    _raise_for_status(put_resp)
    result = put_resp.json()

    return [{"path": file_path, "sha": result.get("content", {}).get("sha"), "url": result.get("content", {}).get("html_url")}]


def validate_teaching_blog_config_live(config: dict[str, Any], *, session=None) -> dict:
    """Validate Teaching Blog config by checking GitHub repo access."""
    owner = str(_config_value(config, "repoOwner") or "").strip()
    repo = str(_config_value(config, "repoName") or "").strip()
    token = str(_config_value(config, "githubToken", default_env="SAU_TEACHING_BLOG_GITHUB_TOKEN") or "").strip()
    if not owner or not repo or not token:
        raise PreparedPublishError("Teaching Blog validation requires repoOwner, repoName, and githubToken")
    http = _get_session(session)
    url = f"{GITHUB_API_ROOT}/repos/{owner}/{repo}"
    resp = http.get(url, headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}, timeout=30)
    _raise_for_status(resp)
    data = resp.json()
    return {"repo_full_name": data.get("full_name"), "default_branch": data.get("default_branch"), "private": data.get("private")}


# ---- NW/SW Blog (sexualwill.com — git-pushed MDX posts) ----

NW_SW_BLOG_VALID_PERSONAS = ("sexualwill", "nakedwill")
NW_SW_BLOG_VALID_LOCALES = ("en", "zh")
_NW_SW_BLOG_TOKEN_ENV = "SAU_NW_SW_BLOG_GITHUB_TOKEN"


def _nw_sw_blog_locales(config: dict[str, Any], payload: dict | None = None) -> list[str]:
    """Resolve the ordered list of locales this publish should produce.

    Precedence:
      1. ``payload.draft.locales`` (per-publish override; csv or list)
      2. ``config.locales`` / ``config.locale`` (account default)
      3. fall back to ``en``
    """
    payload = payload or {}
    draft = payload.get("draft") or {}
    raw = draft.get("locales") or config.get("locales") or config.get("locale") or "en"
    if isinstance(raw, list):
        items = [str(item).strip().lower() for item in raw]
    else:
        items = [part.strip().lower() for part in str(raw).replace("；", ";").replace(";", ",").replace("+", ",").split(",")]
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out or ["en"]


def _nw_sw_blog_lang_sections(message: str) -> list[str]:
    """Split a possibly-bilingual Markdown body on a standalone ``---`` rule.

    The bilingual blog builder writes the COMPLETE primary-language version,
    then a Markdown horizontal rule alone on its line (``---``), then the
    second-language version.  A single-language body has no standalone rule
    and is returned whole.
    """
    if not message:
        return []
    lines = message.splitlines()
    split_at = None
    for i, line in enumerate(lines):
        if line.strip() == "---":
            split_at = i
            break
    if split_at is None:
        return [message.strip()]
    first = "\n".join(lines[:split_at]).strip()
    rest = "\n".join(lines[split_at + 1:]).strip()
    sections = [first]
    if rest:
        sections.append(rest)
    return [section for section in sections if section]


def _nw_sw_blog_yaml_quote(value: str) -> str:
    """Quote a scalar for YAML frontmatter, escaping embedded double quotes."""
    text = str(value or "").strip()
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _nw_sw_blog_yaml_list(values) -> str:
    items = [str(v).strip().strip('"').strip("'") for v in (values or []) if str(v).strip()]
    if not items:
        return "[]"
    return "[" + ", ".join(_nw_sw_blog_yaml_quote(item) for item in items) + "]"


def _nw_sw_blog_frontmatter(*, title, slug, persona, description="", category="", tags=None,
                            date_iso=None, content_warning="") -> str:
    """Build the frontmatter block matching sexualwill_static's _TEMPLATE.mdx."""
    from datetime import datetime, timezone
    date_str = date_iso or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    lines = [
        "---",
        f"title: {_nw_sw_blog_yaml_quote(title)}",
        f"slug: {_nw_sw_blog_yaml_quote(slug)}",
        f"translationKey: {_nw_sw_blog_yaml_quote(slug)}",
        f"description: {_nw_sw_blog_yaml_quote(description)}",
        f'date: "{date_str}"',
        f"category: {_nw_sw_blog_yaml_quote(category or 'General Topics')}",
        f"tags: {_nw_sw_blog_yaml_list(tags)}",
        f"audience: \"18+\"",
        f"persona: {_nw_sw_blog_yaml_quote(persona)}",
        f"seoIndex: true",
    ]
    if content_warning:
        lines.append(f"contentWarning: {_nw_sw_blog_yaml_quote(content_warning)}")
    lines.append("---")
    return "\n".join(lines)


def _nw_sw_blog_title_for(payload: dict, message: str, index: int, locale: str) -> str:
    """Pick a title for one locale out of a possibly ｜-separated multi-title string."""
    draft = payload.get("draft") or {}
    raw_title = str(draft.get("title") or "").strip()
    if not raw_title:
        parsed_fm, _body = _parse_frontmatter(message)
        raw_title = str(parsed_fm.get("title") or _message_title(payload)).strip()
    # The bilingual builder separates short fields with "｜".
    parts = [p.strip() for p in raw_title.split("｜") if p.strip()]
    if parts and index < len(parts):
        return parts[index]
    return raw_title or f"{locale} post"


def publish_nw_sw_blog_sync(
    account,
    payload: dict,
    *,
    session=None,
) -> list[dict[str, Any]]:
    """Publish an MDX post by committing it to the sexualwill_static GitHub repo.

    Each account maps to one persona and one or more locales; every locale is
    written as a separate ``.mdx`` file under ``content/posts`` (en) or
    ``content/posts/zh`` (zh) in the target repo, sharing the same slug /
    ``translationKey`` so the site links the two versions.
    """
    config = account.config or {}
    owner = str(_config_value(config, "repoOwner") or "").strip()
    repo = str(_config_value(config, "repoName") or "").strip()
    branch = str(_config_value(config, "branch") or "main").strip()
    token = str(_config_value(config, "githubToken", default_env=_NW_SW_BLOG_TOKEN_ENV) or "").strip()
    persona = str(_config_value(config, "persona") or "").strip().lower()
    locales = _nw_sw_blog_locales(config, payload)

    if not owner or not repo:
        raise PreparedPublishError("NW/SW Blog account requires repoOwner and repoName in config")
    if not token:
        raise PreparedPublishError("NW/SW Blog account requires githubToken or githubTokenEnv")
    if persona not in NW_SW_BLOG_VALID_PERSONAS:
        raise PreparedPublishError(f"NW/SW Blog persona must be 'sexualwill' or 'nakedwill', got '{persona}'")
    for locale in locales:
        if locale not in NW_SW_BLOG_VALID_LOCALES:
            raise PreparedPublishError(f"NW/SW Blog locale must be 'en' or 'zh', got '{locale}'")

    message = _payload_message(payload)
    if not message:
        raise PreparedPublishError("NW/SW Blog publish requires a message (MDX body)")

    # A single message may carry multiple language sections (bilingual build);
    # zip them onto the requested locales. If counts differ, repeat the last
    # section for trailing locales (safer than dropping content).
    sections = _nw_sw_blog_lang_sections(message)
    if len(sections) < len(locales) and len(sections) == 1:
        sections = sections * len(locales)

    http = _get_session(session)
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    draft = payload.get("draft") or {}
    category = str(draft.get("category") or config.get("category") or "").strip()
    tags = draft.get("tags") or config.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    content_warning = str(draft.get("contentWarning") or config.get("contentWarning") or "").strip()
    hero_image = str(draft.get("heroImage") or config.get("heroImage") or "").strip()

    results: list[dict[str, Any]] = []
    # A bilingual account shares one slug / translationKey across locales; base
    # it on the primary (first-locale) title so en & zh files line up.
    explicit_slug = str(draft.get("slug") or "").strip()
    primary_title = _nw_sw_blog_title_for(payload, message, 0, locales[0] if locales else "en")
    shared_slug = explicit_slug or _slugify_title(primary_title)

    for index, locale in enumerate(locales):
        locale_message = sections[min(index, len(sections) - 1)] if sections else message

        title = _nw_sw_blog_title_for(payload, locale_message, index, locale)
        slug = shared_slug

        # Re-parse frontmatter already present in the section so round-trips
        # (SAU → file → edit → re-publish) keep author-set fields.
        parsed_fm, parsed_body = _parse_frontmatter(locale_message)
        body = parsed_body or locale_message.strip()

        description = str(draft.get("description") or parsed_fm.get("description") or "").strip()
        fm = _nw_sw_blog_frontmatter(
            title=title,
            slug=slug,
            persona=persona,
            description=description,
            category=str(parsed_fm.get("category") or category),
            tags=parsed_fm.get("tags") or tags,
            content_warning=str(parsed_fm.get("contentWarning") or content_warning),
        )
        mdx = f"{fm}\n\n{body}\n"

        content_dir = "content/posts/zh" if locale == "zh" else "content/posts"
        file_path = f"{content_dir}/{slug}.mdx"
        content_b64 = base64.b64encode(mdx.encode("utf-8")).decode("ascii")

        # Idempotent upsert: read existing file SHA when present, then PUT.
        get_url = f"{GITHUB_API_ROOT}/repos/{owner}/{repo}/contents/{file_path}"
        get_resp = http.get(get_url, headers=headers, params={"ref": branch}, timeout=30)
        existing_sha = None
        if get_resp.status_code == 200:
            existing_sha = (get_resp.json() or {}).get("sha")
        put_body: dict[str, Any] = {
            "message": f"publish: {slug} ({locale})",
            "content": content_b64,
            "branch": branch,
        }
        if existing_sha:
            put_body["sha"] = existing_sha
        put_resp = http.put(get_url, headers=headers, json=put_body, timeout=30)
        _raise_for_status(put_resp)
        put_json = put_resp.json() if hasattr(put_resp, "json") else {}
        results.append({
            "locale": locale,
            "path": file_path,
            "sha": (put_json.get("content") or {}).get("sha") or put_json.get("commit", {}).get("sha"),
            "html_url": f"https://github.com/{owner}/{repo}/blob/{branch}/{file_path}",
            "persona": persona,
        })
    return results


def validate_nw_sw_blog_config_live(config: dict[str, Any], *, session=None) -> dict:
    """Validate NW/SW Blog config by checking read access to the target repo."""
    owner = str(_config_value(config, "repoOwner") or "").strip()
    repo = str(_config_value(config, "repoName") or "").strip()
    token = str(_config_value(config, "githubToken", default_env=_NW_SW_BLOG_TOKEN_ENV) or "").strip()
    persona = str(_config_value(config, "persona") or "").strip().lower()
    if not owner or not repo or not token:
        raise PreparedPublishError("NW/SW Blog validation requires repoOwner, repoName, and githubToken")
    if persona not in NW_SW_BLOG_VALID_PERSONAS:
        raise PreparedPublishError(f"NW/SW Blog persona must be 'sexualwill' or 'nakedwill', got '{persona}'")
    http = _get_session(session)
    url = f"{GITHUB_API_ROOT}/repos/{owner}/{repo}"
    resp = http.get(url, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }, timeout=30)
    _raise_for_status(resp)
    data = resp.json()
    return {
        "repo_full_name": data.get("full_name"),
        "default_branch": data.get("default_branch"),
        "private": data.get("private"),
        "persona": persona,
    }


# ---- Bluesky (AT Protocol) ----

BLUESKY_DEFAULT_SERVICE = "https://bsky.social"
BLUESKY_LABELS = ("sexual", "nudity", "porn", "graphic-media", "suggestive")


def _bluesky_config(config: dict[str, Any]) -> dict[str, str]:
    handle = str(_config_value(config, "handle") or "").strip()
    password = str(_config_value(config, "appPassword", default_env="BLUESKY_APP_PASSWORD") or "").strip()
    service = str(config.get("service") or BLUESKY_DEFAULT_SERVICE).strip().rstrip("/")
    label = str(config.get("label") or "").strip().lower()
    if not handle:
        raise PreparedPublishError("Bluesky account requires handle in config")
    if not password:
        raise PreparedPublishError("Bluesky account requires appPassword or appPasswordEnv")
    if label and label not in BLUESKY_LABELS:
        raise PreparedPublishError(f"Bluesky label must be one of {BLUESKY_LABELS}, got '{label}'")
    return {"handle": handle, "password": password, "service": service, "label": label}


def _bluesky_create_session(http, cfg: dict[str, str]) -> dict[str, str]:
    resp = http.post(
        f"{cfg['service']}/xrpc/com.atproto.server.createSession",
        json={"identifier": cfg["handle"], "password": cfg["password"]},
        timeout=30,
    )
    _raise_for_status(resp)
    data = resp.json()
    access_jwt = data.get("accessJwt") or ""
    did = data.get("did") or ""
    if not access_jwt or not did:
        raise PreparedPublishError(f"Bluesky session missing accessJwt/did for {cfg['handle']}")
    return {"accessJwt": access_jwt, "did": did, "handle": data.get("handle") or cfg["handle"]}


# Bluesky's app.bsky.embed.images blob ceiling. The service advertises a larger
# limit than it enforces in practice; keep a safety margin so a 413 does not
# depend on exactly how the PDS rounds. Oversized media is downscaled rather
# than dropped, because a post with no media is still a failed post.
BLUESKY_MAX_IMAGE_BYTES = 900_000
# Videos: the app.bsky.embed.video lexicon now allows 300,000,000 bytes and a
# 10-minute post, so the safety ceiling sits just under 300 MB (the earlier 90 MB
# figure predated the August 2026 increase and needlessly re-encoded valid clips).
BLUESKY_MAX_VIDEO_BYTES = 295_000_000
BLUESKY_MAX_VIDEO_SECONDS = platform_limits.video_max_seconds("bluesky") or 600.0


def _bluesky_shrink_image(local_path: str, *, max_bytes: int = BLUESKY_MAX_IMAGE_BYTES) -> str:
    """Return a path to an image no larger than ``max_bytes``.

    Re-encodes to JPEG and steps the longest side down until the encoded size
    fits. Returns the original path unchanged when it already fits, when
    Pillow is unavailable, or when downscaling cannot get it under the limit —
    the caller then surfaces the service's own 413 rather than a silent drop.
    """
    from pathlib import Path as _Path

    path = _Path(local_path)
    try:
        if path.stat().st_size <= max_bytes:
            return local_path
    except OSError:
        return local_path

    try:
        import io
        import tempfile

        from PIL import Image
    except Exception:  # noqa: BLE001 — Pillow is optional
        return local_path

    try:
        with Image.open(path) as image:
            image.load()
            if image.mode not in ("RGB", "L"):
                background = Image.new("RGB", image.size, (255, 255, 255))
                if image.mode in ("RGBA", "LA", "P"):
                    rgba = image.convert("RGBA")
                    background.paste(rgba, mask=rgba.split()[-1])
                else:
                    background.paste(image.convert("RGB"))
                working = background
            else:
                working = image.convert("RGB")

            longest = max(working.size)
            for _ in range(8):
                buffer = io.BytesIO()
                working.save(buffer, format="JPEG", quality=85, optimize=True)
                if buffer.tell() <= max_bytes:
                    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                    tmp.write(buffer.getvalue())
                    tmp.close()
                    logger.info(
                        "bluesky: downscaled image %s (%d -> %d bytes, longest %d)",
                        path.name, path.stat().st_size, buffer.tell(), longest,
                    )
                    return tmp.name
                longest = int(longest * 0.8)
                if longest < 320:
                    break
                width = max(1, int(working.width * 0.8))
                height = max(1, int(working.height * 0.8))
                working = working.resize((width, height), Image.LANCZOS)
        return local_path
    except Exception as exc:  # noqa: BLE001 — never let a resize break the post
        logger.warning("bluesky: image downscale failed for %s: %s", local_path, exc)
        return local_path


def _bluesky_shrink_video(local_path: str, *, max_bytes: int = BLUESKY_MAX_VIDEO_BYTES) -> str:
    """Return a path to an mp4 no larger than ``max_bytes`` via ffmpeg.

    Best-effort: returns the original path when ffmpeg is unavailable or the
    re-encode fails, so the caller still sees the service's own error.
    """
    from pathlib import Path as _Path

    path = _Path(local_path)
    try:
        size = path.stat().st_size
    except OSError:
        return local_path
    if size <= max_bytes:
        return local_path

    try:
        import subprocess
        import tempfile

        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        tmp.close()
        # Scale the longest side to 720 and re-encode at a conservative bitrate;
        # faststart so Bluesky can probe it without the whole file. Gated on the
        # shared transcode throttle: this is a full re-encode on the publish
        # path, so without it a batch of oversized clips could start more encodes
        # than the box has cores.
        from myUtils import media_prep as _media_prep

        with _media_prep.encode_slot():
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(path),
                    "-vf", "scale='min(720,iw)':-2",
                    "-c:v", "libx264", "-crf", "28", "-preset", "veryfast",
                    "-c:a", "aac", "-b:a", "96k",
                    "-movflags", "+faststart",
                    tmp.name,
                ],
                capture_output=True,
                timeout=1800,
                check=True,
            )
        if _Path(tmp.name).stat().st_size < size:
            logger.info(
                "bluesky: re-encoded video %s (%d -> %d bytes)",
                path.name, size, _Path(tmp.name).stat().st_size,
            )
            return tmp.name
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("bluesky: video re-encode failed for %s: %s", local_path, exc)
    return local_path


def _bluesky_upload_blob(http, *, jwt: str, service: str, local_path: str, mime: str | None = None) -> dict:
    from pathlib import Path
    content_type = mime or mimetypes.guess_type(local_path)[0] or "application/octet-stream"
    with Path(local_path).open("rb") as handle:
        resp = http.post(
            f"{service}/xrpc/com.atproto.repo.uploadBlob",
            headers={"Authorization": f"Bearer {jwt}", "Content-Type": content_type},
            data=handle,
            timeout=600,
        )
    _raise_for_status(resp)
    return resp.json().get("blob") or {}


def _bluesky_video_aspect_ratio(local_path: str) -> dict | None:
    """Best-effort width/height for a local video via ffprobe.

    Bluesky's embed.video aspectRatio is optional; when ffprobe is unavailable
    or the file is missing we return None and let the service infer it.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", local_path],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            return None
        text = result.stdout.strip()
        if not text:
            return None
        width, height = text.split("x")
        return {"width": int(width), "height": int(height)}
    except Exception:  # noqa: BLE001 — best-effort
        return None


def _bluesky_fetch_to_temp(http, *, url: str) -> str:
    """Download a remote media item to a temp file (for uploadBlob)."""
    import tempfile
    resp = http.get(url, timeout=300)
    resp.raise_for_status()
    suffix = Path(urlparse(url).path).suffix or ".bin"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(resp.content)
    tmp.close()
    return tmp.name


def _bluesky_message_and_media(payload: dict) -> tuple[str, list[dict], list[dict]]:
    """Return (message, image_items, video_items) extracted from a publish payload.

    Videos and images are split because Bluesky embeds them differently:
    a single video uses ``app.bsky.embed.video`` (up to 300MB mp4), while
    images use ``app.bsky.embed.images`` (up to 4 images).
    """
    draft = payload.get("draft") or {}
    message = str(draft.get("message") or payload.get("message") or "").strip()
    if not message:
        raise PreparedPublishError("Bluesky publish requires a message (300 char max)")
    hashtags = draft.get("hashtags") or []
    if isinstance(hashtags, str):
        hashtags = [h.strip() for h in hashtags.split(",") if h.strip()]
    for tag in hashtags:
        tag_text = str(tag).lstrip("#")
        if tag_text and f"#{tag_text}" not in message:
            message = f"{message} #{tag_text}"
    # Bluesky's hard post length is 300 chars (graphemes).
    if len(message) > 300:
        message = message[:297].rstrip() + "…"
    media = _extract_media(payload)
    return message, list(media["images"]), list(media["videos"])


def publish_bluesky_sync(account, payload: dict, *, session=None) -> list[dict[str, Any]]:
    """Publish a text/image/video post to Bluesky via the AT Protocol."""
    config = account.config or {}
    cfg = _bluesky_config(config)
    http = _get_session(session)

    message, image_items, video_items = _bluesky_message_and_media(payload)
    artifacts = payload.get("artifacts") or []
    if artifacts and not (image_items or video_items):
        raise PreparedPublishError("Bluesky media artifacts were supplied but none is a supported image/video")
    if len(image_items) > 4 or len(video_items) > 1 or (image_items and video_items):
        raise PreparedPublishError("Bluesky posts support up to four images or one video, not mixed media")
    for item in [*video_items, *image_items]:
        local = Path(str(item.get("local_path") or ""))
        public_url = str(item.get("public_url") or "").strip()
        if not (local.is_file() and local.stat().st_size > 0) and not public_url:
            raise PreparedPublishError("Bluesky media artifact has no readable local file or public URL")
    auth = _bluesky_create_session(http, cfg)

    record: dict[str, Any] = {
        "text": message,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }

    uploaded_media: dict[str, Any] = {"images": 0, "videos": 0}

    def _resolve_local(item: dict) -> tuple[str | None, str | None]:
        local_path = item.get("local_path") or ""
        public_url = item.get("public_url") or ""
        if local_path and Path(local_path).is_file() and Path(local_path).stat().st_size > 0:
            return local_path, None
        if public_url:
            tmp = _bluesky_fetch_to_temp(http, url=public_url)
            if Path(tmp).stat().st_size <= 0:
                Path(tmp).unlink(missing_ok=True)
                raise PreparedPublishError("Bluesky media URL returned an empty file")
            return tmp, tmp
        raise PreparedPublishError("Bluesky media artifact has no readable local file or public URL")

    if video_items:
        # Bluesky allows a single video per post.
        video = video_items[0]
        local_path, tmp_path = _resolve_local(video)
        shrunk_path = None
        try:
            try:
                duration = media_pipeline.probe_video_duration(local_path)
            except Exception:  # noqa: BLE001 - a probe failure is not a rejection
                duration = None
            # ``>`` (not ``>=``) so a video of exactly 600 s is accepted: that
            # matches Bluesky's documented 600 s cap, which rejects only clips
            # strictly longer than 10 minutes.
            if duration and duration > BLUESKY_MAX_VIDEO_SECONDS:
                over_by = duration - BLUESKY_MAX_VIDEO_SECONDS
                raise PreparedPublishError(
                    f"Bluesky video duration {duration:.1f}s exceeds the "
                    f"{BLUESKY_MAX_VIDEO_SECONDS:.0f}s limit by {over_by:.1f}s; "
                    "re-encode or split a shorter cut"
                )
            shrunk_path = _bluesky_shrink_video(local_path)
            blob = _bluesky_upload_blob(
                http, jwt=auth["accessJwt"], service=cfg["service"],
                local_path=shrunk_path, mime="video/mp4",
            )
            if not blob:
                raise PreparedPublishError("Bluesky video upload returned no blob")
            embed: dict[str, Any] = {
                "$type": "app.bsky.embed.video",
                "video": blob,
            }
            ratio = _bluesky_video_aspect_ratio(local_path)
            if ratio:
                embed["aspectRatio"] = ratio
            if str(video.get("alt_text") or "").strip():
                embed["alt"] = str(video["alt_text"]).strip()
            record["embed"] = embed
            uploaded_media["videos"] = 1
        finally:
            if shrunk_path and shrunk_path != local_path:
                Path(shrunk_path).unlink(missing_ok=True)
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)
    elif image_items:
        blobs: list[dict] = []
        alt_texts: list[str] = []
        for item in image_items[:4]:
            local_path, tmp_path = _resolve_local(item)
            shrunk_path = None
            try:
                shrunk_path = _bluesky_shrink_image(local_path)
                blob = _bluesky_upload_blob(http, jwt=auth["accessJwt"], service=cfg["service"], local_path=shrunk_path)
                if not blob:
                    raise PreparedPublishError("Bluesky image upload returned no blob")
                blobs.append(blob)
                alt_texts.append(str(item.get("alt_text") or "").strip())
            finally:
                if shrunk_path and shrunk_path != local_path:
                    Path(shrunk_path).unlink(missing_ok=True)
                if tmp_path:
                    Path(tmp_path).unlink(missing_ok=True)
        images = [
            {"image": blob, "alt": alt_texts[i] or ""}
            for i, blob in enumerate(blobs)
        ]
        record["embed"] = {
            "$type": "app.bsky.embed.images",
            "images": images,
        }
        uploaded_media["images"] = len(blobs)

    if cfg["label"]:
        record["labels"] = {
            "$type": "com.atproto.label.defs#selfLabels",
            "values": [{"val": cfg["label"]}],
        }

    create_resp = http.post(
        f"{cfg['service']}/xrpc/com.atproto.repo.createRecord",
        headers={"Authorization": f"Bearer {auth['accessJwt']}", "Content-Type": "application/json"},
        json={
            "repo": auth["did"],
            "collection": "app.bsky.feed.post",
            "record": record,
        },
        timeout=120,
    )
    _raise_for_status(create_resp)
    result = create_resp.json()
    return [{
        "uri": result.get("uri"),
        "cid": result.get("cid"),
        "handle": auth["handle"],
        "did": auth["did"],
        "chars": len(message),
        **uploaded_media,
    }]


def validate_bluesky_config_live(config: dict[str, Any], *, session=None) -> dict:
    """Validate Bluesky config by creating a session (read-only check)."""
    cfg = _bluesky_config(config)
    http = _get_session(session)
    auth = _bluesky_create_session(http, cfg)
    return {
        "handle": auth["handle"],
        "did": auth["did"],
        "authenticated": True,
        "label": cfg["label"] or None,
    }
