import base64
import hashlib
import hmac
import importlib
import json
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any

DIRECT_PUBLISHERS_FILENAME = "direct_publishers.json"
DIRECT_PUBLISHER_PLATFORMS = {"telegram", "discord", "reddit", "twitter"}


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
    raise ValueError(f"unsupported direct publisher platform: {platform}")


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
    return {}


def _default_target_name(platform: str, config: dict[str, Any]) -> str:
    if platform == "telegram":
        return str(config.get("chatId") or "").strip() or "Telegram"
    if platform == "discord":
        return str(config.get("username") or "").strip() or "Discord Webhook"
    if platform == "reddit":
        subreddit = str(config.get("subreddit") or "").strip()
        return f"r/{subreddit}" if subreddit else "Reddit"
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
