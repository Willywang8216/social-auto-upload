"""In-process request rate limiting (Phase 1 hardening).

Scope and honesty about it
--------------------------
This is a **per-process** limiter. Production runs a single Gunicorn worker
with threads (see ``docker-compose.yml``), so one process is the whole web
tier and an in-process counter is accurate there. It is *not* a distributed
limiter: if the web tier is ever scaled to several processes or hosts, the
effective limit multiplies per process. That limitation is recorded here
rather than hidden behind a library that implies otherwise.

What it protects: the routes where an unthrottled caller causes real damage or
cost — OAuth start/refresh, batch account operations, uploads, and
credential/cookie handling. Public health and static asset paths are exempt so
probes and the SPA keep working.

Configuration
-------------
``SAU_RATE_LIMIT_ENABLED``
    ``1``/``0``. Default: on in production, off while testing.
``SAU_RATE_LIMIT_PER_MINUTE``
    Default ceiling for any non-public route without a specific rule.
``SAU_RATE_LIMIT_STRICT_PER_MINUTE``
    Ceiling for the strict routes (OAuth, uploads, batch, credentials).
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass, field

from flask import Flask, current_app, jsonify, request

_TRUTHY = {"1", "true", "yes", "on"}

#: Route prefixes that get the stricter ceiling.
STRICT_PREFIXES: tuple[str, ...] = (
    "/oauth/",
    "/upload",
    "/accounts/batch/",
    "/accounts/import-cookies",
    "/accounts/tiktok/refresh-stale",
    "/api/auth/cookies",
    "/downloadCookie",
    "/admin/oauth/status",
    "/login",
)

#: Never throttled: liveness/readiness probes and static assets.
EXEMPT_PREFIXES: tuple[str, ...] = ("/healthz", "/readyz", "/assets/", "/static/")


def _int_env(name: str, default: int, environ: dict | None = None) -> int:
    env = environ if environ is not None else os.environ
    try:
        value = int(str(env.get(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def enabled(environ: dict | None = None, *, testing: bool = False) -> bool:
    env = environ if environ is not None else os.environ
    raw = str(env.get("SAU_RATE_LIMIT_ENABLED", "")).strip().lower()
    if raw:
        return raw in _TRUTHY
    if testing or str(env.get("SAU_TESTING", "")).strip() in {"1", "true", "yes"}:
        return False
    return str(env.get("APP_ENV", "")).strip().lower() == "production"


@dataclass
class _Window:
    started: float
    count: int = 0


@dataclass
class RateLimiter:
    """Thread-safe fixed-window counter keyed by ``(bucket, identity)``."""

    limit: int
    window_seconds: float = 60.0
    clock: object = time.monotonic
    _windows: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def check(self, key: str) -> tuple[bool, int]:
        """Return ``(allowed, retry_after_seconds)`` for one hit on ``key``."""
        now = self.clock()
        with self._lock:
            window = self._windows.get(key)
            if window is None or now - window.started >= self.window_seconds:
                window = _Window(started=now, count=0)
                self._windows[key] = window
            window.count += 1
            if window.count > self.limit:
                retry_after = max(1, int(self.window_seconds - (now - window.started)) or 1)
                return False, retry_after
            # Opportunistic pruning keeps the dict bounded on a long-lived process.
            if len(self._windows) > 4096:
                cutoff = now - self.window_seconds
                for name in [k for k, w in self._windows.items() if w.started < cutoff]:
                    self._windows.pop(name, None)
            return True, 0


def _client_identity() -> str:
    """A stable, non-reversible key for the caller.

    Prefers the bearer token (hashed) so one leaked token cannot be used to
    exhaust another caller's budget, then falls back to the peer address.
    """
    authorization = request.headers.get("Authorization") or ""
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            return "t:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
    forwarded = request.headers.get("X-Forwarded-For") or ""
    if forwarded:
        return "a:" + forwarded.split(",")[0].strip()
    return "a:" + (request.remote_addr or "unknown")


def _bucket_for(path: str) -> str:
    for prefix in STRICT_PREFIXES:
        if path.startswith(prefix):
            return "strict"
    return "default"


def install_rate_limiting(app: Flask, *, environ: dict | None = None) -> RateLimiter | None:
    """Attach the limiter to ``app``. Returns the limiter, or None if disabled."""
    env = environ if environ is not None else os.environ
    if not enabled(env, testing=bool(app.config.get("TESTING"))):
        return None

    strict_limit = _int_env("SAU_RATE_LIMIT_STRICT_PER_MINUTE", 60, env)
    default_limit = _int_env("SAU_RATE_LIMIT_PER_MINUTE", 600, env)
    limiters = {
        "strict": RateLimiter(limit=strict_limit),
        "default": RateLimiter(limit=default_limit),
    }
    app.config["SAU_RATE_LIMITERS"] = limiters

    @app.before_request
    def _enforce_rate_limit():  # noqa: ANN001
        path = request.path or "/"
        if request.method == "OPTIONS":
            return None
        if any(path.startswith(prefix) for prefix in EXEMPT_PREFIXES):
            return None
        policy = current_app.config.get("SECURITY_POLICY")
        # Public paths are already covered by their own controls; throttling
        # them would break provider callbacks and legal documents.
        if policy is not None and policy.is_public_path(path):
            return None
        limiter = limiters[_bucket_for(path)]
        allowed, retry_after = limiter.check(f"{_bucket_for(path)}:{_client_identity()}")
        if not allowed:
            response = jsonify({
                "code": 429,
                "msg": "Too many requests; slow down and retry shortly",
                "data": None,
            })
            response.status_code = 429
            response.headers["Retry-After"] = str(retry_after)
            return response
        return None

    return limiters["default"]
