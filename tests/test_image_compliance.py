"""Actual Pillow bytes, immutable generations, route caps and ordered albums."""
from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from myUtils import image_compliance as images, media_compliance as media, platform_limits, publish_media_guard as guard


def image_file(path: Path, *, size=(640, 360), mode="RGB", color="red") -> Path:
    Image.new(mode, size, color).save(path, format="PNG" if path.suffix == ".png" else "JPEG")
    return path


def test_actual_encoding_exif_orientation_and_srgb_not_extension(tmp_path):
    source = tmp_path / "wrong.png"
    image = Image.new("RGB", (80, 240), "red")
    exif = Image.Exif()
    exif[274] = 6
    image.save(source, format="JPEG", exif=exif)
    before = media.sha256_file(source)
    with pytest.raises(media.MediaPreparationError, match="encoding|orientation"):
        images.validate_image(source, platform="bluesky")
    prepared = images.prepare_image(source, tmp_path / "outputs", platform="bluesky")
    info = images.validate_image(prepared.path, platform="bluesky", expected_sha256=prepared.metadata["sha256"])
    assert (info["width"], info["height"]) == (240, 80)
    assert info["format"] == "JPEG" and info["orientation"] == 1 and info["color_space"] == "sRGB"
    assert media.sha256_file(source) == before
    assert prepared.metadata["source_path"] == str(source.resolve())


def test_instagram_pads_panorama_without_cropping_or_dropping_edges(tmp_path):
    source = tmp_path / "panorama.png"
    image = Image.new("RGB", (1600, 160), "blue")
    image.paste("red", (0, 0, 160, 160))
    image.paste("green", (1440, 0, 1600, 160))
    image.save(source)
    prepared = images.prepare_image(source, tmp_path / "outputs", platform="instagram")
    info = images.validate_image(prepared.path, platform="instagram")
    assert info["format"] == "JPEG" and 0.8 <= info["width"] / info["height"] <= 1.91
    with Image.open(prepared.path) as final:
        y = final.height // 2
        left, right = final.getpixel((20, y)), final.getpixel((final.width - 21, y))
        assert left[0] > 180 and left[2] < 60
        assert right[1] > 90 and right[0] < 60
    assert prepared.path.stat().st_size <= platform_limits.image_preparation_limits("instagram")["max_bytes"]


def test_bluesky_real_noise_is_measured_under_safe_target(tmp_path):
    source = tmp_path / "noise.png"
    rng = random.Random(42)
    Image.frombytes("RGB", (1800, 1800), rng.randbytes(1800 * 1800 * 3)).save(source)
    assert source.stat().st_size > 2_000_000
    prepared = images.prepare_image(source, tmp_path / "outputs", platform="bluesky")
    assert prepared.path.stat().st_size <= 1_900_000
    assert prepared.metadata["size_bytes"] == prepared.path.stat().st_size
    assert images.validate_image(prepared.path, platform="bluesky")["mode"] == "RGB"


def test_exact_byte_boundary_is_actual_file_size(tmp_path):
    source = image_file(tmp_path / "boundary.jpg", size=(64, 64))
    original = source.read_bytes()
    source.write_bytes(original + b"\0" * (2_000_000 - len(original)))
    assert images.validate_image(source, platform="bluesky")["size_bytes"] == 2_000_000
    with source.open("ab") as stream:
        stream.write(b"\0")
    with pytest.raises(media.MediaPreparationError, match="size"):
        images.validate_image(source, platform="bluesky")


def test_invalid_decoders_animation_missing_encoder_and_no_bytes_fail_closed(tmp_path):
    invalid = tmp_path / "fake.jpg"
    invalid.write_bytes(b"not an image")
    with pytest.raises(media.MediaPreparationError, match="decode"):
        images.prepare_image(invalid, tmp_path / "bad", platform="bluesky")
    gif = tmp_path / "animated.gif"
    Image.new("RGB", (64, 64), "red").save(gif, save_all=True,
        append_images=[Image.new("RGB", (64, 64), "blue")], duration=100, loop=0)
    with pytest.raises(media.MediaPreparationError, match="Animated"):
        images.prepare_image(gif, tmp_path / "animated", platform="bluesky")
    source = image_file(tmp_path / "source.jpg")
    for encoder in (lambda *args, **kwargs: b"", lambda *args, **kwargs: (_ for _ in ()).throw(media.MediaPreparationError("encoder failed"))):
        with patch.object(images, "_encode", side_effect=encoder):
            with pytest.raises(media.MediaPreparationError):
                images.prepare_image(source, tmp_path / "failed", platform="bluesky")
    assert not list((tmp_path / "failed").rglob("manifest.json"))
    with pytest.raises(media.MediaPreparationError, match="No image set"):
        images.prepare_images([], tmp_path, platform="bluesky")


def test_encode_attempts_finite_no_oversized_original_fallback(tmp_path):
    source = image_file(tmp_path / "source.jpg")
    with patch.object(images, "_encode", return_value=b"x" * 3000) as encode:
        with pytest.raises(media.MediaPreparationError, match="bounded encodes"):
            images.prepare_image(source, tmp_path / "failed", platform="bluesky", limits={"max_bytes": 2000})
    assert encode.call_count == images.MAX_ENCODE_ATTEMPTS
    assert source.is_file() and not list((tmp_path / "failed").rglob("manifest.json"))


def test_image_overrides_tighten_and_unknown_domestic_routes_are_not_invented():
    assert images.effective_limits("bluesky", limits={"max_bytes": 50_000_000, "max_items": 10})["max_bytes"] == 2_000_000
    assert images.effective_limits("bluesky", limits={"max_items": 10})["max_items"] == 4
    assert images.effective_limits("instagram", limits={"min_aspect_ratio": 0.1})["min_aspect_ratio"] == 0.8
    assert images.effective_limits("bluesky", limits={"max_bytes": None})["max_bytes"] == 2_000_000
    assert images.effective_limits("tiktok", backup_compatible=True)["max_items"] == 10
    assert images.effective_limits("tiktok")["max_items"] == 35
    with pytest.raises(media.MediaPreparationError, match="formats"):
        images.effective_limits("instagram", limits={"formats": ["JPEG", "PNG"]})
    for platform in platform_limits.UNVERIFIED_BROWSER_ROUTES:
        with pytest.raises(ValueError, match="No verified"):
            images.effective_limits(platform)


def test_complete_immutable_image_manifest_and_latest_generation(tmp_path):
    sources = [image_file(tmp_path / f"image{i}.jpg", color=color) for i, color in enumerate(("red", "green", "blue"))]
    first = images.prepare_images(sources, tmp_path / "outputs", platform="bluesky")
    hashes = [media.sha256_file(part.path) for part in first]
    second = images.prepare_images(sources, tmp_path / "outputs", platform="bluesky")
    assert first[0].path.parent != second[0].path.parent
    assert [media.sha256_file(part.path) for part in first] == hashes
    manifest = json.loads((first[0].path.parent / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["images"]) == 3
    assert [part.metadata["image_index"] for part in first] == [1, 2, 3]
    rows = [{"id": i, "local_path": str(part.path), "metadata": copy.deepcopy(part.metadata)} for i, part in enumerate(first, 1)]
    assert images.select_prepared_images(rows, "bluesky") == rows
    fresh = {"id": 99, "local_path": str(second[0].path), "metadata": second[0].metadata}
    with pytest.raises(media.MediaPreparationError, match="Incomplete"):
        images.select_prepared_images(rows + [fresh], "bluesky")
    with pytest.raises(media.MediaPreparationError, match="checksum"):
        images.validate_image(first[0].path, platform="bluesky", expected_sha256="0" * 64)


def test_ordered_batched_payloads_keep_every_image_caption_alt_and_cover(tmp_path):
    sources = [image_file(tmp_path / f"image{i}.jpg", size=(64, 64)) for i in range(9)]
    payload = {"message": "  Approved caption.  ", "draft": {"message": "  Approved caption.  "},
        "_preserveApprovedText": True, "cover": {"id": "original-cover"},
        "artifacts": [{"artifact_kind": "local", "local_path": str(path), "source_file_record_id": i,
                       "alt_text": f"  Alt {i}.  ", "metadata": {"role": "image", "content_variant": "sfw"}}
                      for i, path in enumerate(sources)] + [{"artifact_kind": "transcript", "local_path": "notes.txt"}]}
    original = copy.deepcopy(payload)
    parts = guard.prepare_payloads(payload, platform="bluesky", out_dir=tmp_path / "outputs", backup_compatible=True)
    assert [len(part["artifacts"]) for part in parts] == [4, 4, 1]
    all_images = [artifact for part in parts for artifact in part["artifacts"]]
    assert [image["source_file_record_id"] for image in all_images] == list(range(9))
    assert [image["alt_text"] for image in all_images] == [f"  Alt {i}.  " for i in range(9)]
    assert len({image["metadata"]["preparation_group"] for image in all_images}) == 1
    assert [image["metadata"]["image_index"] for image in all_images] == list(range(1, 10))
    assert all(part["message"] == original["message"] and part["cover"] == original["cover"] for part in parts)
    assert payload == original
    for part in parts:
        guard.validate_payload(part, platform="bluesky", backup_compatible=True)


def test_immutable_image_recovery_reprepares_whole_batch_never_old_source(tmp_path):
    sources = [image_file(tmp_path / f"image{i}.jpg", size=(640, 360)) for i in range(2)]
    first = images.prepare_images(sources, tmp_path / "first", platform="bluesky")
    payload = {"message": "Approved copy", "artifacts": [{"artifact_kind": "prepared_image",
        "source_file_record_id": i, "local_path": str(part.path), "metadata": part.metadata, "alt_text": f"Alt {i}"}
        for i, part in enumerate(first)]}
    old_hashes = [media.sha256_file(part.path) for part in first]
    for source in sources:
        source.write_bytes(b"raw source was replaced; it MUST NOT be read")
    with pytest.raises(media.MediaPreparationError, match="dimensions"):
        guard.prepare_payloads(payload, platform="bluesky", out_dir=tmp_path / "failed", image_limits={"max_width": 320})
    repaired = guard.prepare_payloads(payload, platform="bluesky", out_dir=tmp_path / "repaired",
                                     image_limits={"max_width": 320}, allow_reprepare=True)
    artifacts = repaired[0]["artifacts"]
    assert len(artifacts) == 2 and artifacts[0]["metadata"]["preparation_group"] == artifacts[1]["metadata"]["preparation_group"]
    assert [media.sha256_file(part.path) for part in first] == old_hashes
    for old, new in zip(first, artifacts):
        assert new["metadata"]["source_sha256"] == old.metadata["source_sha256"]
        assert new["metadata"]["reprepared_from"]["path"] == str(old.path)
        assert new["metadata"]["input_sha256"] == old.metadata["sha256"]
    assert repaired[0]["message"] == payload["message"]


def test_validation_only_does_not_encode_or_create_files(tmp_path):
    source = image_file(tmp_path / "image.jpg")
    prepared = images.prepare_image(source, tmp_path / "outputs", platform="bluesky")
    payload = {"message": "Approved", "artifacts": [{"artifact_kind": "prepared_image",
        "local_path": str(prepared.path), "metadata": prepared.metadata}]}
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    with patch.object(images, "_encode", side_effect=AssertionError("validation must not encode")):
        guard.validate_payload(payload, platform="bluesky")
        assert guard.prepare_payloads(payload, platform="bluesky", out_dir=tmp_path / "unused") == [payload]
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before
