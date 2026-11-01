"""Google Drive source helpers for the SAU inbox.

The inbox watcher stores only a remote-relative source reference. Publishing
stages that source under ``videoFile/_inbox_cache`` so the existing campaign,
worker, and offload lifecycle can manage it.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import uuid
from pathlib import Path, PurePosixPath

from utils.conf_defaults import BASE_DIR
from myUtils import rclone_config

DEFAULT_REMOTE = "GDrive-willywang8216"
DEFAULT_ROOT = "SAU-Inbox"
_PERSONA_DIRS = {"nw", "sw", "teaching", "msl", "both"}
_MEDIA_DIRS = {"video", "img"}


class InboxDriveError(RuntimeError):
    """A Drive inbox object could not be safely staged."""


def validate_remote_path(value: str) -> str:
    """Return a safe path relative to the configured Drive inbox root."""
    raw = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(raw)
    parts = path.parts
    if (
        not raw
        or path.is_absolute()
        or len(parts) < 3
        or parts[0] not in _PERSONA_DIRS
        or parts[1] not in _MEDIA_DIRS
        or any(part in {"", ".", ".."} for part in parts)
        or any("\x00" in part for part in parts)
    ):
        raise InboxDriveError("Invalid Drive inbox path")
    return str(path)


def remote_path_for_item(item: dict) -> str:
    """Get explicit remote metadata or safely derive a path from legacy inbox paths."""
    explicit = str(item.get("remotePath") or "").strip()
    if explicit:
        return validate_remote_path(explicit)
    raw = str(item.get("sourcePath") or "").replace("\\", "/")
    for marker in ("/sau-inbox/", "/app/sau-inbox/"):
        if marker in raw:
            return validate_remote_path(raw.split(marker, 1)[1])
    raise InboxDriveError("Inbox item has no Google Drive source reference")


def remote_spec(remote_path: str) -> str:
    rel = validate_remote_path(remote_path)
    remote = os.environ.get("SAU_INBOX_DRIVE_REMOTE", DEFAULT_REMOTE).strip()
    root = os.environ.get("SAU_INBOX_DRIVE_ROOT", DEFAULT_ROOT).strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", remote) or not root:
        raise InboxDriveError("Drive inbox remote configuration is invalid")
    return f"{remote}:{root}/{rel}"


def stage_remote_media(remote_path: str, *, runner=None) -> str:
    """Download a Drive inbox object into managed, videoFile-relative staging."""
    spec = remote_spec(remote_path)
    rel = validate_remote_path(remote_path)
    source_name = Path(rel).name
    if source_name in {"", ".", ".."} or "/" in source_name:
        raise InboxDriveError("Invalid Google Drive inbox filename")
    staging_dir = Path(BASE_DIR) / "videoFile" / "_inbox_cache"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = staging_dir.resolve()
    video_root = (Path(BASE_DIR) / "videoFile").resolve()
    if not staging_dir.is_relative_to(video_root):
        raise InboxDriveError("Inbox staging directory escaped videoFile")
    staging_dir.mkdir(parents=True, exist_ok=True)
    destination = staging_dir / f"{uuid.uuid4().hex}_{source_name}"
    run = runner or subprocess.run
    try:
        result = run(
            rclone_config.rclone_argv("copyto", spec, str(destination)),
            check=True,
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("SAU_INBOX_DOWNLOAD_TIMEOUT", "1800")),
        )
        if not destination.is_file() or destination.stat().st_size <= 0:
            raise InboxDriveError("Drive download completed without a non-empty file")
    except Exception as exc:
        destination.unlink(missing_ok=True)
        if isinstance(exc, InboxDriveError):
            raise
        detail = getattr(exc, "stderr", None)
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        message = str(detail or exc).strip()
        raise InboxDriveError(f"Could not download inbox media from Google Drive: {message}") from exc
    # UUID names prevent collisions across queued jobs while retaining the media
    # suffix so MIME and video/image classification remain correct.
    return f"_inbox_cache/{destination.name}"


def register_remote_source(remote_path: str, *, db_path: Path,
                           workspace_id: str | None = None) -> str:
    """Register an exact inbox object without network I/O or staging bytes.

    A stable directory (not a filename prefix) preserves the operator's SFW
    filename convention. Only trusted inbox state supplies this remote path;
    arbitrary browser clients cannot register a remote storage location.
    """
    from myUtils.sqlite_utils import connect
    from myUtils.campaign_media_prep import IMAGE_SUFFIXES, VIDEO_SUFFIXES

    rel = validate_remote_path(remote_path)
    spec = remote_spec(rel)
    remote, full_key = spec.split(":", 1)
    root = full_key[:-(len(rel) + 1)]
    identity = hashlib.sha256(f"{spec}\0{workspace_id or ''}".encode("utf-8")).hexdigest()
    name = PurePosixPath(rel).name
    if Path(name).suffix.lower() not in IMAGE_SUFFIXES | VIDEO_SUFFIXES:
        # The inbox's typed directory provides admission metadata for an
        # extensionless object; preparation still probes the downloaded bytes.
        name += ".mp4" if PurePosixPath(rel).parts[1] == "video" else ".jpg"
    local_ref = f"_inbox_cache/{identity}/{name}"
    slug = "inbox-" + hashlib.sha256(f"{remote}:{root}".encode()).hexdigest()[:24]
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        backend = conn.execute(
            "SELECT id, provider, bucket, endpoint FROM storage_backends WHERE slug = ?",
            (slug,),
        ).fetchone()
        if backend is None:
            cur = conn.execute(
                "INSERT INTO storage_backends "
                "(slug,label,provider,bucket,region,endpoint,access_key,secret_key) "
                "VALUES (?,?,'rclone',?,'',?,'','')",
                (slug, "Inbox source", remote, root),
            )
            backend_id = int(cur.lastrowid)
        else:
            backend_id, provider, bucket, endpoint = backend
            if (provider, bucket, endpoint) != ("rclone", remote, root):
                raise InboxDriveError("Registered inbox backend identity has changed")
        row = conn.execute(
            "SELECT storage_backend_id, storage_key, workspace_id FROM file_records "
            "WHERE file_path = ? ORDER BY id LIMIT 1", (local_ref,),
        ).fetchone()
        if row is not None:
            if tuple(row) != (backend_id, rel, workspace_id):
                raise InboxDriveError("Registered inbox source identity has changed")
        else:
            conn.execute(
                "INSERT INTO file_records "
                "(filename, filesize, file_path, storage_backend_id, storage_key, workspace_id) "
                "VALUES (?,0,?,?,?,?)",
                (name, local_ref, backend_id, rel, workspace_id),
            )
    return local_ref
