"""Publish long-form X Articles by driving the Articles editor in a browser.

Why the browser and not the API
-------------------------------
X opened Articles to the API (``POST /2/articles/draft`` then
``/2/articles/{id}/publish``) but it needs a **user-context** OAuth token. Every
stored token for these accounts is dead (OAuth2 refresh returns
``invalid_request``, the OAuth 1.0a env pair returns 401), and the accounts are
X **Premium** — which is what gates the Articles editor. So Articles are
published the way a person does: with the account's saved browser session.

Verified flow (2026-10-14, @will_sexual):

  1. ``https://x.com/compose/articles`` -> click *Write* (``撰寫``)
  2. fill the title textarea, type the body into the DraftJS editor
  3. click *Publish* (``發佈``) -> X opens the **post composer** with the article
     attached as a card
  4. the composer's Publish button stays disabled until the post has text, so a
     caption is required; click it and the ``CreateTweet`` call creates the post

The account must have Premium; without it the editor 404s.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Sequence

from patchright.async_api import async_playwright

from utils.browser_hook import get_browser_options

ARTICLES_URL = "https://x.com/compose/articles"
COMPOSER_URL_FRAGMENT = "/compose/post"

# The UI is localised (the accounts are Chinese-locale), so match either language
# rather than assuming English.
WRITE_LABELS = ("撰寫", "Write")
PUBLISH_LABELS = ("發佈", "Publish", "Post")
TITLE_PLACEHOLDER_HINTS = ("標題", "title")

TITLE_SELECTOR = "textarea[placeholder*='標題'], textarea[placeholder*='title' i], textarea[name='文章標題']"
BODY_SELECTOR = "[data-testid='composer']"
COMPOSER_TEXT_SELECTOR = "[data-testid='tweetTextarea_0']"
COMPOSER_PUBLISH_SELECTOR = "[data-testid='tweetButton'], [data-testid='tweetButtonInline']"


class XArticleError(RuntimeError):
    """An X Article could not be published."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def parse_article(text: str) -> tuple[str, list[str]]:
    """Split raw article text into ``(title, paragraphs)``.

    A leading ``# Title`` line (or, failing that, the first non-empty line) is the
    title. Remaining blank-line-separated blocks become body paragraphs; leading
    Markdown heading markers are dropped because the editor's block type is set
    from its toolbar, not from the text.
    """
    lines = (text or "").replace("\r\n", "\n").split("\n")
    title = ""
    start = 0
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        title = re.sub(r"^#{1,6}\s*", "", line.strip()).strip()
        start = index + 1
        break
    body_lines = lines[start:]
    paragraphs: list[str] = []
    current: list[str] = []
    for line in body_lines:
        if line.strip():
            current.append(re.sub(r"^#{1,6}\s*", "", line.rstrip()))
        elif current:
            paragraphs.append("\n".join(current).strip())
            current = []
    if current:
        paragraphs.append("\n".join(current).strip())
    if not title:
        title = (paragraphs[0].split("\n")[0][:120] if paragraphs else "").strip()
    return title, [p for p in paragraphs if p]


def _first_label(page: Any, labels: Sequence[str]):
    for label in labels:
        locator = page.get_by_text(label, exact=True)
        if locator.count():
            return locator.first
    return None


class XArticlePublisher:
    """Publish one long-form Article as ``account_file``."""

    def __init__(
        self,
        account_file: str | Path,
        *,
        headless: bool = True,
        debug: bool = False,
    ) -> None:
        account_path = Path(account_file).expanduser().resolve()
        if not account_path.exists():
            raise FileNotFoundError(f"X storage_state file not found: {account_path}")
        self.account_file = str(account_path)
        self.headless = headless
        self.debug = debug

    async def _open_editor(self, page: Any) -> None:
        await page.goto(ARTICLES_URL, timeout=60000, wait_until="domcontentloaded")
        write = _first_label(page, WRITE_LABELS)
        if write is None:
            raise XArticleError(
                "the Articles editor is unavailable for this account "
                "(X Premium is required, or the session has expired)",
                retryable=False,
            )
        await write.click()
        await page.wait_for_selector(TITLE_SELECTOR, timeout=30000)
        await page.wait_for_timeout(1500)

    async def _write_article(self, page: Any, *, title: str, paragraphs: Sequence[str]) -> None:
        title_box = page.locator(TITLE_SELECTOR).first
        await title_box.click()
        await page.keyboard.insert_text(title)
        editor = page.locator(BODY_SELECTOR).first
        await editor.click()
        for index, paragraph in enumerate(paragraphs):
            await page.keyboard.insert_text(paragraph)
            if index < len(paragraphs) - 1:
                # Two Enters make a new paragraph block rather than a soft break.
                await page.keyboard.press("Enter")
                await page.keyboard.press("Enter")
            await page.wait_for_timeout(150)

    async def publish(
        self,
        *,
        title: str,
        body: str | Sequence[str],
        caption: str = "",
        timeout_ms: int = 120000,
    ) -> dict[str, Any]:
        """Publish an Article and return ``{"post_id", "post_url"}``.

        ``body`` is the article body (raw text, or pre-split paragraphs).
        ``caption`` is the post text; X keeps the composer's Publish disabled
        without it, so a non-empty caption is required.
        """
        paragraphs = [body] if isinstance(body, str) else list(body)
        paragraphs = [p.strip() for p in paragraphs if p and p.strip()]
        if not title.strip():
            raise XArticleError("an X Article needs a title", retryable=False)
        if not paragraphs:
            raise XArticleError("an X Article needs a body", retryable=False)
        caption = (caption or title).strip()[:280]

        browser_options = get_browser_options()
        browser_options["headless"] = self.headless
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**browser_options)
            context = None
            try:
                context = await browser.new_context(storage_state=self.account_file)
                page = await context.new_page()
                await self._open_editor(page)
                await self._write_article(page, title=title, paragraphs=paragraphs)

                publish = _first_label(page, PUBLISH_LABELS)
                if publish is None:
                    raise XArticleError("the Article editor has no Publish button")
                await publish.click()
                await page.wait_for_url(f"**{COMPOSER_URL_FRAGMENT}**", timeout=timeout_ms)

                text_box = page.locator(COMPOSER_TEXT_SELECTOR).first
                await text_box.wait_for(state="visible", timeout=timeout_ms)
                await text_box.click()
                await page.keyboard.insert_text(caption)

                button = page.locator(COMPOSER_PUBLISH_SELECTOR).first
                await page.wait_for_function(
                    "() => { const b = document.querySelector(\"[data-testid='tweetButton'], [data-testid='tweetButtonInline']\"); return b && !b.disabled; }",
                    timeout=timeout_ms,
                )
                async with page.expect_response(
                    lambda response: (
                        response.request.method == "POST"
                        and "CreateTweet" in response.url
                    ),
                    timeout=timeout_ms,
                ) as response_info:
                    await button.click()
                response = await response_info.value
                if not response.ok:
                    raise XArticleError(
                        f"X rejected the article post (HTTP {response.status})"
                    )
                post_id = _extract_post_id(await response.json())
                if not post_id:
                    raise XArticleError("X created the article but returned no post id")
                handle = await _current_handle(page)
                url = f"https://x.com/{handle}/status/{post_id}" if handle else ""
                return {"post_id": post_id, "post_url": url}
            finally:
                if context is not None:
                    try:
                        await context.storage_state(path=self.account_file)
                    except Exception:  # noqa: BLE001 - a cookie write is a cache update
                        pass
                    try:
                        await context.close()
                    except Exception:  # noqa: BLE001
                        pass
                await browser.close()


def _extract_post_id(payload: Any) -> str | None:
    """Pull the created post id out of the CreateTweet GraphQL response."""
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"rest_id", "id_str", "id"} and isinstance(value, (str, int)):
                    text = str(value)
                    if text.isdigit() and len(text) >= 15:
                        return text
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return None


async def _current_handle(page: Any) -> str:
    try:
        switcher = page.locator("[data-testid='SideNav_AccountSwitcher_Button']").first
        text = await switcher.inner_text(timeout=5000)
        match = re.search(r"@([A-Za-z0-9_]+)", text or "")
        if match:
            return match.group(1)
    except Exception:  # noqa: BLE001 - the handle is only used to build the URL
        pass
    return ""


def publish_article(
    account_file: str | Path,
    *,
    title: str,
    body: str | Sequence[str],
    caption: str = "",
    headless: bool = True,
) -> dict[str, Any]:
    """Blocking entry point used by the worker/CLI."""
    publisher = XArticlePublisher(account_file, headless=headless)
    return asyncio.run(publisher.publish(title=title, body=body, caption=caption))
