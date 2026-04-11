import asyncio
import importlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from utils.account_registry import parse_metadata, serialize_account_row
from utils.direct_publishers import _build_oauth1_header

ACCOUNT_VALIDATION_FILENAME = "account_validation.json"


def get_account_validation_storage_path(base_dir: Path) -> Path:
    return base_dir / "db" / ACCOUNT_VALIDATION_FILENAME


def get_validation_results(base_dir: Path) -> dict[str, Any]:
    path = get_account_validation_storage_path(base_dir)
    if not path.exists():
        return {"results": {}}

    try:
        raw_data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"results": {}}

    results = raw_data.get("results") if isinstance(raw_data, dict) else {}
    if not isinstance(results, dict):
        return {"results": {}}

    normalized = {}
    for account_id, result in results.items():
        normalized[str(account_id)] = _normalize_validation_result(result)
    return {"results": normalized}


def get_validation_result(base_dir: Path, account_id: int | str) -> dict[str, Any] | None:
    results = get_validation_results(base_dir).get("results", {})
    return results.get(str(account_id))


def save_validation_result(base_dir: Path, account_id: int | str, result: dict[str, Any]) -> dict[str, Any]:
    payload = get_validation_results(base_dir)
    payload["results"][str(account_id)] = _normalize_validation_result(result)
    path = get_account_validation_storage_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload["results"][str(account_id)]


def delete_validation_result(base_dir: Path, account_id: int | str) -> None:
    payload = get_validation_results(base_dir)
    if str(account_id) not in payload["results"]:
        return
    del payload["results"][str(account_id)]
    path = get_account_validation_storage_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def merge_account_validation(account: dict[str, Any], result: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(account)
    validation = _normalize_validation_result(result)
    if not validation:
        merged["lastValidatedAt"] = ""
        merged["lastError"] = ""
        merged["validationMessage"] = ""
        merged["validationDetails"] = {}
        return merged

    merged["statusCode"] = validation["statusCode"]
    merged["status"] = validation["status"]
    merged["lastValidatedAt"] = validation["lastValidatedAt"]
    merged["lastError"] = validation["lastError"]
    merged["validationMessage"] = validation["message"]
    merged["validationDetails"] = validation["details"]
    return merged


def validate_account(base_dir: Path, db_path: Path, account_id: int | str) -> dict[str, Any]:
    account_row = _get_account_row(db_path, account_id)
    account = serialize_account_row(account_row)
    result = _validate_serialized_account(account)
    saved = save_validation_result(base_dir, account["id"], result)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE user_info
            SET status = ?
            WHERE id = ?
            """,
            (int(saved["statusCode"]), int(account["id"])),
        )
        conn.commit()
    return merge_account_validation(account, saved)


def validate_accounts(base_dir: Path, db_path: Path, account_ids: list[int] | None = None) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if account_ids:
            placeholders = ",".join(["?"] * len(account_ids))
            cursor.execute(
                f"SELECT * FROM user_info WHERE id IN ({placeholders}) ORDER BY id DESC",
                tuple(int(item) for item in account_ids),
            )
        else:
            cursor.execute("SELECT * FROM user_info ORDER BY id DESC")
        rows = cursor.fetchall()

    results = []
    for row in rows:
        results.append(validate_account(base_dir, db_path, row["id"]))
    return results


def _get_account_row(db_path: Path, account_id: int | str):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM user_info WHERE id = ?", (int(account_id),))
        row = cursor.fetchone()
    if not row:
        raise ValueError("account not found")
    return row


def _validate_serialized_account(account: dict[str, Any]) -> dict[str, Any]:
    platform_key = str(account.get("platformKey") or "").strip().lower()
    if platform_key in {"xiaohongshu", "channels", "douyin", "kuaishou"}:
        return _validate_domestic_account(account)
    if platform_key == "twitter":
        return _validate_twitter_account(account)
    if platform_key == "reddit":
        return _validate_reddit_account(account)
    if platform_key == "facebook":
        return _validate_facebook_account(account)
    if platform_key == "threads":
        return _validate_threads_account(account)
    if platform_key == "youtube":
        return _validate_youtube_account(account)
    if platform_key == "tiktok":
        return _validate_tiktok_account(account)
    return _build_validation_result(
        status_code=0,
        message="此平台尚未支援驗證",
        last_error="unsupported platform",
        details={"platformKey": platform_key},
    )


def _validate_domestic_account(account: dict[str, Any]) -> dict[str, Any]:
    if not account.get("filePath"):
        return _build_validation_result(
            status_code=0,
            message="缺少 Cookie 檔案",
            last_error="cookie file is required",
        )
    auth_module = importlib.import_module("myUtils.auth")
    is_valid = asyncio.run(auth_module.check_cookie(account["type"], account["filePath"]))
    if is_valid:
        return _build_validation_result(status_code=1, message="Cookie 驗證成功")
    return _build_validation_result(
        status_code=0,
        message="Cookie 已失效或無法登入",
        last_error="cookie validation failed",
    )


def _validate_twitter_account(account: dict[str, Any]) -> dict[str, Any]:
    metadata = parse_metadata(account.get("metadata") or {})
    missing = _missing_fields(metadata, ["apiKey", "apiKeySecret", "accessToken", "accessTokenSecret"])
    if missing:
        return _build_missing_fields_result(missing)

    requests = _get_requests_module()
    url = "https://api.x.com/2/users/me"
    headers = {
        "Authorization": _build_oauth1_header(
            method="GET",
            url=url,
            consumer_key=metadata["apiKey"],
            consumer_secret=metadata["apiKeySecret"],
            token=metadata["accessToken"],
            token_secret=metadata["accessTokenSecret"],
        ),
    }
    response = requests.get(url, headers=headers, timeout=30)
    _raise_for_http_error(response)
    payload = response.json()
    user = payload.get("data") or {}
    if not user.get("id"):
        raise RuntimeError(payload.get("detail") or "x validate failed")
    return _build_validation_result(
        status_code=1,
        message="X API 驗證成功",
        details={"userId": user.get("id"), "username": user.get("username")},
    )


def _validate_reddit_account(account: dict[str, Any]) -> dict[str, Any]:
    metadata = parse_metadata(account.get("metadata") or {})
    missing = _missing_fields(metadata, ["clientId", "clientSecret", "refreshToken"])
    if missing:
        return _build_missing_fields_result(missing)

    requests = _get_requests_module()
    token_response = requests.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(metadata["clientId"], metadata["clientSecret"]),
        data={"grant_type": "refresh_token", "refresh_token": metadata["refreshToken"]},
        headers={"User-Agent": "social-auto-upload/1.0"},
        timeout=30,
    )
    _raise_for_http_error(token_response)
    access_token = (token_response.json() or {}).get("access_token")
    if not access_token:
        raise RuntimeError("reddit access token was not returned")

    response = requests.get(
        "https://oauth.reddit.com/api/v1/me",
        headers={
            "Authorization": f"bearer {access_token}",
            "User-Agent": "social-auto-upload/1.0",
        },
        timeout=30,
    )
    _raise_for_http_error(response)
    payload = response.json() or {}
    if not payload.get("name"):
        raise RuntimeError("reddit profile was not returned")
    return _build_validation_result(
        status_code=1,
        message="Reddit API 驗證成功",
        details={"username": payload.get("name"), "subreddit": metadata.get("subreddit", "")},
    )


def _validate_facebook_account(account: dict[str, Any]) -> dict[str, Any]:
    metadata = parse_metadata(account.get("metadata") or {})
    missing = _missing_fields(metadata, ["accessToken"])
    if missing:
        return _build_missing_fields_result(missing)

    requests = _get_requests_module()
    response = requests.get(
        "https://graph.facebook.com/me",
        params={"fields": "id,name", "access_token": metadata["accessToken"]},
        timeout=30,
    )
    _raise_for_http_error(response)
    payload = response.json() or {}
    if payload.get("error"):
        raise RuntimeError(payload["error"].get("message") or "facebook validate failed")
    if not payload.get("id"):
        raise RuntimeError("facebook profile was not returned")
    return _build_validation_result(
        status_code=1,
        message="Facebook Graph API 驗證成功",
        details={"profileId": payload.get("id"), "name": payload.get("name")},
    )


def _validate_threads_account(account: dict[str, Any]) -> dict[str, Any]:
    metadata = parse_metadata(account.get("metadata") or {})
    missing = _missing_fields(metadata, ["accessToken", "userId"])
    if missing:
        return _build_missing_fields_result(missing)

    requests = _get_requests_module()
    url = f"https://graph.threads.net/v1.0/{metadata['userId']}"
    response = requests.get(
        url,
        params={"fields": "id,username,name", "access_token": metadata["accessToken"]},
        timeout=30,
    )
    _raise_for_http_error(response)
    payload = response.json() or {}
    if payload.get("error"):
        raise RuntimeError(payload["error"].get("message") or "threads validate failed")
    if not payload.get("id"):
        raise RuntimeError("threads profile was not returned")
    return _build_validation_result(
        status_code=1,
        message="Threads API 驗證成功",
        details={"userId": payload.get("id"), "username": payload.get("username")},
    )


def _validate_youtube_account(account: dict[str, Any]) -> dict[str, Any]:
    metadata = parse_metadata(account.get("metadata") or {})
    missing = _missing_fields(metadata, ["accessToken"])
    if missing:
        return _build_missing_fields_result(missing)

    requests = _get_requests_module()
    response = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={"part": "id,snippet", "mine": "true"},
        headers={"Authorization": f"Bearer {metadata['accessToken']}"},
        timeout=30,
    )
    _raise_for_http_error(response)
    payload = response.json() or {}
    items = payload.get("items") or []
    if not items:
        raise RuntimeError(payload.get("error", {}).get("message") or "youtube channel was not returned")
    channel = items[0]
    return _build_validation_result(
        status_code=1,
        message="YouTube Data API 驗證成功",
        details={
            "channelId": channel.get("id"),
            "channelTitle": ((channel.get("snippet") or {}).get("title") or ""),
        },
    )


def _validate_tiktok_account(account: dict[str, Any]) -> dict[str, Any]:
    metadata = parse_metadata(account.get("metadata") or {})
    missing = _missing_fields(metadata, ["accessToken"])
    if missing:
        return _build_missing_fields_result(missing)

    requests = _get_requests_module()
    response = requests.get(
        "https://open.tiktokapis.com/v2/user/info/",
        params={"fields": "open_id,union_id,display_name,avatar_url"},
        headers={"Authorization": f"Bearer {metadata['accessToken']}"},
        timeout=30,
    )
    _raise_for_http_error(response)
    payload = response.json() or {}
    data = payload.get("data") or {}
    user = data.get("user") or data
    if payload.get("error"):
        raise RuntimeError(payload["error"].get("message") or "tiktok validate failed")
    if not (user.get("open_id") or user.get("union_id")):
        raise RuntimeError("tiktok user profile was not returned")
    return _build_validation_result(
        status_code=1,
        message="TikTok API 驗證成功",
        details={
            "openId": user.get("open_id", ""),
            "displayName": user.get("display_name", ""),
        },
    )


def _missing_fields(metadata: dict[str, Any], field_names: list[str]) -> list[str]:
    return [field for field in field_names if not str(metadata.get(field) or "").strip()]


def _build_missing_fields_result(field_names: list[str]) -> dict[str, Any]:
    fields = ", ".join(field_names)
    return _build_validation_result(
        status_code=0,
        message=f"缺少驗證欄位：{fields}",
        last_error=f"missing fields: {fields}",
        details={"missingFields": field_names},
    )


def _build_validation_result(
    status_code: int,
    message: str,
    last_error: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "statusCode": int(status_code),
        "status": "正常" if int(status_code) == 1 else "異常",
        "message": str(message or "").strip(),
        "lastError": str(last_error or "").strip(),
        "lastValidatedAt": datetime.now().isoformat(timespec="seconds"),
        "details": details or {},
    }


def _normalize_validation_result(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    status_code = int(value.get("statusCode", 0))
    return {
        "statusCode": status_code,
        "status": "正常" if status_code == 1 else "異常",
        "message": str(value.get("message") or "").strip(),
        "lastError": str(value.get("lastError") or "").strip(),
        "lastValidatedAt": str(value.get("lastValidatedAt") or "").strip(),
        "details": value.get("details") if isinstance(value.get("details"), dict) else {},
    }


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
        raise RuntimeError("requests is required for account validation") from exc
