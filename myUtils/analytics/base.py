"""Shared utilities for video analytics fetchers."""

from __future__ import annotations

from datetime import datetime, timezone


def compute_engagement_rate(views: int, likes: int, comments: int, shares: int) -> float:
    """Return engagement rate as a fraction (0.0-1.0+)."""
    if views <= 0:
        return 0.0
    return (likes + comments + shares) / views


def normalize_platform_video(platform: str, raw: dict) -> dict:
    """Return a dict with standardised fields for any platform's video data.

    Expected keys in *raw* vary by platform; this function maps them to a
    common schema used by the analytics store.
    """
    return {
        "platform": platform,
        "platform_video_id": str(raw.get("platform_video_id", "")),
        "title": raw.get("title", ""),
        "description": raw.get("description", ""),
        "thumbnail_url": raw.get("thumbnail_url", ""),
        "published_at": raw.get("published_at"),  # ISO string or None
        "duration_seconds": int(raw.get("duration_seconds", 0)),
        "views": int(raw.get("views", 0)),
        "likes": int(raw.get("likes", 0)),
        "comments": int(raw.get("comments", 0)),
        "shares": int(raw.get("shares", 0)),
        "watch_time_seconds": int(raw.get("watch_time_seconds", 0)),
        "raw_metrics": raw.get("raw_metrics", {}),
    }


def safe_isoformat(dt: datetime | None) -> str | None:
    """Convert a datetime to ISO-8601 string, handling None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()
