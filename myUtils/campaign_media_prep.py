"""Flask-free campaign media preparation.

Extracted from ``sau_backend._prepare_campaign_media_artifacts`` so the worker
process can run the transcode / watermark / split / remote-upload pipeline
without a Flask application or request context. Nothing in this module imports
Flask; the only request-derived value the caller used to supply implicitly (the
request host) is now the explicit ``public_base_url`` parameter.

This module is deliberately separate from ``myUtils.campaign_prep`` because a
parallel session owns that module (it holds ``finalize_campaign`` and is wired
into ``myUtils.worker``). See ``logs/async-prep-extract-notes.md``.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import urllib.parse as _urlparse
from pathlib import Path

from utils.conf_defaults import BASE_DIR

from myUtils import campaigns as campaign_store
from myUtils import llm_client
from myUtils import media_pipeline
from myUtils import media_prep
from myUtils import media_remote_storage
from myUtils import ops_alerts
from myUtils import platform_limits
from myUtils import profiles as profile_registry

# Platforms that publish by handing the platform a public media URL (the
# platform fetches the bytes itself), so they require a publicly-reachable
# HTTPS URL. Other platforms (cookie/browser uploaders, byte-upload APIs)
# use the local file directly and do not need remote hosting.
_REMOTE_URL_PLATFORMS = {
    profile_registry.PLATFORM_TIKTOK,
    profile_registry.PLATFORM_FACEBOOK,
    profile_registry.PLATFORM_INSTAGRAM,
    profile_registry.PLATFORM_THREADS,
}

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}


def _resolve_video_file_path_safely(
    file_path: str, base_dir: Path | None = None
) -> Path | None:
    """Resolve a file path under videoFile, rejecting anything that escapes.

    Returns the resolved Path if it's inside videoFile, None otherwise.
    """
    if not file_path or not isinstance(file_path, str):
        return None
    base = (Path(base_dir if base_dir is not None else BASE_DIR) / "videoFile").resolve()
    # file_records hold two shapes: a bare name relative to videoFile/ (what
    # /upload writes) and an explicit "videoFile/..." (what the reddit and
    # batch schedulers emit, matching the payload convention). Joining the
    # prefixed shape straight onto ``base`` produced
    # ".../videoFile/videoFile/name", so the media looked absent and the
    # offload fetch-back wrote a nested duplicate instead of the real file.
    # Normalise the known prefixes away first; a genuinely absolute path
    # still falls through to the containment check below and is rejected.
    relative = str(file_path).strip()
    for prefix in ("/app/videoFile/", "videoFile/"):
        if relative.startswith(prefix):
            relative = relative[len(prefix):]
            break
    try:
        resolved = (base / relative).resolve()
    except (ValueError, OSError):
        return None
    # Check that the resolved path is inside the base directory
    try:
        resolved.relative_to(base)
    except ValueError:
        return None
    return resolved


def _load_storage_backend_by_id(backend_id: int, *, db_path: Path) -> dict | None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM storage_backends WHERE id = ?", (backend_id,)
        ).fetchone()
    return dict(row) if row else None


def _download_file_from_storage(file_path: str, *, db_path: Path) -> Path | None:
    """Download a file from storage if it has a storage_key or CDN URL. Returns local path or None."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT storage_key, storage_backend_id, storage_cdn_url FROM file_records WHERE file_path = ?",
            (file_path,),
        ).fetchone()
    if not row:
        return None

    local_path = _resolve_video_file_path_safely(file_path)
    if not local_path:
        # Try resolving under uploads/ directory for DO Spaces keys
        uploads_base = (Path(BASE_DIR) / "uploads").resolve()
        try:
            resolved = (uploads_base / Path(file_path).name).resolve()
            if resolved.is_relative_to(uploads_base):
                local_path = resolved
        except (ValueError, OSError):
            pass
    if not local_path:
        logging.getLogger(__name__).warning("Refusing to download to unsafe path: %s", file_path)
        return None
    if local_path.exists():
        return local_path

    local_path.parent.mkdir(parents=True, exist_ok=True)

    # Try 1: download through the backend that actually holds the object.
    # Backends register differently — an S3/Spaces row carries access keys and
    # a bucket, an rclone row carries the remote name in ``bucket`` and its
    # root in ``endpoint`` — so the dispatch lives in media_remote_storage.
    # Calling the Spaces client directly here used to fail for every
    # rclone-backed file, i.e. exactly the media the offload cron parks in
    # Google Drive, and the caller only saw the media path missing later.
    if row["storage_key"] and row["storage_backend_id"]:
        try:
            backend_row = _load_storage_backend_by_id(row["storage_backend_id"], db_path=db_path)
            if backend_row:
                media_remote_storage.download_from_backend(
                    backend_row, row["storage_key"], local_path
                )
                if local_path.exists() and local_path.stat().st_size > 0:
                    return local_path
        except Exception:
            logging.getLogger(__name__).exception("Failed to download %s from storage backend", file_path)

    # Try 2: Download via public CDN URL
    if row["storage_cdn_url"]:
        try:
            import requests as _requests
            resp = _requests.get(row["storage_cdn_url"], timeout=60)
            resp.raise_for_status()
            local_path.write_bytes(resp.content)
            return local_path
        except Exception:
            logging.getLogger(__name__).exception("Failed to download %s from CDN", file_path)

    return None


def _is_image_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_SUFFIXES


def _is_video_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in VIDEO_SUFFIXES


def _shrink_for_publish(
    source_path: Path,
    campaign_id: int | None = None,
    selected_platforms: set[str] | None = None,
) -> Path:
    """Compress an oversized source video before upload (Google Drive originals).

    Re-encodes with the project's publishing profile (1080x1920, CRF ~22,
    <=30fps, AAC 128k) when the file exceeds the size ceiling of the campaign's
    target platforms or overruns the profile dimensions/fps; small or
    already-conforming files are copied unchanged. The ceiling is the strictest
    cap among ``selected_platforms`` (Bluesky 300 MB, Instagram 250 MB, …), so a
    Bluesky-bound clip is shrunk while a YouTube-only one is not size-checked.
    Output lands in the campaign workspace so the original is never touched.

    A failed re-encode falls back to the source path only when the source is
    actually publishable. When the source is over a target platform's hard cap,
    falling back would hand the platform a file it must reject while recording
    the campaign as if it had been prepared - that is how a 602 MB / 13.5 min
    original ended up on the wire (Threads rejected it, and the error surfaced
    minutes later as an opaque container failure). In that case raise instead,
    naming the size and the limit, so the target fails immediately with an
    actionable message.
    """
    try:
        import myUtils.media_prep as media_prep

        if not media_prep._ensure_available():
            return source_path
        out_dir = media_pipeline.build_campaign_workspace(campaign_id) if campaign_id else source_path.parent
        out = media_prep.shrink(source_path, out_dir, platforms=selected_platforms)
        if out and Path(out).exists() and Path(out).stat().st_size > 0:
            logging.getLogger(__name__).info(
                "Pre-publish media prep: %s -> %s (%.1fMB)",
                source_path, out, Path(out).stat().st_size / (1024 * 1024),
            )
            return Path(out)
    except Exception as exc:  # noqa: BLE001 — never block publish on prep
        logging.getLogger(__name__).warning(
            "Pre-publish media prep failed for %s: %s", source_path, exc,
        )
    _assert_within_platform_caps(source_path, selected_platforms)
    return source_path


def _assert_within_platform_caps(
    source_path: Path, selected_platforms: set[str] | None
) -> None:
    """Raise when the un-shrunk source cannot be published as-is.

    Called only on the shrink-failure path, so a conforming file is unaffected.
    """
    if not selected_platforms:
        return
    try:
        import myUtils.media_prep as media_prep

        meta = media_prep.probe(source_path)
    except Exception:  # noqa: BLE001 — an unprobeable file is not our call
        return
    size_mb = media_prep.size_mb_decimal(meta)
    limit_mb = media_prep.resolve_size_limit_mb(selected_platforms)
    if limit_mb and size_mb > limit_mb:
        raise ValueError(
            f"Pre-publish re-encode did not produce a usable file, and the "
            f"source is {size_mb:.0f} MB which exceeds the {limit_mb:.0f} MB "
            f"cap for the selected platforms. Refusing to publish the "
            f"oversized original; re-run the publish so the re-encode can "
            f"complete (it is CPU-bound and can take several minutes)."
        )


def _derive_watermark_spec(profile: profile_registry.Profile, data: dict) -> dict:
    watermark = data.get("watermark")
    if watermark is None:
        watermark = (profile.settings or {}).get("watermark")
    if isinstance(watermark, str) and watermark.strip():
        return {"text": watermark.strip(), "style": "static", "position": "random", "angle": -30, "opacity": 0.5, "fontSize": 24, "color": "white"}
    if isinstance(watermark, dict):
        spec = dict(watermark)
        spec.setdefault("style", "static")
        spec.setdefault("position", "random")
        spec.setdefault("angle", -30)
        spec.setdefault("opacity", 0.5)
        spec.setdefault("fontSize", 24)
        spec.setdefault("color", "white")
        return spec
    return {}


def _resolve_file_record_path(file_record_id: int, db_path: Path) -> str | None:
    """Look up a file_record's file_path by ID."""
    import sqlite3
    conn = None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT file_path FROM file_records WHERE id = ?", (file_record_id,)
        ).fetchone()
        if row:
            return row["file_path"]
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()
    return None


def _resolve_ai_config(profile: profile_registry.Profile) -> dict:
    """Resolve AI service config from profile settings. Returns None values if not configured; callers fall back to env vars."""
    ai_services = (profile.settings or {}).get("aiServices") or []
    if ai_services:
        svc = ai_services[0]  # Use first configured service
        return {
            "api_base_url": svc.get("apiBaseUrl") or None,
            "api_key": svc.get("apiKey") or None,
            "model": svc.get("model") or None,
        }
    return {"api_base_url": None, "api_key": None, "model": None}


def prepare_campaign_media_artifacts(
    campaign_id: int,
    profile: profile_registry.Profile,
    media_files: list[dict],
    request_data: dict,
    *,
    selected_platforms: set[str] | None = None,
    db_path: Path,
    public_base_url: str | None = None,
) -> dict:
    watermark_spec = _derive_watermark_spec(profile, request_data)
    selected_platforms = set(selected_platforms or set())
    tiktok_only = selected_platforms == {profile_registry.PLATFORM_TIKTOK}
    # Remote-host media only when a URL-fetch platform is targeted (TikTok/
    # Meta/Threads pull media by URL). An explicit uploadToRemote overrides;
    # otherwise enable automatically when such a platform is present and a
    # public storage backend (share/DO Spaces/rclone) is configured.
    needs_public_url = bool(selected_platforms & _REMOTE_URL_PLATFORMS)
    _explicit_remote = request_data.get("uploadToRemote")
    if _explicit_remote is not None:
        upload_to_remote = bool(_explicit_remote)
    else:
        upload_to_remote = needs_public_url and media_remote_storage.is_any_backend_configured()

    # Resolve intro/outro file paths from profile settings
    profile_settings = profile.settings or {}
    intro_ids = request_data.get("intros") or profile_settings.get("intros") or []
    outro_ids = request_data.get("outros") or profile_settings.get("outros") or []
    intro_paths = []
    outro_paths = []
    def _resolve_aux_path(file_record_id: int) -> str | None:
        raw = _resolve_file_record_path(int(file_record_id), db_path)
        if not raw:
            return None
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            videofile_candidate = Path(BASE_DIR) / "videoFile" / candidate
            if videofile_candidate.exists():
                candidate = videofile_candidate
            else:
                # Try downloading from remote storage
                downloaded = _download_file_from_storage(raw, db_path=db_path)
                if downloaded:
                    candidate = downloaded
        return str(candidate.resolve())
    for fid in intro_ids:
        p = _resolve_aux_path(fid)
        if p:
            intro_paths.append(p)
    for fid in outro_ids:
        p = _resolve_aux_path(fid)
        if p:
            outro_paths.append(p)

    artifacts_context = {
        "imageUrls": [],
        "videoUrl": "",
        "imageLocalPaths": [],
        "videoLocalPath": "",
        "rawImageUrls": [],
        "rawVideoUrl": "",
        "rawImageLocalPaths": [],
        "rawVideoLocalPath": "",
        "screenshotPaths": [],
        "screenshotUrls": [],
        "transcriptText": str(request_data.get("transcriptText", "") or "").strip(),
    }

    screenshots_spec = request_data.get("screenshots") if isinstance(request_data.get("screenshots"), dict) else {}
    screenshots_enabled = bool(screenshots_spec.get("enabled"))
    screenshots_count = int(screenshots_spec.get("count") or 0) if screenshots_enabled else 0
    screenshots_timestamps = screenshots_spec.get("timestamps") if isinstance(screenshots_spec.get("timestamps"), list) else None

    for media_file in media_files:
        raw_path = Path(media_file["file_path"]).expanduser()
        if not raw_path.is_absolute():
            # file_records stores filenames relative to videoFile/ (the
            # /upload endpoint writes there). Resolve to the canonical
            # absolute location so downstream ffmpeg / Pillow calls work
            # regardless of the worker's current working directory.
            candidate = Path(BASE_DIR) / "videoFile" / raw_path
            if candidate.exists():
                raw_path = candidate
            else:
                # Try downloading from remote storage
                downloaded = _download_file_from_storage(media_file["file_path"], db_path=db_path)
                if downloaded:
                    raw_path = downloaded
        source_path = raw_path.resolve()
        publish_path = source_path
        artifact_kind = None

        # Concat intro/outro for videos before watermarking
        if _is_video_file(source_path) and (intro_paths or outro_paths):
            concat_parts = intro_paths + [str(source_path)] + outro_paths
            concat_output = media_pipeline.prepare_campaign_artifact_path(
                campaign_id,
                source_path,
                artifact_kind="concat",
            )
            try:
                media_pipeline.concat_videos(concat_parts, concat_output)
                source_path = concat_output
                publish_path = concat_output
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Intro/outro concat failed for campaign %d, file %s: %s",
                    campaign_id, source_path, exc,
                )

        # Pre-publish compression: shrink oversized source videos before
        # watermarking/upload. This is where large GDrive originals get
        # re-encoded to the publishing profile (1080x1920, CRF~22, <=30fps),
        # with the size ceiling taken from whichever target platform has the
        # strictest cap.
        if _is_video_file(publish_path):
            publish_path = _shrink_for_publish(
                publish_path, campaign_id, selected_platforms
            )

        if watermark_spec and not tiktok_only and _is_image_file(source_path):
            artifact_kind = "watermarked_image"
            publish_path = media_pipeline.prepare_campaign_artifact_path(
                campaign_id,
                source_path,
                artifact_kind=artifact_kind,
            )
            media_pipeline.apply_image_watermark(
                source_path,
                publish_path,
                watermark_text=watermark_spec.get("text"),
                watermark_image_path=watermark_spec.get("imagePath"),
                seed=campaign_id * 1000 + int(media_file["file_record_id"]),
                opacity=int(float(watermark_spec.get("opacity", 0.5)) * 255),
                style=watermark_spec.get("style", "static"),
                angle=float(watermark_spec.get("angle", -30)),
                color=watermark_spec.get("color", "white"),
            )
        elif watermark_spec and not tiktok_only and _is_video_file(source_path):
            artifact_kind = "watermarked_video"
            publish_path = media_pipeline.prepare_campaign_artifact_path(
                campaign_id,
                source_path,
                artifact_kind=artifact_kind,
            )
            media_pipeline.apply_video_watermark(
                source_path,
                publish_path,
                watermark_text=watermark_spec.get("text"),
                watermark_image_path=watermark_spec.get("imagePath"),
                seed=campaign_id * 1000 + int(media_file["file_record_id"]),
                duration_seconds=request_data.get("durationSeconds"),
                style=watermark_spec.get("style", "static"),
                angle=float(watermark_spec.get("angle", -30)) * 3.14159 / 180,
                opacity=float(watermark_spec.get("opacity", 0.5)),
                fontsize=int(watermark_spec.get("fontSize", 24)),
                color=watermark_spec.get("color", "white"),
            )

        if artifact_kind is not None:
            campaign_store.add_campaign_artifact(
                campaign_id,
                source_file_record_id=media_file["file_record_id"],
                artifact_kind=artifact_kind,
                local_path=str(publish_path),
                metadata={"role": media_file["role"]},
                db_path=db_path,
            )

        if screenshots_enabled and _is_video_file(publish_path):
            try:
                shots_dir = media_pipeline.build_campaign_workspace(campaign_id) / "screenshots"
                shots = media_pipeline.extract_video_screenshots(
                    publish_path,
                    shots_dir,
                    count=screenshots_count if not screenshots_timestamps else None,
                    timestamps=screenshots_timestamps,
                    seed=campaign_id * 1000 + int(media_file["file_record_id"]),
                )
                for shot_path in shots:
                    artifacts_context["screenshotPaths"].append(str(shot_path))
                    public_shot_url = None
                    if upload_to_remote:
                        try:
                            remote_shot = media_remote_storage.upload_artifact(
                                shot_path,
                                campaign_id=campaign_id,
                                artifact_subdir="screenshots",
                            )
                            public_shot_url = remote_shot.public_url
                            if public_shot_url:
                                artifacts_context["screenshotUrls"].append(public_shot_url)
                        except Exception as exc:  # noqa: BLE001
                            logging.getLogger(__name__).warning(
                                "Screenshot remote upload failed for campaign %d: %s",
                                campaign_id, exc,
                            )
                    campaign_store.add_campaign_artifact(
                        campaign_id,
                        source_file_record_id=media_file["file_record_id"],
                        artifact_kind="screenshot",
                        local_path=str(shot_path),
                        public_url=public_shot_url,
                        metadata={
                            "role": media_file["role"],
                            "source_video": str(publish_path),
                        },
                        db_path=db_path,
                    )
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning(
                    "Screenshot extraction failed for campaign %d, file %s: %s",
                    campaign_id, publish_path, exc,
                )

        public_url = None
        raw_public_url = None
        if upload_to_remote:
            remote_source = source_path if tiktok_only else publish_path
            remote_kind = "raw_remote_upload" if tiktok_only else "remote_upload"
            remote_artifact = media_remote_storage.upload_artifact(
                remote_source,
                campaign_id=campaign_id,
                artifact_subdir="videos" if _is_video_file(remote_source) else "images",
            )
            public_url = remote_artifact.public_url
            campaign_store.add_campaign_artifact(
                campaign_id,
                source_file_record_id=media_file["file_record_id"],
                artifact_kind=remote_kind,
                local_path=str(remote_source),
                public_url=remote_artifact.public_url,
                remote_path=remote_artifact.remote_path,
                metadata={"role": media_file["role"]},
                db_path=db_path,
            )
            if not tiktok_only and publish_path != source_path:
                raw_remote_artifact = media_remote_storage.upload_artifact(
                    source_path,
                    campaign_id=campaign_id,
                    artifact_subdir="videos" if _is_video_file(source_path) else "images",
                )
                raw_public_url = raw_remote_artifact.public_url
                campaign_store.add_campaign_artifact(
                    campaign_id,
                    source_file_record_id=media_file["file_record_id"],
                    artifact_kind="raw_remote_upload",
                    local_path=str(source_path),
                    public_url=raw_remote_artifact.public_url,
                    remote_path=raw_remote_artifact.remote_path,
                    metadata={"role": media_file["role"]},
                    db_path=db_path,
                )
        else:
            # No remote upload. Cookie/byte-upload platforms use the local file
            # directly; only emit a /getFile URL when it would actually be
            # reachable. Never hand a URL-fetch platform an unreachable
            # localhost URL (that guarantees a downstream 4xx) — leave it None
            # so the publisher raises a clear "no public media URL" error.
            public_url = None
            base_url = ""
            try:
                # Prefer the configured public origin. Outside a Flask request
                # (the worker path) the caller passes ``public_base_url``; when
                # neither is available the artifact gets no URL rather than an
                # invented, unreachable hostname.
                base_url = (
                    ops_alerts.public_app_origin()
                    or (public_base_url or "")
                ).rstrip("/")
                if not base_url:
                    raise RuntimeError("no public origin configured")
                # Serveable path relative to whichever media root holds the
                # file (videoFile/ for staged source, generated/ for watermarked
                # campaign artifacts), not the absolute /app/… path — otherwise
                # /getFile rejects it as outside the root. A path that is already
                # relative (the staged / file-record convention) is videoFile-
                # relative and used as-is.
                _pp = Path(publish_path)
                _rel = None
                if _pp.is_absolute():
                    for _rname in ("videoFile", "generated", "uploads"):
                        _rbase = (Path(BASE_DIR) / _rname).resolve()
                        try:
                            _rel = _pp.resolve().relative_to(_rbase).as_posix()
                            break
                        except (ValueError, OSError):
                            continue
                    if _rel is None:
                        _rel = _pp.name
                else:
                    _rel = _pp.as_posix()
                served_filename = _urlparse.quote(_rel)
                candidate = f"{base_url}/getFile?filename={served_filename}"
                _is_public = (
                    base_url.startswith("https://")
                    and "localhost" not in base_url
                    and "127.0.0.1" not in base_url
                )
                if needs_public_url and not _is_public:
                    logging.getLogger(__name__).warning(
                        "campaign %d targets URL-fetch platforms but no public "
                        "storage backend produced a URL; suppressing unreachable %s",
                        campaign_id, candidate,
                    )
                else:
                    public_url = candidate
            except RuntimeError:
                public_url = None
            campaign_store.add_campaign_artifact(
                campaign_id,
                source_file_record_id=media_file["file_record_id"],
                artifact_kind="local",
                local_path=str(publish_path),
                public_url=public_url,
                metadata={"role": media_file["role"]},
                db_path=db_path,
            )
            # Also store a raw (un-watermarked) artifact for TikTok when
            # watermarks were applied in a mixed-platform batch.
            if (
                base_url
                and not tiktok_only
                and publish_path != source_path
                and profile_registry.PLATFORM_TIKTOK in selected_platforms
            ):
                try:
                    raw_served_filename = Path(source_path).name
                    raw_public_url = f"{base_url}/getFile?filename={raw_served_filename}"
                except (RuntimeError, NameError):
                    raw_public_url = None
                campaign_store.add_campaign_artifact(
                    campaign_id,
                    source_file_record_id=media_file["file_record_id"],
                    artifact_kind="raw_local",
                    local_path=str(source_path),
                    public_url=raw_public_url,
                    metadata={"role": media_file["role"]},
                    db_path=db_path,
                )

        # A video over a platform's duration OR size cap is split into several
        # equal parts (one post each) so nothing is dropped; platforms whose
        # caps it already meets (IG 900s, YouTube 12h) keep the full _pub.mp4.
        if _is_video_file(publish_path) and selected_platforms:
            try:
                duration = float((media_prep.probe(publish_path) or {}).get("duration") or 0)
            except Exception:  # noqa: BLE001
                duration = 0.0
            try:
                size_bytes = int(Path(publish_path).stat().st_size)
            except OSError:
                size_bytes = 0
            # One plan per distinct (seconds, MB) cap pair that the source
            # exceeds, tagging which platforms asked for it so a large-cap
            # platform (YouTube) never picks a small-cap platform's parts.
            plans: dict[tuple[float | None, float | None], set[str]] = {}
            for platform_name in selected_platforms:
                sec = platform_limits.video_max_seconds(platform_name)
                mb = platform_limits.media_max_mb(platform_name)
                over_time = bool(sec and duration and duration > float(sec))
                over_size = bool(mb and size_bytes and size_bytes > float(mb) * 1024 * 1024)
                if over_time or over_size:
                    plans.setdefault((float(sec) if sec else None, float(mb) if mb else None), set()).add(
                        str(platform_name)
                    )
            for (sec, mb), split_for in sorted(plans.items(), key=lambda item: (item[0][0] or 0, item[0][1] or 0)):
                try:
                    parts = media_prep.split_to_seconds(
                        publish_path,
                        media_pipeline.build_campaign_workspace(campaign_id),
                        sec,
                        max_bytes=(mb * 1024 * 1024) if mb else None,
                    )
                except Exception as exc:  # noqa: BLE001
                    logging.getLogger(__name__).warning(
                        "media split at seconds=%s mb=%s failed for campaign %d: %s",
                        sec, mb, campaign_id, exc,
                    )
                    continue
                if len(parts) <= 1:
                    continue
                part_seconds = (duration / len(parts)) if duration else None
                part_mb = (size_bytes / len(parts) / (1024 * 1024)) if size_bytes else None
                for index, part in enumerate(parts, start=1):
                    part_remote = None
                    part_public = None
                    if upload_to_remote:
                        try:
                            part_remote = media_remote_storage.upload_artifact(
                                part,
                                campaign_id=campaign_id,
                                artifact_subdir="videos",
                            )
                            part_public = part_remote.public_url
                        except Exception as exc:  # noqa: BLE001
                            logging.getLogger(__name__).warning(
                                "media split remote upload failed for campaign %d: %s",
                                campaign_id, exc,
                            )
                    campaign_store.add_campaign_artifact(
                        campaign_id,
                        source_file_record_id=media_file["file_record_id"],
                        artifact_kind="remote_upload" if part_public else "local",
                        local_path=str(part),
                        public_url=part_public,
                        remote_path=getattr(part_remote, "remote_path", None) if part_remote else None,
                        metadata={
                            "role": media_file["role"],
                            "max_duration_seconds": part_seconds,
                            "max_media_mb": part_mb,
                            "part_index": index,
                            "part_count": len(parts),
                            "split_for": sorted(split_for),
                        },
                        db_path=db_path,
                    )

        if _is_image_file(publish_path):
            artifacts_context["imageLocalPaths"].append(str(publish_path))
            artifacts_context["rawImageLocalPaths"].append(str(source_path))
            if public_url:
                artifacts_context["imageUrls"].append(public_url)
            if raw_public_url:
                artifacts_context["rawImageUrls"].append(raw_public_url)
        elif _is_video_file(publish_path):
            artifacts_context["videoLocalPath"] = str(publish_path)
            artifacts_context["rawVideoLocalPath"] = str(source_path)
            if public_url:
                artifacts_context["videoUrl"] = public_url
            if raw_public_url:
                artifacts_context["rawVideoUrl"] = raw_public_url

    ai_config = _resolve_ai_config(profile)
    has_ai = bool(
        ai_config["api_base_url"] and ai_config["api_key"]
    ) or bool(
        os.environ.get("SAU_LLM_API_KEY") and os.environ.get("SAU_LLM_API_BASE_URL")
    )
    should_transcribe = bool(
        request_data.get("transcribe", False)
        or (not artifacts_context["transcriptText"] and has_ai)
    )
    primary_video = next((item for item in media_files if item["role"] == "video"), None)
    if should_transcribe and primary_video is not None and not artifacts_context["transcriptText"]:
        raw_video = Path(primary_video["file_path"]).expanduser()
        if not raw_video.is_absolute():
            candidate = Path(BASE_DIR) / "videoFile" / raw_video
            if candidate.exists():
                raw_video = candidate
        source_path = raw_video.resolve()
        try:
            audio_path = media_pipeline.prepare_campaign_artifact_path(
                campaign_id,
                source_path,
                artifact_kind="audio",
                suffix=".wav",
            )
            media_pipeline.extract_video_audio(source_path, audio_path)
            transcribe_kwargs = {}
            if ai_config["api_base_url"]:
                transcribe_kwargs["api_base_url"] = ai_config["api_base_url"]
            if ai_config["api_key"]:
                transcribe_kwargs["api_key"] = ai_config["api_key"]
            transcript = llm_client.transcribe_audio(audio_path, **transcribe_kwargs)
            transcript_path = media_pipeline.prepare_campaign_artifact_path(
                campaign_id,
                source_path,
                artifact_kind="transcript",
                suffix=".txt",
            )
            transcript_path.write_text(transcript.text, encoding="utf-8")
            artifacts_context["transcriptText"] = transcript.text
            campaign_store.add_campaign_artifact(
                campaign_id,
                source_file_record_id=primary_video["file_record_id"],
                artifact_kind="audio",
                local_path=str(audio_path),
                db_path=db_path,
            )
            campaign_store.add_campaign_artifact(
                campaign_id,
                source_file_record_id=primary_video["file_record_id"],
                artifact_kind="transcript",
                local_path=str(transcript_path),
                metadata={"text": transcript.text},
                db_path=db_path,
            )
        except Exception as exc:  # noqa: BLE001
            # Transcription is optional context for the LLM. If the video
            # has no audio stream, or the transcription API rejects the
            # request, we still want the publish to go through.
            logging.getLogger(__name__).warning(
                "Audio extraction / transcription failed for campaign %d: %s",
                campaign_id, exc,
            )

    return artifacts_context
