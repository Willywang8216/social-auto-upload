"""R6/R7: real admission + isolated SQLite, finite prerequisites, no provider writes."""
from __future__ import annotations

import asyncio
import copy
import ipaddress
import hashlib
import json
import socket
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from db.createTable import bootstrap
from myUtils import delivery, generic_delivery, jobs, media_compliance, media_integrity, media_restore
from myUtils import profiles, publish_admission, publish_media_guard as guard, publish_readiness, worker

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 120


def execute_sql(path, sql, args=()):
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(sql, args)


def rows(path, table):
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]


@pytest.fixture(autouse=True)
def no_provider_writes(monkeypatch):
    monkeypatch.setenv("SAU_TESTING", "1")
    monkeypatch.setenv("SAU_X_DIRECT_PUBLISH", "1")
    monkeypatch.setenv("SAU_SOCIAMONIALS_FALLBACK", "0")
    monkeypatch.delenv("SAU_TWITTER_ENABLED", raising=False)
    monkeypatch.delenv("SAU_CONFIG_ENCRYPTION_KEY", raising=False)
    native_connect = socket.socket.connect
    def connect(sock, address):
        if isinstance(address, tuple):
            try:
                if ipaddress.ip_address(address[0]).is_loopback:
                    return native_connect(sock, address)
            except ValueError:
                pass
        raise AssertionError("provider network forbidden")
    # Windows asyncio socketpair needs literal loopback; no external provider
    # endpoint or DNS hostname is allowed by this isolated fixture.
    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    path = tmp_path / "worker.db"
    bootstrap(path)
    monkeypatch.setattr(worker, "BASE_DIR", tmp_path)
    profile = profiles.create_profile("Isolated worker", db_path=path)
    cookie = tmp_path / "cookie.json"
    cookie.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    account = profiles.add_account(profile.id, platform="twitter", account_name="Fixture", auth_type="cookie",
        cookie_path=str(cookie), config={"twitterAuthType": "cookie", "twitterUserId": "1001"}, db_path=path)
    context = publish_readiness._context(account, profile.settings)
    publish_readiness.record_destination_readiness(account, profile.settings, provider="direct", db_path=path,
        proof={"verified": True, "live_check": True, "source": "provider_verification", "requires_approval": False,
               "config_fingerprint": context["fingerprint"], "identity": context["identity"],
               "permissions": {"posts.create": True}, "capabilities": ["video", "image", "carousel", "text"],
               "limits": {"video_max_seconds": 140}, "verified_at": datetime.now(timezone.utc).isoformat()})
    raw = tmp_path / "raw.mp4"
    raw.write_bytes(MP4)
    with closing(sqlite3.connect(path)) as conn, conn:
        fid = conn.execute("INSERT INTO file_records (filename,filesize,file_path,sha256,size_bytes) VALUES (?,?,?,?,?)",
            (raw.name, .001, str(raw), hashlib.sha256(MP4).hexdigest(), len(MP4))).lastrowid
    state = {"path": path, "account": account, "profile": profile, "raw": raw, "fid": fid,
             "needs_prepare": True, "parts": 4, "encodes": 0}
    meta = {"duration": 400.0, "width": 640, "height": 360, "fps": 30.0}
    monkeypatch.setattr(media_compliance, "_source_metadata", lambda _path: meta)
    monkeypatch.setattr(media_compliance, "decode_video", lambda *a, **k: None)
    def validate(path, **kwargs):
        if Path(path) == raw and state["needs_prepare"]:
            raise media_compliance.MediaPreparationRequired("exact source requires all ordered parts")
        return meta
    monkeypatch.setattr(media_compliance, "validate_video", validate)
    def encode(src, out_dir, *, platform, limits, provenance, **kwargs):
        state["encodes"] += 1
        root = Path(out_dir) / f"fixture-generation-{state['encodes']}"
        root.mkdir(parents=True, exist_ok=True)
        count = state["parts"]
        return_parts = []
        for index in range(1, count + 1):
            output = root / f"part-{index}.mp4"
            output.write_bytes(MP4 + bytes([index]))
            return_parts.append(media_compliance._part(output, {**meta, "duration": 400 / count},
                source_sha=provenance["source_sha256"], group=f"fixture-{state['encodes']}", platform=platform,
                limits=limits, start=(index - 1) * 400 / count, end=index * 400 / count, index=index, count=count,
                input_path=Path(src), input_sha=media_integrity.sha256_file(src), interval=(0.0, 400.0), provenance=provenance))
        return return_parts
    monkeypatch.setattr(media_compliance, "prepare_video", encode)
    return state


def enqueue(fixture, *, schedule=None, artifacts=True, key="approved-intent"):
    raw = fixture["raw"]
    payload = {"message": "Exact approved caption about this selected video and its complete ordered parts"}
    if artifacts:
        payload["artifacts"] = [{"artifact_kind": "source_video", "local_path": str(raw),
            "file_record_id": fixture["fid"], "source_file_record_id": fixture["fid"],
            "sha256": hashlib.sha256(MP4).hexdigest(), "size_bytes": len(MP4)}]
    job = jobs.enqueue_job(jobs.JobSpec(platform="twitter", profile_id=fixture["profile"].id, payload=payload,
        targets=[(f"account:{fixture['account'].id}", str(raw), schedule)], idempotency_key=key), db_path=fixture["path"])
    return job, jobs.list_targets(job.id, db_path=fixture["path"])[0]


def instance(fixture, executor=worker.default_executor, *, budget=3):
    return worker.PublishWorker(executor, config=worker.WorkerConfig(poll_interval=.001, max_concurrent=1,
        retry=worker.RetryPolicy(max_attempts=budget)), db_path=fixture["path"])


def claim(fixture):
    return jobs.claim_next_targets(db_path=fixture["path"], owner="fixture-worker", prepare_ahead_seconds=1800)[0]


def sealed_plan(fixture):
    target = claim(fixture)
    current = instance(fixture)
    payload = current._target_payload(target, jobs.get_job(target.job_id, db_path=fixture["path"]))
    worker._materialise_publish_media(payload, target, db_path=fixture["path"])
    source = worker._publication_payload(payload)
    source["_preserveApprovedText"] = True
    parts = guard.prepare_payloads(source, platform="twitter", account_config=fixture["account"].config,
                                   out_dir=fixture["raw"].parent / "generated", allow_reprepare=False)
    return target, jobs.prepare_owned_publication(target.id, lease_token=target.lease_token,
        source_payload=source, prepared_payloads=parts, db_path=fixture["path"])


@pytest.mark.parametrize("alias", ["sha256", "checksum", "metadata.sha256", "metadata.checksum"])
def test_worker_facade_rejects_wrong_bytes_for_every_checksum_alias(tmp_path, alias):
    path = tmp_path / "variant.mp4"
    path.write_bytes(MP4)
    artifact = {alias: "a" * 64} if "." not in alias else {"metadata": {alias.split(".")[1]: "a" * 64}}
    with pytest.raises(worker.MediaRestoreError) as failure:
        worker._verify_artifact(path, artifact)
    assert failure.value.retryable is False
    assert isinstance(failure.value.__cause__, media_integrity.MediaIntegrityError)


@pytest.mark.parametrize("artifact", [
    {"sha256": "a" * 64, "checksum": "b" * 64}, {"sha256": "bad"},
    {"size_bytes": len(MP4), "metadata": {"size_bytes": len(MP4) - 1}}, {"size_bytes": 1.0},
])
def test_worker_facade_rejects_conflicting_or_malformed_facts(tmp_path, artifact):
    path = tmp_path / "selected.mp4"; path.write_bytes(MP4)
    with pytest.raises(worker.MediaRestoreError):
        worker._verify_artifact(path, artifact)


def test_final_hash_does_not_treat_input_or_original_source_hash_as_final(tmp_path):
    path = tmp_path / "selected-part.mp4"; path.write_bytes(MP4)
    digest = hashlib.sha256(MP4).hexdigest()
    assert worker._verify_artifact(path, {"sha256": digest, "metadata": {"source_sha256": "b" * 64, "input_sha256": "c" * 64}}) == {
        "sha256": digest, "size_bytes": len(MP4)}


@pytest.mark.parametrize("future", [False, True])
def test_prospective_raw_prepares_every_part_atomically_and_never_marks_success(fixture, monkeypatch, future):
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    requested = (now + timedelta(minutes=20 if future else -5)).isoformat()
    job, target = enqueue(fixture, schedule=requested)
    original = rows(fixture["path"], "publish_jobs")[0]
    native = Mock(side_effect=AssertionError("create before readiness"))
    managed = Mock(side_effect=AssertionError("managed create before readiness"))
    monkeypatch.setattr(worker, "_run_platform_upload", native)
    monkeypatch.setattr(worker.sociamonials_fallback, "publish_via_sociamonials", managed)
    current = claim(fixture)
    asyncio.run(instance(fixture)._run_target_inline(current))
    all_targets = rows(fixture["path"], "publish_job_targets")
    assert len(all_targets) == fixture["parts"]
    assert all(t["status"] == "pending" for t in all_targets)
    assert fixture["encodes"] == 1
    assert all_targets[0]["attempts"] == current.attempts == 1
    assert all_targets[0]["lease_token"] is None
    assert len(rows(fixture["path"], "campaign_posts")) == 4
    assert len(rows(fixture["path"], "campaign_artifacts")) == 4
    assert rows(fixture["path"], "publish_jobs")[0]["payload_json"] == original["payload_json"]
    assert rows(fixture["path"], "publish_jobs")[0]["intent_hash"] == original["intent_hash"]
    parts = [json.loads(t["checkpoint_json"]).get("payload") or json.loads(rows(fixture["path"], "publish_jobs")[i]["payload_json"])
             for i, t in enumerate(all_targets)]
    assert [p["publicationGroup"]["index"] for p in parts] == [1, 2, 3, 4]
    assert [p["artifacts"][0]["metadata"]["part_index"] for p in parts] == [1, 2, 3, 4]
    assert all(p["message"] == job.payload["message"] for p in parts)
    slots = [jobs._utc_datetime(t["schedule_at"]) for t in all_targets]
    assert all(b - a >= timedelta(minutes=30) for a, b in zip(slots, slots[1:]))
    assert max(sum(s.date() == d for s in slots) for d in {s.date() for s in slots}) <= 3
    assert slots[0] == jobs._utc_datetime(requested) if future else slots[0] >= now + timedelta(minutes=30)
    assert jobs.get_job(job.id, db_path=fixture["path"]).completed_targets == 0
    native.assert_not_called(); managed.assert_not_called()


@pytest.mark.parametrize("drive_worker", [False, True])
def test_owned_raw_secondary_backup_keeps_native_hold_and_every_ordered_part(fixture, monkeypatch, drive_worker):
    from myUtils import sociamonials_fallback as sm
    from tests.test_sociamonials_fallback import ContractSession, profile as managed_profile

    job, _target = enqueue(fixture, schedule=datetime.now(timezone.utc) + timedelta(minutes=20))
    account = profiles.update_account(fixture["account"].id, config_patch={
        "sociamonials": {"network": "tw", "profile_refs": ["fixture"]},
        "_nativeCredentialError": {"kind": "unknown", "code": "invalid_request", "httpStatus": 400},
    }, db_path=fixture["path"])
    fixture["account"] = account
    before_config = copy.deepcopy(account.config)
    original = rows(fixture["path"], "publish_jobs")[0]
    session = ContractSession(profiles=[managed_profile(network="tw", ref="fixture")])
    monkeypatch.setenv("SAU_SOCIAMONIALS_FALLBACK", "1")
    monkeypatch.setenv("SOCIAMONIALS_API_KEY", "fixture-only")
    monkeypatch.setattr(sm, "_RATE_WINDOWS", {})
    monkeypatch.setattr(sm, "_get_session", lambda supplied=None: session)
    assert publish_readiness.destination_readiness(account, fixture["profile"].settings,
        db_path=fixture["path"])["reasonCode"] == "native_hold"

    claimed = claim(fixture)
    current = instance(fixture)
    if drive_worker:
        asyncio.run(current._run_target_inline(claimed))
    else:
        payload = current._target_payload(claimed, job)
        result = worker._prepare_target_prerequisites(claimed, job, payload,
            db_path=fixture["path"], backup=True)
        assert isinstance(result, worker.PreparationCompleted)
    targets = rows(fixture["path"], "publish_job_targets")
    assert len(targets) == fixture["parts"] == 4
    assert all(t["status"] == "pending" for t in targets)
    assert targets[0]["attempts"] == claimed.attempts == 1
    assert all(t["lease_token"] is None for t in targets)
    stored_jobs = rows(fixture["path"], "publish_jobs")
    assert stored_jobs[0]["payload_json"] == original["payload_json"]
    assert stored_jobs[0]["intent_hash"] == original["intent_hash"]
    parts = [json.loads(t["checkpoint_json"]).get("payload") or json.loads(stored_jobs[i]["payload_json"])
             for i, t in enumerate(targets)]
    assert [p["publicationGroup"]["index"] for p in parts] == [1, 2, 3, 4]
    assert [p["artifacts"][0]["metadata"]["part_index"] for p in parts] == [1, 2, 3, 4]
    assert all(p["message"] == job.payload["message"] for p in parts)
    assert all(p["artifacts"][0]["metadata"]["source_sha256"] == hashlib.sha256(MP4).hexdigest() for p in parts)
    assert fixture["encodes"] == 1
    assert not session.calls_for("POST", "/posts", create=True)
    assert profiles.get_account(account.id, db_path=fixture["path"]).config == before_config
    assert publish_readiness.destination_readiness(account, fixture["profile"].settings,
        db_path=fixture["path"])["reasonCode"] == "native_hold"
    assert jobs.get_job(job.id, db_path=fixture["path"]).completed_targets == 0


def test_failure_on_last_sibling_rolls_back_registry_posts_targets_slots_and_facts(fixture, monkeypatch):
    execute_sql(fixture["path"], "UPDATE file_records SET sha256=NULL,size_bytes=NULL WHERE id=?", (fixture["fid"],))
    job, _ = enqueue(fixture, artifacts=False)
    target, plan = sealed_plan(fixture)
    before = {t: rows(fixture["path"], t) for t in ("publish_jobs", "publish_job_targets", "campaigns", "campaign_posts", "campaign_artifacts", "file_records")}
    original = jobs.enqueue_job_in_transaction
    calls = []
    def fail_last(conn, spec, **kwargs):
        calls.append(spec)
        if len(calls) == 3:
            raise RuntimeError("injected final sibling failure")
        return original(conn, spec, **kwargs)
    monkeypatch.setattr(jobs, "enqueue_job_in_transaction", fail_last)
    with pytest.raises(RuntimeError, match="final sibling"):
        jobs.install_owned_publication(plan, db_path=fixture["path"])
    assert {t: rows(fixture["path"], t) for t in before} == before
    monkeypatch.setattr(jobs, "enqueue_job_in_transaction", original)
    installed = jobs.install_owned_publication(plan, db_path=fixture["path"])
    assert len(installed) == 4
    assert rows(fixture["path"], "file_records")[0]["sha256"] == hashlib.sha256(MP4).hexdigest()
    with pytest.raises((delivery.LeaseLost, ValueError)):
        jobs.install_owned_publication(plan, db_path=fixture["path"])
    assert len(rows(fixture["path"], "publish_job_targets")) == 4


@pytest.mark.parametrize("mutation", ["lease", "cancel", "generation", "receipt", "bytes", "config"])
def test_prepared_install_is_fenced_after_expensive_work(fixture, mutation):
    enqueue(fixture)
    target, plan = sealed_plan(fixture)
    if mutation == "lease":
        execute_sql(fixture["path"], "UPDATE publish_job_targets SET lease_token='replacement' WHERE id=?", (target.id,))
    elif mutation == "cancel":
        jobs.cancel_job(target.job_id, db_path=fixture["path"])
    elif mutation in {"generation", "receipt"}:
        extra = {"preparation_generation": "replacement"} if mutation == "generation" else {"provider_receipt": {"provider": "twitter", "id": "123", "status": "processing"}}
        assert jobs.save_target_checkpoint(target.id, extra, lease_token=target.lease_token, db_path=fixture["path"])
    elif mutation == "bytes":
        Path(json.loads(plan.parts_json)[-1]["artifacts"][0]["local_path"]).write_bytes(MP4 + b"changed")
    else:
        account = fixture["account"]
        execute_sql(fixture["path"], "UPDATE accounts SET config_json=? WHERE id=?", (json.dumps({**account.config, "twitterUserId": "other"}), account.id))
    before = {t: rows(fixture["path"], t) for t in ("publish_jobs", "publish_job_targets", "campaign_posts", "campaign_artifacts")}
    with pytest.raises((ValueError, delivery.LeaseLost, delivery.DeliveryUnknown)):
        jobs.install_owned_publication(plan, db_path=fixture["path"])
    assert {t: rows(fixture["path"], t) for t in before} == before


def test_json_ready_and_forged_prepared_snapshot_are_never_authority(fixture):
    job, target = enqueue(fixture)
    assert target.checkpoint["admission"]["mode"] == jobs.PROSPECTIVE_ADMISSION_MODE
    with pytest.raises(ValueError, match="authentic"):
        jobs.install_owned_publication({"ready": True, "target_id": target.id}, db_path=fixture["path"])
    claimed, plan = sealed_plan(fixture)
    with pytest.raises(ValueError, match="forged"):
        jobs.install_owned_publication(replace(plan, parts_json="[]"), db_path=fixture["path"])
    assert len(rows(fixture["path"], "publish_job_targets")) == 1


def test_bounded_warming_does_not_expand_legacy_pending_scope(fixture):
    schedule = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=20)).isoformat()
    job, target = enqueue(fixture, schedule=schedule)
    assert not jobs.has_claimable_targets(db_path=fixture["path"])
    assert jobs.has_claimable_targets(db_path=fixture["path"], prepare_ahead_seconds=1800)
    execute_sql(fixture["path"], "UPDATE publish_job_targets SET checkpoint_json='{}' WHERE id=?", (target.id,))
    assert not jobs.has_claimable_targets(db_path=fixture["path"], prepare_ahead_seconds=1800)
    assert jobs.claim_next_targets(db_path=fixture["path"], owner="fixture", prepare_ahead_seconds=1800) == []


def test_already_ready_generation_uses_final_validation_without_encoding(fixture, monkeypatch):
    fixture["needs_prepare"] = False
    job, target = enqueue(fixture)
    assert job.payload["publishReadiness"]["ready"] is True
    forbidden = Mock(side_effect=AssertionError("ready bytes must not re-encode"))
    monkeypatch.setattr(guard, "prepare_payloads", forbidden)
    async def publish(_platform, payload, current):
        assert delivery.state(payload)["readiness"]["ready"] is True
        delivery.confirmed(payload, provider="twitter", receipt_id="123")
    asyncio.run(instance(fixture, publish)._run_target_inline(claim(fixture)))
    after = jobs.get_target(target.id, db_path=fixture["path"])
    assert after.status == "succeeded" and not after.checkpoint.get("preparation_generation")
    forbidden.assert_not_called()


def install_backend(fixture, *, alternative=False):
    path = fixture["path"]
    with closing(sqlite3.connect(path)) as conn, conn:
        bid = conn.execute("INSERT INTO storage_backends (slug,label,provider,bucket,region,endpoint,access_key,secret_key) VALUES ('exact','Exact','rclone','drive','','sau/videoFile','','')").lastrowid
        conn.execute("UPDATE file_records SET storage_backend_id=?,storage_key='registered/exact.mp4',storage_cdn_url=? WHERE id=?",
                     (bid, "https://healthy.example/registered/exact.mp4" if alternative else None, fixture["fid"]))
    fixture["raw"].unlink()


def test_quota_uses_bounded_exact_registered_healthy_alternative_before_any_budget(fixture, monkeypatch):
    install_backend(fixture, alternative=True)
    job, target = enqueue(fixture)
    probe = Mock(side_effect=RuntimeError("shared Drive daily quota exceeded"))
    monkeypatch.setattr(worker.media_remote_storage, "stat_from_backend", probe)
    downloaded = []
    def healthy(url, destination, **kwargs):
        assert url == "https://healthy.example/registered/exact.mp4"
        assert 0 < kwargs["total_deadline"] <= 90 and kwargs["max_bytes"] == len(MP4)
        downloaded.append(url); Path(destination).write_bytes(MP4)
    monkeypatch.setattr(media_restore.outbound_http, "download_public_file", healthy)
    asyncio.run(instance(fixture)._run_target_inline(claim(fixture)))
    assert fixture["raw"].read_bytes() == MP4 and probe.call_count == 1 and len(downloaded) == 1
    current = jobs.get_target(target.id, db_path=fixture["path"])
    assert current.status == "pending" and current.checkpoint["readiness"]["ready"]
    assert not current.checkpoint.get("publish_routes", {}).get("native", {}).get("attempts")


def test_quota_hold_survives_restart_without_hammering_or_spending_native_budget(fixture, monkeypatch):
    install_backend(fixture)
    job, target = enqueue(fixture)
    probe = Mock(side_effect=RuntimeError("Drive quota exceeded"))
    transfer = Mock(side_effect=AssertionError("known quota must not download"))
    monkeypatch.setattr(worker.media_remote_storage, "stat_from_backend", probe)
    monkeypatch.setattr(worker.media_remote_storage, "download_from_backend", transfer)
    for _ in range(2):
        asyncio.run(instance(fixture)._run_target_inline(claim(fixture)))
        current = jobs.get_target(target.id, db_path=fixture["path"])
        assert current.status == "retrying" and current.error_category == "prerequisite_quota"
        execute_sql(fixture["path"], "UPDATE publish_job_targets SET next_attempt_at='2000-01-01T00:00:00' WHERE id=?", (target.id,))
    current = jobs.get_target(target.id, db_path=fixture["path"])
    assert current.attempts == 2 and current.checkpoint["prerequisites"]["attempts"] == 2
    assert not current.checkpoint.get("publish_routes", {}).get("native", {}).get("attempts")
    assert probe.call_count == 1
    transfer.assert_not_called()


def test_truncated_registered_restore_is_never_exposed_or_published(fixture, monkeypatch):
    install_backend(fixture)
    job, target = enqueue(fixture)
    monkeypatch.setattr(worker.media_remote_storage, "stat_from_backend", lambda *a, **k: {"size_bytes": len(MP4)})
    monkeypatch.setattr(worker.media_remote_storage, "download_from_backend", lambda b, key, dest, **k: Path(dest).write_bytes(MP4[:32]))
    asyncio.run(instance(fixture)._run_target_inline(claim(fixture)))
    current = jobs.get_target(target.id, db_path=fixture["path"])
    assert current.status == "blocked" and current.error_category == "prerequisite_integrity"
    assert not fixture["raw"].exists() and not list(fixture["raw"].parent.glob("*.restore"))
    assert fixture["encodes"] == 0


def test_missing_part_never_restores_raw_parent_or_client_url(fixture, monkeypatch):
    destination = fixture["raw"].parent / "selected-part.mp4"
    transfer = Mock(side_effect=AssertionError("raw/client substitution forbidden"))
    monkeypatch.setattr(worker.media_remote_storage, "download_from_backend", transfer)
    monkeypatch.setattr(media_restore.outbound_http, "download_public_file", transfer)
    with pytest.raises(worker.MediaRestoreError) as failure:
        worker._ensure_artifact_paths_local({"artifacts": [{"artifact_kind": "prepared_video", "local_path": str(destination),
            "source_file_record_id": fixture["fid"], "public_url": "https://unregistered.example/selected-part.mp4",
            "sha256": hashlib.sha256(MP4).hexdigest(), "metadata": {"part_index": 1, "part_count": 2}}]}, db_path=fixture["path"])
    assert failure.value.retryable is False and failure.value.details["dependencyCategory"] == "identity"
    transfer.assert_not_called(); assert not destination.exists()


def test_capacity_defers_before_encode_or_create(fixture, monkeypatch):
    job, target = enqueue(fixture)
    monkeypatch.setattr(media_restore.shutil, "disk_usage", lambda p: type("Usage", (), {"free": 1})())
    create = Mock(side_effect=AssertionError("capacity refused before publication"))
    asyncio.run(instance(fixture, create)._run_target_inline(claim(fixture)))
    current = jobs.get_target(target.id, db_path=fixture["path"])
    assert current.status == "retrying" and current.error_category == "prerequisite_capacity"
    assert current.checkpoint["readiness"]["stage"] == "waiting_capacity" and fixture["encodes"] == 0
    create.assert_not_called()


def test_prerequisite_dispatch_and_provider_attempts_remain_distinct(fixture, monkeypatch):
    job, target = enqueue(fixture)
    execute_sql(fixture["path"], "UPDATE publish_job_targets SET attempts=7 WHERE id=?", (target.id,))
    pending = [media_restore.PrerequisiteUnavailable("transport"), media_restore.PrerequisiteUnavailable("quota")]
    def prerequisite(current, _job, payload, **kwargs):
        if pending:
            raise pending.pop(0)
        delivery.checkpoint(payload, readiness={"ready": True, "phase": "ready", "stage": "ready"})
        payload["_prerequisites_checked"] = True
    monkeypatch.setattr(worker, "_prepare_target_prerequisites", prerequisite)  # local budget-layer unit mock
    published = []
    async def publish(_platform, payload, _target):
        assert delivery.state(payload)["readiness"]["ready"]
        published.append(True)
        delivery.confirmed(payload, provider="twitter", receipt_id="123")
    for _ in range(3):
        asyncio.run(instance(fixture, publish)._run_target_inline(claim(fixture)))
        execute_sql(fixture["path"], "UPDATE publish_job_targets SET next_attempt_at='2000-01-01T00:00:00' WHERE id=?", (target.id,))
    current = jobs.get_target(target.id, db_path=fixture["path"])
    assert current.attempts == 10 and current.checkpoint["prerequisites"]["attempts"] == 2
    assert current.checkpoint["publish_routes"]["native"]["attempts"] == 1 and published == [True]


def test_receipt_poll_needs_no_media_or_new_create_budget(fixture, monkeypatch):
    job, target = enqueue(fixture)
    receipt = {"provider": "twitter", "id": "123", "status": "processing", "submitted_at": delivery.utc_now()}
    execute_sql(fixture["path"], "UPDATE publish_job_targets SET status='awaiting_delivery',attempts=9,checkpoint_json=?,delivery_state='awaiting_delivery' WHERE id=?",
                (json.dumps({"provider_receipt": receipt, "submission_started": True}), target.id))
    fixture["raw"].unlink()
    prerequisite = Mock(side_effect=AssertionError("poll must not need bytes/caps/create"))
    monkeypatch.setattr(worker, "_prepare_target_prerequisites", prerequisite)
    async def poll(_platform, payload, _target):
        assert delivery.receipt_for(payload, "twitter")["id"] == "123"
        delivery.confirmed(payload, provider="twitter", receipt_id="123")
    asyncio.run(instance(fixture, poll)._run_target_inline(claim(fixture)))
    current = jobs.get_target(target.id, db_path=fixture["path"])
    assert current.status == "succeeded" and current.attempts == 9
    assert not current.checkpoint.get("publish_routes", {}).get("native", {}).get("attempts")
    prerequisite.assert_not_called()


@pytest.mark.parametrize("kind", ["preview", "unknown_media", "rejection", "unknown_create", "partial", "delivered"])
def test_actual_x_class_receives_observer_and_precise_before_create_budget(fixture, monkeypatch, kind):
    from uploader.twitter_uploader.main import TwitterThreadVideo
    fixture["needs_prepare"] = False
    job, target = enqueue(fixture)
    monkeypatch.setattr(TwitterThreadVideo, "_validate_media_file", classmethod(lambda cls, p: Path(p).resolve()))
    seen = []
    async def main(app):
        observer = app.delivery_observer
        assert callable(observer)
        seen.append(observer)
        plan = [{"index": 0, "identity": "a" * 64}]
        if kind == "partial": plan.append({"index": 1, "identity": "b" * 64})
        observer({"event": "plan", "parts": plan})
        observer({"event": "preparing", "part": "a" * 64})
        if kind == "preview": raise TimeoutError("preview timeout before any mutation")
        media = observer({"event": "media_request", "part": "a" * 64})
        if kind == "unknown_media": observer({"event": "unknown", "part": "a" * 64, "name": media})
        observer({"event": "receipt", "part": "a" * 64, "name": media, "id": "900", "httpStatus": 200})
        create = observer({"event": "create_action", "part": "a" * 64, "reply_to": None})
        persisted = jobs.get_target(target.id, db_path=fixture["path"]).checkpoint
        assert persisted["submission_started"] is True
        assert persisted["publish_routes"]["native"]["attempts"] == 1
        observer({"event": "create_request", "part": "a" * 64, "name": create})
        if kind == "unknown_create": observer({"event": "unknown", "part": "a" * 64, "name": create})
        if kind == "rejection": observer({"event": "rejection", "part": "a" * 64, "name": create, "httpStatus": 401})
        observer({"event": "receipt", "part": "a" * 64, "name": create, "id": "123", "httpStatus": 200})
        if kind == "partial": raise TimeoutError("next part preview failed after confirmed prefix")
        observer({"event": "complete"})
        return ["123"]
    monkeypatch.setattr(TwitterThreadVideo, "main", main)
    asyncio.run(instance(fixture)._run_target_inline(claim(fixture)))
    current = jobs.get_target(target.id, db_path=fixture["path"])
    assert len(seen) == 1
    journal = current.checkpoint["x_browser_operations"]
    assert journal["target_id"] == target.id and journal["account_key"] == target.account_ref
    if kind == "preview":
        assert current.status == "retrying" and not current.checkpoint.get("submission_started")
        assert not current.checkpoint.get("publish_routes", {}).get("native", {}).get("attempts")
        assert current.checkpoint["prerequisites"]["attempts"] == 1
    elif kind == "unknown_media":
        assert current.status == "needs_review" and not current.checkpoint.get("submission_started")
        assert not current.checkpoint.get("publish_routes", {}).get("native", {}).get("attempts")
    elif kind == "unknown_create":
        assert current.status == "needs_review" and current.checkpoint["submission_started"] is True
    elif kind == "rejection":
        assert current.status == "failed" and current.checkpoint["x_browser_non_delivery"]["httpStatus"] == 401
        assert not generic_delivery.x_browser_has_effects(current.checkpoint)
    elif kind == "partial":
        assert current.status == "awaiting_delivery" and current.checkpoint["provider_receipt"]["id"] == "123"
        assert current.checkpoint["stage"] != "browser_delivered"
    else:
        assert current.status == "succeeded" and current.checkpoint["provider_receipt"]["status"] == "delivered"


def test_unknown_x_media_survives_incremental_save_cancel_retry_and_restart(fixture):
    job, target = enqueue(fixture)
    current = claim(fixture)
    journal = {"version": 1, "target_id": target.id, "account_key": target.account_ref,
        "plan": [{"index": 0, "identity": "a" * 64}], "budgets": {"a:media": {"attempts": 1, "max_attempts": 256}},
        "items": {"media:0:1": {"status": "unknown", "effect": "media_only", "operation_generation": "a" * 32}}}
    assert jobs.save_target_checkpoint(target.id, {"x_browser_operations": journal}, lease_token=current.lease_token, db_path=fixture["path"])
    assert jobs.save_target_checkpoint(target.id, {"x_browser_operations": {**journal, "items": {}, "budgets": {}}}, lease_token=current.lease_token, db_path=fixture["path"])
    assert jobs.get_target(target.id, db_path=fixture["path"]).checkpoint["x_browser_operations"] == journal
    execute_sql(fixture["path"], "UPDATE publish_job_targets SET lease_expires_at='2000-01-01T00:00:00' WHERE id=?", (target.id,))
    assert jobs.requeue_stale_running(db_path=fixture["path"]) == 1
    assert jobs.get_target(target.id, db_path=fixture["path"]).status == "needs_review"
    cancelled = jobs.cancel_target(target.id, db_path=fixture["path"])
    assert cancelled.checkpoint["x_browser_operations"] == journal
    with pytest.raises(ValueError, match="Submission evidence"):
        jobs.resubmit_target(target.id, db_path=fixture["path"])
    assert jobs._recovery_has_submission_evidence(json.dumps({"previous_failure": {"checkpoint": {"x_browser_operations": journal}}}))


def test_restart_during_prerequisites_does_not_exhaust_publication_budget(fixture):
    job, target = enqueue(fixture)
    current = claim(fixture)
    execute_sql(fixture["path"], "UPDATE publish_job_targets SET attempts=15,lease_expires_at='2000-01-01T00:00:00' WHERE id=?", (target.id,))
    assert jobs.requeue_stale_running(max_attempts=3, db_path=fixture["path"]) == 1
    current = jobs.get_target(target.id, db_path=fixture["path"])
    assert current.status == "retrying" and current.attempts == 15
    assert current.checkpoint["prerequisites"]["attempts"] == 1
    assert current.error_category == "prerequisite_interrupted"


def test_runtime_hash_excludes_only_admission_fields_not_copy_options_or_provenance():
    payload = {"message": "approved", "networkOptions": {"title": "approved"},
               "artifacts": [{"sha256": "a" * 64, "metadata": {"source_sha256": "b" * 64}}]}
    spec = jobs.JobSpec(platform="twitter", payload=payload, targets=[("account:1", "selected", None)])
    key = jobs.derive_idempotency_key(spec)
    for name in publish_admission.RUNTIME_PAYLOAD_KEYS:
        assert jobs.derive_idempotency_key(replace(spec, payload={**payload, name: {"ready": True, "checkedAt": "later"}})) == key
    for changed in ({**payload, "message": "different"}, {**payload, "networkOptions": {}},
                    {**payload, "artifacts": [{"sha256": "c" * 64, "metadata": {"source_sha256": "b" * 64}}]},
                    {**payload, "artifacts": [{"sha256": "a" * 64, "metadata": {"source_sha256": "d" * 64}}]}):
        assert jobs.derive_idempotency_key(replace(spec, payload=changed)) != key
