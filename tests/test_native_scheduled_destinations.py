"""No-post grant regressions for supported scheduled native destinations."""
from __future__ import annotations

import copy
import json
import socket
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from myUtils import native_destinations as native, preparation_context as preparation
from myUtils import prepared_publishers as publishers, publish_readiness as readiness, worker, media_restore


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("SAU_TESTING", "1")
    monkeypatch.delenv("SAU_CONFIG_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("live network forbidden")))


def response(body, headers=None):
    return SimpleNamespace(status_code=200, headers=headers or {}, text="", json=lambda: body,
                           raise_for_status=lambda: None)


class HTTP:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def _call(self, method, url, **kwargs):
        assert kwargs.get("timeout") == 45
        assert not any(word in url for word in ("createRecord", "sendMessage", "sendPhoto", "sendVideo", "uploadBlob"))
        self.calls.append((method, url))
        assert self.responses
        result = self.responses.pop(0)
        return result() if callable(result) else result

    def get(self, url, **kwargs):
        return self._call("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._call("POST", url, **kwargs)


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "native.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript('''
            CREATE TABLE profiles(id INTEGER PRIMARY KEY,workspace_id TEXT,settings_json TEXT);
            CREATE TABLE accounts(id INTEGER PRIMARY KEY,profile_id INTEGER,workspace_id TEXT,
                platform TEXT,enabled INTEGER,cookie_path TEXT,config_json TEXT);
            CREATE TABLE publish_destination_readiness(account_id INTEGER PRIMARY KEY,workspace_id TEXT,
                provider TEXT,config_fingerprint TEXT,status TEXT,reason TEXT,evidence_json TEXT,
                checked_at TEXT,expires_at TEXT);
            CREATE TABLE publish_dependency_states(scope_key TEXT PRIMARY KEY,config_fingerprint TEXT,
                kind TEXT,status TEXT,reason TEXT,blocked_until TEXT,last_checked_at TEXT,updated_at TEXT);
            INSERT INTO profiles VALUES(1,'tenant-a','{}');
        ''')

    def save(platform, config):
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO accounts VALUES(1,1,'tenant-a',?,1,'',?)",
                         (platform, json.dumps(config)))
        return SimpleNamespace(id=1, profile_id=1, workspace_id="tenant-a", platform=platform,
                               enabled=True, cookie_path="", config=copy.deepcopy(config)), path
    return save


def stored(db):
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute("SELECT evidence_json FROM publish_destination_readiness").fetchall()


def bluesky_http(**changes):
    auth = {"did": "did:plc:fixture", "handle": "fixture.bsky.social", "accessJwt": "fixture-jwt", "active": True}
    auth.update(changes)
    return HTTP([response(auth), response({"did": "did:plc:fixture", "handle": "fixture.bsky.social", "active": True})])


def github_http(*, scopes="repo", push=True, protected=False, private=False):
    return HTTP([response({"full_name": "fixture/blog", "archived": False, "private": private,
                           "permissions": {"push": push}}, {"X-OAuth-Scopes": scopes}),
                 response({"name": "main", "protected": protected})])


def telegram_http(*, chat_id=-10055, status="administrator", posting=True, sender=77):
    return HTTP([response({"ok": True, "result": {"id": 77, "is_bot": True}}),
                 response({"ok": True, "result": {"id": chat_id, "type": "channel"}}),
                 response({"ok": True, "result": {"user": {"id": sender}, "status": status,
                                                   "can_post_messages": posting}})])


@pytest.mark.parametrize("platform,config,env", [
    ("bluesky", {"handle": "fixture.bsky.social", "appPasswordEnv": "FIXTURE_BSKY"}, {"FIXTURE_BSKY": "fixture-password"}),
    ("nw_sw_blog", {"repoOwner": "fixture", "repoName": "blog"}, {"SAU_NW_SW_BLOG_GITHUB_TOKEN": "fixture-token"}),
    ("teaching_blog", {"repoOwner": "fixture", "repoName": "blog"}, {"SAU_TEACHING_BLOG_GITHUB_TOKEN": "fixture-token"}),
    ("telegram", {"chatIds": ["-10055"], "botTokenEnv": "FIXTURE_BOT"}, {"FIXTURE_BOT": "fixture-token"}),
    ("telegram", {"chatIds": ["-10055"], "mtproto": True, "apiIdEnv": "FIXTURE_ID", "apiHashEnv": "FIXTURE_HASH", "sessionStringEnv": "FIXTURE_SESSION"},
     {"FIXTURE_ID": "123", "FIXTURE_HASH": "fixture-hash", "FIXTURE_SESSION": "fixture-session"}),
])
def test_effective_configured_credentials_are_not_a_live_readiness_claim(registry, monkeypatch, platform, config, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    actor, db = registry(platform, config)
    context = readiness._context(actor, {})
    assert context["configured"] and context["identity"]
    result = readiness.destination_readiness(actor, db_path=db)
    assert result["phase"] == "configured_unverified" and not result["ready"]
    assert result["nativeHealth"]["configured"]
    assert not stored(db)


def test_default_env_rotation_invalidates_native_selection(registry, monkeypatch):
    monkeypatch.setenv("SAU_NW_SW_BLOG_GITHUB_TOKEN", "first-fixture")
    actor, _ = registry("nw_sw_blog", {"repoOwner": "fixture", "repoName": "blog"})
    before = readiness.config_fingerprint(actor, {})
    monkeypatch.setenv("SAU_NW_SW_BLOG_GITHUB_TOKEN", "second-fixture")
    assert readiness.config_fingerprint(actor, {}) != before


def test_bluesky_active_exact_app_password_authorization_persists_safe_facts(registry):
    actor, db = registry("bluesky", {"handle": "fixture.bsky.social", "appPassword": "fixture-password"})
    http = bluesky_http()
    result = preparation.verify_native_destination(actor, db_path=db, session=http)
    assert result["ready"] and "image" in result["evidence"]["capabilities"]
    assert len(http.calls) == 2 and not http.responses
    assert "fixture-password" not in repr(result) + repr(stored(db))
    assert "fixture-jwt" not in repr(result) + repr(stored(db))


@pytest.mark.parametrize("change", [{"handle": "different.bsky.social"}, {"did": ""}, {"active": False}, {"status": "takendown"}, {"accessJwt": ""}])
def test_bluesky_wrong_or_inactive_authorization_cannot_write_readiness(registry, change):
    actor, db = registry("bluesky", {"handle": "fixture.bsky.social", "appPassword": "fixture-password"})
    with pytest.raises(ValueError):
        preparation.verify_native_destination(actor, db_path=db, session=bluesky_http(**change))
    assert not stored(db)


def test_bluesky_config_change_during_verification_fails_cas(registry):
    actor, db = registry("bluesky", {"handle": "fixture.bsky.social", "appPassword": "fixture-password"})
    http = bluesky_http()
    def changed():
        with closing(sqlite3.connect(db)) as conn, conn:
            config = {**actor.config, "appPassword": "rotated-fixture"}
            conn.execute("UPDATE accounts SET config_json=? WHERE id=1", (json.dumps(config),))
        return response({"did": "did:plc:fixture", "handle": "fixture.bsky.social", "active": True})
    http.responses[-1] = changed
    with pytest.raises(ValueError, match="changed"):
        preparation.verify_native_destination(actor, db_path=db, session=http)
    assert not stored(db)


def test_github_requires_exact_repo_push_scope_and_nonprotected_branch(registry):
    actor, db = registry("nw_sw_blog", {"repoOwner": "fixture", "repoName": "blog", "githubToken": "fixture-secret"})
    http = github_http()
    result = preparation.verify_native_destination(actor, db_path=db, session=http)
    assert result["ready"] and all(method == "GET" for method, _ in http.calls)
    assert "fixture-secret" not in repr(result) + repr(stored(db))


@pytest.mark.parametrize("options", [{"scopes": ""}, {"scopes": "read:user"}, {"push": False}, {"protected": True}, {"scopes": "public_repo", "private": True}])
def test_github_read_access_alone_is_not_content_write_authority(registry, options):
    actor, db = registry("nw_sw_blog", {"repoOwner": "fixture", "repoName": "blog", "githubToken": "fixture-secret"})
    with pytest.raises(ValueError):
        preparation.verify_native_destination(actor, db_path=db, session=github_http(**options))
    assert not stored(db)


def test_telegram_bot_channel_permission_without_send_calls(registry):
    actor, db = registry("telegram", {"chatIds": ["-10055"], "botToken": "fixture-secret"})
    http = telegram_http()
    assert preparation.verify_native_destination(actor, db_path=db, session=http)["ready"]
    assert [url.rsplit("/", 1)[-1] for _, url in http.calls] == ["getMe", "getChat", "getChatMember"]
    assert "fixture-secret" not in repr(stored(db))


@pytest.mark.parametrize("options", [{"chat_id": -10099}, {"status": "member"}, {"posting": False}, {"sender": 88}])
def test_telegram_missing_or_foreign_chat_permission_is_rejected(registry, options):
    actor, db = registry("telegram", {"chatIds": ["-10055"], "botToken": "fixture-secret"})
    with pytest.raises(ValueError):
        preparation.verify_native_destination(actor, db_path=db, session=telegram_http(**options))
    assert not stored(db)


@pytest.mark.parametrize("authorized,posting,peer", [(True, True, -10055), (False, True, -10055), (True, False, -10055), (True, True, -10099)])
def test_mtproto_live_grant_only_never_starts_login_or_sends(registry, monkeypatch, authorized, posting, peer):
    from telethon import utils
    events = []
    class Client:
        def __init__(self, *args, **kwargs):
            assert kwargs["request_retries"] == kwargs["connection_retries"] == 0
        async def connect(self): events.append("connect")
        async def is_user_authorized(self): return authorized
        async def get_me(self): return SimpleNamespace(id=77, deleted=False)
        async def get_entity(self, value): return SimpleNamespace(id=peer, broadcast=True)
        async def get_permissions(self, *args):
            return SimpleNamespace(is_creator=False, is_admin=True, participant=SimpleNamespace(admin_rights=SimpleNamespace(post_messages=posting)))
        async def disconnect(self): events.append("disconnect")
    monkeypatch.setattr(publishers, "TelegramClient", Client)
    monkeypatch.setattr(publishers, "StringSession", lambda value: value)
    monkeypatch.setattr(utils, "get_peer_id", lambda entity: entity.id)
    actor, db = registry("telegram", {"chatIds": ["-10055"], "mtproto": True, "apiId": "123", "apiHash": "fixture-hash", "sessionString": "fixture-session"})
    if authorized and posting and peer == -10055:
        assert preparation.verify_native_destination(actor, db_path=db, session=HTTP([]))["ready"]
    else:
        with pytest.raises(ValueError):
            preparation.verify_native_destination(actor, db_path=db, session=HTTP([]))
        assert not stored(db)
    assert events == ["connect", "disconnect"]


def test_worker_verifies_supported_native_destination_instead_of_configuration_dead_end(registry, monkeypatch):
    actor, db = registry("bluesky", {"handle": "fixture.bsky.social", "appPassword": "fixture-password"})
    monkeypatch.setattr(worker.profile_registry, "get_profile", lambda *a, **k: SimpleNamespace(settings={}))
    verify = preparation.verify_native_destination
    http = bluesky_http()
    monkeypatch.setattr(preparation, "verify_native_destination", lambda actor, **kw: verify(actor, session=http, **kw))
    result = worker._fresh_preparation_context(actor, {}, db_path=db)
    assert result["backup_compatible"] is False and readiness.destination_readiness(actor, db_path=db)["ready"]
    assert len(http.calls) == 2


def test_cached_destination_cooldown_spends_no_new_prerequisite_attempt(registry, monkeypatch):
    actor, db = registry("bluesky", {"handle": "fixture.bsky.social", "appPassword": "fixture-password"})
    monkeypatch.setattr(worker.profile_registry, "get_profile", lambda *a, **k: SimpleNamespace(settings={}))
    scope, fingerprint = readiness.destination_scope_key(actor, {}), readiness.config_fingerprint(actor, {})
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("INSERT INTO publish_dependency_states(scope_key,config_fingerprint,kind,status,reason,blocked_until) VALUES(?,?,?,'blocked','transport',?)",
                     (scope, fingerprint, "destination_verification", (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()))
    check = Mock(side_effect=AssertionError("cooldown cannot send another verification"))
    monkeypatch.setattr(preparation, "verify_native_destination", check)
    with pytest.raises(media_restore.PrerequisiteUnavailable) as caught:
        worker._fresh_preparation_context(actor, {}, db_path=db)
    assert caught.value.details["cachedDependencyWait"] is True
    check.assert_not_called()


def test_completed_permission_denial_is_not_retried_as_transport(registry, monkeypatch):
    actor, db = registry("bluesky", {"handle": "fixture.bsky.social", "appPassword": "fixture-password"})
    monkeypatch.setattr(worker.profile_registry, "get_profile", lambda *a, **k: SimpleNamespace(settings={}))
    monkeypatch.setattr(preparation, "verify_native_destination", Mock(side_effect=ValueError("verified destination has no write permission")))
    with pytest.raises(worker.MediaRestoreError) as caught:
        worker._fresh_preparation_context(actor, {}, db_path=db)
    assert caught.value.retryable is False and "authorization requires review" in str(caught.value)
    assert not stored(db)


def test_preparation_refreshes_missing_proof_once_then_reuses_fresh_evidence(registry):
    actor, db = registry("bluesky", {"handle": "fixture.bsky.social", "appPassword": "fixture-password"})
    http = bluesky_http()
    assert preparation.ensure_destination_ready(actor, db_path=db, session=http)["ready"]
    assert preparation.ensure_destination_ready(actor, db_path=db, session=http)["ready"]
    assert len(http.calls) == 2 and not http.responses


def test_preparation_missing_credentials_never_requests_a_grant(registry):
    actor, db = registry("telegram", {"chatIds": ["-10055"]})
    http = HTTP([])
    with pytest.raises(ValueError, match="unavailable"):
        preparation.ensure_destination_ready(actor, db_path=db, session=http)
    assert not http.calls and not stored(db)


@pytest.mark.parametrize("mtproto", [False, True])
def test_per_post_telegram_chats_cannot_borrow_another_destination_proof(registry, mtproto):
    config = {"chatIds": ["-10055"], "botToken": "fixture-secret"}
    if mtproto:
        config.update(mtproto=True, apiId="123", apiHash="fixture-hash", sessionString="fixture-session")
    actor, db = registry("telegram", config)
    payload = {"draft": {"message": "Exact approved message.", "chatIds": ["-10099"]}}
    http = HTTP([])
    with pytest.raises(ValueError, match="chats differ"):
        preparation.verify_native_destination(actor, db_path=db, session=http, payload=payload)
    assert not http.calls and not stored(db)
    from myUtils import publish_admission
    with pytest.raises(publish_admission.AdmissionError, match="chats differ"):
        publish_admission.validate_intent(payload, platform="telegram", account_config=config)


def test_fresh_account_proof_does_not_skip_per_post_telegram_destination_check(registry, monkeypatch):
    actor, db = registry("telegram", {"chatIds": ["-10055"], "botToken": "fixture-secret"})
    http = telegram_http()
    assert preparation.verify_native_destination(actor, db_path=db, session=http)["ready"]
    payload = {"draft": {"message": "Exact approved message.", "chatIds": ["-10099"]}}
    with pytest.raises(ValueError, match="chats differ"):
        preparation.ensure_destination_ready(actor, db_path=db, session=http, payload=payload)
    monkeypatch.setattr(worker.profile_registry, "get_profile", lambda *a, **k: SimpleNamespace(settings={}))
    with pytest.raises(ValueError, match="chats differ"):
        worker._fresh_preparation_context(actor, payload, db_path=db)
    assert len(http.calls) == 3 and readiness.destination_readiness(actor, db_path=db)["ready"]


@pytest.mark.parametrize("key,value", [("botId", 77), ("botId", 88), ("userId", 77), ("userId", 88)])
def test_bot_authorization_is_bound_to_the_registered_sender(registry, key, value):
    actor, db = registry("telegram", {"chatIds": ["-10055"], "botToken": "fixture-secret", key: value})
    http = telegram_http()
    if value == 77:
        assert preparation.verify_native_destination(actor, db_path=db, session=http)["ready"]
    else:
        with pytest.raises(ValueError, match="configured sender"):
            preparation.verify_native_destination(actor, db_path=db, session=http)
        assert not stored(db) and len(http.calls) == 1


def test_github_verifies_the_environment_selected_branch_not_default_main(registry, monkeypatch):
    monkeypatch.setenv("FIXTURE_BLOG_BRANCH", "release")
    actor, db = registry("nw_sw_blog", {"repoOwner": "fixture", "repoName": "blog", "githubToken": "fixture-secret", "branchEnv": "FIXTURE_BLOG_BRANCH"})
    http = github_http()
    http.responses[-1] = response({"name": "release", "protected": True})
    with pytest.raises(ValueError, match="branch requires approval"):
        preparation.verify_native_destination(actor, db_path=db, session=http)
    assert http.calls[-1][1].endswith("/branches/release") and not stored(db)
