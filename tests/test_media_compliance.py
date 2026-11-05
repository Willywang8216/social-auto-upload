"""Real ffmpeg and fail-closed tests for final media preparation."""
from __future__ import annotations

import copy
import math
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from myUtils import media_compliance as prep, media_prep, platform_limits


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required")
    path = tmp_path_factory.mktemp("source") / "source.mkv"
    subprocess.run([
        "ffmpeg", "-v", "error", "-n", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=900:sample_rate=96000", "-t", "6.1",
        "-c:v", "ffv1", "-c:a", "pcm_s16le", "-ac", "2", str(path),
    ], check=True, capture_output=True, timeout=60)
    return path


def test_compresses_before_size_only_splitting(source, tmp_path):
    original = prep.sha256_file(source)
    parts = prep.prepare_video(source, tmp_path, platform="threads", limits={"max_bytes": 400_000})
    assert len(parts) == 1
    part = parts[0]
    assert part.path != source
    assert part.path.stat().st_size <= 400_000 < source.stat().st_size
    meta = prep.validate_video(part.path, platform="threads", limits={"max_bytes": 400_000})
    assert meta["codec"] == "h264"
    assert meta["profile"] == "High"
    assert meta["audio_codec"] == "aac" and meta["audio_profile"] == "LC"
    assert meta["audio_sample_rate"] == 48000
    assert prep.sha256_file(source) == original
    assert part.metadata["size_bytes"] == part.path.stat().st_size
    assert part.metadata["max_duration_seconds"] == meta["duration"]


def test_splits_complete_content_and_checks_actual_outputs(source, tmp_path):
    parts = prep.prepare_video(source, tmp_path, platform="threads", limits={"max_seconds": 2.5})
    assert len(parts) == 3
    assert [part.metadata["part_index"] for part in parts] == [1, 2, 3]
    assert all(part.metadata["part_count"] == 3 for part in parts)
    assert parts[0].metadata["source_start_seconds"] == 0
    assert parts[-1].metadata["source_end_seconds"] == media_prep.probe(source)["duration"]
    durations = []
    for part in parts:
        meta = prep.validate_video(part.path, platform="threads", limits={"max_seconds": 2.5})
        durations.append(meta["duration"])
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(part.path), "-f", "null", "-"],
                       check=True, capture_output=True, timeout=60)
    assert abs(sum(durations) - media_prep.probe(source)["duration"]) < 0.2
    artifacts = [{"id": index, "local_path": str(part.path), "metadata": part.metadata}
                 for index, part in enumerate(parts, 1)]
    assert prep.select_prepared_parts(artifacts, "threads") == artifacts
    with pytest.raises(prep.MediaPreparationError, match="Incomplete"):
        prep.select_prepared_parts(artifacts[:-1], "threads")


def test_extensionless_source_is_encoded_not_renamed(source, tmp_path):
    extensionless = tmp_path / "source-no-extension"
    shutil.copyfile(source, extensionless)
    parts = prep.prepare_video(extensionless, tmp_path / "outputs", platform="instagram")
    assert parts[0].path.suffix == ".mp4"
    assert prep.validate_video(parts[0].path, platform="instagram")["codec"] == "h264"
    assert media_prep.probe(extensionless)["codec"] == "ffv1"


def test_versions_are_immutable_and_valid_media_is_reused(source, tmp_path):
    first = prep.prepare_video(source, tmp_path, platform="threads")
    before = prep.sha256_file(first[0].path)
    second = prep.prepare_video(source, tmp_path, platform="threads")
    assert first[0].path != second[0].path
    assert prep.sha256_file(first[0].path) == before
    with patch.object(prep, "_encode", side_effect=AssertionError("already compliant")):
        reused = prep.prepare_video(first[0].path, tmp_path, platform="threads")
    assert reused[0].path == first[0].path


def test_probe_encoder_and_checksum_fail_closed(source, tmp_path):
    with patch.object(media_prep, "probe", side_effect=RuntimeError("probe failed")):
        with pytest.raises(prep.MediaPreparationError, match="Cannot probe"):
            prep.prepare_video(source, tmp_path, platform="threads")
    with patch.object(prep, "_encode", side_effect=prep.MediaPreparationError("encoder failed")):
        with pytest.raises(prep.MediaPreparationError, match="encoder failed"):
            prep.prepare_video(source, tmp_path, platform="threads")
    assert not list(tmp_path.rglob("manifest.json"))
    good = prep.prepare_video(source, tmp_path, platform="threads")[0]
    with pytest.raises(prep.MediaPreparationError, match="checksum"):
        prep.validate_video(good.path, platform="threads", expected_sha256="0" * 64)


def test_invalid_metadata_does_not_mean_unlimited(tmp_path):
    path = tmp_path / "bad.mp4"
    path.write_bytes(b"bad")
    for field in ("duration", "fps", "width", "height"):
        for value in (0, float("nan"), float("inf"), True):
            meta = {"codec": "h264", "duration": 1, "fps": 30, "width": 640, "height": 360, field: value}
            with patch.object(media_prep, "probe", return_value=meta):
                with pytest.raises(prep.MediaPreparationError, match="Invalid"):
                    prep.prepare_video(path, tmp_path, platform="threads")


def test_limits_account_transport_and_permissions():
    assert prep.effective_limits("telegram")["max_bytes"] == 50_000_000
    assert prep.effective_limits("telegram", account_config={"authMode": "user"})["max_bytes"] == 2_000_000_000
    assert prep.effective_limits("tiktok")["max_seconds"] == 180
    assert prep.effective_limits("tiktok", account_config={"creator_info": {"max_video_post_duration_sec": 75}})["max_seconds"] == 75
    assert prep.effective_limits("tiktok", account_config={"creator_info": {"max_video_post_duration_sec": 900}})["max_seconds"] == 600
    assert prep.effective_limits("youtube")["max_seconds"] == 900
    assert prep.effective_limits("youtube", account_config={"channelStatus": {"longUploadsStatus": "allowed"}})["max_seconds"] == 43200
    assert prep.effective_limits("discord")["max_bytes"] == 20_000_000
    assert prep.effective_limits("nw_sw_blog")["max_bytes"] is None
    with pytest.raises(ValueError):
        prep.effective_limits("unknown")
    for value in (True, 0, math.nan, math.inf):
        with pytest.raises(ValueError):
            prep.effective_limits("threads", limits={"max_seconds": value})
    assert prep.effective_limits("threads", limits={"max_seconds": 900})["max_seconds"] == 300


def test_geometry_preserves_framing_and_tightens_x_canvas():
    limits = prep.effective_limits("twitter")
    filters = prep.build_filters({"width": 720, "height": 1280, "sample_aspect_ratio": "1:1"}, limits, 4_000_000)
    assert "scale=576:1024" in filters and "pad=576:1024" in filters
    assert "crop=" not in filters
    limits = prep.effective_limits("tiktok")
    filters = prep.build_filters({"width": 1920, "height": 200, "sample_aspect_ratio": "1:1"}, limits, 4_000_000)
    assert "pad=1920:360" in filters
    assert "crop=" not in filters


def _fake_meta(**updates):
    return {"codec": "h264", "pix_fmt": "yuv420p", "profile": "High", "field_order": "progressive",
            "sample_aspect_ratio": "1:1", "format_name": "mov,mp4,m4a,3gp,3g2,mj2", "major_brand": "isom",
            "duration": 1, "width": 640, "height": 360, "fps": 30, "has_audio": False, **updates}


def test_output_size_is_measured_and_compression_is_bounded(tmp_path):
    src = tmp_path / "source.mkv"
    src.write_bytes(b"source" * 100_000)
    rates = []
    def encode(_src, output, _meta, _limits, **kwargs):
        rates.append(kwargs["bitrate"])
        output.write_bytes(b"x" * (300_000 if len(rates) == 1 else 180_000))
    with patch.object(prep, "_encode", side_effect=encode), patch.object(media_prep, "probe", return_value=_fake_meta()), patch.object(prep, "decode_video"):
        parts = prep.prepare_video(src, tmp_path, platform="threads", limits={"max_bytes": 200_000})
    assert len(rates) == 2 and rates[1] < rates[0]
    assert parts[0].metadata["size_bytes"] == 180_000
    rates.clear()
    def always_large(_src, output, _meta, _limits, **kwargs):
        rates.append(kwargs["bitrate"])
        output.write_bytes(b"x" * 300_000)
    with patch.object(prep, "_encode", side_effect=always_large), patch.object(media_prep, "probe", return_value=_fake_meta()):
        with pytest.raises(prep.MediaPreparationError, match="violates"):
            prep.prepare_video(src, tmp_path / "failed", platform="threads", limits={"max_bytes": 200_000})
    assert len(rates) == prep.MAX_ENCODE_ATTEMPTS
    assert not list((tmp_path / "failed").rglob("*.mp4"))
    assert src.is_file()


def test_new_generation_cannot_merge_old_parts(source, tmp_path):
    parts = prep.prepare_video(source, tmp_path, platform="threads", limits={"max_seconds": 2.5})
    artifacts = [{"id": index, "metadata": copy.deepcopy(part.metadata)} for index, part in enumerate(parts, 1)]
    newer = copy.deepcopy(artifacts[0])
    newer["id"] = 99
    newer["metadata"]["preparation_group"] = "new-incomplete-generation"
    with pytest.raises(prep.MediaPreparationError, match="Incomplete"):
        prep.select_prepared_parts([*artifacts, newer], "threads")
    altered = copy.deepcopy(artifacts)
    altered[1]["metadata"]["source_start_seconds"] += 1
    with pytest.raises(prep.MediaPreparationError, match="contiguous"):
        prep.select_prepared_parts(altered, "threads")


def test_arbitrary_overrides_cannot_relax_current_account_or_transport():
    assert prep.effective_limits("telegram", limits={"max_bytes": 2_000_000_000})["max_bytes"] == 50_000_000
    assert prep.effective_limits("youtube", limits={"max_seconds": 43200})["max_seconds"] == 900
    assert prep.effective_limits("youtube", account_config={"youtubeLongUploadsAllowed": True})["max_seconds"] == 900
    assert prep.effective_limits("tiktok", account_config={"max_video_post_duration_sec": 600})["max_seconds"] == 180
    assert prep.effective_limits("tiktok", account_config={"creator_info": {"max_video_post_duration_sec": 60}},
                                 limits={"max_seconds": 600})["max_seconds"] == 60
    assert prep.effective_limits("twitter", limits={"max_aspect_ratio": 20})["max_aspect_ratio"] == 3
    assert prep.effective_limits("twitter", limits={"min_aspect_ratio": 0.1})["min_aspect_ratio"] == 1 / 3
    assert prep.effective_limits("telegram", limits={"max_bytes": None})["max_bytes"] == 50_000_000


def test_backup_policy_explicit_conservative_and_no_permission_inference():
    assert prep.effective_limits("youtube", backup_compatible=True)["max_bytes"] == 1_000_000_000
    assert prep.effective_limits("facebook", backup_compatible=True)["max_bytes"] == 1_000_000_000
    assert prep.effective_limits("telegram", backup_compatible=True)["max_bytes"] == 50_000_000
    assert prep.effective_limits("telegram", account_config={"authMode": "user"}, backup_compatible=True)["max_bytes"] == 1_000_000_000
    assert prep.effective_limits("twitter", backup_compatible=True)["max_seconds"] == 140
    assert prep.effective_limits("twitter", backup_compatible=True)["max_bytes"] == 512_000_000
    assert prep.effective_limits("bluesky", backup_compatible=True)["max_bytes"] == 300_000_000
    assert prep.effective_limits("youtube", backup_compatible=True, limits={"max_bytes": 256_000_000_000})["max_bytes"] == 1_000_000_000
    with pytest.raises(ValueError, match="backup"):
        prep.effective_limits("nw_sw_blog", backup_compatible=True)


def test_compatible_sources_can_be_promoted_without_lossy_reencoding(source, tmp_path):
    ready = prep.prepare_video(source, tmp_path / "first", platform="threads")[0]
    digest = prep.sha256_file(ready.path)
    with patch.object(prep, "_encode", side_effect=AssertionError("compatible bytes must only be copied")):
        promoted = prep.prepare_video(ready.path, tmp_path / "second", platform="threads", force_generation=True)[0]
    assert promoted.path != ready.path
    assert prep.sha256_file(promoted.path) == digest
    assert (promoted.path.parent / "manifest.json").is_file()


def test_explicit_immutable_recovery_preserves_variant_origin_and_intervals(source, tmp_path):
    from myUtils import publish_media_guard as guard
    raw = tmp_path / "raw-source.mkv"
    shutil.copyfile(source, raw)
    older = prep.prepare_video(raw, tmp_path / "older", platform="threads")[0]
    metadata = copy.deepcopy(older.metadata)
    span = metadata["source_end_seconds"] - metadata["source_start_seconds"]
    metadata.update(source_start_seconds=42.0, source_end_seconds=42.0 + span,
                    source_interval_start_seconds=0.0, source_interval_end_seconds=100.0,
                    part_index=2, part_count=3, content_variant="sfw", unwatermarked=False)
    payload = {"message": "  Approved copy.  ", "draft": {"message": "  Approved copy.  "},
        "privacy": "private", "artifacts": [{"id": 9, "artifact_kind": "prepared_video",
        "source_file_record_id": 7, "local_path": str(older.path),
        "public_url": "https://cdn.invalid/older.mp4", "metadata": metadata}]}
    old_hash = prep.sha256_file(older.path)
    raw.write_bytes(b"different raw content; recovery MUST NOT reuse it")
    with pytest.raises(prep.MediaPreparationError, match="duration"):
        guard.prepare_payloads(payload, platform="threads", out_dir=tmp_path / "refused", video_limits={"max_seconds": 2.5})
    repaired = guard.prepare_payloads(payload, platform="threads", out_dir=tmp_path / "repaired",
                                     video_limits={"max_seconds": 2.5}, allow_reprepare=True)
    assert len(repaired) == 3
    assert prep.sha256_file(older.path) == old_hash
    result_meta = [part["artifacts"][0]["metadata"] for part in repaired]
    assert result_meta[0]["source_start_seconds"] == 42.0
    assert result_meta[-1]["source_end_seconds"] == 42.0 + span
    for part, meta in zip(repaired, result_meta):
        assert part["message"] == payload["message"] and part["privacy"] == payload["privacy"]
        assert meta["source_sha256"] == metadata["source_sha256"]
        assert meta["source_path"] == metadata["source_path"]
        assert meta["input_path"] == str(older.path) and meta["input_sha256"] == old_hash
        assert meta["reprepared_from"]["path"] == str(older.path)
        assert meta["reprepared_from"]["part_index"] == 2 and meta["reprepared_from"]["part_count"] == 3
        assert meta["content_variant"] == "sfw" and meta["unwatermarked"] is False
        assert part["artifacts"][0]["public_url"] != payload["artifacts"][0]["public_url"]
        guard.validate_payload(part, platform="threads")


def test_recovery_refuses_tampered_media_and_overlong_copy_before_encoding(source, tmp_path):
    from myUtils import publish_media_guard as guard
    older = prep.prepare_video(source, tmp_path / "older", platform="threads")[0]
    payload = {"message": "x" * 501, "artifacts": [{"artifact_kind": "prepared_video",
        "local_path": str(older.path), "metadata": copy.deepcopy(older.metadata)}]}
    with patch.object(prep, "_encode", side_effect=AssertionError("must reject copy first")):
        with pytest.raises(ValueError, match="limit"):
            guard.prepare_payloads(payload, platform="threads", allow_reprepare=True,
                                   out_dir=tmp_path / "refused", video_limits={"max_seconds": 2.5})
    payload["message"] = "Approved"
    payload["artifacts"][0]["metadata"]["sha256"] = "0" * 64
    with patch.object(prep, "_encode", side_effect=AssertionError("must verify bytes first")):
        with pytest.raises(prep.MediaPreparationError, match="checksum"):
            guard.prepare_payloads(payload, platform="threads", allow_reprepare=True, out_dir=tmp_path / "refused")
    assert not (tmp_path / "refused").exists()


def test_validation_only_never_encodes_decodes_or_writes_in_transaction(source, tmp_path):
    import sqlite3
    from myUtils import publish_media_guard as guard
    part = prep.prepare_video(source, tmp_path / "ready", platform="threads")[0]
    payload = {"message": "Approved", "artifacts": [{"artifact_kind": "prepared_video",
        "local_path": str(part.path), "metadata": part.metadata}]}
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    with sqlite3.connect(":memory:") as conn, patch.object(prep, "_encode", side_effect=AssertionError("no encode")), patch.object(prep, "decode_video", side_effect=AssertionError("no long decode in write transaction")):
        conn.execute("BEGIN IMMEDIATE")
        guard.validate_payload(payload, platform="threads")
        assert conn.in_transaction
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before


def test_final_watermark_is_normalized_and_every_final_part_validated(source, tmp_path, monkeypatch):
    from PIL import Image
    from myUtils import campaign_media_prep, profiles, video_artifacts
    raw = tmp_path / "clip.mkv"
    shutil.copyfile(source, raw)
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (64, 64), (255, 0, 0, 180)).save(logo)
    registered = []
    monkeypatch.setattr(campaign_media_prep.campaign_store, "get_campaign", lambda *a, **k: type("C", (), {"selected_account_ids": []})())
    monkeypatch.setattr(campaign_media_prep.profile_registry, "list_accounts", lambda **k: [])
    monkeypatch.setattr(campaign_media_prep.campaign_store, "add_campaign_artifact_set", lambda cid, rows, **k: registered.extend(rows))
    monkeypatch.setattr(campaign_media_prep.media_pipeline, "GENERATED_MEDIA_ROOT", tmp_path / "generated" / "campaigns")
    profile = profiles.Profile(1, "Test", "test", settings={})
    context = campaign_media_prep.prepare_campaign_media_artifacts(1, profile,
        [{"file_record_id": 7, "file_path": str(raw), "role": "video"}],
        {"watermark": {"imagePath": str(logo)}, "uploadToRemote": False,
         "_preparationLimits": {"threads": {"max_seconds": 2.5, "max_bytes": 500_000}}},
        selected_platforms={"threads"}, db_path=tmp_path / "unused.db")
    assert len(registered) == 3 and registered == context["preparedArtifacts"]
    assert [row["metadata"]["part_index"] for row in registered] == [1, 2, 3]
    assert all(row["artifact_kind"] == "prepared_video" for row in registered)
    for row in registered:
        meta = row["metadata"]
        assert meta["input_path"] != str(raw)
        assert "watermarked_video" in meta["input_path"]
        assert meta["unwatermarked"] is False
        assert meta["origin_source_path"] == str(raw)
        prep.validate_video(row["local_path"], platform="threads", limits={"max_seconds": 2.5, "max_bytes": 500_000}, expected_sha256=meta["sha256"])


def test_partial_or_corrupt_decode_is_never_advertised(source, tmp_path):
    with patch.object(prep, "decode_video", side_effect=prep.MediaPreparationError("late decode error")):
        with pytest.raises(prep.MediaPreparationError, match="late decode"):
            prep.prepare_video(source, tmp_path / "failed", platform="threads")
    assert not list((tmp_path / "failed").rglob("manifest.json"))


def test_display_sar_is_preserved_before_square_pixel_normalization():
    filters = prep.build_filters({"width": 320, "height": 240, "sample_aspect_ratio": "2:1", "field_order": "progressive"}, prep.effective_limits("threads"), 4_000_000)
    assert "scale=640:240" in filters and "pad=640:240" in filters and "setsar=1" in filters
    assert "crop=" not in filters


@pytest.mark.parametrize("image", [True, False], ids=["image", "video"])
@pytest.mark.parametrize("fields", [("checksum",), ("sha256",), ("checksum", "sha256")])
def test_final_artifact_checksums_refer_to_outputs_not_raw_inputs(source, tmp_path, image, fields):
    from types import SimpleNamespace
    from PIL import Image
    from myUtils import publish_media_guard as guard, worker

    if image:
        original_path = tmp_path / "source.jpg"
        final_path = tmp_path / "normalized.jpg"
        pixels = Image.new("RGB", (128, 128), (160, 80, 40))
        pixels.save(original_path, quality=100)
        pixels.save(final_path, quality=65)
        raw_sha = prep.sha256_file(original_path)
        part = SimpleNamespace(path=final_path, metadata={
            "sha256": prep.sha256_file(final_path), "size_bytes": final_path.stat().st_size,
            "source_sha256": raw_sha, "input_sha256": raw_sha})
        platform = "bluesky"
    else:
        original_path = source
        raw_sha = prep.sha256_file(source)
        part = prep.prepare_video(source, tmp_path / "normalized", platform="threads")[0]
        platform = "threads"
    assert part.metadata["sha256"] != raw_sha
    original = {"artifact_kind": "local", "local_path": str(original_path),
        "metadata": {"content_variant": "approved-existing-variant"},
        **{field: raw_sha for field in fields}}
    before = copy.deepcopy(original)
    working = guard._working_payload({"message": "  Unchanged approved copy.  ",
        "artifacts": [original]}, platform)
    result = guard._replacement(working["artifacts"][0], part, original_path, image=image, platform=platform)
    assert original == before and prep.sha256_file(original_path) == raw_sha
    assert all(result[field] == part.metadata["sha256"] for field in fields)
    assert result["metadata"]["source_sha256"] == raw_sha
    assert result["metadata"]["input_sha256"] == raw_sha
    assert result["metadata"]["content_variant"] == "approved-existing-variant"
    worker._verify_artifact(part.path, result)
    final_payload = {"message": working["message"], "_preserveApprovedText": True, "artifacts": [result]}
    guard.validate_payload(final_payload, platform=platform)
    assert final_payload["message"] == "  Unchanged approved copy.  "
