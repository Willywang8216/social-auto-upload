"""Media-processing helpers for campaign preparation."""

from __future__ import annotations

import json
import random
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from conf import BASE_DIR

FFMPEG_COMMAND = "ffmpeg"
FFPROBE_COMMAND = "ffprobe"
GENERATED_MEDIA_ROOT = Path(BASE_DIR) / "generated" / "campaigns"


@dataclass(frozen=True, slots=True)
class OverlayWindow:
    start_seconds: int
    end_seconds: int
    x_expr: str
    y_expr: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class VideoWatermarkPlan:
    output_path: Path
    timeline: list[OverlayWindow]
    command: list[str]

    def to_dict(self) -> dict:
        out = asdict(self)
        out["output_path"] = str(self.output_path)
        return out


def run_subprocess(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, **kwargs)


def build_campaign_workspace(
    campaign_id: int,
    *,
    base_dir: Path | None = None,
) -> Path:
    root = base_dir or GENERATED_MEDIA_ROOT
    path = Path(root) / f"campaign-{campaign_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepare_campaign_artifact_path(
    campaign_id: int,
    source_path: str | Path,
    *,
    artifact_kind: str,
    suffix: str | None = None,
    base_dir: Path | None = None,
) -> Path:
    source = Path(source_path)
    workspace = build_campaign_workspace(campaign_id, base_dir=base_dir)
    artifact_dir = workspace / artifact_kind
    artifact_dir.mkdir(parents=True, exist_ok=True)
    ext = suffix if suffix is not None else source.suffix
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    return artifact_dir / f"{source.stem}-{artifact_kind}{ext}"


def probe_video_duration(
    file_path: str | Path,
    *,
    runner=run_subprocess,
) -> float:
    resolved = Path(file_path).expanduser().resolve()
    completed = runner(
        [
            FFPROBE_COMMAND,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(resolved),
        ],
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout or "{}")
    duration = payload.get("format", {}).get("duration")
    if duration is None:
        raise ValueError(f"ffprobe did not return a duration for {resolved}")
    return float(duration)


def extract_video_audio(
    source_path: str | Path,
    output_path: str | Path,
    *,
    runner=run_subprocess,
) -> Path:
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    runner(
        [
            FFMPEG_COMMAND,
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    return output


def _text_positions(inset: int = 24) -> list[tuple[str, str]]:
    return [
        (str(inset), str(inset)),
        (f"w-text_w-{inset}", str(inset)),
        (str(inset), f"h-text_h-{inset}"),
        (f"w-text_w-{inset}", f"h-text_h-{inset}"),
        ("(w-text_w)/2", str(inset)),
        ("(w-text_w)/2", f"h-text_h-{inset}"),
        (str(inset), "(h-text_h)/2"),
        (f"w-text_w-{inset}", "(h-text_h)/2"),
        ("(w-text_w)/2", "(h-text_h)/2"),
    ]


def _image_positions(inset: int = 24) -> list[tuple[str, str]]:
    return [
        (str(inset), str(inset)),
        (f"main_w-overlay_w-{inset}", str(inset)),
        (str(inset), f"main_h-overlay_h-{inset}"),
        (f"main_w-overlay_w-{inset}", f"main_h-overlay_h-{inset}"),
        ("(main_w-overlay_w)/2", str(inset)),
        ("(main_w-overlay_w)/2", f"main_h-overlay_h-{inset}"),
        (str(inset), "(main_h-overlay_h)/2"),
        (f"main_w-overlay_w-{inset}", "(main_h-overlay_h)/2"),
        ("(main_w-overlay_w)/2", "(main_h-overlay_h)/2"),
    ]


def build_video_overlay_timeline(
    duration_seconds: float,
    *,
    seed: int,
    min_window_seconds: int = 1,
    max_window_seconds: int = 5,
    use_text_positions: bool = True,
) -> list[OverlayWindow]:
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be > 0")
    if min_window_seconds < 1 or max_window_seconds < min_window_seconds:
        raise ValueError("invalid overlay window range")

    rng = random.Random(seed)
    anchor_pool = _text_positions() if use_text_positions else _image_positions()
    current = 0
    windows: list[OverlayWindow] = []
    previous_anchor: tuple[str, str] | None = None
    total_seconds = max(1, int(round(duration_seconds)))

    while current < total_seconds:
        window_length = min(
            rng.randint(min_window_seconds, max_window_seconds),
            total_seconds - current,
        )
        choices = [anchor for anchor in anchor_pool if anchor != previous_anchor]
        anchor = rng.choice(choices or anchor_pool)
        windows.append(
            OverlayWindow(
                start_seconds=current,
                end_seconds=current + window_length,
                x_expr=anchor[0],
                y_expr=anchor[1],
            )
        )
        previous_anchor = anchor
        current += window_length
    return windows


def _escape_drawtext(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )


def _build_text_filter(
    watermark_text: str,
    timeline: Sequence[OverlayWindow],
    *,
    opacity: float,
    fontsize: int,
) -> str:
    escaped = _escape_drawtext(watermark_text)
    parts = []
    for window in timeline:
        parts.append(
            "drawtext="
            f"text='{escaped}':"
            f"fontcolor=white@{opacity:.2f}:"
            f"fontsize={fontsize}:"
            f"x={window.x_expr}:"
            f"y={window.y_expr}:"
            f"enable='between(t,{window.start_seconds},{window.end_seconds})'"
        )
    return ",".join(parts)


def _build_image_filter(timeline: Sequence[OverlayWindow]) -> tuple[str, str]:
    filter_steps: list[str] = []
    current = "[0:v]"
    for index, window in enumerate(timeline):
        next_label = f"[v{index}]"
        filter_steps.append(
            f"{current}[1:v]overlay="
            f"x={window.x_expr}:"
            f"y={window.y_expr}:"
            f"enable='between(t,{window.start_seconds},{window.end_seconds})'"
            f"{next_label}"
        )
        current = next_label
    return ";".join(filter_steps), current


def apply_video_watermark(
    source_path: str | Path,
    output_path: str | Path,
    *,
    watermark_text: str | None = None,
    watermark_image_path: str | Path | None = None,
    seed: int,
    duration_seconds: float | None = None,
    opacity: float = 0.18,
    fontsize: int = 48,
    runner=run_subprocess,
    duration_reader=probe_video_duration,
) -> VideoWatermarkPlan:
    if not watermark_text and not watermark_image_path:
        raise ValueError("watermark_text or watermark_image_path is required")

    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    duration = duration_seconds
    if duration is None:
        duration = duration_reader(source, runner=runner)

    use_text_positions = watermark_image_path is None
    timeline = build_video_overlay_timeline(
        duration,
        seed=seed,
        use_text_positions=use_text_positions,
    )

    if watermark_image_path is not None:
        watermark_image = Path(watermark_image_path).expanduser().resolve()
        filter_complex, video_map = _build_image_filter(timeline)
        command = [
            FFMPEG_COMMAND,
            "-y",
            "-i",
            str(source),
            "-loop",
            "1",
            "-i",
            str(watermark_image),
            "-filter_complex",
            filter_complex,
            "-map",
            video_map,
            "-map",
            "0:a?",
            "-c:a",
            "copy",
            "-shortest",
            str(output),
        ]
    else:
        filter_chain = _build_text_filter(
            watermark_text or "",
            timeline,
            opacity=opacity,
            fontsize=fontsize,
        )
        command = [
            FFMPEG_COMMAND,
            "-y",
            "-i",
            str(source),
            "-vf",
            filter_chain,
            "-c:a",
            "copy",
            str(output),
        ]

    runner(command, capture_output=True, text=True)
    return VideoWatermarkPlan(output_path=output, timeline=timeline, command=command)


def apply_image_watermark(
    source_path: str | Path,
    output_path: str | Path,
    *,
    watermark_text: str | None = None,
    watermark_image_path: str | Path | None = None,
    seed: int,
    opacity: int = 72,
) -> Path:
    if not watermark_text and not watermark_image_path:
        raise ValueError("watermark_text or watermark_image_path is required")

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover - dependency/environment-specific
        raise RuntimeError("Pillow is required for image watermarking") from exc

    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    with Image.open(source) as base_image:
        base = base_image.convert("RGBA")
        overlay = Image.new("RGBA", base.size, (255, 255, 255, 0))

        if watermark_image_path is not None:
            with Image.open(Path(watermark_image_path).expanduser().resolve()) as wm_file:
                watermark = wm_file.convert("RGBA")
                max_width = max(80, int(base.width * 0.22))
                scale = min(1.0, max_width / max(watermark.width, 1))
                resized = watermark.resize(
                    (
                        max(1, int(watermark.width * scale)),
                        max(1, int(watermark.height * scale)),
                    )
                )
                alpha = resized.getchannel("A").point(
                    lambda value: int(value * (opacity / 255))
                )
                resized.putalpha(alpha)
                x, y = _random_pixel_position(base.size, resized.size, rng)
                overlay.alpha_composite(resized, (x, y))
        else:
            font_size = max(18, int(min(base.size) * 0.045))
            try:
                font = ImageFont.truetype("DejaVuSans.ttf", font_size)
            except OSError:
                font = ImageFont.load_default()
            drawer = ImageDraw.Draw(overlay)
            text = watermark_text or ""
            bbox = drawer.textbbox((0, 0), text, font=font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
            x, y = _random_pixel_position(base.size, (text_width, text_height), rng)
            drawer.text((x, y), text, fill=(255, 255, 255, opacity), font=font)

        composited = Image.alpha_composite(base, overlay)
        if output.suffix.lower() in {".jpg", ".jpeg"}:
            composited.convert("RGB").save(output, quality=95)
        else:
            composited.save(output)
    return output


def _random_pixel_position(
    base_size: tuple[int, int],
    overlay_size: tuple[int, int],
    rng: random.Random,
    *,
    inset: int = 24,
) -> tuple[int, int]:
    width = max(inset, base_size[0] - overlay_size[0] - inset)
    height = max(inset, base_size[1] - overlay_size[1] - inset)
    return rng.randint(inset, width), rng.randint(inset, height)
