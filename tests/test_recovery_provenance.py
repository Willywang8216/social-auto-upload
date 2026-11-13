"""Legacy selected-part provenance and single-output recovery regressions."""
from __future__ import annotations

import copy
import json
import re
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from myUtils import media_compliance as compliance, media_integrity as integrity
from myUtils import prepared_recovery, publish_media_guard as guard
from scripts import preprocess_failed_targets as script


@pytest.fixture(autouse=True)
def isolated_media(monkeypatch):
    def probe(path):
        count = re.search(r"of(\d+)-", Path(path).name)
        return {"codec": "h264" if Path(path).suffix == ".mp4" else "ffv1", "pix_fmt": "yuv420p",
            "profile": "High", "field_order": "progressive", "sample_aspect_ratio": "1:1",
            "format_name": "mov,mp4", "major_brand": "isom", "width": 640, "height": 360,
            "fps": 30, "has_audio": False, "duration": 4.0 / int(count[1]) if count else 4.0}
    monkeypatch.setattr(compliance.media_prep, "probe", probe)
    monkeypatch.setattr(compliance, "decode_video", Mock())
    monkeypatch.setattr(compliance, "_encode", lambda src, output, *args, **kwargs: output.write_bytes(b"encoded" + str(output).encode()))


def payload(tmp_path, *, legacy=False, span=True, valid=False):
    path = tmp_path / ("selected.mp4" if valid else "selected.mkv")
    path.write_bytes(b"exact selected variant bytes")
    metadata = {"content_variant": "sfw", "unwatermarked": False}
    if legacy:
        metadata.update(part_index=1, part_count=2, preparation_group="parent", source_path="original-source.mkv")
        if span:
            metadata.update(source_start_seconds=10.0, source_end_seconds=14.0,
                            source_interval_start_seconds=0.0, source_interval_end_seconds=18.0)
    return {"message": "  Original approved caption 原稿  ", "draft": {"privacy": "keep"},
        "_preserveApprovedText": True, "artifacts": [{"local_path": str(path), "source_file_record_id": 9,
            "artifact_kind": "video", "metadata": metadata}]}


def prepare(original, tmp_path, **kwargs):
    return guard.prepare_payloads(original, platform="threads", out_dir=tmp_path / "outputs", allow_reprepare=True, **kwargs)


def outputs(parts):
    return [artifact for part in parts for artifact in part["artifacts"]]


def check(original, parts, **kwargs):
    prepared_recovery.validate_video_provenance(original, outputs(parts), platform="threads", **kwargs)


@pytest.mark.parametrize("index,count", [(1, 2), (2, 2), (1, 1)])
def test_legacy_segment_without_interval_never_becomes_full_source(tmp_path, index, count):
    original = payload(tmp_path, legacy=True, span=False)
    original["artifacts"][0]["metadata"].update(part_index=index, part_count=count)
    before = copy.deepcopy(original)
    with pytest.raises(compliance.MediaPreparationError, match="provable original source interval"):
        prepare(original, tmp_path)
    assert original == before and not (tmp_path / "outputs").exists()


def test_valid_legacy_pending_probe_does_not_require_sha_or_generate(tmp_path, monkeypatch):
    original = payload(tmp_path, legacy=True, span=False, valid=True)
    before = copy.deepcopy(original)
    state = {"job": {"platform": "threads"}, "account": {}}
    monkeypatch.setattr(script.profiles, "_row_to_account", lambda row: type("Account", (), {"config": {}})())
    encoder = Mock(side_effect=AssertionError("Pending valid legacy must not encode"))
    monkeypatch.setattr(compliance, "_encode", encoder)
    script._validate_existing_pending(original, state, False, None, compliance.effective_limits("threads"))
    assert original == before and prepare(original, tmp_path)[0]["artifacts"] == original["artifacts"]
    encoder.assert_not_called()


def test_single_derivative_preserves_original_membership_and_absolute_span(tmp_path):
    original = payload(tmp_path, legacy=True)
    parts = prepare(original, tmp_path)
    assert len(parts) == 1
    artifact = parts[0]["artifacts"][0]
    metadata = artifact["metadata"]
    assert metadata["part_index"] == metadata["part_count"] == 1
    assert metadata["source_start_seconds"] == 10 and metadata["source_end_seconds"] == 14
    assert metadata["source_parent"]["part_index"] == 1 and metadata["source_parent"]["part_count"] == 2
    assert metadata["original_source_interval_start_seconds"] == 0
    assert metadata["original_source_interval_end_seconds"] == 18
    assert metadata["source_sha256"] is None  # Do not invent a raw SHA from segment bytes.
    assert metadata["input_sha256"] == integrity.sha256_file(original["artifacts"][0]["local_path"])
    assert metadata["source_identity"]["source_file_record_id"] == 9
    assert parts[0]["message"] == original["message"] and parts[0]["draft"] == original["draft"]
    check(original, parts)


def test_recovery_rejects_relabelled_legacy_one_of_two_even_one_output(tmp_path):
    original = payload(tmp_path, legacy=True, span=False)
    forged = copy.deepcopy(original)
    # Different derivative bytes/path and claimed complete generation used to
    # bypass validate_parts because len(parts)==1.
    output = tmp_path / "forged.mp4"
    output.write_bytes(b"forged derivative")
    forged["artifacts"][0].update(local_path=str(output), artifact_kind="prepared_video")
    forged["artifacts"][0]["metadata"].update(sha256=integrity.sha256_file(output), part_index=1, part_count=1,
        input_path=original["artifacts"][0]["local_path"], input_sha256=integrity.sha256_file(original["artifacts"][0]["local_path"]),
        preparation_group="new", prepared_for=["threads"], source_start_seconds=0, source_end_seconds=4,
        source_interval_start_seconds=0, source_interval_end_seconds=4)
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE publish_jobs (id INTEGER,payload_json TEXT,platform TEXT)")
        conn.execute("INSERT INTO publish_jobs VALUES (1,?,'threads')", (json.dumps(original),))
        target = {"job_id": 1, "checkpoint_json": "{}", "file_ref": original["artifacts"][0]["local_path"]}
        with pytest.raises(ValueError, match="original source interval"):
            prepared_recovery.validate_parts(conn, target, [forged], ["2035-01-01T12:00:00"])


@pytest.mark.parametrize("change", ["gap", "overlap", "tail", "start", "drop"])
def test_all_outputs_cover_exact_selected_span_without_gap_overlap_or_tail(tmp_path, change):
    original = payload(tmp_path, legacy=True)
    parts = prepare(original, tmp_path, video_limits={"max_seconds": 2.5})
    assert len(parts) == 2
    check(original, parts)
    if change == "gap":
        parts[1]["artifacts"][0]["metadata"]["source_start_seconds"] += 0.5
    elif change == "overlap":
        parts[1]["artifacts"][0]["metadata"]["source_start_seconds"] -= 0.5
    elif change == "tail":
        parts[-1]["artifacts"][0]["metadata"]["source_end_seconds"] -= 0.5
    elif change == "start":
        parts[0]["artifacts"][0]["metadata"]["source_start_seconds"] += 0.5
    else:
        parts.pop()
    with pytest.raises(ValueError):
        check(original, parts)


@pytest.mark.parametrize("field,value", [("source_file_record_id", 10), ("content_variant", "nsfw"),
    ("unwatermarked", True), ("source_sha256", "0" * 64), ("source_path", "another-original"),
    ("source_parent", {}), ("source_is_complete", True)])
def test_source_identity_variant_and_parent_cannot_change(tmp_path, field, value):
    original = payload(tmp_path, legacy=True)
    parts = prepare(original, tmp_path)
    artifact = parts[0]["artifacts"][0]
    if field == "source_file_record_id":
        artifact[field] = value
    else:
        artifact["metadata"][field] = value
    with pytest.raises(ValueError):
        check(original, parts)


def test_single_output_tail_is_checked_and_known_raw_must_be_complete(tmp_path):
    original = payload(tmp_path)
    parts = prepare(original, tmp_path)
    check(original, parts, probe_sources=True)
    assert len(parts) == 1
    parts[0]["artifacts"][0]["metadata"]["source_end_seconds"] -= 1
    with pytest.raises(ValueError, match="complete source interval"):
        check(original, parts)


def test_full_raw_source_can_generate_all_parts_but_not_forged_duration(tmp_path):
    original = payload(tmp_path)
    parts = prepare(original, tmp_path, video_limits={"max_seconds": 2.5})
    check(original, parts, probe_sources=True)
    assert len(parts) == 2
    for artifact in outputs(parts):
        metadata = artifact["metadata"]
        metadata.update(source_duration_seconds=3.0, source_interval_end_seconds=3.0)
    with pytest.raises(ValueError, match="not complete"):
        check(original, parts, probe_sources=True)


def test_source_verification_precedes_conversion_and_source_sha_is_not_output_sha(tmp_path, monkeypatch):
    original = payload(tmp_path)
    original["artifacts"][0]["sha256"] = "0" * 64
    encoder = Mock(side_effect=AssertionError("Wrong source must not convert"))
    monkeypatch.setattr(compliance, "_encode", encoder)
    with pytest.raises(compliance.MediaPreparationError, match="checksum mismatch"):
        prepare(original, tmp_path)
    encoder.assert_not_called()


def test_unchanged_selected_prepared_subset_is_not_forced_to_include_raw_tail(tmp_path):
    original = payload(tmp_path, legacy=True, valid=True)
    original["artifacts"][0]["metadata"]["sha256"] = integrity.sha256_file(original["artifacts"][0]["local_path"])
    parts = prepare(original, tmp_path)
    assert parts[0]["artifacts"] == original["artifacts"]
    check(original, parts)


@pytest.mark.parametrize("legacy", [False, True])
def test_exact_url_source_materialization_retains_identity_not_basename(tmp_path, legacy):
    restored = payload(tmp_path, legacy=legacy)
    artifact = restored["artifacts"][0]
    artifact["public_url"] = "https://cdn.invalid/exact/selected.mkv"
    artifact["sha256"] = integrity.sha256_file(artifact["local_path"])
    registered = copy.deepcopy(restored)
    registered["artifacts"][0].pop("local_path")
    parts = prepare(restored, tmp_path)
    check(registered, parts, probe_sources=True)
    assert outputs(parts)[0]["metadata"]["source_identity"]["source_public_url"] == artifact["public_url"]
    registered["artifacts"][0]["public_url"] = "https://cdn.invalid/another/selected.mkv"
    with pytest.raises(ValueError, match="substituted source identity"):
        check(registered, parts)


def test_repeated_recovery_keeps_ancestral_parent_not_derivative_number(tmp_path):
    original = payload(tmp_path, legacy=True)
    first = prepare(original, tmp_path)
    # New tighter constraints split the derivative, without changing parent 1/2.
    second = prepare(first[0], tmp_path, video_limits={"max_seconds": 2.5})
    assert len(second) == 2
    for artifact in outputs(second):
        assert artifact["metadata"]["source_parent"] == outputs(first)[0]["metadata"]["source_parent"]
        assert artifact["metadata"]["source_parent"]["part_count"] == 2
        assert artifact["metadata"]["reprepared_from"]["part_count"] == 1
    check(first[0], second)


@pytest.mark.parametrize("null_sha", [False, True])
def test_valid_legacy_pending_plan_is_database_noop_with_original_slot(tmp_path, monkeypatch, null_sha):
    from contextlib import closing
    from datetime import datetime
    from db import createTable
    from myUtils import jobs, profiles
    database = tmp_path / "pending.db"
    createTable.bootstrap(database)
    profile = profiles.create_profile("Legacy pending", db_path=database)
    account = profiles.add_account(profile.id, platform="threads", account_name="fixture", auth_type="oauth",
        config={"threadUserId": "fixture"}, db_path=database)
    original = payload(tmp_path, legacy=True, span=False, valid=True)
    if null_sha:
        original["artifacts"][0]["metadata"]["sha256"] = None
    slot = "2035-01-02T14:00:00"
    now = datetime(2035, 1, 2, 12)
    monkeypatch.setattr(jobs, "_now_iso", lambda: now.isoformat())
    # Model a pre-admission legacy row, not a newly admitted/checkpointed intent.
    import json
    with closing(sqlite3.connect(database)) as conn, conn:
        job_id = conn.execute("INSERT INTO publish_jobs (idempotency_key,platform,profile_id,payload_json) VALUES (?,?,?,?)",
            ("legacy-pending", "threads", profile.id, json.dumps(original))).lastrowid
        target_id = conn.execute("INSERT INTO publish_job_targets (job_id,account_ref,file_ref,schedule_at) VALUES (?,?,?,?)",
            (job_id, f"account:{account.id}", original["artifacts"][0]["local_path"], slot)).lastrowid
    target = jobs.get_target(target_id, db_path=database)
    with closing(sqlite3.connect(database)) as conn:
        before = "\n".join(conn.iterdump())
    monkeypatch.setattr(guard, "prepare_payloads", Mock(side_effect=AssertionError("Legacy valid pending must not prepare")))
    plan = script.prepare_plan(db_path=database, target_ids=[target.id], include_unstarted_pending=True,
        now=now, out_dir=tmp_path / "outputs")
    assert plan == [{"_unchanged_pending": [target.id]}]
    assert script.apply_plan(plan, db_path=database, include_unstarted_pending=True, target_ids=[target.id]) == {
        "applied": 0, "queued_parts": 0, "errors": []}
    with closing(sqlite3.connect(database)) as conn:
        assert "\n".join(conn.iterdump()) == before
    assert jobs.get_target(target.id, db_path=database).schedule_at == slot
    assert not (tmp_path / "outputs").exists()


def test_incomplete_raw_decode_never_becomes_a_complete_generation(tmp_path, monkeypatch):
    original = payload(tmp_path)
    monkeypatch.setattr(compliance, "decode_video", Mock(side_effect=compliance.MediaPreparationError("late source decode failure")))
    encoder = Mock(side_effect=AssertionError("An incomplete raw source must not encode"))
    monkeypatch.setattr(compliance, "_encode", encoder)
    with pytest.raises(compliance.MediaPreparationError, match="late source decode"):
        prepare(original, tmp_path)
    encoder.assert_not_called()
    assert not (tmp_path / "outputs").exists()


def test_complete_parts_cannot_be_scheduled_in_reversed_source_order(tmp_path):
    original = payload(tmp_path, legacy=True)
    parts = prepare(original, tmp_path, video_limits={"max_seconds": 2.5})
    with pytest.raises(ValueError, match="reordered"):
        check(original, list(reversed(parts)))
