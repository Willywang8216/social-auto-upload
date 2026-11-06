"""Offline integration gates for complete, immutable preprocessed recovery."""
from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from db import createTable
from myUtils import jobs, media_compliance, platform_limits, profiles, publish_alert_outbox
from myUtils import publish_media_guard as guard
from scripts import preprocess_failed_targets as prep

NOW = datetime(2035, 1, 2, 12)


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Requires ffmpeg and ffprobe")
    path = tmp_path_factory.mktemp("recovery-video") / "source_sfw.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-n", "-f", "lavfi", "-i",
                    "testsrc2=size=640x360:rate=30", "-t", "6.1", "-c:v", "ffv1", str(path)],
                   check=True, capture_output=True, timeout=60)
    return path


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "recovery.db"
    createTable.bootstrap(path)
    profile = profiles.create_profile("Test", db_path=path)
    account = profiles.add_account(profile.id, platform="threads", account_name="fixture", auth_type="oauth",
                                   config={"threadUserId": "fixture"}, db_path=path)
    monkeypatch.setattr(jobs, "_now_iso", lambda: NOW.isoformat())
    monkeypatch.setitem(platform_limits.VIDEO_MAX_SECONDS, "threads", 2.5)
    for key in ("SAU_THREADS_ENABLED", "SAU_SOCIAMONIALS_FALLBACK", "SAU_X_DIRECT_PUBLISH"):
        monkeypatch.delenv(key, raising=False)
    return path, profile, account


def failed_job(database, source):
    path, profile, account = database
    payload = {"message": "Original caption 原稿", "draft": {"privacy": "keep"},
               "artifacts": [{"local_path": str(source), "artifact_kind": "source_video"}]}
    job = jobs.enqueue_job(jobs.JobSpec(platform="threads", profile_id=profile.id, payload=payload,
                          targets=[(f"account:{account.id}", str(source), None)]), db_path=path)
    target = jobs.list_targets(job.id, db_path=path)[0]
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE publish_job_targets SET status='failed',attempts=3,last_error=?,finished_at=? WHERE id=?",
                     ("video duration 6s exceeds the 2s limit", NOW.isoformat(), target.id))
    jobs.recount_job(job.id, db_path=path)
    return job, target


def test_preprocess_then_schedule_all_parts(database, source, tmp_path):
    path, _profile, _account = database
    job, target = failed_job(database, source)
    original = jobs.get_job(job.id, db_path=path).payload
    plan = prep.prepare_plan(db_path=path, target_ids=[target.id], out_dir=tmp_path / "parts", now=NOW,
                             per_day_cap=2)
    assert jobs.list_targets(job.id, db_path=path)[0].status == "failed"
    assert len(plan[0]["prepared_payloads"]) == 3
    assert all(part["message"] == original["message"] for part in plan[0]["prepared_payloads"])
    result = prep.apply_plan(plan, db_path=path)
    assert result == {"applied": 1, "queued_parts": 3, "errors": []}
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute("SELECT account_ref,schedule_at,status,checkpoint_json FROM publish_job_targets ORDER BY id").fetchall()
    assert len(rows) == 3 and all(row[2] == "pending" for row in rows)
    assert len({row[0] for row in rows}) == 1
    assert rows[0][1] < rows[1][1] < rows[2][1]
    assert rows[2][1][:10] != rows[1][1][:10]  # daily capacity retained
    checkpoint = json.loads(rows[0][3])
    assert checkpoint["previous_failure"]["attempts"] == 3
    assert jobs.get_job(job.id, db_path=path).payload == original
    assert prep.apply_plan(plan, db_path=path)["applied"] == 0
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM publish_job_targets").fetchone()[0] == 3


def test_changed_media_rolls_back_entire_part_set(database, source, tmp_path):
    path, _profile, _account = database
    _job, target = failed_job(database, source)
    plan = prep.prepare_plan(db_path=path, target_ids=[target.id], out_dir=tmp_path / "parts", now=NOW)
    Path(plan[0]["prepared_payloads"][1]["artifacts"][0]["local_path"]).write_bytes(b"tampered")
    assert prep.apply_plan(plan, db_path=path)["applied"] == 0
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM publish_job_targets").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM publish_job_targets").fetchone()[0] == "failed"


def test_receipt_never_becomes_retryable(database, source, tmp_path):
    path, _profile, _account = database
    _job, target = failed_job(database, source)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE publish_job_targets SET checkpoint_json=? WHERE id=?", (
                     json.dumps({"provider_receipt": {"provider": "threads", "id": "keep"}}), target.id))
    with pytest.raises(prep.recovery.RecoveryRefused, match="evidence"):
        prep.prepare_plan(db_path=path, target_ids=[target.id], out_dir=tmp_path, now=NOW)
    assert not list(tmp_path.rglob("manifest.json"))


def test_changed_caption_is_refused_before_any_write(database, source, tmp_path):
    path, _profile, _account = database
    _job, target = failed_job(database, source)
    plan = prep.prepare_plan(db_path=path, target_ids=[target.id], out_dir=tmp_path, now=NOW)
    plan[0]["prepared_payloads"][0]["message"] = "different"
    assert prep.apply_plan(plan, db_path=path)["applied"] == 0


def test_publisher_cannot_send_only_first_segment(source, tmp_path, monkeypatch):
    monkeypatch.setitem(platform_limits.VIDEO_MAX_SECONDS, "threads", 2.5)
    payload = {"message": "Keep", "artifacts": [{"local_path": str(source)}]}
    monkeypatch.setattr(guard, "prepare_payloads", lambda *_args, **_kwargs: [{}, {}, {}])
    with pytest.raises(media_compliance.MediaPreparationRequired, match="ALL parts"):
        guard.ensure_single_payload(payload, platform="threads")
    assert payload["artifacts"][0]["local_path"] == str(source)


def test_internal_transcript_and_audio_are_not_media():
    from myUtils import prepared_publishers, sociamonials_fallback
    payload = {"artifacts": [{"local_path": "context.wav"}, {"local_path": "transcript.txt"}]}
    assert prepared_publishers._extract_media(payload) == {"images": [], "videos": [], "covers": []}
    assert sociamonials_fallback.collect_media(payload) == []


def test_failed_alert_is_durable_and_deduplicated(database):
    path, *_ = database
    publish_alert_outbox.enqueue_alert(db_path=path, event_key="stop:1", subject="Stopped", body="Fix and recover")
    assert publish_alert_outbox.flush_due(db_path=path, sender=Mock(return_value=False)) == {"sent": 0, "failed": 1}
    publish_alert_outbox.enqueue_alert(db_path=path, event_key="stop:1", subject="Stopped", body="Fix and recover")
    sender = Mock(return_value=True)
    assert publish_alert_outbox.flush_due(db_path=path, sender=sender,
                                         now=datetime.now(timezone.utc) + timedelta(minutes=2)) == {"sent": 1, "failed": 0}
    assert publish_alert_outbox.flush_due(db_path=path, sender=sender) == {"sent": 0, "failed": 0}
    sender.assert_called_once()


def test_one_bad_target_does_not_abort_unrelated_preprocessing(database, source, tmp_path):
    path, profile, account = database
    _job, good = failed_job(database, source)
    missing = tmp_path / "missing_sfw.mkv"
    job = jobs.enqueue_job(jobs.JobSpec(platform="threads", profile_id=profile.id,
        payload={"message": "Another original caption", "artifacts": [{"local_path": str(missing)}]},
        targets=[(f"account:{account.id}", str(missing), None)]), db_path=path)
    bad = jobs.list_targets(job.id, db_path=path)[0]
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE publish_job_targets SET status='failed',attempts=3,last_error='HTTP 503',finished_at=? WHERE id=?",
                     (NOW.isoformat(), bad.id))
    jobs.recount_job(job.id, db_path=path)
    plan = prep.prepare_plan(db_path=path, target_ids=[bad.id, good.id], out_dir=tmp_path / "parts", now=NOW,
                             isolate_failures=True)
    assert [item["target_id"] for item in plan if "_skipped" not in item] == [good.id]
    assert prep.apply_plan(plan, db_path=path)["applied"] == 1
    assert jobs.get_target(bad.id, db_path=path).status == "failed"


def test_apply_never_encodes_or_probes_under_write_transaction(database, source, tmp_path, monkeypatch):
    path, *_ = database
    _job, target = failed_job(database, source)
    plan = prep.prepare_plan(db_path=path, target_ids=[target.id], out_dir=tmp_path / "parts", now=NOW)
    monkeypatch.setattr(guard, "prepare_payloads", Mock(side_effect=AssertionError("No encoder in transaction")))
    monkeypatch.setattr(media_compliance.media_prep, "probe", Mock(side_effect=AssertionError("No probe in transaction")))
    assert prep.apply_plan(plan, db_path=path) == {"applied": 1, "queued_parts": 3, "errors": []}


def test_changed_token_after_prepared_plan_refuses_apply(database, source, tmp_path):
    path, _profile, account = database
    _job, target = failed_job(database, source)
    plan = prep.prepare_plan(db_path=path, target_ids=[target.id], out_dir=tmp_path / "parts", now=NOW)
    profiles.update_account(account.id, config_patch={"accessToken": "new-fixture-token"}, db_path=path)
    assert prep.apply_plan(plan, db_path=path)["applied"] == 0
    assert jobs.get_target(target.id, db_path=path).status == "failed"


def test_image_batches_are_atomic_and_ordered(database, tmp_path):
    from PIL import Image
    path, profile, account = database
    artifacts = []
    for index in range(21):
        image = tmp_path / f"image-{index}_sfw.png"
        Image.new("RGB", (400, 400), (index * 10, 20, 30)).save(image)
        artifacts.append({"local_path": str(image), "metadata": {"alt_text": f"Keep {index}"}})
    job = jobs.enqueue_job(jobs.JobSpec(platform="threads", profile_id=profile.id,
        payload={"message": "Original image caption", "artifacts": artifacts},
        targets=[(f"account:{account.id}", "images-fixture", None)]), db_path=path)
    target = jobs.list_targets(job.id, db_path=path)[0]
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE publish_job_targets SET status='failed',attempts=3,last_error='HTTP 503',finished_at=? WHERE id=?",
                     (NOW.isoformat(), target.id))
    jobs.recount_job(job.id, db_path=path)
    plan = prep.prepare_plan(db_path=path, target_ids=[target.id], out_dir=tmp_path / "images", now=NOW)
    from myUtils import image_compliance
    max_items = image_compliance.effective_limits("threads")["max_items"]
    expected_parts = (len(artifacts) + max_items - 1) // max_items
    assert len(plan[0]["prepared_payloads"]) == expected_parts
    ordered = [a["metadata"]["alt_text"] for part in plan[0]["prepared_payloads"] for a in part["artifacts"]]
    assert ordered == [f"Keep {index}" for index in range(21)]
    assert prep.apply_plan(plan, db_path=path)["queued_parts"] == expected_parts


def test_preserved_cover_is_not_an_extra_upload(database, source, tmp_path):
    from PIL import Image
    from myUtils.worker import _prepared_artifact_local_paths
    path, *_ = database
    job, target = failed_job(database, source)
    cover = tmp_path / "cover_sfw.jpg"
    Image.new("RGB", (640, 360), "blue").save(cover)
    payload = jobs.get_job(job.id, db_path=path).payload
    payload["artifacts"].append({"local_path": str(cover), "artifact_kind": "thumbnail",
                                  "metadata": {"role": "thumbnail"}})
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE publish_jobs SET payload_json=? WHERE id=?", (json.dumps(payload), job.id))
    plan = prep.prepare_plan(db_path=path, target_ids=[target.id], out_dir=tmp_path / "parts", now=NOW)
    for part in plan[0]["prepared_payloads"]:
        assert cover not in _prepared_artifact_local_paths(part)
        assert any(a.get("artifact_kind") == "thumbnail" and a["metadata"].get("sha256") for a in part["artifacts"])
    assert prep.apply_plan(plan, db_path=path)["queued_parts"] == 3


def _backup_caps(seconds=2.0):
    return {"requires_approval": False,
            "mapping": {"network": "thrd", "profile_refs": ["fixture"], "workspace_id": "34293"},
            "limits": {"policy_version": "fixture-v1", "hosted_video_max_bytes": 1_073_741_824,
                       "external_video_max_bytes": 1_024_000_000, "image_max_bytes": 15_728_640,
                       "max_images": 10, "video_max_seconds": seconds}}


def _backup_compatible_failed_job(database, source):
    path, *_ = database
    job, target = failed_job(database, source)
    payload = jobs.get_job(job.id, db_path=path).payload
    payload["draft"] = {}  # No fixture-only unsupported privacy placeholder.
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE publish_jobs SET payload_json=? WHERE id=?", (json.dumps(payload), job.id))
    return job, target


def test_live_backup_limits_prepare_all_parts_and_saved_plan_round_trips(database, source, tmp_path, monkeypatch):
    path, _profile, account = database
    _job, target = _backup_compatible_failed_job(database, source)
    monkeypatch.setattr(prep.backup_preparation.adapter, "verify_destination", Mock(return_value=_backup_caps()))
    plan = prep.prepare_plan(db_path=path, target_ids=[target.id], out_dir=tmp_path / "parts", now=NOW,
                             backup_account_ids=[account.id])
    assert plan[0]["backup_compatible"] is True
    assert plan[0]["backup_context"]["video_limits"]["max_seconds"] == 2.0
    assert len(plan[0]["prepared_payloads"]) == 4
    stored = json.loads(json.dumps(plan))
    assert prep.apply_plan(stored, db_path=path) == {"applied": 1, "queued_parts": 4, "errors": []}


def test_live_backup_limit_dropping_after_plan_refuses_queue_write(database, source, tmp_path, monkeypatch):
    path, _profile, account = database
    _job, target = _backup_compatible_failed_job(database, source)
    read = Mock(return_value=_backup_caps())
    monkeypatch.setattr(prep.backup_preparation.adapter, "verify_destination", read)
    plan = prep.prepare_plan(db_path=path, target_ids=[target.id], out_dir=tmp_path / "parts", now=NOW,
                             backup_account_ids=[account.id])
    read.return_value = _backup_caps(seconds=1.0)
    result = prep.apply_plan(plan, db_path=path)
    assert result["applied"] == 0 and result["queued_parts"] == 0
    assert "constraints changed" in result["errors"][0]["reason"]
    assert jobs.get_target(target.id, db_path=path).status == "failed"


def test_unavailable_optional_backup_retains_native_profile_and_copy(database, source, tmp_path, monkeypatch):
    path, _profile, account = database
    job, target = failed_job(database, source)
    original = jobs.get_job(job.id, db_path=path).payload
    monkeypatch.setattr(prep.backup_preparation, "resolve_constraints", Mock(side_effect=RuntimeError("Backup grants unavailable")))
    plan = prep.prepare_plan(db_path=path, target_ids=[target.id], out_dir=tmp_path / "parts", now=NOW,
                             backup_account_ids=[account.id])
    assert plan[0]["backup_compatible"] is False and plan[0]["backup_context"] is None
    assert plan[0]["backup_unavailable_reason"] == "Backup grants unavailable"
    assert len(plan[0]["prepared_payloads"]) == 3
    assert all(part["message"] == original["message"] for part in plan[0]["prepared_payloads"])
    assert prep.apply_plan(plan, db_path=path)["applied"] == 1


def test_native_capability_limits_dropping_after_plan_refuses_apply(database, source, tmp_path, monkeypatch):
    path, *_ = database
    _job, target = failed_job(database, source)
    limits = media_compliance.effective_limits("threads")
    read = Mock(return_value={"video_limits": limits, "capability_check": "fixture"})
    monkeypatch.setattr(prep.preparation_context, "account_constraints", read)
    plan = prep.prepare_plan(db_path=path, target_ids=[target.id], out_dir=tmp_path / "parts", now=NOW)
    read.return_value = {"video_limits": {**limits, "max_seconds": 1.0}, "capability_check": "fixture"}
    assert prep.apply_plan(plan, db_path=path)["applied"] == 0
    assert jobs.get_target(target.id, db_path=path).status == "failed"


def test_prepared_recovery_shares_the_finalizer_reservation(database, source, tmp_path, monkeypatch):
    path, _profile, _account = database
    _job, target = failed_job(database, source)
    plan = prep.prepare_plan(db_path=path, target_ids=[target.id], out_dir=tmp_path / "ordered", now=NOW)
    connect = sqlite3.connect
    def short_wait(db, *args, **kwargs):
        if str(db).endswith(".slots.lock"):
            kwargs["timeout"] = .01
        return connect(db, *args, **kwargs)
    with prep.recovery.publish_orchestrator.slot_reservation_lock(path):
        monkeypatch.setattr(prep.recovery.publish_orchestrator.sqlite3, "connect", short_wait)
        result = prep.apply_plan(plan, db_path=path)
        assert result["applied"] == 0 and result["errors"]
        assert jobs.get_target(target.id, db_path=path).status == "failed"
        with closing(connect(path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM publish_job_targets").fetchone()[0] == 1
    assert prep.apply_plan(plan, db_path=path)["queued_parts"] == 3
