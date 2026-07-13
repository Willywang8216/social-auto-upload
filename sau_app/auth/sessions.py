"""Server-side session + CSRF management (Phase 3).

Sessions live in the ``sessions`` table (migration 0015); the browser only holds
an opaque, high-entropy cookie value (the session id). CSRF uses a per-session
secret returned to the SPA and required back in the ``X-CSRF-Token`` header on
unsafe requests. Nothing here is stored in JavaScript-readable storage or a URL.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from flask import Response
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..config import AppConfig
from ..db.identity_models import Session as SessionRow

CSRF_HEADER = "X-CSRF-Token"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SessionStore:
    """CRUD for server-side sessions bound to an open database session."""

    def __init__(self, db: DbSession) -> None:
        self.db = db

    def create(
        self,
        *,
        user_id: str,
        workspace_id: str | None,
        idle_seconds: int,
        absolute_seconds: int,
        user_agent_hash: str | None = None,
        ip_prefix: str | None = None,
    ) -> SessionRow:
        now = _utcnow()
        row = SessionRow(
            id=uuid.uuid4().hex,  # opaque, unguessable cookie value
            user_id=user_id,
            active_workspace_id=workspace_id,
            created_at=now,
            last_seen_at=now,
            # Effective expiry is the sooner of idle and absolute; store the
            # absolute cap in expires_at and enforce idle on read.
            expires_at=now + timedelta(seconds=absolute_seconds),
            csrf_secret=secrets.token_urlsafe(32),
            user_agent_hash=user_agent_hash,
            ip_prefix=ip_prefix,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def get_active(self, session_id: str | None, *, idle_seconds: int) -> SessionRow | None:
        if not session_id:
            return None
        row = self.db.scalars(
            select(SessionRow).where(SessionRow.id == session_id)
        ).one_or_none()
        if row is None or row.revoked_at is not None:
            return None
        now = _utcnow()
        if row.expires_at is not None and row.expires_at <= now:
            return None
        if row.last_seen_at is not None and (now - row.last_seen_at) > timedelta(seconds=idle_seconds):
            return None
        row.last_seen_at = now
        self.db.flush()
        return row

    def revoke(self, session_id: str) -> None:
        row = self.db.scalars(select(SessionRow).where(SessionRow.id == session_id)).one_or_none()
        if row is not None and row.revoked_at is None:
            row.revoked_at = _utcnow()
            self.db.flush()


def set_session_cookie(response: Response, session_id: str, config: AppConfig) -> None:
    response.set_cookie(
        config.session_cookie_name,
        session_id,
        max_age=config.session_absolute_seconds,
        secure=True,
        httponly=True,
        samesite="Lax",
        path="/",
    )


def clear_session_cookie(response: Response, config: AppConfig) -> None:
    response.delete_cookie(config.session_cookie_name, path="/")


def csrf_ok(row: SessionRow, presented: str | None) -> bool:
    if not presented or not row.csrf_secret:
        return False
    return secrets.compare_digest(presented, row.csrf_secret)
