"""Unit tests for the X Article publisher's pure logic.

The browser flow itself is verified live; these pin the parsing/selection logic
that decides what gets typed and what id is reported back.
"""

from __future__ import annotations

from uploader.twitter_uploader import article as xa


def test_parse_article_uses_hash_title_and_splits_paragraphs():
    text = "# My Long Title\n\nFirst paragraph.\n\nSecond paragraph\nspans two lines.\n\nThird."
    title, paragraphs = xa.parse_article(text)
    assert title == "My Long Title"
    assert paragraphs == [
        "First paragraph.",
        "Second paragraph\nspans two lines.",
        "Third.",
    ]


def test_parse_article_falls_back_to_first_line_for_title():
    title, paragraphs = xa.parse_article("Just a title\n\nBody one.\n\nBody two.")
    assert title == "Just a title"
    assert paragraphs == ["Body one.", "Body two."]


def test_parse_article_drops_heading_markers_in_body():
    title, paragraphs = xa.parse_article("T\n\n## Section\n\ntext")
    assert title == "T"
    assert paragraphs == ["Section", "text"]


def test_extract_post_id_finds_nested_rest_id():
    payload = {"data": {"create_tweet": {"tweet_results": {"result": {"rest_id": "2110279404459033052"}}}}}
    assert xa._extract_post_id(payload) == "2110279404459033052"


def test_extract_post_id_ignores_short_ids():
    assert xa._extract_post_id({"data": {"id": "123"}}) is None


class _FakeLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return self._count


class _FakePage:
    def __init__(self, counts: dict) -> None:
        self._counts = counts

    def get_by_text(self, label, exact=True):
        return _FakeLocator(self._counts.get(label, 0))


def test_first_label_async_awaits_count_and_skips_absent_labels():
    """Regression: an un-awaited Locator.count() is always truthy."""
    import asyncio

    page = _FakePage({"撰寫": 0, "Write": 1})
    assert asyncio.run(xa._first_label_async(page, ("撰寫", "Write"))) is not None
    assert asyncio.run(xa._first_label_async(page, ("Nope",))) is None
