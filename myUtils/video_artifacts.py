"""Final immutable artifact sets; no raw variants, partial sets or stale merges."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from myUtils import campaigns, image_compliance, media_compliance, media_pipeline, media_remote_storage, ops_alerts, platform_limits
from utils.conf_defaults import BASE_DIR


def served_url(path: Path, public_base_url: str | None = None) -> str | None:
    from myUtils.campaign_media_prep import _is_public_base_url

    origin = (ops_alerts.public_app_origin() or public_base_url or "").rstrip("/")
    if not _is_public_base_url(origin):
        return None
    for name in ("videoFile", "generated", "uploads"):
        root = (Path(BASE_DIR) / name).resolve()
        if path.resolve().is_relative_to(root):
            relative = path.resolve().relative_to(root).as_posix()
            return f"{origin}/getFile?filename={quote(relative, safe='/')}"
    return None  # Never advertise a basename for a file outside the serving roots.


def _artifact_row(part, *, campaign_id: int, media_file: dict, metadata: dict,
                  image: bool, upload_to_remote: bool, db_path: Path,
                  public_base_url: str | None) -> dict:
    media_compliance.verify_checksum(part.path, part.metadata["sha256"])
    remote = media_remote_storage.upload_artifact(part.path, campaign_id=campaign_id,
              artifact_subdir="images" if image else "videos") if upload_to_remote else None
    backend_id = None
    if remote:
        from myUtils.campaign_media_prep import _resolve_backend_id_for_artifact
        backend_id = _resolve_backend_id_for_artifact(remote, db_path=db_path)
    return {"artifact_kind": "prepared_image" if image else "prepared_video",
            "source_file_record_id": media_file["file_record_id"], "local_path": str(part.path),
            "public_url": remote.public_url if remote else served_url(part.path, public_base_url),
            "remote_path": getattr(remote, "remote_path", None), "storage_backend_id": backend_id,
            **{key: media_file[key] for key in ("alt_text", "altText", "cover") if key in media_file},
            "metadata": {**(media_file.get("metadata") or {}), **metadata,
                **{key: media_file[key] for key in ("alt_text", "altText", "cover") if key in media_file},
                "origin_source_path": media_file.get("original_source_path", metadata.get("source_path")),
                "origin_source_sha256": media_file.get("original_source_sha256", metadata.get("source_sha256"))}}


def _result(rows: list[dict], selected_platforms: set[str]) -> dict:
    by_platform = {platform: [row for row in rows if platform in row["metadata"]["prepared_for"]]
                   for platform in sorted(selected_platforms)}
    first = next((values[0] for platform, values in by_platform.items() if platform != "tiktok" and values),
                 (by_platform.get("tiktok") or [None])[0])
    raw = (by_platform.get("tiktok") or [None])[0]
    return {"artifacts": rows, "by_platform": by_platform,
            "public_url": first.get("public_url") if first else None,
            "raw_public_url": raw.get("public_url") if raw else None}


def prepare_and_register_video(*, campaign_id: int, media_file: dict, source_path: Path,
                               publish_path: Path, selected_platforms: set[str],
                               request_data: dict, upload_to_remote: bool, db_path: Path,
                               public_base_url: str | None = None, register: bool = True) -> dict:
    """Encode/validate ALL platforms first. ``register=False`` defers DB writes."""
    if not selected_platforms:
        raise media_compliance.MediaPreparationRequired("Video preparation needs selected platforms")
    backup = platform_limits.backup_compatible_platforms(request_data)
    plans = {}
    specs = {}
    for platform in sorted(selected_platforms):
        # TikTok uses the SAME selected content variant without our watermark;
        # other routes use the final watermarked bytes, never the raw source.
        source = source_path if platform == "tiktok" else publish_path
        config = (request_data.get("_preparationAccountConfigs") or {}).get(platform) or {}
        limits = media_compliance.effective_limits(platform, account_config=config,
            limits=(request_data.get("_preparationLimits") or {}).get(platform), backup_compatible=platform in backup)
        specs[platform] = {"config": config, "limits": limits, "backup": platform in backup}
        key = (str(source), json.dumps(limits, sort_keys=True))
        plan = plans.setdefault(key, {"source": source, "limits": limits, "platforms": [], "config": config})
        plan["platforms"].append(platform)
    prepared_plans = []
    for plan in plans.values():
        platform = plan["platforms"][0]
        parts = media_compliance.prepare_video(plan["source"], media_pipeline.build_campaign_workspace(campaign_id),
            platform=platform, account_config=plan["config"], limits=plan["limits"],
            backup_compatible=platform in backup, force_generation=True)
        if not parts:
            raise media_compliance.MediaPreparationError("Video preparation returned no complete set")
        for route in plan["platforms"]:
            for part in parts:
                spec = specs[route]
                media_compliance.validate_video(part.path, platform=route, account_config=spec["config"],
                    limits=spec["limits"], expected_sha256=part.metadata["sha256"], backup_compatible=spec["backup"])
        prepared_plans.append((plan, parts))
    rows = []
    for plan, parts in prepared_plans:
        for part in parts:
            metadata = {**part.metadata, "role": "video", "prepared_for": plan["platforms"],
                        "split_for": plan["platforms"], "unwatermarked": plan["source"] == source_path,
                        "backup_compatible": any(route in backup for route in plan["platforms"])}
            rows.append(_artifact_row(part, campaign_id=campaign_id, media_file=media_file, metadata=metadata,
                image=False, upload_to_remote=upload_to_remote, db_path=db_path, public_base_url=public_base_url))
    if register:
        campaigns.add_campaign_artifact_set(campaign_id, rows, db_path=db_path)
    return _result(rows, selected_platforms)


def prepare_and_register_images(*, campaign_id: int, images: list[dict], selected_platforms: set[str],
                                request_data: dict, upload_to_remote: bool, db_path: Path,
                                public_base_url: str | None = None, register: bool = True,
                                video_present: bool = False) -> dict:
    """One complete ordered image generation per platform, before any registration.

    Inputs carry ``media_file``, exact ``source_path`` and final ``publish_path``.
    YouTube's image route is thumbnails ONLY; it never creates standalone posts.
    """
    if not images or not selected_platforms:
        raise media_compliance.MediaPreparationRequired("Image preparation needs a complete set and selected platforms")
    backup = platform_limits.backup_compatible_platforms(request_data)
    prepared = []
    for platform in sorted(selected_platforms):
        if platform == "youtube" and not video_present:
            raise media_compliance.MediaPreparationRequired("YouTube images can only be a cover for a selected video")
        sources = [item["source_path"] if platform == "tiktok" else item["publish_path"] for item in images]
        # A thumbnail is not an extra backup image/post contract.
        backup_image = platform in backup and platform != "youtube"
        limits = image_compliance.effective_limits(platform,
            limits=(request_data.get("_imagePreparationLimits") or {}).get(platform), backup_compatible=backup_image)
        parts = image_compliance.prepare_images(sources, media_pipeline.build_campaign_workspace(campaign_id),
            platform=platform, limits=limits, backup_compatible=backup_image)
        if len(parts) != len(images):
            raise media_compliance.MediaPreparationError("Incomplete final image generation")
        prepared.append((platform, parts, backup_image))
    rows = []
    for platform, parts, backup_image in prepared:
        for item, part in zip(images, parts):
            metadata = {**part.metadata, "role": "cover" if platform == "youtube" else "image",
                        "unwatermarked": platform == "tiktok" or item["publish_path"] == item["source_path"],
                        "backup_compatible": backup_image}
            rows.append(_artifact_row(part, campaign_id=campaign_id, media_file=item["media_file"],
                metadata=metadata, image=True, upload_to_remote=upload_to_remote, db_path=db_path,
                public_base_url=public_base_url))
    if register:
        campaigns.add_campaign_artifact_set(campaign_id, rows, db_path=db_path)
    return _result(rows, selected_platforms)
