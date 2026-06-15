"""Media asset service — batch upload, rclone integration, public URL resolution.

Manages MediaAsset records that represent uploaded files with rich metadata,
rclone remote paths, public URLs, and processing status.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator

from utils.conf_defaults import BASE_DIR

DB_PATH = Path(BASE_DIR) / "db" / "database.db"

UPLOAD_STATUS_PENDING = "pending"
UPLOAD_STATUS_UPLOADING = "uploading"
UPLOAD_STATUS_UPLOADED = "uploaded"
UPLOAD_STATUS_FAILED = "failed"

PROCESSING_STATUS_PENDING = "pending"
PROCESSING_STATUS_PROCESSING = "processing"
PROCESSING_STATUS_PROCESSED = "processed"
PROCESSING_STATUS_FAILED = "failed"
PROCESSING_STATUS_SKIPPED = "skipped"

MEDIA_TYPE_VIDEO = "video"
MEDIA_TYPE_IMAGE = "image"
MEDIA_TYPE_AUDIO = "audio"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".svg"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".aac", ".flac", ".m4a"}


@dataclass(slots=True)
class MediaAsset:
    id: int
    original_filename: str
    media_type: str
    mime_type: str
    local_original_path: str
    local_processed_path: str
    rclone_remote_path: str
    public_url: str
    processed_public_url: str
    thumbnail_public_url: str
    duration_seconds: float
    width: int
    height: int
    file_size: int
    checksum: str
    upload_status: str
    processing_status: str
    transcript_text: str
    content_analysis: dict
    metadata: dict
    file_record_id: int | None
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
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


def detect_media_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return MEDIA_TYPE_IMAGE
    if ext in VIDEO_EXTENSIONS:
        return MEDIA_TYPE_VIDEO
    if ext in AUDIO_EXTENSIONS:
        return MEDIA_TYPE_AUDIO
    return MEDIA_TYPE_VIDEO  # default


def compute_checksum(file_path: str | Path) -> str:
    """Compute SHA-256 checksum of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _row_to_asset(row: sqlite3.Row) -> MediaAsset:
    data = {key: row[key] for key in row.keys()}
    data["content_analysis"] = json.loads(data.get("content_analysis_json", "{}") or "{}")
    data["metadata"] = json.loads(data.get("metadata_json", "{}") or "{}")
    data.pop("content_analysis_json", None)
    data.pop("metadata_json", None)
    return MediaAsset(**data)


# --------------- CRUD ---------------

def create_media_asset(
    *,
    original_filename: str,
    local_original_path: str = "",
    media_type: str | None = None,
    mime_type: str = "",
    file_size: int = 0,
    checksum: str = "",
    file_record_id: int | None = None,
    metadata: dict | None = None,
    db_path: Path | None = None,
) -> MediaAsset:
    if media_type is None:
        media_type = detect_media_type(original_filename)
    now = _now_iso()
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO media_assets
            (original_filename, media_type, mime_type, local_original_path,
             file_size, checksum, upload_status, processing_status,
             content_analysis_json, metadata_json, file_record_id,
             created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                original_filename, media_type, mime_type, local_original_path,
                file_size, checksum, UPLOAD_STATUS_PENDING, PROCESSING_STATUS_PENDING,
                json.dumps({}), json.dumps(metadata or {}), file_record_id,
                now, now,
            ),
        )
        conn.commit()
        return get_media_asset(cur.lastrowid, db_path=db_path)


def get_media_asset(asset_id: int, *, db_path: Path | None = None) -> MediaAsset:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM media_assets WHERE id = ?", (asset_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"MediaAsset {asset_id} not found")
        return _row_to_asset(row)


def list_media_assets(
    *,
    media_type: str | None = None,
    upload_status: str | None = None,
    processing_status: str | None = None,
    limit: int = 200,
    offset: int = 0,
    db_path: Path | None = None,
) -> list[MediaAsset]:
    with _connect(db_path) as conn:
        query = "SELECT * FROM media_assets WHERE 1=1"
        params: list = []
        if media_type:
            query += " AND media_type = ?"
            params.append(media_type)
        if upload_status:
            query += " AND upload_status = ?"
            params.append(upload_status)
        if processing_status:
            query += " AND processing_status = ?"
            params.append(processing_status)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(query, params).fetchall()
        return [_row_to_asset(r) for r in rows]


def update_media_asset(
    asset_id: int, *, db_path: Path | None = None, **fields
) -> MediaAsset:
    allowed = {
        "local_original_path", "local_processed_path", "rclone_remote_path",
        "public_url", "processed_public_url", "thumbnail_public_url",
        "duration_seconds", "width", "height", "file_size", "checksum",
        "upload_status", "processing_status", "transcript_text",
        "content_analysis", "metadata", "mime_type", "file_record_id",
    }
    updates = {}
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "content_analysis":
            k = "content_analysis_json"
            v = json.dumps(v)
        elif k == "metadata":
            k = "metadata_json"
            v = json.dumps(v)
        updates[k] = v
    if not updates:
        return get_media_asset(asset_id, db_path=db_path)
    updates["updated_at"] = _now_iso()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [asset_id]
    with _connect(db_path) as conn:
        conn.execute(f"UPDATE media_assets SET {set_clause} WHERE id = ?", values)
        conn.commit()
    return get_media_asset(asset_id, db_path=db_path)


def delete_media_asset(asset_id: int, *, db_path: Path | None = None) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM media_assets WHERE id = ?", (asset_id,))
        conn.commit()


# --------------- rclone integration ---------------

def build_rclone_path(
    asset: MediaAsset,
    profile_slug: str,
    *,
    remote_root: str | None = None,
) -> str:
    """Build the rclone remote path for a media asset.

    Format: {remote_root}/{profile_slug}/{YYYY-MM-DD}/{asset_id}_{safe_filename}
    """
    root = (remote_root or os.environ.get("SAU_DEFAULT_RCLONE_PATH", "")).strip("/")
    date_str = datetime.now().strftime("%Y-%m-%d")
    safe_name = asset.original_filename.replace(" ", "_").replace("/", "_")
    parts = [p for p in [root, profile_slug, date_str] if p]
    return str(PurePosixPath(*parts) / f"{asset.id}_{safe_name}")


def upload_asset_to_rclone(
    asset: MediaAsset,
    profile_slug: str,
    *,
    remote_name: str | None = None,
    remote_root: str | None = None,
    db_path: Path | None = None,
) -> str:
    """Upload a media asset to rclone remote. Returns the remote path."""
    import subprocess

    remote = remote_name or os.environ.get("SAU_DEFAULT_RCLONE_REMOTE", "")
    if not remote:
        raise ValueError("No rclone remote configured. Set SAU_DEFAULT_RCLONE_REMOTE.")

    remote_path = build_rclone_path(asset, profile_slug, remote_root=remote_root)
    local_path = Path(asset.local_original_path)
    if not local_path.exists():
        raise FileNotFoundError(f"Local file not found: {local_path}")

    remote_spec = f"{remote}:{remote_path}"
    subprocess.run(
        ["rclone", "copy", str(local_path), str(PurePosixPath(remote).parent / PurePosixPath(remote_path).parent),
         "--no-traverse"],
        check=True, capture_output=True, text=True,
    )

    update_media_asset(
        asset.id,
        rclone_remote_path=remote_path,
        upload_status=UPLOAD_STATUS_UPLOADED,
        db_path=db_path,
    )
    return remote_path


def resolve_public_url(
    asset: MediaAsset,
    *,
    public_url_template: str | None = None,
) -> str:
    """Resolve a public URL for a media asset.

    Uses SAU_PUBLIC_URL_TEMPLATE if set, otherwise uses rclone_remote_path.
    """
    template = public_url_template or os.environ.get("SAU_PUBLIC_URL_TEMPLATE", "")
    if template:
        return template.format(
            remote=os.environ.get("SAU_DEFAULT_RCLONE_REMOTE", ""),
            remote_name=os.environ.get("SAU_DEFAULT_RCLONE_REMOTE", ""),
            remote_path=asset.rclone_remote_path,
            filename=asset.original_filename,
            campaign_id="",
        )
    return asset.rclone_remote_path


# --------------- Batch operations ---------------

def create_batch_assets(
    file_paths: list[str | Path],
    *,
    db_path: Path | None = None,
) -> list[MediaAsset]:
    """Create multiple MediaAsset records from local file paths."""
    assets = []
    for fp in file_paths:
        p = Path(fp)
        if not p.exists():
            continue
        file_size = p.stat().st_size
        checksum = compute_checksum(p)
        asset = create_media_asset(
            original_filename=p.name,
            local_original_path=str(p),
            file_size=file_size,
            checksum=checksum,
            db_path=db_path,
        )
        assets.append(asset)
    return assets
