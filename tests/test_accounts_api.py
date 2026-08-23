"""HTTP tests for /api/accounts and the PATCH /accounts/<id> endpoint.

Covers the refinements shipped alongside the per-account nickname/group
feature:
  * ``GET /api/accounts?q=...`` substring filter against ``nickname`` and
    ``account_name``, plus combination with ``platform=`` and ``group=``.
  * ``GET /api/accounts?group=`` returns the ungrouped bucket when the empty
    string is passed (mirrors the frontend's ``__none__`` sentinel).
  * ``PATCH /accounts/<id>`` with ``accountGroup=""`` clears the group.
  * ``PATCH /accounts/<id>`` with a brand-new ``accountGroup`` round-trips
    and shows up in ``GET /api/accounts/groups``.

Drives the real ``sau_backend.app`` via ``test_client`` in open mode (no
SAU_API_TOKENS), matching the pattern in ``test_security_http.py``.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

flask_available = importlib.util.find_spec("flask") is not None


def _ensure_conf_module() -> None:
    if "conf" in sys.modules:
        return
    conf_module = types.ModuleType("conf")
    conf_module.BASE_DIR = str(Path(__file__).resolve().parent.parent)
    conf_module.DEBUG_MODE = True
    conf_module.LOCAL_CHROME_HEADLESS = True
    conf_module.LOCAL_CHROME_PATH = ""
    sys.modules["conf"] = conf_module


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class AccountsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        _ensure_conf_module()

        import db.createTable as create_table
        import sau_backend
        from myUtils import jobs as job_runtime
        from myUtils import profiles as prof
        from myUtils.security import SecurityPolicy

        self.prof = prof
        self.sau_backend = sau_backend

        # Isolated DB so each test starts from a clean slate. We place the file
        # at the *legacy* path (<tmp>/db/database.db) so that the Flask request
        # handlers — which resolve via _current_db_path() → _get_legacy_db_path()
        # → <BASE_DIR>/db/database.db — read the same file the seeding code
        # writes to. We then point BASE_DIR at <tmp> so the legacy resolver and
        # the seed code agree on the same on-disk file.
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)
        self.db_path = self._tmp_path / "db" / "database.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        create_table.bootstrap(self.db_path)

        # The jobs module owns its own DB_PATH; patch it too.
        self._job_patch = patch.object(job_runtime, "DB_PATH", self.db_path)
        self._job_patch.start()
        # Repoint BASE_DIR at <tmp>; the legacy resolver then returns
        # <tmp>/db/database.db, which is exactly self.db_path.
        self._base_dir_patch = patch.object(sau_backend, "BASE_DIR", str(self._tmp_path))
        self._base_dir_patch.start()

        # Open mode = no SAU_API_TOKENS required for /api/* calls.
        self._previous_policy = sau_backend.app.config["SECURITY_POLICY"]
        sau_backend.app.config["SECURITY_POLICY"] = SecurityPolicy(
            tokens=frozenset(), cors_origins=("http://localhost:5173",)
        )
        # Don't lock /api/* behind the Google OIDC gate in tests — single
        # tenancy is the default.
        self._previous_cfg = sau_backend.app.config.get("SAU_APP_CONFIG")
        sau_backend.app.config["SAU_APP_CONFIG"] = None
        sau_backend.app.config["TESTING"] = True
        self.client = sau_backend.app.test_client()

        # Seed: a single profile with three accounts across platforms + groups.
        # OAuth-style auth_type="oauth" so the registry doesn't try to create
        # cookie directories under BASE_DIR/cookies/<platform>/<profile>/.
        self.profile = prof.create_profile(
            "Acme Media", settings={}, db_path=self.db_path
        )
        self.acct_official = prof.add_account(
            self.profile.id,
            prof.PLATFORM_DOUYIN,
            "official_handle",
            auth_type="oauth",
            nickname="Acme Main",
            account_group="Daily drivers",
            db_path=self.db_path,
        )
        self.acct_backup = prof.add_account(
            self.profile.id,
            prof.PLATFORM_DOUYIN,
            "backup_brand",
            auth_type="oauth",
            nickname="Backup Brand",
            account_group="Daily drivers",
            db_path=self.db_path,
        )
        self.acct_blog = prof.add_account(
            self.profile.id,
            prof.PLATFORM_MEDIUM,
            "blog_writer",
            auth_type="oauth",
            nickname="Acme Blog",
            db_path=self.db_path,
        )
        self.acct_demo = prof.add_account(
            self.profile.id,
            prof.PLATFORM_TIKTOK,
            "demo_only",
            auth_type="oauth",
            nickname="Demo Day",
            account_group="Demos",
            db_path=self.db_path,
        )

    def tearDown(self) -> None:
        self.sau_backend.app.config["SECURITY_POLICY"] = self._previous_policy
        if self._previous_cfg is not None:
            self.sau_backend.app.config["SAU_APP_CONFIG"] = self._previous_cfg
        else:
            self.sau_backend.app.config.pop("SAU_APP_CONFIG", None)
        self._job_patch.stop()
        self._base_dir_patch.stop()
        self._tmp.cleanup()

    # ----- helpers -----------------------------------------------------------

    def _list(self, **params: str):
        qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        path = "/api/accounts" + (f"?{qs}" if qs else "")
        return self.client.get(path)

    def _patch_account(self, account_id: int, **body):
        return self.client.patch(f"/accounts/{account_id}", json=body)

    def _account_ids(self, response) -> set[int]:
        payload = response.get_json()
        self.assertEqual(payload.get("code"), 200, payload)
        rows = payload["data"] or []
        return {row["id"] for row in rows}

    # ----- q= substring filter ---------------------------------------------

    def test_q_matches_nickname_case_insensitive(self) -> None:
        response = self._list(q="acme")
        ids = self._account_ids(response)
        self.assertEqual(
            ids, {self.acct_official.id, self.acct_blog.id},
        )

    def test_q_matches_account_name_substring(self) -> None:
        response = self._list(q="backup")
        ids = self._account_ids(response)
        self.assertEqual(ids, {self.acct_backup.id})

    def test_q_combined_with_platform_filter(self) -> None:
        response = self._list(q="acme", platform=self.prof.PLATFORM_DOUYIN)
        ids = self._account_ids(response)
        # "Acme Blog" is on medium; "Acme Main" is on douyin — only the douyin
        # one survives the platform= AND.
        self.assertEqual(ids, {self.acct_official.id})

    def test_q_combined_with_group_filter(self) -> None:
        response = self._list(q="acme", group="Daily drivers")
        ids = self._account_ids(response)
        self.assertEqual(ids, {self.acct_official.id})

    def test_blank_q_returns_all(self) -> None:
        response = self._list(q="   ")
        self.assertEqual(
            self._account_ids(response),
            {
                self.acct_official.id,
                self.acct_backup.id,
                self.acct_blog.id,
                self.acct_demo.id,
            },
        )

    def test_q_no_match_returns_empty(self) -> None:
        response = self._list(q="ghost")
        self.assertEqual(self._account_ids(response), set())

    # ----- group filter sentinel -------------------------------------------

    def test_group_empty_string_returns_ungrouped_bucket(self) -> None:
        response = self._list(group="")
        ids = self._account_ids(response)
        # acct_blog is the only account without a group.
        self.assertEqual(ids, {self.acct_blog.id})

    # ----- PATCH round-trip -------------------------------------------------

    def test_patch_clears_group_with_empty_string(self) -> None:
        response = self._patch_account(self.acct_demo.id, accountGroup="")
        self.assertEqual(response.status_code, 200, response.get_json())
        # /api/accounts/groups no longer surfaces "Demos"
        groups = self.client.get("/api/accounts/groups").get_json()["data"]
        self.assertNotIn("Demos", groups)
        # Row is gone from the Demos bucket …
        still_in_demos = self._list(group="Demos").get_json()["data"]
        self.assertEqual([r["id"] for r in still_in_demos], [])
        # … and now lives in the ungrouped bucket alongside the previously
        # ungrouped acct_blog (which never had a group to begin with).
        ungrouped = self._list(group="").get_json()["data"]
        self.assertEqual(
            {r["id"] for r in ungrouped},
            {self.acct_blog.id, self.acct_demo.id},
        )

    def test_patch_with_new_group_name_persists_and_appears_in_dropdown(self) -> None:
        response = self._patch_account(self.acct_blog.id, accountGroup="Q4 Launch")
        self.assertEqual(response.status_code, 200, response.get_json())
        groups = self.client.get("/api/accounts/groups").get_json()["data"]
        self.assertIn("Q4 Launch", groups)
        # Row now appears under the new group bucket.
        new_bucket = self._list(group="Q4 Launch").get_json()["data"]
        self.assertEqual({r["id"] for r in new_bucket}, {self.acct_blog.id})

    def test_patch_nickname_round_trips_via_payload(self) -> None:
        response = self._patch_account(
            self.acct_official.id, nickname="Acme HQ (Renamed)"
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(
            response.get_json()["data"]["nickname"],
            "Acme HQ (Renamed)",
        )

    def test_patch_unknown_account_returns_404(self) -> None:
        response = self._patch_account(99999, nickname="x")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
