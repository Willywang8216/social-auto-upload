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
        "fields": "id,name,description,created_time,picture,permalink",
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
    """Fetch engagement metrics for a list of Facebook video IDs.

    Facebook Video objects do NOT support ``likes``, ``comments``, or
    ``shares`` fields (those are Post-only). View counts come from the
    ``video_insights`` edge, which requires the ``read_insights``
    permission.  If that permission is missing we log once and return
    zeros for all metrics.
    """
    results = []
    insights_perm_missing_logged = False

    for vid_id in video_ids:
        views = 0

        # Get video view count from the video_insights edge
        try:
            insight_resp = session.get(
                f"{META_GRAPH_ROOT}/{vid_id}/video_insights",
                params={
                    "metric": "total_video_views",
                    "access_token": access_token,
                },
                timeout=30,
            )
            insight_resp.raise_for_status()
            for data_point in insight_resp.json().get("data", []):
                for val in data_point.get("values", []):
                    views += int(val.get("value", 0))
        except Exception as exc:
            if not insights_perm_missing_logged:
                logger.warning("Facebook: video_insights unavailable for %s: %s", vid_id, exc)
                insights_perm_missing_logged = True

        results.append({
            "platform_video_id": vid_id,
            "views": views,
            "likes": 0,
            "comments": 0,
            "shares": 0,
            "raw_metrics": {},
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
        title = raw.get("name") or raw.get("description", "")[:100] or ""
        description = (raw.get("description") or "")[:500]

        # Thumbnail is the "picture" field (a URL string)
        thumbnail_url = raw.get("picture", "")

        videos.append({
            "platform_video_id": m["platform_video_id"],
            "title": title,
            "description": description,
            "thumbnail_url": thumbnail_url,
            "published_at": raw.get("created_time"),
            "duration_seconds": 0,  # Facebook API v25+ doesn't expose video length
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
