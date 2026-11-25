"""Real SQLite atomicity/fencing, with only provider evidence and codecs mocked.

No SAU_TESTING admission bypass: production prepare/validate snapshots, canonical
aliases, pacing, campaign-post insertion and enqueue/link SQL all run unchanged.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import shutil
import sqlite3
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from db.createTable import bootstrap
from myUtils import account_identity, campaign_prep, campaigns, delivery, destination_pacing, jobs
from myUtils import media_compliance, media_groups, media_integrity, platform_limits, preparation_context, profiles
from myUtils import publication_groups as groups, publish_admission, publish_orchestrator as po, publish_readiness

NOW = datetime(2035, 1, 2, 12)
TABLES = ("campaign_posts", "publish_jobs", "publish_job_targets")


def synthetic_video_probe(path, durations=None):
    """Codec-only fixture; callers still execute real byte/provenance checks."""
    return {"duration": (durations or {}).get(str(Path(path)), 10.0), "width": 1280, "height": 720,
            "fps": 30.0, "format_name": "mov,mp4", "major_brand": "isom", "codec": "h264",
            "pix_fmt": "yuv420p", "profile": "High", "field_order": "progressive",
            "sample_aspect_ratio": "1:1", "has_audio": False, "video_bit_rate": 1000000}


def prepared_video_metadata(path, source, platform, *, index=1, count=1, generation="offline-fixture"):
    checksum = hashlib.sha256(Path(source).read_bytes()).hexdigest()
    return {"role": "video", "prepared_for": [platform], "preparation_group": generation,
        "preparation_policy_version": platform_limits.PREPARATION_POLICY_VERSION,
        "part_index": index, "part_count": count, "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        "size_bytes": Path(path).stat().st_size, "source_sha256": checksum, "source_path": str(source),
        "input_path": str(source), "input_sha256": checksum, "source_is_complete": True,
        "source_duration_seconds": float(10 * count), "source_start_seconds": float(10 * (index - 1)),
        "source_end_seconds": float(10 * index), "source_interval_start_seconds": 0.0,
        "source_interval_end_seconds": float(10 * count), "unwatermarked": True,
        "original_source_interval_start_seconds": 0.0, "original_source_interval_end_seconds": float(10 * count)}


def prospective_draft_fixture(draft, platform, media_kind, effective_provider):
    """Local mock of the admission worker's prospective (NOT approved) mapper."""
    draft = copy.deepcopy(draft)
    if not draft.get("firstComment"):
        draft.pop("firstComment", None)
    return draft


@pytest.fixture(scope="module")
def schema(tmp_path_factory):
    path = tmp_path_factory.mktemp("atomic-schema") / "schema.db"
    bootstrap(path)
    return path


@pytest.fixture
def setup(tmp_path, monkeypatch, schema):
    db = tmp_path / "atomic.db"
    shutil.copyfile(schema, db)
    monkeypatch.setenv("SAU_OPERATOR_TIMEZONE", "UTC")
    monkeypatch.setattr(po, "MIN_GAP_MINUTES", 30)
    monkeypatch.setattr(po, "MAX_POSTS_PER_ACCOUNT_PER_DAY", 3)
    profile = profiles.create_profile("Offline atomic fixture", db_path=db)
    account = profiles.add_account(profile.id, "twitter", "fixture", auth_type="oauth",
        config={"twitterUserId": "offline-atomic-destination", "accessToken": "fixture-only"}, db_path=db)
    durations = {}

    def probe(path):
        return synthetic_video_probe(path, durations)

    monkeypatch.setattr(media_compliance, "_source_metadata", probe)
    monkeypatch.setattr(media_compliance, "decode_video", lambda *a, **k: None)
    # Explicit mock of the read-only provider proof projection. Configuration,
    # ownership, fingerprints, registry digests and file seals remain real.
    monkeypatch.setattr(publish_readiness, "_project", lambda *a, **k: {
        "ready": True, "reasonCode": "ready", "evidence": {"capabilities": ["video", "image", "carousel", "first_comment"]}})
    monkeypatch.setattr(preparation_context, "configure_request", lambda accounts, platforms, data, **kw: (data, {}))

    def make(*, count=4, destination=account, source_name=None, lease=False, draft=None):
        source = tmp_path / (source_name or f"SFW source-{len(durations)}.mp4")
        source.write_bytes((source.name + " exact raw source").encode())
        checksum = hashlib.sha256(source.read_bytes()).hexdigest()
        durations[str(source)] = float(10 * count)
        with campaigns._connect(db) as conn, conn:
            fid = conn.execute("INSERT INTO file_records(filename,file_path,filesize,sha256,size_bytes) VALUES(?,?,?,?,?)",
                (source.name, str(source), source.stat().st_size, checksum, source.stat().st_size)).lastrowid
        media = media_groups.create_media_group("Offline parts", primary_video_file_id=fid, db_path=db)
        media_groups.add_media_group_item(media.id, fid, role="video", db_path=db)
        approved = copy.deepcopy(draft or {"message": "Exact approved caption.", "hashtags": ["#offline"]})
        request = {"brief": "Offline test brief.", "options": {}, "schedule": {"startAt": NOW.isoformat() + "Z"},
                   "accountDrafts": {str(destination.id): approved}, "tiktokPostSettings": {}}
        campaign = campaigns.create_campaign(destination.profile_id, media.id, status="preparing",
            selected_account_ids=[destination.id], metadata={campaigns.PREP_REQUEST_KEY: request}, db_path=db)
        if lease:
            campaign = campaigns.claim_next_preparing_campaign(owner="offline-preparer", db_path=db)
        artifacts = []
        for index in range(1, count + 1):
            path = tmp_path / f"campaign-{campaign.id}-part-{index}.mp4"
            path.write_bytes(f"exact output {campaign.id}/{index}".encode())
            durations[str(path)] = 10.0
            metadata = prepared_video_metadata(path, source, destination.platform,
                index=index, count=count, generation=f"fixture-{campaign.id}")
            artifacts.append(campaigns.add_campaign_artifact(campaign.id, artifact_kind="prepared_video",
                source_file_record_id=fid, local_path=str(path), metadata=metadata, db_path=db))
        own_profile = profiles.get_profile(destination.profile_id, db_path=db)
        return SimpleNamespace(campaign=campaign, account=destination, profile=own_profile, draft=approved,
            source=source, media=media, fid=fid, artifacts=artifacts, count=count)

    def finalize(case, **changes):
        arguments = dict(profile=case.profile, accounts=[case.account], media_context={},
            media_files=po._load_media_group_files_via_callback(case.media.id, db_path=db),
            request_data={"notes": "Offline test brief."}, options={}, account_drafts={str(case.account.id): case.draft},
            tiktok_post_settings={}, db_path=db, artifact_payloads_for_platform=lambda values, platform: values,
            generate_account_draft=Mock(side_effect=AssertionError("approved draft was regenerated")),
            job_to_payload=lambda job: job.to_dict(), now_fn=lambda: NOW)
        arguments.update(changes)
        return campaign_prep.finalize_campaign(case.campaign, **arguments)

    return SimpleNamespace(db=db, root=tmp_path, profile=profile, account=account, make=make, finalize=finalize)


def _counts(setup):
    with campaigns._connect(setup.db) as conn:
        return tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES)


def _snapshot(setup):
    with campaigns._connect(setup.db) as conn:
        return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")] for table in TABLES}


def _assert_complete(setup, case, result):
    assert len(result["jobs"]) == case.count, json.dumps(result, ensure_ascii=False)
    queued = [jobs.get_job(job["id"], db_path=setup.db) for job in result["jobs"]]
    labels = [job.payload["publicationGroup"] for job in queued]
    assert len({label["id"] for label in labels}) == 1
    assert [label["index"] for label in labels] == list(range(1, case.count + 1))
    assert all(label["count"] == case.count for label in labels)
    targets = [jobs.list_targets(job.id, db_path=setup.db)[0] for job in queued]
    slots = [destination_pacing.utc_datetime(target.schedule_at) for target in targets]
    assert all(right - left >= timedelta(minutes=30) for left, right in zip(slots, slots[1:]))
    assert max(Counter(slot.date() for slot in slots).values()) <= 3
    assert slots[0] == NOW
    for job, target in zip(queued, targets):
        post = campaigns.get_campaign_post(job.payload["campaignPostId"], db_path=setup.db)
        assert post.last_published_job_id == job.id and post.status == "queued"
        assert target.file_ref == f"campaign_post:{post.id}"
        assert post.draft == case.draft == job.payload["draft"]
        assert job.payload["message"] == "Exact approved caption.\n\n#offline"
    return queued, slots


def test_shared_complete_group_replay_uses_same_jobs_and_slots(setup):
    assert campaign_prep.finalize_campaign is groups.finalize_campaign
    case = setup.make()
    first = setup.finalize(case)
    _assert_complete(setup, case, first)
    before = _snapshot(setup)
    second = setup.finalize(case)
    assert second == first
    assert _snapshot(setup) == before
    assert _counts(setup) == (4, 4, 4)


@pytest.mark.parametrize("stage", ["post", "enqueue", "target", "link"])
@pytest.mark.parametrize("index", [1, 2, 3, 4])
def test_every_sql_install_failure_rolls_back_the_entire_group(setup, stage, index):
    case = setup.make()
    table = {"post": "campaign_posts", "enqueue": "publish_jobs", "target": "publish_job_targets", "link": "campaign_posts"}[stage]
    if stage == "link":
        trigger = f"BEFORE UPDATE OF last_published_job_id ON {table} WHEN NEW.last_published_job_id IS NOT NULL AND (SELECT COUNT(*) FROM publish_jobs)={index}"
    else:
        trigger = f"BEFORE INSERT ON {table} WHEN (SELECT COUNT(*) FROM {table})={index - 1}"
    with campaigns._connect(setup.db) as conn, conn:
        conn.execute(f"CREATE TRIGGER atomic_fault {trigger} BEGIN SELECT RAISE(ABORT,'atomic injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="atomic injected failure"):
        setup.finalize(case)
    assert _counts(setup) == (0, 0, 0), "a committed queued prefix survived"
    with campaigns._connect(setup.db) as conn, conn:
        conn.execute("DROP TRIGGER atomic_fault")
    _assert_complete(setup, case, setup.finalize(case))
    assert _counts(setup) == (4, 4, 4)


@pytest.mark.parametrize("stage", ["post", "enqueue"])
@pytest.mark.parametrize("index", [1, 2, 3, 4])
def test_exception_after_each_helper_step_also_rolls_back(setup, monkeypatch, stage, index):
    case = setup.make()
    owner, name = (campaigns, "add_campaign_post_in_transaction") if stage == "post" else (jobs, "enqueue_job_in_transaction")
    real, seen = getattr(owner, name), 0

    def fail_after(*args, **kwargs):
        nonlocal seen
        result = real(*args, **kwargs)
        seen += 1
        if seen == index:
            raise RuntimeError("after helper fault")
        return result

    monkeypatch.setattr(owner, name, fail_after)
    with pytest.raises(RuntimeError, match="after helper fault"):
        setup.finalize(case)
    assert _counts(setup) == (0, 0, 0)
    monkeypatch.setattr(owner, name, real)
    _assert_complete(setup, case, setup.finalize(case))


def test_suppressed_link_is_not_reported_as_complete(setup):
    case = setup.make()
    with campaigns._connect(setup.db) as conn, conn:
        conn.execute("CREATE TRIGGER ignored_link BEFORE UPDATE OF last_published_job_id ON campaign_posts "
                     "WHEN NEW.last_published_job_id IS NOT NULL BEGIN SELECT RAISE(IGNORE); END")
    with pytest.raises(groups.PublicationGroupConflict, match="links"):
        setup.finalize(case)
    assert _counts(setup) == (0, 0, 0)


@pytest.mark.parametrize("change", ["tail", "target", "count", "draft", "link", "schedule", "media_location"])
def test_incomplete_or_conflicting_existing_group_refuses_without_writes(setup, change):
    case = setup.make()
    queued = setup.finalize(case)["jobs"]
    last = queued[-1]["id"]
    with campaigns._connect(setup.db) as conn, conn:
        if change == "tail":
            # Deliberately damage ONLY the synthetic temporary DB's history.
            conn.execute("DELETE FROM publish_jobs WHERE id=?", (last,))
        elif change == "target":
            conn.execute("DELETE FROM publish_job_targets WHERE job_id=?", (last,))
        elif change == "link":
            conn.execute("UPDATE campaign_posts SET last_published_job_id=NULL WHERE last_published_job_id=?", (last,))
        elif change == "schedule":
            conn.execute("UPDATE publish_job_targets SET schedule_at=? WHERE job_id=?", (NOW.isoformat(), last))
        else:
            payload = json.loads(conn.execute("SELECT payload_json FROM publish_jobs WHERE id=?", (last,)).fetchone()[0])
            if change == "count":
                payload["publicationGroup"]["count"] += 1
            elif change == "draft":
                payload["draft"]["message"] = "Conflicting later-part copy"
            else:
                payload["artifacts"][0]["local_path"] = str(setup.root / "substituted.mp4")
            conn.execute("UPDATE publish_jobs SET payload_json=? WHERE id=?", (json.dumps(payload), last))
    before = _snapshot(setup)
    with pytest.raises(groups.PublicationGroupConflict):
        setup.finalize(case)
    assert _snapshot(setup) == before


@pytest.mark.parametrize("status", ["failed", "cancelled", "succeeded"])
def test_receipt_bearing_labels_never_reset_or_republish(setup, status):
    case = setup.make(count=2)
    first = setup.finalize(case)
    checkpoint = {"previous_failure": {"checkpoint": {"submission_started": True}},
                  "provider_receipt": {"provider": "offline-fixture", "id": "accepted-not-a-retry", "status": "processing"}}
    with campaigns._connect(setup.db) as conn, conn:
        conn.execute("UPDATE publish_job_targets SET status=?,checkpoint_json=?,delivery_state='processing',attempts=7",
                     (status, json.dumps(checkpoint)))
    before = _snapshot(setup)
    replay = setup.finalize(case)
    assert [job["id"] for job in replay["jobs"]] == [job["id"] for job in first["jobs"]]
    assert _snapshot(setup) == before


def _after_admission(monkeypatch, case, callback):
    real = publish_admission.prepare_job_spec
    fired = False

    def changed(spec, **kwargs):
        nonlocal fired
        result = real(spec, **kwargs)
        if not fired and spec.payload["publicationGroup"]["index"] == case.count:
            fired = True
            callback()
        return result

    monkeypatch.setattr(publish_admission, "prepare_job_spec", changed)


@pytest.mark.parametrize("change", ["lease", "attempt", "credential", "account_owner", "source_registry", "source_bytes", "output_bytes"])
def test_late_facts_rotation_is_refused_before_the_first_post_insert(setup, monkeypatch, change):
    case = setup.make(lease=True)

    def mutate():
        if change in {"source_bytes", "output_bytes"}:
            path = case.source if change == "source_bytes" else Path(case.artifacts[-1].local_path)
            path.write_bytes(b"wrong bytes after authoritative verification")
            return
        with campaigns._connect(setup.db) as conn, conn:
            if change in {"lease", "attempt"}:
                metadata = copy.deepcopy(case.campaign.metadata)
                if change == "lease":
                    metadata[campaigns.PREP_LEASE_KEY]["owner"] = "replacement-owner"
                else:
                    metadata[campaigns.PREP_ATTEMPTS_KEY] += 1
                conn.execute("UPDATE campaigns SET metadata_json=? WHERE id=?", (json.dumps(metadata), case.campaign.id))
            elif change == "credential":
                config = {**case.account.config, "accessToken": "rotated-offline-token"}
                conn.execute("UPDATE accounts SET config_json=? WHERE id=?", (json.dumps(config), case.account.id))
            elif change == "account_owner":
                other = conn.execute("INSERT INTO profiles(name,slug,settings_json) VALUES('Other owner','other-owner','{}')").lastrowid
                conn.execute("UPDATE accounts SET profile_id=? WHERE id=?", (other, case.account.id))
            else:
                conn.execute("UPDATE file_records SET size_bytes=size_bytes+1 WHERE id=?", (case.fid,))

    _after_admission(monkeypatch, case, mutate)
    expected = delivery.LeaseLost if change in {"lease", "attempt"} else (publish_admission.AdmissionError, groups.PublicationGroupConflict)
    with pytest.raises(expected):
        setup.finalize(case)
    assert _counts(setup) == (0, 0, 0)


@pytest.mark.parametrize("draft", [
    {"message": "Approved video", "altText": "Must not be discarded"},
    {"message": "Approved options", "networkOptions": {"twitter": {"replySettings": "following"}}},
    {"message": "漢" * 141},
    {"message": "a" * 278, "hashtags": ["#x"]},
])
def test_unsupported_or_weighted_approved_intent_is_blocked_before_reservation(setup, monkeypatch, draft):
    case = setup.make(draft=draft)
    reserve = Mock(side_effect=AssertionError("invalid approved intent reached SQL reservation"))
    monkeypatch.setattr(po, "slot_reservation_lock", reserve)
    result = setup.finalize(case)
    assert not result["jobs"] and result["skipped"]
    assert result["skipped"][0]["textReviewRequired"]
    assert _counts(setup) == (0, 0, 0)
    reserve.assert_not_called()


def test_invalid_requested_timer_cannot_become_publish_now(setup):
    case = setup.make()
    case.campaign.metadata[campaigns.PREP_REQUEST_KEY]["schedule"] = {"startAt": "bad-time"}
    with pytest.raises(destination_pacing.PacingRefused, match="timer"):
        setup.finalize(case)
    assert _counts(setup) == (0, 0, 0)


def test_single_part_requires_the_same_complete_provenance_gate(setup):
    case = setup.make(count=1)
    artifact = case.artifacts[0]
    metadata = copy.deepcopy(artifact.metadata)
    metadata["source_interval_end_seconds"] = 20.0
    with campaigns._connect(setup.db) as conn, conn:
        conn.execute("UPDATE campaign_artifacts SET metadata_json=? WHERE id=?", (json.dumps(metadata), artifact.id))
    result = setup.finalize(case)
    assert not result["jobs"] and result["skipped"]
    assert "complete source interval" in result["skipped"][0]["reason"]
    assert _counts(setup) == (0, 0, 0)


def test_concurrent_exact_finalizers_return_one_complete_group(setup, monkeypatch):
    case = setup.make()
    real = publish_admission.prepare_job_spec
    barrier = threading.Barrier(2)
    joined = threading.local()

    def admitted(spec, **kwargs):
        result = real(spec, **kwargs)
        if spec.payload["publicationGroup"]["index"] == case.count and not getattr(joined, "done", False):
            joined.done = True
            barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(publish_admission, "prepare_job_spec", admitted)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: setup.finalize(case), range(2)))
    assert results[0] == results[1]
    _assert_complete(setup, case, results[0])
    assert _counts(setup) == (4, 4, 4)


def test_restart_replays_persisted_complete_group_without_new_ids(setup):
    case = setup.make()
    first = setup.finalize(case)
    importlib.reload(groups)
    # campaign_prep's public alias remains a valid shared function after reload.
    replay = setup.finalize(case)
    assert replay == first and _counts(setup) == (4, 4, 4)


def test_no_hash_probe_or_drafting_while_reservation_or_writer_is_owned(setup, monkeypatch):
    case = setup.make()
    flags = {"reserved": False, "writer": False}
    real_lock, real_connect = po.slot_reservation_lock, campaigns._connect
    real_verify, real_probe = media_integrity.verify_artifact, media_compliance._source_metadata

    @contextmanager
    def reserved(path):
        with real_lock(path):
            flags["reserved"] = True
            try:
                yield
            finally:
                flags["reserved"] = False

    @contextmanager
    def connection(path=None):
        with real_connect(path) as conn:
            conn.set_trace_callback(lambda sql: flags.update(writer=sql.startswith("BEGIN"))
                if sql.startswith(("BEGIN", "COMMIT", "ROLLBACK")) else None)
            try:
                yield conn
            finally:
                flags["writer"] = False

    def verify(*args, **kwargs):
        assert not flags["reserved"] and not flags["writer"]
        return real_verify(*args, **kwargs)

    def probe(*args, **kwargs):
        assert not flags["reserved"] and not flags["writer"]
        return real_probe(*args, **kwargs)

    monkeypatch.setattr(po, "slot_reservation_lock", reserved)
    monkeypatch.setattr(campaigns, "_connect", connection)
    monkeypatch.setattr(media_integrity, "verify_artifact", verify)
    monkeypatch.setattr(media_compliance, "_source_metadata", probe)
    _assert_complete(setup, case, setup.finalize(case))


def _native_managed_aliases(setup, monkeypatch):
    # Exact native ID plus its configured managed route bridges a new alias.
    monkeypatch.setenv("SOCIAMONIALS_API_KEY", "offline-managed-key")
    monkeypatch.setenv("SAU_SOCIAMONIALS_FALLBACK", "1")
    first = profiles.create_profile("Offline native", db_path=setup.db)
    second = profiles.create_profile("Offline managed", db_path=setup.db)
    mapping = {"workspace_id": "26985", "network": "tiktok", "profile_refs": ["fixture-profile"]}
    native = profiles.add_account(first.id, "tiktok", "native", auth_type="oauth",
        config={"openId": "offline-physical-id", "accessToken": "offline-native-token", "sociamonials": mapping}, db_path=setup.db)
    managed = profiles.add_account(second.id, "tiktok", "managed", auth_type="manual",
        config={"publishingProvider": "sociamonials", "sociamonials": mapping}, db_path=setup.db)
    with campaigns._connect(setup.db) as conn:
        keys = account_identity.canonical_account_keys(conn)
        assert keys[f"account:{native.id}"] == keys[f"account:{managed.id}"]
    return native, managed


def test_native_and_managed_alias_groups_share_three_per_day_and_gap(setup, monkeypatch):
    native, managed = _native_managed_aliases(setup, monkeypatch)
    first, second = setup.make(count=2, destination=native), setup.make(count=2, destination=managed)
    results = [setup.finalize(first), setup.finalize(second)]
    assert all(len(result["jobs"]) == 2 for result in results), results
    with campaigns._connect(setup.db) as conn:
        aliases = destination_pacing.account_refs(conn)
        identity = destination_pacing.identity_for_ref(aliases, f"account:{native.id}")
        slots = [destination_pacing.utc_datetime(value) for value in destination_pacing.booked_slots(conn, aliases, now=NOW)[identity]]
    assert max(Counter(slot.date() for slot in slots).values()) == 3
    assert slots[-1].date() == NOW.date() + timedelta(days=1)
    assert all(right - left >= timedelta(minutes=30) for left, right in zip(slots, slots[1:]))


@pytest.mark.parametrize("status", ["pending", "failed", "cancelled", "succeeded"])
def test_same_physical_alias_cannot_duplicate_a_complete_or_receipted_group(setup, monkeypatch, status):
    native, managed = _native_managed_aliases(setup, monkeypatch)
    case = setup.make(count=2, destination=native)
    original = setup.finalize(case)
    request = copy.deepcopy(case.campaign.metadata[campaigns.PREP_REQUEST_KEY])
    request["accountDrafts"] = {str(managed.id): case.draft}
    other = campaigns.create_campaign(managed.profile_id, case.media.id, status="preparing",
        selected_account_ids=[managed.id], metadata={campaigns.PREP_REQUEST_KEY: request}, db_path=setup.db)
    for artifact in case.artifacts:
        campaigns.add_campaign_artifact(other.id, artifact_kind=artifact.artifact_kind,
            source_file_record_id=case.fid, local_path=artifact.local_path, metadata=artifact.metadata, db_path=setup.db)
    repeated = SimpleNamespace(**{**vars(case), "campaign": other, "account": managed,
                                  "profile": profiles.get_profile(managed.profile_id, db_path=setup.db)})
    checkpoint = {"provider_receipt": {"provider": "offline-fixture", "id": "retained-accepted", "status": "processing"}}
    with campaigns._connect(setup.db) as conn, conn:
        conn.execute("UPDATE publish_job_targets SET status=?,checkpoint_json=?", (status, json.dumps(checkpoint)))
    before = _snapshot(setup)
    result = setup.finalize(repeated)
    assert not result["jobs"] and "already queued" in result["skipped"][0]["reason"]
    assert _snapshot(setup) == before
    assert len(original["jobs"]) == 2


def test_concurrent_aliases_reload_bookings_before_any_insert(setup, monkeypatch):
    native, managed = _native_managed_aliases(setup, monkeypatch)
    cases = [setup.make(count=2, destination=native), setup.make(count=2, destination=managed)]
    barrier, joined = threading.Barrier(2), threading.local()
    real = publish_admission.prepare_job_spec

    def admitted(spec, **kwargs):
        result = real(spec, **kwargs)
        if spec.payload["publicationGroup"]["index"] == 2 and not getattr(joined, "done", False):
            joined.done = True
            barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(publish_admission, "prepare_job_spec", admitted)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(setup.finalize, cases))
    assert all(len(result["jobs"]) == 2 for result in results)
    with campaigns._connect(setup.db) as conn:
        aliases = destination_pacing.account_refs(conn)
        identity = destination_pacing.identity_for_ref(aliases, f"account:{native.id}")
        slots = [destination_pacing.utc_datetime(value) for value in destination_pacing.booked_slots(conn, aliases, now=NOW)[identity]]
    assert len(slots) == len(set(slots)) == 4
    assert max(Counter(slot.date() for slot in slots).values()) == 3
    assert all(right - left >= timedelta(minutes=30) for left, right in zip(slots, slots[1:]))


def test_compatible_requested_start_is_not_shifted_by_other_destinations(setup):
    case = setup.make(count=2)
    _assert_complete(setup, case, setup.finalize(case, offset_index=17))


def test_fresh_runtime_metadata_does_not_change_group_or_job_intent_hash(setup):
    case = setup.make(count=2)
    first = setup.finalize(case)
    with campaigns._connect(setup.db) as conn, conn:
        conn.execute("UPDATE accounts SET last_checked_at='2035-01-02T11:59:00'")
    assert setup.finalize(case) == first
    job = jobs.get_job(first["jobs"][0]["id"], db_path=setup.db)
    targets = jobs.list_targets(job.id, db_path=setup.db)
    original = jobs.JobSpec(platform=job.platform, profile_id=job.profile_id, payload=job.payload,
        targets=[(t.account_ref, t.file_ref, t.schedule_at) for t in targets])
    changed = copy.deepcopy(job.payload)
    changed["publishReadiness"] = {"ready": False, "checkedAt": "new-runtime-proof-time"}
    changed["_publishAdmissionVersion"] += 1
    runtime = jobs.JobSpec(platform=job.platform, profile_id=job.profile_id, payload=changed, targets=original.targets)
    assert jobs._intent_hash(original, None) == jobs._intent_hash(runtime, None)
    changed["draft"]["message"] += "changed approved copy"
    assert jobs._intent_hash(original, None) != jobs._intent_hash(runtime, None)


def test_only_autogenerated_drafts_use_prospective_provider_option_shaping(setup, monkeypatch):
    case = setup.make(count=2)
    # The admission worker owns this API. Its mock is local to this fixture;
    # the real weighted/option validation and atomic transaction still run.
    def prospective(draft, platform, kind, provider):
        assert (platform, kind, provider) == ("twitter", "video", "direct")
        draft = copy.deepcopy(draft)
        draft.pop("altText", None)
        return draft

    shape = Mock(side_effect=prospective)
    monkeypatch.setattr(publish_admission, "prospective_draft_options", shape, raising=False)
    generate = Mock(return_value={"message": "Exact approved caption.", "hashtags": ["#offline"], "altText": "prospective video alt"})
    result = setup.finalize(case, account_drafts={}, generate_account_draft=generate)
    _assert_complete(setup, case, result)
    shape.assert_called_once()
    # Existing durable copy is approved; a restart does not regenerate/reshape it.
    assert setup.finalize(case, account_drafts={}, generate_account_draft=generate) == result
    shape.assert_called_once()
    generate.assert_called_once()


def test_caller_ready_json_cannot_grant_normal_group_admission(setup, monkeypatch):
    case = setup.make(count=1)
    monkeypatch.setattr(publish_readiness, "_project", lambda *a, **k: {"ready": False, "reasonCode": "configured_unverified"})
    with pytest.raises(publish_admission.AdmissionError, match="current verified"):
        setup.finalize(case)
    assert _counts(setup) == (0, 0, 0)


def test_loss_of_old_prep_lease_then_restart_cannot_install_obsolete_parts(setup, monkeypatch):
    case = setup.make(count=2, lease=True)
    old = case.campaign
    with campaigns._connect(setup.db) as conn, conn:
        metadata = copy.deepcopy(old.metadata)
        metadata[campaigns.PREP_LEASE_KEY]["claimedAt"] = "2000-01-01T00:00:00"
        conn.execute("UPDATE campaigns SET metadata_json=? WHERE id=?", (json.dumps(metadata), old.id))
    current = campaigns.claim_next_preparing_campaign(owner="restarted-owner", db_path=setup.db)
    assert current.id == old.id and current.metadata[campaigns.PREP_ATTEMPTS_KEY] == 2
    with pytest.raises(delivery.LeaseLost):
        setup.finalize(case)
    assert _counts(setup) == (0, 0, 0)
    case.campaign = current
    _assert_complete(setup, case, setup.finalize(case))


def test_normal_complete_group_refreshes_expired_destination_evidence_once_outside_queue_lock(setup, monkeypatch):
    case = setup.make(count=3)
    verified = [False]
    monkeypatch.setattr(publish_readiness, "_project", lambda *a, **k: {
        "ready": verified[0], "reasonCode": "ready" if verified[0] else "evidence_expired",
        "evidence": {"capabilities": ["video", "image", "carousel", "first_comment"]}})

    def refresh(account, **kwargs):
        assert _counts(setup) == (0, 0, 0)
        assert account.id == setup.account.id and kwargs["db_path"] == setup.db
        # An actual provider read must not retain the enqueue writer transaction.
        with closing(sqlite3.connect(setup.db, timeout=0.1)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
        verified[0] = True
        return {"ready": True}

    check = Mock(side_effect=refresh)
    monkeypatch.setattr(preparation_context, "ensure_destination_ready", check)
    result = setup.finalize(case)
    _assert_complete(setup, case, result)
    check.assert_called_once()
    # An existing complete group must not replay preparation or verification.
    assert setup.finalize(case) == result
    check.assert_called_once()


def test_permission_denial_before_complete_group_install_leaves_no_prefix(setup, monkeypatch):
    case = setup.make(count=3)
    monkeypatch.setattr(publish_readiness, "_project", lambda *a, **k: {"ready": False, "reasonCode": "evidence_expired"})
    check = Mock(side_effect=ValueError("No verified content-write permission"))
    monkeypatch.setattr(preparation_context, "ensure_destination_ready", check)
    with pytest.raises(publish_admission.AdmissionError, match="current verified"):
        setup.finalize(case)
    assert _counts(setup) == (0, 0, 0)
    check.assert_called_once()
