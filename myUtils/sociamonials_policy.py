"""Conservative Sociamonials transport policy; never edits approved copy.

Source: the public reference and linked OpenAPI at
https://www.sociamonials.com/developers.php (read-only verification 2026-11-05).
Hosted and external video ceilings are intentionally DIFFERENT byte budgets.
The native policy still applies; these restrictions can only tighten it.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from myUtils import platform_limits, text_limits

POLICY_VERSION = "sociamonials-safe-20261105"
HOSTED_VIDEO_MAX_BYTES = 1_073_741_824
EXTERNAL_VIDEO_MAX_BYTES = 1_024_000_000
IMAGE_MAX_BYTES = 15_728_640
IMAGE_MAX_PIXELS = 100_000_000
IMAGE_MAX_LONG_SIDE = 20_000

NETWORK_TO_PLATFORM = {
    "fb": "facebook", "tw": "twitter", "ln": "linkedin", "in": "instagram",
    "pi": "pinterest", "gmb": "google_business", "thrd": "threads",
    "tiktok": "tiktok", "yt": "youtube", "blsk": "bluesky",
}
NETWORK_MAX_MESSAGE_CHARS = {
    "fb": 63_206, "tw": 280, "ln": 3_000, "in": 2_200, "pi": 500,
    "gmb": 1_500, "thrd": 500, "tiktok": 150, "blsk": 300,
}
NETWORK_MESSAGE_UNITS = {
    "tw": "weighted", "tiktok": "utf16_units", "blsk": "graphemes", "yt": "utf8_bytes",
}
# The API accepts at most 20, but some networks silently drop extras. Never do
# that here. Google Business/Pinterest stay single-image conservatively.
NETWORK_MAX_IMAGES = {
    "fb": 20, "tw": 4, "ln": 20, "in": 10, "pi": 1, "gmb": 1,
    "thrd": 10, "tiktok": 20, "yt": 0, "blsk": 4,
}
FIRST_COMMENT_NETWORKS = frozenset({"fb", "in", "ln", "blsk", "thrd"})
THUMBNAIL_NETWORKS = frozenset({"fb", "in", "pi", "ln", "gmb"})


class SociamonialsPolicyError(ValueError):
    retryable = False

    def __init__(self, message: str, *, field: str = "intent", **details: Any):
        super().__init__(message)
        self.details = {"intentReviewRequired": True, "field": field, "retrySafe": True, **details}


def _fail(field: str, reason: str = "is not supported by the backup") -> None:
    # Field names, not field values: provider/customer text must not enter logs.
    raise SociamonialsPolicyError(f"Original {field} {reason}; review the original intent", field=field)


def validate_text(text: str, network: str, *, field: str = "message") -> str:
    """Return the EXACT string, enforcing both native and backup field units."""
    if not isinstance(text, str):
        _fail(field, "must be a string")
    if text.strip().lower() == "null":
        _fail(field, "would be treated as an omitted/default field by Sociamonials")
    try:
        text_limits.utf8_length(text)
        if network == "yt":
            limit, unit = (100, "characters") if field == "title" else (5_000, "utf8_bytes")
        else:
            limit = NETWORK_MAX_MESSAGE_CHARS[network]
            unit = NETWORK_MESSAGE_UNITS.get(network, "characters")
        counters = {"utf8_bytes": text_limits.utf8_length, "utf16_units": text_limits.utf16_length,
                    "graphemes": text_limits.grapheme_length, "weighted": text_limits.weighted_length}
        count = counters[unit](text) if unit in counters else len(text)
        if count > limit:
            raise SociamonialsPolicyError(
                f"Sociamonials {network} {field} is {count} {unit}; limit is {limit}; approved copy needs review",
                field=field, textReviewRequired=True, length=count, limit=limit, unit=unit,
            )
        platform = NETWORK_TO_PLATFORM[network]
        # A native unit/byte ceiling also applies (notably Bluesky's byte cap).
        if platform in platform_limits.MESSAGE_MAX_CHARS:
            text_limits.validate_message(text, platform, field=field)
    except SociamonialsPolicyError:
        raise
    except (ValueError, KeyError) as exc:
        details = getattr(exc, "details", {})
        raise SociamonialsPolicyError("Original text fails the publishing text policy; approved copy needs review",
                                    field=field, textReviewRequired=True,
                                    **{k: v for k, v in details.items() if k in {"length", "limit", "unit"}}) from None
    return text


def limits_for(network: str, storage: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Credential-free caps. Missing storage NEVER grants hosted-video access."""
    if network not in NETWORK_TO_PLATFORM:
        _fail("network", "has no verified backup policy")
    max_upload = (storage or {}).get("max_upload_bytes") or {}
    image = max_upload.get("image") if isinstance(max_upload, Mapping) else None
    video = max_upload.get("video") if isinstance(max_upload, Mapping) else None
    image_cap = min(IMAGE_MAX_BYTES, image) if type(image) is int and image > 0 else IMAGE_MAX_BYTES
    hosted_image_cap = min(IMAGE_MAX_BYTES, image) if type(image) is int and image > 0 else None
    video_cap = min(HOSTED_VIDEO_MAX_BYTES, video) if type(video) is int and video > 0 else None
    platform = NETWORK_TO_PLATFORM[network]
    image_count = NETWORK_MAX_IMAGES[network]
    native_count = platform_limits.max_images(platform)
    if native_count is not None:
        image_count = min(image_count, native_count)
    return {
        "policy_version": POLICY_VERSION,
        "hosted_video_max_bytes": video_cap,
        "external_video_max_bytes": EXTERNAL_VIDEO_MAX_BYTES,
        "image_max_bytes": image_cap, "hosted_image_max_bytes": hosted_image_cap, "image_max_pixels": IMAGE_MAX_PIXELS,
        "image_max_long_side": IMAGE_MAX_LONG_SIDE, "max_images": image_count,
        "video_max_seconds": platform_limits.video_max_seconds(platform),
        "message_max_length": 5_000 if network == "yt" else NETWORK_MAX_MESSAGE_CHARS[network],
        "message_count_unit": NETWORK_MESSAGE_UNITS.get(network, "characters"),
        "title_max_length": 100 if network == "yt" else None,
    }


_BOOL_ALIASES = {
    "allowComment": "allow_comments", "allowComments": "allow_comments", "allow_comments": "allow_comments",
    "allowDuet": "allow_duet", "allow_duet": "allow_duet", "allowStitch": "allow_stitch", "allow_stitch": "allow_stitch",
    "aiGeneratedContent": "ai_generated_content", "ai_generated_content": "ai_generated_content",
    "isAigc": "ai_generated_content", "is_aigc": "ai_generated_content", "isAiGenerated": "ai_generated_content",
    "yourBrand": "promotes_your_brand", "promotesYourBrand": "promotes_your_brand", "promotes_your_brand": "promotes_your_brand",
    "brandedContent": "branded_content", "brandContentToggle": "branded_content", "branded_content": "branded_content",
    "brandOrganicToggle": "promotes_your_brand", "brand_organic_toggle": "promotes_your_brand",
    "brand_content_toggle": "branded_content",
    "notifySubscribers": "notify_subscribers", "notify_subscribers": "notify_subscribers",
    "madeForKids": "made_for_kids", "selfDeclaredMadeForKids": "made_for_kids", "made_for_kids": "made_for_kids",
    "containsSyntheticMedia": "synthetic_media", "syntheticMedia": "synthetic_media", "synthetic_media": "synthetic_media",
    "shorts": "shorts", "isShort": "shorts", "reel": "reel", "reelsOnly": "reels_only", "reels_only": "reels_only",
    "story": "story", "shareToFeed": "share_to_feed", "share_to_feed": "share_to_feed",
}
_INVERSE_ALIASES = {
    "disableComment": "allow_comments", "disableComments": "allow_comments", "disable_comment": "allow_comments",
    "disable_comments": "allow_comments", "disableDuet": "allow_duet", "disable_duet": "allow_duet",
    "disableStitch": "allow_stitch", "disable_stitch": "allow_stitch",
}
_VALUE_ALIASES = {
    "visibility": "visibility", "privacyLevel": "visibility", "privacy_level": "visibility", "privacyStatus": "visibility",
    "title": "title", "description": "description", "tags": "tags", "carouselTitle": "carousel_title", "carousel_title": "carousel_title",
    "postType": "post_type", "post_type": "post_type", "mediaType": "post_type",
    "publishMode": "publish_mode", "publish_mode": "publish_mode", "postMode": "publish_mode", "post_mode": "publish_mode",
    "firstComment": "first_comment", "first_comment": "first_comment", "altText": "alt_text", "alt_text": "alt_text",
    "videoThumbnailUrl": "video_thumbnail_url", "video_thumbnail_url": "video_thumbnail_url",
    "boardName": "board_name", "board_name": "board_name", "boardId": "board_id", "board_id": "board_id",
    "pinTitle": "pin_title", "pin_title": "pin_title", "message": "message",
}
_UNSUPPORTED = {
    "post_preset", "postPreset", "profile_selection", "profileSelection", "profile_refs", "profile_ref",
    "watermark", "hashtag_group", "hashtagGroup", "cta_group", "ctaGroup", "category", "categoryId",
    "publishAt", "publish_at", "schedule", "scheduledAt", "queue_position", "queuePosition", "recurring",
    "videoCoverTimestampMs", "video_cover_timestamp_ms", "coverImageUrl", "cover_image_url",
    "music", "musicId", "soundId", "autoAddMusic", "auto_add_music", "holdAsDraft", "recommendedMusic",
    "embeddable", "publicStatsViewable", "license", "defaultLanguage", "defaultAudioLanguage",
    "recordingDate", "locationDescription", "latitude", "longitude", "location", "geo", "link",
    "commentPolicy", "replyControl", "reply_control", "quoteControl", "quote_control", "contentWarning",
    "sensitive", "isSensitive", "nudity", "spoiler", "community", "linkedin_document",
    "collaborators", "collaboratorIds", "productTags", "userTags", "taggedUsers",
    "framing", "crop", "audience", "privacy", "interactionSettings", "disclosures",
}
_ALLOWED_BY_NETWORK = {
    "tiktok": {"visibility", "allow_comments", "allow_duet", "allow_stitch", "ai_generated_content",
               "promotes_your_brand", "branded_content", "carousel_title"},
    "yt": {"title", "description", "tags", "visibility", "notify_subscribers", "made_for_kids", "synthetic_media", "shorts"},
    "fb": {"reel"}, "in": {"reels_only"}, "pi": {"board_name", "board_id", "pin_title"},
}
_PRIVACY = {"PUBLIC_TO_EVERYONE": "public", "SELF_ONLY": "private", "MUTUAL_FOLLOW_FRIENDS": "friends",
            "FOLLOWER_OF_CREATOR": "followers", "PUBLIC": "public", "PRIVATE": "private",
            "FRIENDS": "friends", "FOLLOWERS": "followers", "UNLISTED": "unlisted"}


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        _fail(field, "must be a literal boolean (null/default substitution is unsafe)")
    return value


def _option_sources(payload: Mapping[str, Any], config: Mapping[str, Any], network: str):
    platform = NETWORK_TO_PLATFORM[network]
    aliases = {network, platform, "google_business_profile" if network == "gmb" else platform}
    draft = payload.get("draft") if isinstance(payload.get("draft"), Mapping) else {}
    # Account credentials/operational metadata are not post settings. Only known
    # intent fields are extracted at the root. Explicit option objects are strict.
    for priority, source in ((0, config), (2, draft), (2, payload)):
        yield priority, source, False
        for name in aliases:
            raw = source.get(name)
            if raw is not None:
                if not isinstance(raw, Mapping):
                    _fail("networkOptions", "must be an object")
                yield priority + 1, raw, True
        for key in ("networkOptions", "network_options"):
            raw = source.get(key)
            if raw is None:
                continue
            if not isinstance(raw, Mapping):
                _fail(key, "must be an object")
            network_keys = set(NETWORK_TO_PLATFORM) | set(NETWORK_TO_PLATFORM.values())
            if set(raw) & network_keys:
                unknown = set(raw) - network_keys
                if unknown:
                    _fail("networkOptions", "contains unknown network selection/options")
                for name in aliases:
                    if name in raw:
                        if not isinstance(raw[name], Mapping):
                            _fail("networkOptions", "must contain option objects")
                        yield priority + 1, raw[name], True
            else:
                yield priority + 1, raw, True
    if network == "tiktok":
        raw = payload.get("tiktokPostSettings")
        if raw is not None:
            if not isinstance(raw, Mapping):
                _fail("tiktokPostSettings", "must be a per-target object")
            yield 4, raw, True


def map_intent(payload: Mapping[str, Any], *, network: str, message: str,
               account_config: Mapping[str, Any] | None = None, media_kind: str | None = None,
               media_count: int = 0) -> dict[str, Any]:
    """Map original per-post/account intent, or reject unsupported intent.

    Returns message, network_options and top-level post fields. Per-post values
    override account defaults. Conflicting aliases at one precedence block.
    Unknown fields inside networkOptions/creator settings are never ignored.
    """
    config = account_config or {}
    if network not in NETWORK_TO_PLATFORM:
        _fail("network")
    values: dict[str, Any] = {}
    ranks: dict[str, int] = {}
    reviewed_title: str | None = None

    def assign(name: str, value: Any, rank: int) -> None:
        if name in values and rank == ranks[name] and values[name] != value:
            _fail(name, "has conflicting aliases")
        if rank >= ranks.get(name, -1):
            values[name], ranks[name] = value, rank

    for rank, source, strict in _option_sources(payload, config, network):
        disclosure_enabled = None
        for key, value in source.items():
            if key in _UNSUPPORTED and value not in (None, "", [], {}):
                _fail("networkOptions" if strict else key)
            if key in _BOOL_ALIASES:
                assign(_BOOL_ALIASES[key], _boolean(value, key), rank)
            elif key in _INVERSE_ALIASES:
                assign(_INVERSE_ALIASES[key], not _boolean(value, key), rank)
            elif key in _VALUE_ALIASES:
                name = _VALUE_ALIASES[key]
                if name == "message" and not strict:
                    continue  # Caller already composed the exact draft + explicit hashtags.
                # Non-YouTube draft titles are separate native text fields, not
                # a request to invent a new provider option. Reviewed TikTok
                # title, however, is the native caption override.
                if name in {"title", "description", "tags"} and network != "yt" and not strict:
                    continue
                if network == "tiktok" and key == "title" and rank == 4:
                    if not isinstance(value, str):
                        _fail("title", "must be a string")
                    reviewed_title = value
                    continue
                if name == "visibility":
                    if not isinstance(value, str) or value.upper() not in _PRIVACY:
                        _fail("visibility", "cannot be mapped exactly")
                    value = _PRIVACY[value.upper()]
                elif name in {"post_type", "publish_mode"}:
                    if not isinstance(value, str):
                        _fail(name, "must be a string")
                    value = value.lower()
                elif name == "tags":
                    if isinstance(value, str):
                        value = [part.strip() for part in value.split(",") if part.strip()]
                    if not isinstance(value, list) or any(not isinstance(tag, str) for tag in value):
                        _fail("tags", "must contain strings")
                elif not isinstance(value, str):
                    _fail(name, "must be a string (null/default substitution is unsafe)")
                if isinstance(value, str) and value.strip().lower() == "null":
                    _fail(name, "would be replaced by a provider default")
                assign(name, value, rank)
            elif key == "tiktokDirectPost":
                if network == "tiktok":
                    if not _boolean(value, key):
                        _fail("tiktokDirectPost", "requires inbox delivery, which this backup cannot preserve")
                    assign("publish_mode", "direct", rank)
            elif key == "contentDisclosure" and network == "tiktok":
                if not isinstance(value, Mapping) or set(value) - {"enabled", "yourBrand", "brandedContent"}:
                    _fail("contentDisclosure", "cannot be mapped exactly")
                enabled = _boolean(value.get("enabled"), "contentDisclosure.enabled")
                for original, canonical in (("yourBrand", "promotes_your_brand"), ("brandedContent", "branded_content")):
                    flag = _boolean(value.get(original, False), f"contentDisclosure.{original}")
                    assign(canonical, flag if enabled else False, rank)
            elif key == "contentDisclosureEnabled" and network == "tiktok":
                disclosure_enabled = _boolean(value, key)
            elif key == "consentChecked" and network == "tiktok":
                if not _boolean(value, key):
                    _fail(key, "has not been approved")
            elif strict and key not in _UNSUPPORTED:
                _fail("networkOptions", "contains an unsupported original setting")
        if disclosure_enabled is False:
            for key in ("promotes_your_brand", "branded_content"):
                # Frontend switches are conditional on the disclosure checkbox.
                values[key], ranks[key] = False, rank

    if "message" in values:
        message = values.pop("message")
    if reviewed_title:
        message = reviewed_title
    if network == "tiktok":
        publish_mode = values.pop("publish_mode", "direct")
        if publish_mode not in {"direct", "direct_post", "publish_now"}:
            _fail("publishMode", "requires TikTok inbox/draft delivery; no silent Direct Post substitution")
        defaults = {"visibility": "private", "allow_comments": True, "allow_duet": True, "allow_stitch": True,
                    "ai_generated_content": False, "promotes_your_brand": False, "branded_content": False}
        defaults.update(values)
        values = defaults
        if values["visibility"] not in {"public", "private", "friends", "followers"}:
            _fail("visibility")
        if media_kind == "video" and values.get("carousel_title"):
            _fail("carousel_title", "is ignored for video")
        if values["visibility"] == "private" and values["branded_content"]:
            _fail("branded_content", "conflicts with the original private visibility")
        creator = config.get("creatorInfo") or config.get("creator_info") or config.get("_tiktok_creator_info") or {}
        creator = creator.get("data", creator) if isinstance(creator, Mapping) else {}
        if isinstance(creator, Mapping):
            options = creator.get("privacy_level_options")
            if isinstance(options, list) and values["visibility"] not in {_PRIVACY.get(str(v).upper()) for v in options}:
                _fail("visibility", "is not allowed by the creator's current capabilities")
            for field, disabled in (("allow_comments", "comment_disabled"), ("allow_duet", "duet_disabled"),
                                    ("allow_stitch", "stitch_disabled")):
                if creator.get(disabled) is True and values[field] is True:
                    _fail(field, "conflicts with the creator's current capabilities")
    elif "publish_mode" in values:
        _fail("publishMode")

    top: dict[str, Any] = {}
    for name in ("first_comment", "alt_text", "video_thumbnail_url", "story", "post_type", "share_to_feed"):
        if name in values:
            top[name] = values.pop(name)
    first_comment = top.get("first_comment", "")
    if first_comment:
        if network not in FIRST_COMMENT_NETWORKS:
            _fail("first_comment", f"is not documented on {network}")
        validate_text(first_comment, network, field="first_comment")
    if top.get("alt_text") and (media_kind != "image" or network in {"in", "gmb", "yt", "tiktok"}):
        _fail("alt_text", "would be ignored on this destination/media route")
    if top.get("video_thumbnail_url") and (media_kind != "video" or network not in THUMBNAIL_NETWORKS):
        _fail("video_thumbnail_url", "would be ignored on this destination/media route")

    post_type = top.pop("post_type", None)
    share_to_feed = top.pop("share_to_feed", None)
    if network in {"fb", "in"}:
        story = top.get("story", False)
        if post_type == "story":
            story = True
        elif post_type is not None:
            if post_type not in {"post", "feed", "grid", "video", "reel", "reels", "reels_only", "carousel", "image"}:
                _fail("postType")
            if story:
                _fail("postType", "conflicts with story")
            if post_type in {"reel", "reels", "reels_only", "video"} and media_kind != "video":
                _fail("postType", "requires video")
            if post_type in {"carousel", "image"} and media_kind != "image":
                _fail("postType", "requires images")
            if network == "fb" and post_type in {"reel", "reels"}:
                if values.get("reel") is False:
                    _fail("postType", "conflicts with reel")
                values["reel"] = True
            if network == "fb" and (post_type == "reels_only" or share_to_feed is not None):
                _fail("postType", "contains Instagram-only format intent")
            if network == "fb" and post_type in {"post", "feed", "grid", "video"} and values.get("reel") is True:
                _fail("postType", "conflicts with the original regular Facebook video")
            if network == "in" and post_type in {"post", "feed", "grid"} and media_kind == "video":
                if share_to_feed is False or values.get("reels_only") is True:
                    _fail("postType", "conflicts with the original Instagram feed/grid intent")
                values["reels_only"] = False
            if network == "in" and post_type == "video":
                _fail("postType", "requires a regular Instagram video; the backup always makes a Reel")
            if network == "in" and post_type == "reels_only":
                values["reels_only"] = True
        if story and (media_count != 1 or first_comment):
            _fail("story", "requires exactly one media item and no unsupported first comment")
        top["story"] = story
        if media_kind == "video" and not story:
            if network == "fb":
                values.setdefault("reel", False)
            else:
                if share_to_feed is not None:
                    if "reels_only" in values and values["reels_only"] != (not share_to_feed):
                        _fail("shareToFeed", "conflicts with reelsOnly")
                    values["reels_only"] = not share_to_feed
                values.setdefault("reels_only", False)
        elif values.get("reel") or values.get("reels_only"):
            _fail("postType", "has a video-format switch on incompatible media")
    elif post_type is not None or share_to_feed is not None or "story" in top:
        _fail("postType")

    if network == "yt":
        defaults = {"visibility": "private", "notify_subscribers": True, "made_for_kids": False,
                    "synthetic_media": False, "shorts": False}
        defaults.update(values)
        values = defaults
        if values["visibility"] not in {"public", "private", "unlisted"}:
            _fail("visibility")
        draft = payload.get("draft") if isinstance(payload.get("draft"), Mapping) else {}
        title = values.get("title", draft.get("title"))
        if not title:
            title = next((line for line in message.splitlines() if line.strip()), "")
        if not title:
            _fail("title", "is missing; do not invent a title")
        values["title"] = validate_text(title, "yt", field="title")
        values["description"] = validate_text(values.get("description", message), "yt", field="description")
        if "tags" not in values and draft.get("hashtags"):
            values["tags"] = [str(tag).lstrip("#") for tag in draft["hashtags"]]
        if "tags" in values:
            try:
                text_limits.validate_youtube_tags(values["tags"])
            except ValueError:
                _fail("tags", "exceed the YouTube aggregate tag limit")
    if network == "pi" and not (values.get("board_name") or values.get("board_id")):
        _fail("board_id", "is required for the original Pinterest board")
    allowed = _ALLOWED_BY_NETWORK.get(network, set())
    if set(values) - allowed:
        _fail("networkOptions", "contain a setting unsupported on the selected network")
    message = validate_text(message, network)
    return {"message": message, "network_options": values, "top_fields": top}
