"""Instagram Business account media analytics fetcher.

Uses the Meta Graph API to list IG media and fetch
like/comment counts directly from the media edge.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from myUtils.analytics.base import compute_engagement_rate

logger = logging.getLogger(__name__)

META_GRAPH_ROOT = "https://graph.facebook.com/v25.0"


def _get_access_token(config: dict[str, Any]) -> str:
    """Return the best available access token from config."""
    token = str(config.get("accessToken") or config.get("metaUserAccessToken") or "").strip()
    if not token:
        raise ValueError("Instagram analytics requires accessToken or metaUserAccessToken in account config")
    return token


def list_ig_media(
    ig_user_id: str,
    access_token: str,
    session: requests.Session,
    max_results: int = 100,
) -> list[dict]:
    """Return a list of raw media objects for the given Instagram business account."""
    media = []
    url = f"{META_GRAPH_ROOT}/{ig_user_id}/media"
    params = {
        "fields": "id,caption,media_type,media_url,thumbnail_url,timestamp,like_count,comments_count",
        "access_token": access_token,
        "limit": min(max_results, 100),
    }

    while len(media) < max_results:
        resp = session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        items = data.get("data", [])
        media.extend(items)

        paging = data.get("paging", {})
        next_url = paging.get("next")
        if not next_url or len(media) >= max_results:
            break
        url = next_url
        params = {}

    return media[:max_results]


def sync_instagram_account(
    config: dict[str, Any],
    session: requests.Session | None = None,
    max_videos: int = 100,
) -> list[dict]:
    """Fetch all media metrics for an Instagram business account.

    Returns a list of normalised video dicts ready for the analytics store.
    Only VIDEO and REEL media types are included.
    """
    if session is None:
        session = requests.Session()

    ig_user_id = str(config.get("igUserId") or "").strip()
    if not ig_user_id:
        raise ValueError("Instagram account config missing igUserId")

    access_token = _get_access_token(config)
    raw_media = list_ig_media(ig_user_id, access_token, session, max_results=max_videos)

    if not raw_media:
        logger.info("Instagram user %s has no media", ig_user_id)
        return []

    videos = []
    for item in raw_media:
        media_type = item.get("media_type", "")
        # Only include VIDEO and REEL types
        if media_type not in ("VIDEO", "REEL"):
            continue

        views = 0  # Instagram doesn't expose view count on the media edge
        likes = int(item.get("like_count", 0))
        comments = int(item.get("comments_count", 0))
        shares = 0  # Not available via API

        caption = item.get("caption", "") or ""
        title = caption.split("\n")[0][:100] if caption else ""
        description = caption[:500]

        videos.append({
            "platform_video_id": item["id"],
            "title": title,
            "description": description,
            "thumbnail_url": item.get("thumbnail_url") or item.get("media_url", ""),
            "published_at": item.get("timestamp"),
            "duration_seconds": 0,  # Not available on media edge
            "views": views,
            "likes": likes,
            "comments": comments,
            "shares": shares,
            "watch_time_seconds": 0,
            "raw_metrics": item,
            "engagement_rate": compute_engagement_rate(views, likes, comments, shares),
        })

    logger.info("Instagram: fetched %d media items for user %s", len(videos), ig_user_id)
    return videos
