"""Regression tests for Reddit cookie-uploader fixes.

Covers:
1. get_browser_options() arity – must be callable with 0 args (regression for
   the historical ``get_browser_options(p)`` bug, commit 8fc146e).
2. Credential-failure path – expired cookies must raise PreparedPublishError
   with retryable=False, NOT a raw RuntimeError.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Bootstrap a minimal ``conf`` module so the uploader can be imported without
# a real conf.py on disk.
# ---------------------------------------------------------------------------
if "conf" not in sys.modules:
    conf_module = types.ModuleType("conf")
    conf_module.BASE_DIR = str(Path(__file__).resolve().parent.parent)
    conf_module.DEBUG_MODE = True
    conf_module.LOCAL_CHROME_HEADLESS = True
    conf_module.LOCAL_CHROME_PATH = ""
    sys.modules["conf"] = conf_module


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── 1.  get_browser_options() arity regression ─────────────────────────────


class GetBrowserOptionsArityTests(unittest.TestCase):
    """get_browser_options() must accept 0 positional arguments.

    A historical bug passed the Playwright instance as the first argument
    (``get_browser_options(p)``), which raised::

        TypeError: get_browser_options() takes 0 positional arguments but 1 was given

    These tests pin the contract so it never regresses.
    """

    def test_hook_signature_takes_no_positional_args(self):
        from utils.browser_hook import get_browser_options

        sig = inspect.signature(get_browser_options)
        positional = [
            p
            for p in sig.parameters.values()
            if p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
            and p.default is inspect.Parameter.empty
        ]
        self.assertEqual(
            positional,
            [],
            "utils.browser_hook.get_browser_options must take 0 required "
            "positional arguments",
        )

    def test_hook_callable_with_zero_args(self):
        from utils.browser_hook import get_browser_options

        result = get_browser_options()
        self.assertIsInstance(result, dict)
        self.assertIn("headless", result)
        self.assertIn("args", result)

    def test_reddit_uploader_imports_hook_version(self):
        """The reddit uploader must use the 0-arg hook, not a local copy."""
        from utils.browser_hook import get_browser_options as hook_fn

        # Verify the import in the reddit module resolves to the same function.
        import uploader.reddit_uploader.main as reddit_mod

        self.assertIs(
            reddit_mod.get_browser_options,
            hook_fn,
            "reddit_uploader.main.get_browser_options should be the same "
            "function as utils.browser_hook.get_browser_options",
        )

    def test_calling_with_one_arg_raises_typeerror(self):
        """Document the exact error that the historical bug produced."""
        from utils.browser_hook import get_browser_options

        with self.assertRaises(TypeError) as ctx:
            get_browser_options("bogus-playwright-instance")  # type: ignore[arg-type]
        self.assertIn("positional argument", str(ctx.exception))


# ── 2.  Credential-failure path ────────────────────────────────────────────


class RedditCredentialErrorTests(unittest.TestCase):
    """RedditCredentialError is raised for dead cookies, not retryable."""

    def test_user_required_code_is_credential_error(self):
        from uploader.reddit_uploader.proxy_manager import _is_credential_error_code

        self.assertTrue(_is_credential_error_code("USER_REQUIRED", "please log in"))

    def test_please_log_in_message_is_credential_error(self):
        from uploader.reddit_uploader.proxy_manager import _is_credential_error_code

        self.assertTrue(
            _is_credential_error_code("", "Please log in to do that.")
        )

    def test_rate_limit_is_not_credential_error(self):
        from uploader.reddit_uploader.proxy_manager import _is_credential_error_code

        self.assertFalse(_is_credential_error_code("RATELIMIT", "take a break"))

    def test_401_http_status_is_credential_error(self):
        from uploader.reddit_uploader.proxy_manager import _looks_like_credential_error

        self.assertTrue(_looks_like_credential_error(401, ""))

    def test_403_http_status_is_credential_error(self):
        from uploader.reddit_uploader.proxy_manager import _looks_like_credential_error

        self.assertTrue(_looks_like_credential_error(403, ""))

    def test_500_is_not_credential_error(self):
        from uploader.reddit_uploader.proxy_manager import _looks_like_credential_error

        self.assertFalse(_looks_like_credential_error(500, "internal error"))


class ProxyManagerSubmitCredentialTests(unittest.TestCase):
    """submit_post / submit_self_post raise RedditCredentialError on auth failure."""

    def _make_client(self, response):
        from uploader.reddit_uploader.proxy_manager import RedditClient

        client = RedditClient("/tmp/fake.json", [])
        client.post = MagicMock(return_value=response)
        return client

    def test_submit_post_raises_on_user_required(self):
        from uploader.reddit_uploader.proxy_manager import RedditCredentialError

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "json": {"errors": [["USER_REQUIRED", "Please log in to do that.", ""]]}
        }
        client = self._make_client(resp)

        with self.assertRaises(RedditCredentialError) as ctx:
            client.submit_post("test", "title", "https://example.com")
        self.assertIn("log in", str(ctx.exception).lower())

    def test_submit_post_raises_on_401(self):
        from uploader.reddit_uploader.proxy_manager import RedditCredentialError

        resp = MagicMock()
        resp.status_code = 401
        resp.text = "Unauthorized"
        client = self._make_client(resp)

        with self.assertRaises(RedditCredentialError):
            client.submit_post("test", "title", "https://example.com")

    def test_submit_self_post_raises_on_user_required(self):
        from uploader.reddit_uploader.proxy_manager import RedditCredentialError

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "json": {"errors": [["USER_REQUIRED", "Please log in to do that.", ""]]}
        }
        client = self._make_client(resp)

        with self.assertRaises(RedditCredentialError):
            client.submit_self_post("test", "title", "body text")

    def test_submit_post_returns_none_on_non_credential_error(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "json": {"errors": [["RATELIMIT", "take a break", ""]]}
        }
        client = self._make_client(resp)
        result = client.submit_post("test", "title", "https://example.com")
        self.assertIsNone(result)


class PostRedditCookieCredentialPathTests(unittest.TestCase):
    """_post_reddit_cookie must raise PreparedPublishError(retryable=False) on
    credential failure, NOT fall back to browser."""

    @patch("uploader.reddit_uploader.main._post_via_api")
    def test_credential_error_does_not_fall_back_to_browser(self, mock_api):
        from uploader.reddit_uploader.proxy_manager import RedditCredentialError

        # Import PreparedPublishError from the reddit module (it imports it)
        from uploader.reddit_uploader.main import PreparedPublishError

        mock_api.side_effect = RedditCredentialError("cookie expired")

        with patch(
            "uploader.reddit_uploader.main._post_via_browser",
            new_callable=AsyncMock,
        ) as mock_browser:
            with self.assertRaises(PreparedPublishError) as ctx:
                _run_async(
                    _post_reddit_cookie(
                        account_file="/tmp/cookies.json",
                        subreddit="test",
                        title="Test",
                        headless=True,
                    )
                )

            # Browser fallback must NOT have been called
            mock_browser.assert_not_called()
            # Must be non-retryable
            self.assertFalse(ctx.exception.retryable)
            self.assertIn("expired", str(ctx.exception).lower())

    @patch("uploader.reddit_uploader.main._post_via_api")
    def test_non_credential_error_still_falls_back_to_browser(self, mock_api):
        """A transient API error should still fall back to browser."""
        mock_api.side_effect = RuntimeError("transient network error")

        with patch(
            "uploader.reddit_uploader.main._post_via_browser",
            new_callable=AsyncMock,
            return_value="https://reddit.com/r/test/comments/abc/post",
        ) as mock_browser:
            result = _run_async(
                _post_reddit_cookie(
                    account_file="/tmp/cookies.json",
                    subreddit="test",
                    title="Test",
                    headless=True,
                )
            )

        mock_browser.assert_called_once()
        self.assertEqual(result, "https://reddit.com/r/test/comments/abc/post")


class BrowserLoginRedirectTests(unittest.TestCase):
    """_post_via_browser raises PreparedPublishError(retryable=False) when
    Reddit redirects to the login page."""

    @patch("uploader.reddit_uploader.main._get_proxy_list", return_value=[])
    @patch("uploader.reddit_uploader.main.get_browser_options")
    def test_login_redirect_raises_prepared_publish_error(
        self, mock_opts, mock_proxies
    ):
        from uploader.reddit_uploader.main import (
            PreparedPublishError,
            _post_via_browser,
        )

        mock_opts.return_value = {"headless": True, "args": []}

        # Fake page that always shows /login in URL
        fake_page = MagicMock()
        fake_page.url = "https://www.reddit.com/login"
        fake_page.goto = AsyncMock()
        fake_page.evaluate = AsyncMock(return_value="normal page content")
        fake_page.wait_for_timeout = AsyncMock()

        fake_context = MagicMock()
        fake_context.new_page = AsyncMock(return_value=fake_page)
        fake_context.close = AsyncMock()

        fake_browser = MagicMock()
        fake_browser.new_context = AsyncMock(return_value=fake_context)
        fake_browser.close = AsyncMock()

        fake_chromium = MagicMock()
        fake_chromium.launch = AsyncMock(return_value=fake_browser)

        fake_playwright = MagicMock()
        fake_playwright.chromium = fake_chromium
        fake_playwright.__aenter__ = AsyncMock(return_value=fake_playwright)
        fake_playwright.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "uploader.reddit_uploader.main.async_playwright",
            return_value=fake_playwright,
        ), patch(
            "uploader.reddit_uploader.main.set_init_script",
            new_callable=AsyncMock,
        ), patch(
            "uploader.reddit_uploader.main.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            with self.assertRaises(PreparedPublishError) as ctx:
                _run_async(
                    _post_via_browser(
                        "/tmp/cookies.json",
                        "test",
                        "Test",
                        "",
                        None,
                        True,
                    )
                )

        self.assertFalse(ctx.exception.retryable)
        self.assertIn("login", str(ctx.exception).lower())


# ── Import helper for tests ────────────────────────────────────────────────

from uploader.reddit_uploader.main import _post_reddit_cookie  # noqa: E402

if __name__ == "__main__":
    unittest.main()
