"""Profile-aware content generation service.

Uses the LLM client to generate platform-specific post content based on
profile settings, media analysis, and platform constraints.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from utils.conf_defaults import BASE_DIR

DB_PATH = Path(BASE_DIR) / "db" / "database.db"

# Platform character limits for generated content
PLATFORM_CHAR_LIMITS = {
    "twitter": 280,
    "threads": 500,
    "instagram": 2200,
    "facebook": 63206,
    "tiktok": 150,
    "youtube": 5000,
    "reddit": 10000,
    "telegram": 4096,
    "discord": 2000,
    "patreon": 10000,
    "bluesky": 300,
    "linkedin": 3000,
    "pinterest": 500,
}

# Platforms excluded from Google Sheet export by default
SHEET_EXCLUDED_PLATFORMS = {"telegram", "patreon", "discord"}


@dataclass(slots=True)
class PreparedPost:
    id: int
    campaign_id: int
    media_group_id: int | None
    profile_id: int | None
    platform: str
    account_id: int | None
    target_name: str
    message: str
    title: str
    description: str
    first_comment: str
    hashtags: str
    link: str
    image_urls: str
    video_url: str
    video_thumbnail_url: str
    alt_text: str
    post_preset: str
    category: str
    watermark_name: str
    hashtag_group: str
    cta_group: str
    story_flag: bool
    pinterest_board: str
    scheduled_month: str
    scheduled_day: str
    scheduled_year: str
    scheduled_hour: str
    scheduled_minute: str
    status: str
    validation_errors: list
    llm_raw_output: dict
    char_count: int
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["story_flag"] = int(self.story_flag)
        return d


def _resolve_db_path(db_path: Path | None) -> Path:
    return db_path if db_path is not None else DB_PATH


@contextmanager
def _connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    resolved = _resolve_db_path(db_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_post(row: sqlite3.Row) -> PreparedPost:
    data = {key: row[key] for key in row.keys()}
    try:
        data["validation_errors"] = json.loads(data.get("validation_errors_json", "[]") or "[]")
    except (json.JSONDecodeError, TypeError):
        data["validation_errors"] = []
    try:
        data["llm_raw_output"] = json.loads(data.get("llm_raw_output_json", "{}") or "{}")
    except (json.JSONDecodeError, TypeError):
        data["llm_raw_output"] = {}
    data["story_flag"] = bool(data.get("story_flag", 0))
    data.pop("validation_errors_json", None)
    data.pop("llm_raw_output_json", None)
    # Filter to only valid dataclass fields (protects against schema evolution)
    valid = {f.name for f in fields(PreparedPost)}
    data = {k: v for k, v in data.items() if k in valid}
    return PreparedPost(**data)


# --------------- CRUD ---------------

def create_prepared_post(
    *,
    campaign_id: int,
    media_group_id: int | None = None,
    profile_id: int | None = None,
    platform: str,
    account_id: int | None = None,
    target_name: str = "",
    message: str = "",
    title: str = "",
    description: str = "",
    first_comment: str = "",
    hashtags: str = "",
    link: str = "",
    image_urls: str = "",
    video_url: str = "",
    video_thumbnail_url: str = "",
    alt_text: str = "",
    post_preset: str = "",
    category: str = "",
    watermark_name: str = "",
    hashtag_group: str = "",
    cta_group: str = "",
    story_flag: bool = False,
    pinterest_board: str = "",
    scheduled_month: str = "",
    scheduled_day: str = "",
    scheduled_year: str = "",
    scheduled_hour: str = "",
    scheduled_minute: str = "",
    status: str = "generated",
    validation_errors: list | None = None,
    llm_raw_output: dict | None = None,
    char_count: int = 0,
    workspace_id: str | None = None,
    db_path: Path | None = None,
) -> PreparedPost:
    now = _now_iso()
    columns = [
        "campaign_id", "media_group_id", "profile_id", "platform", "account_id",
        "target_name", "message", "title", "description", "first_comment", "hashtags",
        "link", "image_urls", "video_url", "video_thumbnail_url", "alt_text",
        "post_preset", "category", "watermark_name", "hashtag_group", "cta_group",
        "story_flag", "pinterest_board", "scheduled_month", "scheduled_day",
        "scheduled_year", "scheduled_hour", "scheduled_minute", "status",
        "validation_errors_json", "llm_raw_output_json", "char_count",
        "created_at", "updated_at",
    ]
    values: list = [
        campaign_id, media_group_id, profile_id, platform, account_id,
        target_name, message, title, description, first_comment, hashtags,
        link, image_urls, video_url, video_thumbnail_url, alt_text,
        post_preset, category, watermark_name, hashtag_group, cta_group,
        int(story_flag), pinterest_board, scheduled_month, scheduled_day,
        scheduled_year, scheduled_hour, scheduled_minute, status,
        json.dumps(validation_errors or []),
        json.dumps(llm_raw_output or {}),
        char_count, now, now,
    ]
    # Reference the workspace_id column only when a scope is supplied so the
    # default single-tenant INSERT stays byte-identical to before.
    if workspace_id is not None:
        columns.append("workspace_id")
        values.append(workspace_id)
    placeholders = ",".join("?" for _ in columns)
    with _connect(db_path) as conn:
        cur = conn.execute(
            f"INSERT INTO prepared_posts ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM prepared_posts WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _row_to_post(row)


def get_prepared_post(
    post_id: int, *, workspace_id: str | None = None, db_path: Path | None = None
) -> PreparedPost:
    with _connect(db_path) as conn:
        if workspace_id is not None:
            row = conn.execute(
                "SELECT * FROM prepared_posts WHERE id = ? AND workspace_id = ?",
                (post_id, workspace_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM prepared_posts WHERE id = ?", (post_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"PreparedPost {post_id} not found")
        return _row_to_post(row)


def list_prepared_posts(
    *,
    campaign_id: int | None = None,
    platform: str | None = None,
    status: str | None = None,
    workspace_id: str | None = None,
    db_path: Path | None = None,
) -> list[PreparedPost]:
    with _connect(db_path) as conn:
        query = "SELECT * FROM prepared_posts WHERE 1=1"
        params: list = []
        if workspace_id is not None:
            query += " AND workspace_id = ?"
            params.append(workspace_id)
        if campaign_id is not None:
            query += " AND campaign_id = ?"
            params.append(campaign_id)
        if platform:
            query += " AND platform = ?"
            params.append(platform)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY platform, target_name"
        rows = conn.execute(query, params).fetchall()
        return [_row_to_post(r) for r in rows]


def update_prepared_post(
    post_id: int, *, workspace_id: str | None = None, db_path: Path | None = None, **fields
) -> PreparedPost:
    allowed = {
        "message", "title", "description", "first_comment", "hashtags",
        "link", "image_urls", "video_url", "video_thumbnail_url", "alt_text",
        "post_preset", "category", "watermark_name", "hashtag_group", "cta_group",
        "story_flag", "pinterest_board", "scheduled_month", "scheduled_day",
        "scheduled_year", "scheduled_hour", "scheduled_minute",
        "status", "validation_errors", "llm_raw_output", "char_count",
        "target_name",
    }
    updates = {}
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "validation_errors":
            k = "validation_errors_json"
            v = json.dumps(v)
        elif k == "llm_raw_output":
            k = "llm_raw_output_json"
            v = json.dumps(v)
        elif k == "story_flag":
            v = int(bool(v))
        updates[k] = v
    if not updates:
        return get_prepared_post(post_id, workspace_id=workspace_id, db_path=db_path)
    updates["updated_at"] = _now_iso()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    where = "WHERE id = ?"
    values = list(updates.values()) + [post_id]
    if workspace_id is not None:
        where += " AND workspace_id = ?"
        values.append(workspace_id)
    with _connect(db_path) as conn:
        conn.execute(f"UPDATE prepared_posts SET {set_clause} {where}", values)
        conn.commit()
        row = conn.execute(
            "SELECT * FROM prepared_posts " + where,
            [post_id] + ([workspace_id] if workspace_id is not None else []),
        ).fetchone()
        if row is None:
            raise ValueError(f"PreparedPost {post_id} not found")
        return _row_to_post(row)


def delete_prepared_post(
    post_id: int, *, workspace_id: str | None = None, db_path: Path | None = None
) -> None:
    with _connect(db_path) as conn:
        if workspace_id is not None:
            conn.execute(
                "DELETE FROM prepared_posts WHERE id = ? AND workspace_id = ?",
                (post_id, workspace_id),
            )
        else:
            conn.execute("DELETE FROM prepared_posts WHERE id = ?", (post_id,))
        conn.commit()


# --------------- Platform-specific prompt builders ---------------

def _build_twitter_prompt(profile: dict, media_info: dict, context: dict) -> str:
    return f"""Generate a tweet for Twitter/X.

Rules:
- Maximum 280 characters
- Include exactly 3 hashtags
- Include at least one emoji at the start
- Be engaging and concise

Profile brand: {profile.get('name', '')}
Writing style: {profile.get('writing_style_prompt', '')}
Contact: {profile.get('contact_details', '')}
CTA: {profile.get('default_cta', '')}
Default hashtags: {profile.get('default_hashtags', '')}

Content topic: {media_info.get('topic', '')}
Key points: {media_info.get('key_points', '')}
User notes: {context.get('user_notes', '')}

Return ONLY a JSON object: {{"message": "...", "hashtags": ["#tag1", "#tag2", "#tag3"]}}"""


def _build_threads_prompt(profile: dict, media_info: dict, context: dict) -> str:
    return f"""Generate a Threads post.

Rules:
- Conversational, thread-style tone
- Include contact details and CTA naturally
- Avoid hashtag spam (2-5 hashtags max)
- Can be longer than a tweet

Profile brand: {profile.get('name', '')}
Writing style: {profile.get('writing_style_prompt', '')}
Contact: {profile.get('contact_details', '')}
CTA: {profile.get('default_cta', '')}

Content topic: {media_info.get('topic', '')}
Key points: {media_info.get('key_points', '')}
User notes: {context.get('user_notes', '')}

Return ONLY a JSON object: {{"message": "...", "first_comment": "...", "hashtags": [...]}}"""


def _build_instagram_prompt(profile: dict, media_info: dict, context: dict) -> str:
    return f"""Generate an Instagram caption.

Rules:
- Maximum 2,200 characters
- Include hook, body, CTA, contact details, and hashtags
- For carousels, write caption based on the whole group
- Generate alt text for images

Profile brand: {profile.get('name', '')}
Writing style: {profile.get('writing_style_prompt', '')}
Contact: {profile.get('contact_details', '')}
CTA: {profile.get('default_cta', '')}
Default hashtags: {profile.get('default_hashtags', '')}

Content topic: {media_info.get('topic', '')}
Key points: {media_info.get('key_points', '')}
User notes: {context.get('user_notes', '')}

Return ONLY a JSON object: {{"message": "...", "alt_text": "...", "first_comment": "...", "hashtags": [...]}}"""


def _build_facebook_prompt(profile: dict, media_info: dict, context: dict) -> str:
    return f"""Generate a Facebook post.

Rules:
- Longer, warm post style
- Include CTA and contact details
- Less hashtag-heavy than Instagram (1-3 max)

Profile brand: {profile.get('name', '')}
Writing style: {profile.get('writing_style_prompt', '')}
Contact: {profile.get('contact_details', '')}
CTA: {profile.get('default_cta', '')}

Content topic: {media_info.get('topic', '')}
Key points: {media_info.get('key_points', '')}
User notes: {context.get('user_notes', '')}

Return ONLY a JSON object: {{"message": "...", "first_comment": "...", "hashtags": [...]}}"""


def _build_youtube_prompt(profile: dict, media_info: dict, context: dict) -> str:
    return f"""Generate YouTube video metadata.

Rules:
- Title: compelling, SEO-friendly
- Description: detailed with timestamps if applicable, links, CTA
- Tags: relevant keywords
- Use the video transcript for context

Profile brand: {profile.get('name', '')}
Writing style: {profile.get('writing_style_prompt', '')}
Contact: {profile.get('contact_details', '')}
CTA: {profile.get('default_cta', '')}
Link: {profile.get('default_link', '')}

Transcript: {media_info.get('transcript', 'N/A')}
Content topic: {media_info.get('topic', '')}
Key points: {media_info.get('key_points', '')}
User notes: {context.get('user_notes', '')}

Return ONLY a JSON object: {{"title": "...", "description": "...", "tags": [...]}}"""


def _build_tiktok_prompt(profile: dict, media_info: dict, context: dict) -> str:
    return f"""Generate a TikTok caption.

Rules:
- Maximum 150 characters
- Include hashtags
- Short and catchy
- Match TikTok's casual style

Profile brand: {profile.get('name', '')}
Writing style: {profile.get('writing_style_prompt', '')}

Content topic: {media_info.get('topic', '')}
User notes: {context.get('user_notes', '')}

Return ONLY a JSON object: {{"message": "...", "hashtags": [...]}}"""


def _build_reddit_prompt(profile: dict, media_info: dict, context: dict) -> str:
    subreddit = context.get("subreddit", "")
    rules = context.get("subreddit_rules", "")
    return f"""Generate a Reddit post.

Rules:
- Title: attention-grabbing but not clickbait
- Body: detailed and authentic
- Respect subreddit rules if provided

Profile brand: {profile.get('name', '')}
Subreddit: {subreddit}
Subreddit rules: {rules}

Content topic: {media_info.get('topic', '')}
Key points: {media_info.get('key_points', '')}
User notes: {context.get('user_notes', '')}

Return ONLY a JSON object: {{"title": "...", "message": "..."}}"""


def _build_telegram_prompt(profile: dict, media_info: dict, context: dict) -> str:
    return f"""Generate a Telegram message.

Rules:
- Concise but complete
- Can include link/media references
- Professional but approachable

Profile brand: {profile.get('name', '')}
Contact: {profile.get('contact_details', '')}
Link: {profile.get('default_link', '')}

Content topic: {media_info.get('topic', '')}
User notes: {context.get('user_notes', '')}

Return ONLY a JSON object: {{"message": "..."}}"""


def _build_patreon_prompt(profile: dict, media_info: dict, context: dict) -> str:
    return f"""Generate a Patreon post.

Rules:
- Long-form supporter/community post
- Include title, body, teaser/summary
- Warm and engaging tone

Profile brand: {profile.get('name', '')}
Writing style: {profile.get('writing_style_prompt', '')}
Contact: {profile.get('contact_details', '')}
CTA: {profile.get('default_cta', '')}

Content topic: {media_info.get('topic', '')}
Key points: {media_info.get('key_points', '')}
User notes: {context.get('user_notes', '')}

Return ONLY a JSON object: {{"title": "...", "message": "...", "description": "..."}}"""


def _build_discord_prompt(profile: dict, media_info: dict, context: dict) -> str:
    return f"""Generate a Discord webhook message.

Rules:
- Concise message with media links
- Can use Discord markdown formatting
- Include relevant links

Profile brand: {profile.get('name', '')}
Link: {profile.get('default_link', '')}

Content topic: {media_info.get('topic', '')}
User notes: {context.get('user_notes', '')}

Return ONLY a JSON object: {{"message": "..."}}"""


PLATFORM_PROMPTS = {
    "twitter": _build_twitter_prompt,
    "threads": _build_threads_prompt,
    "instagram": _build_instagram_prompt,
    "facebook": _build_facebook_prompt,
    "youtube": _build_youtube_prompt,
    "tiktok": _build_tiktok_prompt,
    "reddit": _build_reddit_prompt,
    "telegram": _build_telegram_prompt,
    "patreon": _build_patreon_prompt,
    "discord": _build_discord_prompt,
}


def get_platform_prompt_builder(platform: str):
    """Get the prompt builder function for a platform."""
    return PLATFORM_PROMPTS.get(platform)


def build_generation_context(
    profile: dict,
    media_info: dict,
    platform: str,
    context: dict | None = None,
) -> tuple[str, str]:
    """Build system prompt and user prompt for LLM content generation.

    Returns (system_prompt, user_prompt).
    """
    system_prompt = profile.get("system_prompt", "") or (
        f"You are a social media content creator for {profile.get('name', 'a brand')}. "
        f"Writing style: {profile.get('writing_style_prompt', 'professional and engaging')}. "
        "Generate platform-specific content that matches each platform's conventions and limits."
    )

    builder = PLATFORM_PROMPTS.get(platform)
    if builder is None:
        # Generic fallback
        user_prompt = f"""Generate a {platform} post.

Profile: {profile.get('name', '')}
Style: {profile.get('writing_style_prompt', '')}
Contact: {profile.get('contact_details', '')}
CTA: {profile.get('default_cta', '')}
Topic: {media_info.get('topic', '')}
Notes: {(context or {}).get('user_notes', '')}

Return ONLY a JSON object with "message" field."""
    else:
        user_prompt = builder(profile, media_info, context or {})

    return system_prompt, user_prompt


def parse_llm_response(raw_content: str, platform: str) -> dict:
    """Parse the LLM JSON response into a structured dict."""
    # Try to extract JSON from the response
    content = raw_content.strip()
    # Strip markdown code fence (handles ```json, ```python, etc.)
    content = re.sub(r"^```\w*\s*\n?", "", content)
    content = re.sub(r"\n?```\w*\s*$", "", content)

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                parsed = json.loads(content[start:end])
            except json.JSONDecodeError:
                parsed = {"message": content}
        else:
            parsed = {"message": content}

    # Ensure hashtags is a list
    if "hashtags" in parsed and isinstance(parsed["hashtags"], str):
        parsed["hashtags"] = [h.strip() for h in parsed["hashtags"].split(",") if h.strip()]

    return parsed


def validate_post(platform: str, post_data: dict) -> list[str]:
    """Validate a generated post against platform constraints. Returns list of errors."""
    errors = []
    limit = PLATFORM_CHAR_LIMITS.get(platform)
    message = post_data.get("message", "")

    if not message.strip():
        errors.append("Message is empty")

    if limit is not None and len(message) > limit:
        errors.append(f"Message exceeds {platform} limit of {limit} chars (got {len(message)})")

    if platform == "twitter":
        hashtags = post_data.get("hashtags", [])
        # Handle hashtags stored as JSON string
        if isinstance(hashtags, str):
            try:
                hashtags = json.loads(hashtags)
            except (json.JSONDecodeError, TypeError):
                hashtags = [h.strip() for h in hashtags.split(",") if h.strip()]
        if len(hashtags) != 3:
            errors.append(f"Twitter requires exactly 3 hashtags (got {len(hashtags)})")
        # Check for emoji in first few characters (use broader scan for multi-codepoint emojis)
        emoji_found = False
        for char in message[:4]:
            cp = ord(char)
            # Common emoji ranges (not exhaustive but covers most cases)
            if cp > 0x2600 and cp not in range(0x4e00, 0xa000):
                emoji_found = True
                break
        if not emoji_found and message:
            errors.append("Twitter post should start with an emoji")

    return errors
