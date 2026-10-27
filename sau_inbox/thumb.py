#!/usr/bin/env python3
"""thumb.py — 安全縮圖產生器（inbox-watcher 的獨立核心，EmailVPS host 執行）。

影片 → 抽幀 → 裁 4:5 → 壓 baiting 標題帶 + 角落浮水印 → 安全縮圖。

設計原則（依計畫 wild-wobbling-rocket）：
- 只依賴 host ffmpeg + PIL（已驗證 EmailVPS 有 Python 3.12 + PIL 12.2）。
- 與 SAU 容器 decouple，watcher 直接 import；之後若要套品牌 persona
  浮水印/字型，可再換成 docker exec 呼叫容器內 media_pipeline。
- 內容中立：不預設 nsfw/sfw，安全與否由「抽幀時機 + 裁切錨點」參數決定。

用法：
    python3 thumb.py <video> -o out.jpg [--title "標題"] [--time 8]
                              [--logo logo.png] [--anchor top|center|bottom]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

CJK_FONT_CANDIDATES = [
    "/home/will/.local/share/fonts/NotoSansCJK.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def resolve_font() -> str:
    env = os.environ.get("THUMB_FONT", "").strip()
    if env and os.path.exists(env):
        return env
    for candidate in CJK_FONT_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return ""


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def probe_duration(path: Path) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    try:
        return float(out.stdout.strip())
    except ValueError:
        raise RuntimeError(f"無法讀取影片時長: {path}") from None


def probe_size(path: Path) -> tuple[int, int]:
    out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)])
    try:
        w, h = out.stdout.strip().split("x")
        return int(w), int(h)
    except ValueError:
        raise RuntimeError(f"無法讀取影片尺寸: {path}") from None


def extract_frame(video: Path, out_jpg: Path, t: float) -> Path:
    """抽單幀到 out_jpg。t 為秒數（快 seek，單幀夠用）。"""
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    proc = _run(["ffmpeg", "-hide_banner", "-y", "-ss", f"{t:.3f}",
                 "-i", str(video), "-frames:v", "1", "-q:v", "2", str(out_jpg)])
    if proc.returncode != 0 or not out_jpg.exists():
        raise RuntimeError(f"抽幀失敗 (t={t}): {proc.stderr[-500:]}")
    return out_jpg


def crop_to_aspect(img, aspect: tuple[int, int] = (4, 5), anchor: str = "center"):
    """裁成 aspect 比例。anchor: center|top|bottom（top 對 nsfw 遮敏感很好用）。"""
    from PIL import Image
    ar_w, ar_h = aspect
    target_ratio = ar_w / ar_h
    w, h = img.size
    cur_ratio = w / h
    if cur_ratio > target_ratio:
        new_w = int(h * target_ratio)
        new_h = h
    else:
        new_w = w
        new_h = int(w / target_ratio)
    left = (w - new_w) // 2
    top = (h - new_h) // 2
    if anchor == "top":
        top = 0
    elif anchor == "bottom":
        top = h - new_h
    return img.crop((left, top, left + new_w, top + new_h))


def wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for raw in text.split("\n"):
        cur = ""
        for ch in raw:
            if draw.textlength(cur + ch, font=font) <= max_width or not cur:
                cur += ch
            else:
                lines.append(cur)
                cur = ch
        lines.append(cur)
    return lines


def render_title_band(img, lines: list[str], font_path: str, font_size: int,
                      band_opacity: int = 150):
    """底部半透明黑帶 + 白字 baiting 標題。傳入 img 需為 RGB。"""
    from PIL import Image, ImageDraw, ImageFont
    from PIL.ImageFont import FreeTypeFont
    font = ImageFont.truetype(font_path, font_size) if font_path else None
    draw = ImageDraw.Draw(img)

    # 量出標題區高度
    line_h = int(font_size * 1.35) if font else int(img.height * 0.06)
    pad = int(font_size * 0.9)
    band_h = len(lines) * line_h + pad * 2

    band = Image.new("RGBA", (img.width, band_h), (0, 0, 0, band_opacity))
    img.paste(band, (0, img.height - band_h), band)

    draw = ImageDraw.Draw(img)
    y = img.height - band_h + pad
    for line in lines:
        if font:
            tw = draw.textlength(line, font=font)
        else:
            tw = 0
        x = (img.width - tw) / 2
        draw.text((x + 1, y + 1), line, font=font, fill=(0, 0, 0, 255))
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_h
    return img


def paste_logo(img, logo_path: str | None, margin: int = 24, max_w_ratio: float = 0.18):
    if not logo_path:
        return img
    from PIL import Image
    with Image.open(logo_path).convert("RGBA") as logo:
        max_w = max(60, int(img.width * max_w_ratio))
        scale = min(1.0, max_w / logo.width)
        if scale < 1.0:
            logo = logo.resize((int(logo.width * scale), int(logo.height * scale)))
        img.paste(logo, (img.width - logo.width - margin, margin), logo)
    return img


def make_titlecard(out: str, *, title: str, subtitle: str | None = None,
                   badge: str | None = None, accent: tuple[int, int, int] = (214, 48, 49),
                   bg: tuple[int, int, int] = (18, 22, 32),
                   out_size: tuple[int, int] = (1080, 1350),
                   font_path: str | None = None) -> Path:
    """純文字安全卡（nsfw 預設用，絕不抽原幀＝零外洩）。

    深色底 + accent 色裝飾條 + 置中 baiting 標題；subtitle 放平台名稱，
    badge 放「NSFW」等警示。
    """
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", out_size, bg)
    draw = ImageDraw.Draw(img)
    fpath = font_path or resolve_font()

    # 上方裝飾條（accent）
    bar_h = max(14, int(img.height * 0.02))
    draw.rectangle((0, 0, img.width, bar_h), fill=accent)

    if badge:
        bfont = ImageFont.truetype(fpath, int(img.width * 0.04)) if fpath else None
        if bfont:
            bw = draw.textlength(badge, font=bfont)
            bx = (img.width - bw) / 2
            draw.rounded_rectangle(
                (bx - 20, int(img.height * 0.12), bx + bw + 20, int(img.height * 0.12) + int(img.width * 0.07)),
                radius=10, fill=accent,
            )
            draw.text((bx, int(img.height * 0.12) + 4), badge, font=bfont, fill=(255, 255, 255, 255))

    title_fs = int(img.width * 0.085)
    tfont = ImageFont.truetype(fpath, title_fs) if fpath else None
    if tfont:
        lines = wrap_text(draw, title, tfont, int(img.width * 0.82))
        line_h = int(title_fs * 1.4)
        total_h = len(lines) * line_h
        y = (img.height - total_h) // 2 - (int(img.width * 0.05) if subtitle else 0)
        for line in lines:
            tw = draw.textlength(line, font=tfont)
            draw.text(((img.width - tw) / 2 + 2, y + 2), line, font=tfont, fill=(0, 0, 0, 255))
            draw.text(((img.width - tw) / 2, y), line, font=tfont, fill=(255, 255, 255, 255))
            y += line_h

    if subtitle:
        sfont = ImageFont.truetype(fpath, int(img.width * 0.04)) if fpath else None
        if sfont:
            sw = draw.textlength(subtitle, font=sfont)
            draw.text(((img.width - sw) / 2, y + int(img.width * 0.03)), subtitle, font=sfont, fill=(190, 190, 200, 255))

    img.save(out, quality=88)
    return Path(out)


def make_thumb(src: str, out: str, *, title: str | None = None, time_sec: float | None = None,
               anchor: str = "center", logo: str | None = None,
               out_size: tuple[int, int] = (1080, 1350), font_size: int | None = None,
               font_path: str | None = None) -> Path:
    """主要入口：產生 4:5 安全縮圖。回傳輸出 Path。"""
    from PIL import Image

    source = Path(src).expanduser().resolve()
    output = Path(out).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    if source.suffix.lower() in IMAGE_EXTS:
        img = Image.open(source).convert("RGB")
    else:
        duration = probe_duration(source)
        if time_sec is None:
            # 預設取約 15% 處（避開片頭全黑/片尾 credit）
            time_sec = max(1.0, duration * 0.15)
        else:
            time_sec = min(max(time_sec, 0.0), max(duration - 0.2, 0))
        tmp = output.parent / f".{source.stem}_frame.jpg"
        try:
            extract_frame(source, tmp, time_sec)
            img = Image.open(tmp).convert("RGB")
        finally:
            if tmp.exists():
                tmp.unlink()

    img = crop_to_aspect(img, (4, 5), anchor=anchor)

    # 標題字型／字級：預設約圖寬 4.5%，依字數自動縮小換行
    fpath = font_path or resolve_font()
    if title:
        fs = font_size or max(40, int(img.width * 0.05))
        # 太長時遞減字級，讓標題進得進一行半
        while fs > 26:
            try:
                from PIL import ImageDraw, ImageFont
                fd = ImageDraw.Draw(img)
                font = ImageFont.truetype(fpath, fs) if fpath else None
                if font is None or max(len(wrap_text(fd, title, font, int(img.width * 0.9))), 0) <= 2:
                    break
            except Exception:
                pass
            fs -= 2
        img = render_title_band(img, wrap_text(
            ImageDraw.Draw(img), title,
            ImageFont.truetype(fpath, fs) if fpath else None,
            int(img.width * 0.9)), fpath, fs)

    img = paste_logo(img, logo)

    img = img.resize(out_size, Image.LANCZOS)
    img.save(output, quality=88)
    return output


def main() -> int:
    ap = argparse.ArgumentParser(description="產生 4:5 安全縮圖")
    ap.add_argument("source", nargs="?", default=None, help="影片或圖片路徑（--titlecard 時可省略）")
    ap.add_argument("-o", "--output", default="thumb.jpg")
    ap.add_argument("--title", default=None, help="baiting 標題（可多行 \\n）")
    ap.add_argument("--subtitle", default=None, help="次要文字（平台名等）")
    ap.add_argument("--badge", default=None, help="警示章（NSFW 等）")
    ap.add_argument("--accent", default=None, help="主題色 hex，如 #d63031")
    ap.add_argument("--time", type=float, default=None, help="抽幀秒數（預設 ~15% 處）")
    ap.add_argument("--anchor", choices=["center", "top", "bottom"], default="center")
    ap.add_argument("--logo", default=None, help="角落浮水印圖片")
    ap.add_argument("--font-size", type=int, default=None)
    ap.add_argument("--titlecard", action="store_true", help="只做純文字安全卡（不抽幀）")
    args = ap.parse_args()

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("缺少 ffmpeg/ffprobe", file=sys.stderr)
        return 2

    accent = None
    if args.accent:
        accent = tuple(int(args.accent.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))

    try:
        if args.titlecard:
            if not args.title:
                print("--titlecard 需要 --title", file=sys.stderr)
                return 2
            out = make_titlecard(args.output, title=args.title, subtitle=args.subtitle,
                                 badge=args.badge, accent=accent or (214, 48, 49))
        else:
            if not args.source:
                print("需要 source", file=sys.stderr)
                return 2
            out = make_thumb(args.source, args.output, title=args.title, time_sec=args.time,
                             anchor=args.anchor, logo=args.logo, font_size=args.font_size)
    except Exception as exc:  # noqa: BLE001
        print(f"縮圖失敗: {exc}", file=sys.stderr)
        return 1
    print(f"OK {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
