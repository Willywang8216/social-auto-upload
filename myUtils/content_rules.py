"""Platform-specific content constraints and Google Sheets row mapping."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime

from myUtils import platform_limits


# The LLM is asked for a single ``message`` string, but it sometimes answers
# with the whole draft object instead — a real dict, a JSON/Python-stringified
# dict, or a "Title: ... / Summary: ... / Description: ..." blob.  Posting any
# of those verbatim puts Python repr or field labels in front of the audience,
# so every draft is normalised before the platform rule is applied.
_LABEL_LINE = re.compile(
    r"^\s*(?:影片)?(title|summary|description|body|caption|message|"
    r"標題|摘要|描述|正文|內容|内容)\s*[:：]\s*(.*)$",
    re.IGNORECASE,
)

_TITLE_LABELS = {"title", "標題"}


def _mapping_from_message(value) -> dict | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if len(text) > 1 and text[0] == "{" and text[-1] == "}":
            try:
                parsed = json.loads(text)
            except ValueError:
                try:
                    parsed = ast.literal_eval(text)
                except (ValueError, SyntaxError):
                    return None
            if isinstance(parsed, dict):
                return parsed
    return None


def normalize_draft_fields(draft: dict) -> dict:
    """Return ``draft`` with a plain-text ``message`` and a separate ``title``.

    Handles the malformed shapes the model can return — a real dict, a
    JSON/Python-stringified dict, or a "Title: ... / Description: ..." blob —
    without losing copy: only the field labels are removed.  A normal string
    draft passes through unchanged.
    """
    if not isinstance(draft, dict):
        return {"message": str(draft or "")}
    result = dict(draft)
    title = str(result.get("title") or "").strip()

    mapping = _mapping_from_message(result.get("message"))
    if mapping is not None:
        title = title or str(mapping.get("title") or "").strip()
        body = ""
        for key in ("description", "summary", "message", "body", "caption", "text"):
            candidate = mapping.get(key)
            if isinstance(candidate, str) and candidate.strip():
                body = candidate.strip()
                break
        if not body:
            body = "\n\n".join(
                str(value).strip()
                for value in mapping.values()
                if isinstance(value, str) and value.strip()
            )
        result["message"] = f"{title}\n\n{body}".strip() if (title and body) else (body or title)
        if not result.get("hashtags") and mapping.get("hashtags"):
            result["hashtags"] = mapping["hashtags"]
        if not result.get("firstComment") and mapping.get("firstComment"):
            result["firstComment"] = mapping["firstComment"]

    text = str(result.get("message") or "").strip()
    if any(_LABEL_LINE.match(line) for line in text.splitlines()):
        stripped_lines: list[str] = []
        for line in text.splitlines():
            match = _LABEL_LINE.match(line)
            if match:
                if match.group(1).lower() in _TITLE_LABELS and not title:
                    title = match.group(2).strip()
                stripped_lines.append(match.group(2).strip())
            else:
                stripped_lines.append(line)
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(stripped_lines)).strip()

    result["message"] = text
    if title:
        result["title"] = title
    return result


DEFAULT_EMOJI = "✨"
DEFAULT_HASHTAG_FILLERS = ["#socialmedia", "#content", "#campaign"]

# Copy that is clearly a machine placeholder rather than real caption text:
# the publish-center media-group name, the batch importer's generic brief, a
# bare media filename, or a screenshot label. Publishing these is what put
# "✨ publish-center-20260921-015359" on a live account.
_GENERIC_COPY_RE = re.compile(
    r"publish-center-\d{8}-\d{6}|adult,\s*honest,\s*18\+\s*only|"
    r"^(?:截圖|screenshot)\s*\d*$",
    re.IGNORECASE,
)
_MEDIA_FILENAME_RE = re.compile(
    r"^[\w\-. ()]+\.(?:mp4|mov|webm|m4v|jpg|jpeg|png|gif|webp|bmp)$",
    re.IGNORECASE,
)
# An LLM safety refusal must never be published as if it were copy.
_LLM_REFUSAL_RE = re.compile(
    r"^\s*(?:i can'?t|i cannot|i'?m unable|i am unable|i won'?t|i will not|"
    r"i'?m sorry,? but|as an ai|i must decline|cannot assist|can'?t assist|"
    r"sorry,? (?:but )?i)",
    re.IGNORECASE,
)


def is_usable_copy(text: str | None, *, min_chars: int = 1) -> bool:
    """True when ``text`` is real caption copy, not a placeholder.

    Used both by the generation fallback and by the pre-publish guard so a
    media-group name, the generic batch brief, a bare filename or a screenshot
    label can never reach a platform.
    """
    value = str(text or "").strip()
    if len(value) < min_chars:
        return False
    if _GENERIC_COPY_RE.search(value):
        return False
    if _MEDIA_FILENAME_RE.match(value):
        return False
    if _LLM_REFUSAL_RE.match(value) and len(value) < 400:
        return False
    return True


_CJK_RE = re.compile(r"[\u3400-\u9fff]")

# Characters whose Simplified form differs from Traditional. A zh-Hant caption
# must not contain any of them; this catches a model that silently answered in
# Simplified Chinese. Only unambiguous Simplified-only forms are listed (no
# shared characters such as 制/占/布/信 that are valid in Traditional too).
_SIMPLIFIED_ONLY_CHARS = frozenset(
    "们这来说时对开关门问东车马鸟鱼龙风飞书学习点热爱怀汉语词汇软视频网络质项过认识证无发见觉让边达还进远从众优义乐乡买乱争亏云亚产亩亲亿仅仪价伙会伟传伤伦伪体余侧债倾偿储儿兑兰兴养兽内冈册写军农冲决况冻净凉减凑几凤凭凯击则刚创删别剂剑剥剧劝办务动励劲劳势勋区医华协单卖卢卫却厂厅历厉压厌县參双变叙叠叶号叹吓吕吗吨听启吴呕员呜咏咙响哑唤"
)


def contains_simplified_chinese(text: str | None) -> bool:
    """True when ``text`` contains a Simplified-only character."""
    return any(ch in _SIMPLIFIED_ONLY_CHARS for ch in str(text or ""))


def language_tokens(value: str | None) -> list[str]:
    """Split an account language setting like ``"en,zh-Hant"`` into tokens."""
    return [tok.strip().lower() for tok in re.split(r"[,+\s]+", value or "") if tok.strip()]


def message_matches_language(message: str | None, language: str | None) -> bool:
    """Heuristic guard that a caption is written in the account's language.

    Chinese-family targets must contain CJK; every other target must not. A
    bilingual (en + zh) account is satisfied by either script present because
    the generator emits both; this only catches the gross mismatch the operator
    saw (Mandarin copy on an English account and vice versa).
    """
    tokens = language_tokens(language)
    if not tokens:
        return True
    wants_zh = any(tok.startswith("zh") for tok in tokens)
    has_cjk = bool(_CJK_RE.search(str(message or "")))
    if wants_zh:
        # A Traditional-Chinese target must not receive Simplified characters.
        return has_cjk and not contains_simplified_chinese(message)
    return not has_cjk

SHEET_MESSAGE_MAX_CHARS = {
    "facebook": platform_limits.message_max_chars("facebook"),
    "instagram": platform_limits.message_max_chars("instagram"),
    "twitter": platform_limits.message_max_chars("twitter"),
    "tiktok": platform_limits.message_max_chars("tiktok"),
}

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
    "twitter": PlatformRule("twitter", max_chars=platform_limits.message_max_chars("twitter"), hashtag_count=3, require_emoji=True),
    "threads": PlatformRule("threads", max_chars=platform_limits.message_max_chars("threads"), require_contact_details=True, require_cta=True),
    "patreon": PlatformRule("patreon", long_form=True),
    "instagram": PlatformRule("instagram", max_chars=platform_limits.message_max_chars("instagram"), long_form=True),
    "facebook": PlatformRule("facebook", max_chars=platform_limits.message_max_chars("facebook"), long_form=True),
    "telegram": PlatformRule("telegram", max_chars=platform_limits.message_max_chars("telegram")),
    "youtube": PlatformRule("youtube", max_chars=platform_limits.message_max_chars("youtube")),
    "tiktok": PlatformRule("tiktok", max_chars=platform_limits.message_max_chars("tiktok")),
    "reddit": PlatformRule("reddit", max_chars=platform_limits.message_max_chars("reddit")),
    "discord": PlatformRule("discord", max_chars=platform_limits.message_max_chars("discord")),
    "linkedin": PlatformRule("linkedin", max_chars=platform_limits.message_max_chars("linkedin")),
    "pinterest": PlatformRule("pinterest", max_chars=platform_limits.message_max_chars("pinterest")),
    "teaching_blog": PlatformRule("teaching_blog", long_form=True),
    "nw_sw_blog": PlatformRule("nw_sw_blog", long_form=True),
    "bluesky": PlatformRule("bluesky", max_chars=platform_limits.message_max_chars("bluesky"), hashtag_count=3),
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
    prepared = normalize_draft_fields(dict(draft))
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
    platform: str | None = None,
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
    if platform:
        message = trim_to_max_length(message, SHEET_MESSAGE_MAX_CHARS.get(platform))
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
