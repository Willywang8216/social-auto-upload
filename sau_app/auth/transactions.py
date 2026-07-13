"""Server-side store for in-flight OIDC login transactions (Phase 3).

Holds the ``state``/``nonce``/PKCE ``code_verifier`` for a login between the
``/auth/google/start`` redirect and the ``/auth/google/callback`` return. Only a
hash of ``state`` is persisted; the transaction is single-use (consumed on
callback) and expires after a few minutes, defeating replay and CSRF.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..db.identity_models import OAuthLoginTransaction

STATE_TTL_SECONDS = 600  # 10 minutes


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _hash_state(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def generate_pkce() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for PKCE S256."""
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


@dataclass(frozen=True)
class StartedLogin:
    state: str
    nonce: str
    code_verifier: str
    code_challenge: str


class LoginTransactionStore:
    def __init__(self, db: DbSession) -> None:
        self.db = db

    def start(self, *, redirect_uri: str, next_url: str | None = None) -> StartedLogin:
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(24)
        verifier, challenge = generate_pkce()
        now = _utcnow()
        self.db.add(
            OAuthLoginTransaction(
                state_hash=_hash_state(state),
                nonce=nonce,
                code_verifier=verifier,
                redirect_uri=redirect_uri,
                next_url=next_url,
                created_at=now,
                expires_at=now + timedelta(seconds=STATE_TTL_SECONDS),
            )
        )
        self.db.flush()
        return StartedLogin(state=state, nonce=nonce, code_verifier=verifier, code_challenge=challenge)

    def consume(self, state: str | None) -> OAuthLoginTransaction | None:
        """Return the matching transaction and mark it consumed, or ``None``.

        Rejects unknown, already-consumed, and expired states. The consume is
        atomic within the caller's transaction (single-use).
        """
        if not state:
            return None
        row = self.db.scalars(
            select(OAuthLoginTransaction).where(
                OAuthLoginTransaction.state_hash == _hash_state(state)
            )
        ).one_or_none()
        if row is None or row.consumed_at is not None:
            return None
        if row.expires_at is not None and row.expires_at <= _utcnow():
            return None
        row.consumed_at = _utcnow()
        self.db.flush()
        return row
