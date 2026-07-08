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
    conf_module.DEBUG_MODE = True
    conf_module.LOCAL_CHROME_HEADLESS = True
    conf_module.LOCAL_CHROME_PATH = ""
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
        self.evaluate = AsyncMock(return_value="")


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
    """Tests for _post_reddit_cookie API-first function."""

    @patch("uploader.reddit_uploader.main._post_via_api")
    def test_api_success_returns_url(self, mock_api):
        mock_api.return_value = "https://reddit.com/r/python/comments/abc/post"
        result = _run_async(
            _post_reddit_cookie(
                account_file="/tmp/cookies.json",
                subreddit="python",
                title="Test Post",
                headless=True,
            )
        )
        self.assertEqual(result, "https://reddit.com/r/python/comments/abc/post")
        mock_api.assert_called_once()

    @patch("uploader.reddit_uploader.main._post_via_api")
    def test_api_passes_correct_args(self, mock_api):
        mock_api.return_value = "https://reddit.com/r/test/comments/abc/post"
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"video")
            media_file = f.name
        _run_async(
            _post_reddit_cookie(
                account_file="/tmp/cookies.json",
                subreddit="test",
                title="Test",
                body_text="Body",
                media_path=media_file,
                headless=True,
            )
        )
        args = mock_api.call_args[0]
        self.assertEqual(args[0], "/tmp/cookies.json")
        self.assertEqual(args[1], "test")
        self.assertEqual(args[2], "Test")
        self.assertEqual(args[3], "Body")
        self.assertEqual(args[4], media_file)

    @patch("uploader.reddit_uploader.main._post_via_api")
    def test_api_failure_falls_back_to_browser(self, mock_api):
        mock_api.side_effect = Exception("API failed")
        with patch("uploader.reddit_uploader.main._post_via_browser", new_callable=AsyncMock) as mock_browser:
            mock_browser.return_value = "https://reddit.com/r/test/comments/abc/post"
            result = _run_async(
                _post_reddit_cookie(
                    account_file="/tmp/cookies.json",
                    subreddit="test",
                    title="Test",
                    headless=True,
                )
            )
        self.assertEqual(result, "https://reddit.com/r/test/comments/abc/post")
        mock_browser.assert_called_once()

    @patch("uploader.reddit_uploader.main._post_via_api")
    def test_all_proxies_fail_raises(self, mock_api):
        mock_api.side_effect = Exception("all failed")
        with patch("uploader.reddit_uploader.main._post_via_browser", new_callable=AsyncMock, side_effect=Exception("browser failed")):
            with self.assertRaises(Exception) as ctx:
                _run_async(
                    _post_reddit_cookie(
                        account_file="/tmp/cookies.json",
                        subreddit="test",
                        title="Test",
                        headless=True,
                    )
                )
            # Browser error is raised when API also fails
            self.assertIn("browser failed", str(ctx.exception))


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


class WaitAndClickErrorTests(unittest.TestCase):
    """Error-path tests for _wait_and_click helper."""

    @patch("uploader.reddit_uploader.main.asyncio.sleep", new_callable=AsyncMock)
    def test_timeout_from_wait_for_selector_propagates(self, mock_sleep):
        page = FakePage()
        page.wait_for_selector = AsyncMock(side_effect=Exception("Timeout 5000ms exceeded"))
        with self.assertRaises(Exception) as ctx:
            _run_async(_wait_and_click(page, "#gone", timeout_ms=5000))
        self.assertIn("Timeout", str(ctx.exception))
        page.click.assert_not_called()

    @patch("uploader.reddit_uploader.main.asyncio.sleep", new_callable=AsyncMock)
    def test_click_failure_propagates(self, mock_sleep):
        page = FakePage()
        page.click = AsyncMock(side_effect=Exception("element detached"))
        with self.assertRaises(Exception) as ctx:
            _run_async(_wait_and_click(page, "#sel", timeout_ms=5000))
        self.assertIn("detached", str(ctx.exception))
        page.wait_for_selector.assert_called_once()


class WaitAndFillErrorTests(unittest.TestCase):
    """Error-path tests for _wait_and_fill helper."""

    @patch("uploader.reddit_uploader.main.asyncio.sleep", new_callable=AsyncMock)
    def test_timeout_from_wait_for_selector_propagates(self, mock_sleep):
        page = FakePage()
        page.wait_for_selector = AsyncMock(side_effect=Exception("Timeout 8000ms exceeded"))
        with self.assertRaises(Exception) as ctx:
            _run_async(_wait_and_fill(page, "#input", "text", timeout_ms=8000))
        self.assertIn("Timeout", str(ctx.exception))
        page.fill.assert_not_called()

    @patch("uploader.reddit_uploader.main.asyncio.sleep", new_callable=AsyncMock)
    def test_fill_failure_propagates(self, mock_sleep):
        page = FakePage()
        page.fill = AsyncMock(side_effect=Exception("not editable"))
        with self.assertRaises(Exception) as ctx:
            _run_async(_wait_and_fill(page, "#input", "text", timeout_ms=5000))
        self.assertIn("not editable", str(ctx.exception))


class SelectCommunityEdgeCaseTests(unittest.TestCase):
    """Edge-case tests for _select_community helper."""

    @patch("uploader.reddit_uploader.main.asyncio.sleep", new_callable=AsyncMock)
    def test_timeout_on_input_wait_propagates(self, mock_sleep):
        page = FakePage()
        locator = FakeLocator()
        locator.wait_for = AsyncMock(side_effect=Exception("element not found"))
        page.locator = MagicMock(return_value=locator)
        with self.assertRaises(Exception) as ctx:
            _run_async(_select_community(page, "python", timeout_ms=5000))
        self.assertIn("not found", str(ctx.exception))

    @patch("uploader.reddit_uploader.main.asyncio.sleep", new_callable=AsyncMock)
    def test_fill_failure_on_community_input_propagates(self, mock_sleep):
        page = FakePage()
        locator = FakeLocator()
        locator.fill = AsyncMock(side_effect=Exception("fill failed"))
        page.locator = MagicMock(return_value=locator)
        with self.assertRaises(Exception):
            _run_async(_select_community(page, "python", timeout_ms=5000))

    @patch("uploader.reddit_uploader.main.asyncio.sleep", new_callable=AsyncMock)
    def test_both_autocomplete_and_enter_fail_raises(self, mock_sleep):
        """If autocomplete times out AND Enter press fails, the error propagates."""
        page = FakePage()
        locator = FakeLocator()
        locator.press = AsyncMock(side_effect=Exception("press failed"))
        page.locator = MagicMock(return_value=locator)
        page.wait_for_selector = AsyncMock(side_effect=Exception("timeout"))
        with self.assertRaises(Exception):
            _run_async(_select_community(page, "obscure", timeout_ms=5000))


class UploadMediaEdgeCaseTests(unittest.TestCase):
    """Edge-case tests for _upload_media helper."""

    @patch("uploader.reddit_uploader.main.asyncio.sleep", new_callable=AsyncMock)
    def test_media_path_as_path_object_resolves(self, mock_sleep):
        page = FakePage()
        tab_locator = FakeLocator()
        file_locator = FakeLocator()
        page.locator = MagicMock(side_effect=[tab_locator, file_locator])

        _run_async(_upload_media(page, Path("/tmp/video.mp4"), timeout_ms=30000))

        file_locator.set_input_files.assert_called_once()
        called_path = file_locator.set_input_files.call_args[0][0]
        self.assertTrue(Path(called_path).is_absolute())

    @patch("uploader.reddit_uploader.main.asyncio.sleep", new_callable=AsyncMock)
    def test_set_input_files_failure_propagates(self, mock_sleep):
        page = FakePage()
        tab_locator = FakeLocator()
        file_locator = FakeLocator()
        file_locator.set_input_files = AsyncMock(side_effect=Exception("file not found"))
        page.locator = MagicMock(side_effect=[tab_locator, file_locator])

        with self.assertRaises(Exception) as ctx:
            _run_async(_upload_media(page, "/tmp/missing.mp4", timeout_ms=30000))
        self.assertIn("file not found", str(ctx.exception))

    @patch("uploader.reddit_uploader.main.asyncio.sleep", new_callable=AsyncMock)
    def test_tab_click_failure_is_swallowed_and_file_still_set(self, mock_sleep):
        """If clicking the Images tab fails, file upload should still proceed."""
        page = FakePage()
        tab_locator = FakeLocator()
        tab_locator.click = AsyncMock(side_effect=Exception("click intercepted"))
        file_locator = FakeLocator()
        page.locator = MagicMock(side_effect=[tab_locator, file_locator])

        _run_async(_upload_media(page, "/tmp/video.mp4", timeout_ms=30000))

        file_locator.set_input_files.assert_called_once()

    @patch("uploader.reddit_uploader.main.asyncio.sleep", new_callable=AsyncMock)
    def test_wait_for_timeout_called_after_file_upload(self, mock_sleep):
        page = FakePage()
        tab_locator = FakeLocator()
        file_locator = FakeLocator()
        page.locator = MagicMock(side_effect=[tab_locator, file_locator])

        _run_async(_upload_media(page, "/tmp/video.mp4", timeout_ms=30000))

        page.wait_for_timeout.assert_called_once_with(3000)


class PostRedditCookieErrorTests(unittest.TestCase):
    """Error-path tests for _post_reddit_cookie function."""

    @patch("uploader.reddit_uploader.main._post_via_api")
    def test_api_failure_falls_back_to_browser(self, mock_api):
        mock_api.side_effect = RuntimeError("API failed")
        with patch("uploader.reddit_uploader.main._post_via_browser", new_callable=AsyncMock, return_value="https://reddit.com/r/test/comments/abc"):
            result = _run_async(
                _post_reddit_cookie(
                    account_file="/tmp/cookies.json",
                    subreddit="test",
                    title="Test",
                    headless=True,
                )
            )
        self.assertEqual(result, "https://reddit.com/r/test/comments/abc")

    @patch("uploader.reddit_uploader.main._post_via_api")
    def test_both_api_and_browser_fail_raises(self, mock_api):
        mock_api.side_effect = RuntimeError("API failed")
        with patch("uploader.reddit_uploader.main._post_via_browser", new_callable=AsyncMock, side_effect=Exception("browser also failed")):
            with self.assertRaises(Exception) as ctx:
                _run_async(
                    _post_reddit_cookie(
                        account_file="/tmp/cookies.json",
                        subreddit="test",
                        title="Test",
                        headless=True,
                    )
                )
            self.assertIn("browser also failed", str(ctx.exception))

    @patch("uploader.reddit_uploader.main._post_via_api")
    def test_resolves_account_file_to_absolute(self, mock_api):
        mock_api.return_value = "https://reddit.com/r/test/comments/abc/post"
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            f.write(b'{"cookies": []}')
            account_file = f.name
        _run_async(
            _post_reddit_cookie(
                account_file=account_file,
                subreddit="test",
                title="Test",
                headless=True,
            )
        )
        args = mock_api.call_args[0]
        self.assertTrue(Path(args[0]).is_absolute())


class RedditCookieVideoEdgeCaseTests(unittest.TestCase):
    """Edge-case tests for RedditCookieVideo class."""

    def test_multiple_r_prefixes_stripped(self):
        """lstrip('r/') strips all leading r and / chars, so r/r/python → python."""
        uploader = RedditCookieVideo(
            title="Test",
            subreddit="r/r/python",
            account_file="/tmp/cookies.json",
        )
        self.assertEqual(uploader.subreddit, "python")

    def test_subreddit_with_special_characters(self):
        uploader = RedditCookieVideo(
            title="Test",
            subreddit="r/C++",
            account_file="/tmp/cookies.json",
        )
        self.assertEqual(uploader.subreddit, "C++")

    def test_subreddit_with_underscores(self):
        uploader = RedditCookieVideo(
            title="Test",
            subreddit="r/AskReddit",
            account_file="/tmp/cookies.json",
        )
        self.assertEqual(uploader.subreddit, "AskReddit")

    def test_headless_defaults_to_true(self):
        uploader = RedditCookieVideo(
            title="Test",
            subreddit="python",
            account_file="/tmp/cookies.json",
        )
        self.assertTrue(uploader.headless)

    def test_body_text_defaults_to_empty(self):
        uploader = RedditCookieVideo(
            title="Test",
            subreddit="python",
            account_file="/tmp/cookies.json",
        )
        self.assertEqual(uploader.body_text, "")

    def test_file_path_defaults_to_empty(self):
        uploader = RedditCookieVideo(
            title="Test",
            subreddit="python",
            account_file="/tmp/cookies.json",
        )
        self.assertEqual(uploader.file_path, "")

    @patch("uploader.reddit_uploader.main._post_reddit_cookie", new_callable=AsyncMock)
    @patch("uploader.reddit_uploader.main.LOCAL_CHROME_HEADLESS", True)
    def test_main_returns_post_url(self, mock_post):
        mock_post.return_value = "https://reddit.com/r/test/comments/xyz/my_post"
        uploader = RedditCookieVideo(
            title="Test",
            subreddit="python",
            account_file="/tmp/cookies.json",
        )
        result = _run_async(uploader.main())
        self.assertEqual(result, "https://reddit.com/r/test/comments/xyz/my_post")

    @patch("uploader.reddit_uploader.main._post_reddit_cookie", new_callable=AsyncMock)
    @patch("uploader.reddit_uploader.main.LOCAL_CHROME_HEADLESS", True)
    def test_main_propagates_exception(self, mock_post):
        mock_post.side_effect = RuntimeError("cookie may be expired")
        uploader = RedditCookieVideo(
            title="Test",
            subreddit="python",
            account_file="/tmp/cookies.json",
        )
        with self.assertRaises(RuntimeError) as ctx:
            _run_async(uploader.main())
        self.assertIn("expired", str(ctx.exception))

    @patch("uploader.reddit_uploader.main._post_reddit_cookie", new_callable=AsyncMock)
    @patch("uploader.reddit_uploader.main.LOCAL_CHROME_HEADLESS", True)
    def test_main_with_all_optional_params(self, mock_post):
        mock_post.return_value = "https://reddit.com/r/all/comments/abc/post"
        uploader = RedditCookieVideo(
            title="Full Title",
            subreddit="r/all",
            account_file="/tmp/cookies.json",
            file_path="/tmp/vid.mp4",
            body_text="Extra info",
            headless=False,
        )
        result = _run_async(uploader.main())
        mock_post.assert_called_once_with(
            account_file="/tmp/cookies.json",
            subreddit="all",
            title="Full Title",
            body_text="Extra info",
            media_path="/tmp/vid.mp4",
            headless=False,
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
