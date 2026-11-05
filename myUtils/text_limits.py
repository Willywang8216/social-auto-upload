"""Field-specific publishing counts. Validation never rewrites approved copy.

``message_length`` returns the primary provider unit; Bluesky additionally
requires <=3,000 UTF-8 bytes. ``truncate_message`` is ONLY for ordinary draft
creation/legacy non-recovery flows. Recovery callers use ``validate_message``.
X uses twitter-text v3 weights, NFC for counting, emoji clusters and HTTP(S)
URLs of weight 23; uncommon URL forms remain conservatively literal text.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

import regex

from myUtils import platform_limits as pl

_GRAPHEME = regex.compile(r"\X")
_EMOJI = regex.compile(r"\p{Extended_Pictographic}|\p{Regional_Indicator}|[0-9#*]\ufe0f?\u20e3")
_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


class TextLimitError(ValueError):
    retryable = False

    def __init__(self, platform: str, field: str, length: int, limit: int, unit: str):
        super().__init__(f"{platform} {field} is {length} {unit}; limit is {limit}; approved copy needs review")
        self.details = {"textReviewRequired": True, "platform": platform, "field": field,
                        "length": length, "limit": limit, "unit": unit, "retrySafe": True}


def _text(value: str | None) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Publishing text must be a string")
    try:
        value.encode("utf-8")  # refuse unpaired surrogates, never replace them
    except UnicodeError:
        raise ValueError("Publishing text contains invalid Unicode") from None
    return value


def utf8_length(text: str) -> int:
    return len(_text(text).encode("utf-8"))


def utf16_length(text: str) -> int:
    return len(_text(text).encode("utf-16-le")) // 2


def grapheme_length(text: str) -> int:
    return sum(1 for _ in _GRAPHEME.finditer(_text(text)))


def _x_literal_length(text: str) -> int:
    total = 0
    for cluster in _GRAPHEME.findall(text):
        if _EMOJI.search(cluster):
            total += 2
        else:
            total += sum(1 if (ord(char) <= 0x10FF or 0x2000 <= ord(char) <= 0x200D
                               or 0x2010 <= ord(char) <= 0x201F
                               or 0x2032 <= ord(char) <= 0x2037) else 2 for char in cluster)
    return total


def weighted_length(text: str) -> int:
    text = unicodedata.normalize("NFC", _text(text))
    total, position = 0, 0
    for match in _URL.finditer(text):
        url = match.group().rstrip(".,!?;:'\"")
        while url and url[-1] in ")]" and url.count(url[-1]) > url.count("(" if url[-1] == ")" else "["):
            url = url[:-1]
        # Count only an actual HTTP(S) URL with a host, not a bare scheme.
        if not url.split("://", 1)[1].split("/", 1)[0]:
            continue
        total += _x_literal_length(text[position:match.start()]) + 23
        position = match.start() + len(url)
    return total + _x_literal_length(text[position:])


def youtube_tags_length(tags: Iterable[str] | str) -> int:
    """YouTube's 500-character aggregate, including commas and quoted spaces."""
    values = [tags] if isinstance(tags, str) else list(tags)
    return sum(len(_text(tag)) + (2 if " " in tag else 0) for tag in values) + max(0, len(values) - 1)


def field_limit(platform: str, field: str = "message") -> tuple[int | None, str]:
    platform = str(platform or "").strip().lower()
    field = str(field or "message").strip().lower()
    if platform not in pl.MESSAGE_MAX_CHARS:
        raise ValueError(f"No verified text limit policy for platform: {platform}")
    if platform == "youtube":
        if field == "title":
            return pl.YOUTUBE_TITLE_MAX_CHARS, "characters"
        if field == "tags":
            return pl.YOUTUBE_TAGS_MAX_CHARS, "tag_characters"
        if field in {"message", "description", "caption"}:
            return pl.YOUTUBE_DESCRIPTION_MAX_BYTES, "utf8_bytes"
    if platform == "reddit" and field == "title":
        return pl.REDDIT_TITLE_MAX_CHARS, "characters"
    if platform == "telegram" and field == "caption":
        return pl.TELEGRAM_CAPTION_MAX_CHARS, "characters"
    if field not in {"message", "description", "caption", "title", "first_comment"}:
        raise ValueError(f"No verified text policy for {platform} field: {field}")
    return pl.message_max_chars(platform), pl.MESSAGE_COUNT_UNITS.get(platform, "characters")


def message_length(message: str, platform: str, *, field: str = "message") -> int:
    _, unit = field_limit(platform, field)
    if unit == "tag_characters":
        return youtube_tags_length(message)
    counter = {"utf8_bytes": utf8_length, "utf16_units": utf16_length,
               "graphemes": grapheme_length, "weighted": weighted_length}.get(unit)
    return counter(message) if counter else len(_text(message))


def validate_message(message: str, platform: str, *, field: str = "message",
                     max_length: int | None = None) -> str:
    """Return EXACT text or raise. Supplied ceilings may tighten, never relax."""
    text = _text(message)
    limit, unit = field_limit(platform, field)
    if max_length is not None:
        if type(max_length) is not int or max_length < 0:
            raise ValueError("Text ceiling must be a non-negative integer")
        limit = min(limit, max_length) if limit is not None else max_length
    length = message_length(text, platform, field=field)
    if limit is not None and length > limit:
        raise TextLimitError(platform, field, length, limit, unit)
    if str(platform).strip().lower() == "bluesky" and utf8_length(text) > pl.BLUESKY_BYTE_MAX:
        raise TextLimitError(platform, field, utf8_length(text), pl.BLUESKY_BYTE_MAX, "utf8_bytes")
    return text


def validate_youtube_tags(tags: Iterable[str]) -> list[str]:
    values = list(tags)
    length = youtube_tags_length(values)
    if length > pl.YOUTUBE_TAGS_MAX_CHARS:
        raise TextLimitError("youtube", "tags", length, pl.YOUTUBE_TAGS_MAX_CHARS, "tag_characters")
    return values


def truncate_message(message: str, platform: str, *, field: str = "message",
                     max_length: int | None = None, ellipsis: str = "…") -> str:
    """Grapheme-safe ordinary-draft truncation, measured in the right units."""
    text = _text(message)
    try:
        return validate_message(text, platform, field=field, max_length=max_length)
    except TextLimitError:
        pass
    clusters = _GRAPHEME.findall(text)
    # A bounded binary search avoids quadratic encoding for long descriptions.
    low, high = 0, len(clusters)
    try:
        validate_message(ellipsis, platform, field=field, max_length=max_length)
    except TextLimitError:
        ellipsis = ""
    while low < high:
        middle = (low + high + 1) // 2
        candidate = "".join(clusters[:middle]).rstrip() + ellipsis
        try:
            validate_message(candidate, platform, field=field, max_length=max_length)
        except TextLimitError:
            high = middle - 1
        else:
            low = middle
    return validate_message("".join(clusters[:low]).rstrip() + ellipsis, platform,
                            field=field, max_length=max_length)


def preserves_approved_text(payload: dict) -> bool:
    """Internal opt-in shared by recovery and publishers; no copy regeneration."""
    return (payload.get("_preserveApprovedText") is True
            or payload.get("strictTextLimits") is True
            or bool(payload.get("recoveryGeneration") or payload.get("recovery_generation")))
