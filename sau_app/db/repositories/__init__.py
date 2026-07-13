"""SQLAlchemy repositories (Phase 2+)."""

from __future__ import annotations

from .base import Repository, WorkspaceScopedRepository
from .identity import IdentityRepository, LoginResult, PROVIDER_GOOGLE
from .profiles import ProfileRepository

__all__ = [
    "IdentityRepository",
    "LoginResult",
    "PROVIDER_GOOGLE",
    "ProfileRepository",
    "Repository",
    "WorkspaceScopedRepository",
]
