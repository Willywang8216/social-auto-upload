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
import subprocess
import urllib.parse as _urlparse
import uuid
from pathlib import Path

from utils.conf_defaults import BASE_DIR

from myUtils import campaigns as campaign_store
from myUtils import content_language, llm_client
from myUtils import media_pipeline
from myUtils import media_prep
from myUtils import media_remote_storage
from myUtils import ops_alerts
from myUtils import platform_limits
from myUtils import media_compliance, image_compliance, publish_media_guard, video_artifacts
from myUtils import profiles as profile_registry
from myUtils.sqlite_utils import connect as _db_connection

def _preparation_limits_for_accounts(accounts, platforms: set[str], supplied=None,
                                     *, backup_compatible_platforms=None) -> dict:
    from myUtils.publish_orchestrator import _preparation_limits_for_accounts as resolve
    return resolve(accounts, platforms, supplied, backup_compatible_platforms=backup_compatible_platforms)


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

# Hosts that are only reachable from this machine. A ``/getFile`` URL under one
# of these can never be fetched by a platform, so it must never be stored as an
# artifact's ``public_url`` (targets #3449/#3450 handed Bluesky a
# ``http://localhost:5409/getFile`` URL and got a 404).
_UNREACHABLE_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def _is_public_base_url(base_url: str) -> bool:
    """True when ``base_url`` is a publicly reachable HTTPS origin.

    A non-HTTPS origin, or one whose host is a loopback address, is only
    reachable from this process; emitting it as a ``public_url`` guarantees a
    downstream fetch failure, so callers suppress it instead. The check is
    deliberately host-based: ``localhost`` as a path segment (rare, but
    possible in a configured CDN) still passes.
    """
    text = str(base_url or "").strip()
    if not text.startswith("https://"):
        return False
    host = _urlparse.urlparse(text).hostname or ""
    return host.lower() not in _UNREACHABLE_HOSTS

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
    with _db_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM storage_backends WHERE id = ?", (backend_id,)
        ).fetchone()
    return dict(row) if row else None


def _resolve_backend_id_for_artifact(
    remote_artifact,
    *,
    db_path: Path,
) -> int | None:
    """Look up the ``storage_backends.id`` that matches a ``RemoteArtifact``.

    The ``upload_artifact`` dispatch returns a :class:`RemoteArtifact` whose
    ``remote_name`` identifies the backend (``"share"``, ``"do_spaces"``, or
    an rclone remote name). Matching by provider slug or bucket gives the
    row id the restore path needs to download through the right backend.
    """
    remote_name = getattr(remote_artifact, "remote_name", None)
    if not remote_name:
        return None
    with _db_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        # rclone remotes store the remote name in ``bucket``; share/DO Spaces
        # use a provider slug. Try both.
        row = conn.execute(
            "SELECT id FROM storage_backends WHERE bucket = ? OR slug = ? LIMIT 1",
            (remote_name, remote_name),
        ).fetchone()
    return int(row["id"]) if row else None


def _download_file_from_storage(file_path: str, *, db_path: Path) -> Path | None:
    """Download a file from storage if it has a storage_key or CDN URL. Returns local path or None."""
    with _db_connection(db_path) as conn:
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
            return media_remote_storage.download_public_url(row["storage_cdn_url"], local_path)
        except Exception:
            logging.getLogger(__name__).exception("Failed to download %s from CDN", file_path)

    return None


#: Codecs that mean "still image" when ffprobe reports them as a video stream.
_IMAGE_CODECS = {"mjpeg", "png", "bmp", "gif", "webp", "tiff", "apng", "jpeg"}
_PROBE_CACHE: dict[tuple[str, float], str] = {}


def probe_media_kind(path: str | Path) -> str | None:
    """``"video"`` / ``"image"`` / ``None``, from the file's *content*.

    The suffix is not always there. The operator uploaded
    ``The Anatomy of Connection – Episode 2: The Overlap Myth__sfw`` to the Drive
    inbox with no extension at all, and the suffix-only classifier called it
    neither a video nor an image — so prep produced no publishable artifact, the
    campaign was still marked ``publishing``, and all seven of its queued
    targets failed with "media artifacts were supplied but none is a supported
    image/video". Asking ffprobe what the bytes actually are fixes that, and
    costs one probe per file (cached by path+mtime).
    """
    candidate = Path(str(path))
    try:
        stat = candidate.stat()
    except OSError:
        return None
    key = (str(candidate), stat.st_mtime)
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    kind: str | None = None
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(candidate)],
            capture_output=True, text=True, timeout=60,
        )
        codecs = {
            line.strip().split(",")[0].lower()
            for line in (result.stdout or "").splitlines()
            if line.strip()
        }
        if codecs:
            kind = "image" if codecs <= _IMAGE_CODECS else "video"
    except Exception:  # noqa: BLE001 - an unprobeable file simply stays unknown
        kind = None
    _PROBE_CACHE[key] = kind
    return kind


def _is_image_file(path: str | Path) -> bool:
    if Path(path).suffix.lower() in IMAGE_SUFFIXES:
        return True
    return probe_media_kind(path) == "image" 


def _is_video_file(path: str | Path) -> bool:
    if Path(path).suffix.lower() in VIDEO_SUFFIXES:
        return True
    # An extensionless (or oddly named) file still needs to be published as
    # whatever it actually is.
    return probe_media_kind(path) == "video"


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


def _resolve_stt_config(profile: profile_registry.Profile) -> dict:
    """Resolve speech-to-text config.

    Mirrors :func:`_resolve_ai_config` (same ``aiServices[0]`` shape) so an
    operator can point STT at their own gateway through the profile, but a
    dedicated ``SAU_STT_*`` env var wins because STT is a DIFFERENT gateway
    from the drafting LLM (``wzw.pp.ua`` vs ``muyuan.do``). ``None`` values let
    :func:`myUtils.llm_client.transcribe_audio_via_chat` fall back to the
    verified ``mimo-v2.6-flash`` defaults.
    """
    profile_ai = _resolve_ai_config(profile)
    return {
        "api_base_url": os.environ.get(llm_client.STT_BASE_URL_ENV)
        or profile_ai["api_base_url"],
        "api_key": os.environ.get(llm_client.STT_API_KEY_ENV)
        or profile_ai["api_key"],
        "model": os.environ.get(llm_client.STT_MODEL_ENV) or profile_ai["model"],
    }


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
    request_data = dict(request_data)
    watermark_spec = _derive_watermark_spec(profile, request_data)
    selected_platforms = set(selected_platforms or set())
    generation = uuid.uuid4().hex
    backup_platforms = platform_limits.backup_compatible_platforms(request_data) & selected_platforms
    accounts = []
    if selected_platforms:
        # Resolve current selected accounts before any media/queue write work.
        from myUtils.preparation_context import configure_request
        try:
            campaign = campaign_store.get_campaign(campaign_id, db_path=db_path)
            selected_ids = set(campaign.selected_account_ids)
            accounts = [account for account in profile_registry.list_accounts(
                profile_id=profile.id, enabled=True, db_path=db_path) if account.id in selected_ids]
        except (LookupError, sqlite3.Error):
            accounts = []
        has_video = any(item.get("role") == "video" or _is_video_file(item["file_path"]) for item in media_files
                        if item.get("role") not in media_compliance.CONTEXT_KINDS)
        request_data, _backup_contexts = configure_request(accounts, selected_platforms, request_data,
                                                         settings=profile.settings, has_video=has_video)
        backup_platforms = platform_limits.backup_compatible_platforms(request_data) & selected_platforms
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
        else:
            raise media_compliance.MediaPreparationError("Selected intro media cannot be restored")
    for fid in outro_ids:
        p = _resolve_aux_path(fid)
        if p:
            outro_paths.append(p)
        else:
            raise media_compliance.MediaPreparationError("Selected outro media cannot be restored")

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
        # Draft generation seeds per-account language routing from this. A
        # supplied transcript wins; otherwise the title is classified and STT
        # (below) overwrites it with the model's declared language.
        "language": None,
    }
    # A transcript that is really the gateway's refusal must not seed the copy
    # prompt or the language routing (35 such rows existed on 2026-10-27, and
    # they are what put English media on the Chinese accounts and vice versa).
    artifacts_context["transcriptText"] = llm_client.usable_transcript(
        artifacts_context["transcriptText"]
    )
    _supplied_language = content_language.normalise(request_data.get("language"))
    if artifacts_context["transcriptText"]:
        artifacts_context["language"] = (
            _supplied_language
            if _supplied_language
            else llm_client.detect_language(artifacts_context["transcriptText"])
        )
        artifacts_context["languageSource"] = "supplied" if _supplied_language else "transcript"
    elif _supplied_language:
        artifacts_context["language"] = _supplied_language
        artifacts_context["languageSource"] = "supplied"
    else:
        # No transcript yet (STT may still be about to run). Classify the
        # *media* from its name/brief rather than from the title: the title is
        # generated in the target account's language, so using it here made the
        # routing comparison a tautology. A language-neutral name (a timestamp,
        # a tweet id) yields None, which the router treats as unknown.
        _guess, _guess_source = content_language.detect_from_evidence(
            filenames=[str(item.get("filename") or "") for item in media_files],
            brief=str(request_data.get("brief") or ""),
            title="",
            has_video=any(
                str(item.get("role") or "") == "video"
                or str(item.get("filename") or "").lower().endswith((".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"))
                for item in media_files
            ),
        )
        if _guess:
            artifacts_context["language"] = _guess
            artifacts_context["languageSource"] = _guess_source

    screenshots_spec = request_data.get("screenshots") if isinstance(request_data.get("screenshots"), dict) else {}
    screenshots_enabled = bool(screenshots_spec.get("enabled"))
    screenshots_count = int(screenshots_spec.get("count") or 0) if screenshots_enabled else 0
    screenshots_timestamps = screenshots_spec.get("timestamps") if isinstance(screenshots_spec.get("timestamps"), list) else None
    if screenshots_count > 100 or (screenshots_timestamps and len(screenshots_timestamps) > 100):
        raise media_compliance.MediaPreparationError("Screenshot request exceeds the bounded preparation count")
    if len(media_files) > image_compliance.MAX_SET_IMAGES:
        raise media_compliance.MediaPreparationError("Campaign media exceeds the bounded preparation count")
    pending_rows, image_inputs, original_sources = [], [], []

    for media_file in media_files:
        if publish_media_guard.is_context_artifact({"local_path": media_file["file_path"],
                "artifact_kind": media_file.get("role"), "metadata": {"role": media_file.get("role")}}):
            continue
        raw_path = Path(media_file["file_path"]).expanduser()
        if not raw_path.is_absolute():
            # file_records stores filenames relative to videoFile/ (the
            # /upload endpoint writes there). Resolve to the canonical
            # absolute location so downstream ffmpeg / Pillow calls work
            # regardless of the worker's current working directory.
            candidate = _resolve_video_file_path_safely(str(raw_path))
            if candidate is not None and candidate.exists():
                raw_path = candidate
        if not raw_path.exists():
            # The offload cron moves published media to Drive and deletes the
            # local copy, so a re-prep of an old campaign starts with the raw
            # source absent *regardless of whether file_records stored an
            # absolute or a relative path*. Restore it from storage before
            # probing/transcoding. Previously this branch only ran for relative
            # paths, so an absolute ``/app/videoFile/...`` source silently went
            # missing: ffprobe/ffmpeg failed, prep recorded a "prepared"
            # artifact pointing at the absent raw file, and the re-prep queued
            # targets that could not publish.
            downloaded = _download_file_from_storage(
                media_file["file_path"], db_path=db_path
            )
            if downloaded:
                raw_path = downloaded
        source_path = raw_path.resolve()
        if not source_path.is_file() or source_path.stat().st_size <= 0:
            raise media_compliance.MediaPreparationError("Selected source is missing or empty after restore")
        expected = media_file.get("sha256") or (media_file.get("metadata") or {}).get("sha256")
        if expected:
            media_compliance.verify_checksum(source_path, expected)
        source_sha = media_compliance.sha256_file(source_path)
        original_sources.append((source_path, source_sha))
        media_file = {**media_file, "original_source_path": str(source_path), "original_source_sha256": source_sha}
        if _is_image_file(source_path):
            image_info = image_compliance.probe_image(source_path)
        elif _is_video_file(source_path):
            media_compliance._source_metadata(source_path)
        else:
            raise media_compliance.MediaPreparationError("Selected source contains no supported image/video")
        publish_path = source_path
        artifact_kind = None

        # Concat intro/outro for videos before watermarking
        if _is_video_file(source_path) and (intro_paths or outro_paths):
            concat_parts = intro_paths + [str(source_path)] + outro_paths
            concat_output = media_pipeline.prepare_campaign_artifact_path(
                campaign_id,
                source_path,
                artifact_kind="concat",
                generation=generation,
            )
            try:
                media_pipeline.concat_videos(concat_parts, concat_output, runner=_bounded_media_runner)
                source_path = concat_output
                publish_path = concat_output
            except Exception as exc:
                raise media_compliance.MediaPreparationError("Selected intro/outro concatenation failed; content was not dropped") from exc

        if watermark_spec and not tiktok_only and _is_image_file(source_path):
            artifact_kind = "watermarked_image"
            # Watermarking strips EXIF: orient/color-convert BEFORE that transform.
            watermark_source = source_path
            if image_info["orientation"] not in {None, 1} or image_info["color_space"] != "sRGB":
                normalized = media_pipeline.prepare_campaign_artifact_path(campaign_id, source_path,
                             artifact_kind="watermark_input", generation=generation, suffix=".png")
                watermark_source = image_compliance.materialise_srgb(source_path, normalized)
            publish_path = media_pipeline.prepare_campaign_artifact_path(
                campaign_id,
                source_path,
                artifact_kind=artifact_kind,
                generation=generation,
            )
            media_pipeline.apply_image_watermark(
                watermark_source,
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
                generation=generation,
                suffix=".mp4",
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
                runner=_bounded_media_runner,
            )

        # No intermediate watermark/raw file is registered as publishable.
        if _is_video_file(publish_path) and publish_path != source_path:
            before = media_compliance._source_metadata(source_path)
            after = media_compliance._source_metadata(publish_path)
            if abs(before["duration"] - after["duration"]) > 0.2:
                raise media_compliance.MediaPreparationError("Final watermark changed the complete source duration")

        if screenshots_enabled and _is_video_file(publish_path):
            try:
                shots_dir = media_pipeline.build_campaign_workspace(campaign_id) / generation / "screenshots"
                shots = media_pipeline.extract_video_screenshots(
                    publish_path,
                    shots_dir,
                    count=screenshots_count if not screenshots_timestamps else None,
                    timestamps=screenshots_timestamps,
                    seed=campaign_id * 1000 + int(media_file["file_record_id"]),
                    runner=_bounded_media_runner,
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
                    image_compliance.probe_image(shot_path)
                    pending_rows.append({"source_file_record_id": media_file["file_record_id"],
                        "artifact_kind": "screenshot", "local_path": str(shot_path), "public_url": public_shot_url,
                        "metadata": {"role": "cover", "source_video": str(publish_path),
                                     "sha256": media_compliance.sha256_file(shot_path),
                                     "size_bytes": shot_path.stat().st_size}})
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning(
                    "Screenshot extraction failed for campaign %d, file %s: %s",
                    campaign_id, publish_path, exc,
                )

        public_url = None
        raw_public_url = None
        if _is_video_file(publish_path):
            if not selected_platforms:
                raise media_compliance.MediaPreparationRequired("Video preparation needs selected platforms")
            prepared = video_artifacts.prepare_and_register_video(
                campaign_id=campaign_id, media_file=media_file,
                source_path=source_path, publish_path=publish_path,
                selected_platforms=selected_platforms, request_data=request_data,
                upload_to_remote=upload_to_remote, db_path=db_path,
                public_base_url=public_base_url,
                register=False,
            )
            public_url = prepared["public_url"]
            raw_public_url = prepared["raw_public_url"]
            pending_rows.extend(prepared["artifacts"])
        elif _is_image_file(publish_path):
            image_inputs.append({"media_file": media_file, "source_path": source_path, "publish_path": publish_path})
        else:
            raise media_compliance.MediaPreparationError("Final transform contains no supported media")
        if _is_video_file(publish_path):
            artifacts_context["videoLocalPath"] = str(publish_path)
            artifacts_context["rawVideoLocalPath"] = str(source_path)
            if public_url:
                artifacts_context["videoUrl"] = public_url
            if raw_public_url:
                artifacts_context["rawVideoUrl"] = raw_public_url

    if image_inputs:
        prepared_images = video_artifacts.prepare_and_register_images(
            campaign_id=campaign_id, images=image_inputs, selected_platforms=selected_platforms,
            request_data=request_data, upload_to_remote=upload_to_remote, db_path=db_path,
            public_base_url=public_base_url, register=False,
            video_present=any(row["artifact_kind"] == "prepared_video" for row in pending_rows))
        pending_rows.extend(prepared_images["artifacts"])
        by_platform = prepared_images["by_platform"]
        defaults = next((values for platform, values in by_platform.items() if platform != "tiktok" and values),
                        by_platform.get("tiktok") or [])
        raw_defaults = by_platform.get("tiktok") or []
        artifacts_context["imageLocalPaths"] = [row["local_path"] for row in defaults]
        artifacts_context["imageUrls"] = [row["public_url"] for row in defaults if row.get("public_url")]
        artifacts_context["rawImageLocalPaths"] = [row["local_path"] for row in raw_defaults] or [str(item["source_path"]) for item in image_inputs]
        artifacts_context["rawImageUrls"] = [row["public_url"] for row in raw_defaults if row.get("public_url")]
    for source, checksum in original_sources:
        media_compliance.verify_checksum(source, checksum)
    if pending_rows:
        # The complete campaign set is registered atomically only AFTER all
        # final files/routes pass. No encodes run inside the write transaction.
        campaign_store.add_campaign_artifact_set(campaign_id, pending_rows, db_path=db_path)
    elif media_files:
        raise media_compliance.MediaPreparationError("No complete publishable media set was prepared")
    artifacts_context["preparedArtifacts"] = pending_rows

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
            stt_config = _resolve_stt_config(profile)
            # ``mimo-v2.6-flash`` on wzw.pp.ua is reachable only through
            # /v1/chat/completions with an input_audio part; the native
            # /audio/transcriptions route returns bad_response_status_code.
            transcript = llm_client.transcribe_audio_via_chat(
                audio_path,
                model=stt_config["model"],
                api_base_url=stt_config["api_base_url"],
                api_key=stt_config["api_key"],
                title=str(request_data.get("title", "") or ""),
            )
            transcript_path = media_pipeline.prepare_campaign_artifact_path(
                campaign_id,
                source_path,
                artifact_kind="transcript",
                suffix=".txt",
            )
            transcript_path.write_text(transcript.text, encoding="utf-8")
            artifacts_context["transcriptText"] = transcript.text
            # The video's own audio is the authority. Only a *real* transcript
            # may claim the "transcript" source; a supplied language or a
            # filename guess must not masquerade as one.
            if transcript.language:
                artifacts_context["language"] = transcript.language
                artifacts_context["languageSource"] = "transcript"
            elif not artifacts_context.get("language"):
                artifacts_context["language"] = None
                artifacts_context["languageSource"] = None
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


def _bounded_media_runner(command, **kwargs):
    """Existing watermark/concat runner, with shared slots, CPU/time bounds."""
    argv = list(command)
    if argv and Path(str(argv[0])).name.startswith("ffmpeg"):
        argv = ["-n" if value == "-y" else value for value in argv]
        argv[1:1] = ["-threads", "2", "-filter_threads", "2", "-filter_complex_threads", "2"]
        if Path(str(argv[-1])).suffix.lower() == ".mp4":
            # The legacy watermark runner copies audio; PCM/etc cannot be
            # muxed into MP4. Normalize in the SAME bounded watermark encode.
            for index in range(len(argv) - 1):
                if argv[index] == "-c:a" and argv[index + 1] == "copy":
                    argv[index + 1] = "aac"
            argv[-1:-1] = ["-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
                           "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000", "-movflags", "+faststart"]
        argv[-1:-1] = ["-threads", "2"]
        kwargs.setdefault("timeout", 21_600)
    else:
        kwargs.setdefault("timeout", 90)
    return media_prep._run(argv, check=True, **kwargs)
