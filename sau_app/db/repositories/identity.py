"""Identity + workspace provisioning (Phase 3 foundation).

Implements the durable half of Google login — the part that needs no Google
credentials and is fully testable: given a verified Google identity (the ``sub``
claim and profile fields), upsert the application ``User`` and, on first login,
transactionally create their personal ``Workspace`` with an owner membership.

Identity is keyed by ``(provider, provider_subject)``; email is stored but never
used as the key, so a user who changes their Google email keeps the same account
and workspace.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..identity_models import (
    ROLE_OWNER,
    AuthIdentity,
    User,
    Workspace,
    WorkspaceMember,
)

PROVIDER_GOOGLE = "google"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-")
    return slug or "workspace"


@dataclass(frozen=True)
class LoginResult:
    user: User
    workspace: Workspace
    created: bool  # True when this login provisioned a new user + workspace


class IdentityRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_identity(self, provider: str, subject: str) -> AuthIdentity | None:
        stmt = select(AuthIdentity).where(
            AuthIdentity.provider == provider,
            AuthIdentity.provider_subject == subject,
        )
        return self.session.scalars(stmt).one_or_none()

    def _unique_workspace_slug(self, base: str) -> str:
        base = _slugify(base)
        candidate = base
        while self.session.scalar(select(func.count()).select_from(Workspace).where(Workspace.slug == candidate)):
            candidate = f"{base}-{uuid.uuid4().hex[:6]}"
        return candidate

    def upsert_google_login(
        self,
        *,
        subject: str,
        email: str | None,
        email_verified: bool,
        display_name: str | None = None,
        avatar_url: str | None = None,
        claims: dict | None = None,
    ) -> LoginResult:
        """Provision-or-return the user for a verified Google identity.

        First login: create user + identity + personal workspace + owner
        membership in one transaction (the caller's session; commit is the
        caller's responsibility). Subsequent logins: refresh the stored email,
        claims, profile fields, and ``last_login_at``, and return the existing
        user with their owned workspace.
        """

        if not subject:
            raise ValueError("Google identity is missing the required 'sub' claim")

        identity = self.get_identity(PROVIDER_GOOGLE, subject)
        if identity is not None:
            user = identity.user
            identity.email_at_provider = email
            identity.email_verified = bool(email_verified)
            identity.claims_json = json.dumps(claims or {})
            if display_name:
                user.display_name = display_name
            if avatar_url:
                user.avatar_url = avatar_url
            if email:
                user.primary_email = email
            user.last_login_at = func.current_timestamp()
            self.session.flush()
            workspace = self._owned_workspace(user)
            return LoginResult(user=user, workspace=workspace, created=False)

        # First login — provision everything atomically.
        user = User(
            primary_email=email,
            display_name=display_name,
            avatar_url=avatar_url,
            status="active",
            last_login_at=func.current_timestamp(),
        )
        self.session.add(user)
        self.session.flush()  # assign user.id

        self.session.add(
            AuthIdentity(
                user_id=user.id,
                provider=PROVIDER_GOOGLE,
                provider_subject=subject,
                email_at_provider=email,
                email_verified=bool(email_verified),
                claims_json=json.dumps(claims or {}),
            )
        )

        ws_label = display_name or (email.split("@")[0] if email else "My")
        workspace = Workspace(
            name=f"{ws_label}'s Workspace",
            slug=self._unique_workspace_slug(ws_label),
            status="active",
            created_by_user_id=user.id,
        )
        self.session.add(workspace)
        self.session.flush()  # assign workspace.id

        self.session.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=ROLE_OWNER)
        )
        self.session.flush()
        return LoginResult(user=user, workspace=workspace, created=True)

    def _owned_workspace(self, user: User) -> Workspace:
        """The workspace this user owns (their personal workspace)."""

        stmt = (
            select(Workspace)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .where(
                WorkspaceMember.user_id == user.id,
                WorkspaceMember.role == ROLE_OWNER,
            )
            .order_by(Workspace.created_at, Workspace.id)
        )
        workspace = self.session.scalars(stmt).first()
        if workspace is None:  # defensive: an owner membership should always exist
            raise RuntimeError(f"user {user.id} has no owned workspace")
        return workspace
