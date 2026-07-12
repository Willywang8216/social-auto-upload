"""Role → permission mapping for workspace members (Phase 3/4).

Kept small and explicit. The ``/api/v1/session`` response returns the resolved
permission list so the frontend can gate UI, and route decorators (Phase 4) will
enforce the same permissions server-side.
"""

from __future__ import annotations

from ..db.identity_models import ROLE_ADMIN, ROLE_EDITOR, ROLE_OWNER, ROLE_VIEWER

# Canonical permission strings.
PROFILES_READ = "profiles:read"
PROFILES_WRITE = "profiles:write"
ACCOUNTS_READ = "accounts:read"
ACCOUNTS_CONNECT = "accounts:connect"
MEDIA_WRITE = "media:write"
PUBLISH_WRITE = "publish:write"
ANALYTICS_READ = "analytics:read"
MEMBERS_MANAGE = "members:manage"
WORKSPACE_MANAGE = "workspace:manage"
CREDENTIALS_EXPORT = "credentials:export"  # owner-only, off by default

_VIEWER = frozenset({PROFILES_READ, ACCOUNTS_READ, ANALYTICS_READ})
_EDITOR = _VIEWER | {PROFILES_WRITE, ACCOUNTS_CONNECT, MEDIA_WRITE, PUBLISH_WRITE}
_ADMIN = _EDITOR | {MEMBERS_MANAGE}
_OWNER = _ADMIN | {WORKSPACE_MANAGE}

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_OWNER: frozenset(_OWNER),
    ROLE_ADMIN: frozenset(_ADMIN),
    ROLE_EDITOR: frozenset(_EDITOR),
    ROLE_VIEWER: frozenset(_VIEWER),
}


def permissions_for(role: str) -> list[str]:
    return sorted(ROLE_PERMISSIONS.get(role, frozenset()))
