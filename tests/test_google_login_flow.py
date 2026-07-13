"""End-to-end Google login flow against a mocked OIDC client (Phase 3).

Exercises the whole transport — start → callback → session → logout — with a
fake Google client, so no real credentials or network are needed. Verifies:
first-login provisioning, secure session cookie, session introspection with
permissions + CSRF, CSRF-protected logout, session revocation, and state
replay/`error` rejection.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

flask_available = importlib.util.find_spec("flask") is not None
sqlalchemy_available = importlib.util.find_spec("sqlalchemy") is not None

if flask_available and sqlalchemy_available:
    from flask import Flask

    import db.createTable as create_table
    from sau_app import db as sau_db
    from sau_app.auth.oidc import GoogleClaims, OIDCError
    from sau_app.auth.routes import register_auth
    from sau_app.config import load_config

    class _FakeGoogle:
        """Stand-in for GoogleOIDCClient — records state/nonce, returns claims."""

        def __init__(self, *, claims: GoogleClaims | None = None, raise_verify: bool = False):
            self.state = None
            self.nonce = None
            self.raise_verify = raise_verify
            self._claims = claims or GoogleClaims(
                subject="google-sub-xyz",
                email="zoe@example.com",
                email_verified=True,
                name="Zoe",
                picture="https://img/zoe.png",
                raw={"sub": "google-sub-xyz"},
            )

        def authorization_url(self, *, state, nonce, code_challenge, redirect_uri):
            self.state, self.nonce = state, nonce
            return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}"

        def fetch_claims(self, *, code, code_verifier, redirect_uri, expected_nonce):
            if self.raise_verify:
                raise OIDCError("forced verify failure")
            assert expected_nonce == self.nonce, "nonce must match the stored transaction"
            return self._claims


@unittest.skipUnless(flask_available and sqlalchemy_available, "flask/sqlalchemy not installed")
class GoogleLoginFlowTests(unittest.TestCase):
    def _make_app(self, fake: "_FakeGoogle") -> Flask:
        config = load_config(
            {
                "APP_ENV": "testing",
                "SAU_GOOGLE_LOGIN_ENABLED": "true",
                "GOOGLE_LOGIN_CLIENT_ID": "client-id",
                "GOOGLE_LOGIN_CLIENT_SECRET": "client-secret",
                "SAU_PUBLIC_BASE_URL": "https://up.example.com",
                "SECRET_KEY": "x" * 40,
            }
        )
        app = Flask(__name__)
        app.config["SAU_APP_CONFIG"] = config
        app.config["SAU_OIDC_CLIENT"] = fake
        register_auth(app, config)
        self.config = config
        return app

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "auth.db"
        create_table.bootstrap(self.db_path)  # creates identity tables via 0015/0016
        self._prev_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        sau_db.reset_engine_cache()

    def tearDown(self) -> None:
        sau_db.reset_engine_cache()
        if self._prev_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._prev_url
        self._tmp.cleanup()

    def _client(self, app: Flask):
        # https so the Secure/__Host- session cookie round-trips in the test client.
        return app.test_client()

    def _get(self, client, path):
        return client.get(path, base_url="https://localhost")

    def _post(self, client, path, **kw):
        return client.post(path, base_url="https://localhost", **kw)

    def test_full_login_logout_flow(self) -> None:
        fake = _FakeGoogle()
        app = self._make_app(fake)
        client = self._client(app)

        # 1) start -> 302 to Google, transaction stored server-side
        start = self._get(client, "/auth/google/start")
        self.assertEqual(start.status_code, 302)
        self.assertIn("accounts.google.com", start.headers["Location"])
        self.assertIsNotNone(fake.state)

        # 2) callback -> provisions user+workspace+session, sets cookie
        cb = self._get(client, f"/auth/google/callback?state={fake.state}&code=auth-code")
        self.assertEqual(cb.status_code, 302)
        self.assertIn("/#/dashboard", cb.headers["Location"])
        self.assertIn(self.config.session_cookie_name, cb.headers.get("Set-Cookie", ""))

        # 3) session introspection reflects the logged-in user
        sess = self._get(client, "/api/v1/session")
        self.assertEqual(sess.status_code, 200)
        body = sess.get_json()
        self.assertTrue(body["authenticated"])
        self.assertEqual(body["user"]["email"], "zoe@example.com")
        self.assertEqual(body["workspace"]["role"], "owner")
        self.assertIn("publish:write", body["permissions"])
        csrf = body["csrfToken"]
        self.assertTrue(csrf)

        # 4) logout without CSRF is rejected
        no_csrf = self._post(client, "/api/v1/logout")
        self.assertEqual(no_csrf.status_code, 403)

        # 5) logout with CSRF succeeds and revokes the session
        ok = self._post(client, "/api/v1/logout", headers={"X-CSRF-Token": csrf})
        self.assertEqual(ok.status_code, 200)

        after = self._get(client, "/api/v1/session")
        self.assertFalse(after.get_json()["authenticated"])

    def test_repeat_login_reuses_user(self) -> None:
        fake = _FakeGoogle()
        app = self._make_app(fake)
        c1 = self._client(app)
        self._get(c1, "/auth/google/start")
        self._get(c1, f"/auth/google/callback?state={fake.state}&code=c1")
        # A second, independent login for the same Google sub.
        c2 = self._client(app)
        self._get(c2, "/auth/google/start")
        self._get(c2, f"/auth/google/callback?state={fake.state}&code=c2")

        from sqlalchemy import func, select

        from sau_app.db.identity_models import User, Workspace

        Session = sau_db.get_sessionmaker()
        with Session() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(User)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(Workspace)), 1)

    def test_state_replay_is_rejected(self) -> None:
        fake = _FakeGoogle()
        app = self._make_app(fake)
        client = self._client(app)
        self._get(client, "/auth/google/start")
        state = fake.state
        first = self._get(client, f"/auth/google/callback?state={state}&code=x")
        self.assertIn("/#/dashboard", first.headers["Location"])
        # Reusing the same (now consumed) state must fail.
        replay = self._get(client, f"/auth/google/callback?state={state}&code=x")
        self.assertIn("error=state", replay.headers["Location"])

    def test_unknown_state_is_rejected(self) -> None:
        app = self._make_app(_FakeGoogle())
        client = self._client(app)
        resp = self._get(client, "/auth/google/callback?state=never-issued&code=x")
        self.assertIn("error=state", resp.headers["Location"])

    def test_verification_failure_redirects_to_error(self) -> None:
        fake = _FakeGoogle(raise_verify=True)
        app = self._make_app(fake)
        client = self._client(app)
        self._get(client, "/auth/google/start")
        resp = self._get(client, f"/auth/google/callback?state={fake.state}&code=x")
        self.assertIn("error=verify", resp.headers["Location"])

    def test_provider_error_param_redirects_to_login(self) -> None:
        app = self._make_app(_FakeGoogle())
        client = self._client(app)
        resp = self._get(client, "/auth/google/callback?error=access_denied")
        self.assertIn("error=oauth", resp.headers["Location"])


@unittest.skipUnless(flask_available and sqlalchemy_available, "flask/sqlalchemy not installed")
class GoogleLoginDisabledTests(unittest.TestCase):
    def test_blueprint_not_registered_when_flag_off(self) -> None:
        from flask import Flask

        config = load_config({"APP_ENV": "testing"})
        app = Flask(__name__)
        app.config["SAU_APP_CONFIG"] = config
        register_auth(app, config)
        self.assertNotIn("sau_auth", app.blueprints)


if __name__ == "__main__":
    unittest.main()
