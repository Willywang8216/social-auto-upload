"""Tests for the cookie-based Reddit uploader (Playwright browser automation)."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, call

# Ensure conf module exists for imports
if "conf" not in sys.modules:
    conf_module = types.ModuleType("conf")
    conf_module.BASE_DIR = str(Path(__file__).resolve().parent.parent)
    sys.modules["conf"] = conf_module

from uploader.reddit_uploader.main import (
    RedditCookieVideo,
    _post_reddit_cookie,
    _select_community,
    _upload_media,
    _wait_and_click,
    _wait_and_fill,
    REDDIT_SUBMIT_URL,
    REDDIT_LOGIN_URL,
    TITLE_SELECTOR,
    BODY_SELECTOR,
    COMMUNITY_SELECTOR,
    SUBMIT_BUTTON_SELECTOR,
    IMAGE_TAB_SELECTOR,
    FILE_INPUT_SELECTOR,
)


def _run_async(coro):
    """Helper to run async code in tests."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeLocator:
    """Fake Playwright locator for testing."""

    def __init__(self, visible=True, count=1):
        self._visible = visible
        self._count = count
        self.click = AsyncMock()
        self.fill = AsyncMock()
        self.press = AsyncMock()
        self.set_input_files = AsyncMock()
        self.wait_for = AsyncMock()
        self.first = self

    async def count(self):
        return self._count


class FakePage:
    """Fake Playwright page for testing."""

    def __init__(self, url="https://www.reddit.com/submit"):
        self.url = url
        self.goto = AsyncMock()
        self.click = AsyncMock()
        self.fill = AsyncMock()
        self.wait_for_selector = AsyncMock()
        self.wait_for_url = AsyncMock()
        self.wait_for_timeout = AsyncMock()
        self.locator = MagicMock(return_value=FakeLocator())
        self.keyboard = MagicMock()
        self.keyboard.press = AsyncMock()


class FakeContext:
    """Fake Playwright context for testing."""

    def __init__(self, page=None):
        self._page = page or FakePage()
        self.new_page = AsyncMock(return_value=self._page)
        self.close = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakeBrowser:
    """Fake Playwright browser for testing."""

    def __init__(self, context=None):
        self._context = context or FakeContext()
        self.new_context = AsyncMock(return_value=self._context)
        self.close = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakePlaywright:
    """Fake Playwright instance for testing."""

    def __init__(self, browser=None):
        self._browser = browser or FakeBrowser()
        self.chromium = MagicMock()
        self.chromium.launch = AsyncMock(return_value=self._browser)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class WaitAndClickTests(unittest.TestCase):
    """Tests for _wait_and_click helper."""

    @patch("uploader.reddit_uploader.main.asyncio.sleep", new_callable=AsyncMock)
    def test_waits_for_selector_then_clicks(self, mock_sleep):
        page = FakePage()
        _run_async(_wait_and_click(page, "#my-selector", timeout_ms=5000))
        page.wait_for_selector.assert_called_once_with(
            "#my-selector", state="visible", timeout=5000
        )
        page.click.assert_called_once_with("#my-selector")


class WaitAndFillTests(unittest.TestCase):
    """Tests for _wait_and_fill helper."""

    @patch("uploader.reddit_uploader.main.asyncio.sleep", new_callable=AsyncMock)
    def test_waits_for_selector_then_fills(self, mock_sleep):
        page = FakePage()
        _run_async(_wait_and_fill(page, "#input", "hello world", timeout_ms=8000))
        page.wait_for_selector.assert_called_once_with(
            "#input", state="visible", timeout=8000
        )
        page.fill.assert_called_once_with("#input", "hello world")


class SelectCommunityTests(unittest.TestCase):
    """Tests for _select_community helper."""

    @patch("uploader.reddit_uploader.main.asyncio.sleep", new_callable=AsyncMock)
    def test_types_subreddit_and_selects_from_autocomplete(self, mock_sleep):
        page = FakePage()
        locator = FakeLocator()
        page.locator = MagicMock(return_value=locator)

        _run_async(_select_community(page, "python", timeout_ms=10000))

        locator.wait_for.assert_called_once_with(state="visible", timeout=10000)
        locator.click.assert_called_once()
        locator.fill.assert_called_once_with("python")
        page.wait_for_selector.assert_called_once_with(
            "li[role='option']:has-text('r/python')", state="visible", timeout=8000
        )
        page.click.assert_called_once_with(
            "li[role='option']:has-text('r/python')"
        )

    @patch("uploader.reddit_uploader.main.asyncio.sleep", new_callable=AsyncMock)
    def test_presses_enter_when_autocomplete_not_found(self, mock_sleep):
        page = FakePage()
        locator = FakeLocator()
        page.locator = MagicMock(return_value=locator)
        page.wait_for_selector = AsyncMock(side_effect=Exception("timeout"))

        _run_async(_select_community(page, "obscure_sub", timeout_ms=10000))

        locator.press.assert_called_once_with("Enter")


class UploadMediaTests(unittest.TestCase):
    """Tests for _upload_media helper."""

    @patch("uploader.reddit_uploader.main.asyncio.sleep", new_callable=AsyncMock)
    def test_clicks_images_tab_and_sets_file(self, mock_sleep):
        page = FakePage()
        tab_locator = FakeLocator()
        file_locator = FakeLocator()
        page.locator = MagicMock(side_effect=[tab_locator, file_locator])

        _run_async(_upload_media(page, "/tmp/video.mp4", timeout_ms=30000))

        tab_locator.wait_for.assert_called_once_with(state="visible", timeout=5000)
        tab_locator.click.assert_called_once()
        file_locator.set_input_files.assert_called_once()
        page.wait_for_timeout.assert_called_once_with(3000)

    @patch("uploader.reddit_uploader.main.asyncio.sleep", new_callable=AsyncMock)
    def test_handles_missing_images_tab_gracefully(self, mock_sleep):
        page = FakePage()
        tab_locator = FakeLocator()
        tab_locator.wait_for = AsyncMock(side_effect=Exception("not found"))
        file_locator = FakeLocator()
        page.locator = MagicMock(side_effect=[tab_locator, file_locator])

        _run_async(_upload_media(page, "/tmp/video.mp4", timeout_ms=30000))

        file_locator.set_input_files.assert_called_once()


class PostRedditCookieTests(unittest.TestCase):
    """Tests for _post_reddit_cookie function."""

    @patch("uploader.reddit_uploader.main.set_init_script", new_callable=AsyncMock)
    @patch("uploader.reddit_uploader.main.get_browser_options", return_value={})
    @patch("uploader.reddit_uploader.main.async_playwright")
    @patch("uploader.reddit_uploader.main.LOCAL_CHROME_HEADLESS", True)
    def test_successful_text_post_returns_url(self, mock_pw, mock_opts, mock_init):
        page = FakePage()
        page.url = "https://www.reddit.com/r/python/comments/abc123/test_post/"

        community_locator = FakeLocator()
        title_locator = FakeLocator()
        submit_locator = FakeLocator()

        def locator_factory(selector):
            if selector == COMMUNITY_SELECTOR:
                return community_locator
            elif selector == TITLE_SELECTOR:
                return title_locator
            elif selector == SUBMIT_BUTTON_SELECTOR:
                return submit_locator
            return FakeLocator()

        page.locator = MagicMock(side_effect=locator_factory)
        page.wait_for_url = AsyncMock()

        context = FakeContext(page)
        browser = FakeBrowser(context)
        pw = FakePlaywright(browser)
        mock_pw.return_value = pw

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            f.write(b'{"cookies": []}')
            account_file = f.name

        result = _run_async(
            _post_reddit_cookie(
                account_file=account_file,
                subreddit="python",
                title="Test Post",
                headless=True,
            )
        )

        self.assertEqual(result, "https://www.reddit.com/r/python/comments/abc123/test_post/")
        page.goto.assert_called_once_with(
            REDDIT_SUBMIT_URL, wait_until="domcontentloaded", timeout=30000
        )

    @patch("uploader.reddit_uploader.main.set_init_script", new_callable=AsyncMock)
    @patch("uploader.reddit_uploader.main.get_browser_options", return_value={})
    @patch("uploader.reddit_uploader.main.async_playwright")
    @patch("uploader.reddit_uploader.main.LOCAL_CHROME_HEADLESS", True)
    def test_expired_cookie_raises_runtime_error(self, mock_pw, mock_opts, mock_init):
        page = FakePage()
        page.url = "https://www.reddit.com/login"

        context = FakeContext(page)
        browser = FakeBrowser(context)
        pw = FakePlaywright(browser)
        mock_pw.return_value = pw

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            f.write(b'{"cookies": []}')
            account_file = f.name

        with self.assertRaises(RuntimeError) as ctx:
            _run_async(
                _post_reddit_cookie(
                    account_file=account_file,
                    subreddit="python",
                    title="Test Post",
                    headless=True,
                )
            )

        self.assertIn("cookie may be expired", str(ctx.exception))

    @patch("uploader.reddit_uploader.main.set_init_script", new_callable=AsyncMock)
    @patch("uploader.reddit_uploader.main.get_browser_options", return_value={})
    @patch("uploader.reddit_uploader.main.async_playwright")
    @patch("uploader.reddit_uploader.main.LOCAL_CHROME_HEADLESS", True)
    def test_post_with_body_text_fills_body(self, mock_pw, mock_opts, mock_init):
        page = FakePage()
        page.url = "https://www.reddit.com/r/python/comments/abc123/test_post/"

        community_locator = FakeLocator()
        title_locator = FakeLocator()
        body_locator = FakeLocator(count=1)
        submit_locator = FakeLocator()

        def locator_factory(selector):
            if selector == COMMUNITY_SELECTOR:
                return community_locator
            elif selector == TITLE_SELECTOR:
                return title_locator
            elif selector == BODY_SELECTOR:
                return body_locator
            elif selector == SUBMIT_BUTTON_SELECTOR:
                return submit_locator
            return FakeLocator()

        page.locator = MagicMock(side_effect=locator_factory)
        page.wait_for_url = AsyncMock()

        context = FakeContext(page)
        browser = FakeBrowser(context)
        pw = FakePlaywright(browser)
        mock_pw.return_value = pw

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            f.write(b'{"cookies": []}')
            account_file = f.name

        result = _run_async(
            _post_reddit_cookie(
                account_file=account_file,
                subreddit="python",
                title="Test Post",
                body_text="This is the body content",
                headless=True,
            )
        )

        body_locator.fill.assert_called_once_with("This is the body content")

    @patch("uploader.reddit_uploader.main.set_init_script", new_callable=AsyncMock)
    @patch("uploader.reddit_uploader.main.get_browser_options", return_value={})
    @patch("uploader.reddit_uploader.main.async_playwright")
    @patch("uploader.reddit_uploader.main.LOCAL_CHROME_HEADLESS", True)
    def test_post_with_media_calls_upload(self, mock_pw, mock_opts, mock_init):
        page = FakePage()
        page.url = "https://www.reddit.com/r/python/comments/abc123/test_post/"

        community_locator = FakeLocator()
        title_locator = FakeLocator()
        tab_locator = FakeLocator()
        file_locator = FakeLocator()
        submit_locator = FakeLocator()

        def locator_factory(selector):
            if selector == COMMUNITY_SELECTOR:
                return community_locator
            elif selector == TITLE_SELECTOR:
                return title_locator
            elif selector == "button[role='tab']:has-text('Images')":
                return tab_locator
            elif selector == FILE_INPUT_SELECTOR:
                return file_locator
            elif selector == SUBMIT_BUTTON_SELECTOR:
                return submit_locator
            return FakeLocator()

        page.locator = MagicMock(side_effect=locator_factory)
        page.wait_for_url = AsyncMock()

        context = FakeContext(page)
        browser = FakeBrowser(context)
        pw = FakePlaywright(browser)
        mock_pw.return_value = pw

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            f.write(b'{"cookies": []}')
            account_file = f.name

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b'video content')
            media_file = f.name

        result = _run_async(
            _post_reddit_cookie(
                account_file=account_file,
                subreddit="python",
                title="Video Post",
                media_path=media_file,
                headless=True,
            )
        )

        file_locator.set_input_files.assert_called_once()


class RedditCookieVideoTests(unittest.TestCase):
    """Tests for RedditCookieVideo class."""

    def test_strips_r_prefix_from_subreddit(self):
        uploader = RedditCookieVideo(
            title="Test",
            subreddit="r/python",
            account_file="/tmp/cookies.json",
        )
        self.assertEqual(uploader.subreddit, "python")

    def test_keeps_subreddit_without_prefix(self):
        uploader = RedditCookieVideo(
            title="Test",
            subreddit="python",
            account_file="/tmp/cookies.json",
        )
        self.assertEqual(uploader.subreddit, "python")

    def test_stores_all_parameters(self):
        uploader = RedditCookieVideo(
            title="My Title",
            subreddit="r/test",
            account_file="/tmp/cookies.json",
            file_path="/tmp/video.mp4",
            body_text="Body content",
            headless=False,
        )
        self.assertEqual(uploader.title, "My Title")
        self.assertEqual(uploader.subreddit, "test")
        self.assertEqual(uploader.account_file, "/tmp/cookies.json")
        self.assertEqual(uploader.file_path, "/tmp/video.mp4")
        self.assertEqual(uploader.body_text, "Body content")

    @patch("uploader.reddit_uploader.main._post_reddit_cookie", new_callable=AsyncMock)
    @patch("uploader.reddit_uploader.main.LOCAL_CHROME_HEADLESS", True)
    def test_main_delegates_to_post_reddit_cookie(self, mock_post):
        mock_post.return_value = "https://reddit.com/r/test/comments/abc"

        uploader = RedditCookieVideo(
            title="Test",
            subreddit="python",
            account_file="/tmp/cookies.json",
            file_path="/tmp/video.mp4",
            body_text="Body",
            headless=True,
        )

        result = _run_async(uploader.main())

        mock_post.assert_called_once_with(
            account_file="/tmp/cookies.json",
            subreddit="python",
            title="Test",
            body_text="Body",
            media_path="/tmp/video.mp4",
            headless=True,
        )
        self.assertEqual(result, "https://reddit.com/r/test/comments/abc")

    @patch("uploader.reddit_uploader.main._post_reddit_cookie", new_callable=AsyncMock)
    @patch("uploader.reddit_uploader.main.LOCAL_CHROME_HEADLESS", True)
    def test_main_passes_none_for_empty_file_path(self, mock_post):
        mock_post.return_value = "https://reddit.com/r/test/comments/abc"

        uploader = RedditCookieVideo(
            title="Test",
            subreddit="python",
            account_file="/tmp/cookies.json",
            file_path="",
        )

        _run_async(uploader.main())

        mock_post.assert_called_once_with(
            account_file="/tmp/cookies.json",
            subreddit="python",
            title="Test",
            body_text="",
            media_path=None,
            headless=True,
        )


class SelectorConstantsTests(unittest.TestCase):
    """Verify selector constants match expected Reddit DOM patterns."""

    def test_submit_url_is_reddit_submit(self):
        self.assertEqual(REDDIT_SUBMIT_URL, "https://www.reddit.com/submit")

    def test_login_url_is_reddit_login(self):
        self.assertEqual(REDDIT_LOGIN_URL, "https://www.reddit.com/login")

    def test_title_selector_targets_textarea(self):
        self.assertIn("textarea", TITLE_SELECTOR)
        self.assertIn("Title", TITLE_SELECTOR)

    def test_community_selector_targets_input(self):
        self.assertIn("input", COMMUNITY_SELECTOR)
        self.assertIn("community", COMMUNITY_SELECTOR.lower())

    def test_submit_button_selector_targets_submit_type(self):
        self.assertIn("submit", SUBMIT_BUTTON_SELECTOR)

    def test_file_input_selector_targets_file_type(self):
        self.assertIn("file", FILE_INPUT_SELECTOR)


if __name__ == "__main__":
    unittest.main()
