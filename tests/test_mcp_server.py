"""Tests for the sau-mcp Model Context Protocol server.

Exercises every registered tool through the ``fastmcp.Client`` in-memory
transport against a freshly-bootstrapped SQLite DB. The MCP layer is
deliberately thin (wrappers around ``myUtils.*``); these tests double as
integration coverage that the schema ↔ tool wiring stays correct.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

import db.createTable as create_table

# Test environments commonly lack a populated `conf` module; install a
# minimal stub so `utils.conf_defaults` imports cleanly. Mirrors the
# pattern in tests/test_profiles.py.
if "conf" not in sys.modules:
    conf_module = types.ModuleType("conf")
    conf_module.BASE_DIR = str(Path(__file__).resolve().parent.parent)
    conf_module.DEBUG_MODE = True
    conf_module.LOCAL_CHROME_HEADLESS = True
    conf_module.LOCAL_CHROME_PATH = ""
    sys.modules["conf"] = conf_module


def _run(coro):
    """Drive a coroutine from sync unittest test methods."""
    return asyncio.run(coro)


class _McpTestCase(unittest.TestCase):
    """Base class — bootstraps a temp DB and wires up an MCP client."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "mcp.db"
        create_table.bootstrap(self.db_path)

        # Imported lazily so the conf stub above is in place first.
        from fastmcp import Client
        from mcp_server.server import build_server

        self._client_factory = Client
        self._server = build_server()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _call(self, **kwargs):
        """Call a tool, returning the structured payload synchronously."""
        async def _go():
            async with self._client_factory(self._server) as client:
                result = await client.call_tool(kwargs.pop("tool"), kwargs)
                return result.data

        return _run(_go())

    # Convenience: most tests need a profile + account; provision both.
    def _seed(self, *, name: str = "TestBrand", account_name: str = "main",
              platform: str = "douyin", account_group: str = "core"):
        profile = self._call(tool="profiles_create", name=name, db_path=str(self.db_path))
        account = self._call(
            tool="accounts_create",
            profile_id=profile["id"],
            platform=platform,
            account_name=account_name,
            account_group=account_group,
            db_path=str(self.db_path),
        )
        return profile, account


class DiscoveryToolTests(_McpTestCase):
    def test_whoami_returns_workspace_envelope(self) -> None:
        out = self._call(tool="whoami", db_path=str(self.db_path))
        self.assertEqual(out["server"], "sau-mcp")
        self.assertEqual(out["tenantMode"], "single")
        self.assertEqual(out["dbPath"], str(self.db_path))

    def test_supported_platforms_returns_all_supported(self) -> None:
        out = self._call(tool="supported_platforms")
        slugs = {row["platform"] for row in out["platforms"]}
        # A handful of canonical platforms the server must surface.
        self.assertIn("douyin", slugs)
        self.assertIn("tiktok", slugs)
        self.assertIn("youtube", slugs)
        # Cookie platforms are flagged correctly.
        for row in out["platforms"]:
            if row["platform"] == "douyin":
                self.assertTrue(row["requiresCookie"])
            if row["platform"] == "tiktok":
                self.assertTrue(row["defaultsToOauth"])


class ProfileToolTests(_McpTestCase):
    def test_profiles_create_list_get_update_delete(self) -> None:
        created = self._call(
            tool="profiles_create",
            name="AcmeCorp",
            description="initial",
            settings={"k": "v"},
            db_path=str(self.db_path),
        )
        self.assertEqual(created["slug"], "acmecorp")

        rows = self._call(tool="profiles_list", db_path=str(self.db_path))
        self.assertEqual([r["id"] for r in rows], [created["id"]])

        fetched = self._call(tool="profiles_get", profile_id=created["id"], db_path=str(self.db_path))
        self.assertEqual(fetched["name"], "AcmeCorp")

        updated = self._call(
            tool="profiles_update",
            profile_id=created["id"],
            description="updated",
            default_cta="Buy now",
            db_path=str(self.db_path),
        )
        self.assertEqual(updated["description"], "updated")
        self.assertEqual(updated["default_cta"], "Buy now")

        self._call(tool="profiles_delete", profile_id=created["id"], db_path=str(self.db_path))
        rows = self._call(tool="profiles_list", db_path=str(self.db_path))
        self.assertEqual(rows, [])

    def test_profiles_get_missing_returns_error_envelope(self) -> None:
        out = self._call(tool="profiles_get", profile_id=999, db_path=str(self.db_path))
        self.assertEqual(out["error"], "not_found")

    def test_profiles_create_slug_collision(self) -> None:
        self._call(tool="profiles_create", name="Dup", db_path=str(self.db_path))
        # Underlying call raises because the slug is UNIQUE in the DB.
        # The MCP tool returns an `internal` error envelope; assert that path.
        out = self._call(tool="profiles_create", name="Dup", db_path=str(self.db_path))
        self.assertEqual(out["error"], "internal")
        self.assertEqual(out["type"], "IntegrityError")


class AccountToolTests(_McpTestCase):
    def test_accounts_create_list_get_update_delete(self) -> None:
        profile, account = self._seed()

        listed = self._call(
            tool="accounts_list",
            profile_id=profile["id"],
            db_path=str(self.db_path),
        )
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["accountGroup"], "core")

        only_group = self._call(tool="accounts_list", group="core", db_path=str(self.db_path))
        self.assertEqual(len(only_group), 1)

        only_platform = self._call(
            tool="accounts_list", platform="douyin", db_path=str(self.db_path)
        )
        self.assertEqual(len(only_platform), 1)

        self._call(
            tool="accounts_update",
            account_id=account["id"],
            account_group="secondary",
            nickname="Renamed",
            db_path=str(self.db_path),
        )
        refetched = self._call(tool="accounts_get", account_id=account["id"], db_path=str(self.db_path))
        self.assertEqual(refetched["account_group"], "secondary")
        self.assertEqual(refetched["nickname"], "Renamed")
        self.assertEqual(refetched["accountGroup"], "secondary")

        self._call(tool="accounts_delete", account_id=account["id"], db_path=str(self.db_path))
        rows = self._call(tool="accounts_list", db_path=str(self.db_path))
        self.assertEqual(rows, [])

    def test_accounts_groups_lists_distinct_groups(self) -> None:
        _, _ = self._seed(name="BrandA", account_group="core")
        _, _ = self._seed(name="BrandB", account_group="core")
        _, _ = self._seed(name="BrandC", account_group="alt")

        groups = self._call(tool="accounts_groups", db_path=str(self.db_path))
        self.assertEqual(sorted(groups), ["alt", "core"])

    def test_accounts_health_aggregates_per_platform(self) -> None:
        _, _ = self._seed(platform="douyin", account_group="a")
        # second account under same profile/platform with a different name
        profile = self._call(tool="profiles_list", db_path=str(self.db_path))[0]
        self._call(
            tool="accounts_create",
            profile_id=profile["id"],
            platform="douyin",
            account_name="alt",
            account_group="b",
            db_path=str(self.db_path),
        )
        _, _ = self._seed(name="BrandC", platform="medium", account_group="a")

        rows = self._call(tool="accounts_health", db_path=str(self.db_path))
        agg = {row["platform"]: row for row in rows}
        self.assertEqual(agg["douyin"]["total"], 2)
        self.assertEqual(agg["douyin"]["ready"], 2)
        self.assertEqual(agg["douyin"]["pct"], 100)
        self.assertEqual(agg["medium"]["total"], 1)

    def test_accounts_create_invalid_platform_returns_invalid_input(self) -> None:
        profile, _ = self._seed()
        out = self._call(
            tool="accounts_create",
            profile_id=profile["id"],
            platform="nonsense",
            account_name="x",
            db_path=str(self.db_path),
        )
        self.assertEqual(out["error"], "invalid_input")

    def test_accounts_get_missing_returns_error_envelope(self) -> None:
        out = self._call(tool="accounts_get", account_id=999, db_path=str(self.db_path))
        self.assertEqual(out["error"], "not_found")

    def test_accounts_check_missing_returns_not_found(self) -> None:
        # accounts_check gates on get_account() first, so a missing id
        # surfaces as a `not_found` envelope without running the live probe.
        out = self._call(tool="accounts_check", account_id=999, db_path=str(self.db_path))
        self.assertEqual(out["error"], "not_found")


class TemplateToolTests(_McpTestCase):
    def test_template_crud_roundtrip(self) -> None:
        created = self._call(
            tool="publish_templates_create",
            name="Weekly Promo",
            description="default preset",
            config={"profileIds": [1]},
            included_settings=["profileIds"],
            db_path=str(self.db_path),
        )
        self.assertEqual(created["slug"], "weekly-promo")

        listed = self._call(tool="publish_templates_list", db_path=str(self.db_path))
        self.assertEqual([t["id"] for t in listed], [created["id"]])

        fetched = self._call(
            tool="publish_templates_get", template_id=created["id"], db_path=str(self.db_path)
        )
        self.assertEqual(fetched["config"], {"profileIds": [1]})

        updated = self._call(
            tool="publish_templates_update",
            template_id=created["id"],
            description="updated",
            db_path=str(self.db_path),
        )
        self.assertEqual(updated["description"], "updated")

        self._call(
            tool="publish_templates_delete",
            template_id=created["id"],
            db_path=str(self.db_path),
        )
        listed = self._call(tool="publish_templates_list", db_path=str(self.db_path))
        self.assertEqual(listed, [])

    def test_template_get_missing_returns_error_envelope(self) -> None:
        out = self._call(tool="publish_templates_get", template_id=999, db_path=str(self.db_path))
        self.assertEqual(out["error"], "not_found")

    def test_template_create_blank_name_rejected(self) -> None:
        out = self._call(
            tool="publish_templates_create", name="   ", db_path=str(self.db_path)
        )
        self.assertEqual(out["error"], "invalid_input")


class FilesToolTests(_McpTestCase):
    def test_upload_register_rejects_directory_traversal(self) -> None:
        out = self._call(tool="upload_register", file_path="../escape.txt", db_path=str(self.db_path))
        self.assertEqual(out["error"], "invalid_input")
        self.assertIn("escapes", out["message"])

    def test_upload_register_records_missing_file(self) -> None:
        # The helper silently inserts a row for files that don't exist yet
        # (filesize=None) — the MCP tool surfaces that as `exists=False`.
        out = self._call(
            tool="upload_register", file_path="not-on-disk.mp4", db_path=str(self.db_path)
        )
        self.assertEqual(out["fileRecordId"], 1)
        self.assertEqual(out["filePath"], "not-on-disk.mp4")
        self.assertFalse(out["exists"])

    def test_upload_register_is_idempotent(self) -> None:
        first = self._call(tool="upload_register", file_path="x.mp4", db_path=str(self.db_path))
        second = self._call(tool="upload_register", file_path="x.mp4", db_path=str(self.db_path))
        self.assertEqual(first["fileRecordId"], second["fileRecordId"])


class JobsToolTests(_McpTestCase):
    def test_jobs_list_get_cancel_roundtrip(self) -> None:
        # Queue a job via the underlying myUtils.jobs so we don't have to
        # run the full publish pipeline for this test.
        from myUtils import jobs as job_runtime

        spec = job_runtime.JobSpec(
            platform="douyin",
            payload={"title": "t"},
            targets=[("acct-1", "file-1", None)],
        )
        job = job_runtime.enqueue_job(spec, db_path=self.db_path)

        listed = self._call(tool="jobs_list", db_path=str(self.db_path))
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], job.id)

        fetched = self._call(tool="jobs_get", job_id=job.id, db_path=str(self.db_path))
        self.assertEqual(fetched["platform"], "douyin")
        self.assertEqual(len(fetched["targets"]), 1)

        cancelled = self._call(tool="jobs_cancel", job_id=job.id, db_path=str(self.db_path))
        self.assertEqual(cancelled["status"], job_runtime.JOB_CANCELLED)

    def test_jobs_get_missing_returns_error_envelope(self) -> None:
        out = self._call(tool="jobs_get", job_id=999, db_path=str(self.db_path))
        self.assertEqual(out["error"], "not_found")


class PublishToolTests(_McpTestCase):
    """Smoke tests for the publish orchestrator tools.

    We don't run a real upload (that needs LLM + cookies); we just verify
    the tool surfaces ``not_found`` / ``invalid_input`` envelopes when
    the inputs are wrong and that valid inputs reach the orchestrator.
    """

    def test_publish_submit_missing_profile_returns_not_found(self) -> None:
        out = self._call(
            tool="publish_submit",
            profile_ids=[999],
            media_file_paths=["x.mp4"],
            brief="hi",
            db_path=str(self.db_path),
        )
        self.assertEqual(out["error"], "not_found")

    def test_publish_preview_returns_profiles_envelope(self) -> None:
        profile, _ = self._seed()
        out = self._call(
            tool="publish_preview",
            profile_ids=[profile["id"]],
            brief="hello world",
            db_path=str(self.db_path),
        )
        # Either profiles[].drafts are present (LLM worked) or every
        # draft carries an `error` key (LLM disabled in the test env).
        # Both shapes satisfy the contract.
        self.assertIn("profiles", out)
        self.assertEqual(len(out["profiles"]), 1)

    def test_publish_regenerate_wrong_profile_for_account(self) -> None:
        profile, _ = self._seed()
        # A second profile under a different name; same platform+account.
        profile2 = self._call(
            tool="profiles_create", name="Other", db_path=str(self.db_path)
        )
        # ask to regenerate using profile2 against the original account —
        # cross-profile lookups are rejected with ValueError.
        out = self._call(
            tool="publish_regenerate",
            profile_id=profile2["id"],
            account_id=self._seed_for_other(profile),
            brief="x",
            db_path=str(self.db_path),
        )
        self.assertEqual(out["error"], "invalid_input")

    def _seed_for_other(self, profile):
        # Used by the cross-profile test — reuse the original account id.
        return self._call(
            tool="accounts_list",
            profile_id=profile["id"],
            db_path=str(self.db_path),
        )[0]["id"]


if __name__ == "__main__":
    unittest.main()
