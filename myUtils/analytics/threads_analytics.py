"""Threads post analytics fetcher.

Uses the Threads API to list user posts and fetch
like/reply/repost counts.
"""

from __future__ import annotations

import logging
import os
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


def _refresh_token_if_expired(config: dict[str, Any]) -> dict[str, Any]:
    """Refresh the Threads long-lived token if it's near expiry.

    Updates ``config`` in-place and returns it.
    """
    from datetime import datetime, timedelta

    expires_at = config.get("accessTokenExpiresAt")
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00").replace("+00:00", ""))
            if datetime.now() < exp_dt - timedelta(days=7):
                return config  # token still fresh (with 7-day buffer)
        except Exception:
            pass

    token = str(config.get("accessToken") or "").strip()
    if not token:
        return config

    client_secret_env = "THREADS_APP_SECRET"
    client_secret = str(os.environ.get(client_secret_env) or os.environ.get("THREADS_CLIENT_SECRET") or "").strip()
    if not client_secret:
        logger.warning("Threads: cannot refresh token — missing %s env var", client_secret_env)
        return config

    try:
        resp = requests.get(
            f"{THREADS_GRAPH_ROOT}/refresh_access_token",
            params={
                "grant_type": "th_refresh_token",
                "access_token": token,
                "client_secret": client_secret,
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        new_token = payload.get("access_token")
        if new_token:
            config["accessToken"] = new_token
            config["accessTokenUpdatedAt"] = datetime.now().isoformat(timespec="seconds")
            expires_in = payload.get("expires_in")
            if expires_in not in (None, ""):
                config["accessTokenExpiresAt"] = (
                    datetime.now() + timedelta(seconds=int(expires_in))
                ).isoformat(timespec="seconds")
            logger.info("Threads: refreshed long-lived token")
    except Exception as exc:
        logger.warning("Threads: token refresh failed (%s). Re-authorize the account.", exc)

    return config


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
        "fields": "id,text,timestamp,media_url,permalink,like_count,reply_count,repost_count,media_product_type,thumbnail_url",
        "access_token": access_token,
        "limit": min(max_results, 100),
    }

    while len(posts) < max_results:
        resp = session.get(url, params=params, timeout=30)
        if not resp.ok:
            error = {}
            try:
                error = resp.json().get("error", {})
            except Exception:
                pass
            code = error.get("code", 0)
            msg = error.get("message", resp.text[:200])
            # Auth errors — raise so sync reports an error
            if code == 190 or "permission" in msg.lower() or resp.status_code in (400, 401, 403):
                logger.warning("Threads user %s: auth error %d (%s). Token may be expired — re-authorize.", user_id, resp.status_code, msg[:200])
                raise requests.HTTPError(f"Threads user {user_id}: auth error ({resp.status_code}) — token may be expired", response=resp)
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

    # Refresh token if near expiry
    _refresh_token_if_expired(config)

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

        # Use thumbnail_url for videos (media_url is the video file), media_url for images
        thumb = item.get("thumbnail_url") or item.get("media_url") or ""

        videos.append({
            "platform_video_id": item["id"],
            "title": title,
            "description": description,
            "thumbnail_url": thumb,
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
