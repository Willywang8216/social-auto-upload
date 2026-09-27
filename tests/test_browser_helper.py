from __future__ import annotations

import asyncio
import sys
import types
import unittest
from unittest.mock import patch

from myUtils import browser_helper


class BrowserHelperTests(unittest.TestCase):
    def test_local_browser_context_avoids_remote_cdp(self):
        calls = []

        class BrowserType:
            async def launch(self, **kwargs):
                calls.append(("local", kwargs))
                return "local-browser"

            async def connect_over_cdp(self, url):
                calls.append(("remote", url))
                return "remote-browser"

        root = types.ModuleType("patchright")
        api = types.ModuleType("patchright.async_api")
        generated = types.ModuleType("patchright.async_api._generated")
        generated.BrowserType = BrowserType

        with patch.dict(sys.modules, {
            "patchright": root,
            "patchright.async_api": api,
            "patchright.async_api._generated": generated,
        }), patch.object(browser_helper, "BROWSERLESS_URL", "http://browserless"), \
                patch.object(browser_helper, "BROWSERLESS_WS", "ws://browserless"):
            browser_helper.install_browserless_patch()
            instance = BrowserType()
            self.assertEqual(asyncio.run(instance.launch()), "remote-browser")
            with browser_helper.local_browser():
                self.assertEqual(asyncio.run(instance.launch(args=["--no-sandbox"])), "local-browser")

        self.assertEqual(calls[0], ("remote", "ws://browserless"))
        self.assertEqual(calls[1], ("local", {"args": ["--no-sandbox"]}))


if __name__ == "__main__":
    unittest.main()
