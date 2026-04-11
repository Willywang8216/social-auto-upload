import base64
import hashlib
import hmac
import importlib
import json
import mimetypes
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any

DIRECT_PUBLISHERS_FILENAME = "direct_publishers.json"
DIRECT_PUBLISHER_PLATFORMS = {"telegram", "discord", "reddit", "twitter", "facebook", "threads", "youtube", "tiktok"}
ASYNC_DIRECT_PUBLISH_PLATFORMS = {"threads", "youtube", "tiktok"}


def get_direct_publishers_storage_path(base_dir: Path) -> Path:
    return base_dir / "db" / DIRECT_PUBLISHERS_FILENAME


def get_direct_publishers_config(base_dir: Path) -> dict[str, Any]:
    path = get_direct_publishers_storage_path(base_dir)
    if not path.exists():
        return {"targets": []}

    try:
        raw_data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"targets": []}

    targets = raw_data.get("targets") if isinstance(raw_data, dict) else []
    return {"targets": _normalize_direct_publisher_targets(targets)}


def save_direct_publishers_config(base_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    current = get_direct_publishers_config(base_dir)
    targets = payload.get("targets", current.get("targets", []))
    normalized = {"targets": _normalize_direct_publisher_targets(targets)}
    path = get_direct_publishers_storage_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return normalized


def get_direct_publisher_target(base_dir: Path, target_id: str) -> dict[str, Any]:
    target_id = str(target_id or "").strip()
    if not target_id:
        raise ValueError("direct publisher target id is required")

    config = get_direct_publishers_config(base_dir)
    for target in config.get("targets", []):
        if target.get("id") == target_id:
            return target
    raise ValueError("direct publisher target not found")


def publish_job_to_direct_target(base_dir: Path, job: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    platform = str(target.get("platform") or "").strip().lower()
    if platform != str(job.get("platformKey") or "").strip().lower():
        raise ValueError("direct publisher target platform does not match job platform")
    if not target.get("enabled", True):
        raise ValueError("direct publisher target is disabled")

    if platform == "telegram":
        return _publish_to_telegram(job, target)
    if platform == "discord":
        return _publish_to_discord(job, target)
    if platform == "reddit":
        return _publish_to_reddit(job, target)
    if platform == "twitter":
        return _publish_to_x(job, target)
    if platform == "facebook":
        return _publish_to_facebook(job, target)
    if platform == "threads":
        return _publish_to_threads(job, target)
    if platform == "youtube":
        return _publish_to_youtube(base_dir, job, target)
    if platform == "tiktok":
        return _publish_to_tiktok(job, target)
    raise ValueError(f"unsupported direct publisher platform: {platform}")


def refresh_direct_publish_job_status(base_dir: Path, job: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    platform = str(target.get("platform") or "").strip().lower()
    if platform != str(job.get("platformKey") or "").strip().lower():
        raise ValueError("direct publisher target platform does not match job platform")
    if platform == "threads":
        return _refresh_threads_status(job, target)
    if platform == "youtube":
        return _refresh_youtube_status(job, target)
    if platform == "tiktok":
        return _refresh_tiktok_status(job, target)
    return {
        "status": "published",
        "platform": platform,
        "details": {},
    }


def _normalize_direct_publisher_targets(targets: Any) -> list[dict[str, Any]]:
    if not isinstance(targets, list):
        return []

    normalized = []
    for item in targets:
        if not isinstance(item, dict):
            continue
        platform = str(item.get("platform") or "").strip().lower()
        if platform not in DIRECT_PUBLISHER_PLATFORMS:
            continue
        config = item.get("config") if isinstance(item.get("config"), dict) else {}
        normalized.append(
            {
                "id": str(item.get("id") or f"publisher-{secrets.token_hex(8)}").strip(),
                "platform": platform,
                "name": str(item.get("name") or "").strip() or _default_target_name(platform, config),
                "enabled": bool(item.get("enabled", True)),
                "config": _normalize_target_config(platform, config),
            }
        )
    return normalized


def _normalize_target_config(platform: str, config: dict[str, Any]) -> dict[str, Any]:
    if platform == "telegram":
        return {
            "botToken": str(config.get("botToken") or "").strip(),
            "chatId": str(config.get("chatId") or "").strip(),
            "parseMode": str(config.get("parseMode") or "").strip() or "HTML",
            "disableWebPagePreview": bool(config.get("disableWebPagePreview", False)),
        }
    if platform == "discord":
        return {
            "webhookUrl": str(config.get("webhookUrl") or "").strip(),
            "username": str(config.get("username") or "").strip(),
        }
    if platform == "reddit":
        return {
            "clientId": str(config.get("clientId") or "").strip(),
            "clientSecret": str(config.get("clientSecret") or "").strip(),
            "refreshToken": str(config.get("refreshToken") or "").strip(),
            "subreddit": str(config.get("subreddit") or "").strip(),
        }
    if platform == "twitter":
        return {
            "apiKey": str(config.get("apiKey") or "").strip(),
            "apiKeySecret": str(config.get("apiKeySecret") or "").strip(),
            "accessToken": str(config.get("accessToken") or "").strip(),
            "accessTokenSecret": str(config.get("accessTokenSecret") or "").strip(),
        }
    if platform == "facebook":
        return {
            "accessToken": str(config.get("accessToken") or "").strip(),
            "pageId": str(config.get("pageId") or "").strip(),
        }
    if platform == "threads":
        return {
            "accessToken": str(config.get("accessToken") or "").strip(),
            "userId": str(config.get("userId") or "").strip(),
        }
    if platform == "youtube":
        return {
            "accessToken": str(config.get("accessToken") or "").strip(),
            "privacyStatus": str(config.get("privacyStatus") or "").strip() or "private",
            "categoryId": str(config.get("categoryId") or "").strip() or "22",
            "defaultLanguage": str(config.get("defaultLanguage") or "").strip(),
            "notifySubscribers": bool(config.get("notifySubscribers", False)),
            "thumbnailUrl": str(config.get("thumbnailUrl") or "").strip(),
            "thumbnailPath": str(config.get("thumbnailPath") or "").strip(),
            "captionsPath": str(config.get("captionsPath") or "").strip(),
            "captionsLanguage": str(config.get("captionsLanguage") or "").strip() or "en",
            "captionsName": str(config.get("captionsName") or "").strip() or "Default captions",
            "captionIsDraft": bool(config.get("captionIsDraft", False)),
            "publishAt": str(config.get("publishAt") or "").strip(),
            "premiereAt": str(config.get("premiereAt") or "").strip(),
        }
    if platform == "tiktok":
        return {
            "accessToken": str(config.get("accessToken") or "").strip(),
            "privacyLevel": str(config.get("privacyLevel") or "").strip() or "SELF_ONLY",
            "disableComment": bool(config.get("disableComment", False)),
            "disableDuet": bool(config.get("disableDuet", False)),
            "disableStitch": bool(config.get("disableStitch", False)),
        }
    return {}


def _default_target_name(platform: str, config: dict[str, Any]) -> str:
    if platform == "telegram":
        return str(config.get("chatId") or "").strip() or "Telegram"
    if platform == "discord":
        return str(config.get("username") or "").strip() or "Discord Webhook"
    if platform == "reddit":
        subreddit = str(config.get("subreddit") or "").strip()
        return f"r/{subreddit}" if subreddit else "Reddit"
    if platform == "facebook":
        return str(config.get("pageId") or "").strip() or "Facebook"
    if platform == "threads":
        return str(config.get("userId") or "").strip() or "Threads"
    if platform == "youtube":
        return "YouTube"
    if platform == "tiktok":
        return "TikTok"
    return "X"


def _publish_to_telegram(job: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    requests = _get_requests_module()
    config = target["config"]
    if not config.get("botToken") or not config.get("chatId"):
        raise ValueError("telegram botToken and chatId are required")

    caption = _build_caption(job, 1024)
    media_kind = str(job.get("metadata", {}).get("mediaKind") or "").strip().lower()
    media_url = str(job.get("mediaPublicUrl") or "").strip()
    base_url = f"https://api.telegram.org/bot{config['botToken']}"

    if media_kind == "image" and media_url:
        response = requests.post(
            f"{base_url}/sendPhoto",
            data={
                "chat_id": config["chatId"],
                "photo": media_url,
                "caption": caption,
                "parse_mode": config["parseMode"],
                "disable_notification": False,
            },
            timeout=30,
        )
    elif media_kind == "video" and media_url:
        response = requests.post(
            f"{base_url}/sendVideo",
            data={
                "chat_id": config["chatId"],
                "video": media_url,
                "caption": caption,
                "parse_mode": config["parseMode"],
                "supports_streaming": True,
            },
            timeout=60,
        )
    else:
        response = requests.post(
            f"{base_url}/sendMessage",
            data={
                "chat_id": config["chatId"],
                "text": _build_post_text(job),
                "parse_mode": config["parseMode"],
                "disable_web_page_preview": config["disableWebPagePreview"],
            },
            timeout=30,
        )
    _raise_for_http_error(response)
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description") or "telegram publish failed")
    return {"platform": "telegram", "messageId": payload.get("result", {}).get("message_id")}


def _publish_to_discord(job: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    requests = _get_requests_module()
    config = target["config"]
    webhook_url = str(config.get("webhookUrl") or "").strip()
    if not webhook_url:
        raise ValueError("discord webhookUrl is required")

    media_url = str(job.get("mediaPublicUrl") or "").strip()
    message = _build_post_text(job, include_media_url=False)
    payload: dict[str, Any] = {
        "content": message[:1900] if message else None,
        "username": config.get("username") or None,
    }
    if media_url:
        payload["embeds"] = [
            {
                "title": job.get("title") or job.get("materialName") or "Social Upload",
                "description": (job.get("message") or "")[:4096],
                "url": media_url,
                "image": {"url": media_url} if (job.get("metadata", {}) or {}).get("mediaKind") == "image" else None,
            }
        ]
        payload["embeds"][0] = {key: value for key, value in payload["embeds"][0].items() if value}
    payload = {key: value for key, value in payload.items() if value is not None}
    response = requests.post(webhook_url, json=payload, timeout=30)
    if response.status_code not in {200, 204}:
        _raise_for_http_error(response)
    return {"platform": "discord", "statusCode": response.status_code}


def _publish_to_reddit(job: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    requests = _get_requests_module()
    config = target["config"]
    if not config.get("clientId") or not config.get("clientSecret") or not config.get("refreshToken") or not config.get("subreddit"):
        raise ValueError("reddit clientId, clientSecret, refreshToken, and subreddit are required")

    token_response = requests.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(config["clientId"], config["clientSecret"]),
        data={
            "grant_type": "refresh_token",
            "refresh_token": config["refreshToken"],
        },
        headers={"User-Agent": "social-auto-upload/1.0"},
        timeout=30,
    )
    _raise_for_http_error(token_response)
    access_token = token_response.json().get("access_token")
    if not access_token:
        raise RuntimeError("reddit access token was not returned")

    media_url = str(job.get("mediaPublicUrl") or "").strip()
    submit_data = {
        "api_type": "json",
        "sr": config["subreddit"],
        "title": _truncate_text(job.get("title") or job.get("materialName") or "Untitled", 300),
        "kind": "link" if media_url else "self",
    }
    if media_url:
        submit_data["url"] = media_url
    else:
        submit_data["text"] = _build_post_text(job, include_media_url=False)[:40000]

    response = requests.post(
        "https://oauth.reddit.com/api/submit",
        headers={
            "Authorization": f"bearer {access_token}",
            "User-Agent": "social-auto-upload/1.0",
        },
        data=submit_data,
        timeout=30,
    )
    _raise_for_http_error(response)
    payload = response.json()
    errors = (((payload.get("json") or {}).get("errors")) or [])
    if errors:
        raise RuntimeError(f"reddit publish failed: {errors}")
    return {"platform": "reddit", "subreddit": config["subreddit"]}


def _publish_to_x(job: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    requests = _get_requests_module()
    config = target["config"]
    required = ["apiKey", "apiKeySecret", "accessToken", "accessTokenSecret"]
    if any(not config.get(key) for key in required):
        raise ValueError("x apiKey, apiKeySecret, accessToken, and accessTokenSecret are required")

    url = "https://api.x.com/2/tweets"
    text = _build_post_text(job)
    body = json.dumps({"text": _truncate_text(text, 280)}, ensure_ascii=False, separators=(",", ":"))
    headers = {
        "Authorization": _build_oauth1_header(
            method="POST",
            url=url,
            consumer_key=config["apiKey"],
            consumer_secret=config["apiKeySecret"],
            token=config["accessToken"],
            token_secret=config["accessTokenSecret"],
        ),
        "Content-Type": "application/json",
    }
    response = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=30)
    _raise_for_http_error(response)
    payload = response.json()
    tweet_id = ((payload.get("data") or {}).get("id"))
    if not tweet_id:
        raise RuntimeError(payload.get("detail") or "x publish failed")
    return {"platform": "twitter", "tweetId": tweet_id}


def _publish_to_facebook(job: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    requests = _get_requests_module()
    config = target["config"]
    access_token = str(config.get("accessToken") or "").strip()
    page_id = str(config.get("pageId") or "").strip()
    if not access_token or not page_id:
        raise ValueError("facebook accessToken and pageId are required")

    media_kind = str((job.get("metadata") or {}).get("mediaKind") or "").strip().lower()
    media_url = str(job.get("mediaPublicUrl") or "").strip()
    title = str(job.get("title") or job.get("materialName") or "Untitled").strip()
    message = _build_post_text(job, include_media_url=False)

    if media_kind == "image" and media_url:
        response = requests.post(
            f"https://graph.facebook.com/v25.0/{page_id}/photos",
            data={
                "url": media_url,
                "caption": _truncate_text(message, 2200),
                "access_token": access_token,
            },
            timeout=60,
        )
    elif media_kind == "video" and media_url:
        response = requests.post(
            f"https://graph.facebook.com/v25.0/{page_id}/videos",
            data={
                "file_url": media_url,
                "title": _truncate_text(title, 255),
                "description": _truncate_text(message, 63206),
                "access_token": access_token,
            },
            timeout=120,
        )
    else:
        payload = {
            "message": _truncate_text(_build_post_text(job), 63206),
            "access_token": access_token,
        }
        response = requests.post(
            f"https://graph.facebook.com/v25.0/{page_id}/feed",
            data=payload,
            timeout=60,
        )

    _raise_for_http_error(response)
    data = response.json() or {}
    if data.get("error"):
        raise RuntimeError(data["error"].get("message") or "facebook publish failed")
    post_id = data.get("post_id") or data.get("id")
    if not post_id:
        raise RuntimeError("facebook publish did not return a post id")
    return {"platform": "facebook", "postId": post_id, "finalStatus": "published"}


def _publish_to_threads(job: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    requests = _get_requests_module()
    config = target["config"]
    access_token = str(config.get("accessToken") or "").strip()
    user_id = str(config.get("userId") or "").strip()
    if not access_token or not user_id:
        raise ValueError("threads accessToken and userId are required")

    media_kind = str((job.get("metadata") or {}).get("mediaKind") or "").strip().lower()
    media_url = str(job.get("mediaPublicUrl") or "").strip()
    text = _truncate_text(_build_post_text(job, include_media_url=False), 500)
    container_payload = {
        "access_token": access_token,
    }
    if media_kind == "image" and media_url:
        container_payload.update({
            "media_type": "IMAGE",
            "image_url": media_url,
            "text": text,
        })
    elif media_kind == "video" and media_url:
        container_payload.update({
            "media_type": "VIDEO",
            "video_url": media_url,
            "text": text,
        })
    else:
        container_payload.update({
            "media_type": "TEXT",
            "text": text,
        })
    container_response = requests.post(
        f"https://graph.threads.net/v1.0/{user_id}/threads",
        data=container_payload,
        timeout=60,
    )
    _raise_for_http_error(container_response)
    container_data = container_response.json() or {}
    creation_id = container_data.get("id")
    if not creation_id:
        raise RuntimeError(container_data.get("error", {}).get("message") or "threads container creation failed")

    return {
        "platform": "threads",
        "creationId": creation_id,
        "details": {
            "publishStage": "container_created",
        },
        "finalStatus": "processing",
    }


def _publish_to_youtube(base_dir: Path, job: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    requests = _get_requests_module()
    config = target["config"]
    access_token = str(config.get("accessToken") or "").strip()
    if not access_token:
        raise ValueError("youtube accessToken is required")

    media_path = _resolve_media_path(base_dir, job)
    if media_path.suffix.lower() not in {".mp4", ".mov", ".m4v", ".avi", ".webm"}:
        raise ValueError("youtube direct upload requires a local video file")

    options = _resolve_youtube_publish_options(base_dir, job, target)
    metadata = {
        "snippet": {
            "title": _truncate_text(str(job.get("title") or job.get("materialName") or "Untitled"), 100),
            "description": _truncate_text(str(job.get("message") or ""), 5000),
            "categoryId": str(config.get("categoryId") or "22"),
        },
        "status": {
            "privacyStatus": options["privacyStatus"],
        },
    }
    if options["defaultLanguage"]:
        metadata["snippet"]["defaultLanguage"] = options["defaultLanguage"]
    if options["publishAt"]:
        metadata["status"]["publishAt"] = options["publishAt"]
    metadata["status"]["selfDeclaredMadeForKids"] = bool(options["selfDeclaredMadeForKids"])
    file_size = media_path.stat().st_size
    init_response = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(file_size),
        },
        params={
            "notifySubscribers": "true" if options["notifySubscribers"] else "false",
        },
        data=json.dumps(metadata, ensure_ascii=False),
        timeout=60,
    )
    _raise_for_http_error(init_response)
    upload_url = init_response.headers.get("Location") or init_response.headers.get("location")
    if not upload_url:
        raise RuntimeError("youtube upload session location was not returned")

    upload_response = requests.put(
        upload_url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Length": str(file_size),
            "Content-Type": "video/mp4",
        },
        data=media_path.read_bytes(),
        timeout=600,
    )
    _raise_for_http_error(upload_response)
    upload_data = upload_response.json() or {}
    video_id = upload_data.get("id")
    if not video_id:
        raise RuntimeError(upload_data.get("error", {}).get("message") or "youtube upload failed")
    enhancement_details = {}
    thumbnail_details = _set_youtube_thumbnail(base_dir, access_token, requests, video_id, options)
    if thumbnail_details:
        enhancement_details["thumbnail"] = thumbnail_details
    captions_details = _upload_youtube_captions(access_token, requests, video_id, options)
    if captions_details:
        enhancement_details["captions"] = captions_details
    if options["premiereAt"]:
        enhancement_details["premiereRequestedAt"] = options["premiereAt"]
        enhancement_details["premiereMode"] = "scheduled_publish_fallback"
    if options["publishAt"]:
        enhancement_details["publishAt"] = options["publishAt"]
    return {
        "platform": "youtube",
        "videoId": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "details": enhancement_details,
        "finalStatus": "processing",
    }


def _publish_to_tiktok(job: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    requests = _get_requests_module()
    config = target["config"]
    access_token = str(config.get("accessToken") or "").strip()
    media_url = str(job.get("mediaPublicUrl") or "").strip()
    if not access_token:
        raise ValueError("tiktok accessToken is required")
    if not media_url:
        raise ValueError("tiktok direct upload requires mediaPublicUrl")

    title = _truncate_text(str(job.get("message") or job.get("title") or ""), 2200)
    response = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        data=json.dumps(
            {
                "post_info": {
                    "title": title,
                    "privacy_level": str(config.get("privacyLevel") or "SELF_ONLY"),
                    "disable_comment": bool(config.get("disableComment", False)),
                    "disable_duet": bool(config.get("disableDuet", False)),
                    "disable_stitch": bool(config.get("disableStitch", False)),
                },
                "source_info": {
                    "source": "PULL_FROM_URL",
                    "video_url": media_url,
                },
            },
            ensure_ascii=False,
        ),
        timeout=60,
    )
    _raise_for_http_error(response)
    payload = response.json() or {}
    data = payload.get("data") or {}
    publish_id = data.get("publish_id") or payload.get("publish_id")
    if payload.get("error"):
        raise RuntimeError(payload["error"].get("message") or "tiktok publish init failed")
    if not publish_id:
        raise RuntimeError("tiktok publish id was not returned")
    return {
        "platform": "tiktok",
        "publishId": publish_id,
        "finalStatus": "processing",
    }


def _resolve_media_path(base_dir: Path, job: dict[str, Any]) -> Path:
    media_path_value = str(job.get("mediaPath") or "").strip()
    if not media_path_value:
        raise ValueError("mediaPath is required")
    media_path = Path(media_path_value)
    if not media_path.is_absolute():
        media_path = base_dir / media_path
    if not media_path.exists():
        raise FileNotFoundError(f"media file not found: {media_path}")
    return media_path


def _refresh_threads_status(job: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    requests = _get_requests_module()
    config = target["config"]
    access_token = str(config.get("accessToken") or "").strip()
    metadata = job.get("metadata") or {}
    user_id = str(config.get("userId") or "").strip()
    thread_id = str(metadata.get("threadId") or metadata.get("publishId") or "").strip()
    creation_id = str(metadata.get("creationId") or "").strip()
    if not access_token:
        raise ValueError("threads accessToken is required for status refresh")

    if thread_id:
        response = requests.get(
            f"https://graph.threads.net/v1.0/{thread_id}",
            params={
                "fields": "id,permalink,shortcode,username,text,timestamp",
                "access_token": access_token,
            },
            timeout=30,
        )
        _raise_for_http_error(response)
        payload = response.json() or {}
        if payload.get("error"):
            raise RuntimeError(payload["error"].get("message") or "threads status refresh failed")
        if payload.get("id"):
            details = {
                "publishStage": "published",
                "permalink": payload.get("permalink") or "",
                "shortcode": payload.get("shortcode") or "",
                "username": payload.get("username") or "",
                "text": payload.get("text") or "",
                "timestamp": payload.get("timestamp") or "",
            }
            return {
                "status": "published",
                "platform": "threads",
                "remoteId": payload.get("id"),
                "threadId": payload.get("id"),
                "creationId": creation_id,
                "url": payload.get("permalink") or "",
                "details": {key: value for key, value in details.items() if value},
            }
        return {
            "status": "processing",
            "platform": "threads",
            "threadId": thread_id,
            "creationId": creation_id,
            "details": {
                "publishStage": "published_pending_reconciliation",
            },
        }

    if not user_id or not creation_id:
        raise ValueError("threads userId and creationId are required before final publish reconciliation")

    container_response = requests.get(
        f"https://graph.threads.net/v1.0/{creation_id}",
        params={
            "fields": "id,status,status_code,error_message",
            "access_token": access_token,
        },
        timeout=30,
    )
    _raise_for_http_error(container_response)
    container_payload = container_response.json() or {}
    if container_payload.get("error"):
        raise RuntimeError(container_payload["error"].get("message") or "threads container status refresh failed")

    status_code = str(container_payload.get("status_code") or "").strip().upper()
    status_text = str(container_payload.get("status") or "").strip()
    error_message = str(container_payload.get("error_message") or "").strip()
    container_details = {
        "publishStage": "container_processing",
        "containerStatusCode": status_code,
        "containerStatus": status_text,
    }
    if error_message:
        container_details["containerError"] = error_message

    if status_code in {"ERROR", "FAILED", "EXPIRED"}:
        return {
            "status": "failed",
            "platform": "threads",
            "creationId": creation_id,
            "error": error_message or status_text or "threads media container failed",
            "details": {key: value for key, value in container_details.items() if value},
        }

    if status_code not in {"FINISHED", "READY", "COMPLETE"}:
        return {
            "status": "processing",
            "platform": "threads",
            "creationId": creation_id,
            "details": {key: value for key, value in container_details.items() if value},
        }

    publish_response = requests.post(
        f"https://graph.threads.net/v1.0/{user_id}/threads_publish",
        data={
            "creation_id": creation_id,
            "access_token": access_token,
        },
        timeout=60,
    )
    _raise_for_http_error(publish_response)
    publish_data = publish_response.json() or {}
    thread_id = publish_data.get("id")
    if not thread_id:
        raise RuntimeError(publish_data.get("error", {}).get("message") or "threads publish failed")
    container_details["publishStage"] = "publish_triggered"
    return {
        "status": "processing",
        "platform": "threads",
        "remoteId": thread_id,
        "threadId": thread_id,
        "creationId": creation_id,
        "details": {key: value for key, value in container_details.items() if value},
    }


def _refresh_youtube_status(job: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    requests = _get_requests_module()
    config = target["config"]
    access_token = str(config.get("accessToken") or "").strip()
    metadata = job.get("metadata") or {}
    video_id = str(metadata.get("videoId") or "").strip()
    if not access_token or not video_id:
        raise ValueError("youtube accessToken and videoId are required for status refresh")

    response = requests.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={
            "part": "status,processingDetails,snippet",
            "id": video_id,
        },
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    _raise_for_http_error(response)
    payload = response.json() or {}
    items = payload.get("items") or []
    if not items:
        return {
            "status": "processing",
            "platform": "youtube",
            "details": {"reason": "video_not_visible_yet"},
        }

    video = items[0] or {}
    status = video.get("status") or {}
    processing = video.get("processingDetails") or {}
    upload_status = str(status.get("uploadStatus") or "").strip().lower()
    processing_status = str(processing.get("processingStatus") or "").strip().lower()
    url = f"https://www.youtube.com/watch?v={video_id}"
    details = {
        "uploadStatus": upload_status,
        "processingStatus": processing_status,
        "privacyStatus": status.get("privacyStatus") or "",
        "embeddable": status.get("embeddable"),
        "license": status.get("license") or "",
        "channelTitle": ((video.get("snippet") or {}).get("channelTitle") or ""),
    }
    if upload_status in {"rejected", "failed", "deleted"} or processing_status in {"failed", "terminated"}:
        return {
            "status": "failed",
            "platform": "youtube",
            "remoteId": video_id,
            "url": url,
            "error": processing.get("processingFailureReason") or upload_status or processing_status or "youtube processing failed",
            "details": {key: value for key, value in details.items() if value not in {"", None}},
        }
    if upload_status == "processed" and processing_status in {"", "succeeded"}:
        return {
            "status": "published",
            "platform": "youtube",
            "remoteId": video_id,
            "url": url,
            "details": {key: value for key, value in details.items() if value not in {"", None}},
        }
    return {
        "status": "processing",
        "platform": "youtube",
        "remoteId": video_id,
        "url": url,
        "details": {key: value for key, value in details.items() if value not in {"", None}},
    }


def _refresh_tiktok_status(job: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    requests = _get_requests_module()
    config = target["config"]
    access_token = str(config.get("accessToken") or "").strip()
    metadata = job.get("metadata") or {}
    publish_id = str(metadata.get("publishId") or "").strip()
    if not access_token or not publish_id:
        raise ValueError("tiktok accessToken and publishId are required for status refresh")

    response = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        data=json.dumps({"publish_id": publish_id}, ensure_ascii=False),
        timeout=30,
    )
    _raise_for_http_error(response)
    payload = response.json() or {}
    error = _extract_tiktok_api_error(payload)
    if error:
        raise RuntimeError(error)
    data = payload.get("data") or {}
    raw_status = str(data.get("status") or payload.get("status") or "").strip()
    raw_status_code = str(data.get("status_code") or payload.get("status_code") or "").strip()
    normalized_state = _map_tiktok_publish_state(raw_status, raw_status_code, data)
    public_post_id = data.get("publicaly_available_post_id") or data.get("publicly_available_post_id") or ""
    remote_id = data.get("video_id") or public_post_id or publish_id
    fail_reason = str(data.get("fail_reason") or "").strip()
    details = {
        "publishId": publish_id,
        "status": raw_status,
        "statusCode": raw_status_code,
        "failReason": data.get("fail_reason") or "",
        "uploadedBytes": data.get("downloaded_bytes"),
        "videoId": data.get("video_id") or "",
        "publicalyAvailablePostId": public_post_id,
        "logId": ((payload.get("error") or {}).get("log_id") or ""),
    }
    if normalized_state == "published":
        return {
            "status": "published",
            "platform": "tiktok",
            "remoteId": remote_id,
            "publishId": publish_id,
            "videoId": data.get("video_id") or "",
            "details": {key: value for key, value in details.items() if value not in {"", None}},
        }
    if normalized_state == "failed":
        return {
            "status": "failed",
            "platform": "tiktok",
            "remoteId": remote_id,
            "publishId": publish_id,
            "videoId": data.get("video_id") or "",
            "error": fail_reason or raw_status_code or raw_status or "tiktok publish failed",
            "details": {key: value for key, value in details.items() if value not in {"", None}},
        }
    return {
        "status": "processing",
        "platform": "tiktok",
        "remoteId": remote_id,
        "publishId": publish_id,
        "videoId": data.get("video_id") or "",
        "details": {key: value for key, value in details.items() if value not in {"", None}},
    }


def _extract_tiktok_api_error(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""
    code = str(error.get("code") or "").strip()
    normalized_code = code.lower()
    if normalized_code in {"", "ok", "success"}:
        return ""
    message = str(error.get("message") or "").strip()
    log_id = str(error.get("log_id") or "").strip()
    parts = [part for part in [code, message, f"log_id={log_id}" if log_id else ""] if part]
    return " | ".join(parts) or "tiktok api request failed"


def _map_tiktok_publish_state(raw_status: str, raw_status_code: str, data: dict[str, Any]) -> str:
    normalized_tokens = {
        str(raw_status or "").strip().upper(),
        str(raw_status_code or "").strip().upper(),
    }
    normalized_tokens = {token for token in normalized_tokens if token}
    fail_reason = str(data.get("fail_reason") or "").strip()
    public_post_id = str(data.get("publicaly_available_post_id") or data.get("publicly_available_post_id") or "").strip()
    video_id = str(data.get("video_id") or "").strip()

    success_tokens = {
        "PUBLISH_COMPLETE",
        "PUBLISHED",
        "SUCCESS",
        "SEND_TO_USER_INBOX",
        "POST_SENT_TO_USER_INBOX",
    }
    failure_tokens = {
        "FAIL",
        "FAILED",
        "ERROR",
        "EXPIRED",
        "REJECTED",
        "PUBLISH_FAILED",
        "PUBLISH_REJECTED",
        "CANCELLED",
        "CANCELED",
        "DENIED",
    }
    processing_tokens = {
        "INIT",
        "INITIALIZED",
        "PENDING",
        "PROCESSING",
        "PROCESSING_DOWNLOAD",
        "DOWNLOADING",
        "UPLOADING",
        "IN_PROGRESS",
        "QUEUED",
        "PUBLISHING",
        "SCHEDULED",
    }

    if normalized_tokens & success_tokens:
        return "published"
    if normalized_tokens & failure_tokens:
        return "failed"
    if fail_reason:
        return "failed"
    if public_post_id or video_id:
        return "published"
    if normalized_tokens & processing_tokens:
        return "processing"

    for token in normalized_tokens:
        if any(fragment in token for fragment in ["FAIL", "REJECT", "CANCEL", "DENY", "ERROR", "EXPIRE"]):
            return "failed"
        if any(fragment in token for fragment in ["PROCESS", "PENDING", "QUEUE", "UPLOAD", "DOWNLOAD", "PROGRESS"]):
            return "processing"
        if any(fragment in token for fragment in ["SUCCESS", "PUBLISH_COMPLETE", "PUBLISHED", "INBOX"]):
            return "published"
    return "processing"


def _resolve_youtube_publish_options(base_dir: Path, job: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    config = target["config"]
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    youtube_metadata = metadata.get("youtube") if isinstance(metadata.get("youtube"), dict) else {}
    publish_at = _pick_first_non_empty(
        youtube_metadata.get("publishAt"),
        metadata.get("publishAt"),
        config.get("publishAt"),
    )
    premiere_at = _pick_first_non_empty(
        youtube_metadata.get("premiereAt"),
        metadata.get("premiereAt"),
        config.get("premiereAt"),
    )
    resolved_publish_at = publish_at or premiere_at
    requested_privacy = _pick_first_non_empty(
        youtube_metadata.get("privacyStatus"),
        metadata.get("privacyStatus"),
        config.get("privacyStatus"),
        "private",
    )
    privacy_status = requested_privacy
    if resolved_publish_at:
        privacy_status = "private"

    return {
        "privacyStatus": privacy_status,
        "requestedPrivacyStatus": requested_privacy,
        "publishAt": resolved_publish_at,
        "premiereAt": premiere_at,
        "thumbnailUrl": _pick_first_non_empty(
            youtube_metadata.get("thumbnailUrl"),
            metadata.get("thumbnailUrl"),
            config.get("thumbnailUrl"),
        ),
        "thumbnailPath": _pick_first_non_empty(
            youtube_metadata.get("thumbnailPath"),
            metadata.get("thumbnailPath"),
            config.get("thumbnailPath"),
        ),
        "captionsPath": _pick_first_non_empty(
            youtube_metadata.get("captionsPath"),
            metadata.get("captionsPath"),
            config.get("captionsPath"),
        ),
        "captionsLanguage": _pick_first_non_empty(
            youtube_metadata.get("captionsLanguage"),
            metadata.get("captionsLanguage"),
            config.get("captionsLanguage"),
            "en",
        ),
        "captionsName": _pick_first_non_empty(
            youtube_metadata.get("captionsName"),
            metadata.get("captionsName"),
            config.get("captionsName"),
            "Default captions",
        ),
        "captionIsDraft": bool(
            youtube_metadata.get("captionIsDraft", metadata.get("captionIsDraft", config.get("captionIsDraft", False)))
        ),
        "defaultLanguage": _pick_first_non_empty(
            youtube_metadata.get("defaultLanguage"),
            metadata.get("defaultLanguage"),
            config.get("defaultLanguage"),
        ),
        "notifySubscribers": bool(
            youtube_metadata.get("notifySubscribers", metadata.get("notifySubscribers", config.get("notifySubscribers", False)))
        ),
        "selfDeclaredMadeForKids": bool(
            youtube_metadata.get("selfDeclaredMadeForKids", metadata.get("selfDeclaredMadeForKids", False))
        ),
        "baseDir": base_dir,
    }


def _set_youtube_thumbnail(
    base_dir: Path,
    access_token: str,
    requests: Any,
    video_id: str,
    options: dict[str, Any],
) -> dict[str, Any]:
    thumbnail_url = str(options.get("thumbnailUrl") or "").strip()
    thumbnail_path_value = str(options.get("thumbnailPath") or "").strip()
    if not thumbnail_url and not thumbnail_path_value:
        return {}

    if thumbnail_url:
        source = "url"
        download_response = requests.get(thumbnail_url, timeout=60)
        _raise_for_http_error(download_response)
        content = download_response.content
        content_type = download_response.headers.get("Content-Type") or "image/png"
        source_name = thumbnail_url
    else:
        source = "file"
        thumbnail_path = _resolve_optional_local_path(base_dir, thumbnail_path_value)
        content = thumbnail_path.read_bytes()
        content_type = mimetypes.guess_type(thumbnail_path.name)[0] or "application/octet-stream"
        source_name = thumbnail_path.name

    response = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
        params={"videoId": video_id},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": content_type,
        },
        data=content,
        timeout=120,
    )
    _raise_for_http_error(response)
    payload = response.json() if hasattr(response, "json") else {}
    return {
        "source": source,
        "sourceName": source_name,
        "mimeType": content_type,
        "etag": (payload or {}).get("etag") or "",
    }


def _upload_youtube_captions(
    access_token: str,
    requests: Any,
    video_id: str,
    options: dict[str, Any],
) -> dict[str, Any]:
    captions_path_value = str(options.get("captionsPath") or "").strip()
    if not captions_path_value:
        return {}

    captions_path = _resolve_optional_local_path(Path(options["baseDir"]), captions_path_value)
    content = captions_path.read_bytes()
    content_type = mimetypes.guess_type(captions_path.name)[0] or "application/octet-stream"
    boundary = f"===============sau-{secrets.token_hex(8)}=="
    snippet = {
        "snippet": {
            "videoId": video_id,
            "language": str(options.get("captionsLanguage") or "en"),
            "name": str(options.get("captionsName") or "Default captions"),
            "isDraft": bool(options.get("captionIsDraft", False)),
        }
    }
    body = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(snippet, ensure_ascii=False)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    response = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/captions",
        params={
            "part": "snippet",
            "uploadType": "multipart",
        },
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": f'multipart/related; boundary="{boundary}"',
        },
        data=body,
        timeout=120,
    )
    _raise_for_http_error(response)
    payload = response.json() or {}
    caption_id = payload.get("id") or ""
    return {
        "captionId": caption_id,
        "language": str(options.get("captionsLanguage") or "en"),
        "name": str(options.get("captionsName") or "Default captions"),
        "sourceName": captions_path.name,
    }


def _resolve_optional_local_path(base_dir: Path, path_value: str) -> Path:
    normalized = str(path_value or "").strip()
    if not normalized:
        raise ValueError("path is required")
    candidate = Path(normalized)
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    if not candidate.exists():
        raise FileNotFoundError(f"file not found: {candidate}")
    return candidate


def _pick_first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _build_post_text(job: dict[str, Any], include_media_url: bool = True) -> str:
    message = str(job.get("message") or "").strip()
    media_url = str(job.get("mediaPublicUrl") or "").strip()
    if include_media_url and media_url and media_url not in message:
        return f"{message}\n{media_url}".strip()
    return message


def _build_caption(job: dict[str, Any], limit: int) -> str:
    return _truncate_text(_build_post_text(job, include_media_url=False), limit)


def _truncate_text(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return f"{text[: limit - 1].rstrip()}…"


def _raise_for_http_error(response) -> None:
    if response.ok:
        return
    try:
        detail = response.json()
    except ValueError:
        detail = response.text
    raise RuntimeError(f"http {response.status_code}: {detail}")


def _get_requests_module():
    try:
        return importlib.import_module("requests")
    except ModuleNotFoundError as exc:
        raise RuntimeError("requests is required for direct publisher delivery") from exc


def _build_oauth1_header(
    method: str,
    url: str,
    consumer_key: str,
    consumer_secret: str,
    token: str,
    token_secret: str,
) -> str:
    oauth_params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    normalized_url = _normalize_oauth_url(url)
    signature = _build_oauth_signature(
        method=method,
        normalized_url=normalized_url,
        oauth_params=oauth_params,
        consumer_secret=consumer_secret,
        token_secret=token_secret,
    )
    oauth_params["oauth_signature"] = signature
    header_pairs = ", ".join(
        f'{urllib.parse.quote(key, safe="")}="{urllib.parse.quote(str(value), safe="")}"'
        for key, value in sorted(oauth_params.items())
    )
    return f"OAuth {header_pairs}"


def _normalize_oauth_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower() if parsed.hostname else ""
    port = parsed.port
    include_port = port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443))
    netloc = f"{host}:{port}" if include_port else host
    path = parsed.path or "/"
    return urllib.parse.urlunparse((scheme, netloc, path, "", "", ""))


def _build_oauth_signature(
    method: str,
    normalized_url: str,
    oauth_params: dict[str, str],
    consumer_secret: str,
    token_secret: str,
) -> str:
    parameter_string = "&".join(
        f"{urllib.parse.quote(key, safe='')}={urllib.parse.quote(str(value), safe='')}"
        for key, value in sorted(oauth_params.items())
    )
    base_elems = [
        method.upper(),
        urllib.parse.quote(normalized_url, safe=""),
        urllib.parse.quote(parameter_string, safe=""),
    ]
    base_string = "&".join(base_elems)
    signing_key = "&".join(
        [
            urllib.parse.quote(consumer_secret, safe=""),
            urllib.parse.quote(token_secret, safe=""),
        ]
    )
    digest = hmac.new(signing_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")
