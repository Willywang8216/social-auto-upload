"""Exercise uploader regression branches with fake pages, never real browsers."""

import asyncio
import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

pytest.importorskip("patchright")


@pytest.fixture
def douyin(monkeypatch):
    # The local checkout need not have a deployment conf.py just to test polling.
    if "conf" not in sys.modules and importlib.util.find_spec("conf") is None:
        conf = ModuleType("conf")
        conf.DEBUG_MODE, conf.LOCAL_CHROME_HEADLESS, conf.LOCAL_CHROME_PATH = False, True, ""
        monkeypatch.setitem(sys.modules, "conf", conf)
    module = importlib.import_module("uploader.douyin_uploader.main")
    monkeypatch.setattr(module, "douyin_logger", Mock())
    return module


@pytest.fixture
def xiaohongshu(monkeypatch):
    module = importlib.import_module("uploader.xiaohongshu_uploader.main")
    monkeypatch.setattr(module, "xiaohongshu_logger", Mock())
    return module


class FakeLocator:
    def __init__(self, count=0):
        self.first = self
        self.count = AsyncMock(return_value=count)
        self.is_visible = AsyncMock(return_value=bool(count))
        self.click = AsyncMock()
        self.wait_for = AsyncMock()

    def locator(self, selector):
        return self


class LoginPage:
    def __init__(self, urls):
        self.url, *self.remaining_urls = urls
        self.expired = FakeLocator()
        self.sms = FakeLocator(count=1)

    def get_by_text(self, text, **kwargs):
        return self.expired if text == "二维码失效" else FakeLocator()

    def get_by_role(self, *args, **kwargs):
        return FakeLocator()

    def locator(self, selector):
        assert 'input[type="tel"]' in selector
        return self.sms

    async def advance(self, seconds):
        if self.remaining_urls:
            self.url = self.remaining_urls.pop(0)


def test_douyin_polling_reaches_sms_branch_and_logs_once(douyin, monkeypatch):
    page = LoginPage([
        "https://creator.douyin.com/",
        "https://creator.douyin.com/passport/sms",
        "https://creator.douyin.com/passport/sms",
        "https://creator.douyin.com/creator-micro/home",
    ])
    monkeypatch.setattr(douyin.asyncio, "sleep", page.advance)
    result = asyncio.run(douyin._wait_for_douyin_login(page, "unused.json", {}, poll_interval=0, max_checks=4))
    assert result["success"] is True
    assert result["current_url"] == page.url
    assert page.sms.count.await_count == 2
    douyin.douyin_logger.warning.assert_called_once()
    assert "(2/4)" in douyin.douyin_logger.warning.call_args.args[0]


def test_douyin_polling_times_out_without_url_change(douyin, monkeypatch):
    page = LoginPage(["https://creator.douyin.com/"])
    monkeypatch.setattr(douyin.asyncio, "sleep", page.advance)
    result = asyncio.run(douyin._wait_for_douyin_login(page, "unused.json", {}, poll_interval=0, max_checks=2))
    assert result["success"] is False
    assert result["status"] == "timeout"
    douyin.douyin_logger.warning.assert_not_called()


def test_douyin_expired_qrcode_refresh_continues_polling(douyin, monkeypatch):
    page = LoginPage(["https://creator.douyin.com/", "https://creator.douyin.com/",
                      "https://creator.douyin.com/creator-micro/home"])
    page.expired = FakeLocator(count=1)
    page.expired.click.side_effect = lambda: setattr(page.expired.count, "return_value", 0)
    refreshed = {"image_path": "new.png", "image_data_url": "fake"}
    save_qr = AsyncMock(return_value=refreshed)
    monkeypatch.setattr(douyin, "_save_douyin_qrcode", save_qr)
    monkeypatch.setattr(douyin.asyncio, "sleep", page.advance)
    result = asyncio.run(douyin._wait_for_douyin_login(
        page, "unused.json", {"image_path": "old.png"}, poll_interval=0, max_checks=2))
    assert result["success"] is True
    assert result["qrcode"] == refreshed
    save_qr.assert_awaited_once_with(page, "unused.json", Path("old.png"), qrcode_callback=None)


class TagPage:
    def __init__(self):
        self.keyboard = SimpleNamespace(type=AsyncMock(), press=AsyncMock())
        self.desc, self.container, self.item = FakeLocator(1), FakeLocator(1), FakeLocator(1)

    def locator(self, selector):
        return {
            'p[data-placeholder*="输入正文描述"]': self.desc,
            '#creator-editor-topic-container': self.container,
            '#creator-editor-topic-container .item': self.item,
        }[selector]


def test_xiaohongshu_tag_overflow_warns_and_truncates(xiaohongshu):
    tags = [f"tag-{i}" for i in range(12)]
    uploader = SimpleNamespace(tags=tags, desc="")
    page = TagPage()
    asyncio.run(xiaohongshu.XiaoHongShuBaseUploader.fill_tags(uploader, page))
    assert uploader.tags == tags[:10]
    assert page.keyboard.type.await_args_list == [call("#" + tag, delay=30) for tag in tags[:10]]
    assert page.item.click.await_count == 10
    xiaohongshu.xiaohongshu_logger.warning.assert_called_once()


@pytest.mark.parametrize("failed_locator", ["container", "item"])
def test_xiaohongshu_missing_tag_candidates_warn_and_continue(xiaohongshu, failed_locator):
    uploader = SimpleNamespace(tags=["missing", "ok"], desc="")
    page = TagPage()
    getattr(page, failed_locator).wait_for.side_effect = [TimeoutError("no candidates"), None]
    asyncio.run(xiaohongshu.XiaoHongShuBaseUploader.fill_tags(uploader, page))
    assert page.keyboard.type.await_args_list == [call("#missing", delay=30), call("#ok", delay=30)]
    assert page.keyboard.press.await_args_list == [call("Backspace")] * len("#missing")
    page.item.click.assert_awaited_once()
    xiaohongshu.xiaohongshu_logger.warning.assert_called_once()
    assert "missing" in xiaohongshu.xiaohongshu_logger.warning.call_args.args[0]
