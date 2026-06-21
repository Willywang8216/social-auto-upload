"""Tests for the share/tgstate uploader and the remote-storage dispatcher."""

from pathlib import Path

import pytest

from myUtils import media_remote_storage, share_storage
from myUtils.rclone_storage import RemoteArtifact


@pytest.fixture
def media_file(tmp_path):
    path = tmp_path / "demo.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 64)
    return path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "SAU_SHARE_ENABLED",
        "SAU_SHARE_SESSION_TOKEN",
        "SAU_SHARE_PUBLIC_BASE",
        "SAU_SHARE_UPLOAD_BASE",
        "SAU_STORAGE_BACKENDS",
        "DO_SPACES_BUCKET",
        "DO_SPACES_KEY",
        "DO_SPACES_SECRET",
        "SAU_DEFAULT_RCLONE_REMOTE",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


class _Resp:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Session:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._resp


# --------------------------------------------------------------------------- #
# share_storage
# --------------------------------------------------------------------------- #

def test_share_is_configured_requires_enabled_and_token(monkeypatch):
    assert share_storage.is_configured() is False
    monkeypatch.setenv("SAU_SHARE_ENABLED", "1")
    assert share_storage.is_configured() is False  # still no token
    monkeypatch.setenv("SAU_SHARE_SESSION_TOKEN", "tok")
    assert share_storage.is_configured() is True


def test_share_upload_prefers_short_id_for_public_url(monkeypatch, media_file):
    monkeypatch.setenv("SAU_SHARE_ENABLED", "1")
    monkeypatch.setenv("SAU_SHARE_SESSION_TOKEN", "tok")
    monkeypatch.setenv("SAU_SHARE_PUBLIC_BASE", "https://share.example.com")
    session = _Session(_Resp({"status": "ok", "data": {"short_id": "abc", "file_id": "LONG"}}))

    artifact = share_storage.upload_artifact(media_file, campaign_id=3, session=session)

    assert artifact.public_url == "https://share.example.com/d/abc"
    assert artifact.remote_name == "share"
    assert artifact.remote_path == "abc"
    # the session cookie must carry the configured token
    assert session.calls[0][1]["cookies"][share_storage.SESSION_COOKIE] == "tok"


def test_share_upload_falls_back_to_file_id(monkeypatch, media_file):
    monkeypatch.setenv("SAU_SHARE_ENABLED", "1")
    monkeypatch.setenv("SAU_SHARE_SESSION_TOKEN", "tok")
    monkeypatch.setenv("SAU_SHARE_PUBLIC_BASE", "https://share.example.com")
    session = _Session(_Resp({"file_id": "FID"}))

    artifact = share_storage.upload_artifact(media_file, campaign_id=3, session=session)

    assert artifact.public_url == "https://share.example.com/d/FID"


def test_share_upload_raises_on_error_payload(monkeypatch, media_file):
    monkeypatch.setenv("SAU_SHARE_ENABLED", "1")
    monkeypatch.setenv("SAU_SHARE_SESSION_TOKEN", "tok")
    session = _Session(_Resp({"status": "error", "detail": {"message": "upload failed"}}))

    with pytest.raises(share_storage.ShareUploadError):
        share_storage.upload_artifact(media_file, campaign_id=3, session=session)


def test_share_upload_raises_on_http_error(monkeypatch, media_file):
    monkeypatch.setenv("SAU_SHARE_ENABLED", "1")
    monkeypatch.setenv("SAU_SHARE_SESSION_TOKEN", "tok")
    session = _Session(_Resp(None, status_code=401, text="unauthorized"))

    with pytest.raises(share_storage.ShareUploadError):
        share_storage.upload_artifact(media_file, campaign_id=3, session=session)


def test_share_upload_requires_configuration(media_file):
    with pytest.raises(share_storage.ShareUploadError):
        share_storage.upload_artifact(media_file, campaign_id=1)


# --------------------------------------------------------------------------- #
# media_remote_storage dispatcher
# --------------------------------------------------------------------------- #

def _fake_spaces_client(monkeypatch, url_prefix="https://cdn.example"):
    class FakeClient:
        def upload_file(self, path, key, content_type=""):
            return f"{url_prefix}/{key}"

    monkeypatch.setattr(media_remote_storage.do_spaces, "client_from_env", lambda: FakeClient())


def test_dispatch_prefers_first_configured_backend(monkeypatch, media_file):
    monkeypatch.setenv("SAU_STORAGE_BACKENDS", "share,do_spaces,rclone")
    monkeypatch.setenv("SAU_SHARE_ENABLED", "1")
    monkeypatch.setenv("SAU_SHARE_SESSION_TOKEN", "tok")
    artifact = RemoteArtifact(
        local_path=str(media_file), remote_name="share", remote_path="x",
        public_url="https://share.example/d/x",
    )
    monkeypatch.setattr(share_storage, "upload_artifact", lambda *a, **k: artifact)

    out = media_remote_storage.upload_artifact(media_file, campaign_id=1)
    assert out.remote_name == "share"


def test_dispatch_falls_back_when_share_fails(monkeypatch, media_file):
    monkeypatch.setenv("SAU_STORAGE_BACKENDS", "share,do_spaces")
    monkeypatch.setenv("SAU_SHARE_ENABLED", "1")
    monkeypatch.setenv("SAU_SHARE_SESSION_TOKEN", "tok")
    monkeypatch.setenv("DO_SPACES_BUCKET", "b")
    monkeypatch.setenv("DO_SPACES_KEY", "k")
    monkeypatch.setenv("DO_SPACES_SECRET", "s")

    def boom(*a, **k):
        raise share_storage.ShareUploadError("dead bot token")

    monkeypatch.setattr(share_storage, "upload_artifact", boom)
    _fake_spaces_client(monkeypatch)

    out = media_remote_storage.upload_artifact(media_file, campaign_id=7, artifact_subdir="videos")
    assert out.remote_name == "do_spaces"
    assert out.public_url == "https://cdn.example/campaigns/7/videos/demo.mp4"


def test_dispatch_raises_when_all_backends_fail(monkeypatch, media_file):
    monkeypatch.setenv("SAU_STORAGE_BACKENDS", "share")
    monkeypatch.setenv("SAU_SHARE_ENABLED", "1")
    monkeypatch.setenv("SAU_SHARE_SESSION_TOKEN", "tok")

    def boom(*a, **k):
        raise share_storage.ShareUploadError("nope")

    monkeypatch.setattr(share_storage, "upload_artifact", boom)
    with pytest.raises(media_remote_storage.RemoteStorageError):
        media_remote_storage.upload_artifact(media_file, campaign_id=1)


def test_dispatch_raises_when_no_backend_configured(media_file):
    with pytest.raises(media_remote_storage.RemoteStorageError):
        media_remote_storage.upload_artifact(media_file, campaign_id=1)


def test_dispatch_rejects_non_https_url(monkeypatch, media_file):
    """A backend that returns an unreachable localhost URL must be rejected,
    never propagated — this is the regression guard for the original bug."""
    monkeypatch.setenv("SAU_STORAGE_BACKENDS", "do_spaces")
    monkeypatch.setenv("DO_SPACES_BUCKET", "b")
    monkeypatch.setenv("DO_SPACES_KEY", "k")
    monkeypatch.setenv("DO_SPACES_SECRET", "s")
    _fake_spaces_client(monkeypatch, url_prefix="http://localhost:5409/getFile?filename=")

    with pytest.raises(media_remote_storage.RemoteStorageError):
        media_remote_storage.upload_artifact(media_file, campaign_id=1)


def test_is_any_backend_configured(monkeypatch):
    assert media_remote_storage.is_any_backend_configured() is False
    monkeypatch.setenv("DO_SPACES_BUCKET", "b")
    monkeypatch.setenv("DO_SPACES_KEY", "k")
    monkeypatch.setenv("DO_SPACES_SECRET", "s")
    assert media_remote_storage.is_any_backend_configured() is True
    assert "do_spaces" in media_remote_storage.configured_backends()
