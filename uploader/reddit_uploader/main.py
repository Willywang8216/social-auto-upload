"""Cookie-based Reddit publishing via Playwright (browser automation).

Route: navigate to Reddit submit page, fill in subreddit/title/media, post.
"""
from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

from patchright.async_api import Page, async_playwright

from utils.conf_defaults import DEBUG_MODE, LOCAL_CHROME_HEADLESS, REDDIT_PROXY
from utils.browser_hook import get_browser_options
from utils.base_social_media import set_init_script

REDDIT_SUBMIT_URL = "https://www.reddit.com/submit"
REDDIT_LOGIN_URL = "https://www.reddit.com/login"

TITLE_SELECTOR = "textarea[placeholder='Title']"
BODY_SELECTOR = "div[contenteditable='true']"
COMMUNITY_SELECTOR = "input[placeholder='Choose a community']"
SUBMIT_BUTTON_SELECTOR = "button[type='submit']"
IMAGE_TAB_SELECTOR = "button[data-testid='image-post-tab']"
FILE_INPUT_SELECTOR = "input[type='file']"


async def _wait_and_click(page: Page, selector: str, *, timeout_ms: int = 15000) -> None:
    await page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
    await page.click(selector)


async def _wait_and_fill(page: Page, selector: str, text: str, *, timeout_ms: int = 15000) -> None:
    await page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
    await page.fill(selector, text)


async def _select_community(page: Page, subreddit: str, *, timeout_ms: int = 15000) -> None:
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


async def _upload_media(page: Page, file_path: str | Path, *, timeout_ms: int = 30000) -> None:
    resolved = Path(file_path).expanduser().resolve()
    tab = page.locator("button[role='tab']:has-text('Images')")
    try:
        await tab.wait_for(state="visible", timeout=5000)
        await tab.click()
    except Exception:
        pass
    file_input = page.locator(FILE_INPUT_SELECTOR)
    await file_input.set_input_files(str(resolved))
    await page.wait_for_timeout(3000)


async def _post_reddit_cookie(
    account_file: str | Path,
    subreddit: str,
    title: str,
    body_text: str = "",
    media_path: str | Path | None = None,
    *,
    headless: bool = True,
) -> str:
    resolved_account = Path(account_file).expanduser().resolve()
    async with async_playwright() as p:
        launch_options = get_browser_options()
        launch_options["headless"] = headless if LOCAL_CHROME_HEADLESS else False
        if REDDIT_PROXY:
            launch_options["proxy"] = {"server": REDDIT_PROXY}
        browser = await p.chromium.launch(**launch_options)
        context = await browser.new_context(
            storage_state=str(resolved_account),
            viewport={"width": 1280, "height": 900},
        )
        await set_init_script(context)
        page = await context.new_page()

        try:
            await page.goto(REDDIT_SUBMIT_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            # Check for Reddit's IP block
            body_text_content = await page.evaluate("document.body.innerText")
            if "blocked by network security" in body_text_content.lower():
                raise RuntimeError(
                    "Reddit blocked this IP address. Configure REDDIT_PROXY in conf.py "
                    "with a residential proxy or SSH tunnel to your home machine. "
                    f"Current page: {page.url}"
                )

            if "/login" in page.url:
                await page.goto(REDDIT_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)

            if "/login" in page.url:
                raise RuntimeError(
                    "Reddit login page detected — cookie may be expired. "
                    "Re-login via the account management page to refresh the storage state."
                )

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