"""Shared publishing ceilings and conservative preparation policies.

Video sources were rechecked with Exa on 2026-11-05; citations and uncertainty
are recorded in ``logs/platform-limits-research.md``. A configured route may
have a lower limit than its consumer app (notably TikTok and Telegram).

Sizes here use decimal MB as a conservative interpretation unless the source
specifies exact bytes. The tables include compatibility policies, not just
universal provider maxima: X's legacy 140-second profile remains deliberate
for backup compatibility; the newer native entitlement is documented, not
silently enabled. ``preparation_limits`` adds account/transport constraints.
Unknown route limits must not be interpreted as permission to upload anything.
"""

from __future__ import annotations

# Legacy numeric lookup; units are field-specific (see MESSAGE_COUNT_UNITS and
# text_limits), not universally Python characters. None is explicit long-form.
MESSAGE_COUNT_UNITS = {"youtube": "utf8_bytes", "tiktok": "utf16_units",
                       "bluesky": "graphemes", "twitter": "weighted"}
MESSAGE_MAX_CHARS: dict[str, int | None] = {
    "twitter": 280,          # free tier; URLs count as 23, media does not
    "bluesky": 300,          # graphemes (and 3,000 UTF-8 bytes)
    "facebook": 63206,
    "instagram": 2200,
    "threads": 500,
    "tiktok": 2200,          # API cap (in-app allows 4000)
    "youtube": 5000,         # description UTF-8 BYTES; title is separate below
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
YOUTUBE_DESCRIPTION_MAX_BYTES = 5000
TIKTOK_CAPTION_MAX_UTF16 = 2200

# Largest single media file, decimal MB. The publisher that fetches from a URL
# is bound by the server-side fetch ceiling; the send path is bound by the
# platform's own API ceiling.
MEDIA_MAX_MB: dict[str, int] = {
    "twitter": 512,
    "bluesky": 300,
    "facebook": 4000,        # conservative Page /videos policy, not Reels API
    "instagram": 300,
    # 1000 (1 GB) rather than 1024: this table is decimal MB (the publisher
    # compares `bytes > limit * 1_000_000`), and Meta documents Threads video
    # at 1 GB. 1024 allowed a file up to 1,024,000,000 bytes, which is over the
    # documented cap - the operator confirmed 1000.
    "threads": 1000,
    "tiktok": 4000,
    "youtube": 256000,
    "reddit": 1000,
    "telegram": 2000,
    "linkedin": 5000,
    "pinterest": 2000,
    "discord": 20,          # conservative decimal budget; current default is 20 MiB
}

# Maximum video duration in seconds. ``None`` = no platform cap.
VIDEO_MAX_SECONDS: dict[str, float | None] = {
    "twitter": 140.0,
    "bluesky": 600.0,
    "facebook": 14460.0,
    "instagram": 900.0,
    "threads": 300.0,
    "tiktok": 600.0,        # SAU compatibility cap; inbox upload <=600s, Direct Post uses creator allowance
    "youtube": 43200.0,
    "reddit": 900.0,
    "linkedin": 900.0,
    "pinterest": 300.0,
    "telegram": None,
}

# How many media items one post may carry.
MAX_IMAGES: dict[str, int] = {
    "twitter": 4,
    "bluesky": 4,          # app.bsky.embed.images; gallery (10) is NOT implemented
    "facebook": 10,
    "instagram": 10,
    "threads": 10,         # conservative implemented carousel route (not app max)
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
    "telegram": 1,         # this publisher implements image albums, not video albums
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


# Offline processing policy, separate from an account's runtime entitlement.
# 30 fps / H.264 / AAC / MP4 is a compatibility output, not a claim that other
# encodings are universally forbidden. Content framing is preserved by padding.
PREPARATION_POLICY_VERSION = "2026-11-05-v3"
TIKTOK_DEFAULT_PREP_SECONDS = 180.0
TELEGRAM_CLOUD_UPLOAD_BYTES = 50_000_000
TELEGRAM_URL_FETCH_BYTES = 20_000_000
TELEGRAM_USER_UPLOAD_BYTES = 2_000_000_000
# A viable backup is selected by the caller, never by credential/network lookup.
# Below BOTH the external 1,024,000,000-byte and hosted 1-GiB video ceilings.
BACKUP_MEDIA_MAX_BYTES = 1_000_000_000
BACKUP_MAX_IMAGES = {key: min(count, 10) for key, count in MAX_IMAGES.items()}
BACKUP_MAX_VIDEOS = {key: 1 for key in NETWORK_TO_PLATFORM.values()}

# Image output compatibility profiles, NOT newly verified universal maxima.
# Bounds on geometry are operational encoder/compatibility bounds unless noted.
# Unknown domestic/browser profiles deliberately have no invented preparation cap.
# JPEG/PNG are sRGB still-image outputs; animated input must be reviewed, not flattened.
IMAGE_PREPARATION_POLICIES: dict[str, dict] = {
    "bluesky": {"route": "app.bsky.embed.images", "max_bytes": 2_000_000,
                "target_bytes": 1_900_000, "formats": ("JPEG", "PNG")},
    "twitter": {"route": "native static media", "max_bytes": 5_000_000,
                "formats": ("JPEG", "PNG")},
    "facebook": {"route": "Page photos compatibility", "max_bytes": 4_000_000,
                 "formats": ("JPEG", "PNG")},
    "instagram": {"route": "Graph feed/carousel (qualified compatibility)",
                  "max_bytes": 8_000_000, "formats": ("JPEG",),
                  "max_width": 1080, "max_height": 1350, "min_width": 320,
                  "min_aspect_ratio": 4 / 5, "max_aspect_ratio": 1.91},
    "threads": {"route": "Graph carousel (qualified compatibility)",
                "max_bytes": 8_000_000, "formats": ("JPEG", "PNG"),
                "min_aspect_ratio": 0.1, "max_aspect_ratio": 10.0},
    "tiktok": {"route": "Direct Post photo compatibility", "max_bytes": 20_000_000,
               "formats": ("JPEG",)},
    "reddit": {"route": "native gallery compatibility", "max_bytes": 20_000_000,
               "formats": ("JPEG", "PNG")},
    "telegram": {"route": "Bot API sendPhoto / media group", "max_bytes": 10_000_000,
                 "formats": ("JPEG", "PNG"), "max_dimension_sum": 10_000,
                 "min_aspect_ratio": 1 / 20, "max_aspect_ratio": 20.0},
    "linkedin": {"route": "native image compatibility", "max_bytes": 10_000_000,
                 "formats": ("JPEG", "PNG")},
    "pinterest": {"route": "single Pin compatibility", "max_bytes": 20_000_000,
                  "formats": ("JPEG", "PNG")},
    "discord": {"route": "multipart attachment compatibility", "max_bytes": 20_000_000,
                "formats": ("JPEG", "PNG")},
    "youtube": {"route": "thumbnails.set ONLY (not a standalone image post)",
                "cover_only": True, "max_bytes": 2_000_000, "formats": ("JPEG", "PNG"),
                "max_width": 1280, "max_height": 720},
    **{key: {"route": "blog image compatibility (no provider maximum claimed)",
             "max_bytes": None, "formats": ("JPEG", "PNG")}
       for key in ("nw_sw_blog", "teaching_blog", "patreon", "medium", "substack")},
}


def image_preparation_limits(platform: str, *, account_config: dict | None = None,
                             backup_compatible: bool = False) -> dict:
    """Offline still-image policy; no network reads or inferred backup entitlement."""
    key = str(platform or "").strip().lower()
    if key not in IMAGE_PREPARATION_POLICIES:
        raise ValueError(f"No verified image preparation policy for platform: {key}")
    if account_config is not None and not isinstance(account_config, dict):
        raise ValueError("account_config must be an object")
    if type(backup_compatible) is not bool:
        raise ValueError("backup_compatible must be a boolean")
    policy = IMAGE_PREPARATION_POLICIES[key]
    result = {"max_bytes": policy["max_bytes"], "target_bytes": policy.get("target_bytes"),
              "max_width": policy.get("max_width", 4096),
              "max_height": policy.get("max_height", 4096),
              "min_width": policy.get("min_width", 32), "min_height": 32,
              "min_aspect_ratio": policy.get("min_aspect_ratio"),
              "max_aspect_ratio": policy.get("max_aspect_ratio"),
              "max_dimension_sum": policy.get("max_dimension_sum"),
              "formats": policy["formats"], "max_items": 1 if policy.get("cover_only") else MAX_IMAGES.get(key, 10)}
    if backup_compatible:
        if key not in BACKUP_MAX_IMAGES:
            raise ValueError(f"No supported backup image contract for {key}")
        result["max_bytes"] = min(result["max_bytes"] or BACKUP_MEDIA_MAX_BYTES, BACKUP_MEDIA_MAX_BYTES)
        result["max_items"] = min(result["max_items"], BACKUP_MAX_IMAGES[key])
    return result


def preparation_limits(platform: str, *, account_config: dict | None = None,
                       backup_compatible: bool = False) -> dict:
    """Effective offline ceilings; callers must still check runtime permissions.

    No network/config-file reads. A caller may supply freshly queried TikTok
    creator_info; absent that, use the documented baseline, never the example
    response's privilege. Account settings may tighten, never relax API caps.
    Telegram defaults to cloud multipart; an explicitly configured MTProto user
    route keeps its own ceiling. Local Bot API is not enabled by this function.
    """
    import math

    key = str(platform or "").strip().lower()
    generic_routes = {"nw_sw_blog", "teaching_blog", "patreon", "medium", "substack"}
    if key not in MEDIA_MAX_MB and key not in generic_routes:
        raise ValueError(f"No verified video preparation policy for platform: {key}")
    if type(backup_compatible) is not bool:
        raise ValueError("backup_compatible must be a boolean")
    cfg = {} if account_config is None else account_config
    if not isinstance(cfg, dict):
        raise ValueError("account_config must be an object")
    seconds = VIDEO_MAX_SECONDS.get(key)
    size = MEDIA_MAX_MB[key] * 1_000_000 if key in MEDIA_MAX_MB else None
    if key == "tiktok":
        creator = cfg.get("creator_info", cfg.get("creatorInfo", {}))
        if creator is None:
            creator = {}
        if not isinstance(creator, dict):
            raise ValueError("TikTok creator_info must be an object")
        # Only caller-supplied fresh creator_info is an allowance input. A raw
        # request setting must never raise the conservative 180-second baseline.
        raw = creator.get("max_video_post_duration_sec")
        if raw is None:
            seconds = TIKTOK_DEFAULT_PREP_SECONDS
        else:
            if isinstance(raw, bool):
                raise ValueError("Invalid TikTok creator duration allowance")
            try:
                allowance = float(raw)
            except (TypeError, ValueError):
                raise ValueError("Invalid TikTok creator duration allowance") from None
            if not math.isfinite(allowance) or allowance <= 0:
                raise ValueError("Invalid TikTok creator duration allowance")
            seconds = min(float(VIDEO_MAX_SECONDS[key]), allowance)
    elif key == "youtube":
        status = cfg.get("channelStatus", {})
        if status is None:
            status = {}
        if not isinstance(status, dict):
            raise ValueError("YouTube channelStatus must be an object")
        long_uploads = status.get("longUploadsStatus") == "allowed"
        seconds = VIDEO_MAX_SECONDS[key] if long_uploads else 900.0
    elif key == "telegram":
        user_route = (cfg.get("mtproto") is True
                      or str(cfg.get("authMode") or "").strip().lower() == "user"
                      or bool(cfg.get("apiId") and cfg.get("apiHash")))
        # Unresolved env-name references alone are NOT proof of a user route.
        # A trusted caller may explicitly pass authMode=user / mtproto=True.
        size = TELEGRAM_USER_UPLOAD_BYTES if user_route else TELEGRAM_CLOUD_UPLOAD_BYTES
    max_items = MAX_VIDEOS.get(key, 1)
    if backup_compatible:
        if key not in BACKUP_MAX_VIDEOS:
            raise ValueError(f"No supported backup video contract for {key}")
        size = min(size or BACKUP_MEDIA_MAX_BYTES, BACKUP_MEDIA_MAX_BYTES)
        max_items = min(max_items, BACKUP_MAX_VIDEOS[key])
    return {
        "max_seconds": seconds,
        "max_bytes": int(size) if size is not None else None,
        "max_items": max_items,
        "min_seconds": {"instagram": 3.0, "twitter": 0.5, "pinterest": 4.0,
                        "linkedin": 3.0, "tiktok": 3.0}.get(key, 0.1),
        "max_width": 1280 if key == "twitter" else 1920,
        "max_height": 1024 if key == "twitter" else 1920,
        "min_dimension": 360 if key == "tiktok" else 32,
        "min_fps": 23.0 if key in {"tiktok", "instagram", "threads"} else 1.0,
        "max_fps": 30.0,
        "max_video_bitrate": 25_000_000 if key in {"instagram", "threads"} else 20_000_000,
        "min_aspect_ratio": 1 / 3 if key == "twitter" else None,
        "max_aspect_ratio": 3.0 if key == "twitter" else None,
    }


def preparation_account_config(platform: str, configs: list[dict]) -> dict:
    """Merge ONLY caller-supplied fresh allowance/transport facts; no secrets.

    This is not authorization and does not establish freshness. The caller must
    supply fresh creator_info/channelStatus; arbitrary duration/long-upload flags
    are intentionally ignored. Missing selected-account facts use the baseline.
    """
    policies = [preparation_limits(platform, account_config=config) for config in configs or [{}]]
    if platform == "tiktok":
        return {"creator_info": {"max_video_post_duration_sec": min(value["max_seconds"] for value in policies)}}
    if platform == "youtube":
        return {"channelStatus": {"longUploadsStatus": "allowed" if all(
            value["max_seconds"] == VIDEO_MAX_SECONDS["youtube"] for value in policies) else "disallowed"}}
    if platform == "telegram":
        return {"authMode": "user" if all(value["max_bytes"] == TELEGRAM_USER_UPLOAD_BYTES for value in policies) else "bot"}
    return {}


def backup_compatible_platforms(request_data: dict) -> frozenset[str]:
    """Trusted caller selection only; does not inspect mappings or credentials."""
    supplied = request_data.get("_backupCompatiblePlatforms") or []
    if isinstance(supplied, dict):
        supplied = [key for key, enabled in supplied.items() if enabled is True]
    if not isinstance(supplied, (list, tuple, set, frozenset)) or any(not isinstance(key, str) for key in supplied):
        raise ValueError("Invalid backup-compatible platform selection")
    return frozenset(key.strip().lower() for key in supplied)


# Essential evidence for the corrected fields; tracked research/docs are kept by
# the route owner. Other image bounds above remain qualified compatibility policy.
LIMIT_SOURCES = {
    "youtube_text": "https://developers.google.com/youtube/v3/docs/videos#snippet",
    "youtube_allowance": "https://developers.google.com/youtube/v3/docs/channels#status.longUploadsStatus",
    "tiktok_caption": "https://developers.tiktok.com/doc/content-posting-api-reference-direct-post",
    "tiktok_allowance": "https://developers.tiktok.com/doc/content-posting-api-reference-query-creator-info",
    "bluesky_text": "https://github.com/bluesky-social/atproto/blob/main/lexicons/app/bsky/feed/post.json",
    "bluesky_images": "https://github.com/bluesky-social/atproto/blob/main/lexicons/app/bsky/embed/images.json",
    "bluesky_video": "https://github.com/bluesky-social/atproto/blob/main/lexicons/app/bsky/embed/video.json",
}
UNVERIFIED_BROWSER_ROUTES = frozenset({"douyin", "kuaishou", "xiaohongshu", "tencent", "bilibili", "baijiahao"})
