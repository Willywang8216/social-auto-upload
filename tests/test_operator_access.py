"""Operator authorization with in-memory identity tables and a fake Google client.

No backend import, real provider, browser, deployment database, or cookie files.
"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

from flask import Flask, g, jsonify
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from myUtils.security import SecurityPolicy
from sau_app import db as sau_db
from sau_app.auth import routes
from sau_app.auth.oidc import GoogleClaims, GoogleOIDCClient
from sau_app.auth.sessions import SessionStore
from sau_app.config import ConfigError, load_config
from sau_app.db.identity_models import (
    AuthIdentity, OAuthLoginTransaction, Session as SessionRow,
    User, Workspace, WorkspaceMember,
)
from sau_app.db.repositories import IdentityRepository
from sau_app.tenancy import install_tenancy, require_auth, require_permission
from sau_app.tenancy.context import current_auth


_ENV = {
    "APP_ENV": "testing", "DEBUG_MODE": "false", "SECRET_KEY": "test-secret" * 4,
    "SAU_API_TOKENS": "test-bearer", "SAU_AUTH_MODE": "hybrid",
    "SAU_GOOGLE_LOGIN_ENABLED": "true", "GOOGLE_LOGIN_CLIENT_ID": "client",
    "GOOGLE_LOGIN_CLIENT_SECRET": "secret", "SAU_PUBLIC_BASE_URL": "https://localhost",
    "SAU_GOOGLE_ALLOWED_EMAILS": "operator@example.com",
}
_MODELS = (User, AuthIdentity, Workspace, WorkspaceMember, SessionRow, OAuthLoginTransaction)


def _config(mode="single", **overrides):
    return load_config({**_ENV, "SAU_TENANCY_MODE": mode, **overrides})


def _claims(**overrides):
    return replace(GoogleClaims(
        subject="google-operator", email="operator@example.com", email_verified=True,
        name="Operator", picture=None,
    ), **overrides)


class FakeGoogle:
    def __init__(self, claims):
        self.claims = claims

    def authorization_url(self, *, state, nonce, **kwargs):
        self.state, self.nonce = state, nonce
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}"

    def fetch_claims(self, *, expected_nonce, **kwargs):
        assert expected_nonce == self.nonce
        return self.claims


@pytest.fixture
def db_factory(monkeypatch):
    engine = sau_db.make_engine("sqlite://", poolclass=StaticPool)
    sau_db.Base.metadata.create_all(engine, tables=[model.__table__ for model in _MODELS])
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(sau_db, "get_sessionmaker", lambda: factory)
    monkeypatch.setattr(routes, "get_sessionmaker", lambda: factory)
    yield factory
    engine.dispose()


def _app(config, claims=None, *, middleware=True):
    app = Flask(__name__)
    app.config.update(TESTING=True, SAU_APP_CONFIG=config, SAU_OIDC_CLIENT=FakeGoogle(claims or _claims()))
    app.config["SECURITY_POLICY"] = SecurityPolicy(tokens=frozenset({"test-bearer"}), cors_origins=("*",))
    if middleware:
        install_tenancy(app)
    routes.register_auth(app, config)

    @app.get("/auth-context")
    def auth_context():
        ctx = current_auth()
        return jsonify(sessionAuthenticated=g.sau_session_authenticated, method=ctx.auth_method,
                       workspace=ctx.workspace_id, role=ctx.role)

    @app.get("/protected")
    @require_auth
    def protected():
        return jsonify(ok=True)

    @app.post("/publish")
    @require_permission("publish:write")
    def publish():
        return jsonify(ok=True)

    return app


def _login(app, client):
    assert client.get("/auth/google/start", base_url="https://localhost").status_code == 302
    return client.get("/auth/google/callback", base_url="https://localhost",
                      query_string={"state": app.config["SAU_OIDC_CLIENT"].state, "code": "fake-code"})


def _seed(factory, *, subject="google-operator", email="operator@example.com"):
    # Simulate a cookie issued before the operator policy existed.
    with factory() as db:
        login = IdentityRepository(db).upsert_google_login(subject=subject, email=email, email_verified=True)
        row = SessionStore(db).create(user_id=login.user.id, workspace_id=login.workspace.id,
                                      idle_seconds=3600, absolute_seconds=7200)
        result = SimpleNamespace(sid=row.id, csrf=row.csrf_secret, uid=login.user.id, wid=login.workspace.id)
        db.commit()
        return result


def _invalidate(factory, seeded, reason):
    with factory() as db:
        identity = db.scalars(select(AuthIdentity).where(AuthIdentity.user_id == seeded.uid)).one()
        if reason == "unlisted":
            identity.email_at_provider = "intruder@example.com"  # primary_email is still allowed
        elif reason == "unverified":
            identity.email_verified = False
        elif reason == "missing-email":
            identity.email_at_provider = None
        elif reason == "missing-identity":
            db.delete(identity)
        elif reason == "other-provider":
            identity.provider = "not-google"
        elif reason == "inactive-user":
            db.get(User, seeded.uid).status = "disabled"
        elif reason == "inactive-workspace":
            db.get(Workspace, seeded.wid).status = "disabled"
        elif reason == "missing-membership":
            db.delete(db.get(WorkspaceMember, (seeded.wid, seeded.uid)))
        elif reason == "unknown-role":
            db.get(WorkspaceMember, (seeded.wid, seeded.uid)).role = "not-a-role"
        elif reason == "missing-workspace":
            db.get(SessionRow, seeded.sid).active_workspace_id = None
        db.commit()


def test_allowlist_parsing_is_explicit_and_normalized():
    cfg = _config(SAU_GOOGLE_ALLOWED_EMAILS=" Operator@Example.COM, backup@example.com\n\tOPERATOR@example.com , ",
                  SAU_GOOGLE_ALLOWED_SUBJECTS="Google-123, google-456\nGoogle-123")
    assert cfg.google_allowed_emails == {"operator@example.com", "backup@example.com"}
    assert cfg.google_allowed_subjects == {"Google-123", "google-456"}


@pytest.mark.parametrize("mode", ["single", "shadow"])
@pytest.mark.parametrize("env", ["development", "testing", "production"])
def test_enabled_google_requires_allowlist_at_startup(mode, env):
    with pytest.raises(ConfigError, match="SAU_GOOGLE_ALLOWED_EMAILS"):
        _config(mode, APP_ENV=env, SAU_GOOGLE_ALLOWED_EMAILS=" , \n\t", SAU_GOOGLE_ALLOWED_SUBJECTS="")


def test_disabled_google_preserves_production_legacy_bearer_configuration():
    cfg = _config(APP_ENV="production", SAU_AUTH_MODE="legacy", SAU_GOOGLE_LOGIN_ENABLED="false",
                  SAU_GOOGLE_ALLOWED_EMAILS="")
    assert cfg.api_tokens_configured
    assert not cfg.sessions_honored_by_gate
    assert not cfg.warnings


@pytest.mark.parametrize("mode", ["single", "shadow"])
@pytest.mark.parametrize("authorization", ["email", "subject"])
def test_verified_allowed_callback_and_session_succeed(db_factory, mode, authorization):
    cfg = (_config(mode, SAU_GOOGLE_ALLOWED_EMAILS="OPERATOR@EXAMPLE.COM") if authorization == "email"
           else _config(mode, SAU_GOOGLE_ALLOWED_EMAILS="", SAU_GOOGLE_ALLOWED_SUBJECTS="google-operator"))
    app = _app(cfg, _claims(email="Operator@Example.com"))
    client = app.test_client()
    response = _login(app, client)
    assert "/#/dashboard" in response.location
    assert cfg.session_cookie_name in response.headers["Set-Cookie"]
    payload = client.get("/api/v1/session", base_url="https://localhost").get_json()
    assert payload["authenticated"] is True
    assert payload["workspace"]["role"] == "owner"
    assert client.get("/protected", base_url="https://localhost").status_code == 200
    assert client.get("/auth-context", base_url="https://localhost").get_json()["sessionAuthenticated"] is True


@pytest.mark.parametrize("mode", ["single", "shadow"])
@pytest.mark.parametrize("reason", ["unlisted", "unverified", "malformed-verification", "missing-email",
                                    "missing-subject", "no-allowlist", "subject-case", "unverified-subject"])
def test_callback_denied_before_provisioning(db_factory, monkeypatch, mode, reason):
    cfg, claims = _config(mode), _claims()
    if reason == "unlisted":
        claims = _claims(email="someone-else@example.com")
    elif reason == "unverified":
        claims = _claims(email_verified=False)
    elif reason == "malformed-verification":
        claims = _claims(email_verified="false")
    elif reason == "missing-email":
        claims = _claims(email=None)
    elif reason == "missing-subject":
        claims = _claims(subject="")
    elif reason == "no-allowlist":
        cfg = replace(cfg, google_allowed_emails=frozenset())  # bypass startup to test runtime fail-closed
    elif reason == "subject-case":
        cfg = _config(mode, SAU_GOOGLE_ALLOWED_EMAILS="", SAU_GOOGLE_ALLOWED_SUBJECTS="Google-Operator")
    elif reason == "unverified-subject":
        cfg = _config(mode, SAU_GOOGLE_ALLOWED_EMAILS="", SAU_GOOGLE_ALLOWED_SUBJECTS=claims.subject)
        claims = _claims(email_verified=False)
    provision = Mock(side_effect=AssertionError("denied callback reached provisioning"))
    monkeypatch.setattr(IdentityRepository, "upsert_google_login", provision)
    app = _app(cfg, claims)
    client = app.test_client()
    response = _login(app, client)
    assert "error=access_denied" in response.location
    assert "Set-Cookie" not in response.headers
    provision.assert_not_called()
    with db_factory() as db:
        for model in _MODELS[:-1]:
            assert db.scalar(select(func.count()).select_from(model)) == 0
        assert db.scalars(select(OAuthLoginTransaction)).one().consumed_at is not None
    replay = client.get("/auth/google/callback", base_url="https://localhost",
                        query_string={"state": app.config["SAU_OIDC_CLIENT"].state, "code": "replay"})
    assert "error=state" in replay.location


@pytest.mark.parametrize("mode", ["single", "shadow", "enforced"])
@pytest.mark.parametrize("reason", ["inactive-user", "inactive-workspace", "missing-membership"])
def test_existing_callback_checks_status_before_upsert(db_factory, monkeypatch, mode, reason):
    seeded = _seed(db_factory)
    _invalidate(db_factory, seeded, reason)
    provision = Mock(side_effect=AssertionError("inactive callback reached upsert"))
    monkeypatch.setattr(IdentityRepository, "upsert_google_login", provision)
    app = _app(_config(mode))
    assert "error=access_denied" in _login(app, app.test_client()).location
    provision.assert_not_called()
    with db_factory() as db:
        assert db.scalar(select(func.count()).select_from(SessionRow)) == 1


@pytest.mark.parametrize("mode", ["single", "shadow"])
@pytest.mark.parametrize("consumer", ["middleware", "introspection"])
@pytest.mark.parametrize("reason", ["unlisted", "unverified", "missing-email", "missing-identity", "other-provider",
                                    "inactive-user", "inactive-workspace", "missing-membership", "unknown-role",
                                    "missing-workspace", "no-allowlist", "removed-operator"])
def test_existing_session_is_denied_and_revoked(db_factory, mode, consumer, reason):
    seeded = _seed(db_factory)
    _invalidate(db_factory, seeded, reason)
    cfg = _config(mode)
    if reason == "no-allowlist":
        cfg = replace(cfg, google_allowed_emails=frozenset())
    elif reason == "removed-operator":
        cfg = _config(mode, SAU_GOOGLE_ALLOWED_EMAILS="replacement@example.com")
    app = _app(cfg, middleware=(consumer == "middleware"))
    client = app.test_client()
    client.set_cookie(cfg.session_cookie_name, seeded.sid)
    if consumer == "middleware":
        assert client.get("/protected", base_url="https://localhost").status_code == 401
        assert client.get("/auth-context", base_url="https://localhost").get_json()["sessionAuthenticated"] is False
    else:
        assert client.get("/api/v1/session", base_url="https://localhost").get_json() == {"authenticated": False}
    with db_factory() as db:
        assert db.get(SessionRow, seeded.sid).revoked_at is not None


def test_denied_cookie_does_not_disable_bearer_access(db_factory):
    seeded = _seed(db_factory, email="intruder@example.com")
    cfg = _config()
    app = _app(cfg)
    client = app.test_client()
    client.set_cookie(cfg.session_cookie_name, seeded.sid)
    headers = {"Authorization": "Bearer test-bearer"}
    assert client.get("/protected", headers=headers, base_url="https://localhost").status_code == 200
    context = client.get("/auth-context", headers=headers, base_url="https://localhost").get_json()
    assert context["sessionAuthenticated"] is False
    assert context["method"] == "legacy_token"


def test_enforced_mode_keeps_tenant_membership_and_permissions(db_factory):
    cfg = _config("enforced", SAU_GOOGLE_ALLOWED_EMAILS="")
    app = _app(cfg)
    client = app.test_client()
    assert "/#/dashboard" in _login(app, client).location  # scoped signup needs no operator allowlist
    with db_factory() as db:
        row = db.scalars(select(SessionRow)).one()
        seeded = SimpleNamespace(sid=row.id, uid=row.user_id, wid=row.active_workspace_id, csrf=row.csrf_secret)
        db.get(WorkspaceMember, (seeded.wid, seeded.uid)).role = "viewer"
        db.commit()
    # Being listed as an operator cannot elevate the explicit viewer role.
    app.config["SAU_APP_CONFIG"] = _config("enforced")
    assert client.get("/protected", base_url="https://localhost").status_code == 200
    assert client.post("/publish", headers={"X-CSRF-Token": seeded.csrf}, base_url="https://localhost").status_code == 403
    other = _seed(db_factory, subject="other", email="other@example.com")
    with db_factory() as db:
        db.get(SessionRow, seeded.sid).active_workspace_id = other.wid
        db.commit()
    assert client.get("/protected", base_url="https://localhost").status_code == 401


@pytest.mark.parametrize("verified", [False, None, "false", "true", 0, 1, True])
def test_oidc_does_not_coerce_truthy_unverified_claims(monkeypatch, verified):
    requests = pytest.importorskip("requests")
    jose = pytest.importorskip("authlib.jose")

    class Token(dict):
        def validate(self):
            pass

    token = Token(iss="https://accounts.google.com", aud="client", nonce="nonce", sub="subject",
                  email="operator@example.com", email_verified=verified)
    monkeypatch.setattr(requests, "post", lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: {"id_token": "fake"}))
    monkeypatch.setattr(requests, "get", lambda *a, **kw: SimpleNamespace(json=lambda: {}))
    monkeypatch.setattr(jose.JsonWebKey, "import_key_set", lambda *a, **kw: object())
    monkeypatch.setattr(jose.jwt, "decode", lambda *a, **kw: token)
    claims = GoogleOIDCClient("client", "secret").fetch_claims(
        code="fake", code_verifier="verifier", redirect_uri="https://localhost/auth/google/callback", expected_nonce="nonce")
    assert claims.email_verified is (verified is True)
