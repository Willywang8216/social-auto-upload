"""YouTube Data API v3 analytics fetcher.

Uses the youtube.readonly scope (already granted) to list channel videos
and fetch view/like/comment/share counts.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import requests

from myUtils.analytics.base import compute_engagement_rate

logger = logging.getLogger(__name__)

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _refresh_token(config: dict[str, Any], session: requests.Session) -> str:
    """Refresh YouTube access token and return the new access_token.

    Client credentials are stored as environment variable names in the
    account config (``clientIdEnv`` / ``clientSecretEnv``) and resolved
    at runtime — matching the pattern used by ``youtube_auth.py``.
    """
    import os

    client_id_env = config.get("clientIdEnv") or "YT_CLIENT_ID"
    client_secret_env = config.get("clientSecretEnv") or "YT_CLIENT_SECRET"
    client_id = str(os.environ.get(client_id_env, "") or "").strip()
    client_secret = str(os.environ.get(client_secret_env, "") or "").strip()
    refresh_token = str(config.get("refreshToken") or "").strip()
    if not client_id or not client_secret or not refresh_token:
        raise ValueError(
            "YouTube analytics requires env vars "
            f"{client_id_env}/{client_secret_env} and refreshToken in account config"
        )

    resp = session.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if not resp.ok:
        err_desc = ""
        try:
            err_body = resp.json()
            err_desc = err_body.get("error_description") or err_body.get("error") or ""
        except Exception:
            pass
        logger.error(
            "YouTube token refresh failed for %s: %s — %s",
            config.get("channelId", "?"),
            resp.status_code,
            err_desc,
        )
        raise ValueError(
            f"YouTube token refresh failed ({resp.status_code}): {err_desc or 'refresh token may be revoked — re-authorize the account'}"
        )
    token_data = resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        raise ValueError("Google token response did not include access_token")
    return access_token


def list_channel_videos(
    channel_id: str,
    access_token: str,
    session: requests.Session,
    max_results: int = 50,
) -> list[str]:
    """Return a list of video IDs for the given channel."""
    video_ids = []
    page_token = None

    while True:
        params = {
            "part": "snippet",
            "channelId": channel_id,
            "type": "video",
            "order": "date",
            "maxResults": min(max_results - len(video_ids), 50),
        }
        if page_token:
            params["pageToken"] = page_token

        resp = session.get(
            YOUTUBE_SEARCH_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
            timeout=30,
        )
        if resp.status_code in (400, 401, 403):
            error = resp.json().get("error", {})
            msg = error.get("message", "")
            logger.warning("YouTube channel %s: API error %d (%s). Re-authorize if needed.", channel_id, resp.status_code, msg[:200])
            return []
        resp.raise_for_status()
        data = resp.json()

        for item in data.get("items", []):
            vid = item.get("id", {}).get("videoId")
            if vid:
                video_ids.append(vid)

        page_token = data.get("nextPageToken")
        if not page_token or len(video_ids) >= max_results:
            break

    return video_ids


def fetch_video_metrics(
    video_ids: list[str],
    access_token: str,
    session: requests.Session,
) -> list[dict]:
    """Fetch metrics for a batch of video IDs (max 50 per call)."""
    results = []

    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        resp = session.get(
            YOUTUBE_VIDEOS_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "part": "statistics,snippet,contentDetails",
                "id": ",".join(batch),
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        for item in data.get("items", []):
            stats = item.get("statistics", {})
            snippet = item.get("snippet", {})
            content = item.get("contentDetails", {})

            views = int(stats.get("viewCount", 0))
            likes = int(stats.get("likeCount", 0))
            comments = int(stats.get("commentCount", 0))
            # YouTube doesn't provide share count via Data API v3
            shares = 0

            published_at = snippet.get("publishedAt")
            thumbnail = snippet.get("thumbnails", {}).get("default", {}).get("url", "")

            results.append({
                "platform_video_id": item["id"],
                "title": snippet.get("title", ""),
                "description": snippet.get("description", "")[:500],
                "thumbnail_url": thumbnail,
                "published_at": published_at,
                "duration_seconds": _parse_youtube_duration(content.get("duration", "")),
                "views": views,
                "likes": likes,
                "comments": comments,
                "shares": shares,
                "watch_time_seconds": 0,  # Not available via Data API v3
                "raw_metrics": stats,
            })

    return results


def _parse_youtube_duration(iso_duration: str) -> int:
    """Parse ISO 8601 duration (e.g. PT1M30S) to seconds."""
    if not iso_duration:
        return 0
    import re
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso_duration)
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def sync_youtube_account(
    config: dict[str, Any],
    session: requests.Session | None = None,
    max_videos: int = 100,
) -> list[dict]:
    """Fetch all video metrics for a YouTube account.

    Returns a list of normalised video dicts ready for the analytics store.
    """
    if session is None:
        session = requests.Session()

    channel_id = str(config.get("channelId") or "").strip()
    if not channel_id:
        raise ValueError("YouTube account config missing channelId")

    try:
        access_token = _refresh_token(config, session)
    except ValueError as exc:
        logger.warning("YouTube channel %s: token refresh failed (%s). Re-authorize the account.", channel_id, exc)
        raise  # surface auth failure so sync logs it as an error
    video_ids = list_channel_videos(channel_id, access_token, session, max_results=max_videos)

    if not video_ids:
        logger.info("YouTube channel %s has no videos", channel_id)
        return []

    videos = fetch_video_metrics(video_ids, access_token, session)

    # Compute engagement rates
    for v in videos:
        v["engagement_rate"] = compute_engagement_rate(
            v["views"], v["likes"], v["comments"], v["shares"]
        )

    logger.info("YouTube: fetched %d videos for channel %s", len(videos), channel_id)
    return videos
