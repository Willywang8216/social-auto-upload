"""YouTube-only creator-link policy; never follow redirects or fetch destinations.

Only direct Patreon / Instagram / Facebook / Threads profile links are allowed.
Explicit profile settings constrain generation to the creator's own URLs. Adult
sites, link hubs, redirects and content/post routes are not eligible. A brand
handle such as ``NakedWill`` is not itself evidence of nude destination content.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from urllib.parse import parse_qsl, unquote, urlsplit

SETTING = "youtubeAllowedLinks"
PROVIDERS = {
    "patreon.com": "Patreon",
    "instagram.com": "Instagram",
    "facebook.com": "Facebook",
    "threads.com": "Threads",
    "threads.net": "Threads",
}
PROVIDER_ORDER = ("Patreon", "Instagram", "Facebook", "Threads")
TEXT_FIELDS = (
    "message", "description", "title", "summary", "firstComment", "first_comment",
    "contactDetails", "contact_details", "cta", "link", "altText", "alt_text",
)
_URL = re.compile(
    r"[\w.+-]+@(?:[\w-]+\.)+[a-z]{2,63}"
    r"|(?:[a-z][a-z0-9+.-]*://|//|www\.)[^\s<>\"'`]+"
    r"|(?<![\w@/])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}(?:[/?#][^\s<>\"'`]+)?",
    re.IGNORECASE,
)
_MARKDOWN_LINK = re.compile(r"\[([^\]\n]*)\]\(([^\s)]+)\)")
_UNSAFE_SEGMENT = re.compile(
    r"(?:^|[-_])(?:nude|nudes|nudity|nsfw|porn|pornography|xxx|onlyfans|justforfans|fansly)(?:$|[-_])",
    re.IGNORECASE,
)
_UNSUPPORTED_CONTACT = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?(?:website|contacts?|email|e-mail|linktree|external link|"
    r"onlyfans|justforfans|fansly|youtube|twitter(?:\s*/\s*x)?|x|bluesky|tiktok|"
    r"pinterest|linkedin|line)\s*[:：]",
    re.IGNORECASE,
)
_EMPTY_LABEL = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?(?:patreon|instagram|facebook|threads|linktree|contacts?|website|link)(?:\*\*)?\s*[:：]\s*(?:\*\*)?\s*$", re.IGNORECASE)
_HANDLE_LINE = re.compile(r"^\s*(?:instagram|facebook|threads|patreon)\s*[:：]\s*@\S+\s*$", re.IGNORECASE)


class YouTubeLinkPolicyError(ValueError):
    def __init__(self, field: str = "description"):
        super().__init__(
            f"YouTube {field} contains a link outside the creator's Patreon/Instagram/Facebook/Threads policy; review required"
        )
        self.details = {"provider": "youtube", "stage": "external_link_policy", "field": field}
        self.retryable = False


def _candidate(match: re.Match) -> str:
    return match.group(0).rstrip(".,;:!?)]}")


def _parts(value: str):
    try:
        value = value.strip()
        if re.fullmatch(r"[\w.+-]+@(?:[\w-]+\.)+[a-z]{2,63}", value, re.IGNORECASE):
            return None
        parsed = urlsplit(value if "://" in value or value.startswith("//") else "https://" + value)
        if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
            return None
        if parsed.port is not None or parsed.fragment:
            return None
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if host not in PROVIDERS:
            return None
        path = unquote(parsed.path).rstrip("/")
        segments = path.strip("/").split("/")
        if any(_UNSAFE_SEGMENT.search(segment.lstrip("@")) for segment in segments):
            return None
        if host == "facebook.com" and path == "/profile.php":
            query = parse_qsl(parsed.query, keep_blank_values=True)
            if len(query) != 1 or query[0][0] != "id" or not query[0][1].isdigit():
                return None
        else:
            if parsed.query:
                return None
            handle = r"[a-z0-9_.-]{1,100}"
            patterns = {
                "patreon.com": rf"/(?:c/)?{handle}",
                "instagram.com": rf"/{handle}",
                "facebook.com": rf"/(?:share/)?{handle}",
                "threads.com": rf"/@{handle}",
                "threads.net": rf"/@{handle}",
            }
            if not re.fullmatch(patterns[host], path, re.IGNORECASE):
                return None
            if segments[-1].lower() in {"posts", "reels", "stories", "explore", "redirect", "login", "signup", "share", "l.php"}:
                return None
        return host, path.lower(), parsed.query
    except (TypeError, ValueError):
        return None


def is_allowed_link(value: str, *, allowed_links: Sequence[str] | None = None) -> bool:
    parts = _parts(value)
    return parts is not None and (allowed_links is None or parts in {_parts(link) for link in allowed_links})


def links_in_text(text: str) -> list[str]:
    return list(dict.fromkeys(_candidate(match) for match in _URL.finditer(text or "")))


def links_for_profile(profile) -> list[str]:
    def value(name):
        return profile.get(name) if isinstance(profile, Mapping) else getattr(profile, name, None)

    settings = value("settings") or {}
    if SETTING in settings:
        candidates = settings[SETTING]
        if not isinstance(candidates, (list, tuple)) or any(not isinstance(link, str) or not is_allowed_link(link) for link in candidates):
            raise YouTubeLinkPolicyError("configured creator links")
    else:
        text = "\n".join(str(item or "") for item in (
            value("contact_details"), value("default_link"), settings.get("contactDetails"),
        ))
        candidates = [link for link in links_in_text(text) if is_allowed_link(link)]
    result = list(dict.fromkeys(link.strip() for link in candidates))
    return sorted(result, key=lambda link: PROVIDER_ORDER.index(PROVIDERS[_parts(link)[0]]))


def creator_footer(links: Sequence[str]) -> str:
    return "\n".join(f"{PROVIDERS[_parts(link)[0]]}: {link}" for link in links if is_allowed_link(link))


def generation_instruction(links: Sequence[str]) -> str:
    return (
        "YOUTUBE EXTERNAL-LINK POLICY (mandatory; overrides general brand/CTA rules): "
        "Only use the creator's direct Patreon, Instagram, Facebook and Threads links listed below. "
        "Never add Linktree/link hubs, OnlyFans, JustForFans, Fansly, nude/NSFW destinations, "
        "other websites, email addresses, redirect links or links to individual content/posts. "
        "Do not invent or infer handles/URLs. If no creator links are configured, include no links.\n"
        + (creator_footer(links) or "No creator links configured.")
    )


def sanitize_text(text: str, *, allowed_links: Sequence[str] | None = None) -> str:
    original = text or ""
    text = _MARKDOWN_LINK.sub(
        lambda match: match.group(0) if is_allowed_link(match.group(2), allowed_links=allowed_links) else match.group(1), original
    )
    def replace(match):
        candidate = _candidate(match)
        return match.group(0) if is_allowed_link(candidate, allowed_links=allowed_links) else match.group(0)[len(candidate):]
    source_lines = text.splitlines()
    text = _URL.sub(replace, text)
    lines = []
    for source_line, line in zip(source_lines, text.splitlines()):
        contact_removed = (_UNSUPPORTED_CONTACT.match(line) and (links_in_text(source_line) or "@" in source_line)
                           and not any(is_allowed_link(link, allowed_links=allowed_links) for link in links_in_text(line)))
        if not contact_removed and not (_EMPTY_LABEL.fullmatch(line) and line != source_line) and not _HANDLE_LINE.fullmatch(line):
            lines.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def validate_text(text: str, *, field: str = "description", allowed_links: Sequence[str] | None = None) -> None:
    if any(not is_allowed_link(link, allowed_links=allowed_links) for link in links_in_text(text)):
        raise YouTubeLinkPolicyError(field)


def validate_draft(draft: Mapping, *, allowed_links: Sequence[str] | None = None) -> None:
    for field in TEXT_FIELDS:
        if isinstance(draft.get(field), str):
            validate_text(draft[field], field=field, allowed_links=allowed_links)
    for field in ("tags", "hashtags"):
        values = draft.get(field) or []
        for value in [values] if isinstance(values, str) else values:
            if isinstance(value, str):
                validate_text(value, field=field, allowed_links=allowed_links)


def sanitize_draft(draft: Mapping, *, allowed_links: Sequence[str] | None = None, add_footer: bool = False) -> dict:
    result = dict(draft)
    for field in TEXT_FIELDS:
        if isinstance(result.get(field), str):
            result[field] = sanitize_text(result[field], allowed_links=allowed_links)
    for field in ("tags", "hashtags"):
        if isinstance(result.get(field), (list, tuple)):
            result[field] = [cleaned for value in result[field]
                             if (cleaned := sanitize_text(str(value), allowed_links=allowed_links))]
    if add_footer and allowed_links is not None:
        result["contactDetails"] = creator_footer(allowed_links)
        for field in ("message", "description"):
            if result.get(field):
                from myUtils import platform_limits
                # Keep one complete canonical footer, never a truncated URL.
                body = sanitize_text(result[field], allowed_links=[])
                footer = creator_footer(allowed_links)
                budget = platform_limits.YOUTUBE_DESCRIPTION_MAX_BYTES - len(footer.encode("utf-8")) - 2
                if len(body.encode("utf-8")) > budget:
                    from myUtils import text_limits
                    body = text_limits.truncate_message(body, "youtube", field="description", max_length=budget)
                result[field] = (body + "\n\n" + footer).strip() if footer else body
    if "charCount" in result:
        result["charCount"] = len(str(result.get("message") or "").encode("utf-8"))
    validate_draft(result, allowed_links=allowed_links)
    return result


def links_for_account(account, *, profile=None, db_path=None) -> list[str] | None:
    """One authority order for generation/native/backup: account, then profile."""
    config = (account.get("config") if isinstance(account, Mapping) else getattr(account, "config", None)) or {}
    if SETTING in config:
        return links_for_profile({"settings": {SETTING: config[SETTING]}})
    if profile is None:
        profile_id = account.get("profile_id") if isinstance(account, Mapping) else getattr(account, "profile_id", None)
        if profile_id is not None:
            from pathlib import Path
            from myUtils import profiles
            try:
                profile = profiles.get_profile(int(profile_id), db_path=Path(db_path) if db_path is not None else profiles.DB_PATH)
            except (LookupError, ValueError, OSError):
                return []  # No verified profile means no creator URLs, not arbitrary provider links.
    return links_for_profile(profile) if profile is not None else None


def links_for_generation(profile, request_data: Mapping) -> list[str]:
    if "_youtubeAllowedLinks" in request_data:
        return links_for_profile({"settings": {SETTING: request_data["_youtubeAllowedLinks"]}})
    return links_for_profile(profile)
