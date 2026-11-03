"""Phase 1 hardening regressions: deletion safety, security headers, rate limiting.

Synthetic only — a throwaway bootstrapped database and temp media root. No
provider calls, no production mounts, no real credentials.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A migrated throwaway database with BASE_DIR pointed at a temp tree."""
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


def _add_file(env, name="clip.mp4", content=b"synthetic-bytes"):
    media = env.root / "videoFile" / name
    media.write_bytes(content)
    with sqlite3.connect(env.db) as conn:
        cursor = conn.execute(
            "INSERT INTO file_records (filename, filesize, file_path) VALUES (?, ?, ?)",
            (name, len(content), name),
        )
        return int(cursor.lastrowid), media


def _add_profile_and_account(env, account_id=1, name="structured-row"):
    with sqlite3.connect(env.db) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO profiles (id, name, slug, description, settings_json)"
            " VALUES (1, 'P', 'p', '', '{}')"
        )
        conn.execute(
            "INSERT INTO accounts (id, profile_id, platform, account_name, cookie_path,"
            " auth_type, config_json, enabled, status, nickname, account_group)"
            " VALUES (?, 1, 'facebook', ?, '', 'oauth', '{}', 1, 1, '', '')",
            (account_id, name),
        )


def _add_target(env, *, filename="clip.mp4", status="pending", checkpoint=None,
                account_ref="account:1", payload=None):
    body = payload if payload is not None else {
        "artifacts": [{"local_path": filename, "public_url": f"https://cdn.invalid/{filename}"}]
    }
    with sqlite3.connect(env.db) as conn:
        job = conn.execute(
            "INSERT INTO publish_jobs (idempotency_key, platform, payload_json, status,"
            " total_targets, completed_targets, failed_targets) VALUES (?, 'facebook', ?,"
            " 'pending', 1, 0, 0)",
            (f"key-{filename}-{status}-{account_ref}", json.dumps(body)),
        ).lastrowid
        target = conn.execute(
            "INSERT INTO publish_job_targets (job_id, account_ref, file_ref, status,"
            " attempts, checkpoint_json) VALUES (?, ?, ?, ?, 0, ?)",
            (job, account_ref, filename, status, json.dumps(checkpoint or {})),
        ).lastrowid
        return int(job), int(target)


def _count(env, table, where, params):
    with sqlite3.connect(env.db) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]


# --------------------------------------------------------------------------- #
# Read methods must not mutate
# --------------------------------------------------------------------------- #

def test_delete_endpoints_reject_get(env):
    """A GET must never delete: the middleware treats GET as CSRF-safe."""
    file_id, media = _add_file(env)
    _add_profile_and_account(env)

    assert env.client.get(f"/deleteFile?id={file_id}").status_code == 405
    assert env.client.get("/deleteAccount?id=1").status_code == 405

    assert media.exists(), "a rejected GET must not have removed any bytes"
    assert _count(env, "file_records", "id = ?", (file_id,)) == 1
    assert _count(env, "accounts", "id = ?", (1,)) == 1


# --------------------------------------------------------------------------- #
# File deletion
# --------------------------------------------------------------------------- #

def test_delete_file_refused_while_unresolved_target_references_it(env):
    file_id, media = _add_file(env)
    _add_target(env, filename="clip.mp4", status="pending")

    response = env.client.delete(f"/deleteFile?id={file_id}")

    assert response.status_code == 409
    assert "unresolved" in response.get_json()["msg"].lower()
    assert media.exists()
    assert _count(env, "file_records", "id = ?", (file_id,)) == 1


@pytest.mark.parametrize("status", ["pending", "retrying", "running", "blocked",
                                    "awaiting_delivery", "needs_review"])
def test_every_live_target_status_blocks_file_deletion(env, status):
    file_id, media = _add_file(env)
    _add_target(env, status=status)
    assert env.client.delete(f"/deleteFile?id={file_id}").status_code == 409
    assert media.exists()


def test_terminal_target_does_not_block_file_deletion(env):
    """A succeeded/cancelled target no longer needs the media."""
    file_id, media = _add_file(env)
    _add_target(env, status="succeeded")
    response = env.client.delete(f"/deleteFile?id={file_id}")
    assert response.status_code == 200
    assert not media.exists()


def test_delete_file_removes_row_then_bytes(env):
    file_id, media = _add_file(env)

    response = env.client.delete(f"/deleteFile?id={file_id}")

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["bytesRemoved"] is True
    assert data["cleanupPending"] is False
    assert not media.exists()
    assert _count(env, "file_records", "id = ?", (file_id,)) == 0


def test_delete_file_missing_returns_404(env):
    assert env.client.delete("/deleteFile?id=9999").status_code == 404
    assert env.client.delete("/deleteFile?id=abc").status_code == 400


def test_delete_file_database_failure_leaves_bytes_and_row(env, monkeypatch):
    """The critical ordering guarantee: a DB failure destroys nothing."""
    from myUtils import asset_lifecycle

    file_id, media = _add_file(env)

    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic database failure")

    monkeypatch.setattr(asset_lifecycle, "assert_file_deletable", boom)
    response = env.client.delete(f"/deleteFile?id={file_id}")

    assert response.status_code == 500
    assert media.exists(), "bytes must survive a failed delete"
    assert _count(env, "file_records", "id = ?", (file_id,)) == 1


def test_delete_file_reports_pending_cleanup_when_bytes_remain(env, monkeypatch):
    """Bytes are removed only after the row is gone; a failure is reported."""
    import sau_backend as backend

    file_id, media = _add_file(env)
    monkeypatch.setattr(
        backend, "_remove_local_media_best_effort",
        lambda record: (False, "local bytes remain"),
    )

    response = env.client.delete(f"/deleteFile?id={file_id}")

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["cleanupPending"] is True
    assert "could not be removed" in response.get_json()["msg"]
    assert _count(env, "file_records", "id = ?", (file_id,)) == 0


def test_batch_delete_reports_in_use_items_without_deleting_them(env):
    kept_id, kept_media = _add_file(env, name="kept.mp4")
    free_id, free_media = _add_file(env, name="free.mp4")
    _add_target(env, filename="kept.mp4", status="pending")

    response = env.client.post("/deleteFiles", json={"ids": [kept_id, free_id]})

    assert response.status_code == 200
    details = response.get_json()["data"]["details"]
    assert details["succeeded"] == [free_id]
    assert details["failed"][0]["reason"] == "in_use"
    assert kept_media.exists()
    assert not free_media.exists()


# --------------------------------------------------------------------------- #
# Account deletion namespaces
# --------------------------------------------------------------------------- #

def test_account_namespace_collision_cannot_delete_the_wrong_row(env):
    """An id present in both tables must resolve in exactly one namespace."""
    _add_profile_and_account(env, account_id=1, name="structured-row")
    with sqlite3.connect(env.db) as conn:
        conn.execute(
            "INSERT INTO user_info (id, type, filePath, userName, status)"
            " VALUES (1, 'twitter', '', 'legacy-row', 1)"
        )

    response = env.client.delete("/deleteAccount?id=1&type=structured")

    assert response.status_code == 200
    assert response.get_json()["data"]["namespace"] == "structured"
    assert _count(env, "accounts", "id = ?", (1,)) == 0
    assert _count(env, "user_info", "id = ?", (1,)) == 1, "legacy row must survive"


def test_account_legacy_namespace_is_opt_in(env):
    _add_profile_and_account(env, account_id=1, name="structured-row")
    with sqlite3.connect(env.db) as conn:
        conn.execute(
            "INSERT INTO user_info (id, type, filePath, userName, status)"
            " VALUES (1, 'twitter', '', 'legacy-row', 1)"
        )

    response = env.client.delete("/deleteAccount?id=1&type=legacy")

    assert response.status_code == 200
    assert response.get_json()["data"]["namespace"] == "legacy"
    assert _count(env, "user_info", "id = ?", (1,)) == 0
    assert _count(env, "accounts", "id = ?", (1,)) == 1, "structured row must survive"


def test_account_deletion_defaults_to_structured(env):
    _add_profile_and_account(env, account_id=7, name="default-ns")
    assert env.client.delete("/deleteAccount?id=7").status_code == 200
    assert _count(env, "accounts", "id = ?", (7,)) == 0


def test_account_deletion_rejects_unknown_namespace(env):
    _add_profile_and_account(env, account_id=1)
    response = env.client.delete("/deleteAccount?id=1&type=bogus")
    assert response.status_code == 400
    assert _count(env, "accounts", "id = ?", (1,)) == 1


def test_account_deletion_refused_while_outcome_unknown(env):
    """A cancelled target that already started may have published."""
    _add_profile_and_account(env, account_id=1)
    _add_target(
        env, status="cancelled", account_ref="account:1",
        checkpoint={"submission_started": True},
    )

    response = env.client.delete("/deleteAccount?id=1&type=structured")

    assert response.status_code == 409
    assert _count(env, "accounts", "id = ?", (1,)) == 1


def test_account_deletion_refused_on_unreadable_checkpoint(env):
    """An unreadable checkpoint is not evidence that nothing was sent."""
    _add_profile_and_account(env, account_id=1)
    with sqlite3.connect(env.db) as conn:
        job = conn.execute(
            "INSERT INTO publish_jobs (idempotency_key, platform, payload_json, status,"
            " total_targets, completed_targets, failed_targets)"
            " VALUES ('bad', 'facebook', '{}', 'pending', 1, 0, 0)"
        ).lastrowid
        conn.execute(
            "INSERT INTO publish_job_targets (job_id, account_ref, file_ref, status,"
            " attempts, checkpoint_json) VALUES (?, 'account:1', 'x.mp4', 'failed', 0, ?)",
            (job, "{not valid json"),
        )

    assert env.client.delete("/deleteAccount?id=1&type=structured").status_code == 409
    assert _count(env, "accounts", "id = ?", (1,)) == 1


def test_account_deletion_removes_cookie_after_the_row(env):
    cookie_dir = env.root / "cookies" / "facebook"
    cookie_dir.mkdir(parents=True, exist_ok=True)
    cookie_file = cookie_dir / "main.json"
    cookie_file.write_text("{}")
    with sqlite3.connect(env.db) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO profiles (id, name, slug, description, settings_json)"
            " VALUES (1, 'P', 'p', '', '{}')"
        )
        conn.execute(
            "INSERT INTO accounts (id, profile_id, platform, account_name, cookie_path,"
            " auth_type, config_json, enabled, status, nickname, account_group)"
            " VALUES (1, 1, 'facebook', 'c', ?, 'cookie', '{}', 1, 1, '', '')",
            (str(cookie_file),),
        )

    response = env.client.delete("/deleteAccount?id=1&type=structured")

    assert response.status_code == 200
    assert response.get_json()["data"]["cookieRemoved"] is True
    assert not cookie_file.exists()


# --------------------------------------------------------------------------- #
# asset_lifecycle fails closed
# --------------------------------------------------------------------------- #

def test_asset_lifecycle_fails_closed_on_undecodable_payload(env):
    from myUtils import asset_lifecycle

    file_id, _ = _add_file(env)
    with sqlite3.connect(env.db) as conn:
        job = conn.execute(
            "INSERT INTO publish_jobs (idempotency_key, platform, payload_json, status,"
            " total_targets, completed_targets, failed_targets)"
            " VALUES ('undecodable', 'facebook', '{not json', 'pending', 1, 0, 0)"
        ).lastrowid
        conn.execute(
            "INSERT INTO publish_job_targets (job_id, account_ref, file_ref, status,"
            " attempts, checkpoint_json) VALUES (?, 'account:9', 'clip.mp4', 'pending', 0, '{}')",
            (job,),
        )
        conn.commit()

    with sqlite3.connect(env.db) as conn:
        conn.row_factory = sqlite3.Row
        with pytest.raises(asset_lifecycle.AssetInUse):
            asset_lifecycle.assert_file_deletable(conn, file_record_id=file_id, filename="clip.mp4")


def test_asset_lifecycle_allows_unreferenced_file(env):
    from myUtils import asset_lifecycle

    file_id, _ = _add_file(env)
    with sqlite3.connect(env.db) as conn:
        conn.row_factory = sqlite3.Row
        asset_lifecycle.assert_file_deletable(conn, file_record_id=file_id, filename="clip.mp4")


def test_resolve_account_namespace_rejects_unknown_kind(env):
    from myUtils import asset_lifecycle

    with sqlite3.connect(env.db) as conn:
        with pytest.raises(ValueError):
            asset_lifecycle.resolve_account_namespace(conn, 1, kind="bogus")


# --------------------------------------------------------------------------- #
# Security headers and rate limiting
# --------------------------------------------------------------------------- #

def test_security_headers_present_on_api_responses(env):
    response = env.client.get("/whoami")
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_csp_is_added_to_html_only():
    from flask import Flask, Response
    from sau_app.security.headers import install_security_headers

    app = Flask(__name__)

    @app.route("/html")
    def html():  # noqa: ANN202
        return Response("<html><body>hi</body></html>", mimetype="text/html")

    @app.route("/json")
    def js():  # noqa: ANN202
        return {"ok": True}

    install_security_headers(app, environ={})
    client = app.test_client()

    html_response = client.get("/html")
    assert "Content-Security-Policy" in html_response.headers
    assert "frame-ancestors 'none'" in html_response.headers["Content-Security-Policy"]

    json_response = client.get("/json")
    assert "Content-Security-Policy" not in json_response.headers
    assert json_response.headers["X-Content-Type-Options"] == "nosniff"


def test_csp_is_configurable():
    from sau_app.security.headers import csp_policy

    assert csp_policy({"SAU_CSP": "default-src 'none'"}) == "default-src 'none'"
    assert "default-src" in csp_policy({})


def test_rate_limiter_allows_then_throttles():
    from sau_app.security.rate_limit import RateLimiter

    limiter = RateLimiter(limit=2, window_seconds=60, clock=lambda: 0.0)
    assert limiter.check("k")[0] is True
    assert limiter.check("k")[0] is True
    allowed, retry_after = limiter.check("k")
    assert allowed is False
    assert retry_after >= 1


def test_rate_limiter_window_resets():
    from sau_app.security.rate_limit import RateLimiter

    now = {"t": 0.0}
    limiter = RateLimiter(limit=1, window_seconds=60, clock=lambda: now["t"])
    assert limiter.check("k")[0] is True
    assert limiter.check("k")[0] is False
    now["t"] = 61.0
    assert limiter.check("k")[0] is True


def test_install_rate_limiting_throttles_requests_with_429():
    from flask import Flask, jsonify
    from sau_app.security.rate_limit import install_rate_limiting

    app = Flask(__name__)

    @app.route("/ping")
    def ping():  # noqa: ANN202
        return jsonify({"ok": True})

    install_rate_limiting(app, environ={
        "SAU_RATE_LIMIT_ENABLED": "1",
        "SAU_RATE_LIMIT_PER_MINUTE": "2",
        "SAU_RATE_LIMIT_STRICT_PER_MINUTE": "2",
    })
    client = app.test_client()

    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 200
    throttled = client.get("/ping")
    assert throttled.status_code == 429
    assert throttled.headers.get("Retry-After")


def test_rate_limiting_defaults_off_outside_production():
    from sau_app.security.rate_limit import enabled

    assert enabled({"APP_ENV": "testing"}) is False
    assert enabled({"APP_ENV": "production"}) is True
    assert enabled({"SAU_RATE_LIMIT_ENABLED": "0", "APP_ENV": "production"}) is False
