"""Phase 2 regressions: safe media serving and one outbound-fetch policy.

Synthetic only — no network. DNS is stubbed so the SSRF checks are exercised
deterministically rather than depending on the sandbox's resolver.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest


# --------------------------------------------------------------------------- #
# Serving policy (pure)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "name,inline",
    [("clip.mp4", True), ("photo.JPG", True), ("a.webm", True), ("song.mp3", True),
     ("page.html", False), ("vector.svg", False), ("script.js", False),
     ("payload.exe", False), ("archive.zip", False), ("noextension", False),
     ("x.html.mp4", True)],
)
def test_inline_serving_is_allow_listed(name, inline):
    from myUtils.media_serving import should_serve_inline

    assert should_serve_inline(name) is inline


def test_active_content_gets_an_opaque_type_and_attachment():
    from myUtils.media_serving import content_type_for, disposition_for

    assert content_type_for("page.html") == "application/octet-stream"
    assert content_type_for("vector.svg") == "application/octet-stream"
    assert disposition_for("page.html") == "attachment"
    assert content_type_for("clip.mp4") == "video/mp4"
    assert disposition_for("clip.mp4") == "inline"


def test_svg_is_not_treated_as_a_safe_image():
    """The browser mimetypes db maps .svg under image/, but it can run script."""
    from myUtils.media_serving import guessed_type, should_serve_inline

    assert guessed_type("vector.svg").startswith("image/")
    assert should_serve_inline("vector.svg") is False


# --------------------------------------------------------------------------- #
# /getFile integration
# --------------------------------------------------------------------------- #

@pytest.fixture
def env(tmp_path, monkeypatch):
    from db.createTable import bootstrap
    import sau_backend as backend
    from myUtils.security import SecurityPolicy

    db = tmp_path / "sau.db"
    bootstrap(db)
    for name in ("videoFile", "cookiesFile", "cookies", "uploads", "generated"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("SAU_DB_PATH", str(db))
    monkeypatch.setattr(backend, "BASE_DIR", tmp_path)
    monkeypatch.setattr(backend, "_current_db_path", lambda: db)
    monkeypatch.setitem(
        backend.app.config, "SECURITY_POLICY",
        SecurityPolicy(tokens=frozenset(), cors_origins=()),
    )
    monkeypatch.setitem(backend.app.config, "TESTING", True)
    return SimpleNamespace(
        db=db, backend=backend, client=backend.app.test_client(), root=tmp_path,
    )


def test_getfile_does_not_serve_uploaded_html_inline(env):
    (env.root / "videoFile" / "evil.html").write_text("<script>alert(1)</script>")
    response = env.client.get("/getFile?filename=evil.html")

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("application/octet-stream")
    assert "attachment" in response.headers.get("Content-Disposition", "")
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_getfile_still_serves_real_media_inline(env):
    payload = b"\x00\x00\x00\x18ftypmp42synthetic"
    (env.root / "videoFile" / "clip.mp4").write_bytes(payload)
    response = env.client.get("/getFile?filename=clip.mp4")

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("video/mp4")
    assert "attachment" not in response.headers.get("Content-Disposition", "")
    assert response.get_data() == payload


def test_getfile_serves_svg_as_a_download(env):
    (env.root / "videoFile" / "vector.svg").write_text("<svg onload='x()'></svg>")
    response = env.client.get("/getFile?filename=vector.svg")

    assert response.headers["Content-Type"].startswith("application/octet-stream")
    assert "attachment" in response.headers.get("Content-Disposition", "")


# --------------------------------------------------------------------------- #
# Outbound fetch policy
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "url",
    ["http://example.com/a.mp4",           # not https
     "https://localhost/a.mp4",            # loopback name
     "https://user:pw@example.com/a.mp4",  # embedded credentials
     "https://example.com:8443/a.mp4",     # non-default port
     "https:///a.mp4",                     # no host
     "ftp://example.com/a.mp4"],
)
def test_unsafe_urls_are_refused_before_any_request(url):
    from myUtils.outbound_http import OutboundUrlError, assert_public_https_url

    with pytest.raises(OutboundUrlError):
        assert_public_https_url(url)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "192.168.1.10",
                                     "169.254.169.254", "::1", "fd00::1"])
def test_private_and_link_local_addresses_are_refused(monkeypatch, address):
    from myUtils import outbound_http

    monkeypatch.setattr(
        outbound_http.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", (address, 443))],
    )
    with pytest.raises(outbound_http.OutboundUrlError):
        outbound_http.assert_public_https_url("https://storage.example.com/a.mp4")


def test_a_name_with_a_private_record_among_public_ones_is_refused(monkeypatch):
    from myUtils import outbound_http

    monkeypatch.setattr(
        outbound_http.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443)),
                         (2, 1, 6, "", ("10.1.2.3", 443))],
    )
    with pytest.raises(outbound_http.OutboundUrlError):
        outbound_http.assert_public_https_url("https://storage.example.com/a.mp4")


def test_public_https_is_allowed(monkeypatch):
    from myUtils import outbound_http

    monkeypatch.setattr(
        outbound_http.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    url = "https://storage.example.com/a.mp4"
    assert outbound_http.assert_public_https_url(url) == url


class _FakeResponse:
    def __init__(self, *, status=200, headers=None, body=b"", redirect_to=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self._redirect_to = redirect_to

    @property
    def is_redirect(self):
        return self._redirect_to is not None

    @property
    def is_permanent_redirect(self):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=0):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requested = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return self._responses.pop(0)


def _allow_public(monkeypatch):
    from myUtils import outbound_http

    monkeypatch.setattr(
        outbound_http.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )


def test_fetch_enforces_the_byte_limit(monkeypatch):
    from myUtils import outbound_http

    _allow_public(monkeypatch)
    session = _FakeSession([_FakeResponse(body=b"x" * 2048)])
    with pytest.raises(outbound_http.OutboundUrlError):
        outbound_http.fetch_public_bytes(
            "https://storage.example.com/a.mp4", max_bytes=1024, session=session,
        )


def test_fetch_rejects_an_oversized_content_length(monkeypatch):
    from myUtils import outbound_http

    _allow_public(monkeypatch)
    session = _FakeSession([_FakeResponse(headers={"Content-Length": "999999"}, body=b"x")])
    with pytest.raises(outbound_http.OutboundUrlError):
        outbound_http.fetch_public_bytes(
            "https://storage.example.com/a.mp4", max_bytes=1024, session=session,
        )


def test_fetch_revalidates_every_redirect_hop(monkeypatch):
    """A public first hop must not be able to redirect to a private one."""
    from myUtils import outbound_http

    def resolver(host, *args, **kwargs):
        if host == "evil.example.com":
            return [(2, 1, 6, "", ("127.0.0.1", 443))]
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(outbound_http.socket, "getaddrinfo", resolver)
    session = _FakeSession([
        _FakeResponse(status=302, headers={"Location": "https://evil.example.com/x"}, body=b""),
    ])
    with pytest.raises(outbound_http.OutboundUrlError):
        outbound_http.fetch_public_bytes(
            "https://storage.example.com/a.mp4", session=session,
        )
    assert session.requested == ["https://storage.example.com/a.mp4"]


def test_fetch_rejects_an_empty_object(monkeypatch):
    from myUtils import outbound_http

    _allow_public(monkeypatch)
    session = _FakeSession([_FakeResponse(body=b"")])
    with pytest.raises(outbound_http.OutboundUrlError):
        outbound_http.fetch_public_bytes("https://storage.example.com/a.mp4", session=session)


def test_download_public_file_refuses_loopback(monkeypatch, tmp_path):
    """The share-backend restore path must not fetch a private host."""
    from myUtils import media_remote_storage, outbound_http

    monkeypatch.setattr(
        outbound_http.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(media_remote_storage.RemoteStorageError):
        media_remote_storage.download_public_url(
            "https://localhost/secret.mp4", tmp_path / "out.mp4",
        )
    assert not (tmp_path / "out.mp4").exists()


def test_download_public_file_writes_verified_bytes(monkeypatch, tmp_path):
    from myUtils import outbound_http

    _allow_public(monkeypatch)
    session = _FakeSession([_FakeResponse(body=b"payload-bytes")])
    target = outbound_http.download_public_file(
        "https://storage.example.com/a.mp4", tmp_path / "out.mp4", session=session,
    )
    assert target.read_bytes() == b"payload-bytes"
    assert not list(tmp_path.glob(".*.part"))


# --------------------------------------------------------------------------- #
# Upload registration must verify, not trust
# --------------------------------------------------------------------------- #

_KEY = "uploads/11111111-2222-3333-4444-555555555555_clip.mp4"


class _HeadClient:
    bucket = "synthetic-bucket"

    def __init__(self, *, size=None, error=None):
        self._size = size
        self._error = error

    def _get_client(self):
        client = self

        class _S3:
            def head_object(self, **_kwargs):
                if client._error is not None:
                    raise client._error
                return {"ContentLength": client._size}

        return _S3()

    def cdn_url_for(self, key):
        return "https://derived.invalid/" + key


def _patch_storage(monkeypatch, *, backend=True, size=2048, error=None):
    from myUtils import do_spaces
    import sau_backend as backend_module

    monkeypatch.setattr(
        backend_module, "_load_default_storage_backend",
        lambda **_k: ({"provider": "do_spaces"} if backend else None),
    )
    monkeypatch.setattr(
        do_spaces, "client_from_row",
        lambda _row: _HeadClient(size=size, error=error),
    )


def test_upload_register_refuses_when_the_object_cannot_be_verified(env, monkeypatch):
    """A client's public_url/size are assertions; the bucket is the evidence."""
    _patch_storage(monkeypatch, error=RuntimeError("NoSuchKey"))
    response = env.client.post("/upload/register", json={
        "filename": "clip.mp4", "key": _KEY,
        "public_url": "https://attacker.invalid/x.mp4", "size": 999999,
    })
    assert response.status_code == 400
    with sqlite3.connect(env.db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM file_records").fetchone()[0] == 0


def test_upload_register_refuses_without_a_storage_backend(env, monkeypatch):
    _patch_storage(monkeypatch, backend=False)
    response = env.client.post(
        "/upload/register", json={"filename": "clip.mp4", "key": _KEY}
    )
    assert response.status_code == 400


def test_upload_register_records_the_verified_size_and_derived_url(env, monkeypatch):
    _patch_storage(monkeypatch, size=2 * 1024 * 1024)
    response = env.client.post("/upload/register", json={
        "filename": "clip.mp4", "key": _KEY,
        "public_url": "https://attacker.invalid/x.mp4", "size": 1,
    })
    assert response.status_code == 200
    assert response.get_json()["data"]["size"] == 2 * 1024 * 1024
    with sqlite3.connect(env.db) as conn:
        row = conn.execute(
            "SELECT filename, filesize, storage_cdn_url FROM file_records"
        ).fetchone()
    assert row[0] == "clip.mp4"
    assert row[1] == 2.0
    assert row[2] == "https://derived.invalid/" + _KEY
    assert "attacker.invalid" not in (row[2] or "")


def test_upload_register_rejects_an_empty_object(env, monkeypatch):
    _patch_storage(monkeypatch, size=0)
    response = env.client.post(
        "/upload/register", json={"filename": "clip.mp4", "key": _KEY}
    )
    assert response.status_code == 400


def test_upload_register_still_rejects_an_invalid_key(env, monkeypatch):
    _patch_storage(monkeypatch)
    response = env.client.post(
        "/upload/register", json={"filename": "clip.mp4", "key": "../../etc/passwd"}
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# Bounded read paths (no unbounded full-table fetch)
# --------------------------------------------------------------------------- #

def _seed_files(env, count):
    with sqlite3.connect(env.db) as conn:
        conn.executemany(
            "INSERT INTO file_records (filename, filesize, file_path) VALUES (?, ?, ?)",
            [(f"c{i}.mp4", 10, f"uploads/{i:036d}_c{i}.mp4") for i in range(count)],
        )
        conn.commit()


def test_getfiles_is_bounded_and_reports_the_total(env):
    """The media list pages in SQL instead of returning the whole table."""
    _seed_files(env, 25)
    response = env.client.get("/getFiles?limit=10&offset=0")
    assert response.status_code == 200
    body = response.get_json()
    assert len(body["data"]) == 10
    assert body["total"] == 25
    assert body["limit"] == 10
    assert body["hasMore"] is True
    assert body["data"][0]["filename"] == "c24.mp4"


def test_getfiles_second_page_has_no_more(env):
    _seed_files(env, 25)
    body = env.client.get("/getFiles?limit=20&offset=20").get_json()
    assert len(body["data"]) == 5
    assert body["hasMore"] is False


def test_getfiles_caps_an_absurd_limit(env):
    import sau_backend as backend

    body = env.client.get("/getFiles?limit=999999").get_json()
    assert body["limit"] == backend._FILES_MAX_LIMIT


def test_getfiles_rejects_a_non_integer_limit(env):
    assert env.client.get("/getFiles?limit=abc").status_code == 400


def test_publish_entity_candidates_are_sql_bounded(env):
    """The entity list bounds its candidate scan in SQL, not after loading."""
    import sau_backend as backend

    assert 0 < backend._ENTITY_JOB_SCAN_LIMIT <= 5000
    assert 0 < backend._ENTITY_CAMPAIGN_SCAN_LIMIT <= 5000
    with sqlite3.connect(env.db) as conn:
        conn.executemany(
            "INSERT INTO publish_jobs (idempotency_key, platform, payload_json, status,"
            " total_targets, completed_targets, failed_targets)"
            " VALUES (?, 'facebook', '{}', 'pending', 1, 0, 0)",
            [(f"bounded-{i}",) for i in range(backend._ENTITY_JOB_SCAN_LIMIT + 25)],
        )
        conn.commit()

    jobs = backend._load_publish_entity_jobs(db_path=env.db, workspace_id=None)

    assert 0 < len(jobs) <= backend._ENTITY_JOB_SCAN_LIMIT


def test_fetch_rejects_a_truncated_body(monkeypatch):
    """A short body must not be accepted as a complete object.

    The pre-rewrite implementation compared the received byte count against
    Content-Length; a rewrite of this function must not lose that check.
    """
    from myUtils import outbound_http

    _allow_public(monkeypatch)
    session = _FakeSession([
        _FakeResponse(headers={"Content-Length": "100"}, body=b"short"),
    ])
    with pytest.raises(outbound_http.OutboundUrlError):
        outbound_http.fetch_public_bytes(
            "https://storage.example.com/a.mp4", session=session,
        )


def test_fetch_accepts_an_exact_length_match(monkeypatch):
    from myUtils import outbound_http

    _allow_public(monkeypatch)
    payload = b"exactly-right"
    session = _FakeSession([
        _FakeResponse(headers={"Content-Length": str(len(payload))}, body=payload),
    ])
    assert outbound_http.fetch_public_bytes(
        "https://storage.example.com/a.mp4", session=session,
    ) == payload
