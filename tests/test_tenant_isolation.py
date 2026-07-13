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

        # Point every runtime DB path at the same temp DB as the sessions.
        # profiles/accounts/media-groups routes go through _current_db_path();
        # the jobs and media_asset modules resolve their own module DB_PATH
        # (a monkeypatch hook), so patch those too.
        from myUtils import jobs as _jobs
        from myUtils import media_asset_service as _mas

        self._patches = [
            patch.object(sau_backend, "_get_legacy_db_path", return_value=self.db_path),
            patch.object(_jobs, "DB_PATH", self.db_path),
            patch.object(_mas, "DB_PATH", self.db_path),
        ]
        for p in self._patches:
            p.start()

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
        for p in self._patches:
            p.stop()
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

    # --- accounts domain ---------------------------------------------------

    def _seed_account(self, workspace_id: str, profile_name: str) -> int:
        """Create a profile+account in a workspace; returns the account id."""
        p = prof.create_profile(name=profile_name, workspace_id=workspace_id, db_path=self.db_path)
        acct = prof.add_account(p.id, "douyin", "acct1", db_path=self.db_path)
        return acct.id

    def test_account_inherits_parent_profile_workspace(self) -> None:
        acct_id = self._seed_account(self.ws_a, "A-brand")
        got = prof.get_account(acct_id, workspace_id=self.ws_a, db_path=self.db_path)
        self.assertEqual(got.id, acct_id)
        with self.assertRaises(LookupError):
            prof.get_account(acct_id, workspace_id=self.ws_b, db_path=self.db_path)

    def test_cannot_patch_foreign_account(self) -> None:
        b_acct = self._seed_account(self.ws_b, "B-brand")
        resp = self._patch(self._client(self.sid_a), f"/accounts/{b_acct}", self.csrf_a,
                           accountName="hacked")
        self.assertEqual(resp.status_code, 404)
        # B's account is unchanged.
        got = prof.get_account(b_acct, workspace_id=self.ws_b, db_path=self.db_path)
        self.assertEqual(got.account_name, "acct1")

    def test_cannot_export_foreign_account_cookies(self) -> None:
        b_acct = self._seed_account(self.ws_b, "B-brand")
        resp = self._get(self._client(self.sid_a), f"/api/auth/cookies/{b_acct}/export")
        self.assertEqual(resp.status_code, 404)

    def test_cannot_import_cookies_into_foreign_account(self) -> None:
        b_acct = self._seed_account(self.ws_b, "B-brand")
        resp = self._post(self._client(self.sid_a), f"/accounts/{b_acct}/import-cookies",
                          self.csrf_a, cookies="name\tvalue")
        self.assertEqual(resp.status_code, 404)

    def test_cannot_check_or_refresh_foreign_account(self) -> None:
        b_acct = self._seed_account(self.ws_b, "B-brand")
        a = self._client(self.sid_a)
        self.assertEqual(
            self._post(a, f"/accounts/{b_acct}/check-connection", self.csrf_a).status_code, 404
        )
        self.assertEqual(
            self._post(a, f"/accounts/{b_acct}/refresh-token", self.csrf_a).status_code, 404
        )
        self.assertEqual(
            self._post(a, f"/api/accounts/{b_acct}/check", self.csrf_a).status_code, 404
        )

    def test_api_accounts_lists_only_own_workspace(self) -> None:
        self._seed_account(self.ws_a, "A-brand")
        self._seed_account(self.ws_b, "B-brand")
        a = self._get(self._client(self.sid_a), "/api/accounts").get_json()["data"]
        b = self._get(self._client(self.sid_b), "/api/accounts").get_json()["data"]
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)

    # --- jobs + media domains ----------------------------------------------

    def _seed_job(self, workspace_id: str, key: str) -> int:
        from myUtils import jobs
        spec = jobs.JobSpec(platform="douyin", payload={}, targets=[("account:1", "f.mp4", None)],
                            idempotency_key=key)
        return jobs.enqueue_job(spec, workspace_id=workspace_id, db_path=self.db_path).id

    def test_cannot_read_or_cancel_foreign_job(self) -> None:
        b_job = self._seed_job(self.ws_b, "kb")
        a = self._client(self.sid_a)
        self.assertEqual(self._get(a, f"/jobs/{b_job}").status_code, 404)
        self.assertEqual(self._post(a, f"/jobs/{b_job}/cancel", self.csrf_a).status_code, 404)
        # Owner can read it.
        self.assertEqual(self._get(self._client(self.sid_b), f"/jobs/{b_job}").status_code, 200)

    def test_jobs_list_scoped_to_workspace(self) -> None:
        self._seed_job(self.ws_a, "ka")
        self._seed_job(self.ws_b, "kb")
        a = self._get(self._client(self.sid_a), "/jobs").get_json()["data"]
        b = self._get(self._client(self.sid_b), "/jobs").get_json()["data"]
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)

    def _seed_media_group(self, workspace_id: str, name: str) -> int:
        from myUtils import media_groups
        return media_groups.create_media_group(name, workspace_id=workspace_id, db_path=self.db_path).id

    def test_cannot_read_foreign_media_group(self) -> None:
        b_g = self._seed_media_group(self.ws_b, "Bgroup")
        self.assertEqual(self._get(self._client(self.sid_a), f"/media-groups/{b_g}").status_code, 404)
        self.assertEqual(self._get(self._client(self.sid_b), f"/media-groups/{b_g}").status_code, 200)

    def test_media_groups_list_scoped(self) -> None:
        self._seed_media_group(self.ws_a, "Agroup")
        self._seed_media_group(self.ws_b, "Bgroup")
        a = self._get(self._client(self.sid_a), "/media-groups").get_json()["data"]
        self.assertEqual(len(a), 1)

    def _seed_media_asset(self, workspace_id: str, name: str) -> int:
        from myUtils import media_asset_service
        return media_asset_service.create_media_asset(
            original_filename=name, workspace_id=workspace_id, db_path=self.db_path
        ).id

    def test_cannot_read_or_delete_foreign_media_asset(self) -> None:
        b_a = self._seed_media_asset(self.ws_b, "b.mp4")
        a = self._client(self.sid_a)
        self.assertEqual(self._get(a, f"/api/media/assets/{b_a}").status_code, 404)
        self.assertEqual(self._delete(a, f"/api/media/assets/{b_a}", self.csrf_a).status_code, 404)
        # Owner still sees it.
        self.assertEqual(
            self._get(self._client(self.sid_b), f"/api/media/assets/{b_a}").status_code, 200
        )

    def test_media_assets_list_scoped(self) -> None:
        self._seed_media_asset(self.ws_a, "a.mp4")
        self._seed_media_asset(self.ws_b, "b.mp4")
        a = self._get(self._client(self.sid_a), "/api/media/assets").get_json()
        self.assertEqual(len(a), 1)

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
