"""Fail-closed preparation of COMPLETE publication intents before scheduling.

``prepare_payloads`` may encode outside queue transactions. Scheduled bytes are
immutable unless explicit recovery passes ``allow_reprepare=True``; even then
only the checksum-verified scheduled part is used, never an original/raw URL.
``validate_payload`` is validation-only: no encodes, files or database writes.
"""
from __future__ import annotations

import copy
from pathlib import Path
from urllib.parse import urlsplit

from myUtils import image_compliance, media_compliance as compliance, text_limits

CONTEXT_SUFFIXES = {".txt", ".wav", ".mp3", ".flac", ".srt", ".vtt", ".json"}
SUPPORT_ROLES = {"cover", "thumbnail", "screenshot", "video_cover"}
SUPPORT_KINDS = {"cover", "thumbnail", "screenshot", "video_thumbnail", "video_cover"}
_MIXED_ROUTES = {"discord", "nw_sw_blog", "teaching_blog", "patreon", "medium", "substack"}


def is_context_artifact(artifact: dict) -> bool:
    metadata = artifact.get("metadata") or {}
    kind = str(artifact.get("artifact_kind") or "").lower()
    role = str(metadata.get("role") or "").lower()
    path = str(artifact.get("local_path") or artifact.get("public_url") or "")
    return (role in {"audio", "transcript"} or "transcript" in kind or "audio" in kind
            or Path(urlsplit(path).path).suffix.lower() in CONTEXT_SUFFIXES)


def is_support_artifact(artifact: dict) -> bool:
    metadata = artifact.get("metadata") or {}
    kind = str(artifact.get("artifact_kind") or "").lower()
    role = str(metadata.get("role") or "").lower()
    path = str(artifact.get("local_path") or artifact.get("public_url") or "")
    return (role in SUPPORT_ROLES or kind in SUPPORT_KINDS
            or (bool(metadata.get("source_video")) and "video" not in kind and role != "video"
                and Path(urlsplit(path).path).suffix.lower() not in compliance.VIDEO_SUFFIXES))


def media_kind(artifact: dict) -> str | None:
    if is_context_artifact(artifact) or is_support_artifact(artifact):
        return None
    metadata = artifact.get("metadata") or {}
    kind = str(artifact.get("artifact_kind") or "").lower()
    path = str(artifact.get("local_path") or artifact.get("public_url") or "")
    suffix = Path(urlsplit(path).path).suffix.lower()
    if suffix in image_compliance.IMAGE_SUFFIXES or "image" in kind or metadata.get("role") == "image":
        return "image"
    if suffix in compliance.VIDEO_SUFFIXES or "video" in kind or metadata.get("role") == "video":
        return "video"
    local = artifact.get("local_path")
    if local:
        from myUtils.campaign_media_prep import probe_media_kind
        return probe_media_kind(local)
    return None


def _working_payload(payload: dict, platform: str) -> dict:
    try:
        text_limits.field_limit(platform)  # unknown never means unlimited permission
    except ValueError as exc:
        raise compliance.MediaPreparationRequired(str(exc)) from exc
    supplied = payload.get("artifacts") or []
    if not isinstance(supplied, list) or any(not isinstance(item, dict) for item in supplied):
        raise compliance.MediaPreparationError("Invalid media artifact set")
    if any(item.get("metadata") is not None and not isinstance(item["metadata"], dict) for item in supplied):
        raise compliance.MediaPreparationError("Invalid media artifact metadata")
    if len(supplied) > image_compliance.MAX_SET_IMAGES:
        raise compliance.MediaPreparationError("Media intent exceeds the bounded preparation count")
    working = copy.deepcopy(payload)
    artifacts = [item for item in working.get("artifacts") or [] if not is_context_artifact(item)]
    aliases = {}
    for item in artifacts:
        key = (item.get("local_path") or item.get("public_url"), item.get("source_file_record_id"))
        if not key[0]:
            raise compliance.MediaPreparationError("A supplied media artifact has no exact path or URL")
        previous = aliases.get(key)
        if previous is not None:
            if (previous.get("metadata") or {}).get("sha256") != (item.get("metadata") or {}).get("sha256"):
                raise compliance.MediaPreparationError("Media aliases refer to different approved bytes")
            if item.get("public_url") and not previous.get("public_url"):
                previous["public_url"] = item["public_url"]
        else:
            aliases[key] = item
    artifacts = list(aliases.values())
    if supplied and not artifacts:
        raise compliance.MediaPreparationError("Only internal audio/transcript artifacts were supplied")
    videos = [item for item in artifacts if media_kind(item) == "video"]
    if platform == "youtube" and not videos and any(media_kind(item) == "image" for item in artifacts):
        raise compliance.MediaPreparationRequired("YouTube images can only be a cover for a selected video")
    if videos:
        for item in artifacts:
            if media_kind(item) != "image":
                continue
            local = Path(str(item.get("local_path") or ""))
            # YouTube already uses image artifacts as a thumbnail, not a second
            # publication. An unlabelled same-stem image is likewise a cover.
            if platform == "youtube" or (not (item.get("metadata") or {}).get("role") and any(
                    local.stem == Path(str(video.get("local_path") or "")).stem for video in videos)):
                item.setdefault("metadata", {})["role"] = "cover"
    working["artifacts"] = artifacts
    unknown = [item for item in artifacts if not is_support_artifact(item) and media_kind(item) is None]
    if unknown:
        raise compliance.MediaPreparationError("Supplied artifact is not a supported decodable image/video")
    return working


def _local_source(artifact: dict) -> Path:
    local = artifact.get("local_path")
    if not local or not Path(local).is_file():
        raise compliance.MediaPreparationRequired("Materialise the exact local media before preprocessing or uploading")
    return Path(local).expanduser().resolve()


def _is_prepared(metadata: dict) -> bool:
    return bool(metadata.get("preparation_policy_version") or "sha256" in metadata)


def _constraints(metadata: dict, platform: str, account_config: dict | None,
                 backup_compatible: bool, supplied: dict | None, *, image: bool = False) -> dict:
    module = image_compliance if image else compliance
    current = module.effective_limits(platform, account_config=account_config, backup_compatible=backup_compatible)
    planned = compliance.tighten_limits(current, metadata.get("preparation_limits"))
    return compliance.tighten_limits(planned, supplied)


def _validate_artifact(artifact: dict, platform: str, account_config: dict | None,
                       backup_compatible: bool, supplied: dict | None = None) -> None:
    metadata = artifact.get("metadata") or {}
    source = _local_source(artifact)
    image = media_kind(artifact) == "image"
    if platform == "tiktok" and _is_prepared(metadata) and metadata.get("unwatermarked") is not True:
        raise compliance.MediaPreparationError("TikTok requires the prepared unwatermarked variant")
    expected = metadata.get("sha256") if _is_prepared(metadata) else None
    if _is_prepared(metadata) and not expected:
        raise compliance.MediaPreparationError("Prepared media has no checksum")
    module = image_compliance if image else compliance
    validator = module.validate_image if image else module.validate_video
    validator(source, platform=platform, account_config=account_config,
              limits=_constraints(metadata, platform, account_config, backup_compatible, supplied, image=image),
              expected_sha256=expected, backup_compatible=backup_compatible)


def _validate_approved_text(payload: dict, platform: str, *, has_media: bool) -> None:
    if not text_limits.preserves_approved_text(payload):
        return
    draft = payload.get("draft") or {}
    message = payload.get("message") if "message" in payload else draft.get("message", "")
    field = "caption" if platform == "telegram" and has_media else "message"
    text_limits.validate_message(message, platform, field=field)
    if platform == "youtube":
        if draft.get("title") is not None:
            text_limits.validate_message(draft["title"], platform, field="title")
        if draft.get("description") is not None:
            text_limits.validate_message(draft["description"], platform, field="description")
        tags = draft.get("tags") or draft.get("hashtags") or []
        if isinstance(tags, str):
            tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
        text_limits.validate_youtube_tags([str(tag).lstrip("#") for tag in tags])
        from myUtils.prepared_publishers import youtube_text_fields
        youtube_text_fields(payload)  # same read-only mapping as the native publisher
    elif platform == "tiktok" and (payload.get("tiktokPostSettings") or {}).get("title") is not None:
        text_limits.validate_message(payload["tiktokPostSettings"]["title"], platform, field="caption")


def validate_payload(payload: dict, *, platform: str, account_config: dict | None = None,
                     backup_compatible: bool = False) -> None:
    """Read-only current-account gate, suitable for guarded transaction rechecks."""
    platform = str(platform or "").strip().lower()
    if type(backup_compatible) is not bool:
        raise compliance.MediaPreparationError("backup_compatible must be a boolean")
    working = _working_payload(payload, platform)
    images = [item for item in working["artifacts"] if media_kind(item) == "image"]
    videos = [item for item in working["artifacts"] if media_kind(item) == "video"]
    if images:
        cap = min(_constraints(item.get("metadata") or {}, platform, account_config,
                   backup_compatible, None, image=True)["max_items"] for item in images)
        if len(images) > cap:
            raise compliance.MediaPreparationRequired(f"Schedule ALL {len(images)} images in ordered batches of {cap}")
    if videos:
        cap = min(_constraints(item.get("metadata") or {}, platform, account_config,
                   backup_compatible, None)["max_items"] for item in videos)
        if len(videos) > cap or (images and (backup_compatible or platform not in _MIXED_ROUTES)):
            raise compliance.MediaPreparationRequired("Schedule separate complete video/image intents before publication")
    _validate_approved_text(working, platform, has_media=bool(images or videos))
    for item in [*videos, *images]:
        _validate_artifact(item, platform, account_config, backup_compatible)
    if platform == "youtube":
        for cover in (item for item in working["artifacts"] if is_support_artifact(item)):
            metadata = cover.get("metadata") or {}
            image_compliance.validate_image(_local_source(cover), platform="youtube",
                limits=_constraints(metadata, "youtube", account_config, False, None, image=True),
                expected_sha256=metadata.get("sha256"))


def _replacement(original: dict, part, source: Path, *, image: bool, platform: str) -> dict:
    from myUtils.video_artifacts import served_url
    replacement = {**original, "artifact_kind": "prepared_image" if image else "prepared_video",
                   "local_path": str(part.path), "metadata": {**(original.get("metadata") or {}), **part.metadata}}
    if platform == "tiktok":
        replacement["metadata"]["unwatermarked"] = True
    if part.path != source:
        for key in ("id", "remote_path", "storage_backend_id"):
            replacement.pop(key, None)
        replacement["public_url"] = served_url(part.path)
    return replacement


def prepare_payloads(payload: dict, *, platform: str, account_config: dict | None = None,
                     out_dir: Path | None = None, backup_compatible: bool = False,
                     allow_reprepare: bool = False, video_limits: dict | None = None,
                     image_limits: dict | None = None) -> list[dict]:
    """Return EVERY ordered post. Never use this encoding API in a DB transaction."""
    from utils.conf_defaults import BASE_DIR
    platform = str(platform or "").strip().lower()
    if type(allow_reprepare) is not bool or type(backup_compatible) is not bool:
        raise compliance.MediaPreparationError("Preparation flags must be booleans")
    working = _working_payload(payload, platform)
    if allow_reprepare:
        working["_preserveApprovedText"] = True
    artifacts = working["artifacts"]
    media = [item for item in artifacts if media_kind(item)]
    support = [item for item in artifacts if is_support_artifact(item)]
    _validate_approved_text(working, platform, has_media=bool(media))
    if not media:
        if artifacts:
            raise compliance.MediaPreparationRequired("Only cover/support artifacts were supplied; publishable media is required")
        return [working]
    root = out_dir or (Path(BASE_DIR) / "generated" / "publish-preprocessing")
    states = {}
    # Verify every immutable source BEFORE starting any repair/encoding.
    for original in media:
        source, metadata = _local_source(original), original.get("metadata") or {}
        image = media_kind(original) == "image"
        if platform == "tiktok" and ("watermarked" in str(original.get("artifact_kind") or "")
                or metadata.get("unwatermarked") is False
                or (_is_prepared(metadata) and metadata.get("unwatermarked") is not True)):
            raise compliance.MediaPreparationError("TikTok requires the unwatermarked source variant")
        needs_prepare = True
        if _is_prepared(metadata):
            compliance.verify_checksum(source, metadata.get("sha256"))
            try:
                _validate_artifact(original, platform, account_config, backup_compatible,
                                   image_limits if image else video_limits)
            except compliance.MediaPreparationError:
                if not allow_reprepare:
                    raise
            else:
                needs_prepare = False
        states[id(original)] = (source, metadata, image, needs_prepare)
    prepared_images = {}
    image_members = [item for item in media if states[id(item)][2]]
    if any(states[id(item)][3] for item in image_members):
        # Explicit recovery creates one complete new image generation, including
        # verified valid neighbours; it never mixes a new index with stale tails.
        members = image_members if allow_reprepare else [item for item in image_members if states[id(item)][3]]
        constraints = image_compliance.effective_limits(platform, account_config=account_config,
                       limits=image_limits, backup_compatible=backup_compatible)
        for item in members:
            constraints = compliance.tighten_limits(constraints, (item.get("metadata") or {}).get("preparation_limits"))
        origins = [_repair_origin(states[id(item)][1], states[id(item)][0], image=True)
                   if _is_prepared(states[id(item)][1]) else None for item in members]
        parts = image_compliance.prepare_images([states[id(item)][0] for item in members], root,
            platform=platform, account_config=account_config, limits=constraints,
            backup_compatible=backup_compatible, provenance=origins)
        if len(parts) != len(members):
            raise compliance.MediaPreparationError("Incomplete prepared image set")
        prepared_images = {id(item): _replacement(item, part, states[id(item)][0], image=True, platform=platform)
                           for item, part in zip(members, parts)}
    replacements = []
    for original in media:
        source, metadata, image, needs_prepare = states[id(original)]
        if image:
            replacements.append((True, [prepared_images.get(id(original), original)]))
        elif not needs_prepare:
            replacements.append((False, [original]))
        else:
            constraints = _constraints(metadata, platform, account_config, backup_compatible, video_limits)
            origin = _repair_origin(metadata, source, image=False) if _is_prepared(metadata) else None
            parts = compliance.prepare_video(source, root, platform=platform, account_config=account_config,
                limits=constraints, backup_compatible=backup_compatible, provenance=origin,
                force_generation=True)
            if not parts:
                raise compliance.MediaPreparationError("Preparation returned no media set")
            replacements.append((False, [_replacement(original, part, source, image=False, platform=platform) for part in parts]))
    if platform == "youtube":
        support = [_prepare_cover(cover, root, account_config, allow_reprepare) for cover in support]
    flattened = [(image, item) for image, parts in replacements for item in parts]
    if len(flattened) > compliance.MAX_PARTS:
        raise compliance.MediaPreparationError("Complete intent exceeds the bounded output-part count")
    groups, run = [], []
    for image, item in flattened:
        if image:
            run.append(item)
        else:
            if run:
                groups.extend(image_compliance.ordered_batches(run, platform, account_config=account_config,
                              limits=image_limits, backup_compatible=backup_compatible))
            run = []
            groups.append([item])
    if run:
        groups.extend(image_compliance.ordered_batches(run, platform, account_config=account_config,
                      limits=image_limits, backup_compatible=backup_compatible))
    result = []
    for index, group in enumerate(groups, 1):
        current = copy.deepcopy(working)
        current["artifacts"] = copy.deepcopy(support + group)
        if len(group) == 1 and media_kind(group[0]) == "video":
            metadata = group[0].get("metadata") or {}
            current["preparedPart"] = {"index": metadata.get("part_index"), "count": metadata.get("part_count"),
                                       "group": metadata.get("preparation_group")}
        if len(groups) > 1:
            current["preparedPost"] = {"index": index, "count": len(groups)}
        validate_payload(current, platform=platform, account_config=account_config, backup_compatible=backup_compatible)
        result.append(current)
    if not result:
        raise compliance.MediaPreparationError("Preparation returned no complete payload set")
    return result


def ensure_single_payload(payload: dict, *, platform: str, account_config: dict | None = None,
                          backup_compatible: bool = False, allow_reprepare: bool = False) -> None:
    prepared = prepare_payloads(payload, platform=platform, account_config=account_config,
                                backup_compatible=backup_compatible, allow_reprepare=allow_reprepare)
    if len(prepared) != 1:
        raise compliance.MediaPreparationRequired(
            f"Media requires {len(prepared)} ordered posts; preprocess and schedule ALL parts before publication")
    payload.update(prepared[0])
    from myUtils import delivery
    delivery.save_payload_progress(payload)


def _repair_origin(metadata: dict, source: Path, *, image: bool) -> dict:
    origin = copy.deepcopy(metadata)
    origin.setdefault("source_sha256", metadata["sha256"])
    if not origin.get("source_path"):
        origin["source_path"] = str(source)
    if not image and ("source_start_seconds" not in origin or "source_end_seconds" not in origin):
        if (origin.get("part_count") or 1) > 1 or (origin.get("part_index") or 1) > 1:
            raise compliance.MediaPreparationError("Older prepared part has no provable original source interval")
        origin["source_start_seconds"] = 0.0
        origin["source_end_seconds"] = compliance._source_metadata(source)["duration"]
    return origin


def _prepare_cover(cover: dict, root: Path, config: dict | None, allow_reprepare: bool) -> dict:
    metadata = cover.get("metadata") or {}
    source = _local_source(cover)
    constraints = _constraints(metadata, "youtube", config, False, None, image=True)
    if _is_prepared(metadata):
        compliance.verify_checksum(source, metadata.get("sha256"))
        try:
            image_compliance.validate_image(source, platform="youtube", limits=constraints,
                                           expected_sha256=metadata["sha256"])
            return cover
        except compliance.MediaPreparationError:
            if not allow_reprepare:
                raise
    part = image_compliance.prepare_image(source, root, platform="youtube", limits=constraints,
        provenance=_repair_origin(metadata, source, image=True) if _is_prepared(metadata) else None)
    replacement = _replacement(cover, part, source, image=True, platform="youtube")
    replacement["metadata"]["role"] = "cover"
    return replacement
