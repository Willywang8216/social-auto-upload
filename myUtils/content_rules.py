"""Platform-specific content constraints and Google Sheets row mapping."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime


DEFAULT_EMOJI = "✨"
DEFAULT_HASHTAG_FILLERS = ["#socialmedia", "#content", "#campaign"]

SHEET_COLUMN_ORDER = [
    "Message",
    "Link",
    "ImageURL",
    "VideoURL",
    "Month(1-12)",
    "Day(1-31)",
    "Year",
    "Hour",
    "Minute(0-59)",
    "PinTitle",
    "Category",
    "Watermark",
    "HashtagGroup",
    "VideoThumbnailURL",
    "CTAGroup",
    "FirstComment",
    "Story(YorN)",
    "PinterestBoard",
    "AltText",
    "PostPreset",
]


@dataclass(frozen=True, slots=True)
class PlatformRule:
    platform: str
    max_chars: int | None = None
    hashtag_count: int = 0
    require_emoji: bool = False
    require_contact_details: bool = False
    require_cta: bool = False
    long_form: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


PLATFORM_RULES: dict[str, PlatformRule] = {
    "twitter": PlatformRule("twitter", max_chars=280, hashtag_count=3, require_emoji=True),
    "threads": PlatformRule("threads", require_contact_details=True, require_cta=True),
    "patreon": PlatformRule("patreon", long_form=True),
    "instagram": PlatformRule("instagram", max_chars=2200, long_form=True),
    "facebook": PlatformRule("facebook", max_chars=63206, long_form=True),
    "telegram": PlatformRule("telegram"),
    "youtube": PlatformRule("youtube"),
    "tiktok": PlatformRule("tiktok", max_chars=150),
    "reddit": PlatformRule("reddit"),
    "discord": PlatformRule("discord"),
}


def get_platform_rule(platform: str) -> PlatformRule:
    try:
        return PLATFORM_RULES[platform]
    except KeyError as exc:
        raise ValueError(f"Unsupported platform rule: {platform!r}") from exc


def trim_to_max_length(message: str, max_chars: int | None) -> str:
    text = (message or "").strip()
    if max_chars is None or len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return text[:max_chars]
    return text[: max_chars - 1].rstrip() + "…"


def normalize_hashtags(
    hashtags: list[str] | tuple[str, ...] | str | None,
    *,
    expected_count: int = 0,
) -> list[str]:
    if hashtags is None:
        values: list[str] = []
    elif isinstance(hashtags, str):
        values = [item.strip() for item in hashtags.replace(",", " ").split()]
    else:
        values = [str(item).strip() for item in hashtags]

    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        cleaned = item.lstrip("#").strip()
        if not cleaned:
            continue
        hashtag = f"#{cleaned.replace(' ', '')}"
        lowered = hashtag.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(hashtag)

    if expected_count > 0:
        filler_index = 0
        while len(normalized) < expected_count and filler_index < len(DEFAULT_HASHTAG_FILLERS):
            candidate = DEFAULT_HASHTAG_FILLERS[filler_index]
            filler_index += 1
            if candidate.lower() in seen:
                continue
            seen.add(candidate.lower())
            normalized.append(candidate)
        normalized = normalized[:expected_count]
    return normalized


def ensure_emoji_prefix(message: str, emoji: str = DEFAULT_EMOJI) -> str:
    stripped = (message or "").strip()
    if not stripped:
        return emoji
    if any(ord(char) > 10000 for char in stripped[:2]):
        return stripped
    return f"{emoji} {stripped}"


def prepare_platform_draft(
    platform: str,
    draft: dict,
    *,
    contact_details: str | None = None,
    cta: str | None = None,
    default_hashtags: list[str] | None = None,
) -> dict:
    rule = get_platform_rule(platform)
    prepared = dict(draft)
    message = str(prepared.get("message", "") or "").strip()
    hashtags = normalize_hashtags(
        prepared.get("hashtags") or default_hashtags,
        expected_count=rule.hashtag_count,
    )

    if rule.require_emoji:
        message = ensure_emoji_prefix(message)
    if hashtags and not all(tag in message for tag in hashtags):
        message = f"{message} {' '.join(hashtags)}".strip()

    prepared_contact_details = (
        str(prepared.get("contactDetails") or contact_details or "").strip()
    )
    prepared_cta = str(prepared.get("cta") or cta or "").strip()

    if rule.require_contact_details and not prepared_contact_details:
        raise ValueError(f"{platform} draft requires contact details")
    if rule.require_cta and not prepared_cta:
        raise ValueError(f"{platform} draft requires a CTA")

    if rule.require_contact_details and prepared_contact_details and prepared_contact_details not in message:
        message = f"{message}\n\n{prepared_contact_details}".strip()
    if rule.require_cta and prepared_cta and prepared_cta not in message:
        message = f"{message}\n\n{prepared_cta}".strip()

    prepared["hashtags"] = hashtags
    prepared["contactDetails"] = prepared_contact_details
    prepared["cta"] = prepared_cta
    prepared["message"] = trim_to_max_length(message, rule.max_chars)
    prepared["charCount"] = len(prepared["message"])
    return prepared


def _schedule_parts(schedule: dict | datetime | None) -> tuple[str, str, str, str, str]:
    if not schedule:
        return ("", "", "", "", "")
    if isinstance(schedule, datetime):
        return (
            str(schedule.month),
            str(schedule.day),
            str(schedule.year),
            str(schedule.hour),
            str(schedule.minute),
        )

    month = str(schedule.get("month", "") or "")
    day = str(schedule.get("day", "") or "")
    year = str(schedule.get("year", "") or "")
    hour = str(schedule.get("hour", "") or "")
    minute = str(schedule.get("minute", "") or "")
    return month, day, year, hour, minute


def build_sheet_row(
    *,
    message: str,
    link: str = "",
    image_urls: list[str] | None = None,
    video_url: str = "",
    schedule: dict | datetime | None = None,
    pin_title: str = "",
    category: str = "",
    watermark: str = "",
    hashtag_group: str = "",
    video_thumbnail_url: str = "",
    cta_group: str = "",
    first_comment: str = "",
    story: bool = False,
    pinterest_board: str = "",
    alt_text: str = "",
    post_preset: str = "",
) -> dict[str, str]:
    month, day, year, hour, minute = _schedule_parts(schedule)
    image_url_value = ",".join(image_urls or [])
    if image_url_value and video_url:
        raise ValueError("ImageURL and VideoURL cannot both be populated")
    return {
        "Message": message,
        "Link": link,
        "ImageURL": image_url_value,
        "VideoURL": video_url,
        "Month(1-12)": month,
        "Day(1-31)": day,
        "Year": year,
        "Hour": hour,
        "Minute(0-59)": minute,
        "PinTitle": pin_title,
        "Category": category,
        "Watermark": watermark,
        "HashtagGroup": hashtag_group,
        "VideoThumbnailURL": video_thumbnail_url,
        "CTAGroup": cta_group,
        "FirstComment": first_comment,
        "Story(YorN)": "Y" if story else "",
        "PinterestBoard": pinterest_board,
        "AltText": alt_text,
        "PostPreset": post_preset,
    }


def sheet_rows_to_values(rows: list[dict[str, str]]) -> list[list[str]]:
    values: list[list[str]] = []
    for row in rows:
        values.append([str(row.get(column, "") or "") for column in SHEET_COLUMN_ORDER])
    return values
