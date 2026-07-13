"""Idempotent wiring of the factory-layer extensions onto a Flask app.

``init_extensions(app)`` is called from two places — the legacy monolith at
import time and ``sau_app.create_app()`` — so it must be safe to run more than
once. A flag on ``app.config`` guards against double registration (which Flask
would otherwise reject for the health blueprint).
"""

from __future__ import annotations

import logging
from typing import Callable, Mapping

from flask import Flask

from . import observability
from .config import AppConfig, load_config
from .health import READINESS_CHECKS_KEY, health_bp

_INIT_FLAG = "SAU_EXTENSIONS_INITIALIZED"
_LOGGER = logging.getLogger("sau.extensions")

ReadinessCheck = Callable[[], object]


def init_extensions(app: Flask, *, environ: Mapping[str, str] | None = None) -> AppConfig:
    """Attach config, observability, and the health blueprint to ``app`` once.

    Returns the resolved :class:`AppConfig`. In production a missing/unsafe
    setting raises :class:`~sau_app.config.ConfigError` (fail closed); in dev and
    test the same issues are logged as warnings and startup continues.
    """

    existing = app.config.get("SAU_APP_CONFIG")
    if app.config.get(_INIT_FLAG) and isinstance(existing, AppConfig):
        return existing

    config = load_config(environ)
    app.config["SAU_APP_CONFIG"] = config
    app.config["SAU_REQUEST_ID_HEADER"] = config.request_id_header
    if config.secret_key:
        app.config.setdefault("SECRET_KEY", config.secret_key)
    app.config.setdefault(READINESS_CHECKS_KEY, {})

    for warning in config.warnings:
        _LOGGER.warning("[config] %s", warning)

    observability.install(app, header_name=config.request_id_header)

    # Tenancy middleware resolves the per-request AuthContext and (only for
    # session-cookie requests) enforces CSRF/Origin. Inert under the default
    # legacy mode. Registered after request-id so its before_request runs early,
    # before the legacy auth gate.
    from .tenancy import install_tenancy  # local import: pulls in the SQLAlchemy stack

    install_tenancy(app)

    if "sau_health" not in app.blueprints:
        app.register_blueprint(health_bp)

    # Google login blueprint — a no-op unless SAU_GOOGLE_LOGIN_ENABLED is set.
    from .auth import register_auth  # local import: pulls in SQLAlchemy/auth stack

    register_auth(app, config)

    app.config[_INIT_FLAG] = True
    return config


def register_readiness_check(app: Flask, name: str, check: ReadinessCheck) -> None:
    """Register a named readiness probe consulted by ``GET /readyz``."""

    checks = app.config.setdefault(READINESS_CHECKS_KEY, {})
    checks[name] = check
