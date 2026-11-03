"""Cross-cutting request hardening: authorization, headers and rate limiting.

Each installer is idempotent-friendly (Flask tolerates repeated
``after_request``/``before_request`` registration, and ``init_extensions``
already guards against double initialisation) and inert when its feature flag
is off, so importing this package never changes an app by itself.
"""
from __future__ import annotations

from .authorization import (
    AREA_PERMISSIONS,
    AREA_RULES,
    area_for,
    install_authorization,
    required_permission,
)
from .headers import BASELINE_HEADERS, DEFAULT_CSP, csp_policy, install_security_headers
from .rate_limit import RateLimiter, install_rate_limiting

__all__ = [
    "AREA_PERMISSIONS",
    "AREA_RULES",
    "BASELINE_HEADERS",
    "DEFAULT_CSP",
    "RateLimiter",
    "area_for",
    "csp_policy",
    "install_authorization",
    "install_rate_limiting",
    "install_security_headers",
    "required_permission",
]
