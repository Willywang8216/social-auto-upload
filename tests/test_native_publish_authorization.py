"""Native readiness requires live exact grants, never an account-name lookup."""
from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from db.createTable import bootstrap
from myUtils import native_authorization as auth, preparation_context, profiles, publish_admission, publish_readiness, token_state

NOW = datetime(2030, 4, 5, 12, tzinfo=timezone.utc)


class Response:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return copy.deepcopy(self.payload)

    def raise_for_status(self):
        return None


class HTTP:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, **kwargs):
        assert kwargs["timeout"] == 45
        self.calls.append((url, kwargs))
        value = self.payloads.pop(0)
        return Response(value() if callable(value) else value)

    def post(self, *args, **kwargs):
        raise AssertionError("No native provider writes during verification")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(token_state, "utc_now", lambda: NOW)
    monkeypatch.setattr(publish_readiness, "_now", lambda: NOW)
    for key, value in {"META_APP_ID": "meta-app", "META_APP_SECRET": "meta-secret",
                       "THREADS_APP_ID": "threads-app", "THREADS_APP_SECRET": "threads-secret"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SAU_CONFIG_ENCRYPTION_KEY", raising=False)


def fixture(tmp_path, platform):
    path = tmp_path / "native.db"
    bootstrap(path)
    profile = profiles.create_profile("Native fixture", db_path=path)
    config = {"accessToken": "mock-private-token", "accessTokenExpiresAt": (NOW + timedelta(days=1)).isoformat()}
    config.update({"pageId": "page-1"} if platform == "facebook" else
                  {"pageId": "page-1", "igUserId": "ig-1"} if platform == "instagram" else
                  {"threadUserId": "threads-1"})
    account = profiles.add_account(profile.id, platform=platform, account_name="Fixture", config=config,
                                   auth_type="oauth", db_path=path)
    return path, profile, account


def grant(platform, **changes):
    body = {"is_valid": True, "app_id": "threads-app" if platform == "threads" else "meta-app",
            "expires_at": int((NOW + timedelta(seconds=60)).timestamp()), "data_access_expires_at": 0,
            "type": "PAGE", "profile_id": "page-1", "user_id": "threads-1",
            "scopes": ["pages_manage_posts", "pages_read_engagement"] if platform == "facebook" else
                      ["instagram_basic", "instagram_content_publish"] if platform == "instagram" else
                      ["threads_basic", "threads_content_publish"]}
    body.update(changes)
    return {"data": body}


def http_for(platform, introspection=None, identity=None):
    payloads = [introspection or grant(platform)]
    payloads.append(identity or ({"id": "threads-1"} if platform == "threads" else
                                {"id": "page-1", "instagram_business_account": {"id": "ig-1"}}))
    if platform == "instagram":
        payloads.append({"id": "ig-1"})
    return HTTP(*payloads)


def verify(path, profile, account, http):
    return preparation_context.verify_native_destination(account, settings=profile.settings, db_path=path, session=http)


def proof_count(path):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM publish_destination_readiness").fetchone()[0]


@pytest.mark.parametrize("platform", ["facebook", "instagram", "threads"])
def test_live_exact_grants_record_only_safe_capabilities_and_token_deadline(tmp_path, platform):
    path, profile, account = fixture(tmp_path, platform)
    http = http_for(platform)
    result = verify(path, profile, account, http)
    assert result["ready"] and proof_count(path) == 1
    assert result["expiresAt"] == (NOW + timedelta(seconds=60)).isoformat()
    assert "first_comment" not in result["evidence"]["capabilities"]
    assert not http.payloads
    serialized = json.dumps(result)
    with closing(sqlite3.connect(path)) as conn:
        serialized += conn.execute("SELECT evidence_json FROM publish_destination_readiness").fetchone()[0]
    for secret in ("mock-private-token", "meta-secret", "threads-secret", "input_token"):
        assert secret not in serialized


@pytest.mark.parametrize("platform", ["facebook", "instagram", "threads"])
@pytest.mark.parametrize("change", [
    {"is_valid": False}, {"is_valid": 1}, {"scopes": []}, {"scopes": ["read_only"]},
    {"expires_at": None}, {"expires_at": False}, {"expires_at": -1}, {"expires_at": 1},
    {"data_access_expires_at": 1}, {"app_id": "different-app"},
])
def test_invalid_grants_never_write_ready(tmp_path, platform, change):
    path, profile, account = fixture(tmp_path, platform)
    with pytest.raises((ValueError, token_state.CredentialError)):
        verify(path, profile, account, http_for(platform, introspection=grant(platform, **change)))
    assert proof_count(path) == 0


@pytest.mark.parametrize("platform,change", [
    ("facebook", {"profile_id": "other-page"}), ("facebook", {"type": "USER"}),
    ("instagram", {"profile_id": "other-page"}), ("threads", {"user_id": "other-user"}),
    ("facebook", {"granular_scopes": [{"scope": "pages_manage_posts", "target_ids": ["other-page"]}]}),
    ("instagram", {"granular_scopes": [{"scope": "instagram_content_publish", "target_ids": ["other-page"]}]}),
])
def test_foreign_destination_grants_never_write_ready(tmp_path, platform, change):
    path, profile, account = fixture(tmp_path, platform)
    with pytest.raises(ValueError):
        verify(path, profile, account, http_for(platform, introspection=grant(platform, **change)))
    assert proof_count(path) == 0


@pytest.mark.parametrize("platform", ["facebook", "instagram", "threads"])
def test_authenticated_foreign_identity_is_not_authorization(tmp_path, platform):
    path, profile, account = fixture(tmp_path, platform)
    with pytest.raises(ValueError):
        verify(path, profile, account, http_for(platform, identity={"id": "other", "name": "Fixture"}))
    assert proof_count(path) == 0


def test_instagram_user_grant_requires_exact_page_tasks_and_association(tmp_path):
    path, profile, account = fixture(tmp_path, "instagram")
    data = grant("instagram", type="USER", scopes=["instagram_basic", "instagram_content_publish", "pages_show_list"])
    pages = {"data": [{"id": "page-1", "tasks": ["CREATE_CONTENT"], "instagram_business_account": {"id": "ig-1"}}]}
    assert verify(path, profile, account, HTTP(data, pages, {"id": "ig-1"}))["ready"]
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DELETE FROM publish_destination_readiness")
    pages["data"][0]["tasks"] = ["ANALYZE"]
    with pytest.raises(ValueError):
        verify(path, profile, account, HTTP(data, pages))
    assert proof_count(path) == 0


@pytest.mark.parametrize("platform", ["facebook", "instagram", "threads"])
def test_introspection_app_rotation_invalidates_proof_and_inflight_record(tmp_path, monkeypatch, platform):
    path, profile, account = fixture(tmp_path, platform)
    assert verify(path, profile, account, http_for(platform))["ready"]
    key = "THREADS_APP_SECRET" if platform == "threads" else "META_APP_SECRET"
    monkeypatch.setenv(key, "new-mock-secret")
    assert not publish_readiness.destination_readiness(account, profile.settings, db_path=path)["ready"]
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DELETE FROM publish_destination_readiness")
    def rotating():
        monkeypatch.setenv(key, "rotated-mid-read")
        return {"id": "threads-1"} if platform == "threads" else {"id": "page-1", "instagram_business_account": {"id": "ig-1"}}
    http = http_for(platform)
    http.payloads[1] = rotating
    with pytest.raises(ValueError, match="configuration|credential"):
        verify(path, profile, account, http)
    assert proof_count(path) == 0


@pytest.mark.parametrize("platform", ["facebook", "instagram", "threads"])
@pytest.mark.parametrize("field", ["altText", "alt_text"])
def test_unimplemented_native_image_alt_text_requires_review(platform, field):
    with pytest.raises(publish_admission.AdmissionError, match="cannot preserve"):
        publish_admission.validate_intent({"message": "approved", "artifacts": [{"artifact_kind": "image", field: "approved description"}]}, platform=platform)


@pytest.mark.parametrize("field", ["firstComment", "first_comment"])
def test_unimplemented_threads_first_comment_requires_review(field):
    with pytest.raises(publish_admission.AdmissionError, match="cannot preserve"):
        publish_admission.validate_intent({"message": "approved", field: "approved comment"}, platform="threads")


@pytest.mark.parametrize("platform", ["facebook", "instagram"])
@pytest.mark.parametrize("comment_target", ["page-1", "other-page", None])
def test_optional_comment_grants_are_bound_to_the_exact_page(tmp_path, platform, comment_target):
    path, profile, account = fixture(tmp_path, platform)
    body = grant(platform)
    comment_scope = "pages_manage_engagement" if platform == "facebook" else "instagram_manage_comments"
    body["data"]["scopes"].append(comment_scope)
    body["data"]["granular_scopes"] = [{"scope": scope, "target_ids": ["page-1"]}
                                           for scope in body["data"]["scopes"] if scope != comment_scope]
    if comment_target is not None:
        body["data"]["granular_scopes"].append({"scope": comment_scope, "target_ids": [comment_target]})
    result = verify(path, profile, account, http_for(platform, introspection=body))
    assert result["ready"]
    assert ("first_comment" in result["evidence"]["capabilities"]) is (comment_target == "page-1")


@pytest.mark.parametrize("tasks", [None, "CREATE_CONTENT", [{}], [False], [""]])
def test_malformed_instagram_page_tasks_fail_closed(tmp_path, tasks):
    path, profile, account = fixture(tmp_path, "instagram")
    data = grant("instagram", type="USER", scopes=["instagram_basic", "instagram_content_publish", "pages_show_list"])
    pages = {"data": [{"id": "page-1", "tasks": tasks, "instagram_business_account": {"id": "ig-1"}}]}
    with pytest.raises(ValueError, match="content-creation task"):
        verify(path, profile, account, HTTP(data, pages))
    assert proof_count(path) == 0


@pytest.mark.parametrize("granular", [None, {}, [{"scope": None}], [{"scope": "pages_manage_posts", "target_ids": [False]}]])
def test_malformed_granular_page_grants_never_write_ready(tmp_path, granular):
    path, profile, account = fixture(tmp_path, "facebook")
    with pytest.raises(ValueError, match="grants are malformed"):
        verify(path, profile, account, http_for("facebook", introspection=grant("facebook", granular_scopes=granular)))
    assert proof_count(path) == 0
