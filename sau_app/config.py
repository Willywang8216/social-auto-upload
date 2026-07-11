"""Validated application configuration (Phase 1).

Reads deployment settings from the environment into an immutable ``AppConfig``
and enforces fail-closed rules **in production only**. Development and test
environments keep today's permissive behavior (open mode allowed, no secrets
required) so nothing existing breaks.

The intent is to give later phases a single place to add mandatory secrets
(``SECRET_KEY``, ``DATABASE_URL``, ``REDIS_URL``, ``CREDENTIAL_MASTER_KEY``…)
and have production startup refuse to boot when they are missing, while dev and
CI stay frictionless.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

DEFAULT_REQUEST_ID_HEADER = "X-Request-ID"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid in production."""


@dataclass(frozen=True)
class AppConfig:
    env: str
    debug: bool
    secret_key: str | None
    api_tokens_configured: bool
    request_id_header: str
    warnings: tuple[str, ...] = ()

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def is_testing(self) -> bool:
        return self.env == "testing"


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _as_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config(environ: Mapping[str, str] | None = None) -> AppConfig:
    """Build and validate an :class:`AppConfig` from ``environ`` (or ``os.environ``).

    Raises :class:`ConfigError` when ``APP_ENV=production`` and a mandatory
    setting is missing or unsafe. In any other environment the same problems are
    recorded as ``warnings`` and the app boots normally.
    """

    env_map = os.environ if environ is None else environ

    env = (_clean(env_map.get("APP_ENV")) or _clean(env_map.get("SAU_ENV")) or "development").lower()
    secret_key = _clean(env_map.get("SECRET_KEY"))
    api_tokens_configured = bool(_clean(env_map.get("SAU_API_TOKENS")))
    # DEBUG defaults off in production, on elsewhere (matches conf.example DEBUG_MODE).
    debug = _as_bool(env_map.get("DEBUG_MODE"), default=(env != "production"))
    request_id_header = _clean(env_map.get("SAU_REQUEST_ID_HEADER")) or DEFAULT_REQUEST_ID_HEADER

    # Fail-closed rules that only bite in production.
    problems: list[str] = []
    if secret_key is None:
        problems.append("SECRET_KEY is not set")
    if not api_tokens_configured:
        problems.append("SAU_API_TOKENS is empty — the API would run in open (unauthenticated) mode")
    if debug:
        problems.append("DEBUG_MODE is enabled")

    if env == "production" and problems:
        raise ConfigError(
            "refusing to start in production with unsafe configuration: "
            + "; ".join(problems)
        )

    return AppConfig(
        env=env,
        debug=debug,
        secret_key=secret_key,
        api_tokens_configured=api_tokens_configured,
        request_id_header=request_id_header,
        warnings=tuple(problems),
    )
