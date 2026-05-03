"""Prepared-campaign publishers for API-driven platforms."""

from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
from typing import Any

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - environment-specific
    requests = None

TELEGRAM_API_ROOT = "https://api.telegram.org/bot{token}/{method}"
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_SUBMIT_URL = "https://oauth.reddit.com/api/submit"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_RESUMABLE_UPLOAD_URL = (
    "https://www.googleapis.com/upload/youtube/v3/videos"
    "?uploadType=resumable&part=snippet,status"
)
YOUTUBE_PLAYLIST_INSERT_URL = (
    "https://www.googleapis.com/youtube/v3/playlistItems?part=snippet"
)


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
