"""Reddit publishing via API (token_v2 Bearer auth) with proxy support.

Falls back to browser automation if API fails.
Supports multiple proxies with automatic failover.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

import requests
from patchright.async_api import async_playwright

from utils.conf_defaults import DEBUG_MODE, LOCAL_CHROME_HEADLESS, REDDIT_PROXY
from utils.browser_hook import get_browser_options
from utils.base_social_media import set_init_script

log = logging.getLogger(__name__)

REDDIT_API_BASE = "https://oauth.reddit.com"
REDDIT_SUBMIT_URL = "https://www.reddit.com/submit"
REDDIT_LOGIN_URL = "https://www.reddit.com/login"

TITLE_SELECTOR = "textarea[placeholder='Title']"
BODY_SELECTOR = "div[contenteditable='true']"
COMMUNITY_SELECTOR = "input[placeholder='Choose a community']"
SUBMIT_BUTTON_SELECTOR = "button[type='submit']"
IMAGE_TAB_SELECTOR = "button[data-testid='image-post-tab']"
FILE_INPUT_SELECTOR = "input[type='file']"


def _normalize_proxy(proxy: str) -> str:
    """Normalize proxy URL for requests library."""
    if proxy.startswith("socks://"):
        return proxy.replace("socks://", "socks5://", 1)
    return proxy


def _get_proxy_list() -> list[str]:
    """Get list of proxies from config."""
    if not REDDIT_PROXY:
        return []
    if isinstance(REDDIT_PROXY, str):
        return [_normalize_proxy(REDDIT_PROXY)]
    if isinstance(REDDIT_PROXY, (list, tuple)):
        return [_normalize_proxy(p) for p in REDDIT_PROXY if p]
    return []


def _get_proxies_dict(proxy_url: str | None) -> dict:
    """Convert proxy URL to requests proxies dict."""
    if not proxy_url:
        return {}
    return {"http": proxy_url, "https": proxy_url}


def _extract_token_v2(cookie_file: str) -> str | None:
    """Extract token_v2 from Playwright storage_state cookie file."""
    try:
        with open(cookie_file, "r") as f:
            data = json.load(f)
        cookies = data.get("cookies", [])
        for c in cookies:
            if c.get("name") == "token_v2":
                return c.get("value")
    except Exception as e:
        log.warning("Failed to read cookie file: %s", e)
    return None


def _make_headers(token_v2: str) -> dict:
    """Make API headers with Bearer token."""
    return {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Authorization": f"Bearer {token_v2}",
    }


def _upload_image_to_reddit(
    image_path: str,
    token_v2: str,
    proxy: str | None = None,
) -> str | None:
    """Upload image to Reddit and return the media URL."""
    headers = _make_headers(token_v2)
    proxies = _get_proxies_dict(proxy)

    with open(image_path, "rb") as f:
        files = {"file": f}
        r = requests.post(
            "https://oauth.reddit.com/api/media/asset",
            files=files,
            headers=headers,
            proxies=proxies,
            timeout=60,
        )

    if r.status_code != 200:
        log.error("Image upload failed: %s %s", r.status_code, r.text[:200])
        return None

    result = r.json()
    return result.get("asset_id") or result.get("url")


def _post_via_api(
    cookie_file: str,
    subreddit: str,
    title: str,
    body_text: str = "",
    media_path: str | None = None,
    proxy: str | None = None,
) -> str:
    """Post to Reddit using the API with token_v2 authentication."""
    token_v2 = _extract_token_v2(cookie_file)
    if not token_v2:
        raise RuntimeError("No token_v2 found in cookie file. Re-login required.")

    headers = _make_headers(token_v2)
    proxies = _get_proxies_dict(proxy)

    # Determine post kind
    if media_path:
        # Upload image first
        asset_id = _upload_image_to_reddit(media_path, token_v2, proxy)
        if asset_id:
            kind = "image"
            submit_data = {
                "sr": subreddit,
                "kind": "image",
                "title": title,
                "nsfw": "true",
                "api_type": "json",
            }
            # For image posts, we need to use the upload_url
            submit_data["upload_asset"] = asset_id
        else:
            # Fallback to link post with media URL
            kind = "link"
            submit_data = {
                "sr": subreddit,
                "kind": "link",
                "title": title,
                "url": media_path,
                "nsfw": "true",
                "api_type": "json",
            }
    elif body_text:
        kind = "self"
        submit_data = {
            "sr": subreddit,
            "kind": "self",
            "title": title,
            "text": body_text,
            "nsfw": "true",
            "api_type": "json",
        }
    else:
        # Link post with placeholder URL
        kind = "link"
        submit_data = {
            "sr": subreddit,
            "kind": "link",
            "title": title,
            "url": "https://www.reddit.com",
            "nsfw": "true",
            "api_type": "json",
        }

    # Submit the post
    r = requests.post(
        f"{REDDIT_API_BASE}/api/submit",
        data=submit_data,
        headers=headers,
        proxies=proxies,
        timeout=30,
    )

    if r.status_code != 200:
        raise RuntimeError(f"Reddit API error: {r.status_code} {r.text[:200]}")

    result = r.json()
    errors = result.get("json", {}).get("errors", [])
    if errors:
        error_msg = errors[0][1] if len(errors[0]) > 1 else str(errors[0])
        raise RuntimeError(f"Reddit API error: {error_msg}")

    data = result.get("json", {}).get("data", {})
    post_url = data.get("url", "")
    if not post_url:
        post_id = data.get("id", "")
        post_url = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/"

    log.info("Posted to r/%s: %s", subreddit, post_url)
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
    """Post to Reddit. Tries API first, falls back to browser automation."""
    resolved_account = str(Path(account_file).expanduser().resolve())
    proxy_list = _get_proxy_list()
    proxies_to_try = proxy_list + [None]

    last_error = None

    for proxy in proxies_to_try:
        try:
            result = _post_via_api(
                resolved_account, subreddit, title,
                body_text, str(media_path) if media_path else None,
                proxy,
            )
            return result
        except Exception as e:
            log.warning("API attempt failed (proxy=%s): %s", proxy or "direct", e)
            last_error = e
            continue

    # All API attempts failed - try browser automation as fallback
    log.info("API failed, trying browser automation...")
    try:
        return await _post_via_browser(
            resolved_account, subreddit, title,
            body_text, media_path, headless,
        )
    except Exception as e:
        log.error("Browser automation also failed: %s", e)
        raise last_error or e


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


async def _post_via_browser(
    account_file: str,
    subreddit: str,
    title: str,
    body_text: str,
    media_path: str | Path | None,
    headless: bool,
) -> str:
    """Fallback: post via browser automation."""
    from patchright.async_api import async_playwright
    from utils.browser_hook import get_browser_options
    from utils.base_social_media import set_init_script

    REDDIT_SUBMIT_URL = "https://www.reddit.com/submit"
    TITLE_SELECTOR = "textarea[placeholder='Title']"
    BODY_SELECTOR = "div[contenteditable='true']"
    COMMUNITY_SELECTOR = "input[placeholder='Choose a community']"
    SUBMIT_BUTTON_SELECTOR = "button[type='submit']"

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

            # Check for IP block
            body = await page.evaluate("document.body.innerText")
            if "blocked by network security" in body.lower():
                raise RuntimeError("Reddit blocked this IP address")

            if "/login" in page.url:
                raise RuntimeError("Not logged in - re-login required")

            # Select community
            inp = page.locator(COMMUNITY_SELECTOR)
            await inp.wait_for(state="visible", timeout=15000)
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

            # Fill title
            await page.wait_for_selector(TITLE_SELECTOR, state="visible", timeout=10000)
            await page.fill(TITLE_SELECTOR, title)

            # Fill body if provided
            if body_text:
                body_el = page.locator(BODY_SELECTOR)
                if await body_el.count() > 0:
                    await body_el.first.fill(body_text)

            # Upload media if provided
            if media_path:
                from utils.base_social_media import set_init_script
                resolved = Path(media_path).expanduser().resolve()
                tab = page.locator("button[role='tab']:has-text('Images')")
                try:
                    await tab.wait_for(state="visible", timeout=5000)
                    await tab.click()
                except Exception:
                    pass
                file_input = page.locator("input[type='file']")
                await file_input.set_input_files(str(resolved))
                await page.wait_for_timeout(3000)

            # Submit
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
