"""Route-level permission enforcement (Phase 1 hardening).

Why this exists
---------------
``sau_app/auth/permissions.py`` has always defined viewer/editor/admin/owner
permissions and the frontend has always used them to hide controls — but no
route enforced them. A viewer session could call every write endpoint the UI
merely greyed out. This module closes that gap in one place instead of adding a
decorator to ~120 handlers.

Who it applies to
-----------------
Only **session** callers (``google_session``) are role-checked.

* A legacy bearer token is the trusted single-tenant operator path;
  ``AuthContext.has_permission`` already returns ``True`` for it, so behaviour
  there is unchanged.
* Anonymous callers are left alone here — the existing bearer gate rejects
  them, and this module deliberately runs *before* it so a permission failure
  is reported as 403 rather than being masked by a 401.

Because Google login is disabled in the current deployment, this layer is
inert in production until that is deliberately enabled. It is what makes
enabling it safe.
"""
from __future__ import annotations

from flask import Flask, current_app, jsonify, request

from ..auth import permissions as perms
from ..tenancy.context import current_auth

_READ = "read"
_WRITE = "write"

#: Paths that any authenticated caller may use regardless of role.
EXEMPT_PATHS: frozenset[str] = frozenset({
    "/whoami",   # the login screen probes this
    "/login",    # SSE stream; reads its own token from the query string
})

#: Prefixes that are never role-gated.
EXEMPT_PREFIXES: tuple[str, ...] = ("/api/publish/health",)

#: ``(prefix, area)`` — the LONGEST matching prefix wins, so ordering here is
#: for readability only. Keep this table exhaustive: the test suite asserts
#: every non-public mutating route resolves to a permission.
AREA_RULES: tuple[tuple[str, str], ...] = (
    ("/api/auth/cookies", "credentials"),
    ("/downloadCookie", "credentials"),
    ("/uploadCookie", "credentials"),
    ("/accounts/import-cookies", "credentials"),
    ("/api/data-deletion-request", "credentials"),
    ("/admin", "admin"),
    ("/account", "accounts"),
    ("/deleteAccount", "accounts"),
    ("/updateUserinfo", "accounts"),
    ("/getAccounts", "accounts"),
    ("/getValidAccounts", "accounts"),
    ("/tiktok", "accounts"),
    ("/api/accounts", "accounts"),
    ("/oauth/", "accounts"),
    ("/api/media-groups", "media"),
    ("/api/media", "media"),
    ("/api/watermark-configs", "media"),
    ("/media", "media"),
    ("/media-groups", "media"),
    ("/uploadSave", "media"),
    ("/upload", "media"),
    ("/getFiles", "media"),
    ("/getFile", "media"),
    ("/deleteFile", "media"),
    ("/deleteFiles", "media"),
    ("/postVideo", "publish"),
    ("/api/campaigns", "publish"),
    ("/api/inbox", "publish"),
    ("/api/publish", "publish"),
    ("/campaigns", "publish"),
    ("/publish-center", "publish"),
    ("/publish-entities", "publish"),
    ("/publish-templates", "publish"),
    ("/jobs", "publish"),
    ("/analytics", "analytics"),
    ("/api/sheet-exports", "analytics"),
    ("/profiles", "profiles"),
)

#: ``(area, mode) -> permission``.
AREA_PERMISSIONS: dict[tuple[str, str], str] = {
    ("accounts", _READ): perms.ACCOUNTS_READ,
    ("accounts", _WRITE): perms.ACCOUNTS_CONNECT,
    ("credentials", _READ): perms.CREDENTIALS_EXPORT,
    ("credentials", _WRITE): perms.CREDENTIALS_EXPORT,
    ("admin", _READ): perms.WORKSPACE_MANAGE,
    ("admin", _WRITE): perms.WORKSPACE_MANAGE,
    ("media", _READ): perms.MEDIA_READ,
    ("media", _WRITE): perms.MEDIA_WRITE,
    ("publish", _READ): perms.PUBLISH_WRITE,
    ("publish", _WRITE): perms.PUBLISH_WRITE,
    ("analytics", _READ): perms.ANALYTICS_READ,
    ("analytics", _WRITE): perms.PUBLISH_WRITE,
    ("profiles", _READ): perms.PROFILES_READ,
    ("profiles", _WRITE): perms.PROFILES_WRITE,
}

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def area_for(path: str) -> str | None:
    """Return the area owning ``path``, or None when nothing matches."""
    best: tuple[int, str] | None = None
    for prefix, area in AREA_RULES:
        if path == prefix or path.startswith(prefix.rstrip("/") + "/") or path.startswith(prefix):
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), area)
    return best[1] if best else None


def required_permission(method: str, path: str) -> str | None:
    """The permission a session caller needs for this request, or None.

    None means "no area rule applies" — either an explicitly exempt path or a
    route outside the mapped surface (the suite asserts the latter is empty for
    mutating routes).
    """
    if path in EXEMPT_PATHS or any(path.startswith(p) for p in EXEMPT_PREFIXES):
        return None
    area = area_for(path)
    if area is None:
        return None
    mode = _WRITE if method.upper() in _MUTATING_METHODS else _READ
    return AREA_PERMISSIONS.get((area, mode))


def install_authorization(app: Flask) -> None:
    """Enforce the route→permission table for session callers."""

    @app.before_request
    def _enforce_route_permission():  # noqa: ANN202
        if request.method == "OPTIONS":
            return None
        path = request.path or "/"
        policy = current_app.config.get("SECURITY_POLICY")
        if policy is not None and policy.is_public_path(path):
            return None
        ctx = current_auth()
        # Anonymous callers are the bearer gate's job; a legacy-token operator
        # is fully trusted (has_permission short-circuits for it).
        if not ctx.authenticated:
            return None
        required = required_permission(request.method, path)
        if required is None:
            return None
        if not ctx.has_permission(required):
            return jsonify({
                "code": 403,
                "msg": f"missing permission: {required}",
                "data": None,
            }), 403
        return None
