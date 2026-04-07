import sqlite3
from pathlib import Path

from conf import BASE_DIR
from myUtils.platforms import REDDIT_PLATFORM, X_PLATFORM
from uploader.reddit_uploader.main import (
    RedditAPIError,
    load_reddit_credentials,
    save_reddit_credentials,
    submit_post as submit_reddit_post,
)
from uploader.x_uploader.main import (
    XAPIError,
    load_x_credentials,
    publish_post as publish_x_post,
    save_x_credentials,
)


class SocialPublishValidationError(Exception):
    pass


def _get_db_path():
    return Path(BASE_DIR / "db" / "database.db")


def _get_cookie_path(relative_path):
    return Path(BASE_DIR / "cookiesFile" / relative_path)


def _get_video_path(file_name):
    return Path(BASE_DIR / "videoFile" / file_name)


def _normalize_account_ids(account_ids):
    normalized_ids = []
    for account_id in account_ids or []:
        try:
            normalized_ids.append(int(account_id))
        except (TypeError, ValueError) as exc:
            raise SocialPublishValidationError(f"无效账号 ID: {account_id}") from exc
    return normalized_ids


def _load_accounts(account_ids, platform_type):
    account_ids = _normalize_account_ids(account_ids)
    if not account_ids:
        raise SocialPublishValidationError("账号列表不能为空")

    placeholders = ",".join("?" for _ in account_ids)
    with sqlite3.connect(_get_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            f'''
            SELECT id, type, filePath, userName, status
            FROM user_info
            WHERE id IN ({placeholders}) AND type = ?
            ''',
            (*account_ids, platform_type),
        )
        rows = cursor.fetchall()

    account_map = {row["id"]: dict(row) for row in rows}
    missing_ids = [account_id for account_id in account_ids if account_id not in account_map]
    if missing_ids:
        raise SocialPublishValidationError(f"账号不存在或平台不匹配: {missing_ids}")
    return [account_map[account_id] for account_id in account_ids]


def _resolve_media_paths(file_list):
    media_paths = []
    for file_name in file_list or []:
        file_path = _get_video_path(file_name)
        if not file_path.exists():
            raise SocialPublishValidationError(f"文件不存在: {file_name}")
        media_paths.append(file_path)
    return media_paths


def _result_entry(platform, account, success, message, **extra):
    payload = {
        "platform": platform,
        "accountId": account.get("id"),
        "accountName": account.get("userName"),
        "success": success,
        "message": message,
    }
    payload.update(extra)
    return payload


def _publish_x_destination(payload, destination, media_paths):
    accounts = _load_accounts(destination.get("accountIds"), X_PLATFORM)
    text = (destination.get("text") or payload.get("body") or payload.get("title") or "").strip()
    if not text:
        raise SocialPublishValidationError("X 发布内容不能为空")

    results = []
    for account in accounts:
        credential_path = _get_cookie_path(account["filePath"])
        try:
            credentials = load_x_credentials(credential_path)
            post_data, refreshed_credentials = publish_x_post(credentials, text, media_paths=media_paths)
            save_x_credentials(credential_path, refreshed_credentials)
            results.append(
                _result_entry(
                    "x",
                    account,
                    True,
                    "发布成功",
                    postId=post_data.get("id", ""),
                    text=text,
                )
            )
        except (XAPIError, FileNotFoundError, KeyError, ValueError) as exc:
            results.append(_result_entry("x", account, False, str(exc), text=text))
    return results


def _publish_reddit_destination(payload, destination, media_paths):
    accounts = _load_accounts(destination.get("accountIds"), REDDIT_PLATFORM)
    subreddits = destination.get("subreddits") or []
    post_kind = (destination.get("postKind") or "self").strip().lower()
    title = (destination.get("title") or payload.get("title") or "").strip()
    body = destination.get("body") if destination.get("body") is not None else payload.get("body", "")
    link_url = (destination.get("linkUrl") or "").strip()
    if not subreddits:
        raise SocialPublishValidationError("Reddit 子版块列表不能为空")

    normalized_subreddits = []
    for subreddit in subreddits:
        normalized_subreddit = (subreddit or "").strip()
        if not normalized_subreddit:
            continue
        normalized_subreddits.append(normalized_subreddit)
    if not normalized_subreddits:
        raise SocialPublishValidationError("Reddit 子版块列表不能为空")

    results = []
    for account in accounts:
        credential_path = _get_cookie_path(account["filePath"])
        for subreddit in normalized_subreddits:
            if media_paths:
                results.append(
                    _result_entry(
                        "reddit",
                        account,
                        False,
                        "Reddit 原生媒体发布将在第二阶段实现",
                        subreddit=subreddit,
                        postKind=post_kind,
                    )
                )
                continue
            try:
                credentials = load_reddit_credentials(credential_path)
                post_data, refreshed_credentials = submit_reddit_post(
                    credentials,
                    subreddit=subreddit,
                    title=title,
                    post_kind=post_kind,
                    body=body,
                    url=link_url,
                )
                save_reddit_credentials(credential_path, refreshed_credentials)
                results.append(
                    _result_entry(
                        "reddit",
                        account,
                        True,
                        "发布成功",
                        subreddit=subreddit,
                        postKind=post_kind,
                        postId=post_data.get("id", ""),
                        postName=post_data.get("name", ""),
                        postUrl=post_data.get("url", ""),
                    )
                )
            except (RedditAPIError, FileNotFoundError, KeyError, ValueError) as exc:
                results.append(
                    _result_entry(
                        "reddit",
                        account,
                        False,
                        str(exc),
                        subreddit=subreddit,
                        postKind=post_kind,
                    )
                )
    return results


def publish_social_destinations(payload):
    destinations = payload.get("destinations") or []
    if not destinations:
        raise SocialPublishValidationError("destinations 不能为空")

    media_paths = _resolve_media_paths(payload.get("fileList", []))
    results = []
    for destination in destinations:
        platform = (destination.get("platform") or "").strip().lower()
        if platform == "x":
            results.extend(_publish_x_destination(payload, destination, media_paths))
        elif platform == "reddit":
            results.extend(_publish_reddit_destination(payload, destination, media_paths))
        else:
            raise SocialPublishValidationError(f"不支持的平台: {platform}")
    return results
