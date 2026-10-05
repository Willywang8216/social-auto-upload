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

    def calls_for(self, method, contains=""):
        return [c for c in self.calls if c[0] == method and contains in c[1]]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        sm.ENABLED_ENV,
        sm.API_KEY_ENV,
        sm.WORKSPACE_ENV,
        sm.SECRETS_FILE_ENV,
        sm.TIMEOUT_ENV,
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


def _posts_handler(*, post_result=None, grants=None, upload_etag="etag-1"):
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

    def handler(method, url, kwargs):
        if method == "HEAD":
            return FakeResponse(200, headers={"Content-Type": "video/mp4"})
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
