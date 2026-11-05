"""Measured, immutable video preparation before scheduling or provider uploads.

This is a preparation policy, not a substitute for live creator permissions.
Every returned part covers an ordered source interval and has been probed after
encoding. A failed probe/encode or incomplete set never returns the original.
Legacy CRF helpers remain available; new publishing uses this byte-budgeted path.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from myUtils import media_prep, platform_limits

BYTE_HEADROOM = 0.94
MIN_VIDEO_BITRATE = 256_000  # operational quality floor, not a provider minimum
TARGET_VIDEO_BITRATE = 4_000_000
AUDIO_BITRATE = 128_000
MAX_PARTS = 1000
MAX_ENCODE_ATTEMPTS = 3
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
CONTEXT_KINDS = {"audio", "transcript"}


class MediaPreparationError(ValueError):
    """Fail closed with an actionable, non-retryable preparation diagnostic."""

    retryable = False

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.details = {"mediaPreparationRequired": True, "retrySafe": True}


class MediaPreparationRequired(MediaPreparationError):
    """A whole publication intent must become paced posts, not a partial upload."""


@dataclass(frozen=True)
class PreparedVideoPart:
    path: Path
    metadata: dict[str, Any]


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _number(value: Any, label: str, *, positive: bool = True) -> float:
    if isinstance(value, bool):
        raise MediaPreparationError(f"Invalid {label}")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise MediaPreparationError(f"Invalid {label}") from None
    if not math.isfinite(number) or (number <= 0 if positive else number < 0):
        raise MediaPreparationError(f"Invalid {label}")
    return number


def tighten_limits(baseline: dict, supplied: dict | None) -> dict:
    """Untrusted constraints can only tighten CURRENT account/transport policy."""
    if supplied is None:
        supplied = {}
    if not isinstance(supplied, dict) or set(supplied) - set(baseline):
        raise MediaPreparationError("Invalid preparation constraints")
    result = dict(baseline)
    for key, value in supplied.items():
        if value is None:
            continue  # None cannot remove any established ceiling/floor.
        current = baseline[key]
        if key == "formats":
            if (not isinstance(value, (list, tuple)) or not value
                    or any(not isinstance(fmt, str) for fmt in value)):
                raise MediaPreparationError("Invalid preparation formats")
            result[key] = tuple(fmt for fmt in current if fmt in value)
            if not result[key] or set(value) - set(current):
                raise MediaPreparationError("Preparation formats cannot relax the route")
            continue
        number = _number(value, key, positive=not key.startswith("min_"))
        if current is not None:
            number = max(number, current) if key.startswith("min_") else min(number, current)
        if key in {"max_bytes", "target_bytes", "max_width", "max_height", "min_width",
                   "min_height", "min_dimension", "max_dimension_sum", "max_items", "max_video_bitrate"}:
            if isinstance(number, float) and not number.is_integer():
                raise MediaPreparationError(f"{key} must be an integer")
            number = int(number)
        result[key] = number
    for low, high in (("min_seconds", "max_seconds"), ("min_fps", "max_fps"),
                      ("min_aspect_ratio", "max_aspect_ratio"),
                      ("min_width", "max_width"), ("min_height", "max_height")):
        if (result.get(low) is not None and result.get(high) is not None
                and result[low] > result[high]):
            raise MediaPreparationError("Inconsistent preparation constraints")
    if "min_dimension" in result and result["min_dimension"] > min(result["max_width"], result["max_height"]):
        raise MediaPreparationError("Inconsistent video preparation constraints")
    return result


def effective_limits(platform: str, *, account_config: dict | None = None,
                     limits: dict | None = None, backup_compatible: bool = False) -> dict:
    """No allowance comes from arbitrary limits; only fresh creator/channel data."""
    baseline = platform_limits.preparation_limits(
        platform, account_config=account_config, backup_compatible=backup_compatible)
    return tighten_limits(baseline, limits)


def merge_limits(values: list[dict]) -> dict:
    """Use the strictest constraints across the selected accounts of one route."""
    if not values:
        raise MediaPreparationError("No selected-account preparation constraints")
    result = dict(values[0])
    for key in result:
        candidates = [value[key] for value in values if value[key] is not None]
        result[key] = ((max(candidates) if key.startswith("min_") else min(candidates))
                       if candidates else None)
    return result


def _source_metadata(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size <= 0:
        raise MediaPreparationError("Video source is missing or empty")
    try:
        meta = media_prep.probe(path)
    except Exception as exc:
        raise MediaPreparationError(f"Cannot probe video: {type(exc).__name__}") from exc
    for field in ("duration", "width", "height", "fps"):
        _number(meta.get(field), f"video {field}")
    if not meta.get("codec"):
        raise MediaPreparationError("Source contains no decodable video stream")
    meta["size"] = path.stat().st_size
    return meta


def validation_errors(path: Path, meta: dict, limits: dict) -> list[str]:
    errors: list[str] = []
    duration = _number(meta.get("duration"), "video duration")
    size = path.stat().st_size
    if size <= 0:
        errors.append("empty video")
    if limits["max_bytes"] is not None and size > limits["max_bytes"]:
        errors.append("size")
    if duration < limits["min_seconds"]:
        errors.append("minimum duration")
    if limits["max_seconds"] is not None and duration > limits["max_seconds"]:
        errors.append("duration")
    width = _number(meta.get("width"), "video width")
    height = _number(meta.get("height"), "video height")
    if (min(width, height) < limits["min_dimension"]
            or width > limits["max_width"] or height > limits["max_height"]):
        errors.append("dimensions")
    if ((limits["min_aspect_ratio"] is not None and width / height < limits["min_aspect_ratio"])
            or (limits["max_aspect_ratio"] is not None and width / height > limits["max_aspect_ratio"])):
        errors.append("aspect ratio")
    fps = _number(meta.get("fps"), "video frame rate")
    if not limits["min_fps"] <= fps <= limits["max_fps"]:
        errors.append("frame rate")
    # ffprobe groups MOV and MP4; the MP4 suffix AND its brand are checked.
    if (path.suffix.lower() != ".mp4" or "mp4" not in str(meta.get("format_name") or "").split(",")
            or str(meta.get("major_brand") or "").strip() == "qt"):
        errors.append("MP4 container")
    if meta.get("codec") != "h264" or meta.get("pix_fmt") != "yuv420p":
        errors.append("H.264/yuv420p")
    if str(meta.get("profile") or "").lower() != "high":
        errors.append("H.264 High profile")
    if meta.get("field_order") not in {"progressive", "unknown"}:
        errors.append("progressive scan")
    if meta.get("sample_aspect_ratio") not in {"1:1", "1/1"}:
        errors.append("square pixels")
    if meta.get("rotation"):
        errors.append("rotation metadata")
    if meta.get("has_audio"):
        if meta.get("audio_codec") != "aac" or meta.get("audio_profile") != "LC":
            errors.append("AAC-LC audio")
        if not 1 <= int(meta.get("audio_channels") or 0) <= 2:
            errors.append("audio channels")
        if not 0 < int(meta.get("audio_sample_rate") or 0) <= 48_000:
            errors.append("audio sample rate")
        # AAC is VBR; allow small encoder rounding around the chosen 128 kbps.
        if int(meta.get("audio_bit_rate") or 0) > 140_800:
            errors.append("audio bitrate")
    if int(meta.get("video_bit_rate") or 0) > limits["max_video_bitrate"]:
        errors.append("video bitrate")
    return errors


def validate_video(path: str | Path, *, platform: str, account_config: dict | None = None,
                   limits: dict | None = None, expected_sha256: str | None = None,
                   backup_compatible: bool = False) -> dict:
    """Read-only final-byte validation; safe inside a guarded queue transaction."""
    path = Path(path).expanduser().resolve()
    constraints = effective_limits(platform, account_config=account_config, limits=limits,
                                   backup_compatible=backup_compatible)
    meta = _source_metadata(path)
    errors = validation_errors(path, meta, constraints)
    if errors:
        raise MediaPreparationError(f"Video needs preprocessing for {platform}: {', '.join(errors)}")
    if expected_sha256 is not None:
        verify_checksum(path, expected_sha256)
    return meta


def verify_checksum(path: str | Path, expected_sha256: str) -> None:
    if (not isinstance(expected_sha256, str) or len(expected_sha256) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in expected_sha256)):
        raise MediaPreparationError("Prepared media has no valid checksum")
    try:
        actual = sha256_file(path)
    except OSError as exc:
        raise MediaPreparationError("Prepared media is missing or unreadable") from exc
    if actual != expected_sha256.lower():
        raise MediaPreparationError("Prepared media checksum changed")


def _even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def build_filters(meta: dict, limits: dict, video_bitrate: int) -> str:
    """Scale/pad to a bounded canvas, never crop source content away."""
    width, height = float(meta["width"]), float(meta["height"])
    sar = str(meta.get("sample_aspect_ratio") or "1:1").replace("/", ":").split(":")
    if len(sar) == 2:
        try:
            ratio = float(sar[0]) / float(sar[1])
            if math.isfinite(ratio) and ratio > 0:
                width *= ratio
        except (ValueError, ZeroDivisionError):
            pass
    if abs(int(meta.get("rotation") or 0)) % 180 == 90:
        width, height = height, width
    quality_bound = 640 if video_bitrate < 600_000 else 1280 if video_bitrate < 1_500_000 else 1920
    max_w = min(limits["max_width"], quality_bound)
    max_h = min(limits["max_height"], quality_bound)
    scale = min(1.0, max_w / width, max_h / height)
    minimum = _even(math.ceil(limits["min_dimension"] / 2) * 2)
    out_w, out_h = max(minimum, _even(width * scale)), max(minimum, _even(height * scale))
    if limits["min_aspect_ratio"] is not None:
        out_w = max(out_w, math.ceil(out_h * limits["min_aspect_ratio"] / 2) * 2)
    if limits["max_aspect_ratio"] is not None:
        out_h = max(out_h, math.ceil(out_w / limits["max_aspect_ratio"] / 2) * 2)
    filters = []
    if meta.get("field_order") not in {"progressive", "unknown"}:
        filters.append("yadif")
    if out_w > limits["max_width"] or out_h > limits["max_height"]:
        raise MediaPreparationError("Video cannot fit the bounded padded canvas")
    # Account for display SAR explicitly before discarding metadata. Merely
    # setsar=1 after a physical-pixel fit would distort anamorphic source frames.
    fit = min(out_w / width, out_h / height)
    content_w, content_h = _even(width * fit), _even(height * fit)
    filters.extend([
        f"scale={content_w}:{content_h}",
        f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2",
        "setsar=1", f"fps={min(30, limits['max_fps']):g}",
    ])
    return ",".join(filters)


def _part_count(meta: dict, limits: dict) -> int:
    duration = float(meta["duration"])
    count = 1
    if limits["max_seconds"] is not None and duration > limits["max_seconds"]:
        # Leave room for AAC/frame rounding instead of encoding right on a cap.
        safe_duration = limits["max_seconds"] - min(0.2, limits["max_seconds"] * 0.01)
        count = math.ceil(duration / safe_duration)
    if limits["max_bytes"] is not None:
        audio_rate = AUDIO_BITRATE if meta.get("has_audio") else 0
        minimum_bytes = duration * (MIN_VIDEO_BITRATE + audio_rate) / 8
        count = max(count, math.ceil(minimum_bytes / (limits["max_bytes"] * BYTE_HEADROOM)))
    if count > MAX_PARTS or duration / count < limits["min_seconds"]:
        raise MediaPreparationError("Video cannot fit a complete split set at the preparation quality floor")
    return max(1, count)


def _bitrate(seconds: float, limits: dict, has_audio: bool) -> int:
    rate = min(TARGET_VIDEO_BITRATE, int(limits["max_video_bitrate"]) // 2)
    if limits["max_bytes"] is not None:
        budget = math.floor(limits["max_bytes"] * BYTE_HEADROOM * 8 / seconds)
        rate = min(rate, budget - (AUDIO_BITRATE if has_audio else 0))
    if rate < MIN_VIDEO_BITRATE:
        raise MediaPreparationError("Insufficient byte budget for a decodable video part")
    return rate


def _encode(src: Path, output: Path, meta: dict, limits: dict, *, start: float,
            seconds: float, bitrate: int) -> None:
    fps = min(30, limits["max_fps"])
    command = [media_prep._ensure_tool(media_prep.FFMPEG), "-hide_banner", "-loglevel", "error",
               "-filter_threads", "2", "-n", "-ss", f"{start:.6f}", "-i", str(src), "-t", f"{seconds:.6f}",
               "-map", f"0:{meta.get('video_stream_index', 0)}", "-map", "0:a:0?", "-map_metadata", "-1",
               "-vf", build_filters(meta, limits, bitrate), "-c:v", "libx264", "-preset", "fast",
               "-profile:v", "high", "-pix_fmt", "yuv420p",
               "-b:v", str(bitrate), "-maxrate", str(bitrate), "-bufsize", str(bitrate * 2),
               "-flags", "+cgop", "-g", str(int(fps * 2)), "-keyint_min", str(int(fps * 2)),
               "-sc_threshold", "0", "-fps_mode", "cfr", "-threads", "2",
               "-c:a", "aac", "-profile:a", "aac_low", "-b:a", "128k", "-ac", "2",
               "-ar", "48000", "-movflags", "+faststart", str(output)]
    try:
        result = media_prep._run(command, capture_output=True, text=True,
                                 timeout=min(21_600, max(120, seconds * 12)))
    except Exception as exc:
        raise MediaPreparationError(f"Video encoder failed: {type(exc).__name__}") from exc
    if result.returncode != 0:
        raise MediaPreparationError("Video encoder failed: " + result.stderr.strip()[-1600:])


def decode_video(path: str | Path, *, duration: float) -> None:
    """Bounded offline decode check. Run during preparation, NOT a queue write."""
    command = [media_prep._ensure_tool(media_prep.FFMPEG), "-hide_banner", "-loglevel", "error",
               "-xerror", "-threads", "2", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?",
               "-threads", "2", "-f", "null", "-"]
    try:
        result = media_prep._run(command, capture_output=True, text=True,
                                 timeout=min(21_600, max(120, duration * 3)))
    except Exception as exc:
        raise MediaPreparationError(f"Video decode check failed: {type(exc).__name__}") from exc
    if result.returncode != 0:
        raise MediaPreparationError("Video decode check failed: " + result.stderr.strip()[-1600:])


def _part(path: Path, meta: dict, *, source_sha: str, group: str, platform: str,
          limits: dict, start: float, end: float, index: int, count: int,
          input_path: Path, input_sha: str, interval: tuple[float, float],
          provenance: dict | None = None) -> PreparedVideoPart:
    return PreparedVideoPart(path, {
        "role": "video", "preparation_policy_version": platform_limits.PREPARATION_POLICY_VERSION,
        "preparation_group": group, "prepared_for": [platform], "source_sha256": source_sha,
        "decoded": True,
        "source_path": (provenance or {}).get("source_path", str(input_path)),
        "input_path": str(input_path), "input_sha256": input_sha,
        "source_interval_start_seconds": interval[0], "source_interval_end_seconds": interval[1],
        **({"reprepared_from": {"path": str(input_path), "sha256": input_sha,
            "preparation_group": provenance.get("preparation_group"),
            "part_index": provenance.get("part_index"), "part_count": provenance.get("part_count"),
            "source_start_seconds": interval[0], "source_end_seconds": interval[1]}}
           if provenance else {}),
        "sha256": sha256_file(path), "size_bytes": path.stat().st_size,
        "max_media_mb": path.stat().st_size / 1_000_000,
        "max_duration_seconds": float(meta["duration"]),
        "width": int(meta["width"]), "height": int(meta["height"]), "fps": float(meta["fps"]),
        "source_start_seconds": start, "source_end_seconds": end,
        "part_index": index, "part_count": count, "split_for": [platform], "preparation_limits": limits,
    })


def prepare_video(src: str | Path, out_dir: str | Path, *, platform: str,
                  account_config: dict | None = None, limits: dict | None = None,
                  backup_compatible: bool = False, provenance: dict | None = None,
                  force_generation: bool = False) -> list[PreparedVideoPart]:
    """Compress when possible; otherwise return every measured ordered part.

    Encodes are written to a new private generation. A set becomes usable only
    after all parts pass validation; no advertised old artifact is overwritten.
    Byte overflow receives bounded bitrate reductions, never a CRF guess.
    """
    source = Path(src).expanduser().resolve()
    constraints = effective_limits(platform, account_config=account_config, limits=limits,
                                   backup_compatible=backup_compatible)
    meta = _source_metadata(source)
    input_hash = sha256_file(source)
    source_hash = input_hash
    interval = (0.0, float(meta["duration"]))
    if provenance:
        # Recovery encodes EXACT verified older prepared bytes, not the raw source
        # (which may have a different watermark/content variant).
        verify_checksum(source, provenance.get("sha256"))
        source_hash = provenance.get("source_sha256")
        if not isinstance(source_hash, str) or len(source_hash) != 64:
            raise MediaPreparationError("Prepared source has no origin checksum")
        start = _number(provenance.get("source_start_seconds"), "source interval start", positive=False)
        end = _number(provenance.get("source_end_seconds"), "source interval end")
        if end <= start:
            raise MediaPreparationError("Invalid prepared source interval")
        interval = (start, end)
        if abs((end - start) - float(meta["duration"])) > 0.25:
            raise MediaPreparationError("Older prepared part does not match its recorded source interval")
    recipe = hashlib.sha256(json.dumps({"input": input_hash, "limits": constraints,
        "interval": interval, "version": platform_limits.PREPARATION_POLICY_VERSION}, sort_keys=True).encode()).hexdigest()
    compatible = not validation_errors(source, meta, constraints)
    if compatible and not force_generation:
        decode_video(source, duration=meta["duration"])
        verify_checksum(source, input_hash)
        return [_part(source, meta, source_sha=source_hash, group=recipe, platform=platform,
                      limits=constraints, start=interval[0], end=interval[1], index=1, count=1,
                      input_path=source, input_sha=input_hash, interval=interval, provenance=provenance)]
    count = 1 if compatible else _part_count(meta, constraints)
    generation = Path(out_dir).expanduser().resolve() / platform_limits.PREPARATION_POLICY_VERSION / uuid.uuid4().hex
    generation.mkdir(parents=True, exist_ok=False)
    try:
        # A duration rounded up by muxing can require one additional split plan.
        for plan_attempt in range(2):
            parts: list[PreparedVideoPart] = []
            duration_overflow = False
            length = meta["duration"] / count
            for index in range(count):
                start, end = index * length, min((index + 1) * length, meta["duration"])
                rate = 0 if compatible else _bitrate(end - start, constraints, bool(meta.get("has_audio")))
                for attempt in range(MAX_ENCODE_ATTEMPTS):
                    output = generation / f"plan{plan_attempt}-part{index + 1}of{count}-encode{attempt}.mp4"
                    if compatible:
                        shutil.copyfile(source, output)  # new immutable path, NO lossy re-encode
                    else:
                        _encode(source, output, meta, constraints, start=start, seconds=end - start, bitrate=rate)
                    measured = _source_metadata(output)
                    errors = validation_errors(output, measured, constraints)
                    if not errors:
                        parts.append(_part(output, measured, source_sha=source_hash,
                            group=recipe + "-" + generation.name, platform=platform, limits=constraints,
                            start=interval[0] + start / meta["duration"] * (interval[1] - interval[0]),
                            end=interval[0] + end / meta["duration"] * (interval[1] - interval[0]),
                            index=index + 1, count=count, input_path=source, input_sha=input_hash,
                            interval=interval, provenance=provenance))
                        break
                    if errors == ["duration"] and plan_attempt == 0:
                        duration_overflow = True
                        break
                    if errors != ["size"] or attempt == MAX_ENCODE_ATTEMPTS - 1:
                        raise MediaPreparationError(f"Encoded video still violates {platform}: {', '.join(errors)}")
                    ratio = constraints["max_bytes"] * 0.9 / measured["size"]
                    rate = int(rate * ratio)
                    if rate < MIN_VIDEO_BITRATE:
                        raise MediaPreparationError("Compression cannot fit the byte cap at the preparation quality floor")
                if duration_overflow:
                    break
            if duration_overflow:
                count += 1
                if count > MAX_PARTS or meta["duration"] / count < constraints["min_seconds"]:
                    raise MediaPreparationError("Muxed duration cannot fit a complete split set")
                continue
            if len(parts) != count:
                raise MediaPreparationError("Incomplete encoded video part set")
            if sha256_file(source) != input_hash:
                raise MediaPreparationError("Source changed during preprocessing")
            # Revalidate ALL retained final files before advertising the manifest.
            for part in parts:
                decode_video(part.path, duration=part.metadata["max_duration_seconds"])
                validate_video(part.path, platform=platform, account_config=account_config,
                               limits=constraints, expected_sha256=part.metadata["sha256"],
                               backup_compatible=backup_compatible)
            manifest = {"source_sha256": source_hash, "input_path": str(source), "input_sha256": input_hash,
                        "preparation_policy_version": platform_limits.PREPARATION_POLICY_VERSION,
                        "preparation_limits": constraints,
                        "parts": [{"path": str(part.path), **part.metadata} for part in parts]}
            with (generation / "manifest.json").open("x", encoding="utf-8") as stream:
                json.dump(manifest, stream, sort_keys=True, indent=2)
            retained = {part.path for part in parts}
            for path in generation.glob("*.mp4"):
                if path not in retained:
                    path.unlink()
            return parts
        raise MediaPreparationError("Video duration remains outside the platform cap")
    except BaseException:
        # Only our unpublished generation is removed; existing sources stay intact.
        shutil.rmtree(generation, ignore_errors=True)
        raise


def select_prepared_parts(artifacts: list[dict], platform: str) -> list[dict]:
    """Select the latest whole generation, never mix or drop tail segments."""
    groups: dict[str, list[dict]] = {}
    for artifact in artifacts:
        metadata = artifact.get("metadata") or {}
        if platform in (metadata.get("prepared_for") or []):
            groups.setdefault(str(metadata.get("preparation_group") or ""), []).append(artifact)
    if not groups or "" in groups:
        raise MediaPreparationRequired(f"No complete prepared video generation for {platform}")
    group = max(groups.values(), key=lambda values: max(int(a.get("id") or 0) for a in values))
    count = (group[0].get("metadata") or {}).get("part_count")
    if type(count) is not int or not 1 <= count <= MAX_PARTS:
        raise MediaPreparationError("Invalid prepared video part count")
    selected: dict[int, dict] = {}
    source_hash = group[0]["metadata"].get("source_sha256")
    for artifact in group:
        metadata = artifact["metadata"]
        start = _number(metadata.get("source_start_seconds"), "part source start", positive=False)
        end = _number(metadata.get("source_end_seconds"), "part source end")
        if end <= start:
            raise MediaPreparationError("Invalid prepared video source interval")
        if not isinstance(metadata.get("sha256"), str) or len(metadata["sha256"]) != 64:
            raise MediaPreparationError("Prepared video has no checksum")
        index = metadata.get("part_index")
        if (metadata.get("part_count") != count or type(index) is not int or not 1 <= index <= count
                or metadata.get("source_sha256") != source_hash
                or (platform == "tiktok" and metadata.get("unwatermarked") is not True)):
            raise MediaPreparationError("Inconsistent prepared video part set")
        current = selected.get(index)
        if current is not None and current["metadata"].get("sha256") != metadata.get("sha256"):
            raise MediaPreparationError("Prepared video index refers to different bytes")
        if current is None or artifact.get("public_url"):
            selected[index] = artifact
    if set(selected) != set(range(1, count + 1)):
        raise MediaPreparationError("Incomplete prepared video part set; publication stopped")
    ordered = [selected[index] for index in range(1, count + 1)]
    for previous, following in zip(ordered, ordered[1:]):
        if abs(previous["metadata"]["source_end_seconds"] - following["metadata"]["source_start_seconds"]) > 0.000001:
            raise MediaPreparationError("Prepared video source intervals are not contiguous")
    first, last = ordered[0]["metadata"], ordered[-1]["metadata"]
    if (abs(first["source_start_seconds"] - first.get("source_interval_start_seconds", first["source_start_seconds"])) > 0.000001
            or abs(last["source_end_seconds"] - last.get("source_interval_end_seconds", last["source_end_seconds"])) > 0.000001):
        raise MediaPreparationError("Prepared video set does not cover its complete source interval")
    return ordered
