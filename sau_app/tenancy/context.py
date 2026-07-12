"""The immutable per-request auth context (Phase 4).

Resolved once by the middleware and stashed on ``flask.g``. Route handlers and
(later) repositories read it to know *who* is calling and *which* workspace they
are scoped to, rather than trusting anything from the request body.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from flask import g

# auth_method values
METHOD_ANONYMOUS = "anonymous"
METHOD_LEGACY_TOKEN = "legacy_token"
METHOD_GOOGLE_SESSION = "google_session"


@dataclass(frozen=True)
class AuthContext:
    authenticated: bool
    auth_method: str
    user_id: str | None = None
    workspace_id: str | None = None
    role: str | None = None
    permissions: frozenset[str] = field(default_factory=frozenset)
    session_id: str | None = None

    @classmethod
    def anonymous(cls) -> "AuthContext":
        return cls(authenticated=False, auth_method=METHOD_ANONYMOUS)

    @classmethod
    def legacy_token(cls) -> "AuthContext":
        # A valid legacy bearer token is treated as full access. It maps to the
        # legacy workspace once that is provisioned (Phase 5); until then the
        # workspace is unset and scoping stays in compatibility mode.
        return cls(
            authenticated=True,
            auth_method=METHOD_LEGACY_TOKEN,
            role="owner",
        )

    def has_permission(self, permission: str) -> bool:
        # Legacy-token callers are unscoped and fully trusted (single-tenant).
        if self.auth_method == METHOD_LEGACY_TOKEN:
            return True
        return permission in self.permissions


def current_auth() -> AuthContext:
    """Return the request's :class:`AuthContext` (anonymous if unresolved)."""
    ctx = getattr(g, "auth_ctx", None)
    if isinstance(ctx, AuthContext):
        return ctx
    return AuthContext.anonymous()
