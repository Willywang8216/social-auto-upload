"""Repository base classes (Phase 2).

Repositories are the seam where tenant isolation will be enforced. Tenant-owned
repositories take an explicit ``workspace_id`` on every read and write; until
the ``workspace_id`` columns exist (added in the tenant-schema phase) the value
is accepted and threaded through but not yet used as a filter. Building the
signatures now means the isolation phase only has to fill in the ``WHERE`` — not
change every call site again.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from sqlalchemy.orm import Session

from ..base import Base

ModelT = TypeVar("ModelT", bound=Base)


class Repository(Generic[ModelT]):
    """A repository bound to an open SQLAlchemy :class:`Session`."""

    model: type[ModelT]

    def __init__(self, session: Session) -> None:
        self.session = session


class WorkspaceScopedRepository(Repository[ModelT]):
    """Base for tenant-owned resources.

    Every method requires a ``workspace_id``. The scoping filter is applied by
    :meth:`_scope` — a no-op today (the column does not exist yet) that becomes
    ``model.workspace_id == workspace_id`` once the tenant-schema migration
    lands, at which point isolation is enforced for every subclass at once.
    """

    def _scope(self, query, workspace_id):  # noqa: ANN001
        column = getattr(self.model, "workspace_id", None)
        if column is not None and workspace_id is not None:
            return query.where(column == workspace_id)
        return query
