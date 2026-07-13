"""Request middleware that resolves the auth context and guards CSRF (Phase 4).

Design invariant: **inert by default.** With ``google_login_enabled`` off (the
default) no session lookup happens and no new response is ever produced, so the
537-plus existing tests behave exactly as before. Only when Google login is on
does a session cookie get resolved; only in ``hybrid``/``oidc`` does a session
satisfy the auth gate; and CSRF/Origin is enforced only for requests that are
themselves authenticated by the (ambient) session cookie — never for the bearer
token or webhooks, which are not CSRF-able.
"""

from __future__ import annotations

import logging
from secrets import compare_digest

from flask import Flask, current_app, g, jsonify, request

from ..auth.permissions import permissions_for
from ..auth.sessions import CSRF_HEADER, SessionStore
from ..config import AppConfig
from .context import AuthContext, METHOD_GOOGLE_SESSION
from myUtils.security import extract_bearer_token

_LOGGER = logging.getLogger("sau.tenancy")
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _resolve_session(config: AppConfig):
    """Return ``(AuthContext, csrf_secret)`` for a valid session cookie, else None."""
    session_id = request.cookies.get(config.session_cookie_name)
    if not session_id:
        return None
    try:
        from sqlalchemy import select

        from ..db import get_sessionmaker
        from ..db.identity_models import WorkspaceMember

        Session = get_sessionmaker()
        with Session() as db:
            row = SessionStore(db).get_active(session_id, idle_seconds=config.session_idle_seconds)
            if row is None:
                db.commit()
                return None
            role = "viewer"
            if row.active_workspace_id:
                member = db.scalars(
                    select(WorkspaceMember).where(
                        WorkspaceMember.workspace_id == row.active_workspace_id,
                        WorkspaceMember.user_id == row.user_id,
                    )
                ).one_or_none()
                if member is not None:
                    role = member.role
            ctx = AuthContext(
                authenticated=True,
                auth_method=METHOD_GOOGLE_SESSION,
                user_id=row.user_id,
                workspace_id=row.active_workspace_id,
                role=role,
                permissions=frozenset(permissions_for(role)),
                session_id=row.id,
            )
            csrf = row.csrf_secret
            db.commit()
            return ctx, csrf
    except Exception as exc:  # noqa: BLE001 — never let resolution error deny/allow wrongly
        _LOGGER.warning("session resolution failed (treating as anonymous): %s", exc)
        return None


def _legacy_token_context() -> AuthContext:
    """Informational context for a valid legacy bearer token (read-only)."""
    policy = current_app.config.get("SECURITY_POLICY")
    if policy is None or policy.open_mode:
        return AuthContext.anonymous()
    token = extract_bearer_token(request.headers, request.args, is_sse=(request.path == "/login"))
    if policy.token_is_valid(token):
        return AuthContext.legacy_token()
    return AuthContext.anonymous()


def _csrf_denied(config: AppConfig, csrf_secret: str | None):
    """Return a 403 response for a bad CSRF token / Origin, else None."""
    if request.method in _SAFE_METHODS:
        return None
    presented = request.headers.get(CSRF_HEADER)
    if not csrf_secret or not presented or not compare_digest(presented, csrf_secret):
        return jsonify({"code": 403, "msg": "invalid or missing CSRF token", "data": None}), 403
    origin = request.headers.get("Origin")
    expected = (config.frontend_origin or config.public_base_url or "").rstrip("/")
    if origin and expected and origin.rstrip("/") != expected:
        return jsonify({"code": 403, "msg": "origin not allowed", "data": None}), 403
    return None


def install_tenancy(app: Flask) -> None:
    @app.before_request
    def _resolve_auth_context():  # noqa: ANN202
        g.auth_ctx = AuthContext.anonymous()
        g.sau_session_authenticated = False

        config: AppConfig | None = current_app.config.get("SAU_APP_CONFIG")
        if config is None:
            return None

        # Session cookies only exist when the login flow is enabled; skip all
        # session/DB work otherwise (keeps the default path a no-op).
        if config.google_login_enabled:
            resolved = _resolve_session(config)
            if resolved is not None:
                ctx, csrf = resolved
                g.auth_ctx = ctx
                if config.sessions_honored_by_gate:
                    g.sau_session_authenticated = True
                # CSRF/Origin bind only to session-cookie (ambient) auth.
                denied = _csrf_denied(config, csrf)
                if denied is not None:
                    return denied
                return None

        # No session — record the legacy-token context for decorators. This is
        # read-only and does not affect the legacy gate itself.
        g.auth_ctx = _legacy_token_context()
        return None
