"""Facebook Page video analytics fetcher.

Uses the Meta Graph API to list page videos and fetch
view/like/comment/share counts.
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
        raise ValueError("Facebook analytics requires accessToken or metaUserAccessToken in account config")
    return token


def list_page_videos(
    page_id: str,
    access_token: str,
    session: requests.Session,
    max_results: int = 100,
) -> list[dict]:
    """Return a list of raw video objects for the given Facebook page."""
    videos = []
    url = f"{META_GRAPH_ROOT}/{page_id}/videos"
    params = {
        "fields": "id,title,description,created_time,thumbnails,permalink,length",
        "access_token": access_token,
        "limit": min(max_results, 100),
    }

    while len(videos) < max_results:
        resp = session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        items = data.get("data", [])
        videos.extend(items)

        paging = data.get("paging", {})
        next_url = paging.get("next")
        if not next_url or len(videos) >= max_results:
            break
        url = next_url
        params = {}  # next URL includes params

    return videos[:max_results]


def fetch_video_metrics(
    video_ids: list[str],
    access_token: str,
    session: requests.Session,
) -> list[dict]:
    """Fetch engagement metrics for a list of Facebook video IDs."""
    results = []

    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        ids_str = ",".join(batch)
        resp = session.get(
            META_GRAPH_ROOT,
            params={
                "ids": ids_str,
                "fields": "id,views,likes.summary(true),comments.summary(true),shares",
                "access_token": access_token,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        for vid_id, item in data.items():
            views = int(item.get("views", 0))
            likes = int(item.get("likes", {}).get("summary", {}).get("total_count", 0))
            comments = int(item.get("comments", {}).get("summary", {}).get("total_count", 0))
            shares = int(item.get("shares", {}).get("count", 0))

            results.append({
                "platform_video_id": vid_id,
                "views": views,
                "likes": likes,
                "comments": comments,
                "shares": shares,
                "raw_metrics": item,
            })

    return results


def sync_facebook_account(
    config: dict[str, Any],
    session: requests.Session | None = None,
    max_videos: int = 100,
) -> list[dict]:
    """Fetch all video metrics for a Facebook page.

    Returns a list of normalised video dicts ready for the analytics store.
    """
    if session is None:
        session = requests.Session()

    page_id = str(config.get("pageId") or "").strip()
    if not page_id:
        raise ValueError("Facebook account config missing pageId")

    access_token = _get_access_token(config)
    raw_videos = list_page_videos(page_id, access_token, session, max_results=max_videos)

    if not raw_videos:
        logger.info("Facebook page %s has no videos", page_id)
        return []

    video_ids = [v["id"] for v in raw_videos]
    metrics = fetch_video_metrics(video_ids, access_token, session)

    # Build a lookup from video ID to raw video metadata
    raw_lookup = {v["id"]: v for v in raw_videos}

    videos = []
    for m in metrics:
        raw = raw_lookup.get(m["platform_video_id"], {})
        title = raw.get("title") or raw.get("description", "")[:100] or ""
        description = (raw.get("description") or "")[:500]

        # Extract thumbnail from thumbnails array
        thumbnails = raw.get("thumbnails", {}).get("data", [])
        thumbnail_url = thumbnails[-1].get("uri", "") if thumbnails else ""

        videos.append({
            "platform_video_id": m["platform_video_id"],
            "title": title,
            "description": description,
            "thumbnail_url": thumbnail_url,
            "published_at": raw.get("created_time"),
            "duration_seconds": _parse_fb_duration(raw.get("length")),
            "views": m["views"],
            "likes": m["likes"],
            "comments": m["comments"],
            "shares": m["shares"],
            "watch_time_seconds": 0,
            "raw_metrics": m.get("raw_metrics", {}),
            "engagement_rate": compute_engagement_rate(
                m["views"], m["likes"], m["comments"], m["shares"]
            ),
        })

    logger.info("Facebook: fetched %d videos for page %s", len(videos), page_id)
    return videos


def _parse_fb_duration(seconds_val: Any) -> int:
    """Parse Facebook video length (in seconds) to int."""
    if not seconds_val:
        return 0
    try:
        return int(float(seconds_val))
    except (ValueError, TypeError):
        return 0
