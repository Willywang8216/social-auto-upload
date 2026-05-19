"""Platform capability flags consulted by the Publish Center.

This module is the single source of truth for two cross-cutting questions
about a target platform:

* Does the platform accept multiple media items in a single post?
* Does the platform natively support a "link in the first comment" pattern
  that we should populate from ``draft["firstComment"]``?

Worker / orchestrator code consults these helpers so we can split single-
media-only platforms into staggered N-posts-5-minutes-apart fan-outs and
so the link-in-first-comment toggle has an effect only on platforms that
can honour it.
"""

from __future__ import annotations


SINGLE_MEDIA_PLATFORMS: frozenset[str] = frozenset(
    {
        "tencent",
        "tiktok",
        "youtube",
        "telegram",
        "discord",
    }
)

SUPPORTS_FIRST_COMMENT: frozenset[str] = frozenset(
    {
        "facebook",
        "instagram",
    }
)


def platform_supports_multi_media(platform: str) -> bool:
    return (platform or "").lower() not in SINGLE_MEDIA_PLATFORMS


def platform_supports_first_comment(platform: str) -> bool:
    return (platform or "").lower() in SUPPORTS_FIRST_COMMENT
