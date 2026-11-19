"""Opt-in recovery of exact restored dependency waits, never a quota refund."""
from __future__ import annotations

import copy
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from myUtils import jobs, media_restore, platform_limits, reviewed_recovery
from scripts import recover_reviewed_publications as reviewed
from tests.test_reviewed_prerequisite_recovery import NOW, rows
from tests.test_reviewed_prerequisite_recovery import clips as clips, database as database


@pytest.fixture
def restored(database, tmp_path, monkeypatch):
    path, ids, native = database
    config = tmp_path / "synthetic-rclone.conf"
    config.write_text("[fixture-drive]\ntype = drive\nclient_id = fixture-client\nclient_secret = fixture-secret\nscope = drive\n")
    monkeypatch.setenv("RCLONE_CONFIG", str(config))
    monkeypatch.setitem(platform_limits.VIDEO_MAX_SECONDS, "threads", 10)
    network = Mock(side_effect=AssertionError("Restored-wait review cannot probe, restore or post"))
    monkeypatch.setattr(media_restore, "restore_artifact", network)
    monkeypatch.setattr(media_restore, "probe_source", network)
    monkeypatch.setattr(media_restore, "_admit", network)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        backend_id = conn.execute("INSERT INTO storage_backends(slug,provider,bucket,region,endpoint,access_key,secret_key) "
            "VALUES ('restored-fixture','rclone','fixture-drive','','fixture-root','','')").lastrowid
        backend = dict(conn.execute("SELECT * FROM storage_backends WHERE id=?", (backend_id,)).fetchone())
        for index, target_id in enumerate(ids):
            target = next(row for row in rows(path) if row["id"] == target_id)
            checkpoint = json.loads(target["checkpoint_json"])
            payload = checkpoint["payload"]
            artifact = payload["artifacts"][0]
            if index:
                owned = tmp_path / "owned-second-selected_sfw.mkv"
                shutil.copy2(artifact["local_path"], owned)
                artifact["local_path"] = str(owned)
                conn.execute("UPDATE publish_job_targets SET file_ref=? WHERE id=?", (str(owned), target_id))
            metadata = {**artifact["metadata"], "sha256": artifact["sha256"], "size_bytes": artifact["size_bytes"]}
            remote_path = f"fixture/exact-approved-part-{index + 1}.mkv"
            artifact_id = conn.execute("INSERT INTO campaign_artifacts(campaign_id,source_file_record_id,artifact_kind,"
                "local_path,remote_path,storage_backend_id,metadata_json) VALUES (123,901,'local',?,?,?,?)",
                (artifact["local_path"], remote_path, backend_id, json.dumps(metadata))).lastrowid
            artifact["id"] = artifact_id
            category, attempts, exhausted = ("quota", 7, False) if index else ("transport", 8, True)
            due = (NOW + timedelta(minutes=30)).isoformat() if index else None
            checkpoint["prerequisites"].update(category=category, attempts=attempts, exhausted=exhausted,
                                                next_attempt_at=due, cached_wait=True)
            conn.execute("UPDATE publish_jobs SET payload_json=? WHERE id=?", (json.dumps(payload), target["job_id"]))
            conn.execute("UPDATE publish_job_targets SET status=?,attempts=?,error_category=?,last_error=?,"
                "next_attempt_at=?,checkpoint_json=? WHERE id=?", ("retrying" if index else "blocked", attempts,
                "prerequisite_" + category, "media prerequisite unavailable: " + category, due,
                json.dumps(checkpoint), target_id))
            source = {"backend": backend, "storage_key": remote_path, "remote_checksum": None,
                      "expected_size_bytes": artifact["size_bytes"], "workspace_id": None, "account_id": None}
            for (scope, fingerprint), kind in ((media_restore.dependency_identity(source), "media_restore"),
                                               (media_restore._object_identity(source), "media_object")):
                conn.execute("INSERT INTO publish_dependency_states(scope_key,config_fingerprint,kind,status,last_checked_at,updated_at) "
                    "VALUES (?,?,?,'healthy',?,?) ON CONFLICT(scope_key) DO NOTHING",
                    (scope, fingerprint, kind, NOW.isoformat(), NOW.isoformat()))
    for row in rows(path):
        jobs.recount_job(row["job_id"], db_path=path)
    return path, ids, native, network, config


def prepare(restored, tmp_path, *, opt_in=True):
    path, ids, *_ = restored
    return reviewed.prepare_plan(db_path=path, target_ids=ids, review_resolved_prerequisites=True,
        review_restored_dependency_waits=opt_in, out_dir=tmp_path / "prepared", now=NOW)


def apply(restored, plan, *, opt_in=True):
    path, ids, *_ = restored
    return reviewed.apply_plan(plan, db_path=path, target_ids=ids, review_resolved_prerequisites=True,
                               review_restored_dependency_waits=opt_in)


def helper(restored):
    path, ids, *_ = restored
    with jobs._connect(path, must_exist=True) as conn:
        conn.execute("BEGIN")
        row = conn.execute("SELECT * FROM publish_job_targets WHERE id=?", (ids[1],)).fetchone()
        payload = conn.execute("SELECT payload_json FROM publish_jobs WHERE id=?", (row["job_id"],)).fetchone()[0]
        before = conn.total_changes
        proof = reviewed_recovery.assert_restored_dependency(row, payload, conn=conn)
        assert conn.total_changes == before
        return proof


def test_mixed_complete_group_only_recovers_with_explicit_restored_wait_review(restored, tmp_path, monkeypatch):
    path, ids, _, network, _ = restored
    before = rows(path)
    with pytest.raises(ValueError):
        reviewed.prepare_plan(db_path=path, target_ids=ids, review_resolved_prerequisites=True,
                              out_dir=tmp_path / "not-authorized", now=NOW)
    assert rows(path) == before
    monkeypatch.setitem(platform_limits.VIDEO_MAX_SECONDS, "threads", 2.5)
    plan = prepare(restored, tmp_path)
    assert rows(path) == before and all(item["review_restored_dependency_waits"] is True for item in plan)
    assert all(len(item["prepared_payloads"]) == 3 for item in plan)
    with pytest.raises(ValueError, match="restored|review|authority"):
        apply(restored, plan, opt_in=False)
    assert rows(path) == before
    result = apply(restored, json.loads(json.dumps(plan)))
    assert result["applied_target_ids"] == ids and len(result["queued_target_ids"]) == 6 and not result["errors"]
    for row in rows(path):
        checkpoint = json.loads(row["checkpoint_json"])
        origin = checkpoint.get("recovery_origin_target_id", row["id"])
        original = next(item for item in before if item["id"] == origin)
        assert row["attempts"] == original["attempts"]
        assert checkpoint["prerequisites"] == json.loads(original["checkpoint_json"])["prerequisites"]
        assert checkpoint["publish_routes"]["native"]["attempts"] == 1
        assert checkpoint["provider_budget_baseline"] == 1
        assert checkpoint["previous_failure"]["checkpoint"] == json.loads(original["checkpoint_json"])
        assert jobs.get_job(original["job_id"], db_path=path).payload == json.loads(original["checkpoint_json"])["payload"]
    late = max(jobs._utc_datetime(value) for item in plan for value in item["part_schedule_ats"]) + timedelta(minutes=1)
    monkeypatch.setattr(jobs, "_now_iso", lambda: late.isoformat())
    with jobs._connect(path) as conn:
        assert [row["id"] for row in jobs._eligible_targets(conn, late.isoformat())] == [ids[0]]
    network.assert_not_called()


def test_exhausted_blocked_quota_requires_the_same_exact_proof(restored, tmp_path):
    path, ids, *_ = restored
    with closing(sqlite3.connect(path)) as conn, conn:
        row = next(row for row in rows(path) if row["id"] == ids[1])
        checkpoint = json.loads(row["checkpoint_json"])
        checkpoint["prerequisites"].update(attempts=8, exhausted=True, next_attempt_at=None)
        conn.execute("UPDATE publish_job_targets SET status='blocked',attempts=8,next_attempt_at=NULL,checkpoint_json=? WHERE id=?",
                     (json.dumps(checkpoint), ids[1]))
    with pytest.raises(ValueError):
        prepare(restored, tmp_path, opt_in=False)
    plan = prepare(restored, tmp_path)
    assert apply(restored, plan)["applied_target_ids"] == ids
    assert all(row["attempts"] == 8 for row in rows(path))


@pytest.mark.parametrize("change", ["zero", "negative", "bool", "maximum_bool", "over_maximum", "exhausted_retry", "zero_row",
    "wrong_category", "spent_provider", "bad_baseline", "baseline_exceeds_row", "lease", "nested_receipt", "payload_receipt",
    "submission_started", "blocked_not_exhausted", "retrying_other", "review", "running"])
def test_restored_wait_review_refuses_invalid_counters_effects_or_states(restored, change):
    path, ids, *_ = restored
    row = next(row for row in rows(path) if row["id"] == ids[1])
    checkpoint = json.loads(row["checkpoint_json"])
    with closing(sqlite3.connect(path)) as conn, conn:
        if change in {"zero", "negative", "bool", "over_maximum"}:
            checkpoint["prerequisites"]["attempts"] = {"zero": 0, "negative": -1, "bool": True, "over_maximum": 9}[change]
        elif change == "maximum_bool":
            checkpoint["prerequisites"]["max_attempts"] = True
        elif change == "exhausted_retry":
            checkpoint["prerequisites"]["exhausted"] = True
        elif change == "zero_row":
            conn.execute("UPDATE publish_job_targets SET attempts=0 WHERE id=?", (ids[1],))
        elif change == "wrong_category":
            checkpoint["prerequisites"]["category"] = "transport"
        elif change == "spent_provider":
            checkpoint["publish_routes"]["native"]["attempts"] = 3
        elif change == "bad_baseline":
            checkpoint["provider_budget_baseline"] = False
        elif change == "baseline_exceeds_row":
            checkpoint["provider_budget_baseline"] = 99
        elif change == "lease":
            conn.execute("UPDATE publish_job_targets SET lease_token='retain-lease' WHERE id=?", (ids[1],))
        elif change == "nested_receipt":
            checkpoint["older"] = {"provider_receipt": {"id": "retain-effect"}}
        elif change == "payload_receipt":
            payload = copy.deepcopy(checkpoint["payload"])
            payload["history"] = {"provider_receipt": {"id": "retain"}}
            conn.execute("UPDATE publish_jobs SET payload_json=? WHERE id=?", (json.dumps(payload), row["job_id"]))
        elif change == "submission_started":
            checkpoint["submission_started"] = True
        elif change == "blocked_not_exhausted":
            conn.execute("UPDATE publish_job_targets SET status='blocked' WHERE id=?", (ids[1],))
        elif change == "retrying_other":
            checkpoint["prerequisites"]["category"] = "transport"
            conn.execute("UPDATE publish_job_targets SET error_category='prerequisite_transport' WHERE id=?", (ids[1],))
        else:
            conn.execute("UPDATE publish_job_targets SET status=? WHERE id=?", ("needs_review" if change == "review" else "running", ids[1]))
        conn.execute("UPDATE publish_job_targets SET checkpoint_json=? WHERE id=?", (json.dumps(checkpoint), ids[1]))
    before = rows(path)
    with pytest.raises(ValueError):
        helper(restored)
    assert rows(path) == before


@pytest.mark.parametrize("change", ["unknown", "blocked", "probing", "stale_fingerprint", "reason", "hold", "object_hold",
                                      "no_registered_sha", "no_registered_size", "missing_file", "changed_file", "unregistered"])
def test_exact_restored_proof_cannot_be_replaced_by_manifest_or_availability(restored, change):
    path, ids, *_ = restored
    row = next(row for row in rows(path) if row["id"] == ids[1])
    checkpoint = json.loads(row["checkpoint_json"])
    artifact = checkpoint["payload"]["artifacts"][0]
    with closing(sqlite3.connect(path)) as conn, conn:
        if change in {"unknown", "blocked", "probing"}:
            conn.execute("UPDATE publish_dependency_states SET status=? WHERE kind='media_restore'", (change,))
        elif change == "stale_fingerprint":
            conn.execute("UPDATE publish_dependency_states SET config_fingerprint='old' WHERE kind='media_restore'")
        elif change == "reason":
            conn.execute("UPDATE publish_dependency_states SET reason='quota' WHERE kind='media_restore'")
        elif change == "hold":
            conn.execute("UPDATE publish_dependency_states SET blocked_until=? WHERE kind='media_restore'", (NOW.isoformat(),))
        elif change == "object_hold":
            conn.execute("UPDATE publish_dependency_states SET status='blocked',reason='integrity' WHERE kind='media_object'")
        elif change in {"no_registered_sha", "no_registered_size"}:
            metadata = json.loads(conn.execute("SELECT metadata_json FROM campaign_artifacts WHERE id=?", (artifact["id"],)).fetchone()[0])
            metadata.pop("sha256" if change == "no_registered_sha" else "size_bytes")
            conn.execute("UPDATE campaign_artifacts SET metadata_json=? WHERE id=?", (json.dumps(metadata), artifact["id"]))
        elif change == "unregistered":
            conn.execute("DELETE FROM campaign_artifacts WHERE id=?", (artifact["id"],))
        elif change == "missing_file":
            Path(artifact["local_path"]).unlink()
        else:
            Path(artifact["local_path"]).write_bytes(b"different selected bytes")
    before = rows(path)
    with pytest.raises(ValueError):
        helper(restored)
    assert rows(path) == before


@pytest.mark.parametrize("change", ["config", "dependency", "registered_object", "source", "lease", "receipt", "budget"])
def test_stale_restored_wait_plan_is_atomic_for_the_complete_original_group(restored, tmp_path, change):
    path, ids, _, _, config = restored
    plan = prepare(restored, tmp_path)
    with closing(sqlite3.connect(path)) as conn, conn:
        if change == "config":
            config.write_text(config.read_text().replace("fixture-client", "changed-client"))
        elif change == "dependency":
            conn.execute("UPDATE publish_dependency_states SET status='blocked',reason='quota' WHERE kind='media_restore'")
        else:
            row = next(row for row in rows(path) if row["id"] == ids[1])
            checkpoint = json.loads(row["checkpoint_json"])
            artifact = checkpoint["payload"]["artifacts"][0]
            if change == "registered_object":
                conn.execute("UPDATE campaign_artifacts SET remote_path='other/exact-object' WHERE id=?", (artifact["id"],))
            elif change == "source":
                Path(artifact["local_path"]).write_bytes(b"substitute")
            elif change == "lease":
                conn.execute("UPDATE publish_job_targets SET lease_owner='retain' WHERE id=?", (ids[1],))
            else:
                if change == "receipt":
                    checkpoint["older"] = {"provider_receipt": {"id": "retain"}}
                else:
                    checkpoint["publish_routes"]["native"]["attempts"] = 3
                conn.execute("UPDATE publish_job_targets SET checkpoint_json=? WHERE id=?", (json.dumps(checkpoint), ids[1]))
    before = rows(path)
    result = apply(restored, plan)
    assert result["applied_target_ids"] == [] and result["queued_target_ids"] == [] and result["errors"]
    assert rows(path) == before


def test_same_transaction_dependency_recheck_rolls_back_even_an_earlier_part(restored, tmp_path, monkeypatch):
    path, ids, *_ = restored
    plan = prepare(restored, tmp_path)
    before = rows(path)
    original = jobs._reset_target
    def reset(conn, row, schedule_at, **kwargs):
        if row["id"] == ids[1]:
            conn.execute("UPDATE publish_dependency_states SET status='blocked',reason='quota' WHERE kind='media_restore'")
        return original(conn, row, schedule_at, **kwargs)
    monkeypatch.setattr(jobs, "_reset_target", reset)
    result = apply(restored, plan)
    assert not result["applied_target_ids"] and result["errors"] and rows(path) == before
    assert helper(restored)["category"] == "quota"  # In-transaction change was rolled back too.


def test_dependency_review_uses_caller_snapshot_without_another_database_or_provider_call(restored, monkeypatch):
    path, ids, _, network, _ = restored
    with jobs._connect(path, must_exist=True) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM publish_job_targets WHERE id=?", (ids[1],)).fetchone()
        payload = conn.execute("SELECT payload_json FROM publish_jobs WHERE id=?", (row["job_id"],)).fetchone()[0]
        another = Mock(side_effect=AssertionError("Must use the same write snapshot"))
        monkeypatch.setattr(jobs, "_connect", another)
        assert reviewed_recovery.assert_restored_dependency(row, payload, conn=conn)["category"] == "quota"
        another.assert_not_called()
    network.assert_not_called()


def test_healthy_check_timestamp_refresh_does_not_fake_or_invalidate_byte_proof(restored):
    path, *_ = restored
    before = helper(restored)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE publish_dependency_states SET last_checked_at=?,updated_at=?",
                     ((NOW + timedelta(minutes=1)).isoformat(), (NOW + timedelta(minutes=1)).isoformat()))
    assert helper(restored) == before


def test_cli_requires_the_extra_operator_flag_in_both_phases(restored, tmp_path):
    path, ids, *_ = restored
    plan_file = tmp_path / "private-plan.json"
    args = ["--db-path", str(path), "--plan-file", str(plan_file), "--review-resolved-prerequisites"]
    for target_id in ids:
        args += ["--target-id", str(target_id)]
    assert reviewed.main(args + ["--prepare", "--review-restored-dependency-waits", "--out-dir", str(tmp_path / "cli-parts")]) == 0
    with pytest.raises(SystemExit) as error:
        reviewed.main(args + ["--apply"])
    assert error.value.code == 2 and len(rows(path)) == 2
    assert reviewed.main(args + ["--apply", "--review-restored-dependency-waits"]) == 0


@pytest.fixture
def restored_files(restored):
    """Explicit exact selected files; raw source id 901 is only provenance."""
    path, ids, *_ = restored
    with closing(sqlite3.connect(path)) as conn, conn:
        for target_id in ids:
            target = next(row for row in rows(path) if row["id"] == target_id)
            checkpoint = json.loads(target["checkpoint_json"])
            payload = checkpoint["payload"]
            artifact = payload["artifacts"][0]
            registration = conn.execute("SELECT storage_backend_id,remote_path FROM campaign_artifacts WHERE id=?",
                                        (artifact["id"],)).fetchone()
            digest, size = artifact.pop("sha256"), artifact.pop("size_bytes")
            file_id = conn.execute("INSERT INTO file_records(filename,file_path,filesize,sha256,size_bytes,"
                "storage_backend_id,storage_key,workspace_id) VALUES (?,?,?,?,?,?,?,?)",
                (Path(artifact["local_path"]).name, artifact["local_path"], size / 1024**2, digest, size,
                 registration[0], registration[1], target["workspace_id"])).lastrowid
            conn.execute("DELETE FROM campaign_artifacts WHERE id=?", (artifact.pop("id"),))
            artifact["file_record_id"] = file_id
            conn.execute("UPDATE publish_jobs SET payload_json=? WHERE id=?", (json.dumps(payload), target["job_id"]))
            conn.execute("UPDATE publish_job_targets SET checkpoint_json=? WHERE id=?", (json.dumps(checkpoint), target_id))
    return restored


def test_exact_file_records_fill_missing_payload_byte_facts_without_changing_originals(restored_files, tmp_path):
    path, ids, _, network, _ = restored_files
    before = rows(path)
    originals = [jobs.get_job(row["job_id"], db_path=path).payload for row in before]
    proof = helper(restored_files)
    assert proof["media"][0]["file_record_id"] == originals[1]["artifacts"][0]["file_record_id"]
    assert all("sha256" not in value["artifacts"][0] and "size_bytes" not in value["artifacts"][0] for value in originals)
    plan = prepare(restored_files, tmp_path)
    assert rows(path) == before
    result = apply(restored_files, plan)
    assert result["applied_target_ids"] == ids and not result["errors"]
    for row, original in zip(before, originals):
        assert jobs.get_job(row["job_id"], db_path=path).payload == original
        installed = jobs.get_target(row["id"], db_path=path)
        assert installed.attempts == row["attempts"]
    network.assert_not_called()


@pytest.mark.parametrize("change", ["missing_sha", "missing_size", "conflicting_sha", "conflicting_size",
                                      "foreign_path", "foreign_workspace", "missing_record", "raw_provenance_only"])
def test_file_record_review_still_refuses_missing_conflicting_or_unowned_proof(restored_files, change):
    path, ids, *_ = restored_files
    target = next(row for row in rows(path) if row["id"] == ids[1])
    checkpoint = json.loads(target["checkpoint_json"])
    artifact = checkpoint["payload"]["artifacts"][0]
    file_id = artifact["file_record_id"]
    with closing(sqlite3.connect(path)) as conn, conn:
        if change in {"missing_sha", "missing_size"}:
            conn.execute("UPDATE file_records SET " + ("sha256" if change == "missing_sha" else "size_bytes") + "=NULL WHERE id=?", (file_id,))
        elif change in {"conflicting_sha", "conflicting_size"}:
            artifact["sha256" if change == "conflicting_sha" else "size_bytes"] = "a" * 64 if change == "conflicting_sha" else 1
            conn.execute("UPDATE publish_job_targets SET checkpoint_json=? WHERE id=?", (json.dumps(checkpoint), ids[1]))
        elif change == "foreign_path":
            conn.execute("UPDATE file_records SET file_path='unrelated/raw-source.mkv' WHERE id=?", (file_id,))
        elif change == "foreign_workspace":
            conn.execute("UPDATE file_records SET workspace_id='foreign-workspace' WHERE id=?", (file_id,))
        else:
            conn.execute("DELETE FROM file_records WHERE id=?", (file_id,))
            if change == "raw_provenance_only":
                artifact.pop("file_record_id")
                conn.execute("INSERT INTO file_records(id,filename,file_path,sha256,size_bytes) VALUES (901,'raw.mkv','unrelated/raw.mkv',?,1)", ("a" * 64,))
                conn.execute("UPDATE publish_job_targets SET checkpoint_json=? WHERE id=?", (json.dumps(checkpoint), ids[1]))
    before = rows(path)
    with pytest.raises(ValueError):
        helper(restored_files)
    assert rows(path) == before


@pytest.mark.parametrize("field,value", [("sha256", "a" * 64), ("size_bytes", 1), ("storage_key", "another/selected-part.mkv")])
def test_changed_exact_file_registration_rolls_back_the_complete_group(restored_files, tmp_path, field, value):
    path, ids, *_ = restored_files
    plan = prepare(restored_files, tmp_path)
    target = next(row for row in rows(path) if row["id"] == ids[1])
    artifact = json.loads(target["checkpoint_json"])["payload"]["artifacts"][0]
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE file_records SET " + field + "=? WHERE id=?", (value, artifact["file_record_id"]))
    before = rows(path)
    result = apply(restored_files, plan)
    assert not result["applied_target_ids"] and not result["queued_target_ids"] and result["errors"]
    assert rows(path) == before


def test_file_record_review_keeps_the_callers_snapshot(restored_files, monkeypatch):
    path, ids, _, network, _ = restored_files
    with jobs._connect(path, must_exist=True) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM publish_job_targets WHERE id=?", (ids[1],)).fetchone()
        payload = conn.execute("SELECT payload_json FROM publish_jobs WHERE id=?", (row["job_id"],)).fetchone()[0]
        another = Mock(side_effect=AssertionError("File facts must use the same write snapshot"))
        monkeypatch.setattr(jobs, "_connect", another)
        assert reviewed_recovery.assert_restored_dependency(row, payload, conn=conn)["media"][0]["file_record_id"]
        another.assert_not_called()
    network.assert_not_called()
