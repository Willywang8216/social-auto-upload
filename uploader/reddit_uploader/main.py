"""Cookie-based Reddit publishing via Playwright (browser automation).

Route: navigate to Reddit submit page, fill in subreddit/title/media, post.
Supports multiple proxies with automatic failover via a local forwarder
(Chromium does not support SOCKS5 authentication directly).
"""
from __future__ import annotations

import asyncio
import logging
import re
import socket
import struct
import threading
from pathlib import Path

import socks as pysocks

from patchright.async_api import Page, async_playwright

from utils.conf_defaults import DEBUG_MODE, LOCAL_CHROME_HEADLESS, REDDIT_PROXY
from utils.browser_hook import get_browser_options
from utils.base_social_media import set_init_script

log = logging.getLogger(__name__)

REDDIT_SUBMIT_URL = "https://www.reddit.com/submit"
REDDIT_LOGIN_URL = "https://www.reddit.com/login"

TITLE_SELECTOR = "textarea[placeholder='Title']"
BODY_SELECTOR = "div[contenteditable='true']"
COMMUNITY_SELECTOR = "input[placeholder='Choose a community']"
SUBMIT_BUTTON_SELECTOR = "button[type='submit']"
IMAGE_TAB_SELECTOR = "button[role='tab']:has-text('Images')"
FILE_INPUT_SELECTOR = "input[type='file']"

# --- Proxy helpers ---

def _normalize_proxy(proxy: str) -> str:
    """Normalize proxy URL. Convert socks:// to socks5:// for PySocks."""
    if proxy.startswith("socks://"):
        return proxy.replace("socks://", "socks5://", 1)
    return proxy


def _get_proxy_list() -> list[str]:
    """Get list of proxies from config. Supports string or list."""
    if not REDDIT_PROXY:
        return []
    if isinstance(REDDIT_PROXY, str):
        return [_normalize_proxy(REDDIT_PROXY)]
    if isinstance(REDDIT_PROXY, (list, tuple)):
        return [_normalize_proxy(p) for p in REDDIT_PROXY if p]
    return []


def _parse_socks_url(url: str) -> dict:
    """Parse socks5://user:pass@host:port into components."""
    if not url.startswith("socks5://"):
        raise ValueError(f"Not a SOCKS5 URL: {url}")
    rest = url[len("socks5://"):]
    if "@" in rest:
        creds, hostport = rest.rsplit("@", 1)
        username, password = creds.split(":", 1)
    else:
        hostport = rest
        username = password = None
    host, port = hostport.rsplit(":", 1)
    return {"host": host, "port": int(port), "username": username, "password": password}


def _start_local_forwarder(remote_url: str, local_port: int = 0) -> int:
    """Start a local SOCKS5 forwarder that proxies through the authenticated remote.

    Returns the local port number.
    """
    cfg = _parse_socks_url(remote_url)

    def handle_client(client_sock: socket.socket):
        try:
            remote = pysocks.socksocket()
            remote.set_proxy(
                pysocks.SOCKS5,
                cfg["host"],
                cfg["port"],
                username=cfg["username"],
                password=cfg["password"],
            )
            remote.settimeout(30)

            header = client_sock.recv(2)
            if len(header) < 2:
                return
            client_sock.recv(header[1])  # consume methods
            client_sock.sendall(b"\x05\x00")  # no auth

            req = client_sock.recv(4)
            if len(req) < 4:
                return
            _, _, _, atyp = struct.unpack("BBBB", req)

            if atyp == 1:
                addr = socket.inet_ntoa(client_sock.recv(4))
            elif atyp == 3:
                length = client_sock.recv(1)[0]
                addr = client_sock.recv(length).decode()
            elif atyp == 4:
                addr = socket.inet_ntop(socket.AF_INET6, client_sock.recv(16))
            else:
                return

            port = struct.unpack("!H", client_sock.recv(2))[0]
            remote.connect((addr, port))
            client_sock.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)

            def relay(src, dst):
                try:
                    while True:
                        data = src.recv(65536)
                        if not data:
                            break
                        dst.sendall(data)
                except Exception:
                    pass
                finally:
                    try: src.close()
                    except: pass
                    try: dst.close()
                    except: pass

            threading.Thread(target=relay, args=(client_sock, remote), daemon=True).start()
            threading.Thread(target=relay, args=(remote, client_sock), daemon=True).start()
        except Exception as e:
            log.error("Forwarder error: %s", e)
            try: client_sock.close()
            except: pass

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.listen(100)

    def accept_loop():
        while True:
            try:
                client, _ = server.accept()
                threading.Thread(target=handle_client, args=(client,), daemon=True).start()
            except Exception:
                break

    threading.Thread(target=accept_loop, daemon=True).start()
    log.info("Local SOCKS5 forwarder on 127.0.0.1:%d -> %s", port, remote_url[:30])
    return port


# --- Playwright helpers ---

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
    """Check if Reddit blocked this IP."""
    try:
        body = await page.evaluate("document.body.innerText")
        return "blocked by network security" in body.lower()
    except Exception:
        return False


# --- Main entry point ---

async def _attempt_post(
    account_file: str,
    local_port: int | None,
    subreddit: str,
    title: str,
    body_text: str,
    media_path: str | Path | None,
    headless: bool,
) -> str:
    """Single attempt to post to Reddit."""
    async with async_playwright() as p:
        launch_options = get_browser_options()
        launch_options["headless"] = headless if LOCAL_CHROME_HEADLESS else False
        if local_port:
            launch_options["proxy"] = {"server": f"socks5://127.0.0.1:{local_port}"}
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


async def _post_reddit_cookie(
    account_file: str | Path,
    subreddit: str,
    title: str,
    body_text: str = "",
    media_path: str | Path | None = None,
    *,
    headless: bool = True,
) -> str:
    resolved_account = str(Path(account_file).expanduser().resolve())
    proxy_list = _get_proxy_list()

    # Try each proxy, then direct as last resort
    proxies_to_try = proxy_list + [None]
    last_error = None

    for proxy_url in proxies_to_try:
        local_port = None
        try:
            if proxy_url:
                local_port = _start_local_forwarder(proxy_url)

            result = await _attempt_post(
                resolved_account, local_port, subreddit, title,
                body_text, media_path, headless,
            )
            return result
        except RuntimeError as e:
            if "blocked by network security" in str(e).lower():
                log.warning("Proxy %s blocked by Reddit, trying next...", proxy_url or "direct")
                last_error = e
                continue
            raise
        except Exception as e:
            log.warning("Proxy %s failed: %s", proxy_url or "direct", e)
            last_error = e
            continue

    raise last_error or RuntimeError("All proxies failed for Reddit")


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
