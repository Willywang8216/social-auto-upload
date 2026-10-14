"""Rules-first tests for the bundle.social Reddit route.

Every past Reddit failure here was avoidable (banned-from-sub, link whitelist,
NO_SELFS, missing flair), so these tests pin the *refusals* as much as the happy
path: a rule violation must raise, never be submitted "to see what happens".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from myUtils import bundle_social as bs


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else json.dumps(payload if payload is not None else {})
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload

    def close(self):
        return None


class FakeSession:
    def __init__(self, handler):
        self._handler = handler
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self._handler(method, url, kwargs)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._handler("GET", url, kwargs)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._handler("POST", url, kwargs)

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return self._handler("PUT", url, kwargs)


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setenv(bs.API_KEY_ENV, "test-key")
    monkeypatch.setenv(bs.TEAM_ID_ENV, "team-1")
    monkeypatch.delenv(bs.CAP_ENV, raising=False)
    monkeypatch.delenv(bs.SUBS_PER_TARGET_ENV, raising=False)


def _channels(**metadata_by_sub):
    return {
        name.lower(): {"username": f"r/{name}", "name": name, "metadata": md}
        for name, md in metadata_by_sub.items()
    }


# --------------------------------------------------------------------------- #
# rule checks
# --------------------------------------------------------------------------- #
def test_subreddit_denial_blocks_native_video_but_allows_image():
    channels = _channels(NudistMen={"allowImages": True, "allowGalleries": True, "allowVideos": False})
    assert "does not allow native video" in bs.subreddit_denial(
        channels, "NudistMen", media_kind="video"
    )
    assert bs.subreddit_denial(channels, "NudistMen", media_kind="image") is None


def test_subreddit_denial_blocks_disallowed_gallery():
    channels = _channels(SomeSub={"allowImages": True, "allowGalleries": False, "allowVideos": True})
    assert "galleries" in bs.subreddit_denial(
        channels, "SomeSub", media_kind="image", is_gallery=True
    )


def test_unknown_channel_is_not_denied_here():
    assert bs.subreddit_denial({}, "whatever", media_kind="video") is None


def test_flair_required_without_a_resolvable_id_is_refused(monkeypatch):
    monkeypatch.setattr(bs, "reddit_subreddit_flairs", lambda *a, **k: [])
    with pytest.raises(bs.BundleSocialRuleError, match="requires a post flair"):
        bs.resolve_flair_id(
            "NudistMen",
            requirements={"isFlairRequired": True, "isFlairEnabled": True},
        )


def test_flair_resolves_from_account_config():
    flair = bs.resolve_flair_id(
        "NudistMen",
        requirements={"isFlairRequired": True},
        config_flair_ids={"NudistMen": {"In Nature": "flair-abc"}},
        content_kind="In Nature",
    )
    assert flair == "flair-abc"


def test_single_api_flair_is_used_when_config_is_silent(monkeypatch):
    monkeypatch.setattr(
        bs, "reddit_subreddit_flairs", lambda *a, **k: [{"id": "only-1", "text": "General"}]
    )
    assert (
        bs.resolve_flair_id("NudistMen", requirements={"isFlairRequired": True})
        == "only-1"
    )


# --------------------------------------------------------------------------- #
# quota
# --------------------------------------------------------------------------- #
def test_publish_refuses_when_monthly_quota_is_exhausted(monkeypatch):
    monkeypatch.setattr(bs, "remaining_posts", lambda **k: 0)
    monkeypatch.setattr(bs, "reddit_channels", lambda account: _channels(Body={"allowVideos": True}))
    monkeypatch.setattr(bs, "get_reddit_account", lambda **k: {"username": "u"})
    with pytest.raises(bs.BundleSocialQuotaError, match="monthly post quota"):
        bs.publish_reddit_post(subreddit="Body", title="hi", media_kind=None)


def test_remaining_posts_honours_the_configured_cap(monkeypatch):
    monkeypatch.setenv(bs.CAP_ENV, "5")
    monkeypatch.setattr(bs, "org_posts_usage", lambda **k: {"used": 2, "limit": 20, "remaining": 18})
    assert bs.remaining_posts() == 3


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #
def test_publish_creates_flaired_post_with_upload(monkeypatch, tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"v" * 1024)
    monkeypatch.setattr(
        bs, "reddit_channels",
        lambda account: _channels(Body={"allowVideos": True, "allowImages": True}),
    )
    monkeypatch.setattr(bs, "get_reddit_account", lambda **k: {"username": "u"})
    monkeypatch.setattr(bs, "remaining_posts", lambda **k: 20)
    monkeypatch.setattr(
        bs, "reddit_post_requirements",
        lambda *a, **k: {"isFlairRequired": True, "isFlairEnabled": True},
    )
    monkeypatch.setattr(bs, "get_post_by_reference_key", lambda *a, **k: None)
    monkeypatch.setattr(
        bs, "upload_file_simple", lambda path, **k: {"id": "upload-1"}
    )
    session = FakeSession(
        lambda m, u, kw: FakeResponse(200, {"id": "post-1", "status": "SCHEDULED"})
    )
    result = bs.publish_reddit_post(
        subreddit="r/Body",
        title="Title",
        body="Body text",
        media_path=media,
        media_kind="video",
        flair_id="flair-xyz",
        reference="sau-reddit-9-body",
        session=session,
    )
    assert result["post_id"] == "post-1"
    assert result["subreddit"] == "Body"
    assert result["upload_ids"] == ["upload-1"]
    call = [c for c in session.calls if c[0] == "POST" and c[1].endswith("/api/v1/post/")][0]
    body = call[2]["json"]
    assert body["socialAccountTypes"] == ["REDDIT"]
    assert body["data"]["REDDIT"]["sr"] == "r/Body"
    assert body["data"]["REDDIT"]["flairId"] == "flair-xyz"
    assert body["data"]["REDDIT"]["uploadIds"] == ["upload-1"]
    assert body["data"]["REDDIT"]["nsfw"] is True
    assert body["referenceKey"] == "sau-reddit-9-body"
    # SCHEDULED requires postDate or bundle.social rejects with a 400.
    assert body["postDate"].endswith("Z")


def test_publish_is_idempotent_by_reference_key(monkeypatch):
    monkeypatch.setattr(bs, "reddit_channels", lambda account: _channels(Body={"allowVideos": True}))
    monkeypatch.setattr(bs, "get_reddit_account", lambda **k: {"username": "u"})
    monkeypatch.setattr(bs, "remaining_posts", lambda **k: 20)
    monkeypatch.setattr(
        bs, "get_post_by_reference_key",
        lambda key, **k: {"id": "old-post", "status": "POSTED"},
    )
    session = FakeSession(lambda m, u, kw: FakeResponse(200, {}))
    result = bs.publish_reddit_post(
        subreddit="Body", title="t", media_kind=None, reference="sau-reddit-9-body",
        session=session,
    )
    assert result["deduplicated"] is True
    assert result["post_id"] == "old-post"
    assert not [c for c in session.calls if c[0] == "POST"]


def test_publish_refuses_video_to_a_video_less_channel(monkeypatch):
    monkeypatch.setattr(
        bs, "reddit_channels",
        lambda account: _channels(NudistMen={"allowImages": True, "allowVideos": False}),
    )
    monkeypatch.setattr(bs, "get_reddit_account", lambda **k: {"username": "u"})
    with pytest.raises(bs.BundleSocialRuleError, match="does not allow native video"):
        bs.publish_reddit_post(
            subreddit="NudistMen", title="t", media_kind="video", media_path="/nope.mp4"
        )


# --------------------------------------------------------------------------- #
# media
# --------------------------------------------------------------------------- #
def test_upload_media_prefers_a_fetchable_url(monkeypatch):
    monkeypatch.setattr(bs, "_url_is_fetchable", lambda url, **k: True)
    monkeypatch.setattr(bs, "upload_from_url", lambda url, **k: {"id": "url-upload"})
    assert bs.upload_media(url="https://example.test/a.mp4") == "url-upload"


def test_upload_media_falls_back_to_the_local_file(monkeypatch, tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"v" * 16)
    monkeypatch.setattr(bs, "_url_is_fetchable", lambda url, **k: False)
    monkeypatch.setattr(bs, "upload_file_simple", lambda path, **k: {"id": "local-upload"})
    assert bs.upload_media(path=media, url="https://dead.test/a.mp4") == "local-upload"


def test_upload_media_without_any_source_is_retryable():
    with pytest.raises(bs.BundleSocialError) as excinfo:
        bs.upload_media()
    assert excinfo.value.retryable is True


def test_reference_key_shape():
    assert bs.reference_key(42, "r/NudistMen") == "sau-reddit-42-nudistmen"
    assert bs.reference_key(42, "NudistMen", attempt=3) == "sau-reddit-42-nudistmen-a3"


def test_not_configured_without_a_key(monkeypatch):
    monkeypatch.delenv(bs.API_KEY_ENV, raising=False)
    monkeypatch.delenv(bs._ALT_ENV[bs.API_KEY_ENV], raising=False)
    assert bs.is_enabled() is False
    with pytest.raises(bs.BundleSocialNotConfigured):
        bs.publish_reddit_post(subreddit="x", title="t")
