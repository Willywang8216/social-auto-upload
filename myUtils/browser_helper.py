"""Shared browser connection helper for social-auto-upload.

When BROWSERLESS_URL is set, monkey-patches patchright/playwright chromium
to redirect all launch() calls to a remote browserless/chrome instance.
This avoids editing every uploader file individually.
"""

import contextvars
import os
from contextlib import contextmanager

BROWSERLESS_URL = os.environ.get("BROWSERLESS_URL", "")
_local_browser = contextvars.ContextVar("sau_local_browser", default=False)


@contextmanager
def local_browser():
    """Use a co-located Chromium browser for code that transfers local files."""
    token = _local_browser.set(True)
    try:
        yield
    finally:
        _local_browser.reset(token)


# Convert HTTP URL to WebSocket URL (browserless returns 0.0.0.0 in its
# /json/version response, which breaks cross-container resolution).
BROWSERLESS_WS = BROWSERLESS_URL.replace("http://", "ws://").replace("https://", "wss://")


def install_browserless_patch():
    """Patch patchright/playwright to use remote browser via CDP.

    Call this once at startup before any browser usage.
    """
    if not BROWSERLESS_URL:
        return

    ws_url = BROWSERLESS_WS

    # Patch patchright
    try:
        from patchright.async_api._generated import BrowserType as PatchrightBrowserType
        original_launch = PatchrightBrowserType.launch

        async def _patched_patchright_launch(self, **kwargs):
            if _local_browser.get():
                return await original_launch(self, **kwargs)
            kwargs.pop("executable_path", None)
            kwargs.pop("channel", None)
            kwargs.pop("args", None)
            kwargs.pop("headless", None)
            return await self.connect_over_cdp(ws_url)

        PatchrightBrowserType.launch = _patched_patchright_launch
        print(f"[browser_helper] Patched patchright chromium.launch -> connect_over_cdp({ws_url})")
    except Exception as e:
        print(f"[browser_helper] Failed to patch patchright: {e}")

    # playwright is no longer patched — the project uses patchright exclusively.
