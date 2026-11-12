"""Frozen pending preprocessing contracts. All databases/media/providers are isolated."""
from __future__ import annotations

import copy
import gc
import json
import shutil
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

from db import createTable
from myUtils import jobs, media_compliance, profiles
from scripts import preprocess_failed_targets as prep

NOW = datetime(2035, 1, 2, 12)
SLOT = "2035-01-02T14:00:00"


@pytest.fixture(scope="module")
def schema(tmp_path_factory):
    path = tmp_path_factory.mktemp("pending-schema") / "schema.db"
    createTable.bootstrap(path)
    profile = profiles.create_profile("Pending fixture", db_path=path)
    account = profiles.add_account(profile.id, platform="threads", account_name="fixture", auth_type="oauth",
                                   config={"threadUserId": "pending-fixture"}, db_path=path)
    gc.collect()
    return path, profile, account


@pytest.fixture
def database(tmp_path, schema):
    source, profile, account = schema
    path = tmp_path / "pending # 測試.db"
    shutil.copyfile(source, path)
    return path, profile, account


@pytest.fixture(autouse=True)
def isolated_policy(monkeypatch):
    for key in ("SAU_THREADS_ENABLED", "SAU_SOCIAMONIALS_FALLBACK", "SAU_X_DIRECT_PUBLISH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(jobs, "_now_iso", lambda: NOW.isoformat())
    monkeypatch.setattr(prep.recovery.publish_orchestrator, "MIN_GAP_MINUTES", 30)
    monkeypatch.setattr(prep.recovery.publish_orchestrator, "MAX_POSTS_PER_ACCOUNT_PER_DAY", 3)
    monkeypatch.setattr(prep.recovery.publish_orchestrator, "_operator_timezone", lambda: UTC)


def sql(path, statement, params=()):
    with closing(sqlite3.connect(path)) as conn, conn:
        return conn.execute(statement, params).lastrowid


def row(path, table, row_id):
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        found = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
        return dict(found) if found is not None else None


def update(path, table, row_id, **fields):
    sql(path, f"UPDATE {table} SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
        (*fields.values(), row_id))


def dump(path):
    with closing(sqlite3.connect(path)) as conn:
        return "\n".join(conn.iterdump())


def pending_job(database, tmp_path, *, name="selected", schedule=SLOT, payload_patch=None):
    path, profile, account = database
    source = tmp_path / f"{name}_sfw.png"
    Image.new("RGB", (400, 400), "blue").save(source)
    payload = {"message": f"Approved {name} 原稿", "draft": {"privacy": "keep"},
               "artifacts": [{"local_path": str(source), "metadata": {"alt_text": "Keep"}}]}
    payload.update(payload_patch or {})
    job = jobs.enqueue_job(jobs.JobSpec(platform="threads", profile_id=profile.id, payload=payload,
        targets=[(f"account:{account.id}", str(source), schedule)], idempotency_key=f"pending-fixture:{name}"), db_path=path)
    return job, jobs.list_targets(job.id, db_path=path)[0]


@pytest.fixture
def invalid_pipeline(tmp_path, monkeypatch):
    """Mock the encoder, not the DB, fingerprints, complete-set validation or allocator."""
    def prepare(payload, *, allow_reprepare=False, out_dir=None, **_kwargs):
        if not allow_reprepare:
            return [copy.deepcopy(payload)]
        root = out_dir or tmp_path / "parts"
        root.mkdir(parents=True, exist_ok=True)
        parts = []
        for index in range(1, 4):
            path = root / f"part-{index}_sfw.png"
            Image.new("RGB", (400, 400), (index * 30, 40, 50)).save(path)
            part = copy.deepcopy(payload)
            part.update(_preserveApprovedText=True, preparedPost={"index": index, "count": 3})
            part["artifacts"] = [{"local_path": str(path), "artifact_kind": "prepared_image",
                "metadata": {"sha256": media_compliance.sha256_file(path), "alt_text": f"Ordered {index}"}}]
            parts.append(part)
        return parts
    encoder = Mock(side_effect=prepare)
    monkeypatch.setattr(prep.publish_media_guard, "prepare_payloads", encoder)
    monkeypatch.setattr(prep, "_validate_existing_pending",
        Mock(side_effect=media_compliance.MediaPreparationRequired("Selected media needs preprocessing")))
    return encoder


def prepare(database, target, tmp_path, **kwargs):
    return prep.prepare_plan(db_path=database[0], target_ids=[target.id], now=NOW, out_dir=tmp_path / "parts",
                             include_unstarted_pending=True, **kwargs)


def apply(database, target, plan):
    return prep.apply_plan(plan, db_path=database[0], include_unstarted_pending=True, target_ids=[target.id])


def assert_apply_refused(database, target, plan):
    before = dump(database[0])
    result = apply(database, target, plan)
    assert result["applied"] == result["queued_parts"] == 0 and result["errors"]
    assert dump(database[0]) == before


def test_default_pending_and_plain_failed_reset_refuse(database, tmp_path, invalid_pipeline):
    path, *_ = database
    _job, target = pending_job(database, tmp_path)
    before = dump(path)
    with pytest.raises(prep.recovery.RecoveryRefused, match="no longer failed"):
        prep.prepare_plan(db_path=path, target_ids=[target.id], now=NOW, out_dir=tmp_path / "parts")
    with pytest.raises(ValueError, match="no longer failed"):
        jobs.reschedule_failed_target(target.id, SLOT, expected_fingerprint="0" * 64,
            revalidate=lambda conn: pytest.fail("default must not validate pending"), db_path=path)
    invalid_pipeline.assert_not_called()
    assert dump(path) == before


@pytest.mark.parametrize("selection", [None, []])
def test_api_opt_in_requires_nonempty_explicit_selection(database, selection):
    with pytest.raises(ValueError, match="explicit nonempty frozen"):
        prep.prepare_plan(db_path=database[0], target_ids=selection, include_unstarted_pending=True)


@pytest.mark.parametrize("mode", [[], ["--prepare"], ["--apply"], ["--apply", "--plan-file", "unused.json"]])
def test_cli_opt_in_never_scans_backlog_without_selection(monkeypatch, mode):
    connect = Mock(side_effect=AssertionError("No automatic pending scan"))
    monkeypatch.setattr(prep.recovery, "_connect", connect)
    with pytest.raises(SystemExit) as exc:
        prep.main(["--include-unstarted-pending", *mode])
    assert exc.value.code == 2
    connect.assert_not_called()


@pytest.mark.parametrize("mode", ["--prepare", "--apply"])
def test_cli_empty_selection_file_refuses(database, tmp_path, mode):
    selection = tmp_path / "selection.json"
    selection.write_text('{"target_ids":[]}', encoding="utf-8")
    before = dump(database[0])
    with pytest.raises(SystemExit) as exc:
        prep.main(["--db-path", str(database[0]), "--include-unstarted-pending", mode,
                   "--selection-file", str(selection)])
    assert exc.value.code == 2 and dump(database[0]) == before


@pytest.mark.parametrize("prepared_media", [False, True])
def test_valid_pending_is_byte_for_byte_unchanged(database, tmp_path, monkeypatch, prepared_media):
    path, *_ = database
    job, target = pending_job(database, tmp_path)
    if prepared_media:
        payload = jobs.get_job(job.id, db_path=path).payload
        payload["artifacts"][0]["metadata"]["sha256"] = media_compliance.sha256_file(target.file_ref)
        update(path, "publish_jobs", job.id, payload_json=json.dumps(payload))
    before = dump(path)
    encoder = Mock(side_effect=AssertionError("Valid pending must not encode"))
    monkeypatch.setattr(prep.publish_media_guard, "prepare_payloads", encoder)
    monkeypatch.setattr(prep.recovery, "plan_recovery", Mock(side_effect=AssertionError("Valid pending must not allocate")))
    plan = prepare(database, target, tmp_path)
    assert plan == [{"_unchanged_pending": [target.id]}]
    assert apply(database, target, plan) == {"applied": 0, "queued_parts": 0, "errors": []}
    assert dump(path) == before
    encoder.assert_not_called()


@pytest.mark.parametrize("options", [{}, {"include_unstarted_pending": True},
    {"include_unstarted_pending": True, "target_ids": []},
    {"include_unstarted_pending": True, "target_ids": [999999]}])
def test_saved_pending_api_requires_same_opt_in_and_selection(database, tmp_path, invalid_pipeline, options):
    _job, target = pending_job(database, tmp_path)
    plan = prepare(database, target, tmp_path)
    before = dump(database[0])
    with pytest.raises(ValueError, match="opt-in|frozen"):
        prep.apply_plan(plan, db_path=database[0], **options)
    assert dump(database[0]) == before


TARGET_UNSAFE = [
    ("attempts", value) for value in (-1, 1, 3, 0.5, "invalid")
] + [
    (field, value) for field in ("started_at", "finished_at") for value in (NOW.isoformat(), "")
] + [
    (field, value) for field in ("lease_owner", "lease_token", "lease_expires_at")
    for value in ("fixture-lease", "", "2000-01-01T00:00:00")
] + [
    ("checkpoint_json", value) for value in (
        '{"prepared":true}', '{"payload":{}}', '{"recovery_generation":"old"}',
        '{"provider_receipt":{"id":"keep"}}', '{"provider_receipt":{}}', '{"provider_receipt":null}',
        '{"submission_started":false}', '{"provider_operations":{"threads":{"plan":["post"],"items":{}}}}',
        '{"previous_failure":{"attempts":2}}', '{"history":["old"]}', "broken", "", "[]", "null",
        '{"x":NaN}', '{"prepared":true,"prepared":false}')
] + [
    ("delivery_state", value) for value in ("failed", "delivered", "awaiting_delivery", "unknown")
] + [
    ("next_attempt_at", value) for value in (NOW.isoformat(), "")
] + [("last_error", "prior error"), ("error_category", "transient")] + [
    ("status", value) for value in ("running", "retrying", "cancelled", "succeeded", "blocked", "awaiting_delivery", "needs_review", "invalid")
]


@pytest.mark.parametrize("field,value", TARGET_UNSAFE)
def test_unsafe_target_state_refuses_prepare_and_apply(database, tmp_path, invalid_pipeline, field, value):
    path, *_ = database
    _job, target = pending_job(database, tmp_path)
    plan = prepare(database, target, tmp_path)
    update(path, "publish_job_targets", target.id, **{field: value})
    before, calls = dump(path), invalid_pipeline.call_count
    with pytest.raises(ValueError):
        prepare(database, target, tmp_path)
    assert invalid_pipeline.call_count == calls and dump(path) == before
    assert_apply_refused(database, target, plan)


@pytest.mark.parametrize("payload_patch", [
    {"_delivery": {}}, {"provider_receipt": {}}, {"recovery_generation": "old"},
    {"recoveryOriginTargetId": 7}, {"delivery_history": ["old"]}, {"checkpoint": {"prepared": True}},
    {"draft": {"provider_operations": {"threads": {"plan": ["post"], "items": {}}}}},
    {"previous_failure": {"attempts": 1}}, {"telegramCompletedByDelivery": {"one": ["receipt"]}},
])
def test_any_payload_checkpoint_recovery_or_receipt_history_refuses(database, tmp_path, invalid_pipeline, payload_patch):
    path, *_ = database
    job, target = pending_job(database, tmp_path)
    plan = prepare(database, target, tmp_path)
    payload = jobs.get_job(job.id, db_path=path).payload
    payload.update(payload_patch)
    update(path, "publish_jobs", job.id, payload_json=json.dumps(payload))
    before, calls = dump(path), invalid_pipeline.call_count
    with pytest.raises(ValueError, match="history|evidence"):
        prepare(database, target, tmp_path)
    assert invalid_pipeline.call_count == calls and dump(path) == before
    assert_apply_refused(database, target, plan)


@pytest.mark.parametrize("table,field,value", [
    ("accounts", "enabled", 0), ("accounts", "enabled", 2), ("accounts", "profile_id", 999999),
    ("accounts", "workspace_id", "other"), ("accounts", "platform", "instagram"),
    ("accounts", "config_json", "broken"), ("accounts", "config_json", '{"_needsReconnect":true}'),
    ("publish_job_targets", "workspace_id", "other"), ("publish_job_targets", "account_ref", "cookie:1"),
    ("publish_jobs", "workspace_id", "other"), ("publish_jobs", "profile_id", None),
    ("publish_jobs", "status", "cancelled"), ("publish_jobs", "status", "succeeded"),
    ("publish_jobs", "status", "failed"), ("publish_jobs", "status", "invalid"),
    ("profiles", "workspace_id", "other"), ("profiles", "settings_json", "[]"),
])
def test_account_ownership_and_cancelled_intent_refuse_prepare_and_apply(database, tmp_path, invalid_pipeline, table, field, value):
    path, profile, account = database
    job, target = pending_job(database, tmp_path)
    plan = prepare(database, target, tmp_path)
    ids = {"accounts": account.id, "profiles": profile.id, "publish_jobs": job.id, "publish_job_targets": target.id}
    update(path, table, ids[table], **{field: value})
    before, calls = dump(path), invalid_pipeline.call_count
    with pytest.raises(ValueError):
        prepare(database, target, tmp_path)
    assert invalid_pipeline.call_count == calls and dump(path) == before
    assert_apply_refused(database, target, plan)


@pytest.mark.parametrize("table", ["accounts", "profiles", "publish_jobs", "publish_job_targets"])
def test_deleted_state_refuses_prepare_and_apply(database, tmp_path, invalid_pipeline, table):
    path, profile, account = database
    job, target = pending_job(database, tmp_path)
    plan = prepare(database, target, tmp_path)
    ids = {"accounts": account.id, "profiles": profile.id, "publish_jobs": job.id, "publish_job_targets": target.id}
    sql(path, f"DELETE FROM {table} WHERE id=?", (ids[table],))
    with pytest.raises(ValueError):
        prepare(database, target, tmp_path)
    assert_apply_refused(database, target, plan)


def campaign_job(database, tmp_path):
    path, profile, _account = database
    job, target = pending_job(database, tmp_path)
    group = sql(path, "INSERT INTO media_groups(name) VALUES ('pending fixture')")
    campaign = sql(path, "INSERT INTO campaigns(profile_id,media_group_id,status) VALUES (?,?,'publishing')", (profile.id, group))
    post = sql(path, "INSERT INTO campaign_posts(campaign_id,platform,status,last_published_job_id) VALUES (?,'threads','queued',?)",
               (campaign, job.id))
    payload = jobs.get_job(job.id, db_path=path).payload
    payload.update(campaignId=campaign, campaignPostId=post)
    update(path, "publish_jobs", job.id, payload_json=json.dumps(payload))
    update(path, "publish_job_targets", target.id, file_ref=f"campaign_post:{post}")
    return job, target, campaign, post


@pytest.mark.parametrize("table,field,value", [
    ("campaign_posts", "last_published_job_id", None), ("campaign_posts", "last_published_job_id", 999999),
    ("campaign_posts", "status", "cancelled"), ("campaign_posts", "status", "published"),
    ("campaign_posts", "status", "draft"), ("campaign_posts", "status", "needs_review"),
    ("campaign_posts", "workspace_id", "other"), ("campaigns", "status", "cancelled"),
    ("campaigns", "status", "published"), ("campaigns", "status", "preparing"),
    ("campaigns", "profile_id", 999999),
])
def test_superseded_or_cancelled_campaign_refuses_both_phases(database, tmp_path, invalid_pipeline, table, field, value):
    path, *_ = database
    _job, target, campaign, post = campaign_job(database, tmp_path)
    plan = prepare(database, target, tmp_path)
    update(path, table, post if table == "campaign_posts" else campaign, **{field: value})
    with pytest.raises(ValueError):
        prepare(database, target, tmp_path)
    assert_apply_refused(database, target, plan)


@pytest.mark.parametrize("other_status,checkpoint", [
    ("pending", {}), ("running", {}), ("succeeded", {}), ("awaiting_delivery", {}), ("needs_review", {}),
    ("failed", {"provider_receipt": {"id": "keep"}}), ("cancelled", {"submission_started": True}),
])
def test_new_delivered_or_inflight_duplicate_is_rechecked(database, tmp_path, invalid_pipeline, other_status, checkpoint):
    path, profile, account = database
    job, target = pending_job(database, tmp_path)
    plan = prepare(database, target, tmp_path)
    alias = profiles.add_account(profile.id, platform="threads", account_name="alias", auth_type="oauth",
                                 config=account.config, db_path=path)
    other = jobs.enqueue_job(jobs.JobSpec(platform="threads", profile_id=profile.id,
        payload=jobs.get_job(job.id, db_path=path).payload, idempotency_key="new-duplicate",
        targets=[(f"account:{alias.id}", target.file_ref, SLOT)]), db_path=path)
    other_target = jobs.list_targets(other.id, db_path=path)[0]
    update(path, "publish_job_targets", other_target.id, status=other_status, checkpoint_json=json.dumps(checkpoint))
    with pytest.raises(prep.recovery.RecoveryRefused, match="duplicate"):
        prepare(database, target, tmp_path)
    assert_apply_refused(database, target, plan)


@pytest.mark.parametrize("table,field,value", [
    ("publish_job_targets", "schedule_at", "2035-01-04T14:00:00"),
    ("publish_job_targets", "file_ref", "changed.png"), ("publish_job_targets", "status", "failed"),
    ("publish_jobs", "payload_json", '{"message":"changed approved caption"}'),
    ("publish_jobs", "intent_hash", "changed-intent"), ("profiles", "settings_json", '{"language":"zh"}'),
    ("accounts", "account_name", "renamed"),
    ("accounts", "config_json", '{"threadUserId":"pending-fixture","accessToken":"rotated"}'),
])
def test_changed_original_fingerprint_refuses_stale_plan(database, tmp_path, invalid_pipeline, table, field, value):
    path, profile, account = database
    job, target = pending_job(database, tmp_path)
    plan = prepare(database, target, tmp_path)
    ids = {"accounts": account.id, "profiles": profile.id, "publish_jobs": job.id, "publish_job_targets": target.id}
    update(path, table, ids[table], **{field: value})
    assert_apply_refused(database, target, plan)


@pytest.mark.parametrize("during", ["encode", "slot-lock"])
def test_started_during_preparation_or_before_write_refuses(database, tmp_path, invalid_pipeline, monkeypatch, during):
    path, *_ = database
    _job, target = pending_job(database, tmp_path)
    if during == "encode":
        original = invalid_pipeline.side_effect
        def raced(payload, **kwargs):
            result = original(payload, **kwargs)
            if kwargs.get("allow_reprepare"):
                update(path, "publish_job_targets", target.id, started_at=NOW.isoformat())
            return result
        invalid_pipeline.side_effect = raced
        with pytest.raises(prep.recovery.RecoveryRefused, match="started"):
            prepare(database, target, tmp_path)
        assert jobs.get_target(target.id, db_path=path).checkpoint == {}
    else:
        plan = prepare(database, target, tmp_path)
        lock = prep.recovery.publish_orchestrator.slot_reservation_lock
        @contextmanager
        def raced_lock(db_path):
            with lock(db_path):
                update(path, "publish_job_targets", target.id, lease_token="new-claim")
                yield
        monkeypatch.setattr(prep.recovery.publish_orchestrator, "slot_reservation_lock", raced_lock)
        assert apply(database, target, plan)["applied"] == 0
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM publish_job_targets").fetchone()[0] == 1


@pytest.mark.parametrize("tamper", ["output", "source", "caption", "tail", "fingerprint"])
def test_changed_media_incomplete_parts_or_stale_plan_never_partially_queue(database, tmp_path, invalid_pipeline, tamper):
    _job, target = pending_job(database, tmp_path)
    plan = prepare(database, target, tmp_path)
    if tamper == "output":
        Path(plan[0]["prepared_payloads"][1]["artifacts"][0]["local_path"]).write_bytes(b"changed")
    elif tamper == "source":
        Path(target.file_ref).write_bytes(b"changed source")
    elif tamper == "caption":
        plan[0]["prepared_payloads"][0]["message"] = "Not approved"
    elif tamper == "tail":
        plan[0]["prepared_payloads"].pop()
        plan[0]["part_schedule_ats"].pop()
    else:
        plan[0]["expected_fingerprint"] = "0" * 64
    assert_apply_refused(database, target, plan)


def test_reapply_preserves_first_generation_and_every_tail(database, tmp_path, invalid_pipeline):
    path, *_ = database
    job, target = pending_job(database, tmp_path)
    original = jobs.get_job(job.id, db_path=path).payload
    plan = json.loads(json.dumps(prepare(database, target, tmp_path)))
    assert apply(database, target, plan) == {"applied": 1, "queued_parts": 3, "errors": []}
    assert jobs.get_job(job.id, db_path=path).payload == original
    checkpoint = jobs.get_target(target.id, db_path=path).checkpoint
    assert checkpoint["original_pending"]["state_identity"] == plan[0]["original_state_identity"]
    assert checkpoint["include_unstarted_pending"] is True and "previous_failure" not in checkpoint
    assert_apply_refused(database, target, plan)


def test_recount_failure_rolls_back_all_pending_parts(database, tmp_path, invalid_pipeline, monkeypatch):
    _job, target = pending_job(database, tmp_path)
    plan = prepare(database, target, tmp_path)
    monkeypatch.setattr(jobs, "_recount_job", Mock(side_effect=RuntimeError("rollback fixture")))
    assert_apply_refused(database, target, plan)


def test_pending_apply_hashes_only_under_current_reservation_and_write_lock(database, tmp_path, invalid_pipeline, monkeypatch):
    path, *_ = database
    _job, target = pending_job(database, tmp_path)
    plan = prepare(database, target, tmp_path)
    original = prep._revalidate_prepared
    def locked(conn, item):
        assert conn.in_transaction
        with closing(sqlite3.connect(path, timeout=0)) as rival, pytest.raises(sqlite3.OperationalError, match="locked"):
            rival.execute("BEGIN IMMEDIATE")
        return original(conn, item)
    monkeypatch.setattr(prep, "_revalidate_prepared", locked)
    monkeypatch.setattr(prep.publish_media_guard, "prepare_payloads", Mock(side_effect=AssertionError("No encode on apply")))
    monkeypatch.setattr(media_compliance.media_prep, "probe", Mock(side_effect=AssertionError("No probe on apply")))
    assert apply(database, target, plan)["queued_parts"] == 3


def test_cli_manifest_pins_opt_in_original_identity_and_selection(database, tmp_path, invalid_pipeline, capsys):
    path, *_ = database
    _job, target = pending_job(database, tmp_path)
    selection, manifest = tmp_path / "selection.json", tmp_path / "plan.json"
    selection.write_text(json.dumps({"target_ids": [target.id]}), encoding="utf-8")
    flags = ["--db-path", str(path), "--include-unstarted-pending", "--selection-file", str(selection), "--plan-file", str(manifest)]
    before = dump(path)
    assert prep.main([*flags, "--prepare", "--out-dir", str(tmp_path / "parts")]) == 0
    saved = json.loads(manifest.read_text(encoding="utf-8"))
    assert saved["include_unstarted_pending"] is True and saved["selected_target_ids"] == [target.id]
    assert saved["plan"][0]["original_status"] == "pending" and len(saved["plan"][0]["original_state_identity"]) == 64
    assert dump(path) == before
    with pytest.raises(SystemExit) as exc:
        prep.main(["--db-path", str(path), "--apply", "--plan-file", str(manifest)])
    assert exc.value.code == 2 and dump(path) == before
    selection.write_text(json.dumps({"target_ids": [target.id, 999999]}), encoding="utf-8")
    with pytest.raises(SystemExit):
        prep.main([*flags, "--apply"])
    assert dump(path) == before
    selection.write_text(json.dumps({"target_ids": [target.id]}), encoding="utf-8")
    assert prep.main([*flags, "--apply"]) == 0
    assert prep.main([*flags, "--apply"]) == 1
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["applied"] == 0


def test_only_exact_replaced_reservation_is_excluded_and_future_slot_retained(database, tmp_path, invalid_pipeline):
    path, profile, account = database
    _job, target = pending_job(database, tmp_path)
    alias = profiles.add_account(profile.id, platform="threads", account_name="alias", auth_type="oauth",
                                 config=account.config, db_path=path)
    other_ids = []
    for index, slot in enumerate(("2035-01-02T12:30:00", "2035-01-02T13:00:00")):
        job = jobs.enqueue_job(jobs.JobSpec(platform="threads", profile_id=profile.id, payload={"message": "Other"},
            targets=[(f"account:{alias.id}", f"other-{index}", slot)]), db_path=path)
        other_ids.append(jobs.list_targets(job.id, db_path=path)[0].id)
    other_rows = [row(path, "publish_job_targets", target_id) for target_id in other_ids]
    plan = prepare(database, target, tmp_path)
    assert plan[0]["slot"] == SLOT and plan[0]["per_day_cap"] == 3
    assert all(slot[:10] == "2035-01-03" for slot in plan[0]["part_schedule_ats"][1:])
    assert apply(database, target, plan)["queued_parts"] == 3
    assert [row(path, "publish_job_targets", target_id) for target_id in other_ids] == other_rows


@pytest.mark.parametrize("status,checkpoint", [
    ("pending", {}), ("succeeded", {"provider_receipt": {"provider": "threads", "id": "delivered", "status": "delivered", "delivered_at": SLOT}}),
    ("awaiting_delivery", {"provider_receipt": {"provider": "threads", "id": "uncertain", "submitted_at": SLOT}}),
    ("failed", {"submission_started": True}), ("cancelled", {"submission_started": True}),
])
def test_equal_time_other_bookings_and_uncertain_history_are_never_removed(database, tmp_path, invalid_pipeline, status, checkpoint):
    path, profile, account = database
    _job, target = pending_job(database, tmp_path)
    other_job = jobs.enqueue_job(jobs.JobSpec(platform="threads", profile_id=profile.id, payload={"message": "Different intent"},
        targets=[(f"account:{account.id}", "unrelated", SLOT)]), db_path=path)
    other = jobs.list_targets(other_job.id, db_path=path)[0]
    update(path, "publish_job_targets", other.id, status=status, started_at=SLOT if status in {"failed", "cancelled"} else None,
           checkpoint_json=json.dumps(checkpoint))
    history = row(path, "publish_job_targets", other.id)
    plan = prepare(database, target, tmp_path)
    assert jobs._utc_datetime(plan[0]["slot"]) != jobs._utc_datetime(SLOT)
    assert apply(database, target, plan)["queued_parts"] == 3
    assert row(path, "publish_job_targets", other.id) == history


def test_new_slot_collision_refuses_whole_pending_generation(database, tmp_path, invalid_pipeline):
    path, profile, account = database
    _job, target = pending_job(database, tmp_path)
    plan = prepare(database, target, tmp_path)
    jobs.enqueue_job(jobs.JobSpec(platform="threads", profile_id=profile.id, payload={"message": "New booking"},
        targets=[(f"account:{account.id}", "new unrelated booking", plan[0]["part_schedule_ats"][1])]), db_path=path)
    assert_apply_refused(database, target, plan)


def test_pending_opt_in_cannot_raise_cap_or_relax_gap(database, tmp_path, invalid_pipeline, monkeypatch):
    _job, target = pending_job(database, tmp_path)
    monkeypatch.setattr(prep.recovery.publish_orchestrator, "MAX_POSTS_PER_ACCOUNT_PER_DAY", 12)
    monkeypatch.setattr(prep.recovery.publish_orchestrator, "MIN_GAP_MINUTES", 1)
    plan = prepare(database, target, tmp_path, per_day_cap=99)
    assert plan[0]["per_day_cap"] == 3
    moments = [jobs._utc_datetime(slot) for slot in plan[0]["part_schedule_ats"]]
    assert all(right - left >= timedelta(minutes=30) for left, right in pairwise(moments))
    assert apply(database, target, plan)["queued_parts"] == 3


@pytest.mark.parametrize("stored_slot", ["2035-01-02T14:00:00.123456", "2035-01-02T22:00:00+08:00"])
def test_compatible_future_timestamp_representation_is_retained(database, tmp_path, invalid_pipeline, stored_slot):
    path, *_ = database
    _job, target = pending_job(database, tmp_path)
    update(path, "publish_job_targets", target.id, schedule_at=stored_slot)
    plan = prepare(database, target, tmp_path)
    assert plan[0]["slot"] == stored_slot
    assert apply(database, target, plan)["queued_parts"] == 3
    assert jobs.get_target(target.id, db_path=path).schedule_at == stored_slot


@pytest.mark.parametrize("field,value", [
    ("include_unstarted_pending", False), ("original_status", "failed"), ("original_status", {}),
    ("original_state_identity", None), ("original_state_identity", "0" * 64),
    ("selected_target_ids", [999999]), ("original_media", []), ("per_day_cap", 4),
])
def test_saved_original_state_and_opt_in_guards_cannot_be_edited(database, tmp_path, invalid_pipeline, field, value):
    _job, target = pending_job(database, tmp_path)
    plan = prepare(database, target, tmp_path)
    plan[0][field] = value
    before = dump(database[0])
    try:
        result = apply(database, target, plan)
    except ValueError:
        pass  # Structural guards abort the entire plan before any writes.
    else:
        assert result["applied"] == result["queued_parts"] == 0 and result["errors"]
    assert dump(database[0]) == before


def test_selected_pending_repair_never_expands_to_unselected_targets(database, tmp_path, invalid_pipeline):
    path, *_ = database
    _job, selected = pending_job(database, tmp_path)
    other_job, other = pending_job(database, tmp_path, name="unselected", schedule="2035-01-03T14:00:00")
    original_target, original_job = row(path, "publish_job_targets", other.id), row(path, "publish_jobs", other_job.id)
    plan = prepare(database, selected, tmp_path)
    assert [item["target_id"] for item in plan] == [selected.id]
    assert apply(database, selected, plan)["queued_parts"] == 3
    assert row(path, "publish_job_targets", other.id) == original_target
    assert row(path, "publish_jobs", other_job.id) == original_job


def test_disabled_route_is_not_overridden_by_pending_opt_in(database, tmp_path, invalid_pipeline, monkeypatch):
    _job, target = pending_job(database, tmp_path)
    plan = prepare(database, target, tmp_path)
    monkeypatch.setenv("SAU_THREADS_ENABLED", "0")
    with pytest.raises(prep.recovery.RecoveryRefused, match="disabled publish route"):
        prepare(database, target, tmp_path)
    assert_apply_refused(database, target, plan)


def test_pending_apply_cannot_interleave_with_finalizer_reservation(database, tmp_path, invalid_pipeline, monkeypatch):
    path, *_ = database
    _job, target = pending_job(database, tmp_path)
    plan = prepare(database, target, tmp_path)
    connect = sqlite3.connect
    def short_wait(db_path, *args, **kwargs):
        if str(db_path).endswith(".slots.lock"):
            kwargs["timeout"] = .01
        return connect(db_path, *args, **kwargs)
    with prep.recovery.publish_orchestrator.slot_reservation_lock(path):
        monkeypatch.setattr(prep.recovery.publish_orchestrator.sqlite3, "connect", short_wait)
        assert_apply_refused(database, target, plan)
    assert apply(database, target, plan)["queued_parts"] == 3


def test_cli_reports_valid_pending_as_unchanged_not_refused(database, tmp_path, monkeypatch, capsys):
    path, *_ = database
    _job, target = pending_job(database, tmp_path)
    encoder = Mock(side_effect=AssertionError("Valid pending cannot be rewritten"))
    monkeypatch.setattr(prep.publish_media_guard, "prepare_payloads", encoder)
    before = dump(path)
    assert prep.main(["--db-path", str(path), "--apply", "--include-unstarted-pending", "--target-id", str(target.id)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["unchanged_pending_target_ids"] == [target.id] and report["refused_targets"] == []
    assert report["applied"] == report["queued_parts"] == 0 and dump(path) == before
    encoder.assert_not_called()
