"""One place that decides what language a campaign's *content* is in.

This is the module that should have existed from the start. Its absence is why
the operator reported "English videos on my Chinese channel" three times while
``scripts/audit_language_routing.py`` cheerfully printed **0 violations**.

What went wrong
---------------
Routing compares the *content* language against each account's
``audience_language``. The content language came from, in order:

1. the campaign transcript (STT),
2. otherwise the campaign **title**.

Both are unsound:

* the hosted STT gateway died on 2026-10-27, so there was no transcript — and
  the pipeline *silently* continued;
* the title is LLM-generated **in the target account's language** during draft
  generation, so it always "matches" the account it was written for. The
  comparison was therefore a tautology;
* when the title was empty (``__sfw.mp4``) the language was ``None``, and
  ``language_routing_skip_reason`` treats ``None`` as "do not filter". So the
  *absence* of information disabled routing entirely — the more broken STT
  became, the more English content reached Chinese accounts.

What this module does
---------------------
Ranks the evidence by how much it actually says about the *media*:

===================  =========================================================
``transcript``       what the video's own audio says. Authoritative.
``audio``            the language detector ran on the audio (no transcript).
``supplied``         the caller passed ``language`` explicitly.
``brief``            the operator's own description (they write in the
                     content's language when they know it).
``filename``         the uploaded name, minus uuid prefix and ``__sfw`` tag.
``title``            last resort; weakest, because it is generated per account.
===================  =========================================================

A text is only classified when it actually contains words: a filename of
``20261024095109313__nsfw.mp4`` or ``GjTNV3GXAAAxyCq__nsfw.jpg`` is language
*neutral*, not English. Calling those English is what turned a 1 882-target
sweep into mostly false positives. Neutral evidence yields ``None`` — and
``None`` means **unknown**, which the caller must treat as "ask a human", never
as "allow".
"""

from __future__ import annotations

import json
import re

#: Campaign metadata keys. Stored on ``campaigns.metadata_json`` so the language
#: survives the request that produced it and can be audited later.
CONTENT_LANGUAGE_KEY = "contentLanguage"
CONTENT_LANGUAGE_SOURCE_KEY = "contentLanguageSource"

#: Sources strong enough to route on without a human in the loop.
STRONG_SOURCES = frozenset({"transcript", "audio", "supplied"})
#: Sources that are a guess about the media rather than a reading of it.
WEAK_SOURCES = frozenset({"brief", "filename", "title"})

_CJK = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_LATIN_WORD = re.compile(r"[A-Za-z]{4,}")
_VOWEL = re.compile(r"[aeiouAEIOU]")
#: ``<uuid>_<name>`` (upload staging) and ``<name>__sfw`` / ``_SFW_20260101``.
_UUID_PREFIX = re.compile(r"^[0-9a-f]{32}_")
_TAG_SUFFIX = re.compile(r"(__|_)(sfw|nsfw)(_\d{8})?(_\d+)?$", re.IGNORECASE)
#: Words that appear in generated briefs regardless of the media's language.
_BOILERPLATE = re.compile(r"(sfw|nsfw|video|image|版|請依|文案|素材)", re.IGNORECASE)
#: Explicit language markers the operator puts in filenames (``..._zh.mp4``).
_LANGUAGE_MARKER = re.compile(
    r"(?:^|[^A-Za-z])(zh|en|cht|chs|chinese|english|中文|英文|繁中|简中)(?:[^A-Za-z]|$)",
    re.IGNORECASE,
)
_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+")


#: The watcher's auto-generated brief. It is Chinese regardless of the media
#: (``「<topic>」video（sfw 版）。請依此發想各平台適合的文案。``), so it says nothing
#: about the content's language and must not be classified at all.
_GENERATED_BRIEF = re.compile(
    r"(請依|請依此發想|無主題標籤|新素材|請依影片內容|各平台適合的文案|適合的文案)"
)


def _is_generated_brief(text: str) -> bool:
    return bool(_GENERATED_BRIEF.search(str(text or "")))


def _marker_language(name: str) -> str | None:
    """``_zh`` / ``_en`` / ``_cht`` in a filename is a deliberate declaration."""
    match = _LANGUAGE_MARKER.search(str(name or ""))
    if not match:
        return None
    token = match.group(1).lower()
    if token in {"zh", "cht", "chs", "chinese", "中文", "繁中", "简中"}:
        return "zh"
    return "en"


def normalise(value) -> str | None:
    """Coerce anything to ``"zh"``, ``"en"`` or ``None``."""
    text = str(value or "").strip().lower()
    if text.startswith("zh") or text in {"chinese", "zh-hant", "zh-hans", "cmn"}:
        return "zh"
    if text.startswith("en") or text in {"english", "eng"}:
        return "en"
    return None


def clean_filename(name: str) -> str:
    """The human part of an uploaded filename, or ``""`` when there is none."""
    text = str(name or "").strip()
    text = text.rsplit("/", 1)[-1]
    text = _UUID_PREFIX.sub("", text)
    stem = text.rsplit(".", 1)[0] if "." in text else text
    stem = _TAG_SUFFIX.sub("", stem).strip(" _-–—")
    return stem


def _looks_like_identifier(token: str) -> bool:
    """A media id rather than a word: ``GjTNV3GXAAAxyCq``, ``20260907103730``."""
    if not token:
        return True
    if any(character.isdigit() for character in token):
        return True
    if len(token) < 4:
        return True
    if not _VOWEL.search(token):
        return True
    # A run of three capitals mid-token is a hallmark of an id, not a word.
    if re.search(r"[A-Z]{3,}", token):
        return True
    if sum(c.isupper() for c in token) >= len(token) * 0.6:
        return True
    return False


def classify_text(text: str) -> str | None:
    """``"zh"`` / ``"en"`` / ``None`` — ``None`` when the text says nothing.

    ``None`` is the important return value: it is the difference between "this
    filename is Chinese" and "this filename is a timestamp". A Latin string only
    counts as English when it contains at least one plausible *word* —
    ``GjTNV3GXAAAxyCq__nsfw.jpg`` is a tweet id, not English.
    """
    value = str(text or "").strip()
    if not value:
        return None
    stripped = _BOILERPLATE.sub(" ", value)
    if _CJK.search(stripped):
        return "zh"
    words = [token for token in _TOKEN_SPLIT.split(stripped) if token]
    if any(not _looks_like_identifier(token) for token in words):
        return "en"
    return None


def detect_from_evidence(
    *, filenames=(), brief: str = "", title: str = "", has_video: bool = True
) -> tuple[str | None, str | None]:
    """Classify the media from its metadata, strongest evidence first.

    ``has_video`` matters. A *video* has a spoken language, so its filename and
    the operator's brief are reasonable proxies for it. An *image* has no spoken
    language at all — ``Forest Enigma 48.jpg`` is not "English content", and
    treating it as such would push every photo off the Chinese accounts. For an
    image only a deliberate marker (``..._zh.jpg``) counts.
    """
    names = [clean_filename(name) for name in filenames]
    names = [name for name in names if name]
    if names:
        # An explicit ``_zh`` / ``_en`` marker beats guessing from words.
        markers = {_marker_language(name) for name in names}
        markers.discard(None)
        if len(markers) == 1:
            return markers.pop(), "filename"
        if has_video:
            # A group of files counts only when they agree; mixed names are neutral.
            verdicts = {classify_text(name) for name in names}
            verdicts.discard(None)
            if len(verdicts) == 1:
                return verdicts.pop(), "filename"
    if not has_video:
        return None, None
    for value, source in ((brief, "brief"), (title, "title")):
        if source == "brief" and _is_generated_brief(value):
            continue  # derived from the filename, in Chinese, says nothing
        verdict = classify_text(value)
        if verdict:
            return verdict, source
    return None, None


def from_metadata(metadata) -> tuple[str | None, str | None]:
    """``(language, source)`` recorded on a campaign, if any."""
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata or "{}")
        except ValueError:
            return None, None
    if not isinstance(metadata, dict):
        return None, None
    return (
        normalise(metadata.get(CONTENT_LANGUAGE_KEY)),
        str(metadata.get(CONTENT_LANGUAGE_SOURCE_KEY) or "").strip() or None,
    )


def resolve(
    *,
    metadata=None,
    media_context=None,
    request_data=None,
) -> tuple[str | None, str | None]:
    """The best available ``(language, source)`` for a campaign.

    Ordered strongest-first. A *weak* source already stored on the campaign
    never overrides a *strong* one supplied in this request, and vice versa.
    """
    candidates: list[tuple[str | None, str | None]] = []
    stored_language, stored_source = from_metadata(metadata)
    if stored_language:
        candidates.append((stored_language, stored_source))
    for source in (media_context or {}, request_data or {}):
        language = normalise(source.get("language"))
        if language:
            candidates.append((language, str(source.get("languageSource") or "") or None))
    for language, source in candidates:
        if source in STRONG_SOURCES:
            return language, source
    if candidates:
        return candidates[0]
    return None, None
