"""TikTok API analytics fetcher.

Uses the TikTok Content Posting API to list videos and fetch metrics.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

from myUtils import tiktok_auth
from myUtils.analytics.base import compute_engagement_rate

logger = logging.getLogger(__name__)

TIKTok_VIDEO_LIST_URL = "https://open.tiktokapis.com/v2/video/list/"
TIKTok_VIDEO_QUERY_URL = "https://open.tiktokapis.com/v2/video/query/"


def _refresh_token(config: dict[str, Any], session: requests.Session) -> str:
    """Refresh TikTok access token using tiktok_auth (reads credentials from env vars)."""
    refresh_token = str(config.get("refreshToken") or "").strip()
    if not refresh_token:
        raise ValueError("TikTok analytics requires refreshToken in account config")
    data = tiktok_auth.refresh_access_token(refresh_token=refresh_token, session=session)
    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        raise ValueError("TikTok token refresh did not return access_token")
    return access_token


def list_videos(
    access_token: str,
    session: requests.Session,
    max_results: int = 50,
) -> list[dict]:
    """List videos for the authenticated user."""
    videos = []
    cursor = None

    while len(videos) < max_results:
        json_body: dict[str, Any] = {"max_count": min(max_results - len(videos), 20)}
        if cursor:
            json_body["cursor"] = cursor

        resp = session.post(
            TIKTok_VIDEO_LIST_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json=json_body,
            params={"fields": "id,title,create_time,cover_image_url,view_count,like_count,comment_count,share_count,duration"},
            timeout=30,
        )

        # Check for scope/auth errors BEFORE raise_for_status, because
        # TikTok returns 401 for insufficient scopes instead of a JSON body.
        if resp.status_code == 401:
            err_code = ""
            try:
                err_body = resp.json()
                err_code = err_body.get("error", {}).get("code", "")
            except Exception:
                pass
            if err_code in ("scope_not_authorized", "insufficient_scope"):
                raise ValueError(
                    "TikTok account lacks video.list scope. "
                    "Re-authorize with video.list permission to enable analytics."
                )
            raise ValueError(
                "TikTok API returned 401 Unauthorized. "
                "The account may be missing the video.list scope — re-authorize to fix."
            )

        resp.raise_for_status()
        body = resp.json()

        if body.get("error", {}).get("code") != "ok":
            error_code = body.get("error", {}).get("code", "unknown")
            if error_code in ("scope_not_authorized", "insufficient_scope"):
                raise ValueError(
                    "TikTok account lacks video.list scope. "
                    "Re-authorize with video.list permission to enable analytics."
                )
            raise ValueError(f"TikTok API error: {body.get('error', {}).get('message', error_code)}")

        data = body.get("data", {})
        for item in data.get("videos", []):
            videos.append(item)

        cursor = data.get("cursor")
        if not cursor or not data.get("has_more", False):
            break

    return videos


def fetch_video_metrics(
    video_ids: list[str],
    access_token: str,
    session: requests.Session,
) -> list[dict]:
    """Fetch metrics for a list of video IDs."""
    results = []

    # TikTok query API accepts up to 20 IDs at a time
    for i in range(0, len(video_ids), 20):
        batch = video_ids[i:i + 20]
        resp = session.post(
            TIKTok_VIDEO_QUERY_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            params={"fields": "id,title,create_time,cover_image_url,view_count,like_count,comment_count,share_count,duration"},
            json={"filters": {"video_ids": batch}},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()

        if body.get("error", {}).get("code") != "ok":
            logger.warning("TikTok query error: %s", body.get("error", {}).get("message"))
            continue

        for item in body.get("data", {}).get("videos", []):
            views = int(item.get("view_count", 0))
            likes = int(item.get("like_count", 0))
            comments = int(item.get("comment_count", 0))
            shares = int(item.get("share_count", 0))

            create_time = item.get("create_time")
            published_at = None
            if create_time:
                from datetime import datetime, timezone
                published_at = datetime.fromtimestamp(create_time, tz=timezone.utc).isoformat()

            results.append({
                "platform_video_id": str(item.get("id", "")),
                "title": item.get("title", ""),
                "description": "",
                "thumbnail_url": item.get("cover_image_url", ""),
                "published_at": published_at,
                "duration_seconds": int(item.get("duration", 0)),
                "views": views,
                "likes": likes,
                "comments": comments,
                "shares": shares,
                "watch_time_seconds": 0,
                "raw_metrics": item,
            })

    return results


def sync_tiktok_account(
    config: dict[str, Any],
    session: requests.Session | None = None,
    max_videos: int = 100,
) -> list[dict]:
    """Fetch all video metrics for a TikTok account."""
    if session is None:
        session = requests.Session()

    access_token = _refresh_token(config, session)
    raw_videos = list_videos(access_token, session, max_results=max_videos)

    if not raw_videos:
        logger.info("TikTok: no videos found")
        return []

    video_ids = [str(v.get("id", "")) for v in raw_videos if v.get("id")]
    if not video_ids:
        return []

    videos = fetch_video_metrics(video_ids, access_token, session)

    for v in videos:
        v["engagement_rate"] = compute_engagement_rate(
            v["views"], v["likes"], v["comments"], v["shares"]
        )

    logger.info("TikTok: fetched %d videos", len(videos))
    return videos
