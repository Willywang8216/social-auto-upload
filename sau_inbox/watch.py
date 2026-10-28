#!/usr/bin/env python3
"""watch.py — Drive-backed SAU-Inbox watcher on the EmailVPS.

The watcher lists Drive objects, temporarily downloads unprocessed media for
classification/thumbnail generation, then removes the temporary original.
Queue state keeps Drive-relative paths; publishing retrieves originals on demand.

v1 設計原則（對齊計畫 wild-wobbling-rocket + 交接摘要）：
- 不自己接 LLM：各平台草稿由 SAU `publish-center/submit` 依 `brief` 自動生成
  （後端 _generate_account_draft 有 LLM + brief 兜底），watcher 只組 brief。
- 安全預設：nsfw 一律只產「純文字安全卡」（不抽原幀＝零外洩），標
  needsFrameSelection，等威威/Claude 挑安全幀後再補真縮圖。
- sfw 才自動抽幀產真縮圖。
- 去重：state.processed 記住已處理的絕對路徑；同步是 copy 不會搬檔，
  所以「新檔」＝磁碟上有、processed 沒有。
- Telegram 通知 opt-in（環境變數），沒設就只 log。

用法：
    python3 watch.py --once        # 掃一輪就結束（測試/CI）
    python3 watch.py               # daemon，每 WATCH_INTERVAL 秒掃一輪
    WATCH_INTERVAL=30 python3 watch.py
"""
from __future__ import annotations

import argparse
import fcntl
import json
from contextlib import contextmanager
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import NamedTuple

import thumb  # 同目錄的 thumb.py

LOG = logging.getLogger("sau-watch")
HERE = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    """讀同目錄 .env（KEY=VALUE，逐行），不覆蓋已存在的環境變數。"""
    dotenv = HERE / ".env"
    if not dotenv.exists():
        return
    for raw in dotenv.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()

# ---- 設定（可被環境變數蓋掉）----
INBOX = Path(os.environ.get("SAU_INBOX", "/home/will/sau-inbox"))
STATE_PATH = Path(os.environ.get("SAU_WATCH_STATE", str(HERE / "state.json")))
DEFAULT_INTERVAL = float(os.environ.get("WATCH_INTERVAL", "60"))
MIN_AGE_SEC = float(os.environ.get("WATCH_MIN_AGE_SEC", "8"))  # 太新=可能還在傳，跳過

# persona → profile id（計畫盤點：nw=1, sw=3, teaching=4, msl=10）
PERSONA_PROFILES: dict[str, list[int]] = {
    "nw": [1], "sw": [3], "teaching": [4], "msl": [10],
    "both": [1, 3],  # cross-post NW + SW (nudism into the sexual channel)
}
# titlecard 主題色（accent），純視覺用
PERSONA_ACCENT: dict[str, tuple[int, int, int]] = {
    "nw": (214, 48, 49),      # 深夜檔紅
    "sw": (244, 114, 182),    # 粉
    "teaching": (93, 173, 226),  # 藍
    "msl": (162, 89, 255),    # 紫
    "both": (229, 81, 115),   # NW red + SW pink
}

MEDIA_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi", ".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"}
KIND_TO_DIR = {"video": "video", "img": "img"}

# ---- 孤兒偵測（掉在 inbox 根目錄／未知資料夾的媒體）----
# 2026-09-17 事故：兩支 4.4–4.8GB 原檔被 root 身分的同步 cron 放到 inbox「根目錄」，
# 而 detect_new_files() 只掃 <persona>/{video,img}，所以它們對 watcher 完全隱形 ——
# 沒通知、沒 ready、沒警告，就只是靜靜把 97G 的磁碟吃到 96%。
# 這裡把「掃不到的媒體檔」抓出來告警（只告警，絕不搬動或刪除）。
KNOWN_ROOT_DIRS = set(PERSONA_PROFILES) | {"_pub", "__pycache__", "logs", "thumb"}
# 根目錄本來就有的非媒體檔（.py/.json/.log/README）不必列 —— 非 MEDIA_EXTS 一律跳過。
# 這裡只列「是媒體、但屬於內部資產」的例外。
ROOT_ALLOWLIST = {"_sample2.mp4"}
# 專案慣例：`_` 開頭是內部物（_pub/_batch/_sample2.mp4），`.` 開頭是暫存（.env/.partial）。
ROOT_ALLOW_PREFIXES = ("_", ".")
# persona → gen_title 的品牌調性參數
PERSONA_BRAND = {"nw": "nakedwill", "sw": "sexualwill", "teaching": "", "msl": "", "both": "nakedwill"}

from manual_platforms import manual_block, brand_note  # noqa: E402

# ⚠️ 教學隔離：teaching 帳號（職業身份）絕不允許任何 NW/SW/裸體內容，連 SFW 都不行。
# 命中即隔離＋TG 告警，永不進 ready。刻意不含單字「性」（避免誤殺「性質/性能」）。
TEACHING_BAD_MARKERS = (
    "naked", "nude", "nudity", "nudism", "nudist", "naturism", "naturist",
    "nakedwill", "sexualwill", "bodyfreedom", "nakedhappy",
    "裸", "裸體", "天體", "全裸", "露點", "性愛", "性行為", "做愛", "情色", "色情", "成人", "光光",
)


def _docx_text(path: Path) -> str:
    """Plain text out of a .docx, stdlib only (the watcher's cron interpreter has no python-docx).

    The operator writes hand transcripts/descriptions in Word (``both/*.docx``
    since 2026-10-13, ``Nakedwill-news-20261016__sfw.docx`` on 2026-10-17) and
    expects them to be picked up exactly like a same-name ``.txt``. Before this
    they were silently ignored and every docx-backed clip fell back to auto
    titling from the filename.
    """
    try:
        import re
        import zipfile
        from xml.etree import ElementTree

        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(xml)
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paragraphs = []
        for para in root.iter(f"{ns}p"):
            text = "".join(
                node.text or ""
                for node in para.iter(f"{ns}t")
            )
            if text.strip():
                paragraphs.append(text.strip())
        return re.sub(r"\n{3,}", "\n\n", "\n".join(paragraphs)).strip()
    except Exception:  # noqa: BLE001 — a broken/legacy .doc must not kill the scan
        return ""


def _read_sibling_note(source: Path) -> str:
    """讀同資料夾的同名文字說明（.txt / .md / .content.txt / .docx，無則空字串）。"""
    for p in (
        source.with_suffix(".txt"),
        source.with_suffix(".md"),
        source.parent / f"{source.stem}.content.txt",
        source.with_suffix(".docx"),
    ):
        if not p.exists():
            continue
        if p.suffix.lower() == ".docx":
            return _docx_text(p)
        try:
            return p.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def _has_cjk(text: str) -> bool:
    """Any CJK ideograph (incl. Ext-A / compatibility) ⇒ treat the copy as zh."""
    return any("\u3400" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff" for ch in text)


def detect_language(note: str | None, title: str | None) -> str | None:
    """zh|en from the transcript note, falling back to the title; None if both empty.

    Mirrors the routing rule in the filename spec §4: CJK codepoints ⇒ zh,
    otherwise en, and a missing transcript falls back to the parsed title.
    """
    for text in (note, title):
        if text and text.strip():
            return "zh" if _has_cjk(text) else "en"
    return None


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- state ----
def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            LOG.warning("state.json unavailable or corrupt")
    return {"version": 1, "processed": {}, "ready": [], "pending": [], "orphans": {}}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_name(f"{STATE_PATH.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


@contextmanager
def _watch_run_lock():
    """Prevent overlapping one-minute cron scans from deleting each other's temp files."""
    INBOX.mkdir(parents=True, exist_ok=True)
    with (INBOX / ".watch-run.lock").open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield


@contextmanager
def _state_lock():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_PATH.with_suffix(".lock")
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield


# ---- 檔名解析（v2 舊規格 + v3 新規格；型別看副檔名）----
# v2: <主題?>__<sfw|nsfw>
# v3: <Title with spaces>_<SFW|NSFW>_<YYYYMMDD>（單底線，標題可含空白，日期可省略）
FLAGS = ("sfw", "nsfw")
FILENAME_DATE_FMT = "%Y%m%d"


class ParsedName(NamedTuple):
    topic: str
    flag: str
    publish_date: str | None  # YYYY-MM-DD


def _looks_like_date_token(token: str) -> bool:
    token = token.strip()
    return len(token) == 8 and token.isdigit()


def _parse_date_token(token: str) -> str | None:
    """YYYYMMDD → YYYY-MM-DD, or None when the value is not a real date."""
    token = token.strip()
    if not _looks_like_date_token(token):
        return None
    try:
        return datetime.strptime(token, FILENAME_DATE_FMT).strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_filename(stem: str) -> ParsedName | None:
    """Parse either filename convention; None when no valid sfw/nsfw flag is present.

    v2 (legacy): qixi-30-virgin__nsfw, __sfw, nsfw
    v3 (new):    My great title_NSFW_20261015, Pride invitation_SFW,
                 News update_NSFW
    """
    # v2: double underscore separator wins when its trailing token is a flag.
    if "__" in stem:
        topic, tail = stem.rsplit("__", 1)
        flag = tail.strip().lower()
        if flag in FLAGS:
            return ParsedName(topic.strip(), flag, None)

    # v3 with explicit date: <Title>_<flag>_<YYYYMMDD>
    parts = stem.rsplit("_", 2)
    if len(parts) == 3:
        title, flag_tok, date_tok = parts
        if flag_tok.strip().lower() in FLAGS and _looks_like_date_token(date_tok):
            publish_date = _parse_date_token(date_tok)
            if publish_date is None:
                return None  # date-shaped but impossible (e.g. 20260230)
            return ParsedName(title.strip(), flag_tok.strip().lower(), publish_date)

    # v3 without date: <Title>_<flag>
    parts = stem.rsplit("_", 1)
    if len(parts) == 2 and parts[1].strip().lower() in FLAGS:
        return ParsedName(parts[0].strip(), parts[1].strip().lower(), None)

    # v2 convenience: bare nsfw / sfw, topic left for auto-naming.
    if stem.strip().lower() in FLAGS:
        return ParsedName("", stem.strip().lower(), None)
    return None


def parse_name(stem: str) -> tuple[str, str] | None:
    """Back-compat wrapper returning (topic, flag) or None. Accepts v2 and v3."""
    parsed = parse_filename(stem)
    if parsed is None:
        return None
    return parsed.topic, parsed.flag


def _drive_remote_and_root() -> tuple[str, str]:
    remote = os.environ.get("SAU_INBOX_DRIVE_REMOTE", "GDrive-willywang8216").strip()
    root = os.environ.get("SAU_INBOX_DRIVE_ROOT", "SAU-Inbox").strip("/")
    if not remote or any(ch in remote for ch in ":/\\\0") or not root:
        raise ValueError("SAU inbox Drive remote configuration is invalid")
    return remote, root


def _rclone_command(args: list[str]) -> list[str]:
    """Run via the application container, which owns the restricted Drive config."""
    mode = os.environ.get("SAU_INBOX_RCLONE_MODE", "docker").strip().lower()
    if mode == "host":
        config = os.environ.get("SAU_INBOX_RCLONE_CONFIG", "").strip()
        return ["rclone", *( ["--config", config] if config else []), *args]
    if mode != "docker":
        raise ValueError("SAU_INBOX_RCLONE_MODE must be 'docker' or 'host'")
    return ["docker", "exec", "social-auto-upload", "rclone", *args]


def _scan_host_to_container(path: Path) -> str:
    try:
        relative = path.resolve().relative_to(INBOX.resolve())
    except ValueError as exc:
        raise ValueError("Temporary inbox download escaped the mounted inbox") from exc
    return f"/app/sau-inbox/{relative.as_posix()}"


def _cleanup_scan_root(root: Path) -> None:
    """Remove one generated scan directory, including root-owned Docker files."""
    try:
        relative = root.resolve().relative_to(INBOX.resolve())
    except ValueError as exc:
        raise ValueError("Refusing to remove scan directory outside inbox") from exc
    if len(relative.parts) != 1 or not relative.name.startswith(".sau-inbox-scan-"):
        raise ValueError("Refusing to remove non-scan inbox path")
    try:
        container_path = _scan_host_to_container(root)
        if not container_path.startswith("/app/sau-inbox/.sau-inbox-scan-"):
            raise ValueError("Refusing to remove unexpected container scan path")
        subprocess.run(
            ["docker", "exec", "social-auto-upload", "rm", "-rf", container_path],
            check=True, capture_output=True, text=True, timeout=30,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


class DriveQuotaError(RuntimeError):
    """Google Drive answered 403 rate-limit/quota for the listing.

    Distinct from a generic failure because it is *expected* to clear on its own
    within a minute, must not page anyone on the first occurrence, and needs a
    much longer backoff than a network blip.
    """


_QUOTA_MARKERS = ("quota exceeded", "rateLimitExceeded", "userRateLimitExceeded", "Error 403")


def _drive_list(runner=None) -> list[dict]:
    """List all files in the Drive inbox recursively.

    Uses --fast-list for a single recursive request (much faster on large drives).
    Retries with exponential backoff; a Drive rate limit gets a longer schedule
    because the window is a minute, not a second, and the box has several rclone
    consumers (watcher, archive, offload, agents) sharing one Google project.
    """
    remote, root = _drive_remote_and_root()
    run = runner or subprocess.run
    timeout = int(os.environ.get("SAU_INBOX_LIST_TIMEOUT", "300"))
    max_retries = int(os.environ.get("SAU_INBOX_LIST_RETRIES", "5"))
    quota_backoff = [10, 30, 45, 60]

    last_error = None
    last_was_quota = False
    for attempt in range(max_retries):
        try:
            result = run(
                _rclone_command([
                    "lsjson", f"{remote}:{root}", "--recursive", "--files-only",
                    "--fast-list",
                    # Be a polite API citizen: a burst is what triggers the 403
                    # in the first place (observed 2026-10-27).
                    "--tpslimit", os.environ.get("SAU_INBOX_TPSLIMIT", "4"),
                    "--tpslimit-burst", "2",
                ]),
                check=True, capture_output=True, text=True,
                timeout=timeout,
            )
            entries = json.loads(result.stdout or "[]")
            if not isinstance(entries, list):
                raise ValueError("rclone returned an invalid Drive inbox listing")
            if attempt > 0:
                LOG.info("Drive listing succeeded on attempt %d", attempt + 1)
            return entries
        except subprocess.TimeoutExpired as exc:
            last_error = exc
            LOG.warning("Drive listing timed out after %ds (attempt %d/%d)",
                       timeout, attempt + 1, max_retries)
        except subprocess.CalledProcessError as exc:
            last_error = exc
            stderr_snippet = (exc.stderr or "")[:500]
            last_was_quota = any(marker.lower() in stderr_snippet.lower()
                                 for marker in _QUOTA_MARKERS)
            if last_was_quota:
                LOG.warning(
                    "Drive rate limit hit (attempt %d/%d); backing off: %s",
                    attempt + 1, max_retries, stderr_snippet.splitlines()[0][:160],
                )
            else:
                LOG.warning("Drive listing failed (attempt %d/%d): %s",
                            attempt + 1, max_retries, stderr_snippet)
        except json.JSONDecodeError as exc:
            # Don't retry parse errors — they indicate a real problem
            raise ValueError(f"rclone returned invalid JSON: {exc}") from exc

        # Exponential backoff: 2s, 4s, 8s ... or the quota schedule when Drive
        # is rate-limiting us (the window is a minute, so short sleeps just burn
        # requests and extend the block).
        if attempt < max_retries - 1:
            if last_was_quota:
                backoff = quota_backoff[min(attempt, len(quota_backoff) - 1)]
            else:
                backoff = 2 ** (attempt + 1)
            LOG.info("Retrying Drive listing in %ds...", backoff)
            time.sleep(backoff)

    # All retries exhausted
    if last_was_quota:
        raise DriveQuotaError(
            f"Drive rate limit persisted through {max_retries} attempts: {last_error}"
        ) from last_error
    raise RuntimeError(
        f"Drive listing failed after {max_retries} attempts: {last_error}"
    ) from last_error


_MAGIC_KINDS: tuple[tuple[bytes, str], ...] = (
    (b"\x1a\x45\xdf\xa3", "video"),           # Matroska / WebM
    (b"\xff\xd8\xff", "img"),                  # JPEG
    (b"\x89PNG\r\n\x1a\n", "img"),            # PNG
    (b"GIF87a", "img"),
    (b"GIF89a", "img"),
)


def _sniff_kind(remote: str, root: str, rel: str) -> str | None:
    """``video``/``img`` for a file whose extension is unknown, else None.

    The operator's uploader sometimes drops the extension ("…__sfw" instead of
    "…__sfw.mp4", 2026-10-27). The strict suffix rule then made the file
    invisible to the watcher — no notification, no queue entry — which is the
    silent-failure class this pipeline must not have. Read 16 bytes from Drive
    and decide from the container magic instead.
    """
    try:
        result = subprocess.run(
            _rclone_command(["cat", f"{remote}:{root}/{rel}", "--count", "32", "--offset", "0"]),
            capture_output=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:  # noqa: BLE001
        LOG.warning("could not sniff %s: %s", rel, exc)
        return None
    head = result.stdout or b""
    if len(head) < 12:
        return None
    if head[4:8] == b"ftyp":  # ISO base media (mp4/mov/m4v)
        return "video"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "img"
    for magic, kind in _MAGIC_KINDS:
        if head.startswith(magic):
            return kind
    return None


_EXT_FOR_KIND = {"video": ".mp4", "img": ".jpg"}


def _sidecar_candidates(rel: str) -> list[str]:
    """Same-name sibling notes that can carry the transcript/copy for a media file."""
    remote = PurePosixPath(rel)
    return [
        str(remote.with_suffix(".txt")),
        str(remote.with_suffix(".md")),
        f"{remote.with_suffix('')}.content.txt",
        str(remote.with_suffix(".docx")),
    ]


def _download_sidecars(remote: str, root: str, rel: str, item_root: Path,
                       remote_files: set[str]) -> int:
    """Fetch optional same-folder notes; return how many were downloaded."""
    downloaded = 0
    for sidecar_rel in _sidecar_candidates(rel):
        if sidecar_rel not in remote_files:
            continue
        sidecar = item_root / sidecar_rel
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            _rclone_command(["copyto", f"{remote}:{root}/{sidecar_rel}", _scan_host_to_container(sidecar)]),
            check=True, capture_output=True, text=True,
            timeout=int(os.environ.get("SAU_INBOX_DOWNLOAD_TIMEOUT", "1800")),
        )
        downloaded += 1
    return downloaded


def _chown_scan_root(item_root: Path) -> None:
    subprocess.run(
        ["docker", "exec", "social-auto-upload", "chown", "-R",
         f"{os.getuid()}:{os.getgid()}", _scan_host_to_container(item_root)],
        check=True, capture_output=True, text=True, timeout=30,
    )


def _remote_rel_path(value: str) -> str | None:
    raw = str(value or "").replace("\\", "/")
    path = PurePosixPath(raw)
    parts = path.parts
    if (path.is_absolute() or len(parts) < 3 or parts[0] not in PERSONA_PROFILES
            or parts[1] not in ("video", "img")
            or any(part in ("", ".", "..") for part in parts)):
        return None
    return str(path)


def detect_new_files(state: dict) -> list[tuple[Path, str, str]]:
    """Download unprocessed Drive media to a temporary path for one scan."""
    remote, root = _drive_remote_and_root()
    processed = state.get("processed", {})
    processed_paths = set()
    for key in processed:
        candidate = str(key)
        if candidate.startswith("drive:"):
            processed_paths.add(candidate[len("drive:"):])
            continue
        try:
            processed_paths.add(Path(candidate).resolve().relative_to(INBOX.resolve()).as_posix())
        except (ValueError, OSError):
            continue

    def known_paths(status: str) -> set[str]:
        paths = set()
        for item in state.get(status, []):
            remote_path = item.get("remotePath")
            if remote_path:
                paths.add(remote_path)
                continue
            source_path = str(item.get("sourcePath") or item.get("path") or "")
            try:
                paths.add(Path(source_path).resolve().relative_to(INBOX.resolve()).as_posix())
            except (ValueError, OSError):
                continue
        return paths

    ready_paths = known_paths("ready")
    pending_paths = known_paths("pending")
    quarantined_paths = known_paths("quarantined")
    found: list[tuple[Path, str, str]] = []
    INBOX.mkdir(parents=True, exist_ok=True)
    # Prevent another cron tick from cleaning temp files mid-scan, then recover
    # remnants of a previous process which exited before its finally clause.
    for abandoned in INBOX.glob(".sau-inbox-scan-*"):
        if abandoned.is_dir():
            _cleanup_scan_root(abandoned)
    temp_root = Path(tempfile.mkdtemp(prefix=".sau-inbox-scan-", dir=INBOX))
    try:
        drive_entries = _drive_list()
        remote_files = {
            str(entry.get("Path") or entry.get("Name") or "").replace("\\", "/")
            for entry in drive_entries
            if not entry.get("IsDir")
        }
        for entry in drive_entries:
            rel = _remote_rel_path(entry.get("Path") or entry.get("Name"))
            if not rel:
                continue
            if rel in ready_paths | pending_paths | quarantined_paths:
                continue
            if rel in processed_paths or rel in processed:
                continue
            sniffed_kind = None
            if Path(rel).suffix.lower() not in MEDIA_EXTS:
                # Unknown extension: ask the bytes instead of ignoring the file.
                # Only for objects we have not queued yet — a queued file is
                # skipped above, so this never re-reads or re-warns each scan.
                sniffed_kind = _sniff_kind(remote, root, rel)
                if sniffed_kind is None:
                    continue
                LOG.warning(
                    "%s has no media extension; treating it as %s (rename it to %s%s)",
                    rel, sniffed_kind, rel, _EXT_FOR_KIND[sniffed_kind],
                )
            persona = rel.split("/", 1)[0]
            item_root = temp_root / str(len(found))
            # The Drive path keeps its name (that is the archive identity); only
            # the local temp copy gets an extension so the rest of the pipeline
            # (kind, sidecar lookup, thumbnail) sees a normal media file.
            local_rel = rel if sniffed_kind is None else f"{rel}{_EXT_FOR_KIND[sniffed_kind]}"
            safe_dest = item_root / local_rel
            _parsed = parse_filename(Path(rel).stem)
            if _parsed and _parsed.flag == "nsfw":
                # nsfw thumbnails are generated title cards (no source bytes are
                # read), and the worker re-fetches from Drive at publish time via
                # remotePath, so skip the slow video download entirely. The tiny
                # sibling note (transcript) is still fetched — plain text, never
                # source pixels, so the no-frame policy is untouched. sfw still
                # downloads because its frame thumbnail needs the source pixels.
                if _download_sidecars(remote, root, rel, item_root, remote_files):
                    _chown_scan_root(item_root)
                found.append((safe_dest, persona, rel))
                continue
            safe_dest.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                _rclone_command(["copyto", f"{remote}:{root}/{rel}", _scan_host_to_container(safe_dest)]),
                check=True, capture_output=True, text=True,
                timeout=int(os.environ.get("SAU_INBOX_DOWNLOAD_TIMEOUT", "1800")),
            )
            if not safe_dest.is_file() or safe_dest.stat().st_size <= 0:
                raise RuntimeError(f"Drive download produced no data for {rel}")
            if entry.get("Size") is not None and safe_dest.stat().st_size != int(entry["Size"]):
                raise RuntimeError(f"Drive download size mismatch for {rel}")
            # Preserve optional same-folder notes (.txt/.md/.content.txt) while
            # keeping them only in the temporary scan tree, never in the
            # persistent inbox cache.
            _download_sidecars(remote, root, rel, item_root, remote_files)
            _chown_scan_root(item_root)
            found.append((safe_dest, persona, rel))
        return found
    except Exception:
        _cleanup_scan_root(temp_root)
        raise
    finally:
        # Keep temp files through process_paths; it deletes this directory
        # after thumbnails/content notes have been prepared.
        if not found and temp_root.exists():
            _cleanup_scan_root(temp_root)


def _cleanup_scan_sources(paths: list[tuple[Path, str, str]]) -> None:
    roots = {path.parents[3] for path, _, _ in paths}
    for root in roots:
        _cleanup_scan_root(root)


def _processed_media_key(remote_path: str) -> str:
    return f"drive:{remote_path}"


def human_size(num: float) -> str:
    """1048576 -> '1.0 MB'。"""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def detect_orphans(state: dict) -> list[dict]:
    """找出 watcher 掃不到的媒體檔（inbox 根目錄、以及未知資料夾的直屬檔）。

    只回傳「新的或變過的」——以 state["orphans"] 記錄的 size:mtime 為鍵去重，
    所以同一顆沒動過的檔不會每分鐘重發通知，但覆蓋或重新丟一次會再告警。
    只讀取、只記錄，**絕不搬動或刪除任何檔案**。
    """
    known: dict = state.setdefault("orphans", {})
    stale: list[dict] = []
    if not INBOX.is_dir():
        return stale

    # 修剪：檔案已經不在了就把記錄刪掉。否則每顆來過一次的孤兒都會永久佔一筆，
    # state.json 會無界成長；而且舊簽章留著會讓「同路徑同大小同 mtime」的檔案
    # 不再告警。
    for key in [k for k in known if not Path(k).exists()]:
        known.pop(key, None)

    allow = set(ROOT_ALLOWLIST)
    allow |= {n.strip() for n in os.environ.get("SAU_WATCH_ORPHAN_ALLOW", "").split(",") if n.strip()}
    now = time.time()

    def consider(p: Path) -> None:
        name = p.name
        if not p.is_file() or name.startswith(ROOT_ALLOW_PREFIXES) or name in allow:
            return
        if p.suffix.lower() not in MEDIA_EXTS:
            return
        try:
            st = p.stat()
        except OSError:
            return
        if now - st.st_mtime < MIN_AGE_SEC:
            return  # 可能還在寫入（rclone / 手機上傳），下一輪再看
        key = str(p.resolve())
        sig = f"{st.st_size}:{int(st.st_mtime)}"
        if known.get(key, {}).get("sig") == sig:
            return  # 同一顆、沒變過
        entry = {"path": key, "name": name, "size": st.st_size,
                 "mtime": st.st_mtime, "sig": sig, "at": utcnow()}
        known[key] = dict(entry)
        stale.append(entry)
        LOG.warning("孤兒 %s（%s）在 %s —— watcher 不會處理，只會佔磁碟",
                    name, human_size(st.st_size), p.parent)

    # 1) 根目錄的直屬檔案
    for p in INBOX.iterdir():
        if not p.is_dir():
            consider(p)

    # 2) 未知資料夾裡的直屬媒體檔（只掃一層：未知結構無限遞迴成本不可控）
    for d in INBOX.iterdir():
        # `_`/`.` 開頭是內部物（_pub/_batch/.stt-tmp/...) 與同檔慣例一致，不告警。
        if (not d.is_dir() or d.name.startswith(("_", "."))
                or d.name in KNOWN_ROOT_DIRS):
            continue
        try:
            for p in d.iterdir():
                consider(p)
        except OSError:
            continue
    return stale


# ---- 縮圖 ----
def build_thumb(entry: dict, source: Path, persona: str) -> None:
    """依 sfw/nsfw 產縮圖，把結果寫回 entry。"""
    flag = entry["sfwFlag"]
    stem = source.stem
    out_dir = INBOX / persona / "thumb"
    out_dir.mkdir(parents=True, exist_ok=True)
    title = entry["topic"] or "(自動命名待補)"

    if flag == "nsfw":
        # 純文字安全卡：零外洩，等人工挑幀後才換真幀。
        #
        # 2026-10-20: this branch used to be unguarded. When Pillow went missing
        # on the host interpreter (`ModuleNotFoundError: No module named 'PIL'`)
        # every nsfw item raised here, which aborted `process_paths()` *before*
        # `save_state()` — so no new uploads reached `ready` at all and the
        # one-minute cron crash-looped forever (781 KB of tracebacks in
        # watch.log). A thumbnail is decoration; never let it stop the queue.
        out = out_dir / f"{stem}_thumb.jpg"
        try:
            thumb.make_titlecard(
                str(out), title=title, subtitle="NSFW 影片・安全預覽", badge="NSFW",
                accent=PERSONA_ACCENT.get(persona, (214, 48, 49)),
            )
            entry["thumbPath"] = str(out)
            entry["thumbKind"] = "titlecard"
            entry["needsFrameSelection"] = True
            LOG.info("nsfw %s → titlecard（需人工選幀）", stem)
        except Exception as exc:  # noqa: BLE001 — thumb is optional, the queue is not
            LOG.error("nsfw %s 安全卡失敗: %s", stem, exc)
            entry["thumbPath"] = None
            entry["thumbKind"] = "error"
            entry["needsFrameSelection"] = True
            entry["error"] = str(exc)
        return

    # sfw：抽真幀
    out = out_dir / f"{stem}_thumb.jpg"
    try:
        thumb.make_thumb(
            str(source), str(out), title=title,
            anchor="center" if source.suffix.lower() not in VIDEO_EXTS else "center",
        )
        entry["thumbPath"] = str(out)
        entry["thumbKind"] = "frame"
        entry["needsFrameSelection"] = False
        LOG.info("sfw %s → frame thumb", stem)
    except Exception as exc:  # noqa: BLE001
        LOG.error("sfw %s 縮圖失敗: %s", stem, exc)
        entry["thumbKind"] = "error"
        entry["error"] = str(exc)


# ---- 通知（opt-in）----
def _maybe_auto_title(entry: dict, source: Path) -> None:
    """沒主題時，若同資料夾有同名 <stem>.txt（內容說明），用 LLM 生標題建議。

    不阻塞：LLM 掛掉/沒文字檔就留 needsTitleGeneration=true，之後再補。
    """
    if not entry.get("needsTitleGeneration"):
        return
    note = _read_sibling_note(source)
    if not note:
        return
    try:
        import gen_title
        brand = PERSONA_BRAND.get(entry["persona"], "")
        suggested = gen_title.generate_title(
            note, persona=brand, sfw=entry["sfwFlag"] == "sfw",
        )
        entry["titleSuggestion"] = suggested
        entry["needsTitleGeneration"] = False
        LOG.info("%s 自動標題: %s", source.name, suggested[:60].replace("\n", " / "))
    except Exception as exc:  # noqa: BLE001
        LOG.warning("%s 自動標題失敗: %s", source.name, exc)


def notify(title: str, body: str) -> bool:
    token = os.environ.get("SAU_WATCH_TG_BOT_TOKEN", "").strip()
    chat = os.environ.get("SAU_WATCH_TG_CHAT_ID", "").strip()
    if not token or not chat:
        LOG.info("Telegram 未設定，跳過通知：%s — %s", title, body[:80])
        return False
    import requests
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    delivered = False
    for chat_id in [c.strip() for c in chat.replace(";", ",").split(",") if c.strip()]:
        try:
            requests.post(url, json={
                "chat_id": chat_id, "text": f"📬 {title}\n{body}",
                "disable_web_page_preview": True,
            }, timeout=20).raise_for_status()
            delivered = True
        except Exception as exc:  # noqa: BLE001
            LOG.warning("TG 通知失敗 %s: %s", chat_id, exc)
    if not delivered:
        # Do not arm the cooldown when nothing was actually delivered: a broken
        # channel must keep retrying, not go quiet for half an hour.
        LOG.error("所有 Telegram 通知都失敗（title=%s）", title)
    return delivered


# ---- health + crash alerting ----
# 2026-10-17..20 事故：Pillow 不在 cron 的解譯器裡，每一次掃描都在
# build_thumb 炸掉（traceback 只進 watch.log，沒人看），新上傳因此整整四天
# 沒有進 ready。log 檔不是通知管道 —— 這裡把「掃描失敗」與「根本沒在跑」
# 都推到 Telegram，並寫一份 health 檔讓外部 watchdog 讀。
HEALTH_PATH = Path(os.environ.get("SAU_WATCH_HEALTH", str(HERE / ".watch-health.json")))
ALERT_COOLDOWN_SEC = float(os.environ.get("SAU_WATCH_ALERT_COOLDOWN", "1800"))


def _load_health() -> dict:
    try:
        return json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_health(**fields) -> None:
    data = _load_health()
    data.update(fields)
    data["pid"] = os.getpid()
    try:
        tmp = HEALTH_PATH.with_name(f"{HEALTH_PATH.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(HEALTH_PATH)
    except OSError as exc:  # noqa: BLE001 - health bookkeeping must never break a scan
        LOG.warning("health 檔寫入失敗: %s", exc)


def alert_once(signature: str, title: str, body: str) -> bool:
    """Telegram alert at most once per ``ALERT_COOLDOWN_SEC`` per signature."""
    now = time.time()
    health = _load_health()
    last = health.get("alerts", {}).get(signature, 0)
    if now - float(last or 0) < ALERT_COOLDOWN_SEC:
        LOG.info("同類告警冷卻中，跳過：%s", signature)
        return False
    sent = notify(title, body)
    if not sent:
        return False
    alerts = dict(health.get("alerts") or {})
    alerts[signature] = now
    # keep the map tiny: only retain recent signatures
    alerts = {k: v for k, v in alerts.items() if now - float(v or 0) < 7 * 24 * 3600}
    write_health(alerts=alerts)
    return sent


def report_scan_failure(exc: BaseException) -> None:
    import traceback

    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    signature = f"scan-error:{type(exc).__name__}:{str(exc)[:80]}"
    write_health(
        last_error=utcnow(),
        last_error_epoch=time.time(),
        last_error_type=type(exc).__name__,
        last_error_message=str(exc)[:300],
        last_error_traceback=tb[-2000:],
    )
    alert_once(
        signature,
        "🚨 SAU inbox watcher 掃描失敗",
        f"{type(exc).__name__}: {exc}\n\n"
        f"新上傳目前不會進佇列。以下是最後幾行：\n\n{tb.strip().splitlines()[-1][:300]}\n\n"
        f"log: /home/will/sau-inbox/watch.log",
    )


# ---- 主要掃描 ----
def process_paths(state: dict) -> dict:
    """Scan and process Drive items without keeping originals on the VPS."""
    processed = state.setdefault("processed", {})
    processed_before = dict(processed)
    now_s = utcnow()

    # Migrate old local-path queue entries to remote identities before originals
    # are removed; concurrent API mutations are blocked by scan_once's lock.
    for status in ("ready", "pending", "quarantined"):
        for item in state.get(status, []):
            raw = str(item.get("sourcePath") or item.get("path") or "")
            if item.get("remotePath"):
                continue
            try:
                rel = Path(raw).resolve().relative_to(INBOX.resolve()).as_posix()
            except (ValueError, OSError):
                continue
            if _remote_rel_path(rel):
                item["remotePath"] = rel
                item["sourcePath"] = rel
                if status == "pending":
                    item["path"] = rel

    new_ready: list[dict] = []
    new_pending: list[dict] = []
    # 先掃孤兒（根目錄／未知資料夾的媒體），再一起 save_state ——
    # 去重記錄要跟這輪的結果原子地寫進去，才不會重複告警。
    new_orphans = detect_orphans(state)

    scan_sources = detect_new_files(state)
    try:
        for path, persona, remote_path in scan_sources:
            key = _processed_media_key(remote_path)
            parsed = parse_filename(path.stem)
            if parsed is None:
                entry = {"path": key, "remotePath": remote_path, "persona": persona,
                         "reason": "檔名缺 <sfw|nsfw> 標籤（舊式 <主題>__<sfw|nsfw> 或"
                                   "新式 <主題>_<SFW|NSFW>_<YYYYMMDD>，型別看副檔名）", "at": now_s}
                state.setdefault("pending", []).append(entry)
                new_pending.append(entry)
                processed[key] = now_s
                continue

            topic, flag = parsed.topic, parsed.flag
            publish_date = parsed.publish_date
            kind = "video" if path.suffix.lower() in VIDEO_EXTS else "img"
            profile_ids = PERSONA_PROFILES.get(persona)
            if not profile_ids:
                processed[key] = now_s
                continue

            note = _read_sibling_note(path)
            language = detect_language(note, topic)
            if persona == "teaching":
                hits = [w for w in TEACHING_BAD_MARKERS if w in (topic + " " + note).lower()]
                if flag == "nsfw" or hits:
                    reason = "teaching 收到 nsfw 素材" if flag == "nsfw" else f"teaching 內容疑似帶 NW/SW 字樣：{hits}"
                    entry = {"path": key, "remotePath": remote_path, "persona": persona,
                             "topic": topic, "flag": flag, "reason": reason,
                             "at": now_s, "snippet": note[:200]}
                    state.setdefault("quarantined", []).append(entry)
                    processed[key] = now_s
                    notify("🚨 teaching 隔離擋下", f"{path.name}\n{reason}\n絕不會發任何教學帳號。")
                    continue

            topic_label = topic or "（無主題）"
            if note:
                brief = (f"【素材】{persona}/{kind}（{flag} 版），主題「{topic_label}」。\n"
                         f"【內容文稿/描述】\n{note[:4000]}\n\n請依以上內容與品牌調性發想各平台適合的標題＋文案。")
            else:
                brief = (f"「{topic}」{kind}（{flag} 版）。請依此發想各平台適合的文案。" if topic
                         else f"新素材（{kind}, {flag} 版，無主題標籤）。請依影片內容生成 SEO/吸睛標題與各平台文案。")

            entry = {
                "id": f"{persona}:{path.stem}:{now_s}", "persona": persona,
                "profileIds": profile_ids, "topic": topic, "title": topic,
                "sfwFlag": flag, "publishDate": publish_date,
                "language": language, "transcript": note or None,
                "kind": kind,
                "sourcePath": str(INBOX / persona / kind / path.name),
                "remotePath": remote_path,
                "thumbPath": None, "thumbKind": None,
                "needsFrameSelection": False,
                "needsTitleGeneration": bool(not topic),
                "contentNote": note or None, "brief": brief,
                "status": "ready", "createdAt": now_s,
            }
            # "Publish unless I object": the deadline is decided once, here, and
            # announced in the ready message; myUtils.inbox_autopublish acts on it.
            auto_hours = float(os.environ.get("SAU_INBOX_AUTOPUBLISH_HOURS", "24") or 0)
            if auto_hours > 0 and str(os.environ.get("SAU_INBOX_AUTOPUBLISH", "1")).lower() not in (
                "0", "false", "no", "off"
            ):
                deadline = datetime.fromisoformat(now_s) + timedelta(hours=auto_hours)
                entry["autoPublishAt"] = deadline.isoformat(timespec="seconds")
                entry["autoPublishHours"] = auto_hours
            try:
                build_thumb(entry, path, persona)
            except Exception as exc:  # noqa: BLE001 — never lose a ready item over a thumb
                LOG.error("縮圖步驟失敗（不影響入列）%s: %s", path.name, exc)
                entry.setdefault("thumbPath", None)
                entry.setdefault("thumbKind", "error")
                entry["error"] = str(exc)
            _maybe_auto_title(entry, path)
            state.setdefault("ready", []).append(entry)
            new_ready.append(entry)
            processed[key] = now_s
        with _state_lock():
            latest = load_state()
            for key, value in processed.items():
                if key not in processed_before or processed_before.get(key) != value:
                    latest.setdefault("processed", {})[key] = value
            for status, new_items in (("ready", new_ready), ("pending", new_pending)):
                known = {
                    str(item.get("remotePath") or item.get("path") or "")
                    for list_name in ("ready", "pending", "quarantined", "processed_items")
                    for item in latest.get(list_name, [])
                }
                appended = []
                for item in new_items:
                    identity = str(item.get("remotePath") or item.get("path") or "")
                    if identity in known or identity in latest.get("processed", {}):
                        continue
                    latest.setdefault(status, []).append(item)
                    known.add(identity)
                    appended.append(item)
                if status == "ready":
                    new_ready = appended
                else:
                    new_pending = appended
            # Persist one-way migration fields for existing queue entries while
            # retaining any transitions the web app made during the scan.
            for status in ("ready", "pending", "quarantined"):
                latest_items = latest.setdefault(status, [])
                by_id = {str(item.get("id")): item for item in latest_items if item.get("id")}
                for scanned in state.get(status, []):
                    item_id = str(scanned.get("id") or "")
                    current = by_id.get(item_id) if item_id else None
                    if current is None or current.get("remotePath"):
                        continue
                    if scanned.get("remotePath"):
                        current["remotePath"] = scanned["remotePath"]
                        if status == "pending":
                            current["path"] = scanned.get("path", current.get("path"))
                        else:
                            current["sourcePath"] = scanned.get("sourcePath", current.get("sourcePath"))
            latest_quarantined = {
                str(item.get("remotePath") or item.get("path") or "")
                for item in latest.get("quarantined", [])
            }
            for item in state.get("quarantined", []):
                identity = str(item.get("remotePath") or item.get("path") or "")
                if identity not in latest_quarantined:
                    latest.setdefault("quarantined", []).append(item)
                    latest_quarantined.add(identity)
            latest.setdefault("orphans", {}).update(state.get("orphans", {}))
            save_state(latest)
    finally:
        _cleanup_scan_sources(scan_sources)
    return {"ready": new_ready, "pending": new_pending, "orphans": new_orphans}


#: Jobs the pipeline depends on, by a stable substring of their crontab line. The
#: watcher runs from ``/etc/cron.d/sau-inbox-watch`` (a *different* scheduler), so
#: it is the one process that can still notice when the operator's own crontab is
#: emptied — which is exactly what happened on 2026-10-28: the whole file was
#: replaced with a single bisync line, taking the monitors, the deployer, the
#: Drive offload and the daily backups with it. Nothing noticed for a day and a
#: half, because every one of those jobs was itself the thing that would have
#: raised the alarm.
CRON_GUARD_MARKERS = (
    ("ci-watch", "scripts/ci-watch.sh"),
    ("offload", "offload_to_drive.sh"),
    ("pipeline monitor", "myUtils.pipeline_monitor"),
    ("auto-archive", "myUtils.inbox_archive"),
    ("auto-publish", "myUtils.inbox_autopublish"),
    ("watcher watchdog", "inbox_monitor.py"),
    ("disk reclaim", "scripts/disk-reclaim.sh"),
    ("publish digest", "myUtils.publish_digest"),
)
CRON_GUARD_INTERVAL_SEC = float(os.environ.get("SAU_WATCH_CRON_GUARD_SEC", "1800"))


def check_cron_guard(*, runner=None) -> list[str]:
    """Alert when the operator's crontab no longer schedules the pipeline.

    Returns the labels that are missing (empty = healthy). Runs at most once per
    ``SAU_WATCH_CRON_GUARD_SEC``; a host without ``crontab`` at all is left alone
    (some deployments schedule everything externally).
    """
    health = _load_health()
    now = time.time()
    if now - float(health.get("cron_guard_at") or 0) < CRON_GUARD_INTERVAL_SEC:
        return []
    write_health(cron_guard_at=now)
    run = runner or subprocess.run
    try:
        result = run(["crontab", "-l"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
        LOG.info("cron guard: 讀不到 crontab，跳過（%s）", exc)
        return []
    if getattr(result, "returncode", 1) != 0:
        LOG.info("cron guard: crontab -l 失敗，跳過")
        return []
    text = result.stdout or ""
    active = [
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    missing = [label for label, marker in CRON_GUARD_MARKERS if not any(marker in l for l in active)]
    write_health(cron_guard_missing=missing)
    if missing:
        alert_once(
            "cron-guard",
            "🚨 排程不見了（SAU crontab）",
            "以下工作已不在 crontab 內：\n"
            + "\n".join(f"• {m}" for m in missing)
            + f"\n\n目前 crontab 只剩 {len(active)} 行。"
            "排程被清空時，監控、部署、offload、備份都會一起停掉，"
            "而且沒有東西會通知你。備份在 ~/.config/crontab-keep/。",
        )
    return missing


def scan_once() -> int:
    try:
        check_cron_guard()
    except Exception as exc:  # noqa: BLE001 - the guard must never break a scan
        LOG.warning("cron guard 失敗: %s", exc)
    # Serialize cron invocations for scan-temp ownership, while keeping the JSON
    # state lock short so approvals/rejections can happen during Drive transfer.
    try:
        with _watch_run_lock():
            result = process_paths(load_state())
    except DriveQuotaError as exc:
        # Expected to clear within a minute. Record it (so the watchdog can see
        # it) but do not alert: the watchdog already alerts when scans stop
        # succeeding for 15 minutes, which is the signal that actually matters.
        LOG.warning("Drive rate limit: %s", exc)
        write_health(last_quota_error=utcnow(), last_quota_message=str(exc)[:200])
        return 0
    for item in result["ready"]:
        try:
            _announce_ready(item)
        except Exception as exc:  # noqa: BLE001 - a message must never lose a scan
            LOG.error("ready 通知失敗（不影響入列）%s: %s", item.get("remotePath"), exc)
    for item in result["pending"]:
        try:
            notify("待確認標籤",
                   f"{Path(item['path']).name} 檔名缺 <sfw|nsfw> 標籤"
                   "（可為 <主題>__<sfw|nsfw> 或 <主題>_<SFW|NSFW>_<YYYYMMDD>）")
        except Exception as exc:  # noqa: BLE001
            LOG.error("pending 通知失敗: %s", exc)
    for item in result.get("orphans", []):
        try:
            _announce_orphan(item)
        except Exception as exc:  # noqa: BLE001
            LOG.error("orphan 通知失敗: %s", exc)
    state_now = load_state()
    write_health(
        last_success=utcnow(),
        last_success_epoch=time.time(),
        ready=len(state_now.get("ready", [])),
        pending=len(state_now.get("pending", [])),
        quarantined=len(state_now.get("quarantined", [])),
        new_ready=len(result["ready"]),
        new_pending=len(result["pending"]),
        last_error=None,
        last_error_type=None,
        last_error_message=None,
    )
    return len(result["ready"])


def _announce_ready(item: dict) -> None:
    """The "已備好" Telegram card for one newly ready item."""
    title = item["topic"] or "（待確認標題）"
    topic_ref = item["topic"] or "這支"
    sug = item.get("titleSuggestion")
    body = f"檔：{Path(item['sourcePath']).name}\n安全縮圖：{item['thumbKind']}"
    if sug:
        body += f"\n自動標題建議：\n{sug}"
    body += brand_note(item)
    body += f"\n回「{topic_ref} 發」或「{topic_ref} X點發」"
    deadline_raw = item.get("autoPublishAt")
    if deadline_raw:
        try:
            when = datetime.fromisoformat(deadline_raw).astimezone(
                timezone(timedelta(hours=8))
            ).strftime("%m-%d %H:%M")
            body += (f"\n\n⏳ 不回覆就自動發布：{when}（台北時間）"
                     f"\n要改／要停：現在回我，或到網頁 reject。")
        except ValueError:
            pass
    body += manual_block(item)
    notify(f"已備好：{title}（{item['persona']}）", body)


def _announce_orphan(item: dict) -> None:
    """Warn about media that landed where the watcher never looks."""
    when = datetime.fromtimestamp(item["mtime"], timezone.utc).isoformat(timespec="seconds")
    notify(
        "⚠️ 有檔案掉在 inbox 根目錄（永遠不會被發布）",
        f"{item['name']}\n"
        f"大小：{human_size(item['size'])}\n"
        f"修改時間：{when}\n"
        f"位置：{item['path']}\n\n"
        "這個位置 watcher 不會處理，所以它不會進 ready、不會被發布，"
        "只會一直佔磁碟。\n"
        "要發：搬到 nw/video/（或 sw/、both/）並改名成 "
        "<主題>__<sfw|nsfw>.mp4 或 <主題>_<SFW|NSFW>_<YYYYMMDD>.mp4；"
        "不要：請刪掉它。",
    )


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("SAU_WATCH_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description="SAU-Inbox watcher")
    ap.add_argument("--once", action="store_true", help="掃一輪就結束")
    args = ap.parse_args()

    if args.once:
        try:
            n = scan_once()
        except Exception as exc:  # noqa: BLE001 — cron exits 1 *and* alerts
            LOG.exception("掃描失敗: %s", exc)
            report_scan_failure(exc)
            return 1
        LOG.info("一次掃描完成，%d 支進 ready", n)
        return 0

    LOG.info("watcher daemon 啟動，每 %ss 掃一次（inbox=%s）", DEFAULT_INTERVAL, INBOX)
    while True:
        try:
            scan_once()
        except Exception as exc:  # noqa: BLE001 — daemon 永不因單輪失敗退出
            LOG.exception("掃描失敗: %s", exc)
            report_scan_failure(exc)
        time.sleep(DEFAULT_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
