"""Prepared-campaign publishers for API-driven platforms."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import mimetypes
import os
from pathlib import Path
from typing import Any

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


class PreparedPublishError(RuntimeError):
    """Raised when a prepared publish cannot be completed."""


def _get_session(session=None):
    if session is not None:
        return session
    if requests is None:
        raise RuntimeError("requests is required for prepared publishers")
    return requests.Session()


def _raise_for_status(response) -> None:
    if hasattr(response, "raise_for_status"):
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


def publish_facebook_sync(account, payload: dict, *, session=None) -> list[Any]:
    config = account.config or {}
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
    config = account.config or {}
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
    return {"container_id": container_id, "publish": _response_payload(publish_response)}


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


def publish_threads_sync(account, payload: dict, *, session=None) -> dict:
    config = account.config or {}
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
    return {"container_id": container_id, "publish": _response_payload(publish_response)}


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
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json={},
        timeout=120,
    )
    _raise_for_status(response)
    payload = _response_payload(response)
    return payload


def publish_tiktok_sync(account, payload: dict, *, session=None) -> dict:
    config = dict(account.config or {})
    http = _get_session(session)
    access_token, updated_config = _ensure_tiktok_access_token(config, session=http)
    creator_info = query_tiktok_creator_info(config, access_token=access_token, session=http)
    if updated_config is not None:
        updated_config = _apply_tiktok_token_payload(updated_config, {'access_token': access_token}, creator_info)
    media = _extract_media(payload)
    message = _payload_message(payload)
    publish_mode = str(config.get("publishMode") or "direct").strip().lower()
    post_mode = "DIRECT_POST" if publish_mode == "direct" else "MEDIA_UPLOAD"
    privacy_level = str(config.get("privacyLevel") or "PUBLIC_TO_EVERYONE")

    if media["videos"]:
        public_url = media["videos"][0].get("public_url") or ""
        if not public_url:
            raise PreparedPublishError("TikTok video publish requires a public_url")
        request_body = {
            "post_info": {
                "title": message[:2200],
                "privacy_level": privacy_level,
                "disable_duet": bool(config.get("disableDuet", False)),
                "disable_comment": bool(config.get("disableComment", False)),
                "disable_stitch": bool(config.get("disableStitch", False)),
            },
            "source_info": {
                "source": "PULL_FROM_URL",
                "video_url": public_url,
            },
            "post_mode": post_mode,
        }
        if config.get("videoCoverTimestampMs") not in (None, ""):
            request_body["post_info"]["video_cover_timestamp_ms"] = int(config["videoCoverTimestampMs"])
        response = http.post(
            TIKTOK_VIDEO_INIT_URL,
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
        }

    if media["images"]:
        public_urls = [item.get("public_url") or "" for item in media["images"][:35]]
        if not all(public_urls):
            raise PreparedPublishError("TikTok photo publish requires public image URLs")
        request_body = {
            "post_info": {
                "title": _message_title(payload),
                "description": message[:4000],
                "privacy_level": privacy_level,
                "disable_comment": bool(config.get("disableComment", False)),
                "auto_add_music": bool(config.get("autoAddMusic", True)),
            },
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
        }

    raise PreparedPublishError("TikTok publish requires at least one video or image")


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


def publish_reddit_sync(account, payload: dict, *, session=None) -> list[Any]:
    config = dict(account.config or {})
    config.setdefault("accountName", getattr(account, "account_name", "sau"))
    subreddits = config.get("subreddits") or []
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


def _google_access_token(config: dict[str, Any], *, session=None) -> str:
    access_token = str(_config_value(config, "accessToken") or "").strip()
    if access_token:
        return access_token

    client_id = str(_config_value(config, "clientId") or "").strip()
    client_secret = str(_config_value(config, "clientSecret") or "").strip()
    refresh_token = str(_config_value(config, "refreshToken") or "").strip()
    if not client_id or not client_secret or not refresh_token:
        raise PreparedPublishError(
            "YouTube publish requires accessToken or clientId/clientSecret/refreshToken"
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
