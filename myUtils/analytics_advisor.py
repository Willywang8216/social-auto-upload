"""AI-powered analytics advisor.

Uses the existing LLM client to analyse video performance data
and provide actionable optimisation advice.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from myUtils import analytics_store
from myUtils.llm_client import generate_chat_completion

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a social media analytics advisor for a content creator who publishes videos across multiple platforms (YouTube, TikTok, Twitter/X, Instagram, Facebook, etc.).

Analyse the provided video performance data and give actionable optimisation advice. Be specific and data-driven.

Respond in JSON format with these keys:
- "summary": a 2-3 sentence overview of performance
- "insights": an array of 3-5 key observations from the data (each a string)
- "recommendations": an array of 3-5 specific actionable recommendations (each a string)
- "platformTips": an object keyed by platform name, each containing a string with platform-specific advice

Focus on:
- Which content themes/formats perform best
- Optimal posting times (if data shows patterns)
- Engagement rate analysis (what's good, what needs improvement)
- Platform-specific strategies
- Growth opportunities

Be concise and practical. No fluff."""


def generate_advice(
    *,
    platform: str | None = None,
    account_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    db_path: Path = analytics_store.DB_PATH,
) -> dict:
    """Generate AI-powered advice based on collected analytics data.

    Returns the parsed JSON response from the LLM.
    """
    # Gather data for the advisor
    stats = analytics_store.get_aggregate_stats(
        platform=platform, account_id=account_id,
        date_from=date_from, date_to=date_to,
        db_path=db_path,
    )
    top_videos = analytics_store.get_top_videos(
        platform=platform, account_id=account_id,
        metric="views", limit=20,
        db_path=db_path,
    )
    trends = analytics_store.get_trends(
        platform=platform, account_id=account_id,
        date_from=date_from, date_to=date_to,
        metric="views",
        db_path=db_path,
    )

    # Build data summary for the LLM
    data_summary = {
        "overview": {
            "total_views": stats.get("total_views", 0),
            "total_likes": stats.get("total_likes", 0),
            "total_comments": stats.get("total_comments", 0),
            "total_shares": stats.get("total_shares", 0),
            "avg_engagement_rate": round(stats.get("avg_engagement_rate", 0), 4),
            "video_count": stats.get("video_count", 0),
        },
        "per_platform": stats.get("per_platform", {}),
        "top_videos": [
            {
                "title": v.get("title", ""),
                "platform": v.get("platform", ""),
                "views": v.get("views", 0),
                "likes": v.get("likes", 0),
                "comments": v.get("comments", 0),
                "shares": v.get("shares", 0),
                "engagement_rate": round(v.get("engagement_rate", 0), 4),
                "published_at": v.get("published_at", ""),
            }
            for v in top_videos[:20]
        ],
        "recent_trends": trends[-30:],  # Last 30 data points
    }

    # If no data at all, return a helpful message
    if stats.get("video_count", 0) == 0:
        return {
            "summary": "No analytics data available yet. Sync your accounts first to start collecting video performance metrics.",
            "insights": [
                "No video data has been collected yet",
                "Connect your YouTube or TikTok accounts and run a sync to start tracking",
            ],
            "recommendations": [
                "Go to the analytics page and click 'Sync Now' to fetch your video data",
                "Ensure your OAuth tokens are valid and have the necessary scopes",
            ],
            "platformTips": {},
        }

    user_prompt = json.dumps(data_summary, ensure_ascii=False, default=str)

    try:
        result = generate_chat_completion(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.3,
            response_json=True,
            timeout_seconds=120,
        )
        return result.parsed_json or {
            "summary": result.content,
            "insights": [],
            "recommendations": [],
            "platformTips": {},
        }
    except Exception as exc:
        logger.error("LLM advisor failed: %s", exc)
        return {
            "summary": f"Unable to generate advice: {exc}",
            "insights": [],
            "recommendations": ["Check that the LLM API is configured (SAU_LLM_API_BASE_URL and SAU_LLM_API_KEY)"],
            "platformTips": {},
        }
