"""Tenancy layer: request-scoped auth context, middleware, and guards (Phase 4).

Everything here is inert under the default ``legacy`` auth mode — the middleware
resolves an anonymous/legacy context and enforces nothing new, so existing
behavior is unchanged. Hybrid/oidc modes (which require Google login to be on)
let a valid server-side session satisfy the auth gate and turn on CSRF/Origin
checks for session-cookie requests.
"""

from __future__ import annotations

from .context import AuthContext, current_auth
from .decorators import require_auth, require_permission
from .middleware import install_tenancy

__all__ = [
    "AuthContext",
    "current_auth",
    "install_tenancy",
    "require_auth",
    "require_permission",
]
