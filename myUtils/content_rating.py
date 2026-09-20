"""Content rating: which accounts a piece of media may be published to.

The rule the operator set, verbatim: a file is SFW only when its name starts
or ends with ``sfw`` (any case). **Everything else is NSFW.** That default is
the whole point — an unlabelled file must never reach a platform that bans
nudity. The earlier inbox guard treated "unlabelled" as SFW, which is the wrong
way round for a library where most material is explicit.

``restrict_accounts`` is the single choke point. Every publish entry — the
web UI's ``/publish-center/submit``, the inbox one-click route, and the MCP
publish tool — funnels through ``publish_orchestrator.submit_publish``, which
calls this after resolving the profile's accounts. There is deliberately no
way to opt out from a request payload: a caller that wants nudity on Instagram
has to change this module, not pass a flag.
"""
from __future__ import annotations

import re
from pathlib import PurePath
from typing import Iterable

RATING_SFW = "sfw"
RATING_NSFW = "nsfw"

# Platforms whose terms prohibit nudity / sexual content. Media rated NSFW is
# never sent to an account on any of these, regardless of what the caller
# selected. Keep this the authoritative list; sau_backend re-exports it.
NSFW_RESTRICTED_PLATFORMS: frozenset[str] = frozenset(
    {"instagram", "facebook", "threads", "youtube", "tiktok"}
)

# The token must sit at an edge of the stem, separated from the rest by a
# space, underscore, dash or dot — or be the whole stem. ``\b`` alone is not
# enough because ``_`` counts as a word character, so ``sfw_morning`` would
# fail the edge check while ``SFW morning`` passed.
_SFW_EDGE = re.compile(r"^sfw(?:[\s_\-.]|$)|(?:^|[\s_\-.])sfw$", re.IGNORECASE)


def rating_for_filename(name: str) -> str:
    """``sfw`` when the stem starts or ends with the token ``sfw``; else ``nsfw``.

    Only the basename's stem is inspected, so a parent directory called
    ``sfw_batch`` does not launder its contents, and ``_part1`` / ``_pub``
    suffixes are stripped first so ``SFW clip_part2_pub.mp4`` still matches
    the edge rule the operator stated.
    """
    stem = PurePath(str(name)).stem.strip()
    # Strip the pipeline's own suffixes so they don't hide the trailing token.
    stem = re.sub(r"(?:_part\d+|_pub|_sfw_pub|_nsfw_pub)+$", "", stem, flags=re.IGNORECASE)
    return RATING_SFW if _SFW_EDGE.search(stem) else RATING_NSFW


def rating_for_media(media_file_paths: Iterable[str], *, explicit: str | None = None) -> str:
    """Rating for a whole publish.

    ``explicit`` is a caller-supplied ``sfwFlag`` (the inbox carries one). It
    may only make the result *stricter*: passing ``"sfw"`` for a file whose
    name says otherwise is ignored, because the filename is the rule the
    operator chose and a flag in a JSON body is the easiest thing to get
    wrong. A publish with any NSFW file is NSFW as a whole — one explicit clip
    in a batch bound for Instagram must block the batch.
    """
    ratings = {rating_for_filename(p) for p in media_file_paths}
    if RATING_NSFW in ratings or not ratings:
        return RATING_NSFW
    if str(explicit or "").strip().lower() == RATING_NSFW:
        return RATING_NSFW
    return RATING_SFW


def restrict_accounts(accounts, rating: str):
    """Drop accounts on nudity-banning platforms when ``rating`` is NSFW.

    Returns the filtered list. Raises ``ValueError`` when filtering leaves
    nothing — callers must not interpret an empty result as "publish to all",
    which is exactly the bug this exists to prevent.
    """
    if rating != RATING_NSFW:
        return list(accounts)
    kept = [
        a for a in accounts
        if str(getattr(a, "platform", "")).strip().lower() not in NSFW_RESTRICTED_PLATFORMS
    ]
    if accounts and not kept:
        raise ValueError(
            "NSFW media has no adult-safe account on this profile — refusing to "
            "publish (an empty selection would otherwise mean every account)"
        )
    return kept
