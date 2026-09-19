"""Tests for the X heavy-media blocker.

x.com serves its home timeline alongside the composer, and that timeline
autoplays multi-megabyte 4K clips from video.twimg.com. Those downloads starve
the session so the composer never becomes interactive in time (observed as
"Page.goto: Timeout 30000ms exceeded" and "TargetClosedError" mid-typing).
``_block_heavy_media`` aborts those responses.
"""
from __future__ import annotations

import asyncio
import unittest

from uploader.twitter_uploader.main import (
    BLOCKED_MEDIA_RESOURCE_TYPES,
    BLOCKED_MEDIA_URL_PATTERNS,
    _block_heavy_media,
)


class _FakeRequest:
    def __init__(self, url: str, resource_type: str) -> None:
        self.url = url
        self.resource_type = resource_type


class _FakeRoute:
    def __init__(self, url: str, resource_type: str) -> None:
        self.request = _FakeRequest(url, resource_type)
        self.aborted = False
        self.continued = False

    async def abort(self) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


class _FakeContext:
    def __init__(self, *, fail_route: bool = False) -> None:
        self.handler = None
        self.pattern = None
        self.fail_route = fail_route

    async def route(self, pattern, handler) -> None:
        if self.fail_route:
            raise RuntimeError("routing unavailable")
        self.pattern = pattern
        self.handler = handler


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class BlockHeavyMediaTests(unittest.TestCase):
    def test_registers_a_catch_all_route(self):
        ctx = _FakeContext()
        out = _run(_block_heavy_media(ctx))
        self.assertIs(out, ctx)
        self.assertEqual(ctx.pattern, "**/*")

    def test_aborts_video_host_media(self):
        ctx = _FakeContext()
        _run(_block_heavy_media(ctx))
        route = _FakeRoute("https://video.twimg.com/amplify_video/1/vid/1280x720/abc.mp4", "media")
        _run(ctx.handler(route))
        self.assertTrue(route.aborted)
        self.assertFalse(route.continued)

    def test_aborts_amplify_video_thumbnail(self):
        ctx = _FakeContext()
        _run(_block_heavy_media(ctx))
        route = _FakeRoute(
            "https://pbs.twimg.com/amplify_video_thumb/1/img/abc.jpg", "image"
        )
        _run(ctx.handler(route))
        self.assertTrue(route.aborted)

    def test_aborts_font_resource_type(self):
        ctx = _FakeContext()
        _run(_block_heavy_media(ctx))
        route = _FakeRoute("https://abs.twimg.com/fonts/x.woff2", "font")
        _run(ctx.handler(route))
        self.assertTrue(route.aborted)

    def test_allows_ordinary_api_and_document_requests(self):
        ctx = _FakeContext()
        _run(_block_heavy_media(ctx))
        for url, kind in (
            ("https://x.com/compose/post", "document"),
            ("https://x.com/i/api/graphql/abc/CreateTweet", "fetch"),
            ("https://abs.twimg.com/responsive-web/client-web/main.js", "script"),
        ):
            route = _FakeRoute(url, kind)
            _run(ctx.handler(route))
            self.assertTrue(route.continued, f"{url} should be allowed")
            self.assertFalse(route.aborted, f"{url} should not be aborted")

    def test_routing_failure_is_swallowed(self):
        ctx = _FakeContext(fail_route=True)
        out = _run(_block_heavy_media(ctx))
        self.assertIs(out, ctx)
        self.assertIsNone(ctx.handler)

    def test_blocklists_cover_the_heavy_video_host(self):
        self.assertIn("media", BLOCKED_MEDIA_RESOURCE_TYPES)
        self.assertTrue(any("video.twimg.com" in p for p in BLOCKED_MEDIA_URL_PATTERNS))


if __name__ == "__main__":
    unittest.main(verbosity=2)