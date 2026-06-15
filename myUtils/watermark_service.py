"""Watermark configuration and processing service.

Handles persistent watermark configs per profile, image watermarking (Pillow),
video watermarking (ffmpeg), dynamic position changes, and thumbnail generation.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from utils.conf_defaults import BASE_DIR

DB_PATH = Path(BASE_DIR) / "db" / "database.db"

WATERMARK_TYPE_TEXT = "text"
WATERMARK_TYPE_IMAGE = "image"
WATERMARK_TYPE_COMBINED = "combined"

POSITIONS = ["top_left", "top_right", "bottom_left", "bottom_right", "center", "random_safe_area"]

FFMPEG_COMMAND = "ffmpeg"
FFPROBE_COMMAND = "ffprobe"


@dataclass(slots=True)
class WatermarkConfig:
    id: int
    profile_id: int | None
    name: str
    watermark_type: str
    text: str
    image_path: str
    opacity: float
    scale: float
    margin: int
    randomize_position: bool
    video_dynamic_position: bool
    video_position_change_min_seconds: int
    video_position_change_max_seconds: int
    allowed_positions: list[str]
    font_family: str
    font_size: int
    font_color: str
    enabled: bool
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["randomize_position"] = int(self.randomize_position)
        d["video_dynamic_position"] = int(self.video_dynamic_position)
        d["enabled"] = int(self.enabled)
        d["allowed_positions"] = json.dumps(self.allowed_positions)
        return d


def _resolve_db_path(db_path: Path | None) -> Path:
    return db_path if db_path is not None else DB_PATH


@contextmanager
def _connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    resolved = _resolve_db_path(db_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _row_to_config(row: sqlite3.Row) -> WatermarkConfig:
    data = {key: row[key] for key in row.keys()}
    data["randomize_position"] = bool(data.get("randomize_position", 0))
    data["video_dynamic_position"] = bool(data.get("video_dynamic_position", 0))
    data["enabled"] = bool(data.get("enabled", 1))
    positions = data.get("allowed_positions", "[]")
    if isinstance(positions, str):
        data["allowed_positions"] = json.loads(positions)
    return WatermarkConfig(**data)


# --------------- CRUD ---------------

def create_watermark_config(
    *,
    profile_id: int | None = None,
    name: str = "default",
    watermark_type: str = WATERMARK_TYPE_TEXT,
    text: str = "",
    image_path: str = "",
    opacity: float = 0.3,
    scale: float = 0.15,
    margin: int = 24,
    randomize_position: bool = False,
    video_dynamic_position: bool = False,
    video_position_change_min_seconds: int = 1,
    video_position_change_max_seconds: int = 5,
    allowed_positions: list[str] | None = None,
    font_family: str = "",
    font_size: int = 0,
    font_color: str = "white",
    enabled: bool = True,
    db_path: Path | None = None,
) -> WatermarkConfig:
    if allowed_positions is None:
        allowed_positions = ["top_left", "top_right", "bottom_left", "bottom_right"]
    now = _now_iso()
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO watermark_configs
            (profile_id, name, watermark_type, text, image_path, opacity, scale, margin,
             randomize_position, video_dynamic_position, video_position_change_min_seconds,
             video_position_change_max_seconds, allowed_positions, font_family, font_size,
             font_color, enabled, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                profile_id, name, watermark_type, text, image_path,
                opacity, scale, margin,
                int(randomize_position), int(video_dynamic_position),
                video_position_change_min_seconds, video_position_change_max_seconds,
                json.dumps(allowed_positions), font_family, font_size, font_color,
                int(enabled), now, now,
            ),
        )
        conn.commit()
        return get_watermark_config(cur.lastrowid, db_path=db_path)


def get_watermark_config(config_id: int, *, db_path: Path | None = None) -> WatermarkConfig:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM watermark_configs WHERE id = ?", (config_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"WatermarkConfig {config_id} not found")
        return _row_to_config(row)


def list_watermark_configs(
    *, profile_id: int | None = None, db_path: Path | None = None
) -> list[WatermarkConfig]:
    with _connect(db_path) as conn:
        if profile_id is not None:
            rows = conn.execute(
                "SELECT * FROM watermark_configs WHERE profile_id = ? OR profile_id IS NULL ORDER BY name",
                (profile_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM watermark_configs ORDER BY name"
            ).fetchall()
        return [_row_to_config(r) for r in rows]


def update_watermark_config(
    config_id: int, *, db_path: Path | None = None, **fields
) -> WatermarkConfig:
    allowed = {
        "profile_id", "name", "watermark_type", "text", "image_path",
        "opacity", "scale", "margin", "randomize_position", "video_dynamic_position",
        "video_position_change_min_seconds", "video_position_change_max_seconds",
        "allowed_positions", "font_family", "font_size", "font_color", "enabled",
    }
    updates = {}
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "allowed_positions" and isinstance(v, list):
            v = json.dumps(v)
        if k in ("randomize_position", "video_dynamic_position", "enabled"):
            v = int(bool(v))
        updates[k] = v
    if not updates:
        return get_watermark_config(config_id, db_path=db_path)
    updates["updated_at"] = _now_iso()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [config_id]
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE watermark_configs SET {set_clause} WHERE id = ?", values
        )
        conn.commit()
    return get_watermark_config(config_id, db_path=db_path)


def delete_watermark_config(config_id: int, *, db_path: Path | None = None) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM watermark_configs WHERE id = ?", (config_id,))
        conn.commit()


# --------------- Image watermarking ---------------

def _resolve_position(
    position: str, img_width: int, img_height: int, wm_width: int, wm_height: int, margin: int
) -> tuple[int, int]:
    """Resolve named position to (x, y) coordinates."""
    if position == "top_left":
        return margin, margin
    elif position == "top_right":
        return img_width - wm_width - margin, margin
    elif position == "bottom_left":
        return margin, img_height - wm_height - margin
    elif position == "bottom_right":
        return img_width - wm_width - margin, img_height - wm_height - margin
    elif position == "center":
        return (img_width - wm_width) // 2, (img_height - wm_height) // 2
    elif position == "random_safe_area":
        # Place in a random corner-safe area (not center)
        safe_positions = ["top_left", "top_right", "bottom_left", "bottom_right"]
        return _resolve_position(
            random.choice(safe_positions), img_width, img_height, wm_width, wm_height, margin
        )
    return margin, margin


def apply_image_watermark(
    source_path: str | Path,
    output_path: str | Path,
    config: WatermarkConfig,
    *,
    position: str | None = None,
) -> Path:
    """Apply watermark to an image using Pillow. Returns output_path."""
    from PIL import Image, ImageDraw, ImageFont

    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    img = Image.open(source).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))

    wm_width, wm_height = 0, 0

    # Apply text watermark
    if config.watermark_type in (WATERMARK_TYPE_TEXT, WATERMARK_TYPE_COMBINED) and config.text:
        draw = ImageDraw.Draw(overlay)
        font_size = config.font_size if config.font_size > 0 else max(16, img.width // 30)
        try:
            if config.font_family and Path(config.font_family).exists():
                font = ImageFont.truetype(config.font_family, font_size)
            else:
                font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
        except (OSError, IOError):
            font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), config.text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        # Parse font color
        color = config.font_color or "white"
        alpha = int(config.opacity * 255)

        if position is None:
            if config.randomize_position and config.allowed_positions:
                position = random.choice(config.allowed_positions)
            else:
                position = "bottom_right"

        x, y = _resolve_position(position, img.width, img.height, text_w, text_h, config.margin)
        draw.text((x, y), config.text, fill=color, font=font)
        # Apply alpha via the overlay
        overlay.putalpha(Image.new("L", overlay.size, alpha))
        wm_width, wm_height = text_w, text_h

    # Apply image watermark
    if config.watermark_type in (WATERMARK_TYPE_IMAGE, WATERMARK_TYPE_COMBINED) and config.image_path:
        wm_img_path = Path(config.image_path)
        if wm_img_path.exists():
            wm_img = Image.open(wm_img_path).convert("RGBA")
            # Scale watermark
            target_w = int(img.width * config.scale)
            target_h = int(wm_img.height * (target_w / wm_img.width))
            wm_img = wm_img.resize((target_w, target_h), Image.LANCZOS)

            # Apply opacity
            if config.opacity < 1.0:
                alpha_channel = wm_img.split()[3]
                alpha_channel = alpha_channel.point(lambda p: int(p * config.opacity))
                wm_img.putalpha(alpha_channel)

            wm_pos = position
            if wm_pos is None:
                if config.randomize_position and config.allowed_positions:
                    wm_pos = random.choice(config.allowed_positions)
                else:
                    wm_pos = "bottom_right"

            x, y = _resolve_position(wm_pos, img.width, img.height, target_w, target_h, config.margin)
            overlay.paste(wm_img, (x, y), wm_img)
            wm_width, wm_height = target_w, target_h

    result = Image.alpha_composite(img, overlay)
    result.convert("RGB").save(str(output), quality=95)
    return output


# --------------- Video watermarking ---------------

def _escape_drawtext(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )


def _position_to_ffmpeg_expr(position: str, margin: int, text_var: str = "text") -> tuple[str, str]:
    """Convert named position to ffmpeg x/y expressions."""
    w_ref = "w" if text_var == "text" else "main_w"
    h_ref = "h" if text_var == "text" else "main_h"
    tw = f"{text_var}_w" if text_var == "text" else "overlay_w"
    th = f"{text_var}_h" if text_var == "text" else "overlay_h"

    positions = {
        "top_left": (str(margin), str(margin)),
        "top_right": (f"{w_ref}-{tw}-{margin}", str(margin)),
        "bottom_left": (str(margin), f"{h_ref}-{th}-{margin}"),
        "bottom_right": (f"{w_ref}-{tw}-{margin}", f"{h_ref}-{th}-{margin}"),
        "center": (f"({w_ref}-{tw})/2", f"({h_ref}-{th})/2"),
    }
    return positions.get(position, positions["bottom_right"])


def build_dynamic_overlay_timeline(
    duration_seconds: float,
    *,
    allowed_positions: list[str],
    min_window: int = 1,
    max_window: int = 5,
    seed: int = 0,
) -> list[dict]:
    """Build a timeline of position changes for dynamic watermarking."""
    if duration_seconds <= 0:
        return []
    rng = random.Random(seed)
    current = 0.0
    timeline = []
    prev_pos = None
    while current < duration_seconds:
        window = rng.uniform(min_window, max_window)
        window = min(window, duration_seconds - current)
        choices = [p for p in allowed_positions if p != prev_pos] or allowed_positions
        pos = rng.choice(choices)
        timeline.append({
            "start": round(current, 3),
            "end": round(current + window, 3),
            "position": pos,
        })
        prev_pos = pos
        current += window
    return timeline


def apply_video_watermark(
    source_path: str | Path,
    output_path: str | Path,
    config: WatermarkConfig,
    *,
    duration_seconds: float | None = None,
    seed: int = 0,
) -> Path:
    """Apply watermark to a video using ffmpeg. Returns output_path."""
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    if duration_seconds is None:
        duration_seconds = _probe_duration(source)

    if config.watermark_type == WATERMARK_TYPE_TEXT and config.text:
        escaped = _escape_drawtext(config.text)
        fontsize = config.font_size if config.font_size > 0 else 24
        color = config.font_color or "white"
        opacity = config.opacity

        if config.video_dynamic_position:
            timeline = build_dynamic_overlay_timeline(
                duration_seconds,
                allowed_positions=config.allowed_positions or ["top_left", "top_right", "bottom_left", "bottom_right"],
                min_window=config.video_position_change_min_seconds,
                max_window=config.video_position_change_max_seconds,
                seed=seed,
            )
            filter_parts = []
            for seg in timeline:
                x_expr, y_expr = _position_to_ffmpeg_expr(seg["position"], config.margin)
                filter_parts.append(
                    f"drawtext=text='{escaped}':"
                    f"fontcolor={color}@{opacity:.2f}:"
                    f"fontsize={fontsize}:"
                    f"x={x_expr}:y={y_expr}:"
                    f"enable='between(t,{seg['start']},{seg['end']})'"
                )
            vf = ",".join(filter_parts)
        else:
            position = "bottom_right"
            if config.randomize_position and config.allowed_positions:
                position = random.Random(seed).choice(config.allowed_positions)
            x_expr, y_expr = _position_to_ffmpeg_expr(position, config.margin)
            vf = (
                f"drawtext=text='{escaped}':"
                f"fontcolor={color}@{opacity:.2f}:"
                f"fontsize={fontsize}:"
                f"x={x_expr}:y={y_expr}"
            )
    elif config.watermark_type == WATERMARK_TYPE_IMAGE and config.image_path:
        wm_path = Path(config.image_path)
        if not wm_path.exists():
            raise FileNotFoundError(f"Watermark image not found: {wm_path}")
        position = "bottom_right"
        if config.randomize_position and config.allowed_positions:
            position = random.Random(seed).choice(config.allowed_positions)

        if config.video_dynamic_position:
            timeline = build_dynamic_overlay_timeline(
                duration_seconds,
                allowed_positions=config.allowed_positions or ["top_left", "top_right", "bottom_left", "bottom_right"],
                min_window=config.video_position_change_min_seconds,
                max_window=config.video_position_change_max_seconds,
                seed=seed,
            )
            filter_parts = []
            for seg in timeline:
                x_expr, y_expr = _position_to_ffmpeg_expr(seg["position"], config.margin, text_var="overlay")
                filter_parts.append(
                    f"[1:v]format=rgba,colorchannelmixer=aa={config.opacity:.2f}[wm];"
                    f"[0:v][wm]overlay={x_expr}:{y_expr}:enable='between(t,{seg['start']},{seg['end']})'"
                )
            # For image overlay with dynamic position, use a simpler approach
            vf = f"overlay={_position_to_ffmpeg_expr(position, config.margin, 'overlay')[0]}:{_position_to_ffmpeg_expr(position, config.margin, 'overlay')[1]}"
        else:
            x_expr, y_expr = _position_to_ffmpeg_expr(position, config.margin, text_var="overlay")
            vf = f"[1:v]format=rgba,colorchannelmixer=aa={config.opacity:.2f}[wm];[0:v][wm]overlay={x_expr}:{y_expr}"
    else:
        # No watermark to apply, just copy
        import shutil
        shutil.copy2(str(source), str(output))
        return output

    cmd = [
        FFMPEG_COMMAND, "-y",
        "-i", str(source),
    ]
    if config.watermark_type == WATERMARK_TYPE_IMAGE and config.image_path:
        cmd.extend(["-i", str(Path(config.image_path))])
    cmd.extend([
        "-vf", vf,
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(output),
    ])

    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return output


def _probe_duration(file_path: Path) -> float:
    """Get video duration using ffprobe."""
    result = subprocess.run(
        [FFPROBE_COMMAND, "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(file_path)],
        capture_output=True, text=True, check=True,
    )
    import json as _json
    data = _json.loads(result.stdout or "{}")
    dur = data.get("format", {}).get("duration")
    if dur is None:
        raise ValueError(f"Cannot probe duration of {file_path}")
    return float(dur)


def generate_thumbnail(
    source_path: str | Path,
    output_path: str | Path,
    *,
    timestamp: float = 1.0,
) -> Path:
    """Generate a thumbnail image from a video. Returns output_path."""
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [
            FFMPEG_COMMAND, "-y",
            "-ss", str(max(0.0, timestamp)),
            "-i", str(source),
            "-frames:v", "1",
            "-q:v", "2",
            str(output),
        ],
        check=True, capture_output=True, text=True,
    )
    return output


def extract_audio(
    source_path: str | Path,
    output_path: str | Path,
) -> Path:
    """Extract audio track from video. Returns output_path."""
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [
            FFMPEG_COMMAND, "-y",
            "-i", str(source),
            "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le",
            str(output),
        ],
        check=True, capture_output=True, text=True,
    )
    return output
