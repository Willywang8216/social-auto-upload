"""Single source of truth for per-platform posting limits (researched 2026-10-05).

The three enforcement layers — draft generation, media prep and the individual
publishers — used to carry four overlapping, mutually inconsistent tables
(TikTok was 150 chars in two places and 2200 in two others; Threads was both
1 GiB and 1024 MB; Bluesky was 90/295/300 MB). This module owns the numbers so
a limit can only be wrong in one place. Sources for every figure live in
``logs/platform-limits-research.md``.

Sizes are **decimal megabytes** (1 MB = 1_000_000 bytes) because that is the
unit the platforms publish their caps in — a MiB-based check waves through a
file the platform then rejects.

Only *API* limits are encoded, since this repo publishes through the APIs (and
the Sociamonials fallback); where an app limit is more generous it is noted in a
comment but not used.
"""

from __future__ import annotations

# Message/caption character limits. ``None`` means no hard cap (long-form).
MESSAGE_MAX_CHARS: dict[str, int | None] = {
    "twitter": 280,          # free tier; URLs count as 23, media does not
    "bluesky": 300,          # graphemes (and 3,000 UTF-8 bytes)
    "facebook": 63206,
    "instagram": 2200,
    "threads": 500,
    "tiktok": 2200,          # API cap (in-app allows 4000)
    "youtube": 5000,         # description; title is separate below
    "reddit": 40000,         # self-post body
    "telegram": 4096,        # text-only message; media caption is 1024
    "linkedin": 3000,
    "pinterest": 800,        # description (in-app textbox is 250)
    "discord": 2000,
    "google_my_business": 1500,
    "nw_sw_blog": None,
    "teaching_blog": None,
    "patreon": None,
    "medium": None,
    "substack": None,
}

# Field-scoped limits that are not the whole message.
TELEGRAM_CAPTION_MAX_CHARS = 1024
YOUTUBE_TITLE_MAX_CHARS = 100
YOUTUBE_TAGS_MAX_CHARS = 500
REDDIT_TITLE_MAX_CHARS = 300
BLUESKY_BYTE_MAX = 3000

# Largest single media file, decimal MB. The publisher that fetches from a URL
# is bound by the server-side fetch ceiling; the send path is bound by the
# platform's own API ceiling.
MEDIA_MAX_MB: dict[str, int] = {
    "twitter": 512,
    "bluesky": 300,
    "facebook": 4096,
    "instagram": 300,
    "threads": 1024,
    "tiktok": 4096,
    "youtube": 262144,
    "reddit": 1000,
    "telegram": 2000,
    "linkedin": 5000,
    "pinterest": 2048,
    "discord": 25,
}

# Maximum video duration in seconds. ``None`` = no platform cap.
VIDEO_MAX_SECONDS: dict[str, float | None] = {
    "twitter": 140.0,
    "bluesky": 600.0,
    "facebook": 14460.0,
    "instagram": 900.0,
    "threads": 300.0,
    "tiktok": 3600.0,
    "youtube": 43200.0,
    "reddit": 900.0,
    "linkedin": 900.0,
    "pinterest": 300.0,
    "telegram": None,
}

# How many media items one post may carry.
MAX_IMAGES: dict[str, int] = {
    "twitter": 4,
    "bluesky": 10,
    "facebook": 10,
    "instagram": 10,
    "threads": 20,
    "tiktok": 35,
    "reddit": 20,
    "telegram": 10,
    "linkedin": 20,
    "pinterest": 1,
}
MAX_VIDEOS: dict[str, int] = {
    "twitter": 1,
    "bluesky": 1,
    "facebook": 1,
    "instagram": 1,
    "threads": 1,
    "tiktok": 1,
    "reddit": 1,
    "telegram": 10,
    "linkedin": 1,
    "pinterest": 1,
    "youtube": 1,
}

# Platforms this repo can publish to at all (used to reject an unknown platform
# instead of silently treating its limit as "unlimited").
SUPPORTED_PLATFORMS: frozenset[str] = frozenset(
    {"twitter", "bluesky", "facebook", "instagram", "threads", "tiktok",
     "youtube", "reddit", "telegram", "linkedin", "pinterest"}
)

# Sociamonials network codes -> the same limits.
NETWORK_TO_PLATFORM: dict[str, str] = {
    "tw": "twitter",
    "blsk": "bluesky",
    "fb": "facebook",
    "in": "instagram",
    "thrd": "threads",
    "tiktok": "tiktok",
    "yt": "youtube",
    "reddit": "reddit",
    "telegram": "telegram",
    "ln": "linkedin",
    "pi": "pinterest",
}


def _lookup(table: dict, platform: str | None):
    key = str(platform or "").strip().lower()
    return table.get(key)


def message_max_chars(platform: str | None) -> int | None:
    return _lookup(MESSAGE_MAX_CHARS, platform)


def media_max_mb(platform: str | None) -> int | None:
    return _lookup(MEDIA_MAX_MB, platform)


def video_max_seconds(platform: str | None) -> float | None:
    return _lookup(VIDEO_MAX_SECONDS, platform)


def max_images(platform: str | None) -> int | None:
    return _lookup(MAX_IMAGES, platform)


def max_videos(platform: str | None) -> int | None:
    return _lookup(MAX_VIDEOS, platform)


def limits_for_platform(platform: str | None) -> dict:
    key = str(platform or "").strip().lower()
    return {
        "platform": key,
        "message_max_chars": MESSAGE_MAX_CHARS.get(key),
        "media_max_mb": MEDIA_MAX_MB.get(key),
        "video_max_seconds": VIDEO_MAX_SECONDS.get(key),
        "max_images": MAX_IMAGES.get(key),
        "max_videos": MAX_VIDEOS.get(key),
    }


def limits_for_network(network: str | None) -> dict:
    return limits_for_platform(NETWORK_TO_PLATFORM.get(str(network or "").strip().lower(), ""))
