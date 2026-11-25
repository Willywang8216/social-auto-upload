"""Executed no-network gates for explicit prerequisite recovery and ordered delivery."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from db import createTable
from myUtils import jobs, profiles, platform_limits, media_compliance, delivery, reviewed_recovery
from myUtils import publish_media_guard as guard, prepared_recovery
from scripts import recover_reviewed_publications as reviewed, preprocess_failed_targets as ordinary
from tests.legacy_queue import enqueue_legacy_job

NOW = datetime(2035, 1, 2, 12)


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Requires ffmpeg and ffprobe")
    root = tmp_path_factory.mktemp("reviewed-exact-clips")
    result = []
    for index in range(2):
        path = root / f"approved-part-{index + 1}_sfw.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-n", "-f", "lavfi", "-i",
            "testsrc2=size=640x360:rate=30", "-vf", "hflip" if index else "null", "-t", "6.1",
            "-c:v", "ffv1", str(path)], check=True, capture_output=True, timeout=60)
        result.append(path)
    assert media_compliance.sha256_file(result[0]) != media_compliance.sha256_file(result[1])
    return result


@pytest.fixture
def database(tmp_path, monkeypatch, clips):
    path = tmp_path / "reviewed.db"
    createTable.bootstrap(path)
    profile = profiles.create_profile("Fixture", db_path=path)
    account = profiles.add_account(profile.id, platform="threads", account_name="fixture", auth_type="oauth",
        config={"threadUserId": "fixture"}, db_path=path)
    monkeypatch.setattr(jobs, "_now_iso", lambda: NOW.isoformat())
    monkeypatch.setitem(platform_limits.VIDEO_MAX_SECONDS, "threads", 2.5)
    for key in ("SAU_THREADS_ENABLED", "SAU_SOCIAMONIALS_FALLBACK", "SAU_X_DIRECT_PUBLISH"):
        monkeypatch.delenv(key, raising=False)
    native = Mock(return_value={"verified": True})
    monkeypatch.setattr(reviewed.preparation_context, "verify_native_destination", native)
    targets = []
    for index, clip in enumerate(clips, 1):
        artifact = {"local_path": str(clip), "artifact_kind": "local", "source_file_record_id": 901,
            "sha256": media_compliance.sha256_file(clip), "size_bytes": clip.stat().st_size,
            "metadata": {"role": "video", "part_index": index, "part_count": 2}}
        payload = {"message": f"Approved caption 原稿 {index}", "draft": {}, "campaignId": 123,
            "partIndex": index, "partCount": 2, "artifacts": [artifact]}
        job = enqueue_legacy_job(jobs.JobSpec(platform="threads", profile_id=profile.id, payload=payload,
            targets=[(f"account:{account.id}", str(clip), NOW.isoformat())]), db_path=path)
        target = jobs.list_targets(job.id, db_path=path)[0]
        checkpoint = {"payload": payload, "provider_budget_baseline": 1,
            "publish_routes": {"active": "native", "native": {"attempts": 1, "max_attempts": 3, "outcome": "failed"}},
            "prerequisites": {"attempts": 8, "max_attempts": 8, "category": "integrity", "exhausted": True},
            "readiness": {"ready": False, "phase": "blocked_intent"}}
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("UPDATE publish_job_targets SET status='blocked',attempts=8,last_error='Missing exact media',"
                "error_category='prerequisite_integrity',finished_at=?,checkpoint_json=? WHERE id=?",
                (NOW.isoformat(), json.dumps(checkpoint), target.id))
        jobs.recount_job(job.id, db_path=path)
        targets.append(target.id)
    return path, targets, native


def prepare(database, tmp_path):
    path, ids, _ = database
    return reviewed.prepare_plan(db_path=path, target_ids=ids, review_resolved_prerequisites=True,
                                 out_dir=tmp_path / "parts", now=NOW)


def apply(database, plan):
    path, ids, _ = database
    return reviewed.apply_plan(plan, db_path=path, target_ids=ids, review_resolved_prerequisites=True)


def rows(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM publish_job_targets ORDER BY id")]


@pytest.mark.parametrize("category", ["integrity", "verification"])
def test_complete_reviewed_group_preserves_exact_selected_content_and_all_budgets(database, clips, tmp_path, category):
    path, ids, native = database
    if category != "integrity":
        with closing(sqlite3.connect(path)) as conn, conn:
            for row in rows(path):
                checkpoint = json.loads(row["checkpoint_json"])
                checkpoint["prerequisites"]["category"] = category
                conn.execute("UPDATE publish_job_targets SET error_category=?,checkpoint_json=? WHERE id=?",
                    ("prerequisite_" + category, json.dumps(checkpoint), row["id"]))
    before = rows(path)
    jobs_before = [jobs.get_job(row["job_id"], db_path=path).payload for row in before]
    plan = prepare(database, tmp_path)
    assert rows(path) == before  # Encoding is not a retry or a submission.
    assert [item["target_id"] for item in plan] == ids
    assert all(len(item["prepared_payloads"]) == 3 for item in plan)
    assert native.call_count >= 4
    for item, clip in zip(plan, clips):
        media = [part["artifacts"][0] for part in item["prepared_payloads"]]
        assert media[0]["metadata"]["source_start_seconds"] == 0
        assert media[-1]["metadata"]["source_end_seconds"] == pytest.approx(6.1)
        for left, right in zip(media, media[1:]):
            assert left["metadata"]["source_end_seconds"] == right["metadata"]["source_start_seconds"]
        for output in media:
            metadata = output["metadata"]
            assert metadata["source_is_complete"] is False
            assert metadata["source_sha256"] is None  # Never invent the unavailable raw SHA.
            assert metadata["reprepared_from"]["part_count"] == 2
            assert metadata["selected_content_origin"]["coordinate_system"] == "approved_selected_file"
            assert metadata["selected_content_origin"]["sha256"] == media_compliance.sha256_file(clip)
            assert metadata["selected_content_origin"]["original_metadata"] == jobs_before[ids.index(item["target_id"])]["artifacts"][0]["metadata"]
            assert metadata["width"] / metadata["height"] == pytest.approx(640 / 360)
        assert all(part["message"] == jobs_before[ids.index(item["target_id"])]["message"] for part in item["prepared_payloads"])
    result = apply(database, json.loads(json.dumps(plan)))
    assert result["applied_target_ids"] == ids and len(result["queued_target_ids"]) == 6 and result["errors"] == []
    installed = rows(path)
    for row in installed:
        cp = json.loads(row["checkpoint_json"])
        assert row["status"] == "pending" and row["attempts"] == 8
        assert cp["provider_budget_baseline"] == 1
        assert cp["publish_routes"]["native"]["attempts"] == 1
        assert cp["publish_routes"]["native"]["max_attempts"] == 3
        assert cp["prerequisites"]["attempts"] == 8
        assert cp["previous_failure"]["checkpoint"] == json.loads(before[0 if row["id"] == ids[0] or cp.get("recovery_origin_target_id") == ids[0] else 1]["checkpoint_json"])
        runtime = {"_delivery": cp}
        assert delivery.begin_route_attempt(runtime, "native", max_attempts=99) == 2
        assert delivery.begin_route_attempt(runtime, "native", max_attempts=99) == 3
        with pytest.raises(delivery.RouteExhausted):
            delivery.begin_route_attempt(runtime, "native", max_attempts=99)
    assert [jobs.get_job(row["job_id"], db_path=path).payload for row in before] == jobs_before
    assert apply(database, plan)["applied_target_ids"] == []
    assert len(rows(path)) == 6


def test_no_json_flag_can_authorize_application(database, tmp_path):
    path, ids, _ = database
    before = rows(path)
    plan = prepare(database, tmp_path)
    for item in plan:
        item["review_resolved_prerequisites"] = True
    with pytest.raises(ValueError, match="Explicit"):
        reviewed.apply_plan(plan, db_path=path, target_ids=ids)
    with pytest.raises(ValueError, match="Explicit"):
        reviewed.apply_plan(plan, db_path=path, review_resolved_prerequisites=True)
    assert rows(path) == before


def test_ordinary_failed_retry_and_preprocessing_remain_failed_only(database, tmp_path):
    path, ids, _ = database
    with pytest.raises(ordinary.recovery.RecoveryRefused, match="failed"):
        ordinary.prepare_plan(db_path=path, target_ids=ids, out_dir=tmp_path, now=NOW)
    with pytest.raises(ValueError, match="failed"):
        jobs.reschedule_failed_target(ids[0], (NOW + timedelta(hours=1)).isoformat(),
            expected_fingerprint="a" * 64, revalidate=lambda _: "a" * 64, db_path=path)
    assert all(row["status"] == "blocked" for row in rows(path))


def test_incomplete_sibling_selection_refuses_before_encode(database, tmp_path, monkeypatch):
    path, ids, _ = database
    encoder = Mock(side_effect=AssertionError("No partial generation"))
    monkeypatch.setattr(guard, "prepare_payloads", encoder)
    with pytest.raises(ordinary.recovery.RecoveryRefused, match="siblings"):
        reviewed.prepare_plan(db_path=path, target_ids=ids[1:], review_resolved_prerequisites=True, out_dir=tmp_path)
    encoder.assert_not_called()


@pytest.mark.parametrize("change", ["caption", "source", "output", "config", "slot", "nested_receipt", "lease", "budget"])
def test_fresh_change_rolls_back_whole_original_group(database, tmp_path, change):
    path, ids, _ = database
    plan = prepare(database, tmp_path)
    before_count = len(rows(path))
    if change == "caption":
        plan[1]["prepared_payloads"][0]["message"] = "Not approved"
    elif change == "source":
        # Use an owned copy so the module-scoped original fixtures stay intact.
        source = Path(plan[1]["original_media"][0]["local_path"])
        plan[1]["original_media"][0]["sha256"] = "a" * 64
        assert source.is_file()
    elif change == "output":
        Path(plan[1]["prepared_payloads"][1]["artifacts"][0]["local_path"]).write_bytes(b"tampered")
    elif change == "slot":
        plan[1]["part_schedule_ats"][0] = NOW.isoformat()
    else:
        with closing(sqlite3.connect(path)) as conn, conn:
            if change == "config":
                conn.execute("UPDATE accounts SET config_json=?", (json.dumps({"threadUserId": "different"}),))
            elif change == "lease":
                conn.execute("UPDATE publish_job_targets SET lease_token='new-lease' WHERE id=?", (ids[1],))
            else:
                cp = json.loads(rows(path)[1]["checkpoint_json"])
                if change == "nested_receipt":
                    cp["old_history"] = {"provider_receipt": {"id": "retain"}}
                else:
                    cp["publish_routes"]["native"]["attempts"] = 3
                conn.execute("UPDATE publish_job_targets SET checkpoint_json=? WHERE id=?", (json.dumps(cp), ids[1]))
    retained = rows(path)
    result = apply(database, plan)
    assert result["applied_target_ids"] == [] and result["errors"]
    assert rows(path) == retained and len(rows(path)) == before_count


@pytest.mark.parametrize("status,category", [("cancelled", "prerequisite_integrity"), ("needs_review", "prerequisite_integrity"),
    ("awaiting_delivery", "prerequisite_integrity"), ("running", "prerequisite_integrity"),
    ("blocked", "prerequisite_permission"), ("blocked", "prerequisite_configuration"), ("blocked", "unknown")])
def test_review_never_broadens_unsafe_states(database, tmp_path, status, category):
    path, ids, _ = database
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE publish_job_targets SET status=?,error_category=? WHERE id=?", (status, category, ids[1]))
    before = rows(path)
    with pytest.raises(ValueError):
        prepare(database, tmp_path)
    assert rows(path) == before


def test_legacy_provenance_is_not_accepted_by_unreviewed_installer(database, tmp_path):
    path, ids, _ = database
    plan = prepare(database, tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        target = conn.execute("SELECT * FROM publish_job_targets WHERE id=?", (ids[0],)).fetchone()
        with pytest.raises(ValueError, match="explicit frozen review"):
            prepared_recovery.validate_parts(conn, target, plan[0]["prepared_payloads"], plan[0]["part_schedule_ats"])


def test_submitted_predecessor_does_not_release_tail_and_confirmed_chain_does(database, tmp_path, monkeypatch):
    path, ids, _ = database
    plan = prepare(database, tmp_path)
    order = apply(database, plan)["queued_target_ids"]
    assert order[0] == ids[0] and order[3] == ids[1]
    late = max(jobs._utc_datetime(value) for item in plan for value in item["part_schedule_ats"]) + timedelta(minutes=1)
    monkeypatch.setattr(jobs, "_now_iso", lambda: late.isoformat())
    def receipt(target_id, state, status):
        with closing(sqlite3.connect(path)) as conn, conn:
            row = next(row for row in rows(path) if row["id"] == target_id)
            cp = json.loads(row["checkpoint_json"])
            context = {"_delivery": cp}
            if state == "delivered":
                delivery.confirmed(context, provider="threads", receipt_id=f"receipt-{target_id}")
            else:
                delivery.accepted(context, provider="threads", receipt_id=f"receipt-{target_id}")
            conn.execute("UPDATE publish_job_targets SET status=?,delivery_state=?,checkpoint_json=? WHERE id=?",
                         (status, state, json.dumps(context["_delivery"]), target_id))
    receipt(order[0], "submitted", "awaiting_delivery")
    with jobs._connect(path) as conn:
        eligible = list(jobs._eligible_targets(conn, late.isoformat()))
        assert [row["id"] for row in eligible] == [order[0]]  # Poll, never the tail.
    for index, target_id in enumerate(order[:-1]):
        receipt(target_id, "delivered", "succeeded")
        with jobs._connect(path) as conn:
            eligible = list(jobs._eligible_targets(conn, late.isoformat()))
            assert [row["id"] for row in eligible] == [order[index + 1]]


def test_readonly_validator_is_enforced_without_partial_group_writes(database, tmp_path):
    path, ids, _ = database
    plan = prepare(database, tmp_path)
    before = rows(path)
    def writes(conn):
        conn.execute("UPDATE publish_job_targets SET attempts=999 WHERE id=?", (ids[0],))
        return reviewed._group_fingerprint(plan)
    with pytest.raises(ValueError, match="read-only"):
        jobs.recover_reviewed_group(plan, reviewed_target_ids=ids,
            expected_fingerprint=reviewed._group_fingerprint(plan), revalidate=writes, db_path=path)
    assert rows(path) == before


def test_malformed_plan_is_rejected_before_any_write(database, tmp_path):
    path, ids, _ = database
    plan = prepare(database, tmp_path)
    del plan[1]["part_schedule_ats"]
    before = rows(path)
    with pytest.raises(ValueError, match="complete prepared"):
        apply(database, plan)
    assert rows(path) == before


@pytest.mark.parametrize("receipt,delivered", [
    ({"provider": "sociamonials", "id": "verified-post", "status": "delivered"}, True),
    ({"provider": "sociamonials", "id": "post", "status": "processing"}, False),
    ({"provider": "sociamonials", "id": "post", "status": "failed"}, False),
    ({"provider": "sociamonials", "id": "post", "state": "confirmed"}, False),
    ({"provider": "sociamonials", "id": True, "status": "delivered"}, False),
    ({"provider": "sociamonials", "id": "confirmed", "status": "delivered"}, False),
    ({"provider": "sociamonials", "id": "", "status": "delivered"}, False),
    ({"provider": "", "id": "post", "status": "delivered"}, False),
])
def test_only_maintained_valid_delivered_receipts_release_a_predecessor(receipt, delivered):
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE publish_job_targets(id INTEGER,status TEXT,delivery_state TEXT,"
                     "workspace_id TEXT,account_ref TEXT,checkpoint_json TEXT)")
        conn.execute("INSERT INTO publish_job_targets VALUES (1,'succeeded','delivered','same','account:77',?)",
                     (json.dumps({"provider_receipt": receipt}),))
        row = {"id": 2, "status": "pending", "workspace_id": "same", "account_ref": "account:77",
               "checkpoint_json": json.dumps({"recovery_predecessor_target_id": 1})}
        assert reviewed_recovery.predecessor_confirmed(conn, row) is delivered


@pytest.mark.parametrize("tamper", [False, True])
def test_reviewed_missing_payload_sha_binds_only_verified_exact_artifact_rows(database, clips, tmp_path, tamper):
    path, ids, _ = database
    before_payloads = []
    with closing(sqlite3.connect(path)) as conn, conn:
        for target_id in ids:
            target = next(row for row in rows(path) if row["id"] == target_id)
            checkpoint = json.loads(target["checkpoint_json"])
            payload = checkpoint["payload"]
            artifact = payload["artifacts"][0]
            recorded = {**artifact["metadata"], "sha256": artifact.pop("sha256"), "size_bytes": artifact["size_bytes"]}
            cursor = conn.execute("INSERT INTO campaign_artifacts(campaign_id,source_file_record_id,artifact_kind,local_path,metadata_json) "
                "VALUES (123,901,'local',?,?)", (artifact["local_path"], json.dumps(recorded)))
            artifact["id"] = cursor.lastrowid
            before_payloads.append(json.loads(json.dumps(payload)))
            conn.execute("UPDATE publish_jobs SET payload_json=? WHERE id=?", (json.dumps(payload), target["job_id"]))
            conn.execute("UPDATE publish_job_targets SET checkpoint_json=? WHERE id=?", (json.dumps(checkpoint), target_id))
    plan = prepare(database, tmp_path)
    assert all("sha256" not in value["artifacts"][0] for value in before_payloads)
    if tamper:
        with closing(sqlite3.connect(path)) as conn, conn:
            artifact_id = before_payloads[0]["artifacts"][0]["id"]
            metadata = json.loads(conn.execute("SELECT metadata_json FROM campaign_artifacts WHERE id=?", (artifact_id,)).fetchone()[0])
            metadata["sha256"] = "a" * 64
            conn.execute("UPDATE campaign_artifacts SET metadata_json=? WHERE id=?", (json.dumps(metadata), artifact_id))
        retained = rows(path)
        assert apply(database, plan)["applied_target_ids"] == []
        assert rows(path) == retained
    else:
        assert apply(database, plan)["applied_target_ids"] == ids
    for target_id, payload in zip(ids, before_payloads):
        assert jobs.get_job(jobs.get_target(target_id, db_path=path).job_id, db_path=path).payload == payload


def test_reviewed_binding_never_borrows_raw_source_checksum(tmp_path):
    artifact = {"id": 9, "source_file_record_id": 77, "artifact_kind": "remote_upload", "local_path": str(tmp_path / "derivative.mp4"), "metadata": {"role": "video"}}
    payload = {"artifacts": [artifact]}
    raw = {"id": 77, "metadata_json": json.dumps({"sha256": "a" * 64, "size_bytes": 999})}
    with pytest.raises(ValueError, match="SHA256"):
        reviewed_recovery.bind_media_payload(payload, [raw])
    assert payload["artifacts"][0] == artifact
