"""Profile repository (Phase 2) — SQLAlchemy, workspace-scope-ready.

A parallel, ORM-based implementation of the profile reads/writes that
``myUtils/profiles.py`` performs with raw ``sqlite3``. It is not yet wired into
the running backend; it exists so the pattern (session-bound, workspace-scoped)
is proven and tested before domains are cut over one at a time.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from ..models import Profile
from .base import WorkspaceScopedRepository


class ProfileRepository(WorkspaceScopedRepository[Profile]):
    model = Profile

    def list(self, *, workspace_id=None) -> list[Profile]:
        stmt = self._scope(select(Profile), workspace_id).order_by(Profile.id)
        return list(self.session.scalars(stmt))

    def get(self, profile_id: int, *, workspace_id=None) -> Profile | None:
        stmt = self._scope(select(Profile).where(Profile.id == profile_id), workspace_id)
        return self.session.scalars(stmt).one_or_none()

    def get_by_slug(self, slug: str, *, workspace_id=None) -> Profile | None:
        stmt = self._scope(select(Profile).where(Profile.slug == slug), workspace_id)
        return self.session.scalars(stmt).one_or_none()

    def create(
        self,
        *,
        name: str,
        slug: str,
        description: str = "",
        settings: dict | None = None,
        workspace_id=None,
    ) -> Profile:
        profile = Profile(
            name=name,
            slug=slug,
            description=description,
            settings_json=json.dumps(settings or {}),
        )
        if workspace_id is not None and hasattr(Profile, "workspace_id"):
            profile.workspace_id = workspace_id  # type: ignore[attr-defined]
        self.session.add(profile)
        self.session.flush()
        return profile

    def delete(self, profile_id: int, *, workspace_id=None) -> bool:
        profile = self.get(profile_id, workspace_id=workspace_id)
        if profile is None:
            return False
        self.session.delete(profile)
        self.session.flush()
        return True
