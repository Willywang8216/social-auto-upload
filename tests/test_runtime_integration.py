"""Source-only integration checks: no uploads, paid APIs, or real credentials."""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from db.createTable import bootstrap
from myUtils import campaign_media_prep, campaigns, delivery, inbox_drive, jobs, profiles
from myUtils import media_remote_storage, publish_orchestrator, publish_services, sociamonials_reclaim
from myUtils import target_runner
from myUtils.sqlite_utils import connect


@pytest.fixture(scope="module")
def schema(tmp_path_factory):
    path = tmp_path_factory.mktemp("runtime-schema") / "schema.db"
    bootstrap(path)
    return path


@pytest.fixture
def setup(tmp_path, monkeypatch, schema):
    db = tmp_path / "test.db"
    shutil.copyfile(schema, db)
    (tmp_path / "videoFile").mkdir()
    (tmp_path / "videoFile" / "sfw_walk.mp4").write_bytes(b"synthetic-media")
    monkeypatch.setenv("SAU_DB_PATH", str(db))
    monkeypatch.setattr(jobs, "DB_PATH", db)
    monkeypatch.setattr(campaign_media_prep, "BASE_DIR", tmp_path)
    monkeypatch.setattr(inbox_drive, "BASE_DIR", tmp_path)
    profile = profiles.create_profile("Synthetic", db_path=db)
    account = profiles.add_account(profile.id, "telegram", "fixture", auth_type="api", db_path=db)
    request = dict(profile_ids=[profile.id], selected_account_ids=[account.id],
                   media_file_paths=["sfw_walk.mp4"], brief="A synthetic test caption.",
                   options={}, schedule=None, account_drafts={}, db_path=db)
    return SimpleNamespace(db=db, root=tmp_path, profile=profile, account=account, request=request)


@pytest.fixture
def client(setup, monkeypatch):
    import sau_backend
    from myUtils.security import SecurityPolicy
    monkeypatch.setattr(sau_backend, "BASE_DIR", setup.root)
    monkeypatch.setitem(sau_backend.app.config, "TESTING", True)
    monkeypatch.setitem(sau_backend.app.config, "SECURITY_POLICY", SecurityPolicy(
        tokens=frozenset({"synthetic-token"}), cors_origins=()))
    return sau_backend.app.test_client()


AUTH = {"Authorization": "Bearer synthetic-token"}


def test_http_replays_receipt_and_conflicts_changed_intent(setup, client, monkeypatch):
    import sau_backend
    prepare = Mock(side_effect=AssertionError("HTTP must not prepare media"))
    monkeypatch.setattr(sau_backend, "_prepare_campaign_media_artifacts", prepare)
    body = dict(profileIds=[setup.profile.id], selectedAccountIds=[setup.account.id],
                mediaFilePaths=["sfw_walk.mp4"], brief="Fixture caption", submissionId="stable-client-id")
    first = client.post("/publish-center/submit", json=body, headers=AUTH)
    second = client.post("/publish-center/submit", json=body, headers=AUTH)
    assert first.status_code == second.status_code == 202
    assert first.json == second.json
    assert first.json["data"]["status"] == "preparing"
    assert first.json["data"]["jobs"] == []
    assert len(first.json["data"]["campaignIds"]) == 1
    assert client.post("/publish-center/submit", json={**body, "brief": "Changed"}, headers=AUTH).status_code == 409
    prepare.assert_not_called()
    with connect(setup.db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM publish_jobs").fetchone()[0] == 0


def test_http_server_failure_is_not_success(client, monkeypatch):
    monkeypatch.setattr(publish_services, "submit_publish", Mock(side_effect=OSError("synthetic failure")))
    response = client.post("/publish-center/submit", json={}, headers=AUTH)
    assert response.status_code == 500
    assert response.json["code"] == 500
    assert response.json["data"] is None


def test_http_cannot_run_a_production_worker(client, monkeypatch):
    import sau_backend
    monkeypatch.setenv("SAU_PROCESS_ROLE", "web")
    monkeypatch.setenv("APP_ENV", "production")
    run = Mock(side_effect=AssertionError("must not execute"))
    monkeypatch.setattr(sau_backend, "run_worker_drain", run)
    assert client.post("/jobs/run", headers=AUTH).status_code == 409
    run.assert_not_called()


def test_inbox_admits_without_downloading_and_reuses_identity(setup, client, monkeypatch):
    from myUtils import inbox_ops
    item = {"id": "one", "profileIds": [setup.profile.id], "sfwFlag": "sfw",
            "remotePath": "nw/video/sfw_walk.mp4", "brief": "Fixture caption"}
    monkeypatch.setattr(inbox_ops, "list_items", lambda: {"ready": [item]})
    download = Mock(side_effect=AssertionError("HTTP must not download"))
    monkeypatch.setattr(inbox_drive, "stage_remote_media", download)
    first = client.post("/api/inbox/items/one/publish", json={}, headers=AUTH)
    second = client.post("/api/inbox/items/one/publish", json={}, headers=AUTH)
    assert first.status_code == second.status_code == 202
    assert first.json == second.json
    download.assert_not_called()
    with connect(setup.db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] == 1
        row = conn.execute("SELECT file_path,storage_key FROM file_records").fetchone()
        assert Path(row[0]).name == "sfw_walk.mp4"
        assert row[1] == "nw/video/sfw_walk.mp4"
        assert conn.execute("SELECT COUNT(*) FROM storage_backends").fetchone()[0] == 1
    assert not list((setup.root / "videoFile").rglob("_inbox_cache"))


def _claimed_prep(setup):
    result = publish_services.submit_publish(**setup.request, submission_id="prep-fence")
    campaign = campaigns.claim_next_preparing_campaign(owner="owner-one", db_path=setup.db)
    assert campaign.id == result.campaign_ids[0]
    post = campaigns.add_campaign_post(campaign.id, "telegram", account_ids=[setup.account.id],
                                       status="ready", draft={"message": "Fixture"}, db_path=setup.db)
    payload = {"campaignId": campaign.id, "campaignPostId": post.id, "draft": {"message": "Fixture"}}
    return campaign, post, payload


@pytest.mark.parametrize("change", ["owner", "attempt"])
def test_obsolete_preparation_cannot_enqueue(setup, change):
    campaign, post, payload = _claimed_prep(setup)
    metadata = dict(campaign.metadata)
    if change == "owner":
        metadata[campaigns.PREP_LEASE_KEY] = {"owner": "owner-two"}
    else:
        metadata[campaigns.PREP_ATTEMPTS_KEY] += 1
    with connect(setup.db) as conn:
        conn.execute("UPDATE campaigns SET metadata_json=? WHERE id=?", (json.dumps(metadata), campaign.id))
    with pytest.raises(delivery.LeaseLost):
        publish_orchestrator._enqueue_post("telegram", payload,
            [(f"account:{setup.account.id}", f"campaign_post:{post.id}", None)],
            campaign, post, lambda job: job.to_dict(), setup.db)
    with connect(setup.db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM publish_jobs").fetchone()[0] == 0
        assert conn.execute("SELECT last_published_job_id FROM campaign_posts WHERE id=?", (post.id,)).fetchone()[0] is None


def test_campaign_summary_tracks_delivery_not_admission(setup, client):
    campaign, post, payload = _claimed_prep(setup)
    result = publish_orchestrator._enqueue_post("telegram", payload,
        [(f"account:{setup.account.id}", f"campaign_post:{post.id}", None)],
        campaign, post, lambda job: job.to_dict(), setup.db)
    job_id = result["id"]
    assert campaigns.finish_campaign_prep(campaign.id, owner="owner-one", status="publishing", db_path=setup.db)
    current = campaigns.get_campaign(campaign.id, db_path=setup.db)
    assert current.status == "publishing" and current.published_at is None
    target = jobs.claim_next_targets(db_path=setup.db, owner="publisher")[0]
    checkpoint = {"submission_started": True, "provider_receipt": {
        "provider": "fixture", "id": "receipt", "status": "processing", "submitted_at": delivery.utc_now()}}
    assert jobs.mark_target_awaiting_delivery(target.id, checkpoint, lease_token=target.lease_token,
                                             delay_seconds=0, db_path=setup.db)
    assert campaigns.get_campaign_post(post.id, db_path=setup.db).status == "awaiting_delivery"
    assert campaigns.get_campaign(campaign.id, db_path=setup.db).published_at is None
    polled = jobs.claim_next_targets(db_path=setup.db, owner="publisher")[0]
    assert jobs.mark_target_success(polled.id, lease_token=polled.lease_token, db_path=setup.db)
    assert campaigns.get_campaign(campaign.id, db_path=setup.db).status == "published"
    assert campaigns.get_campaign(campaign.id, db_path=setup.db).published_at
    body = client.get(f"/campaigns/{campaign.id}", headers=AUTH).json["data"]
    assert body["posts"][0]["jobId"] == job_id
    assert "_prepLease" not in body["metadata"]


def test_media_pinned_after_cancellation_with_unknown_acceptance(setup):
    job = jobs.enqueue_job(jobs.JobSpec(platform="telegram", payload={"artifacts": [
        {"local_path": "/app/generated/receipt-file.mp4"}]}, targets=[("legacy", "source.mp4", None)]), db_path=setup.db)
    target = jobs.claim_next_targets(db_path=setup.db, owner="one")[0]
    jobs.save_target_checkpoint(target.id, {"submission_started": True}, lease_token=target.lease_token, db_path=setup.db)
    jobs.cancel_job(job.id, db_path=setup.db)
    assert "receipt-file.mp4" in sociamonials_reclaim.live_filenames(setup.db)


def test_malformed_live_payload_stops_reclamation(setup):
    job = jobs.enqueue_job(jobs.JobSpec(platform="telegram", payload={}, targets=[("legacy", "source.mp4", None)]), db_path=setup.db)
    with connect(setup.db) as conn:
        conn.execute("UPDATE publish_jobs SET payload_json='broken json' WHERE id=?", (job.id,))
    with pytest.raises(RuntimeError, match="refusing reclamation"):
        sociamonials_reclaim.live_filenames(setup.db)


def test_account_deletion_blocks_pending_but_preserves_target(setup):
    job = jobs.enqueue_job(jobs.JobSpec(platform="telegram", profile_id=setup.profile.id, payload={},
        targets=[(f"account:{setup.account.id}", "sfw_walk.mp4", None)]), db_path=setup.db)
    profiles.delete_account(setup.account.id, db_path=setup.db)
    target = jobs.list_targets(job.id, db_path=setup.db)[0]
    assert target.status == "blocked"
    assert target.account_ref == f"account:{setup.account.id}"


def test_account_deletion_refuses_unresolved_delivery(setup):
    job = jobs.enqueue_job(jobs.JobSpec(platform="telegram", profile_id=setup.profile.id, payload={},
        targets=[(f"account:{setup.account.id}", "sfw_walk.mp4", None)]), db_path=setup.db)
    target = jobs.claim_next_targets(db_path=setup.db, owner="one")[0]
    jobs.mark_target_needs_review(target.id, "Unknown provider outcome", lease_token=target.lease_token, db_path=setup.db)
    with pytest.raises(ValueError):
        profiles.delete_account(setup.account.id, db_path=setup.db)
    assert jobs.list_targets(job.id, db_path=setup.db)[0].status == "needs_review"
    assert profiles.get_account(setup.account.id, db_path=setup.db)


def test_public_restore_keeps_original_on_partial_download(tmp_path, monkeypatch):
    import urllib.request
    target = tmp_path / "media.mp4"
    target.write_bytes(b"existing-good-bytes")
    class Response(io.BytesIO):
        status = 200
        headers = {"Content-Length": "100"}
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(b"short"))
    with pytest.raises(media_remote_storage.RemoteStorageError, match="incomplete"):
        media_remote_storage.download_public_url("https://media.example/file.mp4", target)
    assert target.read_bytes() == b"existing-good-bytes"
    assert not list(tmp_path.glob("*.part"))


def test_process_group_cleanup_even_after_root_exits(monkeypatch):
    kill = Mock()
    process = SimpleNamespace(pid=42, returncode=0, wait=AsyncMock(return_value=0))
    monkeypatch.setattr(target_runner, "os", SimpleNamespace(name="posix", killpg=kill))
    monkeypatch.setattr(target_runner, "signal", SimpleNamespace(SIGKILL=9))
    asyncio.run(target_runner.terminate_group(process))
    kill.assert_called_once_with(42, target_runner.signal.SIGKILL)
    process.wait.assert_awaited_once()


@pytest.mark.skipif(os.name != "posix", reason="Linux process-group contract")
def test_real_orphaned_child_is_killed(tmp_path, monkeypatch):
    original = asyncio.create_subprocess_exec
    pidfile = tmp_path / "child.pid"
    code = ("import subprocess,sys,pathlib; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid))")
    async def spawn(*args, **kwargs):
        return await original(sys.executable, "-c", code, start_new_session=True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert asyncio.run(target_runner.run_child([], timeout=5)) == 0
    pid = int(pidfile.read_text())
    for _ in range(30):
        stat = Path(f"/proc/{pid}/stat")
        if not stat.exists() or stat.read_text().split()[2] == "Z":
            break
        time.sleep(0.05)
    else:
        pytest.fail("Child process survived its owned process group")


def test_backend_callback_exports_are_shared():
    import sau_backend
    for name in ("_generate_account_draft", "_generate_platform_draft", "_artifact_payloads_for_platform", "_notify_tg_review"):
        assert getattr(sau_backend, name) is getattr(publish_services, name)


def test_new_artifact_records_exact_bytes_and_rejects_substitution(setup):
    import hashlib
    from myUtils import worker
    campaign, _, _ = _claimed_prep(setup)
    source = setup.root / "videoFile" / "sfw_walk.mp4"
    original = source.read_bytes()
    artifact = campaigns.add_campaign_artifact(campaign.id, artifact_kind="watermarked_video",
        local_path=str(source), metadata={"role": "video"}, db_path=setup.db)
    assert artifact.metadata["size_bytes"] == len(original)
    assert artifact.metadata["sha256"] == hashlib.sha256(original).hexdigest()
    worker._verify_artifact(source, artifact.to_dict())
    source.write_bytes(b"x" * len(original))
    with pytest.raises(worker.MediaRestoreError, match="checksum mismatch"):
        worker._verify_artifact(source, artifact.to_dict())


@pytest.mark.parametrize("platform", ["instagram", "youtube", "tiktok"])
def test_receipt_poll_without_media_persists_rotated_credentials(setup, monkeypatch, platform):
    from myUtils import worker
    payload = {"campaignId": 1, "artifacts": [], "_db_path": str(setup.db), "_delivery": {
        "provider_receipt": {"provider": platform, "id": "existing-receipt"}}}
    async def poll(**kwargs):
        kwargs["payload"]["_updated_config"] = {"accessToken": "synthetic-rotated"}
        raise delivery.DeliveryPending("Still processing")
    persist = Mock()
    monkeypatch.setitem(worker.PREPARED_PUBLISHER_REGISTRY, platform, poll)
    monkeypatch.setattr(worker, "_persist_rotated_config", persist)
    with pytest.raises(delivery.DeliveryPending):
        asyncio.run(worker._run_prepared_campaign_upload(platform, payload, SimpleNamespace(id=1),
            account=setup.account, account_file=None))
    persist.assert_called_once_with(setup.account, {"accessToken": "synthetic-rotated"}, payload=payload)
    assert "_updated_config" not in payload
    assert "synthetic-rotated" not in json.dumps(payload["_delivery"])


def test_child_receives_parent_retry_budget(setup, monkeypatch):
    target = SimpleNamespace(id=9, lease_token="fenced", status="succeeded")
    child = AsyncMock(return_value=0)
    monkeypatch.setattr(target_runner, "run_child", child)
    monkeypatch.setattr(jobs, "get_target", lambda *a, **k: target)
    parent = SimpleNamespace(_db_path=setup.db, _config=SimpleNamespace(retry=SimpleNamespace(max_attempts=7)))
    asyncio.run(target_runner.run_isolated_target(parent, target, timeout=4))
    arguments = child.await_args.args[0]
    assert arguments[arguments.index("--max-attempts") + 1] == "7"


def test_worker_twitter_api_receipt_does_not_require_old_cache(setup, monkeypatch):
    from myUtils import worker
    account = SimpleNamespace(id=7, config={}, auth_type="oauth")
    payload = {"campaignId": 1, "artifacts": [{"local_path": str(setup.root / "gone.mp4")}],
        "_delivery": {"provider_receipt": {"provider": "twitter", "id": "tweet-1"}}}
    publisher = Mock(return_value={"id": "tweet-1"})
    monkeypatch.setattr(worker.prepared_publishers, "publish_twitter_sync", publisher)
    result = asyncio.run(worker._publish_prepared_twitter("twitter", payload, SimpleNamespace(id=1),
        account=account, account_file=None))
    assert result == {"id": "tweet-1"}
    publisher.assert_called_once_with(account, payload)
    payload.pop("_delivery")
    with pytest.raises(FileNotFoundError):
        asyncio.run(worker._publish_prepared_twitter("twitter", payload, SimpleNamespace(id=1),
            account=account, account_file=None))


def test_failed_dispatch_is_live_but_never_ready(setup, monkeypatch):
    from myUtils import worker
    publisher = worker.PublishWorker(AsyncMock(), db_path=setup.db, prep_runner=Mock())
    monkeypatch.setattr(publisher, "_prep_tick", AsyncMock(side_effect=ValueError("invalid prep metadata")))
    with pytest.raises(ValueError, match="invalid prep metadata"):
        asyncio.run(publisher._tick())
    state = jobs.worker_health(db_path=setup.db)
    assert state["ok"] is False
    assert state["workers"][0]["alive"] is True
    assert state["workers"][0]["role"] == "publisher_unready"
    monkeypatch.setattr(publisher, "_prep_tick", AsyncMock())
    asyncio.run(publisher._tick())
    assert jobs.worker_health(db_path=setup.db)["ok"] is True


def test_pending_poll_delay_is_bounded_and_persisted(setup):
    from datetime import timezone
    from myUtils import worker
    job = jobs.enqueue_job(jobs.JobSpec(platform="telegram", payload={},
        targets=[(f"account:{setup.account.id}", "fixture.mp4", None)], idempotency_key="poll-delay"), db_path=setup.db)
    target = jobs.claim_next_targets(limit=1, db_path=setup.db)[0]
    payload = {}
    delivery.accepted(payload, provider="telegram", receipt_id="existing-receipt")
    publisher = worker.PublishWorker(AsyncMock(), db_path=setup.db)
    publisher._payloads[target.id] = payload
    with pytest.raises(delivery.DeliveryPending) as raised:
        delivery.pending_or_unknown(payload, "rate limited", retry_after="999999")
    asyncio.run(publisher._handle_failure(target, raised.value, Mock()))
    result = jobs.get_target(target.id, db_path=setup.db)
    assert result.status == jobs.TARGET_AWAITING_DELIVERY
    due = jobs._utc_datetime(result.next_attempt_at).replace(tzinfo=timezone.utc).timestamp()
    assert 3595 <= due - time.time() <= 3601
    assert result.checkpoint["provider_receipt"]["id"] == "existing-receipt"
    assert jobs.get_job(job.id, db_path=setup.db).status == jobs.JOB_AWAITING_DELIVERY


@pytest.mark.parametrize("status", [429, 503])
def test_receipt_poll_retains_provider_retry_hint(status):
    from myUtils import generic_delivery
    payload = {}
    delivery.accepted(payload, provider="facebook", receipt_id="video-1")
    response = SimpleNamespace(status_code=status, headers={"Retry-After": "300"})
    with pytest.raises(delivery.DeliveryPending) as raised:
        generic_delivery._poll_json(Mock(get=Mock(return_value=response)), "https://graph.example/video-1",
                                    payload, provider="facebook")
    assert raised.value.details == {"retryAfter": "300"}
    assert payload["_delivery"]["provider_receipt"]["id"] == "video-1"
