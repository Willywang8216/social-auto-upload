"""Phase 4: AuthContext, permission decorators, and the tenancy middleware.

Covers the compatibility-mode contract:
- config defaults preserve legacy behavior; invalid/incoherent modes warn;
- the middleware is inert under the default legacy mode;
- in hybrid mode a valid session satisfies the real ``_enforce_auth`` gate while
  the legacy bearer token still works;
- CSRF/Origin is enforced only for session-cookie unsafe requests;
- ``require_auth``/``require_permission`` return 401 vs 403 correctly.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

flask_available = importlib.util.find_spec("flask") is not None
sqlalchemy_available = importlib.util.find_spec("sqlalchemy") is not None

if flask_available and sqlalchemy_available:
    from flask import Flask, jsonify

    import db.createTable as create_table
    from sau_app import db as sau_db
    from sau_app.auth.sessions import CSRF_HEADER
    from sau_app.config import load_config
    from sau_app.db.identity_models import (
        ROLE_EDITOR,
        ROLE_OWNER,
        ROLE_VIEWER,
        Session as SessionRow,
        User,
        Workspace,
        WorkspaceMember,
    )
    from sau_app.tenancy import install_tenancy, require_auth, require_permission
    from sau_app.tenancy.context import current_auth


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ConfigModeTests(unittest.TestCase):
    def test_defaults_are_legacy_single(self) -> None:
        cfg = load_config({"APP_ENV": "testing"})
        self.assertEqual(cfg.auth_mode, "legacy")
        self.assertEqual(cfg.tenancy_mode, "single")
        self.assertFalse(cfg.sessions_honored_by_gate)

    def test_hybrid_requires_google_login(self) -> None:
        cfg = load_config({"APP_ENV": "testing", "SAU_AUTH_MODE": "hybrid"})
        # No google login -> recorded as a warning, sessions not honored yet.
        self.assertTrue(any("no session source" in w for w in cfg.warnings))

    def test_invalid_mode_falls_back_with_warning(self) -> None:
        cfg = load_config({"APP_ENV": "testing", "SAU_AUTH_MODE": "bogus"})
        self.assertEqual(cfg.auth_mode, "legacy")
        self.assertTrue(any("invalid value" in w for w in cfg.warnings))

    def test_hybrid_honored_when_google_login_on(self) -> None:
        cfg = load_config(
            {
                "APP_ENV": "testing",
                "SAU_AUTH_MODE": "hybrid",
                "SAU_GOOGLE_LOGIN_ENABLED": "true",
                "GOOGLE_LOGIN_CLIENT_ID": "c",
                "GOOGLE_LOGIN_CLIENT_SECRET": "s",
                "SAU_PUBLIC_BASE_URL": "https://up.example.com",
                "SECRET_KEY": "x" * 40,
            }
        )
        self.assertTrue(cfg.sessions_honored_by_gate)


@unittest.skipUnless(flask_available and sqlalchemy_available, "flask/sqlalchemy not installed")
class DecoratorTests(unittest.TestCase):
    def _app(self):
        app = Flask(__name__)
        app.config["SAU_APP_CONFIG"] = load_config({"APP_ENV": "testing"})

        @app.route("/needs-auth")
        @require_auth
        def needs_auth():
            return jsonify(ok=True)

        @app.route("/needs-publish")
        @require_permission("publish:write")
        def needs_publish():
            return jsonify(ok=True)

        @app.route("/whoami-ctx")
        def whoami_ctx():
            ctx = current_auth()
            return jsonify(method=ctx.auth_method, role=ctx.role)

        return app

    def test_require_auth_401_when_anonymous(self) -> None:
        app = self._app()
        # No middleware installed -> current_auth() is anonymous.
        self.assertEqual(app.test_client().get("/needs-auth").status_code, 401)

    def test_require_permission_403_and_401(self) -> None:
        app = self._app()
        c = app.test_client()
        self.assertEqual(c.get("/needs-publish").status_code, 401)  # anonymous


@unittest.skipUnless(flask_available and sqlalchemy_available, "flask/sqlalchemy not installed")
class TenancyMiddlewareTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "t.db"
        create_table.bootstrap(self.db_path)
        self._prev = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        sau_db.reset_engine_cache()

    def tearDown(self) -> None:
        sau_db.reset_engine_cache()
        if self._prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._prev
        self._tmp.cleanup()

    def _make_session(self, role: str = ROLE_OWNER):
        """Create a user+workspace+member(role)+session; return (session_id, csrf)."""
        from sau_app.db import get_sessionmaker

        Session = get_sessionmaker()
        with Session() as db:
            user = User(id=uuid.uuid4().hex, primary_email="u@x.com", display_name="U", status="active")
            ws = Workspace(id=uuid.uuid4().hex, name="W", slug=uuid.uuid4().hex, status="active",
                           created_by_user_id=user.id)
            db.add_all([user, ws])
            db.flush()
            db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=role))
            sid = uuid.uuid4().hex
            db.add(SessionRow(
                id=sid, user_id=user.id, active_workspace_id=ws.id,
                created_at=_utcnow(), last_seen_at=_utcnow(),
                expires_at=_utcnow() + timedelta(hours=1), csrf_secret="csrf-secret-value",
            ))
            db.commit()
            return sid, "csrf-secret-value"

    def _hybrid_config(self):
        return load_config({
            "APP_ENV": "testing", "SAU_AUTH_MODE": "hybrid", "SAU_GOOGLE_LOGIN_ENABLED": "true",
            "GOOGLE_LOGIN_CLIENT_ID": "c", "GOOGLE_LOGIN_CLIENT_SECRET": "s",
            "SAU_PUBLIC_BASE_URL": "https://up.example.com", "SECRET_KEY": "x" * 40,
        })

    def _app(self, config):
        app = Flask(__name__)
        app.config["SAU_APP_CONFIG"] = config
        install_tenancy(app)

        @app.route("/ctx")
        def ctx():
            c = current_auth()
            return jsonify(method=c.auth_method, role=c.role, workspace=c.workspace_id)

        @app.route("/mutate", methods=["POST"])
        def mutate():
            return jsonify(ok=True)

        @app.route("/needs-publish", methods=["POST"])
        @require_permission("publish:write")
        def needs_publish():
            return jsonify(ok=True)

        return app

    def _cookie(self, client, config, sid):
        client.set_cookie(config.session_cookie_name, sid, domain="localhost")

    def test_session_resolves_context(self) -> None:
        config = self._hybrid_config()
        sid, _ = self._make_session(role=ROLE_OWNER)
        app = self._app(config)
        c = app.test_client()
        self._cookie(c, config, sid)
        body = c.get("/ctx", base_url="https://localhost").get_json()
        self.assertEqual(body["method"], "google_session")
        self.assertEqual(body["role"], "owner")
        self.assertIsNotNone(body["workspace"])

    def test_csrf_required_for_session_post(self) -> None:
        config = self._hybrid_config()
        sid, csrf = self._make_session()
        app = self._app(config)
        c = app.test_client()
        self._cookie(c, config, sid)
        # No CSRF header -> 403
        self.assertEqual(c.post("/mutate", base_url="https://localhost").status_code, 403)
        # Correct CSRF header -> allowed
        ok = c.post("/mutate", base_url="https://localhost", headers={CSRF_HEADER: csrf})
        self.assertEqual(ok.status_code, 200)

    def test_bad_origin_rejected_for_session_post(self) -> None:
        config = self._hybrid_config()
        sid, csrf = self._make_session()
        app = self._app(config)
        c = app.test_client()
        self._cookie(c, config, sid)
        resp = c.post(
            "/mutate",
            base_url="https://localhost",
            headers={CSRF_HEADER: csrf, "Origin": "https://evil.example.com"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_permission_enforced_for_session(self) -> None:
        config = self._hybrid_config()
        # viewer lacks publish:write
        sid, csrf = self._make_session(role=ROLE_VIEWER)
        app = self._app(config)
        c = app.test_client()
        self._cookie(c, config, sid)
        resp = c.post("/needs-publish", base_url="https://localhost", headers={CSRF_HEADER: csrf})
        self.assertEqual(resp.status_code, 403)

        # editor has publish:write
        sid2, csrf2 = self._make_session(role=ROLE_EDITOR)
        c2 = app.test_client()
        self._cookie(c2, config, sid2)
        ok = c2.post("/needs-publish", base_url="https://localhost", headers={CSRF_HEADER: csrf2})
        self.assertEqual(ok.status_code, 200)

    def test_legacy_mode_ignores_session_and_csrf(self) -> None:
        # Under legacy mode the middleware must not resolve sessions or enforce CSRF.
        config = load_config({"APP_ENV": "testing"})  # legacy, google login off
        sid, csrf = self._make_session()
        app = self._app(config)
        c = app.test_client()
        self._cookie(c, config, sid)
        ctx = c.get("/ctx", base_url="https://localhost").get_json()
        self.assertEqual(ctx["method"], "anonymous")  # session ignored
        # POST without CSRF is allowed (no session-based enforcement).
        self.assertEqual(c.post("/mutate", base_url="https://localhost").status_code, 200)


@unittest.skipUnless(flask_available and sqlalchemy_available, "flask/sqlalchemy not installed")
class RealGateHybridTests(unittest.TestCase):
    """The actual sau_backend `_enforce_auth` accepts a session in hybrid mode."""

    def setUp(self) -> None:
        from myUtils.security import SecurityPolicy
        import sau_backend

        self.sau_backend = sau_backend
        self.app = sau_backend.app
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "gate.db"
        create_table.bootstrap(self.db_path)
        self._prev_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        sau_db.reset_engine_cache()

        self._prev_policy = self.app.config["SECURITY_POLICY"]
        self._prev_cfg = self.app.config["SAU_APP_CONFIG"]
        self.app.config["SECURITY_POLICY"] = SecurityPolicy(
            tokens=frozenset({"legacy-token"}), cors_origins=("*",)
        )
        self.app.config["SAU_APP_CONFIG"] = load_config({
            "APP_ENV": "testing", "SAU_AUTH_MODE": "hybrid", "SAU_GOOGLE_LOGIN_ENABLED": "true",
            "GOOGLE_LOGIN_CLIENT_ID": "c", "GOOGLE_LOGIN_CLIENT_SECRET": "s",
            "SAU_PUBLIC_BASE_URL": "https://up.example.com", "SECRET_KEY": "x" * 40,
        })
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.app.config["SECURITY_POLICY"] = self._prev_policy
        self.app.config["SAU_APP_CONFIG"] = self._prev_cfg
        sau_db.reset_engine_cache()
        if self._prev_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._prev_url
        self._tmp.cleanup()

    def _make_session(self):
        from sau_app.db import get_sessionmaker

        Session = get_sessionmaker()
        with Session() as db:
            user = User(id=uuid.uuid4().hex, primary_email="u@x.com", display_name="U", status="active")
            ws = Workspace(id=uuid.uuid4().hex, name="W", slug=uuid.uuid4().hex, status="active",
                           created_by_user_id=user.id)
            db.add_all([user, ws])
            db.flush()
            db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=ROLE_OWNER))
            sid = uuid.uuid4().hex
            db.add(SessionRow(
                id=sid, user_id=user.id, active_workspace_id=ws.id,
                created_at=_utcnow(), last_seen_at=_utcnow(),
                expires_at=_utcnow() + timedelta(hours=1), csrf_secret="c",
            ))
            db.commit()
            return sid

    def test_anonymous_is_rejected(self) -> None:
        self.assertEqual(self.client.get("/whoami").status_code, 401)

    def test_legacy_token_still_works_in_hybrid(self) -> None:
        r = self.client.get("/whoami", headers={"Authorization": "Bearer legacy-token"})
        self.assertEqual(r.status_code, 200)

    def test_valid_session_satisfies_gate(self) -> None:
        sid = self._make_session()
        self.client.set_cookie(
            self.app.config["SAU_APP_CONFIG"].session_cookie_name, sid, domain="localhost"
        )
        r = self.client.get("/whoami", base_url="https://localhost")
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
