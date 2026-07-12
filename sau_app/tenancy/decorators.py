"""Authorization decorators (Phase 4).

Used by new/migrated routes to enforce the 401/403 discipline:
- **401** when the caller is not authenticated at all,
- **403** when authenticated but lacking the required permission.

They read the request's :class:`AuthContext` (resolved by the middleware); they
do not clear the session on a 403 (that is a normal authorization failure).
"""

from __future__ import annotations

from functools import wraps

from flask import jsonify

from .context import current_auth


def _unauthorized():
    return jsonify({"code": 401, "msg": "authentication required", "data": None}), 401


def _forbidden(permission: str):
    return jsonify({"code": 403, "msg": f"missing permission: {permission}", "data": None}), 403


def require_auth(view):
    """Require any authenticated caller (session or legacy token)."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_auth().authenticated:
            return _unauthorized()
        return view(*args, **kwargs)

    return wrapper


def require_permission(permission: str):
    """Require an authenticated caller holding ``permission``."""

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            ctx = current_auth()
            if not ctx.authenticated:
                return _unauthorized()
            if not ctx.has_permission(permission):
                return _forbidden(permission)
            return view(*args, **kwargs)

        return wrapper

    return decorator
