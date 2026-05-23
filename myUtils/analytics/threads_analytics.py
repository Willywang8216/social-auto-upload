"""Threads post analytics fetcher.

Uses the Threads API to list user posts and fetch
like/reply/repost counts.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from myUtils.analytics.base import compute_engagement_rate

logger = logging.getLogger(__name__)

THREADS_GRAPH_ROOT = "https://graph.threads.net/v1.0"


def _get_access_token(config: dict[str, Any]) -> str:
    """Return the access token from config."""
    token = str(config.get("accessToken") or "").strip()
    if not token:
        raise ValueError("Threads analytics requires accessToken in account config")
    return token


def list_threads_posts(
    user_id: str,
    access_token: str,
    session: requests.Session,
    max_results: int = 100,
) -> list[dict]:
    """Return a list of raw thread post objects for the given user."""
    posts = []
    url = f"{THREADS_GRAPH_ROOT}/{user_id}/threads"
    params = {
        "fields": "id,text,timestamp,media_url,permalink,like_count,reply_count,repost_count,media_product_type",
        "access_token": access_token,
        "limit": min(max_results, 100),
    }

    while len(posts) < max_results:
        resp = session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        items = data.get("data", [])
        posts.extend(items)

        paging = data.get("paging", {})
        next_url = paging.get("next")
        if not next_url or len(posts) >= max_results:
            break
        url = next_url
        params = {}

    return posts[:max_results]


def sync_threads_account(
    config: dict[str, Any],
    session: requests.Session | None = None,
    max_videos: int = 100,
) -> list[dict]:
    """Fetch all post metrics for a Threads account.

    Returns a list of normalised video dicts ready for the analytics store.
    """
    if session is None:
        session = requests.Session()

    user_id = str(config.get("threadUserId") or config.get("userId") or "").strip()
    if not user_id:
        raise ValueError("Threads account config missing threadUserId")

    access_token = _get_access_token(config)
    raw_posts = list_threads_posts(user_id, access_token, session, max_results=max_videos)

    if not raw_posts:
        logger.info("Threads user %s has no posts", user_id)
        return []

    videos = []
    for item in raw_posts:
        likes = int(item.get("like_count", 0))
        comments = int(item.get("reply_count", 0))
        shares = int(item.get("repost_count", 0))

        text = item.get("text", "") or ""
        title = text.split("\n")[0][:100] if text else ""
        description = text[:500]

        videos.append({
            "platform_video_id": item["id"],
            "title": title,
            "description": description,
            "thumbnail_url": item.get("media_url", ""),
            "published_at": item.get("timestamp"),
            "duration_seconds": 0,
            "views": 0,  # Threads API doesn't expose view count
            "likes": likes,
            "comments": comments,
            "shares": shares,
            "watch_time_seconds": 0,
            "raw_metrics": item,
            "engagement_rate": compute_engagement_rate(0, likes, comments, shares),
        })

    logger.info("Threads: fetched %d posts for user %s", len(videos), user_id)
    return videos
