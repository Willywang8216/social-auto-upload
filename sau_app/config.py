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
    # Compatibility mode flags (Phase 4). Defaults preserve today's behavior:
    #   auth_mode=legacy   -> only the SAU_API_TOKENS bearer gate; sessions are
    #                         ignored by the gate even if a cookie is present.
    #   auth_mode=hybrid   -> bearer token OR a valid Google session.
    #   auth_mode=oidc     -> Google session is the normal browser auth.
    #   tenancy_mode=single/shadow/enforced -> how strictly workspace scope is
    #                         applied (enforcement arrives with the tenant schema).
    auth_mode: str = "legacy"
    tenancy_mode: str = "single"
    # Google OpenID Connect application login (Phase 3). Disabled by default so
    # the legacy bearer-token flow is untouched until an operator opts in.
    google_login_enabled: bool = False
    google_login_client_id: str | None = None
    google_login_client_secret: str | None = None
    public_base_url: str | None = None
    frontend_origin: str | None = None
    session_cookie_name: str = "__Host-sau_session"
    session_idle_seconds: int = 60 * 60 * 12
    session_absolute_seconds: int = 60 * 60 * 24 * 14
    warnings: tuple[str, ...] = ()

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def is_testing(self) -> bool:
        return self.env == "testing"

    @property
    def sessions_honored_by_gate(self) -> bool:
        """Whether a valid session may satisfy the auth gate (hybrid/oidc)."""
        return self.auth_mode in {"hybrid", "oidc"}

    @property
    def google_login_redirect_uri(self) -> str | None:
        """Server-derived callback URI — never taken from the client."""
        if not self.public_base_url:
            return None
        return self.public_base_url.rstrip("/") + "/auth/google/callback"


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _as_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None, *, default: int) -> int:
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


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

    auth_mode = (_clean(env_map.get("SAU_AUTH_MODE")) or "legacy").lower()
    tenancy_mode = (_clean(env_map.get("SAU_TENANCY_MODE")) or "single").lower()
    google_login_enabled = _as_bool(env_map.get("SAU_GOOGLE_LOGIN_ENABLED"), default=False)
    google_login_client_id = _clean(env_map.get("GOOGLE_LOGIN_CLIENT_ID"))
    google_login_client_secret = _clean(env_map.get("GOOGLE_LOGIN_CLIENT_SECRET"))
    public_base_url = _clean(env_map.get("SAU_PUBLIC_BASE_URL"))
    frontend_origin = _clean(env_map.get("SAU_FRONTEND_ORIGIN")) or public_base_url
    session_cookie_name = _clean(env_map.get("SESSION_COOKIE_NAME")) or "__Host-sau_session"
    session_idle_seconds = _as_int(env_map.get("SESSION_IDLE_SECONDS"), default=60 * 60 * 12)
    session_absolute_seconds = _as_int(
        env_map.get("SESSION_ABSOLUTE_SECONDS"), default=60 * 60 * 24 * 14
    )

    # Fail-closed rules that only bite in production.
    problems: list[str] = []
    if secret_key is None:
        problems.append("SECRET_KEY is not set")
    if not api_tokens_configured:
        problems.append("SAU_API_TOKENS is empty — the API would run in open (unauthenticated) mode")
    if debug:
        problems.append("DEBUG_MODE is enabled")

    if auth_mode not in {"legacy", "hybrid", "oidc"}:
        problems.append(f"SAU_AUTH_MODE has an invalid value {auth_mode!r}")
        auth_mode = "legacy"
    if tenancy_mode not in {"single", "shadow", "enforced"}:
        problems.append(f"SAU_TENANCY_MODE has an invalid value {tenancy_mode!r}")
        tenancy_mode = "single"
    # Honouring sessions at the gate is meaningless without the login flow.
    if auth_mode in {"hybrid", "oidc"} and not google_login_enabled:
        problems.append(
            f"SAU_AUTH_MODE={auth_mode} requires SAU_GOOGLE_LOGIN_ENABLED (no session source otherwise)"
        )

    # When Google login is switched on, its settings become mandatory.
    if google_login_enabled:
        if not google_login_client_id:
            problems.append("SAU_GOOGLE_LOGIN_ENABLED is on but GOOGLE_LOGIN_CLIENT_ID is not set")
        if not google_login_client_secret:
            problems.append("SAU_GOOGLE_LOGIN_ENABLED is on but GOOGLE_LOGIN_CLIENT_SECRET is not set")
        if not public_base_url:
            problems.append("SAU_GOOGLE_LOGIN_ENABLED is on but SAU_PUBLIC_BASE_URL is not set")
        if not secret_key:
            problems.append("SAU_GOOGLE_LOGIN_ENABLED is on but SECRET_KEY is not set (needed to sign sessions)")

    # Google-login misconfiguration fails closed whenever it is enabled — even
    # outside production — because a half-configured login flow is unsafe.
    fatal = (env == "production" and problems) or (
        google_login_enabled and any("GOOGLE_LOGIN" in p or "PUBLIC_BASE_URL" in p for p in problems)
    )
    if fatal:
        raise ConfigError(
            "refusing to start with unsafe configuration: " + "; ".join(problems)
        )

    return AppConfig(
        env=env,
        debug=debug,
        secret_key=secret_key,
        api_tokens_configured=api_tokens_configured,
        request_id_header=request_id_header,
        auth_mode=auth_mode,
        tenancy_mode=tenancy_mode,
        google_login_enabled=google_login_enabled,
        google_login_client_id=google_login_client_id,
        google_login_client_secret=google_login_client_secret,
        public_base_url=public_base_url,
        frontend_origin=frontend_origin,
        session_cookie_name=session_cookie_name,
        session_idle_seconds=session_idle_seconds,
        session_absolute_seconds=session_absolute_seconds,
        warnings=tuple(problems),
    )
