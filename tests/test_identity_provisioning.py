"""Tests for first-login user + workspace provisioning (Phase 3 foundation).

Verifies the core multi-tenant promise — each Google user gets exactly one
personal workspace — and that identity is keyed by the Google ``sub``, not
email. Runs on SQLite by default and, when ``TEST_DATABASE_URL`` is set (CI),
also on PostgreSQL.
"""

from __future__ import annotations

import importlib.util
import json
import os
import unittest

sqlalchemy_available = importlib.util.find_spec("sqlalchemy") is not None

if sqlalchemy_available:
    from sqlalchemy.orm import Session

    from sau_app.db import make_engine
    from sau_app.db.base import Base
    from sau_app.db.identity_models import (
        ROLE_OWNER,
        AuthIdentity,
        User,
        Workspace,
        WorkspaceMember,
    )
    from sau_app.db.repositories import IdentityRepository


@unittest.skipUnless(sqlalchemy_available, "SQLAlchemy not installed")
class IdentityProvisioningTests(unittest.TestCase):
    def _engine(self):
        # Prefer PostgreSQL when configured (CI); otherwise in-memory SQLite.
        url = os.environ.get("TEST_DATABASE_URL") or "sqlite:///:memory:"
        engine = make_engine(url)
        Base.metadata.create_all(engine)
        return engine, url

    def setUp(self) -> None:
        self.engine, self.url = self._engine()
        self.session = Session(self.engine, future=True, expire_on_commit=False)

    def tearDown(self) -> None:
        self.session.close()
        if not self.url.startswith("sqlite"):
            Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_first_login_creates_user_and_personal_workspace(self) -> None:
        repo = IdentityRepository(self.session)
        result = repo.upsert_google_login(
            subject="google-sub-1",
            email="alice@example.com",
            email_verified=True,
            display_name="Alice",
            claims={"hd": "example.com"},
        )
        self.session.commit()

        self.assertTrue(result.created)
        self.assertEqual(result.user.primary_email, "alice@example.com")
        self.assertEqual(result.workspace.created_by_user_id, result.user.id)

        # Exactly one user, one workspace, one owner membership.
        self.assertEqual(self.session.query(User).count(), 1)
        self.assertEqual(self.session.query(Workspace).count(), 1)
        member = self.session.query(WorkspaceMember).one()
        self.assertEqual(member.role, ROLE_OWNER)
        self.assertEqual(member.user_id, result.user.id)
        self.assertEqual(member.workspace_id, result.workspace.id)

        identity = self.session.query(AuthIdentity).one()
        self.assertEqual(identity.provider, "google")
        self.assertEqual(identity.provider_subject, "google-sub-1")
        self.assertEqual(json.loads(identity.claims_json), {"hd": "example.com"})

    def test_repeat_login_reuses_the_same_user_and_workspace(self) -> None:
        repo = IdentityRepository(self.session)
        first = repo.upsert_google_login(
            subject="google-sub-2", email="bob@example.com", email_verified=True, display_name="Bob"
        )
        self.session.commit()
        second = repo.upsert_google_login(
            subject="google-sub-2", email="bob@example.com", email_verified=True, display_name="Bob"
        )
        self.session.commit()

        self.assertFalse(second.created)
        self.assertEqual(first.user.id, second.user.id)
        self.assertEqual(first.workspace.id, second.workspace.id)
        self.assertEqual(self.session.query(User).count(), 1)
        self.assertEqual(self.session.query(Workspace).count(), 1)

    def test_identity_is_keyed_by_sub_not_email(self) -> None:
        repo = IdentityRepository(self.session)
        first = repo.upsert_google_login(
            subject="stable-sub", email="old@example.com", email_verified=True
        )
        self.session.commit()
        # Same person, new email at Google — same sub must map to the same user.
        again = repo.upsert_google_login(
            subject="stable-sub", email="new@example.com", email_verified=True
        )
        self.session.commit()

        self.assertEqual(first.user.id, again.user.id)
        self.assertFalse(again.created)
        self.assertEqual(again.user.primary_email, "new@example.com")
        self.assertEqual(self.session.query(User).count(), 1)

    def test_distinct_users_get_isolated_workspaces(self) -> None:
        repo = IdentityRepository(self.session)
        a = repo.upsert_google_login(subject="sub-a", email="a@x.com", email_verified=True, display_name="Sam")
        b = repo.upsert_google_login(subject="sub-b", email="b@x.com", email_verified=True, display_name="Sam")
        self.session.commit()

        self.assertNotEqual(a.user.id, b.user.id)
        self.assertNotEqual(a.workspace.id, b.workspace.id)
        # Same display name -> distinct, unique slugs.
        self.assertNotEqual(a.workspace.slug, b.workspace.slug)
        self.assertEqual(self.session.query(Workspace).count(), 2)

    def test_missing_sub_is_rejected(self) -> None:
        repo = IdentityRepository(self.session)
        with self.assertRaises(ValueError):
            repo.upsert_google_login(subject="", email="x@x.com", email_verified=True)


if __name__ == "__main__":
    unittest.main()
