"""Instagram Business account media analytics fetcher.

Uses the Meta Graph API to list IG media and fetch
like/comment counts directly from the media edge.
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
        raise ValueError("Instagram analytics requires accessToken or metaUserAccessToken in account config")
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
        logger.warning("Instagram: cannot refresh token — missing %s/%s env vars", client_id_env, client_secret_env)
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
            logger.info("Instagram: refreshed long-lived user token")
    except Exception as exc:
        logger.warning("Instagram: token refresh failed (%s). Re-authorize the account.", exc)

    return config


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
        if resp.status_code in (400, 401, 403):
            error = resp.json().get("error", {})
            code = error.get("code", 0)
            msg = error.get("message", "")
            if code == 190 or "permission" in msg.lower() or resp.status_code == 401:
                logger.warning("Instagram user %s: %s", ig_user_id, msg[:300])
                raise requests.HTTPError(f"Instagram user {ig_user_id}: {msg[:200]}", response=resp)
            raise requests.HTTPError(f"Instagram API error: {msg}", response=resp)
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

    # Refresh token if near expiry
    _refresh_token_if_expired(config)

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
