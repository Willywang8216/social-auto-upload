"""Tests for the Phase 1 application-factory layer (``sau_app``).

Covers config validation, the health/readiness blueprint, request/correlation
IDs, the standard JSON error schema, factory idempotency, and that the health
endpoints bypass the bearer-token gate. These tests are self-contained: they do
not write to the shared ``BASE_DIR`` database (unlike ``test_security_http.py``),
so they are safe to run in CI.
"""

from __future__ import annotations

import importlib.util
import unittest

from sau_app.config import ConfigError, load_config


flask_available = importlib.util.find_spec("flask") is not None


class ConfigValidationTests(unittest.TestCase):
    def test_development_reports_warnings_without_raising(self) -> None:
        cfg = load_config({"APP_ENV": "development"})
        self.assertFalse(cfg.is_production)
        self.assertTrue(cfg.debug)
        # Missing secret key / open mode / debug are recorded, not fatal.
        self.assertTrue(cfg.warnings)
        self.assertFalse(cfg.api_tokens_configured)

    def test_defaults_to_development_when_env_unset(self) -> None:
        cfg = load_config({})
        self.assertEqual(cfg.env, "development")

    def test_production_fails_closed_when_secrets_missing(self) -> None:
        with self.assertRaises(ConfigError):
            load_config({"APP_ENV": "production"})

    def test_production_fails_closed_in_open_mode(self) -> None:
        # SECRET_KEY set but no API tokens -> still refuses (open mode).
        with self.assertRaises(ConfigError):
            load_config(
                {"APP_ENV": "production", "SECRET_KEY": "x" * 32, "DEBUG_MODE": "false"}
            )

    def test_production_fails_closed_when_debug_enabled(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(
                {
                    "APP_ENV": "production",
                    "SECRET_KEY": "x" * 32,
                    "SAU_API_TOKENS": "tok",
                    "DEBUG_MODE": "true",
                }
            )

    def test_production_boots_when_fully_configured(self) -> None:
        cfg = load_config(
            {
                "APP_ENV": "production",
                "SECRET_KEY": "x" * 32,
                "SAU_API_TOKENS": "tok",
                "DEBUG_MODE": "false",
            }
        )
        self.assertTrue(cfg.is_production)
        self.assertFalse(cfg.debug)
        self.assertEqual(cfg.warnings, ())

    def test_custom_request_id_header(self) -> None:
        cfg = load_config({"APP_ENV": "development", "SAU_REQUEST_ID_HEADER": "X-Trace-Id"})
        self.assertEqual(cfg.request_id_header, "X-Trace-Id")


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class HealthEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        from sau_backend import app

        self.app = app
        self.client = app.test_client()

    def test_healthz_liveness(self) -> None:
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "ok")
        self.assertEqual(set(resp.get_json()), {"status", "revision"})

    def test_readyz_returns_checks(self) -> None:
        resp = self.client.get("/readyz")
        self.assertIn(resp.status_code, (200, 503))
        body = resp.get_json()
        self.assertIn("status", body)
        self.assertIn("checks", body)
        # The backend registers a database readiness probe.
        self.assertIn("database", body["checks"])

    def test_readyz_reports_not_ready_when_a_check_fails(self) -> None:
        from sau_app.health import READINESS_CHECKS_KEY

        original = dict(self.app.config.get(READINESS_CHECKS_KEY, {}))
        self.app.config[READINESS_CHECKS_KEY] = {
            **original,
            "explode": lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        }
        try:
            resp = self.client.get("/readyz")
            self.assertEqual(resp.status_code, 503)
            body = resp.get_json()
            self.assertEqual(body["status"], "not_ready")
            self.assertEqual(body["checks"]["explode"], "error")
        finally:
            self.app.config[READINESS_CHECKS_KEY] = original

    def test_health_bypasses_auth_in_protected_mode(self) -> None:
        from myUtils.security import SecurityPolicy

        previous = self.app.config["SECURITY_POLICY"]
        self.app.config["SECURITY_POLICY"] = SecurityPolicy(
            tokens=frozenset({"top-secret"}), cors_origins=("*",)
        )
        try:
            self.assertEqual(self.client.get("/healthz").status_code, 200)
            # Readiness may legitimately fail without a worker/database; it
            # must still bypass the application authentication gate.
            self.assertIn(self.client.get("/readyz").status_code, (200, 503))
            # A protected route without the token is still rejected.
            self.assertEqual(self.client.get("/whoami").status_code, 401)
        finally:
            self.app.config["SECURITY_POLICY"] = previous


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class RequestIdTests(unittest.TestCase):
    def setUp(self) -> None:
        from sau_backend import app

        self.client = app.test_client()

    def test_response_carries_a_request_id(self) -> None:
        resp = self.client.get("/healthz")
        self.assertTrue(resp.headers.get("X-Request-ID"))

    def test_inbound_request_id_is_preserved(self) -> None:
        resp = self.client.get("/healthz", headers={"X-Request-ID": "corr-abc-123"})
        self.assertEqual(resp.headers.get("X-Request-ID"), "corr-abc-123")

    def test_distinct_requests_get_distinct_ids(self) -> None:
        a = self.client.get("/healthz").headers.get("X-Request-ID")
        b = self.client.get("/healthz").headers.get("X-Request-ID")
        self.assertTrue(a and b and a != b)


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class ErrorSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        import os

        from sau_backend import app

        # Ensure open mode (no auth tokens) so unmatched routes reach Flask's
        # error handlers instead of being intercepted by the auth gate.
        self._prev_tokens = os.environ.pop("SAU_API_TOKENS", None)
        from myUtils.security import load_policy

        self._prev_policy = app.config.get("SECURITY_POLICY")
        app.config["SECURITY_POLICY"] = load_policy()
        self.client = app.test_client()

    def tearDown(self) -> None:
        import os

        from sau_backend import app

        if self._prev_tokens is not None:
            os.environ["SAU_API_TOKENS"] = self._prev_tokens
        else:
            os.environ.pop("SAU_API_TOKENS", None)
        if self._prev_policy is not None:
            app.config["SECURITY_POLICY"] = self._prev_policy

    def test_unmatched_route_returns_json_404(self) -> None:
        resp = self.client.get("/this-route-does-not-exist-xyz")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.content_type.split(";")[0], "application/json")
        body = resp.get_json()
        self.assertEqual(body["code"], 404)
        self.assertIn("requestId", body)
        self.assertIsNone(body["data"])

    def test_method_not_allowed_returns_json_405(self) -> None:
        resp = self.client.delete("/whoami")
        self.assertEqual(resp.status_code, 405)
        self.assertEqual(resp.get_json()["code"], 405)


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class FactoryTests(unittest.TestCase):
    def test_create_app_returns_a_fully_wired_independent_instance(self) -> None:
        """Each call builds a distinct, completely wired application.

        This deliberately replaced the earlier contract, where ``create_app()``
        handed back the monolith's module-global app. A factory that always
        returns the same object cannot give a caller its own configuration.
        """
        from sau_app import create_app
        from sau_app.config import AppConfig

        app1 = create_app()
        app2 = create_app()
        self.assertIsNot(app1, app2)
        for app in (app1, app2):
            self.assertIsInstance(app.config.get("SAU_APP_CONFIG"), AppConfig)
            self.assertIn("sau_health", app.blueprints)
            # Registered exactly once per instance, not once per process.
            health_rules = [
                r for r in app.url_map.iter_rules()
                if r.endpoint.startswith("sau_health.")
            ]
            self.assertEqual(len(health_rules), 2)

    def test_init_extensions_does_not_double_register(self) -> None:
        from sau_app import init_extensions
        from sau_backend import app

        before = len(list(app.url_map.iter_rules()))
        init_extensions(app)
        init_extensions(app)
        after = len(list(app.url_map.iter_rules()))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
