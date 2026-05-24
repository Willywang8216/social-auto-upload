"""Sync orchestrator for video analytics.

Coordinates fetching metrics from all supported platforms and storing
them in the analytics tables.
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from myUtils import analytics_store
from myUtils import do_spaces
from myUtils.analytics.base import compute_engagement_rate
from myUtils.profiles import Account, get_account, list_accounts

logger = logging.getLogger(__name__)

DO_SPACES_CDN_PREFIX = do_spaces.DO_SPACES_CDN_URL

SUPPORTED_PLATFORMS = {"youtube", "tiktok", "facebook", "instagram", "threads"}


def _sync_youtube(account: Account, db_path: Path, session: requests.Session) -> int:
    from myUtils.analytics.youtube_analytics import sync_youtube_account
    config = account.config or {}
    videos = sync_youtube_account(config, session)
    return _store_videos(account, videos, db_path)


def _sync_tiktok(account: Account, db_path: Path, session: requests.Session) -> int:
    from myUtils.analytics.tiktok_analytics import sync_tiktok_account
    from myUtils import tiktok_auth
    config = account.config or {}
    videos = sync_tiktok_account(config, session)
    # TikTok CDN requires Authorization header — get access token for thumbnail downloads
    refresh_token = str(config.get("refreshToken") or "").strip()
    auth_headers = {}
    if refresh_token:
        try:
            data = tiktok_auth.refresh_access_token(refresh_token=refresh_token, session=session)
            at = str(data.get("access_token") or "").strip()
            if at:
                auth_headers = {"Authorization": f"Bearer {at}"}
        except Exception:
            pass
    return _store_videos(account, videos, db_path, auth_headers=auth_headers)


def _sync_facebook(account: Account, db_path: Path, session: requests.Session) -> int:
    from myUtils.analytics.facebook_analytics import sync_facebook_account
    config = account.config or {}
    videos = sync_facebook_account(config, session)
    return _store_videos(account, videos, db_path)


def _sync_instagram(account: Account, db_path: Path, session: requests.Session) -> int:
    from myUtils.analytics.instagram_analytics import sync_instagram_account
    config = account.config or {}
    videos = sync_instagram_account(config, session)
    return _store_videos(account, videos, db_path)


def _sync_threads(account: Account, db_path: Path, session: requests.Session) -> int:
    from myUtils.analytics.threads_analytics import sync_threads_account
    config = account.config or {}
    videos = sync_threads_account(config, session)
    return _store_videos(account, videos, db_path)


def _store_thumbnail(
    original_url: str,
    platform: str,
    video_id: str,
    session: requests.Session,
    auth_headers: dict | None = None,
) -> str:
    """Download a thumbnail from the platform CDN and upload to DO Spaces.

    Returns the DO Spaces CDN URL on success, or the original URL as fallback.
    Skips the upload if the URL is already a DO Spaces CDN URL.
    """
    if not original_url:
        return original_url
    if DO_SPACES_CDN_PREFIX and original_url.startswith(DO_SPACES_CDN_PREFIX):
        return original_url  # already stored in DO Spaces

    try:
        headers = dict(auth_headers) if auth_headers else {}
        resp = session.get(original_url, timeout=15, stream=True, headers=headers)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "image/jpeg")
        ext = ".webp" if "webp" in content_type else ".jpg"
        key = f"thumbnails/{platform}/{video_id}{ext}"
        return do_spaces.upload_bytes(resp.content, key, content_type)
    except Exception:
        logger.debug("Thumbnail download failed for %s/%s, using original URL", platform, video_id)
        return original_url


def _store_videos(account: Account, videos: list[dict], db_path: Path, auth_headers: dict | None = None) -> int:
    """Store fetched video data into analytics tables. Returns count stored."""
    count = 0
    session = requests.Session()
    for v in videos:
        # Upload thumbnail to DO Spaces for permanent storage
        thumb_url = v.get("thumbnail_url", "")
        if thumb_url:
            thumb_url = _store_thumbnail(
                thumb_url, account.platform, v["platform_video_id"], session,
                auth_headers=auth_headers,
            )

        analytics_store.upsert_video(
            account_id=account.id,
            platform=account.platform,
            platform_video_id=v["platform_video_id"],
            title=v.get("title", ""),
            description=v.get("description", ""),
            thumbnail_url=thumb_url,
            published_at=v.get("published_at"),
            duration_seconds=v.get("duration_seconds", 0),
            db_path=db_path,
        )
        analytics_store.record_snapshot(
            account_id=account.id,
            platform=account.platform,
            platform_video_id=v["platform_video_id"],
            views=v.get("views", 0),
            likes=v.get("likes", 0),
            comments=v.get("comments", 0),
            shares=v.get("shares", 0),
            watch_time_seconds=v.get("watch_time_seconds", 0),
            engagement_rate=v.get("engagement_rate", 0.0),
            title=v.get("title", ""),
            thumbnail_url=thumb_url,
            published_at=v.get("published_at"),
            raw_metrics=v.get("raw_metrics", {}),
            db_path=db_path,
        )
        count += 1
    return count


_SYNC_FUNCTIONS = {
    "youtube": _sync_youtube,
    "tiktok": _sync_tiktok,
    "facebook": _sync_facebook,
    "instagram": _sync_instagram,
    "threads": _sync_threads,
}


def sync_account_analytics(account_id: int, db_path: Path = analytics_store.DB_PATH) -> dict:
    """Sync analytics for a single account.

    Returns {"status": "completed"|"skipped"|"error", "videos_synced": N, "error": str|None}.
    """
    account = get_account(account_id, db_path=db_path)
    platform = account.platform

    if platform not in SUPPORTED_PLATFORMS:
        return {"status": "skipped", "videos_synced": 0, "error": f"Platform {platform} not supported for analytics"}

    if account.auth_type != "oauth":
        return {"status": "skipped", "videos_synced": 0, "error": f"Account uses {account.auth_type} auth, not OAuth"}

    sync_fn = _SYNC_FUNCTIONS.get(platform)
    if not sync_fn:
        return {"status": "skipped", "videos_synced": 0, "error": f"No sync function for {platform}"}

    sync_id = analytics_store.record_sync_start(account_id, platform, db_path=db_path)
    session = requests.Session()

    try:
        count = sync_fn(account, db_path, session)
        analytics_store.record_sync_finish(
            sync_id, status="completed", videos_synced=count, db_path=db_path,
        )
        logger.info("Analytics sync completed for account %d (%s): %d videos", account_id, platform, count)
        return {"status": "completed", "videos_synced": count, "error": None}
    except Exception as exc:
        error_msg = str(exc)
        analytics_store.record_sync_finish(
            sync_id, status="failed", error_text=error_msg, db_path=db_path,
        )
        logger.error("Analytics sync failed for account %d (%s): %s", account_id, platform, error_msg)
        return {"status": "error", "videos_synced": 0, "error": error_msg}


def sync_all_analytics(db_path: Path = analytics_store.DB_PATH) -> dict:
    """Sync analytics for all enabled OAuth accounts.

    Returns {"synced": int, "skipped": int, "errors": list[str], "skipped_details": list[str]}.
    """
    # Clean up data from deleted accounts to prevent duplicate entries
    analytics_store.cleanup_orphaned_snapshots(db_path)
    analytics_store.cleanup_orphaned_videos(db_path)

    accounts = list_accounts(enabled=True, db_path=db_path)
    synced = 0
    skipped = 0
    errors = []
    skipped_details = []

    for account in accounts:
        if account.platform not in SUPPORTED_PLATFORMS:
            skipped += 1
            skipped_details.append(f"{account.platform}/{account.account_name}: platform not supported")
            continue
        if account.auth_type != "oauth":
            skipped += 1
            skipped_details.append(f"{account.platform}/{account.account_name}: auth_type is '{account.auth_type}', not 'oauth'")
            continue

        result = sync_account_analytics(account.id, db_path=db_path)
        if result["status"] == "completed":
            synced += 1
        elif result["status"] == "error":
            errors.append(f"{account.platform}/{account.account_name}: {result['error']}")

    return {"synced": synced, "skipped": skipped, "errors": errors, "skipped_details": skipped_details}
