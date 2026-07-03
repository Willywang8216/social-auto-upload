"""Prepared-campaign publishers for API-driven platforms."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

from myUtils import media_pipeline
from myUtils import tiktok_auth

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - environment-specific
    requests = None

TELEGRAM_API_ROOT = "https://api.telegram.org/bot{token}/{method}"
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_SUBMIT_URL = "https://oauth.reddit.com/api/submit"
REDDIT_ME_URL = "https://oauth.reddit.com/api/v1/me"
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
TIKTOK_API_ROOT = "https://open.tiktokapis.com"
TIKTOK_CREATOR_INFO_URL = f"{TIKTOK_API_ROOT}/v2/post/publish/creator_info/query/"
TIKTOK_VIDEO_INIT_URL = f"{TIKTOK_API_ROOT}/v2/post/publish/video/init/"
TIKTOK_CONTENT_INIT_URL = f"{TIKTOK_API_ROOT}/v2/post/publish/content/init/"
TIKTOK_STATUS_FETCH_URL = f"{TIKTOK_API_ROOT}/v2/post/publish/status/fetch/"
TIKTOK_MAX_PULL_FROM_URL_BYTES = 4 * 1024 * 1024 * 1024
TIKTOK_MIN_VIDEO_SECONDS = 3.0
TIKTOK_MAX_VIDEO_SECONDS = 60 * 60  # 60 minutes (TikTok expanded from 10 min)
TIKTOK_MAX_CAPTION_CHARS = 2200
TIKTOK_ALLOWED_VIDEO_SUFFIXES = {".mp4", ".webm"}


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
        if detail:
            raise PreparedPublishError(f"HTTP {status}: {detail}") from None
        raise PreparedPublishError(_redact_tokens(str(exc))) from None


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
    if code == "unaudited_client_can_only_post_to_private_accounts":
        raise PreparedPublishError(
            "TikTok app is in development mode — can only post to private accounts. "
            "Submit your app for review at developers.tiktok.com or set your TikTok account to private for testing."
        )
    if code == "url_ownership_unverified":
        raise PreparedPublishError(
            "TikTok requires domain ownership verification for PULL_FROM_URL. "
            "Using FILE_UPLOAD mode instead."
        )
    if message:
        raise PreparedPublishError(f"TikTok API error ({code}): {message}")
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


def _extract_media(payload: dict) -> dict[str, list[dict[str, str]]]:
    items_by_key: dict[str, dict[str, str]] = {}
    for artifact in payload.get("artifacts", []) or []:
        local_path = artifact.get("local_path") or ""
        public_url = artifact.get("public_url") or ""
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


def validate_telegram_config_live(config: dict[str, Any], *, session=None) -> dict:
    token = str(_config_value(config, "botToken", default_env="TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = str(config.get("chatId") or "").strip()
    if not token:
        raise PreparedPublishError("Telegram validation requires botToken or botTokenEnv")
    if not chat_id:
        raise PreparedPublishError("Telegram validation requires chatId")

    http = _get_session(session)
    bot_response = http.post(
        TELEGRAM_API_ROOT.format(token=token, method="getMe"),
        data={},
        timeout=120,
    )
    _raise_for_status(bot_response)
    chat_response = http.post(
        TELEGRAM_API_ROOT.format(token=token, method="getChat"),
        data={"chat_id": chat_id},
        timeout=120,
    )
    _raise_for_status(chat_response)
    return {"bot": _response_payload(bot_response), "chat": _response_payload(chat_response)}


def publish_telegram_sync(account, payload: dict, *, session=None) -> list[Any]:
    config = account.config or {}
    token = str(_config_value(config, "botToken", default_env="TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = str(config.get("chatId") or "").strip()
    if not token:
        raise PreparedPublishError("Telegram publish requires botToken or botTokenEnv")
    if not chat_id:
        raise PreparedPublishError("Telegram publish requires chatId")

    http = _get_session(session)
    message = _payload_message(payload)
    caption, overflow_message = _telegram_caption_chunks(message)
    parse_mode = str(config.get("parseMode") or "").strip() or None
    silent = "true" if bool(config.get("silent", False)) else "false"
    disable_preview = "true" if bool(config.get("disableWebPreview", False)) else "false"
    media = _extract_media(payload)
    attachments = [*media["videos"], *media["images"]]
    responses: list[Any] = []

    def _post(method: str, **kwargs):
        response = http.post(TELEGRAM_API_ROOT.format(token=token, method=method), **kwargs)
        _raise_for_status(response)
        responses.append(response)
        return response

    if not attachments:
        data = {
            "chat_id": chat_id,
            "text": message,
            "disable_notification": silent,
            "disable_web_page_preview": disable_preview,
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        _post("sendMessage", data=data, timeout=120)
        return responses

    if len(attachments) == 1:
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
                _post(method, data=data, files={field_name: (Path(local_path).name, handle)}, timeout=600)
        else:
            data[field_name] = item.get("public_url")
            _post(method, data=data, timeout=120)
        if overflow_message:
            _post(
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
        return responses

    media_payload = []
    files = {}
    open_files = []
    try:
        for index, item in enumerate(attachments[:10]):
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

    if overflow_message:
        _post(
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
    return responses


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

    http = _get_session(session)
    message = _payload_message(payload) or _message_title(payload)
    media = _extract_media(payload)
    attachments = [*media["videos"], *media["images"]][:10]
    files = {}
    open_files = []
    try:
        content_lines = [message] if message else []
        for index, item in enumerate(attachments):
            local_path = item.get("local_path")
            public_url = item.get("public_url")
            if local_path and Path(local_path).exists():
                handle = Path(local_path).open("rb")
                open_files.append(handle)
                files[f"files[{index}]"] = (Path(local_path).name, handle)
            elif public_url:
                content_lines.append(public_url)

        if files:
            response = http.post(
                webhook_url,
                data={"payload_json": json.dumps({"content": "\n".join(content_lines).strip()}, ensure_ascii=False)},
                files=files,
                timeout=600,
            )
        else:
            response = http.post(
                webhook_url,
                json={"content": "\n".join(content_lines).strip()},
                timeout=120,
            )
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
        refreshed = _meta_auth.exchange_for_long_lived_token(access_token=meta_user_token)
    except Exception:
        return config  # best-effort: keep using the old token

    if not refreshed or not refreshed.get("access_token"):
        return config

    updated = dict(config)
    updated["metaUserAccessToken"] = refreshed["access_token"]
    expires_in = refreshed.get("expires_in")
    if expires_in not in (None, ""):
        updated["metaUserAccessTokenExpiresAt"] = (
            datetime.now() + timedelta(seconds=int(expires_in))
        ).isoformat(timespec="seconds")
    updated["accessTokenUpdatedAt"] = datetime.now().isoformat(timespec="seconds")
    return updated


def _maybe_refresh_facebook_token(config: dict[str, Any], *, session=None) -> dict[str, Any]:
    """Refresh Facebook user token if expired or about to expire."""
    return _maybe_refresh_meta_token(config, "facebook", session=session)


def _maybe_refresh_instagram_token(config: dict[str, Any], *, session=None) -> dict[str, Any]:
    """Refresh Instagram user token if expired or about to expire."""
    return _maybe_refresh_meta_token(config, "instagram", session=session)


def publish_facebook_sync(account, payload: dict, *, session=None) -> list[Any]:
    config = dict(account.config or {})
    config = _maybe_refresh_facebook_token(config, session=session)
    _check_meta_token_not_expired(config, "Facebook")
    page_id = str(config.get("pageId") or "").strip()
    access_token = str(_config_value(config, "accessToken") or "").strip()
    if not page_id:
        raise PreparedPublishError("Facebook publish requires pageId")
    if not access_token:
        raise PreparedPublishError("Facebook publish requires accessToken or accessTokenEnv")

    http = _get_session(session)
    message = _payload_message(payload)
    title = _message_title(payload)
    media = _extract_media(payload)
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
        # Extract the post_id from the last response; on /feed Facebook
        # returns "{page_id}_{post_id}" already; on /photos it's two
        # separate keys.
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
    config = _maybe_refresh_instagram_token(config, session=session)
    _check_meta_token_not_expired(config, "Instagram")
    ig_user_id = str(config.get("igUserId") or "").strip()
    access_token = str(_config_value(config, "accessToken") or "").strip()
    if not ig_user_id:
        raise PreparedPublishError("Instagram publish requires igUserId")
    if not access_token:
        raise PreparedPublishError("Instagram publish requires accessToken or accessTokenEnv")

    http = _get_session(session)
    message = _payload_message(payload)
    media = _extract_media(payload)

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

    publish_response = http.post(
        f"{FACEBOOK_GRAPH_ROOT}/{ig_user_id}/media_publish",
        data={"creation_id": container_id, "access_token": access_token},
        timeout=120,
    )
    _raise_for_status(publish_response)
    publish_body = _response_payload(publish_response)
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
    return {"container_id": container_id, "publish": publish_body}


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
    access_token = str(config.get("accessToken") or "").strip()
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
        from datetime import datetime, timedelta
        updated["accessTokenExpiresAt"] = (datetime.now() + timedelta(seconds=int(expires_in))).isoformat(timespec="seconds")
    updated["accessTokenUpdatedAt"] = datetime.now().isoformat(timespec="seconds")
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
    media = _extract_media(payload)

    if media["videos"]:
        public_url = media["videos"][0].get("public_url") or ""
        if not public_url:
            raise PreparedPublishError("Threads video publish requires a public_url")
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
            raise PreparedPublishError("TikTok video uploads currently support up to 1 GB")

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


def _tiktok_file_upload(http, video_path: Path, upload_url: str, chunk_size: int) -> None:
    """Upload a video file to TikTok via chunked PUT to the presigned upload_url."""
    import mimetypes

    total_size = video_path.stat().st_size
    mime_type = mimetypes.guess_type(video_path.name)[0] or "video/mp4"

    with video_path.open("rb") as fh:
        offset = 0
        while offset < total_size:
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
    http = _get_session(session)
    access_token, updated_config = _ensure_tiktok_access_token(config, session=http)
    creator_info = query_tiktok_creator_info(config, access_token=access_token, session=http)
    # Store creator_info's max duration for video validation
    ci_data = creator_info.get("data") or creator_info
    max_dur = ci_data.get("max_video_post_duration_sec")
    if max_dur:
        config["_tiktok_max_video_duration_sec"] = int(max_dur)
    if updated_config is not None:
        updated_config = _apply_tiktok_token_payload(updated_config, {'access_token': access_token}, creator_info)
    media = _extract_media(payload)
    message = _payload_message(payload)
    # Per-publish override has precedence over the account-level default:
    # the Publish Center exposes an explicit "Direct post (skip draft)"
    # toggle that, when checked, must reach this point true regardless of
    # what publishMode the user picked on the account form. This is what
    # the TikTok app review explicitly asks us to demonstrate alongside
    # a confirmation modal in the UI.
    payload_direct_post = payload.get("tiktokDirectPost")
    if isinstance(payload_direct_post, bool):
        publish_mode = "direct" if payload_direct_post else "draft"
    else:
        publish_mode = str(config.get("publishMode") or "direct").strip().lower()
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
                log.info("TikTok PULL_FROM_URL error: code=%s, status=%s, body=%s",
                         error_code, response.status_code, resp_json)
                if error_code == "url_ownership_unverified":
                    log.warning("TikTok PULL_FROM_URL failed (domain not verified), falling back to FILE_UPLOAD")
                else:
                    _raise_tiktok_error(response)
            except PreparedPublishError:
                raise
            except Exception as e:
                log.warning("Failed to parse TikTok error response: %s", e)
                _raise_tiktok_error(response)

        if local_path and Path(local_path).expanduser().resolve().is_file():
            # FILE_UPLOAD for local files (non-Direct-Post or no public URL)
            video_path = Path(local_path).expanduser().resolve()
            file_size = video_path.stat().st_size
            # TikTok requires chunk_size between 5MB-64MB, but for small files
            # we must use file_size as chunk_size with total_chunk_count=1
            if file_size <= TIKTOK_FILE_UPLOAD_CHUNK_SIZE:
                # Small file: single chunk, use actual file size
                chunk_size = file_size
                total_chunks = 1
            elif file_size <= TIKTOK_FILE_UPLOAD_MAX_CHUNK_SIZE:
                # Medium file: single chunk
                chunk_size = file_size
                total_chunks = 1
            else:
                # Large file: multiple chunks
                chunk_size = TIKTOK_FILE_UPLOAD_MAX_CHUNK_SIZE
                total_chunks = -(-file_size // chunk_size)  # ceil division
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
            _tiktok_file_upload(http, video_path, upload_url, chunk_size)
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
        _raise_for_status(response)
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

X_API_ROOT = "https://api.twitter.com"
X_UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"
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
        params={**oauth_params, **(oauth_params_extra or {})},
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


def _x_media_upload(*, file_path: str, api_key: str, api_key_secret: str, access_token: str, access_token_secret: str, session=None) -> str:
    """Upload a media file to Twitter and return the media_id."""
    http = _get_session(session)
    file_size = Path(file_path).stat().st_size
    mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"

    # INIT
    auth_header = _x_auth_header(
        method="POST", url=X_UPLOAD_URL,
        consumer_key=api_key, token=access_token,
        consumer_secret=api_key_secret, token_secret=access_token_secret,
    )
    init_resp = http.post(
        X_UPLOAD_URL,
        headers={"Authorization": auth_header},
        data={
            "command": "INIT",
            "total_bytes": str(file_size),
            "media_type": mime_type,
        },
        timeout=120,
    )
    _raise_for_status(init_resp)
    media_id = str(init_resp.json().get("media_id_string") or "")

    # APPEND
    with Path(file_path).open("rb") as fh:
        auth_header = _x_auth_header(
            method="POST", url=X_UPLOAD_URL,
            consumer_key=api_key, token=access_token,
            consumer_secret=api_key_secret, token_secret=access_token_secret,
        )
        append_resp = http.post(
            X_UPLOAD_URL,
            headers={"Authorization": auth_header},
            data={"command": "APPEND", "media_id": media_id, "segment_index": "0"},
            files={"media": (Path(file_path).name, fh, mime_type)},
            timeout=600,
        )
        _raise_for_status(append_resp)

    # FINALIZE
    auth_header = _x_auth_header(
        method="POST", url=X_UPLOAD_URL,
        consumer_key=api_key, token=access_token,
        consumer_secret=api_key_secret, token_secret=access_token_secret,
    )
    finalize_resp = http.post(
        X_UPLOAD_URL,
        headers={"Authorization": auth_header},
        data={"command": "FINALIZE", "media_id": media_id},
        timeout=120,
    )
    _raise_for_status(finalize_resp)
    return media_id


def _maybe_refresh_twitter_token(config: dict[str, Any], *, session=None) -> dict[str, Any]:
    """Refresh Twitter OAuth 2.0 token if expired or about to expire. Returns possibly-updated config."""
    refresh_token = str(config.get("refreshToken") or "").strip()
    if not refresh_token or config.get("twitterAuthType") != "api":
        return config
    expires_at = str(config.get("accessTokenExpiresAt") or "").strip()
    if expires_at:
        from datetime import datetime, timedelta, timezone
        try:
            exp = datetime.fromisoformat(expires_at)
            now = datetime.now(timezone.utc) if exp.tzinfo else datetime.now()
            if now < exp - timedelta(seconds=300):
                return config  # still valid, plenty of margin
        except (ValueError, TypeError):
            pass  # unparseable → try refresh
    try:
        result = refresh_twitter_access_token(config, session=session)
    except Exception:
        return config  # best-effort: fall through with old token
    updated = dict(config)
    updated["accessToken"] = result["access_token"]
    updated["refreshToken"] = result.get("refresh_token") or refresh_token
    updated["scope"] = result.get("scope") or config.get("scope", "")
    expires_in = result.get("expires_in")
    if expires_in not in (None, ""):
        from datetime import datetime, timedelta
        updated["accessTokenExpiresAt"] = (datetime.now() + timedelta(seconds=int(expires_in))).isoformat(timespec="seconds")
    updated["accessTokenUpdatedAt"] = datetime.now().isoformat(timespec="seconds")
    me = result.get("me") or {}
    user_data = me.get("data", me) if isinstance(me, dict) else {}
    if isinstance(user_data, dict):
        if user_data.get("id"):
            updated["twitterUserId"] = str(user_data["id"])
        if user_data.get("username"):
            updated["twitterUserName"] = str(user_data["username"])
        if user_data.get("name"):
            updated["twitterDisplayName"] = str(user_data["name"])
    return updated


def publish_twitter_sync(account, payload: dict, *, session=None) -> dict[str, Any]:
    """Publish a tweet with optional media via the X API v2.

    Returns ``{"results": [...], "updated_config": {...}}`` so the caller can
    persist refreshed tokens back to the database.
    """
    config = dict(account.config or {})

    # Lazy-refresh expired OAuth 2.0 token before publishing.
    config = _maybe_refresh_twitter_token(config, session=session)

    # Check if we have OAuth 2.0 token (from PKCE flow)
    oauth2_token = str(config.get("accessToken") or "").strip()
    has_oauth1 = all([
        str(_config_value(config, "apiKey", default_env="X_API_KEY") or "").strip(),
        str(_config_value(config, "apiKeySecret", default_env="X_API_KEY_SECRET") or "").strip(),
        str(_config_value(config, "accessToken", default_env="X_ACCESS_TOKEN") or "").strip(),
        str(_config_value(config, "accessTokenSecret", default_env="X_ACCESS_TOKEN_SECRET") or "").strip(),
    ])

    if not oauth2_token and not has_oauth1:
        raise PreparedPublishError(
            "Twitter publish requires either OAuth 2.0 tokens (via Connect button) or OAuth 1.0a credentials"
        )

    http = _get_session(session)
    message = _payload_message(payload)
    media = _extract_media(payload)

    # Upload media first (v1.1 endpoint requires OAuth 1.0a)
    media_ids = []
    if media["images"][:4] + media["videos"][:1]:
        api_key = str(_config_value(config, "apiKey", default_env="X_API_KEY") or "").strip()
        api_key_secret = str(_config_value(config, "apiKeySecret", default_env="X_API_KEY_SECRET") or "").strip()
        access_token = str(_config_value(config, "accessToken", default_env="X_ACCESS_TOKEN") or "").strip()
        access_token_secret = str(_config_value(config, "accessTokenSecret", default_env="X_ACCESS_TOKEN_SECRET") or "").strip()
        if not all([api_key, api_key_secret, access_token, access_token_secret]):
            raise PreparedPublishError(
                "Twitter media upload requires OAuth 1.0a credentials (apiKey, apiKeySecret, accessToken, accessTokenSecret)"
            )
        for item in media["images"][:4] + media["videos"][:1]:
            local_path = item.get("local_path")
            if local_path:
                mid = _x_media_upload(
                    file_path=local_path,
                    api_key=api_key, api_key_secret=api_key_secret,
                    access_token=access_token, access_token_secret=access_token_secret,
                    session=http,
                )
                media_ids.append(mid)

    # Create tweet (v2 endpoint)
    tweet_data: dict[str, Any] = {"text": message}
    if media_ids:
        tweet_data["media"] = {"media_ids": media_ids}

    headers = _twitter_auth_headers(config, method="POST", url=X_TWEET_URL)
    headers["Content-Type"] = "application/json"
    resp = http.post(
        X_TWEET_URL,
        headers=headers,
        data=json.dumps(tweet_data),
        timeout=120,
    )
    _raise_for_status(resp)
    return {"results": [_response_payload(resp)], "updated_config": config}


def _twitter_auth_headers(config: dict[str, Any], *, method: str, url: str) -> dict[str, str]:
    """Return Authorization headers for Twitter, preferring OAuth 2.0 over 1.0a."""
    # OAuth 2.0 Bearer token (from OAuth PKCE flow stored in config)
    oauth2_token = str(config.get("accessToken") or "").strip()
    if oauth2_token:
        return {"Authorization": f"Bearer {oauth2_token}"}

    # OAuth 1.0a (from env vars or config overrides)
    api_key = str(_config_value(config, "apiKey", default_env="X_API_KEY") or "").strip()
    api_key_secret = str(_config_value(config, "apiKeySecret", default_env="X_API_KEY_SECRET") or "").strip()
    access_token = str(_config_value(config, "accessToken", default_env="X_ACCESS_TOKEN") or "").strip()
    access_token_secret = str(_config_value(config, "accessTokenSecret", default_env="X_ACCESS_TOKEN_SECRET") or "").strip()

    if all([api_key, api_key_secret, access_token, access_token_secret]):
        return {
            "Authorization": _x_auth_header(
                method=method, url=url,
                consumer_key=api_key, token=access_token,
                consumer_secret=api_key_secret, token_secret=access_token_secret,
            )
        }

    raise PreparedPublishError(
        "Twitter requires either OAuth 2.0 tokens (via Connect button) or OAuth 1.0a credentials (apiKey, apiKeySecret, accessToken, accessTokenSecret)"
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

    user_info = _x_auth.fetch_user_info(access_token=access_token, session=http) if access_token else {}

    return {
        "access_token": access_token,
        "refresh_token": token_payload.get("refresh_token") or refresh_token,
        "expires_in": token_payload.get("expires_in"),
        "scope": token_payload.get("scope", ""),
        "token_type": token_payload.get("token_type", "bearer"),
        "me": user_info,
    }


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
    message = _payload_message(payload)
    media = _extract_media(payload)
    public_url = ""
    if media["videos"]:
        public_url = media["videos"][0].get("public_url") or ""
    if not public_url and media["images"]:
        public_url = media["images"][0].get("public_url") or ""
    title = _message_title(payload)
    results = []
    for subreddit in subreddits:
        data = {
            "api_type": "json",
            "sr": subreddit,
            "title": title[:300],
            "resubmit": "false",
            "sendreplies": "true",
            "kind": "link" if public_url else "self",
        }
        if public_url:
            data["url"] = public_url
        else:
            data["text"] = message
        response = http.post(REDDIT_SUBMIT_URL, headers=headers, data=data, timeout=120)
        _raise_for_status(response)
        body = response.json()
        errors = body.get("json", {}).get("errors", [])
        if errors:
            raise PreparedPublishError(f"Reddit submit failed for r/{subreddit}: {errors}")
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


def publish_youtube_sync(account, payload: dict, *, session=None) -> dict:
    config = dict(account.config or {})
    channel_id = str(config.get("channelId") or "").strip()
    if not channel_id:
        raise PreparedPublishError("YouTube publish requires channelId")

    media = _extract_media(payload)
    if not media["videos"] or not media["videos"][0].get("local_path"):
        raise PreparedPublishError("YouTube publish requires a local video artifact")
    video_path = Path(media["videos"][0]["local_path"])
    if not video_path.exists():
        raise PreparedPublishError(f"YouTube video artifact not found: {video_path}")

    http = _get_session(session)
    access_token = _google_access_token(config, session=http)
    headers = {"Authorization": f"Bearer {access_token}"}
    title = _message_title(payload)
    description = _payload_message(payload)
    metadata = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "channelId": channel_id,
            "categoryId": str(config.get("categoryId") or "22"),
        },
        "status": {
            "privacyStatus": str(config.get("privacyStatus") or "private"),
            "selfDeclaredMadeForKids": bool(config.get("madeForKids", False)),
        },
    }
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


# ---- NW/SW Blog (sexualwill.com REST API) ----

def publish_nw_sw_blog_sync(
    account,
    payload: dict,
    *,
    session=None,
) -> list[dict[str, Any]]:
    """Publish an MDX post to the NW/SW blog REST API."""
    config = account.config or {}
    api_base = str(_config_value(config, "apiBase") or "").strip().rstrip("/")
    api_token = str(_config_value(config, "apiToken", default_env="SAU_NW_SW_BLOG_API_TOKEN") or "").strip()
    persona = str(_config_value(config, "persona") or "").strip()
    locale = str(_config_value(config, "locale") or "en").strip()

    if not api_base:
        raise PreparedPublishError("NW/SW Blog account requires apiBase in config")
    if not api_token:
        raise PreparedPublishError("NW/SW Blog account requires apiToken or apiTokenEnv")
    if persona not in ("sexualwill", "nakedwill"):
        raise PreparedPublishError(f"NW/SW Blog persona must be 'sexualwill' or 'nakedwill', got '{persona}'")
    if locale not in ("en", "zh"):
        raise PreparedPublishError(f"NW/SW Blog locale must be 'en' or 'zh', got '{locale}'")

    message = _payload_message(payload)
    if not message:
        raise PreparedPublishError("NW/SW Blog publish requires a message (MDX body)")

    title = str((payload.get("draft") or {}).get("title") or "").strip()
    if not title:
        title = _message_title(payload)

    http = _get_session(session)
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    body = {
        "title": title,
        "content": message,
        "persona": persona,
        "locale": locale,
        "status": "published",
    }

    resp = http.post(f"{api_base}/api/admin/posts", headers=headers, json=body, timeout=60)
    _raise_for_status(resp)
    result = resp.json()
    return [result]


def validate_nw_sw_blog_config_live(config: dict[str, Any], *, session=None) -> dict:
    """Validate NW/SW Blog config by listing posts (read-only check)."""
    api_base = str(_config_value(config, "apiBase") or "").strip().rstrip("/")
    api_token = str(_config_value(config, "apiToken", default_env="SAU_NW_SW_BLOG_API_TOKEN") or "").strip()
    if not api_base or not api_token:
        raise PreparedPublishError("NW/SW Blog validation requires apiBase and apiToken")
    http = _get_session(session)
    resp = http.get(
        f"{api_base}/api/admin/posts",
        headers={"Authorization": f"Bearer {api_token}"},
        params={"limit": 1},
        timeout=30,
    )
    _raise_for_status(resp)
    data = resp.json()
    return {"posts_accessible": True, "count": len(data) if isinstance(data, list) else data.get("total", 0)}
