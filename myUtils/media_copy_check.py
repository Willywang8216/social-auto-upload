"""Does a generated post's copy actually match the media it will publish with?

The review queue is the bottleneck for the operator: every queued post is read
by hand before it goes out. Most copy matches its media and needs no human, so
what the operator wants is to be interrupted only when the copy is clearly
*wrong* for the picture. This module answers exactly that one question, and
nothing else.

Policy (deliberately narrow)
---------------------------
``contradicts`` is true only when the copy plainly describes something the media
does not show — a different subject, person or scene; a specific claim the media
contradicts; or copy that describes media of a different kind. Vague copy,
hashtags, emoji, tone, calls to action, or a text about something merely absent
from the frame are **not** contradictions: flagging those would put the operator
back in the loop for most items, which defeats the exercise.

Safety
------
Every failure path yields ``checked=False``. This is an advisory gate on a
publish path, so a missing model, an unreachable endpoint, unreadable media or
malformed JSON must never stop a post or silently mark it wrong.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from myUtils import llm_client

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You audit a social post before publication. You are shown the media (a "
    "picture, or one frame of a video) and the copy that will be posted with it. "
    "You answer one question: does the copy clearly contradict the media?\n\n"
    "Set contradicts=true ONLY for a clear contradiction:\n"
    "- the copy describes a different subject, person, or scene than the media shows\n"
    "- the copy makes a specific claim (a number, place, event, product) the media contradicts\n"
    "- the copy describes media of a different kind (e.g. narrates a video when the media is an unrelated still)\n\n"
    "Set contradicts=false for everything else. Tone, style, hashtags, emoji, "
    "calls to action, vague or generic wording, and copy that merely omits what "
    "the media shows are NOT contradictions. If you cannot see the media, or you "
    "are unsure, answer false.\n\n"
    'Reply with JSON only: {"contradicts": true|false, "reason": "short explanation"}'
)

UNCHECKED = {"checked": False, "contradicts": False, "reason": ""}

# A vision call on every generated post costs money and sits in the publish
# path, so it gets an off switch. Anything other than an explicit falsy value
# keeps it on.
DISABLE_ENV = "SAU_COPY_MEDIA_CHECK"


def is_enabled() -> bool:
    return str(os.environ.get(DISABLE_ENV, "") or "").strip().lower() not in {"0", "false", "no", "off"}


def _copy_text(copy) -> str:
    """Pull the human-readable text out of a parsed copy dict or a plain string."""
    if isinstance(copy, str):
        return copy.strip()
    if not isinstance(copy, dict):
        return ""
    for key in ("message", "title", "description"):
        value = str(copy.get(key) or "").strip()
        if value:
            return value
    return ""


def check_copy_against_media(
    *,
    media: str | Path | None,
    copy,
    platform: str = "",
    session=None,
    model: str | None = None,
) -> dict:
    """Compare one post's copy against its media.

    Returns ``{"checked": bool, "contradicts": bool, "reason": str}``. Never
    raises: an unusable media reference, an unreachable model or unparseable
    output all return ``checked=False`` so the caller publishes as before.
    """
    text = _copy_text(copy)
    if not text or not media or not is_enabled():
        return dict(UNCHECKED)

    user_prompt = (
        f"Platform: {platform or 'unknown'}\n"
        f"Copy that will be posted:\n{text[:2000]}\n\n"
        "Does this copy clearly contradict the media?"
    )
    kwargs = {"images": [media], "response_json": True, "temperature": 0.0}
    if model:
        kwargs["model"] = model
    try:
        result = llm_client.generate_chat_completion(
            SYSTEM_PROMPT, user_prompt, session=session, **kwargs
        )
        payload = result.parsed_json
        if not isinstance(payload, dict):
            payload = json.loads(result.content or "{}")
    except Exception:  # noqa: BLE001 — advisory only; never break a publish
        logger.warning("media_copy_check: check failed", exc_info=True)
        return dict(UNCHECKED)

    if not isinstance(payload, dict):
        return dict(UNCHECKED)
    return {
        "checked": True,
        "contradicts": bool(payload.get("contradicts")),
        "reason": str(payload.get("reason") or "").strip()[:300],
    }


def review_note(result: dict) -> str:
    """The validation error to record when the copy contradicts the media."""
    reason = str((result or {}).get("reason") or "").strip()
    return f"文案與媒體內容可能不符：{reason}" if reason else "文案與媒體內容可能不符"
