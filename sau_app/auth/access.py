"""Shared Google authorization for callbacks and already-issued sessions.

Single/shadow deployments expose legacy, unscoped data, so only explicitly
approved operators may use Google login. Enforced tenancy keeps its scoped
membership/role policy; an operator allowlist never grants tenant privileges.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..config import AppConfig
from ..db.identity_models import (
    ROLE_OWNER,
    ROLES,
    AuthIdentity,
    Session as SessionRow,
    Workspace,
    WorkspaceMember,
)
from .oidc import GoogleClaims


def google_access_allowed(
    config: AppConfig,
    db: DbSession,
    *,
    claims: GoogleClaims | None = None,
    session: SessionRow | None = None,
) -> bool:
    """Authorize exactly one verified callback identity or persisted session.

    Call *before* provisioning with ``claims``. For a session, expiry/revocation
    must also be checked (use ``SessionStore.get_authorized``). Provider email,
    not the user's editable profile email, is the authorization source. A new
    identity may be provisioned only after this check; existing users must have
    an active workspace and a real membership, never an implicit viewer role.
    """
    if not config.google_login_enabled or config.tenancy_mode not in {"single", "shadow", "enforced"}:
        return False
    if (claims is None) == (session is None):
        return False

    if claims is not None:
        identities = [(claims.subject, claims.email, claims.email_verified)]
    else:
        identities = [
            (identity.provider_subject, identity.email_at_provider, identity.email_verified)
            for identity in db.scalars(
                select(AuthIdentity).where(
                    AuthIdentity.user_id == session.user_id,
                    AuthIdentity.provider == "google",
                )
            )
        ]

    if not any(
        isinstance(subject, str) and bool(subject.strip())
        and isinstance(email, str) and bool(email.strip())
        and verified is True
        and (
            config.tenancy_mode == "enforced"
            or email.strip().casefold() in config.google_allowed_emails
            or subject in config.google_allowed_subjects
        )
        for subject, email, verified in identities
    ):
        return False

    if claims is not None:
        identity = db.scalars(
            select(AuthIdentity).where(
                AuthIdentity.provider == "google",
                AuthIdentity.provider_subject == claims.subject,
            )
        ).one_or_none()
        if identity is None:
            return True  # Explicitly authorized first login; no user exists yet.
        user = identity.user
        # Match IdentityRepository's existing owned-workspace selection. Do not
        # silently switch away from a disabled workspace or invent membership.
        member = db.scalars(
            select(WorkspaceMember)
            .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
            .where(WorkspaceMember.user_id == identity.user_id, WorkspaceMember.role == ROLE_OWNER)
            .order_by(Workspace.created_at, Workspace.id)
        ).first()
    else:
        user = session.user
        member = (
            db.get(WorkspaceMember, (session.active_workspace_id, session.user_id))
            if session.active_workspace_id else None
        )

    return (
        user is not None and user.status == "active"
        and member is not None and member.role in ROLES
        and member.workspace is not None and member.workspace.status == "active"
    )
