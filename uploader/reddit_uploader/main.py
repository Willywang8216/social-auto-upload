"""Reddit publishing with self-healing proxy rotation and cookie refresh.

Uses RedditClient for API posting with automatic:
- Proxy rotation when blocked
- token_v2 refresh when expired
- Cookie cleanup when invalidated
- Fallback to browser automation if API fails
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from patchright.async_api import Page, async_playwright

from utils.conf_defaults import DEBUG_MODE, LOCAL_CHROME_HEADLESS, REDDIT_PROXY
from utils.browser_hook import get_browser_options
from utils.base_social_media import set_init_script
from uploader.reddit_uploader.proxy_manager import RedditClient

log = logging.getLogger(__name__)

REDDIT_SUBMIT_URL = "https://www.reddit.com/submit"
REDDIT_LOGIN_URL = "https://www.reddit.com/login"

TITLE_SELECTOR = "textarea[placeholder='Title']"
BODY_SELECTOR = "div[contenteditable='true']"
COMMUNITY_SELECTOR = "input[placeholder='Choose a community']"
SUBMIT_BUTTON_SELECTOR = "button[type='submit']"
IMAGE_TAB_SELECTOR = "button[role='tab']:has-text('Images')"
FILE_INPUT_SELECTOR = "input[type='file']"


def _get_proxy_list() -> list[str]:
    """Get list of proxies from config."""
    if not REDDIT_PROXY:
        return []
    if isinstance(REDDIT_PROXY, str):
        return [REDDIT_PROXY]
    if isinstance(REDDIT_PROXY, (list, tuple)):
        return [p for p in REDDIT_PROXY if p]
    return []


def _resolve_media_url(media_path: str, cookie_file: str) -> str | None:
    """Resolve a local file path to a public URL from the database."""
    import sqlite3 as _sqlite3

    if media_path.startswith("http://") or media_path.startswith("https://"):
        return media_path

    filename = Path(media_path).name
    parts = filename.split("_", 1)
    orig_filename = parts[1] if len(parts) > 1 else filename
    db_path = Path(cookie_file).parent.parent / "db" / "database.db"
    if not db_path.exists():
        return None

    try:
        with _sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = _sqlite3.Row
            row = conn.execute(
                "SELECT storage_cdn_url FROM file_records WHERE (file_path LIKE ? OR filename = ? OR filename = ?) AND storage_cdn_url IS NOT NULL LIMIT 1",
                (f"%{filename}%", filename, orig_filename),
            ).fetchone()
            if row and row["storage_cdn_url"]:
                return row["storage_cdn_url"]

            row = conn.execute(
                "SELECT public_url FROM media_assets WHERE (local_original_path LIKE ? OR original_filename = ? OR original_filename = ?) AND public_url IS NOT NULL LIMIT 1",
                (f"%{filename}%", filename, orig_filename),
            ).fetchone()
            if row and row["public_url"]:
                return row["public_url"]
    except Exception:
        pass
    return None


def _post_via_api(
    cookie_file: str,
    subreddit: str,
    title: str,
    body_text: str = "",
    media_path: str | None = None,
    proxy: str | None = None,
) -> str:
    """Post to Reddit using the self-healing RedditClient."""
    proxy_list = _get_proxy_list()
    client = RedditClient(cookie_file, proxy_list)

    # Resolve media to URL if needed
    media_url = None
    if media_path:
        media_url = _resolve_media_url(str(media_path), cookie_file)

    # Submit the post
    if media_url and media_url.startswith("http"):
        post_url = client.submit_post(subreddit, title, media_url)
    elif body_text:
        post_url = client.submit_self_post(subreddit, title, body_text)
    else:
        # Default to link post
        post_url = client.submit_post(subreddit, title, media_url or "https://www.reddit.com")

    if not post_url:
        raise RuntimeError(f"Failed to submit post to r/{subreddit}")

    return post_url


async def _post_reddit_cookie(
    account_file: str | Path,
    subreddit: str,
    title: str,
    body_text: str = "",
    media_path: str | Path | None = None,
    *,
    headless: bool = True,
) -> str:
    """Post to Reddit. Tries API first with self-healing proxy, falls back to browser."""
    resolved_account = str(Path(account_file).expanduser().resolve())

    try:
        return _post_via_api(
            resolved_account, subreddit, title,
            body_text, str(media_path) if media_path else None,
        )
    except Exception as e:
        log.warning("API posting failed: %s. Trying browser automation...", e)
        return await _post_via_browser(
            resolved_account, subreddit, title,
            body_text, media_path, headless,
        )


# --- Playwright helper functions (used by tests and browser fallback) ---

async def _wait_and_click(page, selector: str, *, timeout_ms: int = 15000) -> None:
    await page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
    await page.click(selector)


async def _wait_and_fill(page, selector: str, text: str, *, timeout_ms: int = 15000) -> None:
    await page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
    await page.fill(selector, text)


async def _select_community(page, subreddit: str, *, timeout_ms: int = 15000) -> None:
    inp = page.locator(COMMUNITY_SELECTOR)
    await inp.wait_for(state="visible", timeout=timeout_ms)
    await inp.click()
    await inp.fill(subreddit)
    await asyncio.sleep(1.5)
    option_selector = f"li[role='option']:has-text('r/{subreddit}')"
    try:
        await page.wait_for_selector(option_selector, state="visible", timeout=8000)
        await page.click(option_selector)
    except Exception:
        await inp.press("Enter")
    await asyncio.sleep(0.5)


async def _upload_media(page, file_path: str | Path, *, timeout_ms: int = 30000) -> None:
    resolved = Path(file_path).expanduser().resolve()
    tab = page.locator(IMAGE_TAB_SELECTOR)
    try:
        await tab.wait_for(state="visible", timeout=5000)
        await tab.click()
    except Exception:
        pass
    file_input = page.locator(FILE_INPUT_SELECTOR)
    await file_input.set_input_files(str(resolved))
    await page.wait_for_timeout(3000)


async def _check_page_blocked(page: Page) -> bool:
    try:
        body = await page.evaluate("document.body.innerText")
        return "blocked by network security" in body.lower()
    except Exception:
        return False


async def _post_via_browser(
    account_file: str,
    subreddit: str,
    title: str,
    body_text: str,
    media_path: str | Path | None,
    headless: bool,
) -> str:
    """Fallback: post via browser automation with proxy support."""
    proxy_list = _get_proxy_list()

    async with async_playwright() as p:
        launch_options = get_browser_options()
        launch_options["headless"] = headless if LOCAL_CHROME_HEADLESS else False
        if proxy_list:
            launch_options["proxy"] = {"server": proxy_list[0]}
        browser = await p.chromium.launch(**launch_options)
        context = await browser.new_context(
            storage_state=account_file,
            viewport={"width": 1280, "height": 900},
        )
        await set_init_script(context)
        page = await context.new_page()

        try:
            await page.goto(REDDIT_SUBMIT_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            if await _check_page_blocked(page):
                raise RuntimeError("Reddit blocked this IP address")

            if "/login" in page.url:
                await page.goto(REDDIT_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)

            if "/login" in page.url:
                raise RuntimeError("Not logged in - re-login required")

            await _select_community(page, subreddit)
            await _wait_and_fill(page, TITLE_SELECTOR, title, timeout_ms=10000)

            if body_text:
                body = page.locator(BODY_SELECTOR)
                if await body.count() > 0:
                    await body.first.fill(body_text)

            if media_path:
                await _upload_media(page, media_path)
                await page.wait_for_timeout(3000)

            submit_btn = page.locator(SUBMIT_BUTTON_SELECTOR).first
            await submit_btn.wait_for(state="visible", timeout=10000)
            await submit_btn.click()

            await page.wait_for_url(re.compile(r"/comments/"), timeout=60000)
            post_url = page.url
        finally:
            await context.close()
            await browser.close()

    return post_url


class RedditCookieVideo:
    def __init__(
        self,
        title: str,
        subreddit: str,
        account_file: str,
        file_path: str = "",
        body_text: str = "",
        headless: bool = True,
    ):
        self.title = title
        self.subreddit = subreddit.lstrip("r/")
        self.account_file = account_file
        self.file_path = file_path
        self.body_text = body_text
        self.headless = headless if LOCAL_CHROME_HEADLESS else False

    async def main(self) -> str:
        return await _post_reddit_cookie(
            account_file=self.account_file,
            subreddit=self.subreddit,
            title=self.title,
            body_text=self.body_text,
            media_path=self.file_path or None,
            headless=self.headless,
        )
