"""Sync orchestrator for video analytics.

Coordinates fetching metrics from all supported platforms and storing
them in the analytics tables.
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from myUtils import analytics_store
from myUtils.analytics.base import compute_engagement_rate
from myUtils.profiles import Account, get_account, list_accounts

logger = logging.getLogger(__name__)

SUPPORTED_PLATFORMS = {"youtube", "tiktok"}


def _sync_youtube(account: Account, db_path: Path, session: requests.Session) -> int:
    from myUtils.analytics.youtube_analytics import sync_youtube_account
    config = account.config or {}
    videos = sync_youtube_account(config, session)
    return _store_videos(account, videos, db_path)


def _sync_tiktok(account: Account, db_path: Path, session: requests.Session) -> int:
    from myUtils.analytics.tiktok_analytics import sync_tiktok_account
    config = account.config or {}
    videos = sync_tiktok_account(config, session)
    return _store_videos(account, videos, db_path)


def _store_videos(account: Account, videos: list[dict], db_path: Path) -> int:
    """Store fetched video data into analytics tables. Returns count stored."""
    count = 0
    for v in videos:
        analytics_store.upsert_video(
            account_id=account.id,
            platform=account.platform,
            platform_video_id=v["platform_video_id"],
            title=v.get("title", ""),
            description=v.get("description", ""),
            thumbnail_url=v.get("thumbnail_url", ""),
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
            thumbnail_url=v.get("thumbnail_url", ""),
            published_at=v.get("published_at"),
            raw_metrics=v.get("raw_metrics", {}),
            db_path=db_path,
        )
        count += 1
    return count


_SYNC_FUNCTIONS = {
    "youtube": _sync_youtube,
    "tiktok": _sync_tiktok,
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

    Returns {"synced": int, "errors": list[str]}.
    """
    accounts = list_accounts(enabled=True, db_path=db_path)
    synced = 0
    errors = []

    for account in accounts:
        if account.platform not in SUPPORTED_PLATFORMS:
            continue
        if account.auth_type != "oauth":
            continue

        result = sync_account_analytics(account.id, db_path=db_path)
        if result["status"] == "completed":
            synced += 1
        elif result["status"] == "error":
            errors.append(f"{account.platform}/{account.account_name}: {result['error']}")

    return {"synced": synced, "errors": errors}
