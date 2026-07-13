"""Google login routes (Phase 3), registered only when the flag is on.

    GET  /auth/google/start     -> 302 to Google (state/nonce/PKCE stored server-side)
    GET  /auth/google/callback  -> validate, provision user+workspace, create session
    GET  /api/v1/session        -> current session (or {authenticated: false})
    POST /api/v1/logout         -> revoke session (CSRF-protected)

The Google network calls go through ``app.config['SAU_OIDC_CLIENT']`` so tests
inject a fake and exercise the whole flow without real credentials.
"""

from __future__ import annotations

import logging

from flask import Blueprint, Flask, current_app, jsonify, redirect, request
from sqlalchemy import select

from ..config import AppConfig
from ..db import get_sessionmaker
from ..db.identity_models import WorkspaceMember, Workspace
from ..db.repositories import IdentityRepository
from .oidc import GoogleOIDCClient, OIDCError
from .permissions import permissions_for
from .sessions import (
    CSRF_HEADER,
    SessionStore,
    clear_session_cookie,
    csrf_ok,
    set_session_cookie,
)
from .transactions import LoginTransactionStore

_LOGGER = logging.getLogger("sau.auth")

auth_bp = Blueprint("sau_auth", __name__)


def _config() -> AppConfig:
    return current_app.config["SAU_APP_CONFIG"]


def _oidc_client():
    return current_app.config.get("SAU_OIDC_CLIENT")


def _frontend_url(config: AppConfig, path: str = "/") -> str:
    base = (config.frontend_origin or config.public_base_url or "").rstrip("/")
    return f"{base}{path}" if base else path


@auth_bp.route("/auth/google/start", methods=["GET"])
def google_start():
    config = _config()
    client = _oidc_client()
    redirect_uri = config.google_login_redirect_uri
    if client is None or not redirect_uri:
        return jsonify({"code": 503, "msg": "google login is not configured", "data": None}), 503

    Session = get_sessionmaker()
    with Session() as db:
        started = LoginTransactionStore(db).start(redirect_uri=redirect_uri)
        db.commit()
    url = client.authorization_url(
        state=started.state,
        nonce=started.nonce,
        code_challenge=started.code_challenge,
        redirect_uri=redirect_uri,
    )
    return redirect(url, code=302)


@auth_bp.route("/auth/google/callback", methods=["GET"])
def google_callback():
    config = _config()
    client = _oidc_client()
    if client is None:
        return jsonify({"code": 503, "msg": "google login is not configured", "data": None}), 503

    if request.args.get("error"):
        return redirect(_frontend_url(config, "/#/login?error=oauth"), code=302)

    state = request.args.get("state")
    code = request.args.get("code")

    Session = get_sessionmaker()
    with Session() as db:
        txn = LoginTransactionStore(db).consume(state)
        if txn is None or not code:
            db.commit()  # persist the consume even on failure
            return redirect(_frontend_url(config, "/#/login?error=state"), code=302)

        try:
            claims = client.fetch_claims(
                code=code,
                code_verifier=txn.code_verifier,
                redirect_uri=txn.redirect_uri,
                expected_nonce=txn.nonce,
            )
        except OIDCError as exc:
            db.commit()
            _LOGGER.warning("google callback rejected: %s", exc)
            return redirect(_frontend_url(config, "/#/login?error=verify"), code=302)

        login = IdentityRepository(db).upsert_google_login(
            subject=claims.subject,
            email=claims.email,
            email_verified=claims.email_verified,
            display_name=claims.name,
            avatar_url=claims.picture,
            claims=claims.raw,
        )
        session_row = SessionStore(db).create(
            user_id=login.user.id,
            workspace_id=login.workspace.id,
            idle_seconds=config.session_idle_seconds,
            absolute_seconds=config.session_absolute_seconds,
        )
        session_id = session_row.id
        db.commit()

    response = redirect(_frontend_url(config, "/#/dashboard"), code=302)
    set_session_cookie(response, session_id, config)
    return response


def _current_session_payload(config: AppConfig) -> dict:
    session_id = request.cookies.get(config.session_cookie_name)
    if not session_id:
        return {"authenticated": False}

    Session = get_sessionmaker()
    with Session() as db:
        row = SessionStore(db).get_active(session_id, idle_seconds=config.session_idle_seconds)
        if row is None:
            db.commit()
            return {"authenticated": False}

        member = db.scalars(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == row.active_workspace_id,
                WorkspaceMember.user_id == row.user_id,
            )
        ).one_or_none()
        workspace = db.get(Workspace, row.active_workspace_id) if row.active_workspace_id else None
        user = row.user
        role = member.role if member else "viewer"
        payload = {
            "authenticated": True,
            "user": {
                "id": user.id,
                "displayName": user.display_name,
                "email": user.primary_email,
                "avatarUrl": user.avatar_url,
            },
            "workspace": (
                {"id": workspace.id, "name": workspace.name, "role": role}
                if workspace is not None
                else None
            ),
            "permissions": permissions_for(role),
            "csrfToken": row.csrf_secret,
        }
        db.commit()
        return payload


@auth_bp.route("/api/v1/session", methods=["GET"])
def api_session():
    return jsonify(_current_session_payload(_config())), 200


@auth_bp.route("/api/v1/logout", methods=["POST"])
def api_logout():
    config = _config()
    session_id = request.cookies.get(config.session_cookie_name)
    if session_id:
        Session = get_sessionmaker()
        with Session() as db:
            store = SessionStore(db)
            row = store.get_active(session_id, idle_seconds=config.session_idle_seconds)
            if row is not None and not csrf_ok(row, request.headers.get(CSRF_HEADER)):
                db.commit()
                return jsonify({"code": 403, "msg": "invalid csrf token", "data": None}), 403
            store.revoke(session_id)
            db.commit()

    response = jsonify({"code": 200, "msg": "logged out", "data": None})
    clear_session_cookie(response, config)
    return response


def register_auth(app: Flask, config: AppConfig) -> None:
    """Register the Google-login blueprint when the feature flag is on."""

    if not config.google_login_enabled:
        return
    if "sau_auth" in app.blueprints:
        return
    if app.config.get("SAU_OIDC_CLIENT") is None and config.google_login_client_id:
        app.config["SAU_OIDC_CLIENT"] = GoogleOIDCClient(
            config.google_login_client_id, config.google_login_client_secret or ""
        )
    app.register_blueprint(auth_bp)
