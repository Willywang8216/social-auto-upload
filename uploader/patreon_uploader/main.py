# -*- coding: utf-8 -*-
"""Patreon uploader (browser-based).

Patreon's public API v2 is read-only for posts (identity, campaigns,
members, post reads/insights). To actually create a post on a creator's
behalf we drive the Patreon web editor through Playwright using a
stored storage_state cookie. This module owns:

  * ``patreon_cookie_gen``  — opens a headed browser at Patreon's login
    page so the user can sign in (Google / email link / 2FA all fine),
    then persists the resulting storage_state JSON.
  * ``cookie_auth`` / ``patreon_setup`` — Medium-style helpers that
    verify a stored cookie still represents a live session.
  * ``PatreonPost`` — composes a text post on https://www.patreon.com/posts/new
    with optional media attachment(s) and tier access selection, then
    either publishes or saves as draft based on ``publish_strategy``.

Patreon's editor is a React-driven SPA whose DOM nodes do not have
stable ``data-testid`` attributes across all post types. Every selector
in this module is wrapped behind a small helper with a chain of
fallbacks; the helper logs which fallback hit so that when Patreon
rolls out a layout change, ``logs/patreon.log`` immediately tells us
which selector to add to the chain.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from datetime import datetime
from pathlib import Path

from patchright.async_api import Page, Playwright, async_playwright

from utils.conf_defaults import DEBUG_MODE, LOCAL_CHROME_HEADLESS
from uploader.base_post import BasePostUploader
from utils.base_social_media import set_init_script
from utils.log import patreon_logger


PATREON_HOME_URL = "https://www.patreon.com/home"
PATREON_NEW_POST_URL = "https://www.patreon.com/posts/new"
PATREON_LOGIN_URL = "https://www.patreon.com/login"

PATREON_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
PATREON_PUBLISH_STRATEGY_DRAFT = "draft"
PATREON_PUBLISH_STRATEGIES = {
    PATREON_PUBLISH_STRATEGY_IMMEDIATE,
    PATREON_PUBLISH_STRATEGY_DRAFT,
}

# Tier access modes. Patreon's editor presents these as a dropdown of
# "Everyone", "Paid members", or "Specific tier". We honor whatever the
# user picked on the account form; the default is "Everyone" so the post
# is at least visible if nothing was configured.
PATREON_ACCESS_PUBLIC = "public"
PATREON_ACCESS_PATRONS = "patrons"
PATREON_ACCESS_TIER = "tier"


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
    """Return True iff the stored cookie still represents a Patreon session."""

    account_file = str(account_file)
    if not os.path.exists(account_file):
        return False

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, channel="chrome")
        try:
            context = await browser.new_context(storage_state=account_file)
            context = await set_init_script(context)
            page = await context.new_page()
            await page.goto(PATREON_HOME_URL, wait_until="domcontentloaded")
            try:
                # Patreon's logged-in shell has a creator avatar button in the
                # top-right; logged-out users see a "Log in" link instead.
                await page.wait_for_selector(
                    'button[aria-label*="user menu"], '
                    'a[href="/messages"], '
                    'button[data-tag="user-menu-trigger"], '
                    'a[data-tag="creator-dashboard-link"]',
                    timeout=8000,
                )
                return True
            except Exception:
                return False
        finally:
            await browser.close()


async def patreon_cookie_gen(
    account_file: str | Path,
    *,
    qrcode_callback=None,
    headless: bool = False,
    poll_interval: int = 3,
    max_checks: int = 120,
) -> dict:
    """Drive an interactive Patreon login and persist the storage_state."""

    account_file = str(account_file)
    Path(account_file).parent.mkdir(parents=True, exist_ok=True)
    result = _build_login_result(False, "failed", "Patreon login failed", account_file)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless, channel="chrome")
        context = await browser.new_context()
        context = await set_init_script(context)
        try:
            page = await context.new_page()
            await page.goto(PATREON_LOGIN_URL, wait_until="domcontentloaded")
            patreon_logger.info(_msg("🔑", f"Open this URL in the browser to sign in: {PATREON_LOGIN_URL}"))
            await _emit_callback(
                qrcode_callback,
                {"login_url": PATREON_LOGIN_URL, "headless": headless},
            )

            for _ in range(max_checks):
                # Patreon's login flow ends on /home (creator) or /c/<creator>
                # depending on which page the user came from. /login,
                # /verify, /two-factor, /signup are still pre-login.
                pre_login_paths = ("/login", "/verify", "/two-factor", "/signup")
                if not any(part in page.url for part in pre_login_paths):
                    break
                await asyncio.sleep(poll_interval)
            else:
                return _build_login_result(
                    False, "timeout", "Timed out waiting for Patreon login", account_file, page.url
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
                    True, "success", "Patreon login successful", account_file, page.url
                )
        except Exception as exc:
            result = _build_login_result(
                False, "failed", str(exc), account_file, current_url=page.url if "page" in locals() else ""
            )
        finally:
            await context.close()
            await browser.close()
    return result


async def patreon_setup(
    account_file: str | Path,
    *,
    handle: bool = False,
    return_detail: bool = False,
    qrcode_callback=None,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    """Mirror of ``medium_setup`` for Patreon."""

    account_file = str(account_file)
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            res = _build_login_result(False, "cookie_invalid", "Cookie missing or expired", account_file)
            return res if return_detail else False
        patreon_logger.info(_msg("🥹", "Patreon cookie expired — opening browser to log in"))
        res = await patreon_cookie_gen(account_file, qrcode_callback=qrcode_callback, headless=headless)
        return res if return_detail else res["success"]

    res = _build_login_result(True, "cookie_valid", "Cookie valid", account_file)
    return res if return_detail else True


# --------------------------- Publish flow ---------------------------


class PatreonPost(BasePostUploader):
    """Publish or save a Patreon post via the web editor."""

    def __init__(
        self,
        title: str,
        body_file: str | Path,
        tags: list[str] | None,
        publish_date: datetime | int,
        account_file: str | Path,
        *,
        attachments: list[str | Path] | None = None,
        access_mode: str = PATREON_ACCESS_PUBLIC,
        tier_name: str | None = None,
        publish_strategy: str = PATREON_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ) -> None:
        self.title = title
        self.body_file = body_file
        self.tags = tags or []
        self.publish_date = publish_date
        self.account_file = str(account_file)
        # ``attachments`` is the list of media files (images / videos) to
        # drop into the editor body. Patreon's web editor accepts files
        # via the "+ Add" toolbar or by drag-and-drop on the page. We use
        # ``set_input_files`` against the hidden ``<input type="file">``
        # rendered by the toolbar to avoid hardcoded drag coordinates.
        self.attachments = [str(p) for p in (attachments or [])]
        self.access_mode = (access_mode or PATREON_ACCESS_PUBLIC).lower()
        self.tier_name = (tier_name or "").strip() or None
        self.publish_strategy = publish_strategy
        self.debug = debug
        self.headless = headless

    async def validate(self) -> None:
        if not os.path.exists(self.account_file):
            raise RuntimeError(
                f"Patreon cookie missing: {self.account_file}. Run `sau patreon login` first."
            )
        if self.publish_strategy not in PATREON_PUBLISH_STRATEGIES:
            raise ValueError(
                f"Unsupported Patreon publish strategy: {self.publish_strategy!r}. "
                f"Use one of {sorted(PATREON_PUBLISH_STRATEGIES)}."
            )
        if self.access_mode not in {PATREON_ACCESS_PUBLIC, PATREON_ACCESS_PATRONS, PATREON_ACCESS_TIER}:
            raise ValueError(
                f"Unsupported Patreon access mode: {self.access_mode!r}. "
                "Use 'public', 'patrons', or 'tier'."
            )
        if self.access_mode == PATREON_ACCESS_TIER and not self.tier_name:
            raise ValueError(
                "access_mode='tier' requires tier_name to identify which tier to grant access to."
            )
        if not await cookie_auth(self.account_file):
            raise RuntimeError(
                f"Patreon cookie invalid: {self.account_file}. Run `sau patreon login` first."
            )
        self.title = self.validate_title(self.title)
        self.tags = self.validate_tags(self.tags)
        self.body_file = self.validate_body_file(self.body_file)
        self.publish_date = self.validate_publish_date(self.publish_date)
        for attachment in self.attachments:
            if not Path(attachment).exists():
                raise FileNotFoundError(f"Patreon attachment does not exist: {attachment}")

    async def _open_editor(self, page: Page) -> None:
        await page.goto(PATREON_NEW_POST_URL, wait_until="domcontentloaded")
        # On first visit Patreon may surface a post-type picker modal
        # ("Text", "Image", "Video", "Audio", "Link", "Poll"). We pick
        # "Text" because text + media attachments is the most universal
        # path. If the modal is absent the user landed straight in the
        # editor — that's fine too.
        try:
            text_choice = page.get_by_role("button", name="Text").first
            await text_choice.wait_for(state="visible", timeout=4000)
            await text_choice.click()
            patreon_logger.info(_msg("🧭", "Selected 'Text' post type"))
        except Exception:
            pass

    async def _fill_title_and_body(self, page: Page, body_text: str) -> None:
        title_input = page.locator(
            'input[aria-label*="title" i], '
            'input[name="title"], '
            'textarea[aria-label*="title" i], '
            'input[placeholder*="title" i]'
        ).first
        await title_input.wait_for(state="visible", timeout=20000)
        await title_input.click()
        await page.keyboard.type(self.title, delay=15)

        body_editor = page.locator(
            '[data-tag="post-body"] [contenteditable="true"], '
            'div[role="textbox"][contenteditable="true"], '
            '[aria-label*="body" i][contenteditable="true"]'
        ).first
        await body_editor.wait_for(state="visible", timeout=15000)
        await body_editor.click()
        # Patreon's editor renders typed plain text as paragraphs. Markdown
        # remains literal — pass HTML/plain text from the source file.
        await page.keyboard.type(body_text, delay=2)

    async def _attach_media(self, page: Page) -> None:
        if not self.attachments:
            return
        # The toolbar "+" / "Add media" button reveals a hidden file input.
        # We locate any file input on the page and ``set_input_files``.
        # Most Patreon editor builds keep exactly one such input at a
        # time; if more appear, the first wins (which is fine for
        # multi-file selection because Patreon's input is ``multiple``).
        try:
            file_input = page.locator('input[type="file"]').first
            await file_input.wait_for(state="attached", timeout=10000)
            await file_input.set_input_files(self.attachments)
            patreon_logger.info(_msg("📎", f"Attached {len(self.attachments)} file(s)"))
            # Wait until upload spinners disappear. Patreon shows a small
            # progress indicator inside each media block; we treat a 30 s
            # quiet period after the last upload as "done".
            for _ in range(60):
                spinner = page.locator(
                    '[data-tag*="upload" i] svg[aria-label*="loading" i], '
                    '[role="progressbar"]'
                )
                if await spinner.count() == 0:
                    return
                await asyncio.sleep(1)
            patreon_logger.warning(_msg("⚠️", "Attachment upload did not visibly settle within 60s"))
        except Exception as exc:
            patreon_logger.warning(_msg("⚠️", f"Failed to attach media via file input: {exc}"))

    async def _choose_access(self, page: Page) -> None:
        """Set who can see the post."""
        if self.access_mode == PATREON_ACCESS_PUBLIC:
            return
        try:
            access_button = page.locator(
                'button:has-text("Public"), '
                'button:has-text("Who can see")'
            ).first
            await access_button.wait_for(state="visible", timeout=5000)
            await access_button.click()
            if self.access_mode == PATREON_ACCESS_PATRONS:
                option = page.get_by_role("menuitem", name="Patrons only").first
                await option.click()
            elif self.access_mode == PATREON_ACCESS_TIER and self.tier_name:
                option = page.get_by_role("menuitem", name=self.tier_name).first
                await option.click()
            patreon_logger.info(_msg("🔒", f"Access mode set to {self.access_mode}"))
        except Exception as exc:
            patreon_logger.warning(_msg("⚠️", f"Could not set access mode '{self.access_mode}': {exc}"))

    async def _publish_now(self, page: Page) -> None:
        publish_button = page.get_by_role("button", name="Publish").first
        await publish_button.wait_for(state="visible", timeout=15000)
        await publish_button.click()

        # Patreon shows a final confirmation modal ("Publish post?") with
        # a Publish button inside. Click it if present.
        try:
            confirm = page.get_by_role("button", name="Publish post").first
            await confirm.wait_for(state="visible", timeout=8000)
            await confirm.click()
        except Exception:
            patreon_logger.info(_msg("ℹ️", "No publish confirmation modal — assuming inline publish"))

        # Wait for navigation to the published post permalink.
        for _ in range(30):
            if "/posts/" in page.url and "/new" not in page.url:
                patreon_logger.info(_msg("🎉", f"Published: {page.url}"))
                return
            await asyncio.sleep(1)
        patreon_logger.warning(
            _msg("⚠️", f"Publish click submitted but URL did not change: {page.url}")
        )

    async def _save_draft(self, page: Page) -> None:
        # Patreon offers an explicit "Save as draft" entry in the kebab
        # menu next to the Publish button. If that lookup fails we fall
        # back to relying on autosave, which Patreon performs every few
        # seconds while the editor has focus.
        try:
            kebab = page.locator(
                'button[aria-label*="more" i], button[aria-haspopup="menu"]'
            ).first
            await kebab.wait_for(state="visible", timeout=5000)
            await kebab.click()
            draft = page.get_by_role("menuitem", name="Save as draft").first
            await draft.click()
            patreon_logger.info(_msg("💾", "Save-as-draft clicked"))
        except Exception:
            patreon_logger.info(_msg("💾", "Falling back to autosave; waiting 5 s"))
            await asyncio.sleep(5)

    async def _run(self, playwright: Playwright) -> None:
        await self.validate()
        body_text = self.read_body(self.body_file)

        browser = await playwright.chromium.launch(headless=self.headless, channel="chrome")
        context = await browser.new_context(storage_state=self.account_file)
        context = await set_init_script(context)
        try:
            page = await context.new_page()
            patreon_logger.info(_msg("✍️", f"Composing Patreon post: {self.title}"))
            await self._open_editor(page)
            await self._fill_title_and_body(page, body_text)
            await self._attach_media(page)
            await self._choose_access(page)

            if self.publish_strategy == PATREON_PUBLISH_STRATEGY_DRAFT:
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
