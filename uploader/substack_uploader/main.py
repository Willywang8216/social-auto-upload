# -*- coding: utf-8 -*-
"""Substack uploader (browser-based).

Each Substack writer has their own publication at ``<subdomain>.substack.com``.
A login session is shared across the substack.com domain, so one cookie file is
enough — but we still need the subdomain to know where to post.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from patchright.async_api import Page, Playwright, async_playwright

from conf import DEBUG_MODE, LOCAL_CHROME_HEADLESS
from uploader.base_post import BasePostUploader
from utils.base_social_media import set_init_script
from utils.log import substack_logger

SUBSTACK_LOGIN_URL = "https://substack.com/sign-in"
SUBSTACK_HOME_URL = "https://substack.com/"

SUBSTACK_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
SUBSTACK_PUBLISH_STRATEGY_SCHEDULED = "scheduled"
SUBSTACK_PUBLISH_STRATEGY_DRAFT = "draft"
SUBSTACK_PUBLISH_STRATEGIES = {
    SUBSTACK_PUBLISH_STRATEGY_IMMEDIATE,
    SUBSTACK_PUBLISH_STRATEGY_SCHEDULED,
    SUBSTACK_PUBLISH_STRATEGY_DRAFT,
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


def normalise_publication(publication: str) -> str:
    """Accept either ``acme`` or ``https://acme.substack.com`` and return the
    bare subdomain. Custom domains are returned unchanged."""

    if not publication:
        raise ValueError("Substack publication subdomain is required")
    publication = publication.strip()
    if "://" in publication:
        host = urlparse(publication).hostname or ""
        return host
    if "." in publication:
        return publication
    return f"{publication}.substack.com"


def publish_url(publication: str) -> str:
    return f"https://{normalise_publication(publication)}/publish/post"


# --------------------------- Cookie auth ---------------------------


async def cookie_auth(account_file: str | Path) -> bool:
    account_file = str(account_file)
    if not os.path.exists(account_file):
        return False
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, channel="chrome")
        try:
            context = await browser.new_context(storage_state=account_file)
            context = await set_init_script(context)
            page = await context.new_page()
            await page.goto(SUBSTACK_HOME_URL, wait_until="domcontentloaded")
            try:
                await page.wait_for_selector(
                    'a[href="/account"], button[aria-label*="user"], img[alt*="avatar"]',
                    timeout=8000,
                )
                return True
            except Exception:
                return False
        finally:
            await browser.close()


async def substack_cookie_gen(
    account_file: str | Path,
    *,
    qrcode_callback=None,
    headless: bool = False,
    poll_interval: int = 3,
    max_checks: int = 120,
) -> dict:
    account_file = str(account_file)
    Path(account_file).parent.mkdir(parents=True, exist_ok=True)
    result = _build_login_result(False, "failed", "Substack login failed", account_file)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless, channel="chrome")
        context = await browser.new_context()
        context = await set_init_script(context)
        try:
            page = await context.new_page()
            await page.goto(SUBSTACK_LOGIN_URL, wait_until="domcontentloaded")
            substack_logger.info(_msg("🔑", f"Open this URL to sign in: {SUBSTACK_LOGIN_URL}"))
            await _emit_callback(
                qrcode_callback,
                {"login_url": SUBSTACK_LOGIN_URL, "headless": headless},
            )

            for _ in range(max_checks):
                if "sign-in" not in page.url and "/login" not in page.url:
                    break
                await asyncio.sleep(poll_interval)
            else:
                return _build_login_result(
                    False, "timeout", "Timed out waiting for Substack login", account_file, page.url
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
                    True, "success", "Substack login successful", account_file, page.url
                )
        except Exception as exc:
            result = _build_login_result(
                False, "failed", str(exc), account_file, current_url=page.url if "page" in locals() else ""
            )
        finally:
            await context.close()
            await browser.close()
    return result


async def substack_setup(
    account_file: str | Path,
    *,
    handle: bool = False,
    return_detail: bool = False,
    qrcode_callback=None,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    account_file = str(account_file)
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            res = _build_login_result(False, "cookie_invalid", "Cookie missing or expired", account_file)
            return res if return_detail else False
        substack_logger.info(_msg("🥹", "Substack cookie expired — opening browser to log in"))
        res = await substack_cookie_gen(account_file, qrcode_callback=qrcode_callback, headless=headless)
        return res if return_detail else res["success"]

    res = _build_login_result(True, "cookie_valid", "Cookie valid", account_file)
    return res if return_detail else True


# --------------------------- Publish flow ---------------------------


class SubstackPost(BasePostUploader):
    def __init__(
        self,
        title: str,
        body_file: str | Path,
        publication: str,
        publish_date: datetime | int,
        account_file: str | Path,
        *,
        subtitle: str = "",
        tags: list[str] | None = None,
        publish_strategy: str = SUBSTACK_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ) -> None:
        self.title = title
        self.body_file = body_file
        self.publication = publication
        self.subtitle = subtitle or ""
        self.tags = tags or []
        self.publish_date = publish_date
        self.account_file = str(account_file)
        self.publish_strategy = publish_strategy
        self.debug = debug
        self.headless = headless

    async def validate(self) -> None:
        if not os.path.exists(self.account_file):
            raise RuntimeError(
                f"Substack cookie missing: {self.account_file}. Run `sau substack login` first."
            )
        if self.publish_strategy not in SUBSTACK_PUBLISH_STRATEGIES:
            raise ValueError(
                f"Unsupported Substack publish strategy: {self.publish_strategy!r}. "
                f"Use one of {sorted(SUBSTACK_PUBLISH_STRATEGIES)}."
            )
        if not await cookie_auth(self.account_file):
            raise RuntimeError(
                f"Substack cookie invalid: {self.account_file}. Run `sau substack login` first."
            )
        self.publication = normalise_publication(self.publication)
        self.title = self.validate_title(self.title)
        self.tags = self.validate_tags(self.tags)
        self.body_file = self.validate_body_file(self.body_file)
        if self.publish_strategy == SUBSTACK_PUBLISH_STRATEGY_SCHEDULED:
            self.publish_date = self.validate_publish_date(self.publish_date)
        else:
            self.publish_date = 0

    async def _fill_editor(self, page: Page, body_text: str) -> None:
        title_input = page.locator(
            'textarea[placeholder="Title"], input[placeholder="Title"], '
            'div[role="textbox"][aria-label="Title"]'
        ).first
        await title_input.wait_for(state="visible", timeout=20000)
        await title_input.click()
        await page.keyboard.type(self.title, delay=15)

        if self.subtitle:
            subtitle_input = page.locator(
                'textarea[placeholder="Add a subtitle"], '
                'input[placeholder="Add a subtitle"]'
            ).first
            if await subtitle_input.count():
                await subtitle_input.click()
                await page.keyboard.type(self.subtitle, delay=15)

        body_editor = page.locator('div[contenteditable="true"][role="textbox"]').last
        await body_editor.wait_for(state="visible", timeout=15000)
        await body_editor.click()
        await page.keyboard.type(body_text, delay=2)

    async def _open_publish_modal(self, page: Page) -> None:
        continue_button = page.get_by_role("button", name="Continue").first
        if await continue_button.count():
            await continue_button.click()
        else:
            await page.get_by_role("button", name="Publish").first.click()

    async def _confirm_send(self, page: Page) -> None:
        send_button = page.get_by_role("button", name="Send to everyone now").first
        if not await send_button.count():
            send_button = page.get_by_role("button", name="Send now").first
        await send_button.wait_for(state="visible", timeout=15000)
        await send_button.click()
        for _ in range(30):
            if "/p/" in page.url:
                substack_logger.info(_msg("🎉", f"Published: {page.url}"))
                return
            await asyncio.sleep(1)
        substack_logger.warning(_msg("⚠️", f"Send clicked but URL did not change: {page.url}"))

    async def _schedule(self, page: Page) -> None:
        if not isinstance(self.publish_date, datetime):
            raise RuntimeError("Schedule requested but publish_date is not a datetime")
        schedule_tab = page.get_by_role("button", name="Schedule").first
        await schedule_tab.wait_for(state="visible", timeout=10000)
        await schedule_tab.click()

        date_input = page.locator('input[type="date"]').first
        time_input = page.locator('input[type="time"]').first
        await date_input.wait_for(state="visible", timeout=10000)
        await date_input.fill(self.publish_date.strftime("%Y-%m-%d"))
        await time_input.fill(self.publish_date.strftime("%H:%M"))

        confirm = page.get_by_role("button", name="Schedule post").first
        await confirm.wait_for(state="visible", timeout=10000)
        await confirm.click()
        substack_logger.info(_msg("⏰", f"Scheduled for {self.publish_date.isoformat()}"))

    async def _save_draft(self, page: Page) -> None:
        for _ in range(20):
            indicator = page.get_by_text("Saved", exact=False).first
            if await indicator.count():
                substack_logger.info(_msg("💾", "Draft saved on Substack"))
                return
            await asyncio.sleep(1)
        substack_logger.warning(_msg("⚠️", "Did not see 'Saved' indicator on Substack"))

    async def _run(self, playwright: Playwright) -> None:
        await self.validate()
        body_text = self.read_body(self.body_file)

        browser = await playwright.chromium.launch(headless=self.headless, channel="chrome")
        context = await browser.new_context(storage_state=self.account_file)
        context = await set_init_script(context)
        try:
            page = await context.new_page()
            await page.goto(publish_url(self.publication), wait_until="domcontentloaded")
            substack_logger.info(
                _msg("✍️", f"Composing Substack post on {self.publication}: {self.title}")
            )
            await self._fill_editor(page, body_text)

            if self.publish_strategy == SUBSTACK_PUBLISH_STRATEGY_DRAFT:
                await self._save_draft(page)
            else:
                await self._open_publish_modal(page)
                if self.publish_strategy == SUBSTACK_PUBLISH_STRATEGY_SCHEDULED:
                    await self._schedule(page)
                else:
                    await self._confirm_send(page)

            await context.storage_state(path=self.account_file)
        finally:
            await context.close()
            await browser.close()

    async def publish(self) -> None:
        async with async_playwright() as playwright:
            await self._run(playwright)

    async def main(self) -> None:
        await self.publish()
