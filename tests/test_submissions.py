"""Real-SQLite tests for atomic, idempotent asynchronous admission."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from alembic import command
from alembic.config import Config

from db import createTable as create_table
from myUtils import campaign_media_prep, campaigns, profiles, publish_services, submissions


def _migration_config(path):
    # Keep the real migrations, but avoid the host's cp950 decoding of the
    # UTF-8 alembic.ini and override any ambient production SAU_DB_URL.
    url = f"sqlite:///{path.resolve()}"
    cfg = Config(cmd_opts=SimpleNamespace(x=[f"url={url}"]))
    cfg.set_main_option("script_location", str(create_table._PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="module")
def schema_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("submission-schema") / "schema.db"
    with patch.object(create_table, "_stamp_alembic_head"):
        create_table.bootstrap(path)
    cfg = _migration_config(path)
    command.stamp(cfg, create_table.RAW_SCHEMA_REVISION)
    command.upgrade(cfg, "head")
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0024_publish_submissions"
        assert conn.execute("SELECT COUNT(*) FROM publish_submissions").fetchone()[0] == 0
    return path


@pytest.fixture
def setup(tmp_path, monkeypatch, schema_db):
    db_path = tmp_path / "submit.db"
    shutil.copyfile(schema_db, db_path)
    root = tmp_path / "media"
    (root / "videoFile").mkdir(parents=True)
    (root / "videoFile" / "walk_sfw.mp4").write_bytes(b"test video bytes")
    (root / "videoFile" / "still_sfw.jpg").write_bytes(b"test image bytes")
    monkeypatch.setattr(campaign_media_prep, "BASE_DIR", root)
    first = profiles.create_profile("One", settings={"default_language": "zh-TW"}, db_path=db_path)
    second = profiles.create_profile("Two", db_path=db_path)
    a1 = profiles.add_account(first.id, "telegram", "first", auth_type="api", db_path=db_path)
    a2 = profiles.add_account(second.id, "twitter", "second", auth_type="api", db_path=db_path)
    request = dict(
        profile_ids=[first.id, second.id], selected_account_ids=[a1.id, a2.id],
        media_file_paths=["walk_sfw.mp4"], brief="Keep the original language and voice.",
        options={"useLlm": True, "watermarkDisabled": True},
        schedule={"publishNow": False, "startAt": "2030-03-04T08:00:00+08:00"},
        account_drafts={a1.id: {"message": "今天走到河邊，剛好看到夕陽。"}},
        tiktok_post_settings={a2.id: {"privacyLevel": "SELF_ONLY"}}, db_path=db_path,
    )
    return SimpleNamespace(db=db_path, root=root, first=first, second=second,
                           a1=a1, a2=a2, request=request)


_TABLES = ("file_records", "media_groups", "media_group_items", "campaigns",
           "campaign_posts", "publish_jobs", "publish_job_targets", "publish_submissions")


def _counts(db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in _TABLES}


def _snapshot(db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        return {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in _TABLES}


def _assert_one_submission(setup, result):
    assert len(result.campaign_ids) == 2
    assert result.jobs == []
    assert result.skipped == []
    assert _counts(setup.db) == dict(zip(_TABLES, (1, 1, 1, 2, 0, 0, 0, 1)))


def test_repeat_uses_durable_receipt_and_full_prep_request(setup):
    first = publish_services.submit_publish(**setup.request, submission_id="request-1")
    second = publish_services.submit_publish(**setup.request, submission_id="request-1")
    assert asdict(second) == asdict(first)
    _assert_one_submission(setup, second)
    for cid, account_id in zip(first.campaign_ids, (setup.a1.id, setup.a2.id)):
        campaign = campaigns.get_campaign(cid, db_path=setup.db)
        assert campaign.status == campaigns.CAMPAIGN_PREPARING
        assert campaign.prepared_at is None
        assert campaign.selected_account_ids == [account_id]
        prep = campaigns.get_prep_request(campaign)
        assert prep["brief"] == setup.request["brief"]
        assert prep["options"] == setup.request["options"]
        assert prep["schedule"] == setup.request["schedule"]
        assert prep["accountDrafts"] == {str(k): v for k, v in setup.request["account_drafts"].items()}
        assert prep["tiktokPostSettings"] == {str(k): v for k, v in setup.request["tiktok_post_settings"].items()}
        assert prep["selectedAccountIds"] == [account_id]
        assert prep["mediaFilePaths"] == ["walk_sfw.mp4"]
        assert prep["contentRating"] == "sfw"
        assert prep["submissionId"] == "request-1"
    with closing(sqlite3.connect(setup.db)) as conn:
        row = conn.execute("SELECT scope, request_hash, request_json, result_json, created_at FROM publish_submissions").fetchone()
        assert row[0] == submissions.LEGACY_SCOPE
        assert row[1] == hashlib.sha256(row[2].encode("utf-8")).hexdigest()
        assert json.loads(row[3]) == asdict(first)
        assert row[4]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    # Returned objects are not an in-process cache of mutable result lists.
    first.campaign_ids.clear()
    assert publish_services.submit_publish(**setup.request, submission_id="request-1") == second


def test_missing_id_is_stable_across_normalized_request_shapes(setup):
    first = submissions.submit_publish(**setup.request)
    equivalent = dict(setup.request)
    equivalent.update(
        profile_ids=[str(setup.second.id), setup.first.id, setup.first.id],
        selected_account_ids=[setup.a2.id, str(setup.a1.id), setup.a1.id],
        options={"watermarkDisabled": True, "useLlm": True},
        media_file_paths=[Path("walk_sfw.mp4"), "walk_sfw.mp4"],
        account_drafts={str(k): v for k, v in setup.request["account_drafts"].items()},
    )
    assert submissions.submit_publish(**equivalent) == first
    _assert_one_submission(setup, first)
    with closing(sqlite3.connect(setup.db)) as conn:
        sid, digest = conn.execute("SELECT submission_id, request_hash FROM publish_submissions").fetchone()
    assert sid == f"sha256:{digest}"
    assert len(digest) == 64
    deliberate = submissions.submit_publish(**setup.request, submission_id="deliberate-repeat")
    assert set(deliberate.campaign_ids).isdisjoint(first.campaign_ids)
    assert _counts(setup.db)["file_records"] == 1
    assert _counts(setup.db)["publish_submissions"] == 2


@pytest.mark.parametrize("change", [
    {"brief": "A different brief"},
    {"options": {"includeZhForEnglish": True, "sfwFlag": "nsfw"}},
    {"schedule": {"startAt": "2031-01-01T12:00:00Z"}},
    {"media_file_paths": ["still_sfw.jpg"]},
    {"account_drafts": {"1": {"message": "A new caption."}}},
    {"tiktok_post_settings": {"1": {"privacyLevel": "PUBLIC_TO_EVERYONE"}}},
    {"profile_ids": [1]}, {"selected_account_ids": [1]},
    {"source": "inbox"},
])
def test_same_id_with_any_changed_intent_rejects_without_writes(setup, change):
    submissions.submit_publish(**setup.request, submission_id="same")
    before = _snapshot(setup.db)
    with pytest.raises(ValueError, match="different publish intent"):
        submissions.submit_publish(**(setup.request | change), submission_id="same")
    assert _snapshot(setup.db) == before


def test_media_order_is_part_of_intent(setup):
    request = setup.request | {"media_file_paths": ["still_sfw.jpg", "walk_sfw.mp4"]}
    submissions.submit_publish(**request, submission_id="ordered")
    with pytest.raises(ValueError, match="different publish intent"):
        submissions.submit_publish(**(request | {"media_file_paths": list(reversed(request["media_file_paths"]))}),
                                   submission_id="ordered")
    with closing(sqlite3.connect(setup.db)) as conn:
        rows = conn.execute("SELECT role, sort_order FROM media_group_items ORDER BY sort_order").fetchall()
        assert rows == [("image", 0), ("video", 1)]
        primary = conn.execute("SELECT primary_video_file_id FROM media_groups").fetchone()[0]
        assert conn.execute("SELECT file_path FROM file_records WHERE id = ?", (primary,)).fetchone()[0] == "walk_sfw.mp4"


@pytest.mark.parametrize("explicit_id", [None, "concurrent-intent"])
def test_concurrent_identical_calls_create_exactly_one_submission(setup, explicit_id):
    gate = threading.Barrier(8)
    def submit(_):
        gate.wait(timeout=15)
        return submissions.submit_publish(**setup.request, submission_id=explicit_id)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(8)))
    assert all(result == results[0] for result in results)
    _assert_one_submission(setup, results[0])


def test_concurrent_conflicting_intents_have_one_winner(setup):
    gate = threading.Barrier(2)
    def submit(brief):
        gate.wait(timeout=15)
        try:
            return submissions.submit_publish(**(setup.request | {"brief": brief}), submission_id="race")
        except ValueError as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ["first intent", "second intent"]))
    assert sum(isinstance(result, ValueError) for result in results) == 1
    _assert_one_submission(setup, next(result for result in results if not isinstance(result, ValueError)))


def test_invalid_second_profile_leaves_no_half_created_campaign(setup):
    with pytest.raises(ValueError, match="Profile not found"):
        submissions.submit_publish(**(setup.request | {"profile_ids": [setup.first.id, 999999]}))
    assert not any(_counts(setup.db).values())


@pytest.mark.parametrize("account_ids", [[999999], [1, 999999], [2]])
def test_invalid_or_foreign_selected_account_rolls_back(setup, account_ids):
    with pytest.raises(ValueError, match="Account.*(not found|does not belong)"):
        submissions.submit_publish(**(setup.request | {
            "profile_ids": [setup.first.id], "selected_account_ids": account_ids,
        }))
    assert not any(_counts(setup.db).values())


@pytest.mark.parametrize("fail_table", ["campaigns", "publish_submissions"])
def test_late_sql_failure_rolls_back_files_groups_campaigns_and_receipt(setup, fail_table):
    condition = f"WHEN NEW.profile_id = {setup.second.id}" if fail_table == "campaigns" else ""
    with closing(sqlite3.connect(setup.db)) as conn, conn:
        conn.execute(f"CREATE TRIGGER fail_insert BEFORE INSERT ON {fail_table} {condition} "
                     "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        submissions.submit_publish(**setup.request, submission_id="retry-after-rollback")
    assert not any(_counts(setup.db).values())
    with closing(sqlite3.connect(setup.db)) as conn, conn:
        conn.execute("DROP TRIGGER fail_insert")
    _assert_one_submission(setup, submissions.submit_publish(**setup.request, submission_id="retry-after-rollback"))


def test_submit_uses_one_writer_transaction_and_no_slow_callbacks(setup, monkeypatch):
    statements = []
    connect = sqlite3.connect
    def traced_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(submissions.sqlite3, "connect", traced_connect)
    resolver = campaign_media_prep._resolve_video_file_path_safely
    def unlocked_resolver(path):
        assert "BEGIN IMMEDIATE" not in statements
        return resolver(path)
    monkeypatch.setattr(campaign_media_prep, "_resolve_video_file_path_safely", unlocked_resolver)
    forbidden = Mock(side_effect=AssertionError("slow work on the admission path"))
    for module, name in (
        (publish_services, "_generate_account_draft"),
        (publish_services, "_prepare_campaign_media_artifacts"),
        (campaign_media_prep, "prepare_campaign_media_artifacts"),
        (campaign_media_prep, "probe_media_kind"),
        (campaign_media_prep, "_download_file_from_storage"),
        (publish_services.llm_client, "generate_chat_completion"),
        (campaigns, "create_campaign"),
    ):
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setenv("SAU_ASYNC_PREP", "0")  # It is not a synchronous escape hatch.
    _assert_one_submission(setup, submissions.submit_publish(**setup.request))
    forbidden.assert_not_called()
    assert statements.count("BEGIN IMMEDIATE") == 1
    assert statements.count("COMMIT") == 1
    assert not any(s.lstrip().startswith(("UPDATE ", "DELETE ")) for s in statements)


@pytest.mark.parametrize("path", ["../outside_sfw.mp4", "missing_sfw.mp4", "https://example.invalid/unknown_sfw.mp4"])
def test_unregistered_unsafe_or_absent_paths_are_rejected(setup, path):
    (setup.root / "outside_sfw.mp4").write_bytes(b"not inside the media root")
    with pytest.raises(ValueError, match="(Unsafe media path|not registered or present locally)"):
        submissions.submit_publish(**(setup.request | {"media_file_paths": ["walk_sfw.mp4", path]}))
    assert not any(_counts(setup.db).values())


def test_unregistered_absolute_external_file_is_rejected(setup):
    outside = setup.root / "outside_sfw.mp4"
    outside.write_bytes(b"outside")
    with pytest.raises(ValueError, match="Unsafe media path"):
        submissions.submit_publish(**(setup.request | {"media_file_paths": [str(outside)]}))
    assert not any(_counts(setup.db).values())


def test_known_remote_file_needs_no_local_path_or_network(setup, monkeypatch):
    remote = "https://cdn.example.invalid/registered_sfw.mp4"
    with closing(sqlite3.connect(setup.db)) as conn, conn:
        fid = conn.execute("INSERT INTO file_records (filename, file_path, storage_cdn_url) VALUES (?, ?, ?)",
                           ("registered_sfw.mp4", remote, remote)).lastrowid
    forbidden = Mock(side_effect=AssertionError("remote files must not be fetched at submit"))
    monkeypatch.setattr(campaign_media_prep, "_resolve_video_file_path_safely", forbidden)
    result = submissions.submit_publish(**(setup.request | {"media_file_paths": [remote]}))
    assert len(result.campaign_ids) == 2
    assert _counts(setup.db)["file_records"] == 1
    with closing(sqlite3.connect(setup.db)) as conn:
        assert conn.execute("SELECT file_record_id FROM media_group_items").fetchone()[0] == fid
    forbidden.assert_not_called()


def test_nsfw_and_photo_platform_filters_match_async_contract(setup):
    db_path = setup.db
    youtube = profiles.add_account(setup.first.id, "youtube", "yt", auth_type="api", db_path=db_path)
    tiktok = profiles.add_account(setup.first.id, "tiktok", "tt", auth_type="api", db_path=db_path)
    request = setup.request | {"profile_ids": [setup.first.id], "selected_account_ids": None,
                               "media_file_paths": ["still_sfw.jpg"]}
    result = submissions.submit_publish(**request)
    assert {row["accountId"] for row in result.skipped} == {youtube.id, tiktok.id}
    assert campaigns.get_campaign(result.campaign_ids[0], db_path=db_path).selected_account_ids == [setup.a1.id]
    direct = submissions.submit_publish(**(request | {"options": {"tiktokDirectPost": True}}))
    assert [row["accountId"] for row in direct.skipped] == [youtube.id]
    assert set(campaigns.get_campaign(direct.campaign_ids[0], db_path=db_path).selected_account_ids) == {setup.a1.id, tiktok.id}
    (setup.root / "videoFile" / "unlabelled.mp4").write_bytes(b"nsfw by filename")
    restricted = submissions.submit_publish(**(request | {"media_file_paths": ["unlabelled.mp4"],
                                                         "options": {"sfwFlag": "sfw"}}))
    campaign = campaigns.get_campaign(restricted.campaign_ids[0], db_path=db_path)
    assert campaign.selected_account_ids == [setup.a1.id]
    assert campaign.metadata["contentRating"] == "nsfw"
    all_restricted = submissions.submit_publish(**(request | {
        "selected_account_ids": [youtube.id, tiktok.id], "options": {"sfwFlag": "nsfw"},
    }))
    assert all_restricted.campaign_ids == []
    assert all_restricted.skipped == [{"profileId": setup.first.id, "reason": "nsfw_no_adult_safe_account"}]
    assert submissions.submit_publish(**(request | {
        "selected_account_ids": [youtube.id, tiktok.id], "options": {"sfwFlag": "nsfw"},
    })) == all_restricted


def test_disabled_account_selection_never_expands_to_other_accounts(setup):
    with closing(sqlite3.connect(setup.db)) as conn, conn:
        conn.execute("UPDATE accounts SET enabled = 0 WHERE id = ?", (setup.a1.id,))
    result = submissions.submit_publish(**(setup.request | {"profile_ids": [setup.first.id],
                                                          "selected_account_ids": [setup.a1.id]}))
    assert result.campaign_ids == []
    assert result.skipped == [{"profileId": setup.first.id, "reason": "no_enabled_accounts"}]


def test_receipt_survives_restart_offload_and_later_campaign_target_changes(setup):
    result = submissions.submit_publish(**setup.request, submission_id="persistent")
    with closing(sqlite3.connect(setup.db)) as conn, conn:
        conn.execute("UPDATE campaigns SET status = 'needs_review', last_error = 'keep me'")
        conn.execute("UPDATE accounts SET enabled = 0")
        job_id = conn.execute("INSERT INTO publish_jobs (idempotency_key, platform, payload_json, status) "
                              "VALUES ('existing-job', 'telegram', '{}', 'running')").lastrowid
        conn.execute("INSERT INTO publish_job_targets (job_id, account_ref, file_ref, status, attempts) "
                     "VALUES (?, 'account:1', 'file:1', 'running', 2)", (job_id,))
    (setup.root / "videoFile" / "walk_sfw.mp4").unlink()
    before = _snapshot(setup.db)
    # A genuinely fresh interpreter, with all web imports and socket connects
    # forbidden, must replay the receipt without revalidating changed entities.
    code = """
import importlib.abc, json, socket, sys
from dataclasses import asdict
class NoWeb(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'flask', 'sau_backend'}:
            raise AssertionError('web import: ' + fullname)
sys.meta_path.insert(0, NoWeb())
def no_network(*args, **kwargs):
    raise AssertionError('unexpected network request')
socket.socket.connect = no_network
from myUtils.publish_services import submit_publish
request = json.loads(sys.stdin.read())
print(json.dumps(asdict(submit_publish(**request)), ensure_ascii=False))
"""
    request = setup.request | {"db_path": str(setup.db), "submission_id": "persistent"}
    child = subprocess.run([sys.executable, "-c", code], input=json.dumps(request),
                           text=True, encoding="utf-8", capture_output=True, timeout=60,
                           cwd=create_table._PROJECT_ROOT,
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8"))
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout) == asdict(result)
    assert _snapshot(setup.db) == before


def test_workspace_receipts_are_separate_and_all_new_rows_inherit_ownership(setup):
    for workspace in ("alpha", "legacy"):
        profile = profiles.create_profile(workspace, workspace_id=workspace, db_path=setup.db)
        account = profiles.add_account(profile.id, "telegram", workspace, auth_type="api", db_path=setup.db)
        name = f"{workspace}_sfw.mp4"
        (setup.root / "videoFile" / name).write_bytes(b"local")
        request = setup.request | {"profile_ids": [profile.id], "selected_account_ids": [account.id],
                                   "media_file_paths": [name], "workspace_id": workspace}
        result = submissions.submit_publish(**request, submission_id="shared-id")
        assert submissions.submit_publish(**request, submission_id="shared-id") == result
        with closing(sqlite3.connect(setup.db)) as conn:
            assert conn.execute("SELECT workspace_id FROM campaigns WHERE id = ?", (result.campaign_ids[0],)).fetchone()[0] == workspace
            for table in ("file_records", "media_groups", "media_group_items", "publish_submissions"):
                assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE workspace_id = ?", (workspace,)).fetchone()[0] == 1
    submissions.submit_publish(**setup.request, submission_id="shared-id")
    with closing(sqlite3.connect(setup.db)) as conn:
        assert {row[0] for row in conn.execute("SELECT scope FROM publish_submissions")} == {
            submissions.LEGACY_SCOPE, "workspace:alpha", "workspace:legacy",
        }


def test_workspace_cannot_borrow_foreign_profiles_accounts_or_files(setup):
    profile = profiles.create_profile("Tenant", workspace_id="tenant", db_path=setup.db)
    account = profiles.add_account(profile.id, "telegram", "tenant", auth_type="api", db_path=setup.db)
    with closing(sqlite3.connect(setup.db)) as conn, conn:
        conn.execute("INSERT INTO file_records (filename, file_path, workspace_id) "
                     "VALUES ('private_sfw.mp4', 'private_sfw.mp4', 'other')")
    request = setup.request | {"workspace_id": "tenant"}
    with pytest.raises(ValueError, match="Profile not found"):
        submissions.submit_publish(**request)
    request |= {"profile_ids": [profile.id], "selected_account_ids": [setup.a1.id]}
    with pytest.raises(ValueError, match="Account not found"):
        submissions.submit_publish(**request)
    request |= {"selected_account_ids": [account.id], "media_file_paths": ["private_sfw.mp4"]}
    with pytest.raises(ValueError, match="not available in this workspace"):
        submissions.submit_publish(**request)
    assert _counts(setup.db)["campaigns"] == 0
    assert _counts(setup.db)["publish_submissions"] == 0


def test_migration_upgrade_downgrade_and_uniqueness(tmp_path, schema_db):
    path = tmp_path / "migration.db"
    cfg = _migration_config(path)
    command.upgrade(cfg, "head")
    with closing(sqlite3.connect(schema_db)) as bootstrap, closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA table_info(publish_submissions)").fetchall() == bootstrap.execute("PRAGMA table_info(publish_submissions)").fetchall()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO publish_submissions (scope, submission_id, request_hash, request_json, result_json) "
                     "VALUES ('legacy', 'unique', 'hash', '{}', '{}')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO publish_submissions (scope, submission_id, request_hash, request_json, result_json) "
                         "VALUES ('legacy', 'unique', 'hash', '{}', '{}')")
    command.downgrade(cfg, "0023_publish_reliability")
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'publish_submissions'").fetchone() is None
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'campaigns'").fetchone()
    command.upgrade(cfg, "head")
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM publish_submissions").fetchone()[0] == 0
