"""Tests for the Sociamonials fallback (Phase 4).

Every test injects a fake HTTP session; nothing here makes a live call.  The
suite covers the config guards, the account->profile mapping, media handling
(direct URL passthrough, three-step upload, multipart), payload composition and
the post request itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from myUtils import sociamonials_fallback as sm


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Records every call and dispatches to a handler."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def _call(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.handler(method, url, kwargs)

    def post(self, url, **kwargs):
        return self._call("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self._call("PUT", url, **kwargs)

    def head(self, url, **kwargs):
        return self._call("HEAD", url, **kwargs)

    def get(self, url, **kwargs):
        return self._call("GET", url, **kwargs)

    def calls_for(self, method, contains=""):
        return [c for c in self.calls if c[0] == method and contains in c[1]]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        sm.ENABLED_ENV,
        sm.API_KEY_ENV,
        sm.WORKSPACE_ENV,
        sm.TEACHING_API_KEY_ENV,
        sm.TEACHING_WORKSPACE_ENV,
        sm.SECRETS_FILE_ENV,
        sm.TIMEOUT_ENV,
        sm.REUSE_ASSETS_ENV,
    ):
        monkeypatch.delenv(key, raising=False)
    # Never let a test read the developer's real ~/.claude/secrets.json.
    monkeypatch.setenv(sm.SECRETS_FILE_ENV, "/nonexistent/sau-test-secrets.json")
    yield


class _Account:
    def __init__(self, account_id, platform="twitter", config=None):
        self.id = account_id
        self.platform = platform
        self.config = config or {}
        self.account_name = f"acct-{account_id}"


def _posts_handler(*, post_result=None, grants=None, upload_etag="etag-1", assets=None):
    """Handler serving the media upload flow and the post create."""
    grants = grants or {
        "default": {
            "upload_id": 5,
            "mode": "single",
            "upload_url": "https://upload.example/put/5",
        }
    }
    post_result = post_result if post_result is not None else {
        "post_id": 999,
        "status": "published",
        "requires_approval": False,
    }
    assets = list(assets or [])

    def handler(method, url, kwargs):
        if method == "HEAD":
            return FakeResponse(200, headers={"Content-Type": "video/mp4"})
        if method == "GET" and url.rstrip("/").endswith("/media/assets"):
            return FakeResponse(200, {"assets": assets, "total": len(assets)})
        if method == "GET" and "/media/assets/" in url:
            return FakeResponse(200, {"asset_id": 77, "processing_status": "ready"})
        if method == "POST" and url.endswith("/media/uploads"):
            name = kwargs["json"].get("filename", "default")
            grant = grants.get(name, grants.get("default"))
            return FakeResponse(200, grant)
        if method == "PUT" and url.startswith("https://upload.example"):
            return FakeResponse(200, headers={"ETag": upload_etag})
        if method == "POST" and url.endswith("/complete"):
            return FakeResponse(200, {"asset_id": 77, "url": "https://cdn.example/a/77.mp4"})
        if method == "POST" and url.endswith("/parts"):
            number = kwargs["json"]["part_number"]
            return FakeResponse(
                200, {"parts": [{"part_number": number, "url": f"https://upload.example/part/{number}"}]}
            )
        if method == "POST" and url.endswith("/api/v1/posts"):
            return FakeResponse(200, post_result)
        raise AssertionError(f"unexpected {method} {url}")

    return handler


# --------------------------------------------------------------------------- #
# Configuration guards
# --------------------------------------------------------------------------- #

def test_is_configured_requires_a_key():
    assert sm.is_configured({}) is False
    assert sm.is_configured({sm.API_KEY_ENV: "sm_agent_x"}) is True


def test_get_api_key_reads_secrets_file(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets.json"
    secrets.write_text(json.dumps({sm.API_KEY_ENV: "sm_agent_file"}), encoding="utf-8")
    monkeypatch.setenv(sm.SECRETS_FILE_ENV, str(secrets))
    assert sm.get_api_key() == "sm_agent_file"


def test_is_enabled_default_off(monkeypatch):
    monkeypatch.setenv(sm.API_KEY_ENV, "sm_agent_x")
    assert sm.is_enabled() is False  # flag not set -> default off
    monkeypatch.setenv(sm.ENABLED_ENV, "1")
    assert sm.is_enabled() is True


def test_is_enabled_requires_a_key(monkeypatch):
    monkeypatch.setenv(sm.ENABLED_ENV, "1")
    assert sm.is_enabled() is False


def test_should_attempt_fallback_never_for_non_retryable():
    assert sm.should_attempt_fallback(
        retryable=False, attempts=3, max_attempts=3, enabled=True
    ) is False


def test_should_attempt_fallback_only_after_budget_exhausted():
    assert sm.should_attempt_fallback(
        retryable=True, attempts=2, max_attempts=3, enabled=True
    ) is False
    assert sm.should_attempt_fallback(
        retryable=True, attempts=3, max_attempts=3, enabled=True
    ) is True
    # disabled master switch short-circuits even at the limit
    assert sm.should_attempt_fallback(
        retryable=True, attempts=3, max_attempts=3, enabled=False
    ) is False


# --------------------------------------------------------------------------- #
# Mapping
# --------------------------------------------------------------------------- #

def test_resolve_mapping_builtin():
    mapping = sm.resolve_mapping(77, platform="twitter")
    assert mapping == {"network": "tw", "profile_refs": ["14100"], "name": "will_sexual"}


def test_resolve_mapping_account_config_wins():
    account = _Account(77, config={"sociamonials": {"network": "tw", "profile_refs": ["9999"]}})
    mapping = sm.resolve_mapping(77, account=account)
    assert mapping["profile_refs"] == ["9999"]


def test_resolve_mapping_settings_override():
    settings = {"sociamonials": {"accounts": {"77": {"network": "tw", "profile_refs": ["1234"]}}}}
    mapping = sm.resolve_mapping(77, settings=settings)
    assert mapping["profile_refs"] == ["1234"]


def test_resolve_mapping_settings_single_shape():
    settings = {"sociamonials": {"77": {"profile_refs": ["4321"]}}}
    mapping = sm.resolve_mapping(77, platform="twitter", settings=settings)
    assert mapping["profile_refs"] == ["4321"]
    assert mapping["network"] == "tw"


def test_resolve_mapping_unmapped_account_returns_none():
    assert sm.resolve_mapping(424242) is None


# --------------------------------------------------------------------------- #
# Direct-URL detection
# --------------------------------------------------------------------------- #

def test_is_direct_media_url_accepts_cdn_file():
    assert sm.is_direct_media_url("https://cdn.example/campaigns/1/videos/clip.mp4") is True


def test_is_direct_media_url_rejects_page_and_local():
    assert sm.is_direct_media_url("http://127.0.0.1:5409/getFile?filename=x.mp4") is False
    assert sm.is_direct_media_url("https://drive.google.com/file/d/abc/view") is False
    assert sm.is_direct_media_url("https://share.example.com/d/abc") is False
    assert sm.is_direct_media_url("not-a-url") is False


# --------------------------------------------------------------------------- #
# Message composition
# --------------------------------------------------------------------------- #

def test_compose_message_flattens_dict_draft_and_hashtags():
    payload = {
        "draft": {
            "message": {"title": "A title", "description": "Body copy", "hashtags": ["#one", "two"]},
        }
    }
    message = sm.compose_message(payload, network="facebook")
    assert "A title" in message
    assert "Body copy" in message
    assert "#one" in message and "#two" in message


def test_compose_message_truncates_twitter():
    payload = {"draft": {"message": "x" * 400}}
    message = sm.compose_message(payload, network="tw")
    assert len(message) <= 280


def test_compose_message_with_links_strips_x_links_and_returns_them():
    payload = {"draft": {"message": "Read more at https://nakedwill.com/post today"}}
    message, links = sm.compose_message_with_links(payload, network="tw")
    assert "http" not in message.lower()
    assert "Read more at" in message and "today" in message
    assert links == ["https://nakedwill.com/post"]


def test_compose_message_keeps_links_for_non_x_networks():
    payload = {"draft": {"message": "Read more at https://nakedwill.com/post today"}}
    message, links = sm.compose_message_with_links(payload, network="facebook")
    assert "https://nakedwill.com/post" in message
    assert links == []


def test_compose_message_truncates_bluesky_to_300():
    payload = {"draft": {"message": "x" * 400}}
    message, _ = sm.compose_message_with_links(payload, network="blsk")
    assert len(message) <= 300


def test_assert_video_duration_rejects_over_limit(tmp_path, monkeypatch):
    import myUtils.media_pipeline as media_pipeline

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"v" * 32)
    monkeypatch.setattr(media_pipeline, "probe_video_duration", lambda path: 500.0)
    with pytest.raises(sm.SociamonialsFallbackError):
        sm._assert_video_duration("tw", str(video))


def test_assert_video_duration_allows_within_limit(tmp_path, monkeypatch):
    import myUtils.media_pipeline as media_pipeline

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"v" * 32)
    monkeypatch.setattr(media_pipeline, "probe_video_duration", lambda path: 120.0)
    sm._assert_video_duration("tw", str(video))


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #

def test_publish_requires_a_key():
    with pytest.raises(sm.SociamonialsNotConfigured):
        sm.publish_via_sociamonials(
            platform="twitter",
            account=_Account(77),
            payload={"draft": {"message": "hello"}},
            api_key="",
        )


def test_publish_unmapped_account_raises():
    with pytest.raises(sm.SociamonialsFallbackError):
        sm.publish_via_sociamonials(
            platform="twitter",
            account=_Account(424242, platform="twitter"),
            payload={"draft": {"message": "hello"}},
            api_key="sm_agent_x",
        )


def test_publish_maps_network_and_idempotency_key():
    session = FakeSession(_posts_handler())
    account = _Account(77, platform="twitter")
    result = sm.publish_via_sociamonials(
        platform="twitter",
        account=account,
        payload={"draft": {"message": "Hello there", "hashtags": ["#naturism"]}},
        target_id=4321,
        api_key="sm_agent_x",
        session=session,
    )
    assert result["ok"] is True
    assert result["post_id"] == 999
    posts = session.calls_for("POST", "/api/v1/posts")
    body = posts[0][2]["json"]
    assert body["mode"] == "publish_now"
    assert body["networks"] == {"tw": {"profile_refs": ["14100"]}}
    assert body["idempotency_key"] == "sau-target-4321"
    assert "Hello there" in body["message"]
    assert "#naturism" in body["message"]


def test_publish_x_moves_links_from_body_to_first_comment():
    session = FakeSession(_posts_handler())
    sm.publish_via_sociamonials(
        platform="twitter",
        account=_Account(124),
        payload={"draft": {"message": "New post: https://nakedwill.com/x"}},
        target_id=321,
        api_key="sm_agent_x",
        session=session,
        delivery_timeout=0,
    )
    body = session.calls_for("POST", "/api/v1/posts")[0][2]["json"]
    assert "http" not in body["message"].lower()
    assert body["first_comment"] == "https://nakedwill.com/x"


def test_publish_x_appends_links_to_existing_first_comment():
    session = FakeSession(_posts_handler())
    sm.publish_via_sociamonials(
        platform="twitter",
        account=_Account(124),
        payload={
            "draft": {
                "message": "Body https://a.example/1",
                "firstComment": "Reply text",
            }
        },
        api_key="sm_agent_x",
        session=session,
        delivery_timeout=0,
    )
    body = session.calls_for("POST", "/api/v1/posts")[0][2]["json"]
    assert "http" not in body["message"].lower()
    assert "Reply text" in body["first_comment"]
    assert "https://a.example/1" in body["first_comment"]


def test_publish_non_x_keeps_link_in_body():
    session = FakeSession(_posts_handler())
    sm.publish_via_sociamonials(
        platform="facebook",
        account=_Account(11, platform="facebook"),
        payload={"draft": {"message": "Body https://a.example/1"}},
        api_key="sm_agent_x",
        session=session,
        delivery_timeout=0,
    )
    body = session.calls_for("POST", "/api/v1/posts")[0][2]["json"]
    assert "https://a.example/1" in body["message"]


def test_local_media_upload_sends_no_idempotency_key(tmp_path):
    """A media-level key makes a completed multipart grant un-reusable."""
    image = tmp_path / "pic.jpg"
    image.write_bytes(b"\xff\xd8\xff" + b"0" * 32)
    session = FakeSession(_posts_handler())
    sm.publish_via_sociamonials(
        platform="instagram",
        account=_Account(72, platform="instagram"),
        payload={"draft": {"message": "A photo"}, "artifacts": [
            {"local_path": str(image), "public_url": "", "metadata": {"role": "image"}}
        ]},
        target_id=55,
        api_key="sm_agent_x",
        session=session,
        delivery_timeout=0,
    )
    grant_body = session.calls_for("POST", "/media/uploads")[0][2]["json"]
    assert "idempotency_key" not in grant_body


def test_publish_waits_for_a_video_asset_to_become_ready(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"v" * 64)
    polls = {"n": 0}

    def handler(method, url, kwargs):
        if method == "GET" and "/media/assets/" in url:
            polls["n"] += 1
            status = "processing" if polls["n"] < 2 else "ready"
            return FakeResponse(200, {"asset_id": 77, "processing_status": status})
        if method == "POST" and url.endswith("/media/uploads"):
            return FakeResponse(200, {"upload_id": 5, "mode": "single", "upload_url": "https://upload.example/put/5"})
        if method == "PUT":
            return FakeResponse(200, headers={"ETag": "e"})
        if method == "POST" and url.endswith("/complete"):
            return FakeResponse(200, {"asset_id": 77})
        if method == "POST" and url.endswith("/api/v1/posts"):
            return FakeResponse(200, {"post_id": 1, "status": "scheduled"})
        raise AssertionError(f"unexpected {method} {url}")

    session = FakeSession(handler)
    sm.publish_via_sociamonials(
        platform="twitter",
        account=_Account(77),
        payload={"draft": {"message": "video post"}, "artifacts": [
            {"local_path": str(video), "metadata": {"role": "video"}}
        ]},
        api_key="sm_agent_x",
        session=session,
        delivery_timeout=0,
    )
    assert polls["n"] >= 2
    assert session.calls_for("POST", "/api/v1/posts")


def test_wait_for_asset_ready_retries_a_transient_404():
    polls = {"n": 0}

    class Sess:
        def get(self, url, **kw):
            polls["n"] += 1
            if polls["n"] == 1:
                return FakeResponse(404, {})
            return FakeResponse(200, {"asset_id": 7, "processing_status": "ready"})

    sm._wait_for_asset_ready(Sess(), {}, 7, timeout=5, interval=0.001)
    assert polls["n"] == 2


def test_publish_raises_when_a_video_asset_never_becomes_ready(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"v" * 64)

    def handler(method, url, kwargs):
        if method == "GET" and "/media/assets/" in url:
            return FakeResponse(200, {"asset_id": 77, "processing_status": "processing"})
        if method == "POST" and url.endswith("/media/uploads"):
            return FakeResponse(200, {"upload_id": 5, "mode": "single", "upload_url": "https://upload.example/put/5"})
        if method == "PUT":
            return FakeResponse(200, headers={"ETag": "e"})
        if method == "POST" and url.endswith("/complete"):
            return FakeResponse(200, {"asset_id": 77})
        raise AssertionError(f"unexpected {method} {url}")

    session = FakeSession(handler)
    with pytest.raises(sm.SociamonialsFallbackError):
        sm._wait_for_asset_ready(session, {}, 77, timeout=0.01, interval=0.001)


def test_publish_uses_direct_video_url_without_uploading():
    session = FakeSession(_posts_handler())
    payload = {
        "draft": {"message": "Watch this"},
        "artifacts": [
            {
                "local_path": "/tmp/clip.mp4",
                "public_url": "https://cdn.example/campaigns/1/videos/clip.mp4",
                "metadata": {"role": "video"},
            }
        ],
    }
    sm.publish_via_sociamonials(
        platform="twitter",
        account=_Account(77),
        payload=payload,
        api_key="sm_agent_x",
        session=session,
    )
    assert not session.calls_for("POST", "/media/uploads")
    body = session.calls_for("POST", "/api/v1/posts")[0][2]["json"]
    assert body["video_url"] == "https://cdn.example/campaigns/1/videos/clip.mp4"


def test_publish_uploads_local_image(tmp_path):
    image = tmp_path / "pic.jpg"
    image.write_bytes(b"\xff\xd8\xff" + b"0" * 32)
    session = FakeSession(_posts_handler())
    payload = {
        "draft": {"message": "A photo"},
        "artifacts": [
            {"local_path": str(image), "public_url": "", "metadata": {"role": "image"}}
        ],
    }
    result = sm.publish_via_sociamonials(
        platform="instagram",
        account=_Account(72, platform="instagram"),
        payload=payload,
        target_id=55,
        api_key="sm_agent_x",
        session=session,
    )
    assert result["ok"] is True
    assert session.calls_for("POST", "/media/uploads")
    assert session.calls_for("PUT", "https://upload.example")
    complete = session.calls_for("POST", "/complete")
    assert complete, "the upload must be completed"
    body = session.calls_for("POST", "/api/v1/posts")[0][2]["json"]
    assert body["image_urls"] == ["asset://77"]


def test_publish_reuses_an_identical_library_asset(tmp_path):
    """A file already in the library must not be uploaded a second time."""
    image = tmp_path / "pic.jpg"
    image.write_bytes(b"\xff\xd8\xff" + b"0" * 32)
    existing = {
        "asset_id": 555,
        "filename": "pic.jpg",
        "size_bytes": image.stat().st_size,
        "media_type": "image",
        "processing_status": "ready",
        "created_at": "2026-10-05 12:00:00",
    }
    session = FakeSession(_posts_handler(assets=[existing]))
    result = sm.publish_via_sociamonials(
        platform="instagram",
        account=_Account(72, platform="instagram"),
        payload={
            "draft": {"message": "A photo"},
            "artifacts": [
                {"local_path": str(image), "public_url": "", "metadata": {"role": "image"}}
            ],
        },
        target_id=55,
        api_key="sm_agent_x",
        session=session,
        delivery_timeout=0,
    )
    assert result["ok"] is True
    assert not session.calls_for("POST", "/media/uploads"), "must not re-upload"
    body = session.calls_for("POST", "/api/v1/posts")[0][2]["json"]
    assert body["image_urls"] == ["asset://555"]


def test_publish_reuses_an_identical_video_asset(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"v" * 64)
    existing = {
        "asset_id": 556,
        "filename": "clip.mp4",
        "size_bytes": video.stat().st_size,
        "media_type": "video",
        "processing_status": "ready",
        "created_at": "2026-10-05 12:00:00",
    }
    session = FakeSession(_posts_handler(assets=[existing]))
    sm.publish_via_sociamonials(
        platform="twitter",
        account=_Account(77),
        payload={
            "draft": {"message": "video post"},
            "artifacts": [
                {"local_path": str(video), "public_url": "", "metadata": {"role": "video"}}
            ],
        },
        api_key="sm_agent_x",
        session=session,
        delivery_timeout=0,
    )
    assert not session.calls_for("POST", "/media/uploads")
    body = session.calls_for("POST", "/api/v1/posts")[0][2]["json"]
    assert body["video_url"] == "asset://556"


def test_publish_uploads_when_the_match_size_differs(tmp_path):
    """Same filename but a different size is a different file: upload it."""
    image = tmp_path / "pic.jpg"
    image.write_bytes(b"\xff\xd8\xff" + b"0" * 32)
    existing = {
        "asset_id": 555,
        "filename": "pic.jpg",
        "size_bytes": image.stat().st_size + 1,
        "media_type": "image",
        "processing_status": "ready",
    }
    session = FakeSession(_posts_handler(assets=[existing]))
    sm.publish_via_sociamonials(
        platform="instagram",
        account=_Account(72, platform="instagram"),
        payload={
            "draft": {"message": "A photo"},
            "artifacts": [
                {"local_path": str(image), "public_url": "", "metadata": {"role": "image"}}
            ],
        },
        target_id=55,
        api_key="sm_agent_x",
        session=session,
        delivery_timeout=0,
    )
    assert session.calls_for("POST", "/media/uploads")


def test_publish_uploads_when_library_listing_is_not_entitled(tmp_path, monkeypatch):
    """A plan without library browsing must still be able to publish."""
    monkeypatch.setenv(sm.REUSE_ASSETS_ENV, "1")
    image = tmp_path / "pic.jpg"
    image.write_bytes(b"\xff\xd8\xff" + b"0" * 32)

    def handler(method, url, kwargs):
        if method == "GET" and url.rstrip("/").endswith("/media/assets"):
            return FakeResponse(403, {"error": {"code": "asset_access_not_enabled"}})
        if method == "GET" and "/media/assets/" in url:
            return FakeResponse(200, {"asset_id": 77, "processing_status": "ready"})
        if method == "POST" and url.endswith("/media/uploads"):
            return FakeResponse(
                200,
                {"upload_id": 5, "mode": "single", "upload_url": "https://upload.example/put/5"},
            )
        if method == "PUT":
            return FakeResponse(200, headers={"ETag": "e"})
        if method == "POST" and url.endswith("/complete"):
            return FakeResponse(200, {"asset_id": 77})
        if method == "POST" and url.endswith("/api/v1/posts"):
            return FakeResponse(200, {"post_id": 1, "status": "published"})
        raise AssertionError(f"unexpected {method} {url}")

    session = FakeSession(handler)
    sm.publish_via_sociamonials(
        platform="instagram",
        account=_Account(72, platform="instagram"),
        payload={
            "draft": {"message": "A photo"},
            "artifacts": [
                {"local_path": str(image), "public_url": "", "metadata": {"role": "image"}}
            ],
        },
        target_id=55,
        api_key="sm_agent_x",
        session=session,
        delivery_timeout=0,
    )
    assert session.calls_for("POST", "/media/uploads")


def test_publish_can_disable_reuse(tmp_path, monkeypatch):
    monkeypatch.setenv(sm.REUSE_ASSETS_ENV, "0")
    image = tmp_path / "pic.jpg"
    image.write_bytes(b"\xff\xd8\xff" + b"0" * 32)
    existing = {
        "asset_id": 555,
        "filename": "pic.jpg",
        "size_bytes": image.stat().st_size,
        "media_type": "image",
        "processing_status": "ready",
    }
    session = FakeSession(_posts_handler(assets=[existing]))
    sm.publish_via_sociamonials(
        platform="instagram",
        account=_Account(72, platform="instagram"),
        payload={
            "draft": {"message": "A photo"},
            "artifacts": [
                {"local_path": str(image), "public_url": "", "metadata": {"role": "image"}}
            ],
        },
        target_id=55,
        api_key="sm_agent_x",
        session=session,
        delivery_timeout=0,
    )
    assert session.calls_for("POST", "/media/uploads")


def test_select_reusable_asset_requires_exact_name_size_and_kind():
    assets = [
        {"asset_id": 1, "filename": "a.mp4", "size_bytes": 10, "media_type": "video"},
        {"asset_id": 2, "filename": "a.mp4", "size_bytes": 11, "media_type": "video"},
        {"asset_id": 3, "filename": "a.mp4", "size_bytes": 10, "media_type": "image"},
        {"asset_id": 4, "filename": "b.mp4", "size_bytes": 10, "media_type": "video"},
    ]
    # asset 1 is the only exact name/size/kind match.
    assert sm.select_reusable_asset(
        assets, filename="a.mp4", size_bytes=10, kind="video"
    )["asset_id"] == 1
    # asset 3 is the wrong kind, 2 the wrong size, 4 the wrong name: nothing.
    assert sm.select_reusable_asset(
        [assets[1], assets[2], assets[3]], filename="a.mp4", size_bytes=10, kind="video"
    ) is None


def test_select_reusable_asset_prefers_starred_then_oldest_and_skips_unready():
    assets = [
        {"asset_id": 10, "filename": "a.mp4", "size_bytes": 10, "media_type": "video",
         "processing_status": "ready", "created_at": "2026-10-05 01:00:00"},
        {"asset_id": 11, "filename": "a.mp4", "size_bytes": 10, "media_type": "video",
         "processing_status": "ready", "created_at": "2026-10-04 01:00:00"},
        {"asset_id": 12, "filename": "a.mp4", "size_bytes": 10, "media_type": "video",
         "processing_status": "ready", "created_at": "2026-10-06 01:00:00", "starred": True},
        {"asset_id": 13, "filename": "a.mp4", "size_bytes": 10, "media_type": "video",
         "processing_status": "processing", "created_at": "2026-10-01 01:00:00"},
    ]
    chosen = sm.select_reusable_asset(
        assets, filename="a.mp4", size_bytes=10, kind="video"
    )
    assert chosen["asset_id"] == 12  # starred wins
    # With no starred copy, the oldest ready upload is chosen.
    chosen = sm.select_reusable_asset(
        assets[:2], filename="a.mp4", size_bytes=10, kind="video"
    )
    assert chosen["asset_id"] == 11


def test_select_reusable_asset_respects_exclude_ids():
    assets = [
        {"asset_id": 1, "filename": "a.mp4", "size_bytes": 10, "media_type": "video"},
        {"asset_id": 2, "filename": "a.mp4", "size_bytes": 10, "media_type": "video"},
    ]
    chosen = sm.select_reusable_asset(
        assets, filename="a.mp4", size_bytes=10, kind="video", exclude_ids={1}
    )
    assert chosen["asset_id"] == 2


def test_publish_multipart_upload_keeps_etags(tmp_path):
    video = tmp_path / "big.mp4"
    video.write_bytes(b"v" * (100 + 16))
    grant = {
        "upload_id": 9,
        "mode": "multipart",
        "part_size_bytes": 64,
        "parts": [{"part_number": 1, "url": "https://upload.example/part/1"}],
    }
    handler = _posts_handler(grants={"big.mp4": grant}, upload_etag="etag-part")
    session = FakeSession(handler)
    payload = {
        "draft": {"message": "big video"},
        "artifacts": [{"local_path": str(video), "public_url": "", "metadata": {"role": "video"}}],
    }
    sm.publish_via_sociamonials(
        platform="twitter",
        account=_Account(77),
        payload=payload,
        api_key="sm_agent_x",
        session=session,
    )
    puts = session.calls_for("PUT")
    assert len(puts) == 2  # part 1 from grant, part 2 fetched
    complete = session.calls_for("POST", "/complete")[0][2]["json"]
    assert sorted(part["part_number"] for part in complete["parts"]) == [1, 2]


def test_publish_raises_on_http_error():
    def handler(method, url, kwargs):
        if method == "POST" and url.endswith("/api/v1/posts"):
            return FakeResponse(422, {"error": {"code": "validation_failed"}})
        return FakeResponse(200, {})

    session = FakeSession(handler)
    with pytest.raises(sm.SociamonialsFallbackError):
        sm.publish_via_sociamonials(
            platform="twitter",
            account=_Account(77),
            payload={"draft": {"message": "hello"}},
            api_key="sm_agent_x",
            session=session,
        )


def test_media_required_network_without_media_raises():
    session = FakeSession(_posts_handler())
    with pytest.raises(sm.SociamonialsFallbackError):
        sm.publish_via_sociamonials(
            platform="youtube",
            account=_Account(110, platform="youtube"),
            payload={"draft": {"message": "no media"}},
            api_key="sm_agent_x",
            session=session,
        )


def test_publish_propagates_warnings_and_approval_hold():
    session = FakeSession(
        _posts_handler(
            post_result={
                "post_id": 1,
                "status": "pending_approval",
                "requires_approval": True,
                "warnings": ["cta_group not found"],
            }
        )
    )
    result = sm.publish_via_sociamonials(
        platform="twitter",
        account=_Account(77),
        payload={"draft": {"message": "held"}},
        api_key="sm_agent_x",
        session=session,
    )
    assert result["requires_approval"] is True
    assert "cta_group not found" in result["warnings"]


# --- Teaching connector (separate workspace / key) ----------------------- #


def test_teaching_accounts_use_the_teaching_workspace():
    for account_id in (43, 58, 61, 100, 107, 108, 125):
        mapping = sm.resolve_mapping(account_id)
        assert mapping is not None, account_id
        assert mapping["workspace_id"] == sm.DEFAULT_TEACHING_WORKSPACE_ID, account_id
        assert mapping["api_key_env"] == sm.TEACHING_API_KEY_ENV, account_id


def test_adult_accounts_do_not_carry_a_workspace_override():
    mapping = sm.resolve_mapping(103)
    assert mapping == {"network": "tw", "profile_refs": ["14099"], "name": "nakedhappylife"}
    assert "workspace_id" not in mapping
    assert "api_key_env" not in mapping


def test_resolve_credentials_prefers_the_mapping_key_and_workspace(monkeypatch):
    monkeypatch.setenv(sm.API_KEY_ENV, "main_key")
    monkeypatch.setenv(sm.TEACHING_API_KEY_ENV, "teach_key")
    monkeypatch.setenv(sm.TEACHING_WORKSPACE_ENV, "34293")
    monkeypatch.setenv(sm.WORKSPACE_ENV, "26985")
    mapping = sm.resolve_mapping(100)
    key, ws = sm.resolve_credentials(mapping)
    assert key == "teach_key"
    assert ws == "34293"
    # An adult mapping still uses the global key/workspace.
    key2, ws2 = sm.resolve_credentials(sm.resolve_mapping(103))
    assert key2 == "main_key"
    assert ws2 == "26985"


def test_publish_uses_the_teaching_workspace(monkeypatch):
    monkeypatch.setenv(sm.TEACHING_API_KEY_ENV, "teach_key")
    monkeypatch.setenv(sm.TEACHING_WORKSPACE_ENV, "34293")
    seen = {}

    def handler(method, url, kwargs):
        if method == "POST" and url == sm.POSTS_URL:
            seen["auth"] = (kwargs.get("headers") or {}).get("Authorization")
            seen["body"] = kwargs.get("json") or {}
            return FakeResponse(200, {"post_id": 9, "status": "published"})
        return FakeResponse(200, {"results": [{"status": "published"}]})

    # The media upload must not fire for a text-only teaching post.
    session = FakeSession(handler)
    result = sm.publish_via_sociamonials(
        platform="twitter",
        account=_Account(107),
        payload={"draft": {"message": "teaching copy"}},
        session=session,
        delivery_timeout=0,
    )
    assert result["post_id"] == 9
    assert seen["auth"] == "Bearer teach_key"
    assert seen["body"]["workspace_registration_id"] == 34293
    assert seen["body"]["networks"] == {"tw": {"profile_refs": ["16087"]}}


class _FakeLibraryResponse:
    def __init__(self, assets):
        self.status_code = 200
        self._assets = assets

    def json(self):
        return {"assets": self._assets}


class _FakeLibrarySession:
    """Records every page request so pagination is observable."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.requests: list[dict] = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.requests.append(dict(params or {}))
        index = int((params or {}).get("offset", 0)) // int((params or {}).get("limit", 200))
        if index < len(self._pages):
            return _FakeLibraryResponse(self._pages[index])
        return _FakeLibraryResponse([])


def test_reuse_lookup_paginates_past_the_first_page(monkeypatch):
    """A match on page 2 must be found, not re-uploaded.

    The lookup is a single page of 200 by original design. Once the library grew
    past one page the match was missed and the file re-uploaded, re-inflating the
    quota this lookup exists to protect.
    """
    monkeypatch.setenv("SAU_SOCIAMONIALS_REUSE_ASSETS", "1")
    page_one = [
        {"asset_id": i, "filename": f"other{i}.mp4", "size_bytes": 1, "media_type": "video"}
        for i in range(200)
    ]
    page_two = [
        {"asset_id": 999, "filename": "wanted.mp4", "size_bytes": 10, "media_type": "video"}
    ]
    session = _FakeLibrarySession([page_one, page_two])
    found = sm._lookup_reusable_asset(
        session, {}, "26985", filename="wanted.mp4", size_bytes=10,
        kind="video", timeout=5,
    )
    assert found is not None and found["asset_id"] == 999
    assert len(session.requests) == 2, "must have walked to the second page"
    assert session.requests[1]["offset"] == 200


def test_reuse_lookup_stops_on_a_short_page(monkeypatch):
    monkeypatch.setenv("SAU_SOCIAMONIALS_REUSE_ASSETS", "1")
    session = _FakeLibrarySession([[
        {"asset_id": 1, "filename": "other.mp4", "size_bytes": 1, "media_type": "video"}
    ]])
    assert sm._lookup_reusable_asset(
        session, {}, "26985", filename="missing.mp4", size_bytes=9,
        kind="video", timeout=5,
    ) is None
    # A page shorter than the page size is the last page: do not keep asking.
    assert len(session.requests) == 1


def test_reuse_lookup_is_bounded(monkeypatch):
    monkeypatch.setenv("SAU_SOCIAMONIALS_REUSE_ASSETS", "1")
    monkeypatch.setenv("SAU_SOCIAMONIALS_REUSE_MAX_PAGES", "3")
    full_page = [
        {"asset_id": i, "filename": f"o{i}.mp4", "size_bytes": 1, "media_type": "video"}
        for i in range(200)
    ]
    session = _FakeLibrarySession([full_page, full_page, full_page, full_page])
    assert sm._lookup_reusable_asset(
        session, {}, "26985", filename="missing.mp4", size_bytes=9,
        kind="video", timeout=5,
    ) is None
    assert len(session.requests) == 3, "must respect the page bound"
