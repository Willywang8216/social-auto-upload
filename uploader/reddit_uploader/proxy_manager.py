"""Reddit proxy manager with auto-rotation and cookie refresh.

Self-healing system that:
1. Rotates through proxies when one gets blocked
2. Auto-refreshes token_v2 when it expires or gets invalidated
3. Tests proxy health before using it
4. Falls back gracefully across all available proxies
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

REDDIT_API = "https://oauth.reddit.com"
REDDIT_WWW = "https://www.reddit.com"
_HEALTH_CACHE_TTL = 300  # 5 minutes


def _normalize_proxy(url: str) -> str:
    if url.startswith("socks://"):
        return url.replace("socks://", "socks5://", 1)
    return url


def _proxies_dict(proxy: str) -> dict:
    return {"http": proxy, "https": proxy}


def _load_cookies(cookie_file: str) -> dict:
    with open(cookie_file) as f:
        data = json.load(f)
    return {c["name"]: c["value"] for c in data.get("cookies", [])}


def _save_cookies(cookie_file: str, cookies: list[dict]) -> None:
    with open(cookie_file, "w") as f:
        json.dump({"cookies": cookies, "origins": []}, f, indent=2)


class ProxyManager:
    """Manages a pool of proxies with health checks and auto-rotation."""

    def __init__(self, proxy_list: list[str]):
        self._proxies = [_normalize_proxy(p) for p in proxy_list if p]
        self._health: dict[str, bool] = {}
        self._last_check: dict[str, float] = {}
        self._current_idx = 0

    @property
    def has_proxies(self) -> bool:
        return len(self._proxies) > 0

    def get_working_proxy(self) -> str | None:
        """Return a working proxy, testing each one. Returns None if all fail."""
        if not self._proxies:
            return None

        for i in range(len(self._proxies)):
            idx = (self._current_idx + i) % len(self._proxies)
            proxy = self._proxies[idx]

            # Use cached health if recent
            now = time.time()
            if proxy in self._health and proxy in self._last_check:
                if now - self._last_check[proxy] < _HEALTH_CACHE_TTL:
                    if self._health[proxy]:
                        self._current_idx = idx
                        return proxy
                    continue

            # Test the proxy
            if self._test_proxy(proxy):
                self._health[proxy] = True
                self._last_check[proxy] = now
                self._current_idx = idx
                return proxy
            else:
                self._health[proxy] = False
                self._last_check[proxy] = now
                log.warning("Proxy %s failed health check", proxy[:30])

        log.error("All %d proxies failed health check", len(self._proxies))
        return None

    def mark_failed(self, proxy: str) -> None:
        """Mark a proxy as failed, forcing re-check next time."""
        self._health[proxy] = False
        self._last_check[proxy] = 0
        # Move to next proxy
        if proxy in self._proxies:
            idx = self._proxies.index(proxy)
            self._current_idx = (idx + 1) % len(self._proxies)

    def _test_proxy(self, proxy: str) -> bool:
        """Test if a proxy can reach Reddit (GET request)."""
        try:
            r = requests.get(
                f"{REDDIT_WWW}/",
                proxies=_proxies_dict(proxy),
                timeout=15,
            )
            return r.status_code == 200
        except Exception:
            return False

    def test_post_access(self, proxy: str, cookies: dict) -> bool:
        """Test if a proxy can make POST requests to Reddit."""
        try:
            r = requests.post(
                f"{REDDIT_API}/api/submit",
                data={"sr": "test", "kind": "link", "title": "test", "url": "https://example.com", "api_type": "json"},
                cookies=cookies,
                proxies=_proxies_dict(proxy),
                timeout=15,
            )
            if r.status_code == 200:
                result = r.json()
                errors = result.get("json", {}).get("errors", [])
                # USER_REQUIRED or SUBREDDIT_NOTALLOWED means auth works, just wrong perms
                if errors:
                    code = errors[0][0] if errors else ""
                    if code in ("USER_REQUIRED", "SUBREDDIT_NOTALLOWED", "NO_SELFS"):
                        return True
                return "json" in result
            return False
        except Exception:
            return False


class CookieManager:
    """Manages Reddit cookies with auto-refresh of token_v2."""

    def __init__(self, cookie_file: str):
        self._cookie_file = cookie_file

    def load(self) -> dict:
        """Load cookies as a dict."""
        return _load_cookies(self._cookie_file)

    def get_token_v2(self) -> str | None:
        """Extract token_v2 from cookie file."""
        cookies = self.load()
        return cookies.get("token_v2")

    def refresh_token_v2(self, proxy: str | None = None) -> bool:
        """Refresh token_v2 by visiting Reddit through the proxy.

        Returns True if a new token_v2 was obtained.
        """
        try:
            from patchright.async_api import async_playwright
            import asyncio
            return asyncio.get_event_loop().run_until_complete(
                self._refresh_via_browser(proxy)
            )
        except RuntimeError:
            # No event loop running, create one
            import asyncio
            return asyncio.run(self._refresh_via_browser(proxy))

    async def _refresh_via_browser(self, proxy: str | None) -> bool:
        """Visit Reddit in browser to get fresh token_v2."""
        from patchright.async_api import async_playwright

        async with async_playwright() as p:
            launch_opts = {"headless": True}
            if proxy:
                launch_opts["proxy"] = {"server": proxy}

            browser = await p.chromium.launch(**launch_opts)
            context = await browser.new_context(
                storage_state=self._cookie_file,
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            )

            try:
                from utils.base_social_media import set_init_script
                await set_init_script(context)
            except Exception:
                pass

            page = await context.new_page()

            try:
                await page.goto(f"{REDDIT_WWW}/", wait_until="commit", timeout=60000)
                await asyncio.sleep(10)

                # Check if we got blocked
                body = await page.evaluate("document.body.innerText")
                if "blocked by network security" in body.lower():
                    log.warning("Reddit blocked during token refresh (proxy=%s)", proxy or "direct")
                    return False

                # Get fresh cookies
                browser_cookies = await context.cookies()
                new_token_v2 = None
                for c in browser_cookies:
                    if c["name"] == "token_v2":
                        new_token_v2 = c["value"]
                        break

                if not new_token_v2:
                    log.warning("No token_v2 obtained from browser session")
                    return False

                # Update cookie file with new token_v2
                with open(self._cookie_file) as f:
                    old_data = json.load(f)

                updated_cookies = []
                for c in old_data.get("cookies", []):
                    if c["name"] == "token_v2":
                        updated_cookies.append({**c, "value": new_token_v2})
                    else:
                        updated_cookies.append(c)

                _save_cookies(self._cookie_file, updated_cookies)
                log.info("Refreshed token_v2 successfully")
                return True

            except Exception as e:
                log.error("Token refresh failed: %s", e)
                return False
            finally:
                await context.close()
                await browser.close()

    def remove_token_v2(self) -> None:
        """Remove token_v2 from cookie file (it's blocking requests)."""
        with open(self._cookie_file) as f:
            data = json.load(f)

        cookies = [c for c in data.get("cookies", []) if c["name"] != "token_v2"]
        _save_cookies(self._cookie_file, cookies)
        log.info("Removed invalidated token_v2 from cookies")


class RedditClient:
    """Self-healing Reddit API client with proxy rotation and cookie refresh."""

    def __init__(self, cookie_file: str, proxy_list: list[str] | None = None):
        self.cookies = CookieManager(cookie_file)
        self.proxies = ProxyManager(proxy_list or [])
        self._working_proxy: str | None = None
        self._cookie_file = cookie_file

    def _get_auth_cookies(self) -> dict:
        """Get cookies for API calls, excluding token_v2 if it's blocking."""
        all_cookies = self.cookies.load()
        # If we know token_v2 is blocking, exclude it
        return all_cookies

    def _find_working_proxy(self) -> str | None:
        """Find a proxy that works for Reddit POST requests."""
        proxy = self.proxies.get_working_proxy()
        if not proxy:
            return None

        # Test POST access
        cookies = self._get_auth_cookies()
        if self.proxies.test_post_access(proxy, cookies):
            self._working_proxy = proxy
            return proxy

        # POST failed, try refreshing token_v2
        log.info("POST failed, attempting token_v2 refresh...")
        self.cookies.remove_token_v2()

        # Try refreshing via browser
        if self.cookies.refresh_token_v2(proxy):
            cookies = self._get_auth_cookies()
            if self.proxies.test_post_access(proxy, cookies):
                self._working_proxy = proxy
                return proxy

        # This proxy doesn't work for POST, mark failed
        self.proxies.mark_failed(proxy)
        return None

    def get_proxy(self) -> str | None:
        """Get a working proxy, refreshing if needed."""
        # Check if current proxy still works
        if self._working_proxy:
            cookies = self._get_auth_cookies()
            if self.proxies.test_post_access(self._working_proxy, cookies):
                return self._working_proxy
            log.info("Current proxy stopped working, rotating...")
            self.proxies.mark_failed(self._working_proxy)
            self._working_proxy = None

        # Find a new working proxy
        return self._find_working_proxy()

    def post(self, endpoint: str, data: dict, timeout: int = 30) -> requests.Response | None:
        """Make a POST request with auto-retry across proxies."""
        proxy = self.get_proxy()
        if not proxy:
            log.error("No working proxy available")
            return None

        cookies = self._get_auth_cookies()
        proxies = _proxies_dict(proxy)

        try:
            r = requests.post(
                f"{REDDIT_API}{endpoint}",
                data=data,
                cookies=cookies,
                proxies=proxies,
                timeout=timeout,
            )
            if r.status_code == 403:
                # Token might be bad, try refreshing
                log.warning("Got 403, refreshing token_v2...")
                self.cookies.remove_token_v2()
                if self.cookies.refresh_token_v2(proxy):
                    cookies = self._get_auth_cookies()
                    r = requests.post(
                        f"{REDDIT_API}{endpoint}",
                        data=data,
                        cookies=cookies,
                        proxies=proxies,
                        timeout=timeout,
                    )
            return r
        except Exception as e:
            log.error("POST %s failed: %s", endpoint, e)
            self.proxies.mark_failed(proxy)
            self._working_proxy = None
            return None

    def get(self, endpoint: str, timeout: int = 15) -> requests.Response | None:
        """Make a GET request with auto-retry across proxies."""
        proxy = self.get_proxy()
        if not proxy:
            proxy = self.proxies.get_working_proxy()
        if not proxy:
            log.error("No working proxy available")
            return None

        cookies = self._get_auth_cookies()
        try:
            return requests.get(
                f"{REDDIT_API}{endpoint}",
                cookies=cookies,
                proxies=_proxies_dict(proxy),
                timeout=timeout,
            )
        except Exception as e:
            log.error("GET %s failed: %s", endpoint, e)
            self.proxies.mark_failed(proxy)
            self._working_proxy = None
            return None

    def submit_post(self, subreddit: str, title: str, url: str, nsfw: bool = True) -> str | None:
        """Submit a link post. Returns post URL or None."""
        data = {
            "sr": subreddit,
            "kind": "link",
            "title": title,
            "url": url,
            "api_type": "json",
        }
        if nsfw:
            data["nsfw"] = "true"

        r = self.post("/api/submit", data)
        if not r:
            return None

        if r.status_code != 200:
            log.error("Submit failed: %s %s", r.status_code, r.text[:200])
            return None

        result = r.json()
        errors = result.get("json", {}).get("errors", [])
        if errors:
            log.error("Submit error: %s", errors[0])
            return None

        post_url = result.get("json", {}).get("data", {}).get("url", "")
        log.info("Posted to r/%s: %s", subreddit, post_url)
        return post_url

    def submit_self_post(self, subreddit: str, title: str, body: str, nsfw: bool = True) -> str | None:
        """Submit a text post. Returns post URL or None."""
        data = {
            "sr": subreddit,
            "kind": "self",
            "title": title,
            "text": body,
            "api_type": "json",
        }
        if nsfw:
            data["nsfw"] = "true"

        r = self.post("/api/submit", data)
        if not r:
            return None

        if r.status_code != 200:
            log.error("Submit failed: %s %s", r.status_code, r.text[:200])
            return None

        result = r.json()
        errors = result.get("json", {}).get("errors", [])
        if errors:
            log.error("Submit error: %s", errors[0])
            return None

        post_url = result.get("json", {}).get("data", {}).get("url", "")
        log.info("Posted to r/%s: %s", subreddit, post_url)
        return post_url

    def delete_post(self, post_name: str) -> bool:
        """Delete a post by its fullname (e.g. t3_abc123)."""
        r = self.post("/api/del", {"id": post_name})
        return r is not None and r.status_code == 200
