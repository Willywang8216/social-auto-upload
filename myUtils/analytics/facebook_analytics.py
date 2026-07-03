"""Facebook Page video analytics fetcher.

Uses the Meta Graph API to list page videos and fetch
view/like/comment/share counts.
"""

from __future__ import annotations

import logging
import os
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


def _refresh_token_if_expired(config: dict[str, Any]) -> dict[str, Any]:
    """Refresh the Meta long-lived user token if it's near expiry.

    Updates ``config`` in-place and returns it.  Uses the
    ``metaUserAccessToken`` to exchange for a fresh long-lived token via
    the ``fb_exchange_token`` grant.
    """
    from datetime import datetime, timedelta

    updated_at = config.get("accessTokenUpdatedAt")
    if updated_at:
        try:
            updated_dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00").replace("+00:00", ""))
        except Exception:
            updated_dt = None
        if updated_dt and datetime.now() - updated_dt < timedelta(days=50):
            return config  # token still fresh

    user_token = str(config.get("metaUserAccessToken") or config.get("accessToken") or "").strip()
    if not user_token:
        return config  # nothing to refresh

    client_id_env = "META_APP_ID"
    client_secret_env = "META_APP_SECRET"
    client_id = str(os.environ.get(client_id_env) or os.environ.get("FACEBOOK_APP_ID") or os.environ.get("FB_APP_ID") or "").strip()
    client_secret = str(os.environ.get(client_secret_env) or os.environ.get("FACEBOOK_APP_SECRET") or os.environ.get("FB_APP_SECRET") or "").strip()
    if not client_id or not client_secret:
        logger.warning("Facebook: cannot refresh token — missing %s/%s env vars", client_id_env, client_secret_env)
        return config

    try:
        resp = requests.get(
            f"{META_GRAPH_ROOT}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "fb_exchange_token": user_token,
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        new_token = payload.get("access_token")
        if new_token:
            config["metaUserAccessToken"] = new_token
            config["accessToken"] = new_token
            from datetime import datetime, timedelta
            config["accessTokenUpdatedAt"] = datetime.now().isoformat(timespec="seconds")
            expires_in = payload.get("expires_in")
            if expires_in not in (None, ""):
                config["accessTokenExpiresAt"] = (
                    datetime.now() + timedelta(seconds=int(expires_in))
                ).isoformat(timespec="seconds")
            logger.info("Facebook: refreshed long-lived user token")
    except Exception as exc:
        logger.warning("Facebook: token refresh failed (%s). Re-authorize the account.", exc)

    return config


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
        "fields": "id,title,description,created_time,picture,permalink",
        "access_token": access_token,
        "limit": min(max_results, 100),
    }

    while len(videos) < max_results:
        resp = session.get(url, params=params, timeout=30)
        if resp.status_code in (400, 401, 403):
            error = resp.json().get("error", {})
            code = error.get("code", 0)
            msg = error.get("message", "")
            # Auth/permission errors — raise with the actual API message
            if code == 190 or "permission" in msg.lower() or resp.status_code == 401:
                logger.warning("Facebook page %s: %s", page_id, msg[:300])
                raise requests.HTTPError(f"Facebook page {page_id}: {msg[:200]}", response=resp)
            raise requests.HTTPError(f"Facebook API error: {msg}", response=resp)
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

    # Refresh token if near expiry
    _refresh_token_if_expired(config)

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
        title = raw.get("title") or raw.get("name") or raw.get("description", "")[:100] or ""
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
