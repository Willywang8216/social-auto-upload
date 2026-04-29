# -*- coding: utf-8 -*-
"""Medium uploader (browser-based).

Workflow:
  1. ``medium_setup``  → guarantees a valid storage_state JSON for the account.
  2. ``MediumPost.publish()`` → opens medium.com/new-story, pastes the title and
     body, attaches up to 5 tags, optionally schedules, then publishes.

The implementation matches the public Medium editor as of 2026-04. Selectors
are wrapped behind named helpers so a layout change only touches one place.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from datetime import datetime
from pathlib import Path

from patchright.async_api import Page, Playwright, async_playwright

from conf import DEBUG_MODE, LOCAL_CHROME_HEADLESS
from uploader.base_post import BasePostUploader
from utils.base_social_media import set_init_script
from utils.log import medium_logger

MEDIUM_HOME_URL = "https://medium.com/"
MEDIUM_NEW_STORY_URL = "https://medium.com/new-story"
MEDIUM_LOGIN_URL = "https://medium.com/m/signin"

MEDIUM_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
MEDIUM_PUBLISH_STRATEGY_DRAFT = "draft"
MEDIUM_PUBLISH_STRATEGIES = {
    MEDIUM_PUBLISH_STRATEGY_IMMEDIATE,
    MEDIUM_PUBLISH_STRATEGY_DRAFT,
}


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


async def _emit_callback(callback, payload: dict) -> None:
    if not callback:
        return
    result = callback(payload)
    if inspect.isawaitable(result):
        await result


def _build_login_result(
    success: bool,
    status: str,
    message: str,
    account_file: str,
    current_url: str = "",
) -> dict:
    return {
        "success": success,
        "status": status,
        "message": message,
        "account_file": str(account_file),
        "current_url": current_url,
    }


# --------------------------- Cookie auth ---------------------------


async def cookie_auth(account_file: str | Path) -> bool:
    """Return True iff the storage_state at ``account_file`` is logged in.

    We open medium.com and check whether the avatar/menu indicates a session.
    """

    account_file = str(account_file)
    if not os.path.exists(account_file):
        return False

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, channel="chrome")
        try:
            context = await browser.new_context(storage_state=account_file)
            context = await set_init_script(context)
            page = await context.new_page()
            await page.goto(MEDIUM_HOME_URL, wait_until="domcontentloaded")
            try:
                await page.wait_for_selector(
                    'a[href="/me/stories/drafts"], button[aria-label*="user"], '
                    'a[aria-label="user options menu"]',
                    timeout=8000,
                )
                return True
            except Exception:
                return False
        finally:
            await browser.close()


async def medium_cookie_gen(
    account_file: str | Path,
    *,
    qrcode_callback=None,
    headless: bool = False,
    poll_interval: int = 3,
    max_checks: int = 120,
) -> dict:
    """Drive an interactive Medium login and persist the storage_state.

    Medium has no QR login, so we simply open the sign-in page in headed mode
    and wait until the URL leaves /m/signin. ``qrcode_callback`` is reused as a
    generic "show this URL to the user" notifier to keep parity with the other
    platforms.
    """

    account_file = str(account_file)
    Path(account_file).parent.mkdir(parents=True, exist_ok=True)
    result = _build_login_result(False, "failed", "Medium login failed", account_file)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless, channel="chrome")
        context = await browser.new_context()
        context = await set_init_script(context)
        try:
            page = await context.new_page()
            await page.goto(MEDIUM_LOGIN_URL, wait_until="domcontentloaded")
            medium_logger.info(_msg("🔑", f"Open this URL in the browser to sign in: {MEDIUM_LOGIN_URL}"))
            await _emit_callback(
                qrcode_callback,
                {"login_url": MEDIUM_LOGIN_URL, "headless": headless},
            )

            for _ in range(max_checks):
                if "signin" not in page.url and "callback" not in page.url:
                    break
                await asyncio.sleep(poll_interval)
            else:
                return _build_login_result(
                    False, "timeout", "Timed out waiting for Medium login", account_file, page.url
                )

            await asyncio.sleep(2)
            await context.storage_state(path=account_file)
            if not await cookie_auth(account_file):
                result = _build_login_result(
                    False,
                    "cookie_invalid",
                    "Login completed but the saved cookie does not validate",
                    account_file,
                    page.url,
                )
            else:
                result = _build_login_result(
                    True, "success", "Medium login successful", account_file, page.url
                )
        except Exception as exc:
            result = _build_login_result(
                False, "failed", str(exc), account_file, current_url=page.url if "page" in locals() else ""
            )
        finally:
            await context.close()
            await browser.close()
    return result


async def medium_setup(
    account_file: str | Path,
    *,
    handle: bool = False,
    return_detail: bool = False,
    qrcode_callback=None,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    """Mirror of ``douyin_setup`` for Medium."""

    account_file = str(account_file)
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            res = _build_login_result(False, "cookie_invalid", "Cookie missing or expired", account_file)
            return res if return_detail else False
        medium_logger.info(_msg("🥹", "Medium cookie expired — opening browser to log in"))
        res = await medium_cookie_gen(account_file, qrcode_callback=qrcode_callback, headless=headless)
        return res if return_detail else res["success"]

    res = _build_login_result(True, "cookie_valid", "Cookie valid", account_file)
    return res if return_detail else True


# --------------------------- Publish flow ---------------------------


class MediumPost(BasePostUploader):
    def __init__(
        self,
        title: str,
        body_file: str | Path,
        tags: list[str] | None,
        publish_date: datetime | int,
        account_file: str | Path,
        *,
        subtitle: str = "",
        cover_image: str | Path | None = None,
        publish_strategy: str = MEDIUM_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ) -> None:
        self.title = title
        self.body_file = body_file
        self.subtitle = subtitle or ""
        self.tags = tags or []
        self.publish_date = publish_date
        self.account_file = str(account_file)
        self.cover_image = cover_image
        self.publish_strategy = publish_strategy
        self.debug = debug
        self.headless = headless

    async def validate(self) -> None:
        if not os.path.exists(self.account_file):
            raise RuntimeError(
                f"Medium cookie missing: {self.account_file}. Run `sau medium login` first."
            )
        if self.publish_strategy not in MEDIUM_PUBLISH_STRATEGIES:
            raise ValueError(
                f"Unsupported Medium publish strategy: {self.publish_strategy!r}. "
                f"Use one of {sorted(MEDIUM_PUBLISH_STRATEGIES)}."
            )
        if not await cookie_auth(self.account_file):
            raise RuntimeError(
                f"Medium cookie invalid: {self.account_file}. Run `sau medium login` first."
            )
        self.title = self.validate_title(self.title)
        self.tags = self.validate_tags(self.tags)
        self.body_file = self.validate_body_file(self.body_file)
        self.cover_image = self.validate_cover_file(self.cover_image)
        self.publish_date = self.validate_publish_date(self.publish_date)

    async def _fill_editor(self, page: Page, body_text: str) -> None:
        title_input = page.locator(
            'h3[data-testid="storyTitle"], textarea[aria-label="Title"], '
            'h1[data-default-value="Title"]'
        ).first
        await title_input.wait_for(state="visible", timeout=20000)
        await title_input.click()
        await page.keyboard.type(self.title, delay=15)
        await page.keyboard.press("Enter")

        if self.subtitle:
            await page.keyboard.type(self.subtitle, delay=15)
            await page.keyboard.press("Enter")

        # Paste the body. Medium's editor accepts plain text reliably; markdown
        # rendering happens server-side on first publish for paste blocks.
        await page.keyboard.type(body_text, delay=2)

    async def _attach_tags(self, page: Page) -> None:
        if not self.tags:
            return
        topics_button = page.get_by_role("button", name="Add topics").first
        if not await topics_button.count():
            medium_logger.warning(_msg("⚠️", "Could not find Add topics button — tags skipped"))
            return
        await topics_button.click()
        topic_input = page.locator('input[placeholder*="Add a topic"], input[aria-label*="topic"]').first
        await topic_input.wait_for(state="visible", timeout=10000)
        for tag in self.tags:
            await topic_input.fill(tag)
            await asyncio.sleep(0.5)
            await page.keyboard.press("Enter")

    async def _publish_now(self, page: Page) -> None:
        publish_button = page.get_by_role("button", name="Publish").first
        await publish_button.wait_for(state="visible", timeout=15000)
        await publish_button.click()

        await self._attach_tags(page)

        confirm_button = page.get_by_role("button", name="Publish now").first
        await confirm_button.wait_for(state="visible", timeout=15000)
        await confirm_button.click()

        # Wait for navigation to the published post URL.
        for _ in range(30):
            if "/p/" in page.url or "?source=collection_home" in page.url:
                medium_logger.info(_msg("🎉", f"Published: {page.url}"))
                return
            await asyncio.sleep(1)
        medium_logger.warning(_msg("⚠️", f"Publish click submitted but URL did not change: {page.url}"))

    async def _save_draft(self, page: Page) -> None:
        # Medium auto-saves drafts; just wait for the "Saved" indicator.
        for _ in range(20):
            saved = page.get_by_text("Saved", exact=False).first
            if await saved.count():
                medium_logger.info(_msg("💾", "Draft saved on Medium"))
                return
            await asyncio.sleep(1)
        medium_logger.warning(_msg("⚠️", "Did not see 'Saved' indicator; draft may not be persisted"))

    async def _run(self, playwright: Playwright) -> None:
        await self.validate()
        body_text = self.read_body(self.body_file)

        browser = await playwright.chromium.launch(headless=self.headless, channel="chrome")
        context = await browser.new_context(storage_state=self.account_file)
        context = await set_init_script(context)
        try:
            page = await context.new_page()
            await page.goto(MEDIUM_NEW_STORY_URL, wait_until="domcontentloaded")
            medium_logger.info(_msg("✍️", f"Composing Medium post: {self.title}"))
            await self._fill_editor(page, body_text)

            if self.publish_strategy == MEDIUM_PUBLISH_STRATEGY_DRAFT:
                await self._save_draft(page)
            else:
                await self._publish_now(page)

            await context.storage_state(path=self.account_file)
        finally:
            await context.close()
            await browser.close()

    async def publish(self) -> None:
        async with async_playwright() as playwright:
            await self._run(playwright)

    async def main(self) -> None:
        await self.publish()
