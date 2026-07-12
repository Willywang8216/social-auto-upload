"""Phase 6: two-user tenant isolation for the profile routes (enforced mode).

Drives the real ``sau_backend`` app with two Google sessions in
``SAU_TENANCY_MODE=enforced`` and asserts the classic matrix: each user manages
their own profiles but cannot list, read, modify, or delete another workspace's
profile even with a known ID (which resolves as 404, not 403 — existence is not
confirmed). Legacy behavior (single mode / legacy token) stays unscoped.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

flask_available = importlib.util.find_spec("flask") is not None
sqlalchemy_available = importlib.util.find_spec("sqlalchemy") is not None

if flask_available and sqlalchemy_available:
    import db.createTable as create_table
    from myUtils import profiles as prof
    from myUtils.security import SecurityPolicy
    from sau_app import db as sau_db
    from sau_app.config import load_config
    from sau_app.db.identity_models import (
        ROLE_OWNER,
        Session as SessionRow,
        User,
        Workspace,
        WorkspaceMember,
    )


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@unittest.skipUnless(flask_available and sqlalchemy_available, "flask/sqlalchemy not installed")
class ProfileTenantIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        import sau_backend

        self.sau_backend = sau_backend
        self.app = sau_backend.app

        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "iso.db"
        create_table.bootstrap(self.db_path)

        self._prev_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        sau_db.reset_engine_cache()

        # Point the legacy DB path (profiles) at the same temp DB as the sessions.
        self._patch_db = patch.object(sau_backend, "_get_legacy_db_path", return_value=self.db_path)
        self._patch_db.start()

        # Two workspaces, each with an owner session.
        self.ws_a, self.csrf_a, self.sid_a = self._make_workspace_session("a@x.com")
        self.ws_b, self.csrf_b, self.sid_b = self._make_workspace_session("b@x.com")

        self._prev_policy = self.app.config["SECURITY_POLICY"]
        self._prev_cfg = self.app.config["SAU_APP_CONFIG"]
        self.app.config["SECURITY_POLICY"] = SecurityPolicy(
            tokens=frozenset({"legacy-token"}), cors_origins=("*",)
        )
        self.app.config["SAU_APP_CONFIG"] = load_config({
            "APP_ENV": "testing", "SAU_AUTH_MODE": "oidc", "SAU_TENANCY_MODE": "enforced",
            "SAU_GOOGLE_LOGIN_ENABLED": "true", "GOOGLE_LOGIN_CLIENT_ID": "c",
            "GOOGLE_LOGIN_CLIENT_SECRET": "s", "SAU_PUBLIC_BASE_URL": "https://up.example.com",
            "SECRET_KEY": "x" * 40,
        })
        self.cookie_name = self.app.config["SAU_APP_CONFIG"].session_cookie_name

    def tearDown(self) -> None:
        self.app.config["SECURITY_POLICY"] = self._prev_policy
        self.app.config["SAU_APP_CONFIG"] = self._prev_cfg
        self._patch_db.stop()
        sau_db.reset_engine_cache()
        if self._prev_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._prev_url
        self._tmp.cleanup()

    def _make_workspace_session(self, email: str):
        from sau_app.db import get_sessionmaker

        Session = get_sessionmaker()
        with Session() as db:
            user = User(id=uuid.uuid4().hex, primary_email=email, display_name=email, status="active")
            ws = Workspace(id=uuid.uuid4().hex, name=email, slug=uuid.uuid4().hex, status="active",
                           created_by_user_id=user.id)
            db.add_all([user, ws])
            db.flush()
            db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=ROLE_OWNER))
            sid = uuid.uuid4().hex
            csrf = uuid.uuid4().hex
            db.add(SessionRow(
                id=sid, user_id=user.id, active_workspace_id=ws.id,
                created_at=_utcnow(), last_seen_at=_utcnow(),
                expires_at=_utcnow() + timedelta(hours=1), csrf_secret=csrf,
            ))
            db.commit()
            return ws.id, csrf, sid

    def _client(self, sid: str | None):
        c = self.app.test_client()
        if sid:
            c.set_cookie(self.cookie_name, sid, domain="localhost")
        return c

    def _get(self, c, path):
        return c.get(path, base_url="https://localhost")

    def _post(self, c, path, csrf, **json_body):
        return c.post(path, base_url="https://localhost", headers={"X-CSRF-Token": csrf}, json=json_body)

    def _patch(self, c, path, csrf, **json_body):
        return c.patch(path, base_url="https://localhost", headers={"X-CSRF-Token": csrf}, json=json_body)

    def _delete(self, c, path, csrf):
        return c.delete(path, base_url="https://localhost", headers={"X-CSRF-Token": csrf})

    def _seed_profile(self, workspace_id: str, name: str) -> int:
        return prof.create_profile(name=name, workspace_id=workspace_id, db_path=self.db_path).id

    # --- the matrix ------------------------------------------------------------

    def test_anonymous_is_rejected(self) -> None:
        self.assertEqual(self._get(self._client(None), "/profiles").status_code, 401)

    def test_user_lists_only_their_own_profiles(self) -> None:
        self._seed_profile(self.ws_a, "A-brand")
        self._seed_profile(self.ws_b, "B-brand")
        a = self._get(self._client(self.sid_a), "/profiles").get_json()["data"]
        self.assertEqual([p["name"] for p in a], ["A-brand"])
        b = self._get(self._client(self.sid_b), "/profiles").get_json()["data"]
        self.assertEqual([p["name"] for p in b], ["B-brand"])

    def test_cannot_read_other_workspace_profile_by_id(self) -> None:
        b_id = self._seed_profile(self.ws_b, "B-brand")
        self.assertEqual(self._get(self._client(self.sid_a), f"/profiles/{b_id}").status_code, 404)
        # Owner can read it.
        self.assertEqual(self._get(self._client(self.sid_b), f"/profiles/{b_id}").status_code, 200)

    def test_cannot_modify_or_delete_other_workspace_profile(self) -> None:
        b_id = self._seed_profile(self.ws_b, "B-brand")
        self.assertEqual(self._patch(self._client(self.sid_a), f"/profiles/{b_id}", self.csrf_a, name="hacked").status_code, 404)
        self.assertEqual(self._delete(self._client(self.sid_a), f"/profiles/{b_id}", self.csrf_a).status_code, 404)
        # B's profile is intact and unchanged.
        got = self._get(self._client(self.sid_b), f"/profiles/{b_id}").get_json()["data"]
        self.assertEqual(got["name"], "B-brand")

    def test_cannot_create_account_under_foreign_profile(self) -> None:
        b_id = self._seed_profile(self.ws_b, "B-brand")
        resp = self._post(self._client(self.sid_a), f"/profiles/{b_id}/accounts", self.csrf_a,
                          accountName="x", platform="douyin")
        self.assertEqual(resp.status_code, 404)

    def test_user_can_manage_their_own_profile(self) -> None:
        created = self._post(self._client(self.sid_a), "/profiles", self.csrf_a, name="Mine")
        self.assertEqual(created.status_code, 200)
        pid = created.get_json()["data"]["id"]
        self.assertEqual(self._get(self._client(self.sid_a), f"/profiles/{pid}").status_code, 200)
        self.assertEqual(self._patch(self._client(self.sid_a), f"/profiles/{pid}", self.csrf_a, name="Renamed").status_code, 200)
        self.assertEqual(self._delete(self._client(self.sid_a), f"/profiles/{pid}", self.csrf_a).status_code, 200)

    def test_created_profile_is_assigned_to_callers_workspace(self) -> None:
        self._post(self._client(self.sid_a), "/profiles", self.csrf_a, name="OwnedByA")
        # Visible to A, not to B.
        a = [p["name"] for p in self._get(self._client(self.sid_a), "/profiles").get_json()["data"]]
        b = [p["name"] for p in self._get(self._client(self.sid_b), "/profiles").get_json()["data"]]
        self.assertIn("OwnedByA", a)
        self.assertNotIn("OwnedByA", b)

    def test_legacy_token_is_unscoped_admin_path(self) -> None:
        # A legacy bearer token (no session) is not workspace-scoped: it sees all
        # profiles. This is the documented single-tenant/admin compatibility path.
        self._seed_profile(self.ws_a, "A-brand")
        self._seed_profile(self.ws_b, "B-brand")
        c = self.app.test_client()
        resp = c.get("/profiles", headers={"Authorization": "Bearer legacy-token"})
        names = {p["name"] for p in resp.get_json()["data"]}
        self.assertEqual(names, {"A-brand", "B-brand"})


if __name__ == "__main__":
    unittest.main()
