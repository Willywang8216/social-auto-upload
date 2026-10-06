"""Media preparation with ffmpeg: compress oversized video before upload.

Google Drive sources can be very large (4K originals, long clips). Before
uploading to a social platform we re-encode with the project's proven
publishing profile:

* H.264 High, yuv420p pixel format, +faststart MP4
* <= 1080x1920 (9:16 vertical), centre-crop-or-letterbox to exactly 1080x1920
* <= 30 fps
* AAC stereo, 128 kbps, 48 kHz
* Single-pass CRF ~22 (~80 s clip typically lands between 20 and 30 MB)

ffmpeg/ffprobe may be absent in some environments. Every subprocess invocation
is hidden behind the lazy ``_ensure_tool``/``_run`` helpers, so the pure-logic
functions (``build_filters``, ``should_shrink``) never touch subprocess and
everything that does raises a clear ``RuntimeError("ffmpeg not available")`` at
the point of invocation.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

from myUtils import platform_limits

# Proven publishing profile.
TARGET_W = 1080
TARGET_H = 1920
MAX_FPS = 30
CRF_DEFAULT = 22
ASPECT_TOLERANCE = 0.05  # relative aspect-ratio tolerance for crop-vs-letterbox

PROFILE = {
    "target": (TARGET_W, TARGET_H),
    "max_fps": MAX_FPS,
    "crf": CRF_DEFAULT,
    "aspect_tolerance": ASPECT_TOLERANCE,
}

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

_TARGET_ASPECT = TARGET_W / TARGET_H  # 0.5625 (9:16 vertical)

# Hard single-file media ceilings per platform, in decimal megabytes (10^6
# bytes), from the single source of truth. Keep this as the shrink trigger only;
# the publishers enforce their own hard caps.
PLATFORM_MAX_MB: dict[str, int] = dict(platform_limits.MEDIA_MAX_MB)

# Used when a target platform is absent from the table (or the caller passed no
# platforms at all). Deliberately the most conservative real cap we know.
DEFAULT_MAX_MB = 300

# Shrink when the file exceeds this fraction of the platform's cap. Re-encodes
# land far below the estimate, so the headroom only has to absorb the gap
# between a probed size and the bytes the platform actually counts.
SIZE_HEADROOM = 0.98


def size_mb_decimal(meta: dict) -> float:
    """Size of ``meta`` in decimal megabytes.

    Platforms publish caps in decimal MB (Bluesky's is exactly 300,000,000
    bytes), so the comparison has to be decimal too: 300 MB is 286.1 MiB, and a
    MiB-based check would wave through a 295 MiB file that the platform then
    rejects. Accepts either the ``size_mb`` key or a raw ``size`` byte count.
    """
    if "size" in meta:
        return float(meta.get("size") or 0) / 1_000_000
    return float(meta.get("size_mb") or 0)


def resolve_size_limit_mb(platforms=None) -> float:
    """File-size trigger, in decimal MB, for a set of target platforms.

    The strictest cap among ``platforms`` wins, so a campaign fanning out to
    Bluesky and Threads shrinks to the Bluesky ceiling while a YouTube-only
    campaign is left alone on size (its dimensions and fps are still checked).
    """
    if not platforms:
        return DEFAULT_MAX_MB * SIZE_HEADROOM
    caps = [
        PLATFORM_MAX_MB.get(str(platform).strip().lower(), DEFAULT_MAX_MB)
        for platform in platforms
    ]
    return min(caps) * SIZE_HEADROOM


def _ensure_available() -> bool:
    """Cheap probe: both ffmpeg and ffprobe resolve on PATH (no subprocess)."""
    return bool(shutil.which(FFMPEG) and shutil.which(FFPROBE))


def _ensure_tool(tool: str) -> str:
    """Resolve an ffmpeg-family binary lazily, or raise when unavailable."""
    exe = shutil.which(tool)
    if not exe:
        raise RuntimeError("ffmpeg not available")
    return exe


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kwargs)


def _parse_fps(raw: str) -> float | None:
    """Parse a ffprobe frame-rate string such as ``60000/2002`` or ``30``."""
    if not raw:
        return None
    raw = raw.strip()
    if "/" in raw:
        num, _, den = raw.partition("/")
        try:
            numerator, denominator = float(num), float(den)
        except (TypeError, ValueError):
            return None
        if denominator == 0:
            return None
        return numerator / denominator
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def probe(path: str | Path) -> dict:
    """Probe a media file with ffprobe and return a normalized metadata dict.

    Returns ``{duration, size, width, height, fps, codec, has_audio,
    audio_channels}``. ``size`` is in bytes; ``fps`` is the decoded average
    frame rate as a float.
    """
    src = Path(path).expanduser().resolve()
    cmd = [
        _ensure_tool(FFPROBE),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(src),
    ]
    completed = _run(cmd, capture_output=True, text=True)
    if completed.returncode != 0:
        raise ValueError(
            f"ffprobe failed on {src}: {completed.stderr.strip()[-2000:]}"
        )
    payload = json.loads(completed.stdout or "{}")
    streams = payload.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = payload.get("format", {})

    duration = fmt.get("duration")
    if duration is None and video is not None:
        duration = video.get("duration")
    fps = _parse_fps(
        (video.get("avg_frame_rate") if video else None)
        or (video.get("r_frame_rate") if video else None)
        or ""
    )

    return {
        "duration": float(duration) if duration not in (None, "") else 0.0,
        "size": int(fmt.get("size") or 0),
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
        "fps": float(fps) if fps else 0.0,
        "codec": (video or {}).get("codec_name"),
        "has_audio": audio is not None,
        "audio_channels": int(audio.get("channels") or 0) if audio else 0,
    }


def build_filters(meta: dict) -> str:
    """Build the ffmpeg ``-vf`` chain to normalize ``meta`` to 1080x1920.

    When the input aspect ratio is within ``ASPECT_TOLERANCE`` of the 9:16
    target we scale-to-cover and centre-crop exactly to 1080x1920; otherwise
    (e.g. landscape, 4:3, 1:1) we scale-to-fit and letterbox with bars. Frames
    above ``MAX_FPS`` are dropped down to it and ``setsar=1`` normalizes the
    pixel aspect ratio at the end.
    """
    width = int(meta.get("width") or 0)
    height = int(meta.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ValueError("meta must include positive width and height")
    fps_in = float(meta.get("fps") or 0)

    aspect = width / height
    if abs(aspect - _TARGET_ASPECT) / _TARGET_ASPECT <= ASPECT_TOLERANCE:
        geometry = (
            f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase"
            f":force_divisible_by=2,crop={TARGET_W}:{TARGET_H}"
        )
    else:
        geometry = (
            f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease"
            f":force_divisible_by=2,pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2"
        )

    parts = [geometry]
    if fps_in > MAX_FPS:
        parts.append(f"fps={MAX_FPS}")
    parts.append("setsar=1")
    return ",".join(parts)


def should_shrink(
    meta: dict,
    threshold_mb: float | None = None,
    *,
    platforms=None,
) -> bool:
    """Decide whether ``meta`` needs an ffmpeg pass before upload.

    True when the file exceeds the size trigger, or when any dimension or the
    frame rate exceed the publishing profile. The size trigger is the strictest
    cap among ``platforms`` — see ``resolve_size_limit_mb`` — so a Bluesky-bound
    file is held to 300 MB while a YouTube-only one is not size-checked at all.
    Pass ``threshold_mb`` to override the trigger outright.
    """
    if threshold_mb is None:
        threshold_mb = resolve_size_limit_mb(platforms)
    width = int(meta.get("width") or 0)
    height = int(meta.get("height") or 0)
    fps = float(meta.get("fps") or 0)
    return (
        size_mb_decimal(meta) > float(threshold_mb)
        or width > TARGET_W
        or height > TARGET_H
        or fps > MAX_FPS
    )


def _format_fps(fps: float) -> str:
    """Trim a float to three decimals for ffmpeg's ``-r`` option."""
    if fps <= 0:
        return str(MAX_FPS)
    capped = min(fps, float(MAX_FPS))
    text = f"{capped:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def shrink(
    src: str | Path,
    out_dir: str | Path,
    *,
    crf: int = CRF_DEFAULT,
    threshold_mb: float | None = None,
    platforms=None,
) -> Path:
    """Compress ``src`` into ``out_dir/<stem>_pub.mp4`` when needed.

    Probes the source; when ``should_shrink`` is true a single-pass ffmpeg
    encode (the publishing profile above) writes the output MP4, otherwise the
    source is copied unchanged. Either way the returned Path exists.

    ``platforms`` (or an explicit ``threshold_mb``) picks the size trigger; see
    ``resolve_size_limit_mb``.
    """
    src_path = Path(src).expanduser().resolve()
    out_dir_path = Path(out_dir).expanduser().resolve()
    out_dir_path.mkdir(parents=True, exist_ok=True)
    out_path = out_dir_path / f"{src_path.stem}_pub.mp4"

    meta = probe(src_path)
    if not should_shrink(meta, threshold_mb=threshold_mb, platforms=platforms):
        shutil.copy2(src_path, out_path)
        return out_path

    filters = build_filters(meta)
    cmd = [
        _ensure_tool(FFMPEG),
        "-y",
        "-i",
        str(src_path),
        "-vf",
        filters,
        "-r",
        _format_fps(float(meta.get("fps") or 0)),
        "-c:v",
        "libx264",
        "-crf",
        str(int(crf)),
        "-preset",
        "medium",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ac",
        "2",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
    ]
    if not meta.get("has_audio"):
        cmd.append("-an")
    cmd.append(str(out_path))

    completed = _run(cmd, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed on {src_path}: {completed.stderr.strip()[-2000:]}"
        )
    return out_path


def resolve_duration_limit_seconds(platforms=None) -> float | None:
    """Strictest video-duration cap among ``platforms`` (seconds), or None."""
    if not platforms:
        return None
    caps = [
        platform_limits.video_max_seconds(str(p).strip().lower())
        for p in platforms
    ]
    caps = [float(c) for c in caps if c]
    return min(caps) if caps else None


def trim_to_seconds(
    src: str | Path,
    out_dir: str | Path,
    max_seconds: float,
    *,
    crf: int = CRF_DEFAULT,
) -> Path:
    """Re-encode ``src`` to at most ``max_seconds`` as ``<stem>_pub_<sec>s.mp4``.

    Uses the same publishing profile as :func:`shrink` plus a hard ``-t`` cut,
    so a platform with a short cap (Threads 300 s, X 140 s) gets a valid copy
    while the full ``_pub.mp4`` still serves platforms with a longer cap.
    """
    src_path = Path(src).expanduser().resolve()
    out_dir_path = Path(out_dir).expanduser().resolve()
    out_dir_path.mkdir(parents=True, exist_ok=True)
    seconds = int(max(1, round(float(max_seconds))))
    out_path = out_dir_path / f"{src_path.stem}_pub_{seconds}s.mp4"

    meta = probe(src_path)
    filters = build_filters(meta)
    cmd = [
        _ensure_tool(FFMPEG),
        "-y",
        "-i",
        str(src_path),
        "-t",
        str(seconds),
        "-vf",
        filters,
        "-r",
        _format_fps(float(meta.get("fps") or 0)),
        "-c:v",
        "libx264",
        "-crf",
        str(int(crf)),
        "-preset",
        "medium",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ac",
        "2",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
    ]
    if not meta.get("has_audio"):
        cmd.append("-an")
    cmd.append(str(out_path))
    completed = _run(cmd, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(
            f"ffmpeg trim failed on {src_path}: {completed.stderr.strip()[-2000:]}"
        )
    return out_path


def split_to_seconds(
    src: str | Path,
    out_dir: str | Path,
    max_seconds: float,
    *,
    crf: int = CRF_DEFAULT,
) -> list[Path]:
    """Split ``src`` into equal parts of at most ``max_seconds`` each.

    Returns ``[src]`` unchanged when it already fits. Otherwise re-encodes
    ``ceil(duration / max_seconds)`` parts (named ``<stem>_part<i>of<n>_pub.mp4``)
    so a long video becomes several under-the-cap posts instead of one truncated
    cut — no footage is dropped, unlike :func:`trim_to_seconds`.
    """
    src_path = Path(src).expanduser().resolve()
    if not src_path.exists():
        raise FileNotFoundError(f"split source missing: {src_path}")
    meta = probe(src_path)
    duration = float(meta.get("duration") or 0.0)
    cap = float(max_seconds or 0.0)
    if duration <= 0 or cap <= 0 or duration <= cap:
        return [src_path]

    count = max(2, int(math.ceil(duration / cap)))
    part_len = duration / count
    out_dir_path = Path(out_dir).expanduser().resolve()
    out_dir_path.mkdir(parents=True, exist_ok=True)
    filters = build_filters(meta)
    fps = _format_fps(float(meta.get("fps") or 0))

    parts: list[Path] = []
    for index in range(count):
        start = index * part_len
        out_path = out_dir_path / f"{src_path.stem}_part{index + 1}of{count}_pub.mp4"
        cmd = [
            _ensure_tool(FFMPEG),
            "-y",
            # -ss before -i is the fast, keyframe-accurate seek; the re-encode
            # fixes the cut at the exact boundary on the output timeline.
            "-ss",
            f"{start:.3f}",
            "-i",
            str(src_path),
            "-t",
            f"{part_len:.3f}",
            "-vf",
            filters,
            "-r",
            fps,
            "-c:v",
            "libx264",
            "-crf",
            str(int(crf)),
            "-preset",
            "medium",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
        ]
        if not meta.get("has_audio"):
            cmd.append("-an")
        cmd.append(str(out_path))
        completed = _run(cmd, capture_output=True, text=True)
        if completed.returncode != 0:
            raise RuntimeError(
                f"ffmpeg split part {index + 1}/{count} failed on {src_path}: "
                f"{completed.stderr.strip()[-2000:]}"
            )
        parts.append(out_path)
    return parts


def resize_to_target_if_landscape(src: str | Path, out_dir: str | Path) -> Path:
    """No-op stub reserved for future landscape reframing work.

    Intended scope: detect landscape sources that escaped ``shrink`` (e.g. a
    landscape clip that passes the size/dimension/fps heuristics) and reframe
    them to the 9:16 publishing target before upload. Currently returns the
    source unchanged; callers must not rely on anything being written yet.
    """
    return Path(src)