"""Publish Center orchestration.

The orchestrator wires together the existing media-prep, draft-generation,
and job-enqueue machinery into one ``submit_publish`` entry point used by
the new Publish Center UI. It is deliberately pure-Python with no Flask
or HTTP coupling — the Flask layer passes in the heavy callbacks that
already live in ``sau_backend`` (artifact preparation, per-account draft
generation, etc.) so this module can be exercised in isolation.

The interesting business logic this module owns is:

* fan-out across multiple selected profiles, one campaign per profile;
* per-account draft selection (user override -> generator output);
* the *single-media-only platform* split: when a platform such as TikTok or
  Tencent only accepts one media item per post and the user uploaded
  several, we create N campaign posts pinned to one media file each and
  stagger them through the destination's minimum gap and daily budget;
* deterministic base-time scheduling so a deferred "Publish at" timestamp
  applies uniformly across every target the user just queued.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from functools import wraps
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from myUtils import campaigns as campaign_store
from myUtils import content_language
from myUtils import content_rating
from myUtils import content_rules
from myUtils import jobs as job_runtime
from myUtils import media_groups as media_group_store
from myUtils import platform_capabilities
from myUtils import media_compliance, image_compliance, platform_limits, publish_media_guard
from myUtils import destination_pacing
from myUtils.account_identity import canonical_account_keys, external_account_identity, structured_account_id

_logger = logging.getLogger(__name__)
from myUtils import profiles as profile_registry
from myUtils.sqlite_utils import connect as _db_connection


STAGGER_MINUTES = 5

# Anti-spam spacing for scheduled targets. Independent submits used to reset
# their stagger offset to zero, so four campaigns queued for the same "21:00
# Taipei" window all landed on the same account at the exact same minute.
# ``_next_free_slot`` now consults the targets already booked for an account
# (and the ones allocated earlier in the same submit) and walks forward until
# it finds a minute at least ``MIN_GAP_MINUTES`` from any existing booking and
# within the per-account daily cap.
MIN_GAP_MINUTES = int(os.environ.get("SAU_PUBLISH_MIN_GAP_MINUTES", "30") or 30)
MAX_POSTS_PER_ACCOUNT_PER_DAY = int(
    os.environ.get("SAU_PUBLISH_MAX_PER_DAY", "3") or 3
)


def _serialize_slot_allocation(function):
    """Use the same cross-process reservation for sync and async finalizers."""
    @wraps(function)
    def reserved(*args, **kwargs):
        with slot_reservation_lock(kwargs["db_path"]):
            return function(*args, **kwargs)
    return reserved


def _external_account_identity(account) -> tuple[str, str] | None:
    """Compatibility wrapper for every supported stable native provider ID."""
    return external_account_identity(account)


def _account_ids_with_identity(platform: str, value: str, *, db_path: Path) -> list[int]:
    """All rows with the exact stable native ID, without decrypting credentials."""
    platform = "twitter" if platform.strip().lower() == "x" else platform.strip().lower()
    try:
        with destination_pacing.connect_readonly(db_path) as conn:
            return [row["id"] for row in conn.execute("SELECT id,platform,config_json FROM accounts ORDER BY id")
                    if external_account_identity(row) == (platform, str(value).strip())]
    except (sqlite3.Error, FileNotFoundError):
        return []


def _shared_identity_already_queued(
    account, media_group_id: int, *, db_path: Path, account_keys: dict[str, str] | None = None,
) -> bool:
    """Compatibility wrapper: queued/evidenced media through another alias row."""
    return _already_queued_for_media(
        account.id, media_group_id, db_path=db_path, account_keys=account_keys, _other_rows_only=True,
    )


def _already_queued_for_media(
    account_id: int, media_group_id: int, *, db_path: Path,
    account_keys: dict[str, str] | None = None, _other_rows_only: bool = False,
) -> bool:
    """Same whole original media group/file set at this canonical destination.

    Check once BEFORE an account's complete part set is enqueued, never between
    parts. Equality is bidirectional on the ORIGINAL group's file records, not
    partial overlap on individual prepared parts or shared thumbnails. Live,
    review and persisted submission/delivery evidence all prevent a duplicate;
    terminal failures without evidence remain eligible for deliberate re-publish.
    """
    account_ref = f"account:{int(account_id)}"
    try:
        with destination_pacing.connect_readonly(db_path) as conn:
            conn.execute("BEGIN")
            keys = account_keys if account_keys is not None else canonical_account_keys(conn, (account_ref,))
            identity = keys.get(account_ref)
            refs = [ref for ref, key in keys.items() if key == identity] if identity else [account_ref]
            if _other_rows_only:
                refs = [ref for ref in refs if ref != account_ref]
            if not refs:
                return False
            params = {f"ref{index}": ref for index, ref in enumerate(refs)}
            params["media_group_id"] = int(media_group_id)
            placeholders = ",".join(f":ref{index}" for index in range(len(refs)))
            rows = conn.execute(
                f"""
                WITH candidate AS (
                    SELECT DISTINCT file_record_id FROM media_group_items
                    WHERE media_group_id = :media_group_id
                )
                SELECT t.status,t.checkpoint_json,t.delivery_state
                FROM publish_job_targets t
                JOIN campaign_posts cp ON ('campaign_post:' || cp.id) = t.file_ref
                JOIN campaigns c ON c.id = cp.campaign_id
                WHERE t.account_ref IN ({placeholders})
                  AND (
                        c.media_group_id = :media_group_id
                        OR (
                            EXISTS (SELECT 1 FROM candidate)
                            AND NOT EXISTS (
                                SELECT 1 FROM candidate cand
                                WHERE NOT EXISTS (
                                    SELECT 1 FROM media_group_items queued_item
                                    WHERE queued_item.media_group_id = c.media_group_id
                                      AND queued_item.file_record_id = cand.file_record_id
                                )
                            )
                            AND NOT EXISTS (
                                SELECT 1 FROM media_group_items queued_item
                                WHERE queued_item.media_group_id = c.media_group_id
                                  AND NOT EXISTS (
                                      SELECT 1 FROM candidate cand
                                      WHERE cand.file_record_id = queued_item.file_record_id
                                  )
                            )
                        )
                  )
                """, params,
            )
            for row in rows:
                if row["status"] in {"pending", "retrying", "running", "awaiting_delivery", "needs_review"}:
                    return True
                if job_runtime._recovery_has_submission_evidence(row["checkpoint_json"], row["delivery_state"]):
                    return True
        return False
    except (sqlite3.Error, FileNotFoundError):
        # Standalone legacy lookups are fail-soft; normal finalizers first require
        # a trustworthy booking snapshot, which fails closed on missing schema.
        _logger.warning("duplicate-queue check unavailable for account=%s media_group=%s",
                        account_id, media_group_id, exc_info=True)
        return False


@dataclass(slots=True)
class SubmitResult:
    campaign_ids: list[int]
    jobs: list[dict]
    skipped: list[dict]


def _to_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _operator_timezone() -> tzinfo:
    """The timezone an operator's naive datetime is expressed in.

    The Publish Center date picker sends a naive local string
    (``value-format="YYYY-MM-DDTHH:mm:00"``, no offset), while the reschedule
    path sends a real UTC instant via ``toISOString()``. Treating the naive form
    as UTC made an operator who picked 07:00 get a post at 15:00, because the
    intended wall clock was read as 07:00 UTC.

    Defaults to Asia/Shanghai (the deployment's operator timezone, and the cron's
    ``CRON_TZ``), overridable with ``SAU_OPERATOR_TIMEZONE``.
    """
    name = str(os.environ.get("SAU_OPERATOR_TIMEZONE") or "Asia/Shanghai").strip()
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - an unknown zone must not break scheduling
        _logger.warning(
            "unknown SAU_OPERATOR_TIMEZONE %r; treating naive times as UTC", name
        )
        return timezone.utc


def _resolve_base_time(schedule: dict | None, *, now_fn=datetime.now) -> datetime | None:
    """Resolve the user-requested base publish time.

    Returns ``None`` as the publish-now signal; allocation still applies the
    destination's budget from the current UTC time. Otherwise returns a
    timezone-naive UTC datetime, as ``publish_job_targets.schedule_at`` expects.

    An offset-aware input is converted to UTC. A *naive* input is taken as the
    operator's local wall clock and converted from ``_operator_timezone()``,
    because that is the only reading of it that matches what the date picker
    shows. Assuming UTC here silently shifted every scheduled post by the
    timezone offset (8 hours for CST).
    """
    if not schedule or not isinstance(schedule, dict):
        return None
    if schedule.get("publishNow"):
        return None
    candidate = _to_datetime(schedule.get("startAt") or schedule.get("scheduledAt"))
    if candidate is None:
        return None
    if candidate.tzinfo is not None:
        return candidate.astimezone(timezone.utc).replace(tzinfo=None)
    # Naive: the operator's wall clock. Localise then convert to UTC.
    try:
        localised = candidate.replace(tzinfo=_operator_timezone())
    except Exception:  # noqa: BLE001
        return candidate
    return localised.astimezone(timezone.utc).replace(tzinfo=None)


def _media_role_for_path(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    image_suffixes = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    if suffix in image_suffixes:
        return media_group_store.ROLE_IMAGE
    return media_group_store.ROLE_VIDEO


def _clean_text(value) -> str:
    """Return a stripped string, or "" for anything that is not a real string."""
    return value.strip() if isinstance(value, str) else ""


def _request_data_for_options(
    *,
    brief: str,
    options: dict,
    profile: profile_registry.Profile,
) -> dict:
    """Translate Publish Center option toggles into the request payload
    shape expected by ``_prepare_campaign_media_artifacts`` and
    ``_generate_platform_draft``.

    Note: when an option toggle is off we explicitly set the relevant
    keys to empty/false so the downstream logic doesn't silently fall
    back to the profile defaults.

    Remote hosting of media (for URL-fetch platforms like TikTok/Meta/
    Threads) is decided downstream in ``_prepare_campaign_media_artifacts``
    from the selected platforms and the configured storage backends; here we
    only forward an explicit user ``uploadToRemote`` toggle.
    """
    options = options or {}
    profile_settings = profile.settings or {}
    request_data: dict = {
        "notes": brief or "",
        "title": (options.get("title") or "").strip(),
        "transcribe": bool(options.get("transcribe", False)),
        "useLlm": bool(options.get("useLlm", True)),
    }

    # Contact details / CTA: explicit option, else the profile column, else
    # the legacy settings key. Threads' platform rule *requires* both; when
    # they were missing draft generation raised and the orchestrator fell
    # back to posting the raw brief text.
    request_data["contactDetails"] = (
        _clean_text(options.get("contactDetails"))
        or _clean_text(getattr(profile, "contact_details", ""))
        or _clean_text(profile_settings.get("contactDetails"))
    )
    request_data["cta"] = (
        _clean_text(options.get("cta"))
        or _clean_text(getattr(profile, "default_cta", ""))
        or _clean_text(profile_settings.get("ctaText"))
    )

    if options.get("watermarkDisabled") is True:
        # Explicit opt-out: an empty string short-circuits
        # _derive_watermark_spec's fallback, so no watermark is applied.
        request_data["watermark"] = ""
    else:
        # Default: an explicit override wins, otherwise the profile's
        # configured watermark. This brands every image and video without
        # relying on the caller to opt in; pass options.watermarkDisabled=true
        # (or a watermarkOverride of "") to turn it off.
        override = options.get("watermarkOverride")
        request_data["watermark"] = (
            override if override is not None else profile_settings.get("watermark")
        ) or ""

    if options.get("intro"):
        request_data["intros"] = options.get("intros") or profile_settings.get("intros") or []
    else:
        request_data["intros"] = []

    if options.get("outro"):
        request_data["outros"] = options.get("outros") or profile_settings.get("outros") or []
    else:
        request_data["outros"] = []

    screenshots = options.get("screenshots") or {}
    request_data["screenshots"] = {
        "enabled": bool(screenshots.get("enabled")),
        "count": int(screenshots.get("count") or 0),
        "timestamps": screenshots.get("timestamps")
        if isinstance(screenshots.get("timestamps"), list)
        else None,
    }

    # Only forward an explicit user toggle; the backend auto-enables remote
    # hosting when a URL-fetch platform is selected and a public storage
    # backend (share/DO Spaces/rclone) is configured.
    if "uploadToRemote" in options:
        request_data["uploadToRemote"] = bool(options.get("uploadToRemote", False))

    return request_data


def _resolve_accounts(
    profile_id: int,
    selected_account_ids: list[int] | None,
    *,
    db_path: Path,
) -> list[profile_registry.Account]:
    rows = profile_registry.list_accounts(
        profile_id=profile_id,
        enabled=True,
        db_path=db_path,
    )
    if not selected_account_ids:
        return list(rows)
    allowed = {int(account_id) for account_id in selected_account_ids}
    return [account for account in rows if account.id in allowed]


def _account_language_tokens(account, profile) -> set[str]:
    """Language tags an account accepts (``audience_language`` etc.)."""
    config = getattr(account, "config", None) or {}
    settings = getattr(profile, "settings", None) or {}
    raw = str(
        config.get("audience_language")
        or config.get("audienceLanguage")
        or settings.get("default_language")
        or settings.get("defaultLanguage")
        or ""
    ).lower().replace(",", " ").replace("+", " ").replace("/", " ")
    return {token for token in raw.split() if token}


def _content_language(media_context, request_data, metadata=None) -> str | None:
    """The prepared content's language (``zh``/``en``), or None when unknown.

    Delegates to :mod:`myUtils.content_language`, which ranks the evidence and
    refuses to call a language-neutral filename "English". The campaign's stored
    language is consulted too, so a target created later (or a re-prep) still
    knows what the media actually is.
    """
    language, _source = content_language.resolve(
        metadata=metadata, media_context=media_context, request_data=request_data
    )
    return language


def language_routing_skip_reason(
    content_language: str | None,
    account,
    profile,
    *,
    include_zh_for_english: bool = False,
) -> str | None:
    """Why ``account`` must not receive ``content_language`` content, or None.

    Chinese content goes only to Chinese-audience accounts. English content
    goes to English-audience accounts, and additionally to Chinese ones only
    when the caller explicitly opts in (``includeZhForEnglish``). An account
    with no audience language is never filtered (it is not classified).
    """
    language = str(content_language or "").strip().lower()
    if language not in {"zh", "en"}:
        return None
    tokens = _account_language_tokens(account, profile)
    if not tokens:
        return None
    is_zh = any(token.startswith("zh") for token in tokens)
    is_en = any(token.startswith("en") for token in tokens)
    if language == "zh":
        if is_zh:
            return None
        return "content language is zh; account audience is English-only"
    if is_en:
        return None
    if include_zh_for_english and is_zh:
        return None
    return (
        "content language is en; account audience is Chinese-only "
        "(pass includeZhForEnglish to allow this)"
    )


def _files_with_roles(media_files: list[dict]) -> list[dict]:
    """Pick out only ``video`` and ``image`` items so intro/outro auxiliary
    files don't accidentally end up in a single-media split.
    """
    return [
        item
        for item in media_files
        if item.get("role") in {media_group_store.ROLE_VIDEO, media_group_store.ROLE_IMAGE}
    ]


def _preparation_limits_for_accounts(accounts, selected_platforms, requested=None,
                                     *, backup_compatible_platforms=None) -> dict:
    """Tighten each selected account FIRST, then merge; requests cannot grant caps."""
    if requested is not None and not isinstance(requested, dict):
        raise media_compliance.MediaPreparationError("Invalid selected-account preparation plan")
    backup = set(backup_compatible_platforms or [])
    result = {}
    for platform in selected_platforms:
        configs = [account.config or {} for account in accounts if account.platform == platform] or [{}]
        plans = [media_compliance.effective_limits(platform, account_config=config,
                 limits=(requested or {}).get(platform), backup_compatible=platform in backup) for config in configs]
        result[platform] = media_compliance.merge_limits(plans)
    return result


def _image_preparation_limits_for_accounts(accounts, selected_platforms, requested=None,
                                           *, backup_compatible_platforms=None) -> dict:
    if requested is not None and not isinstance(requested, dict):
        raise media_compliance.MediaPreparationError("Invalid selected-account image preparation plan")
    backup = set(backup_compatible_platforms or [])
    return {platform: image_compliance.effective_limits(platform,
            limits=(requested or {}).get(platform), backup_compatible=(platform in backup and
                not platform_limits.IMAGE_PREPARATION_POLICIES[platform].get("cover_only")))
            for platform in selected_platforms if platform in platform_limits.IMAGE_PREPARATION_POLICIES}


def _preparation_account_configs_for_accounts(accounts, selected_platforms) -> dict:
    return {platform: platform_limits.preparation_account_config(platform,
            [account.config or {} for account in accounts if account.platform == platform])
            for platform in selected_platforms}


def _video_artifact(artifact: dict) -> bool:
    if publish_media_guard.is_context_artifact(artifact) or publish_media_guard.is_support_artifact(artifact):
        return False
    return (artifact.get("artifact_kind") in {"prepared_video", "watermarked_video"}
            or (artifact.get("metadata") or {}).get("role") == "video"
            or Path(artifact.get("local_path") or "").suffix.lower() in media_compliance.VIDEO_SUFFIXES)


def _validate_prepared_part(artifact, platform, account_config=None, *, backup_compatible=False,
                            limits=None):
    metadata = artifact.get("metadata") or {}
    if not metadata.get("preparation_policy_version"):
        raise media_compliance.MediaPreparationRequired("Reprepare video before scheduling")
    checksum = metadata.get("sha256")
    if not isinstance(checksum, str) or len(checksum) != 64:
        raise media_compliance.MediaPreparationError("Prepared video has no checksum")
    current = media_compliance.effective_limits(platform, account_config=account_config,
                                               backup_compatible=backup_compatible)
    planned = media_compliance.tighten_limits(current, metadata.get("preparation_limits"))
    planned = media_compliance.tighten_limits(planned, limits)
    media_compliance.validate_video(artifact.get("local_path") or "", platform=platform,
        account_config=account_config, limits=planned, expected_sha256=checksum, backup_compatible=backup_compatible)


def _prepared_post_groups(artifacts, platform, media_files, *, supports_multi_media,
                          artifact_payloads_for_platform, artifact_part_groups_for_platform=None,
                          account_config=None, backup_compatible=False, image_limits=None, video_limits=None):
    """Read-only whole-generation gate; every ordered image/video becomes a post."""
    cleaned = [a for a in artifacts if not publish_media_guard.is_context_artifact(a)]
    files = _files_with_roles(media_files)
    videos = {item["file_record_id"] for item in files if item.get("role") == "video"}
    videos.update(a.get("source_file_record_id") for a in cleaned if _video_artifact(a))
    prepared = {}
    for source_id in videos:
        parts = media_compliance.select_prepared_parts(
            [a for a in cleaned if _video_artifact(a) and a.get("source_file_record_id") == source_id], platform)
        for part in parts:
            _validate_prepared_part(part, platform, account_config, backup_compatible=backup_compatible, limits=video_limits)
        prepared[source_id] = parts
    nonvideos = [a for a in cleaned if not _video_artifact(a)]
    selected = artifact_payloads_for_platform(nonvideos, platform)
    selected = [a for a in selected if not publish_media_guard.is_context_artifact(a)]
    support = [a for a in selected if publish_media_guard.is_support_artifact(a)]
    images = [a for a in selected if publish_media_guard.media_kind(a) == "image"]
    if platform == "youtube" and videos:
        # The existing publisher uses these as a thumbnail, never as a post.
        support += [{**a, "metadata": {**(a.get("metadata") or {}), "role": "cover"}} for a in images]
        images = []
    if images:
        if any(not (a.get("metadata") or {}).get("preparation_policy_version") for a in images):
            raise media_compliance.MediaPreparationRequired("Reprepare the complete image set before scheduling")
        # A callback cannot return only the first image or select from an older
        # group. Select the complete generation from the actual campaign rows.
        images = image_compliance.select_prepared_images(
            [a for a in nonvideos if publish_media_guard.media_kind(a) == "image"], platform)
        for image in images:
            metadata = image.get("metadata") or {}
            limits = image_compliance.effective_limits(platform, account_config=account_config,
                        limits=image_limits, backup_compatible=backup_compatible)
            limits = media_compliance.tighten_limits(limits, metadata.get("preparation_limits"))
            image_compliance.validate_image(image.get("local_path") or "", platform=platform,
                account_config=account_config, limits=limits, expected_sha256=metadata.get("sha256"),
                backup_compatible=backup_compatible)
    ordered_ids = [item["file_record_id"] for item in files]
    if set(prepared) - set(ordered_ids):
        raise media_compliance.MediaPreparationError("Prepared video has no selected source file")
    if not files and not (images or prepared):
        return [(selected, None)]  # a genuine text-only intent
    images_by_source = {}
    for image in images:
        images_by_source.setdefault(image.get("source_file_record_id"), []).append(image)
    if set(images_by_source) - set(ordered_ids):
        raise media_compliance.MediaPreparationError("Prepared image has no selected source file")
    groups, run = [], []
    def flush_images():
        if not run:
            return
        planned = image_compliance.effective_limits(platform, account_config=account_config,
                    limits=image_limits, backup_compatible=backup_compatible)
        # ordered_batches tightens counts from every immutable member's policy;
        # heterogeneous JPEG/PNG geometry need not share an encoder recipe.
        for batch in image_compliance.ordered_batches(run, platform, account_config=account_config,
                                                      limits=planned, backup_compatible=backup_compatible):
            groups.append((support + batch, list(dict.fromkeys(a.get("source_file_record_id") for a in batch))))
        run.clear()
    for item in files:
        source_id = item["file_record_id"]
        if source_id in prepared:
            flush_images()
            covers = [a for a in support if a.get("source_file_record_id") in {None, source_id} or platform == "youtube"]
            groups.extend((covers + [part], [source_id]) for part in prepared[source_id])
        elif source_id in images_by_source:
            if not images_by_source[source_id]:
                raise media_compliance.MediaPreparationError("Prepared image source count does not match the selected media")
            run.append(images_by_source[source_id].pop(0))
        elif not any(a.get("source_file_record_id") == source_id for a in support):
            raise media_compliance.MediaPreparationRequired("No prepared media for a selected source file")
    flush_images()
    if any(values for values in images_by_source.values()):
        raise media_compliance.MediaPreparationError("Complete prepared image set does not match the selected source order")
    if not groups:
        raise media_compliance.MediaPreparationRequired("No complete publishable prepared media set")
    if len(groups) == 1:
        groups[0] = (groups[0][0], None)
    return groups


@_serialize_slot_allocation
def submit_publish(
    *,
    profile_ids: list[int],
    selected_account_ids: list[int] | None,
    media_file_paths: list[str],
    brief: str,
    options: dict,
    schedule: dict | None,
    account_drafts: dict[int, dict] | None,
    tiktok_post_settings: dict[int, dict] | None = None,
    db_path: Path,
    prepare_artifacts: Callable,
    generate_account_draft: Callable,
    ensure_file_record_for_path: Callable,
    artifact_payloads_for_platform: Callable,
    artifact_part_groups_for_platform: Callable | None = None,
    job_to_payload: Callable,
    now_fn: Callable = datetime.now,
) -> SubmitResult:
    if not profile_ids:
        raise ValueError("At least one profile must be selected")
    if not media_file_paths:
        raise ValueError("At least one media file is required")

    account_drafts = account_drafts or {}
    tiktok_post_settings = tiktok_post_settings or {}
    base_time = _resolve_base_time(schedule, now_fn=now_fn)

    # 1. Materialise file_records + a single media group shared by every profile.
    file_record_ids = [
        ensure_file_record_for_path(path, db_path=db_path) for path in media_file_paths
    ]
    media_group = media_group_store.create_media_group(
        name=f"publish-center-{now_fn().strftime('%Y%m%d-%H%M%S')}",
        notes=(brief or "")[:255],
        primary_video_file_id=next(
            (
                record_id
                for record_id, path in zip(file_record_ids, media_file_paths)
                if _media_role_for_path(path) == media_group_store.ROLE_VIDEO
            ),
            None,
        ),
        db_path=db_path,
    )
    for index, (record_id, path) in enumerate(zip(file_record_ids, media_file_paths)):
        media_group_store.add_media_group_item(
            media_group.id,
            record_id,
            role=_media_role_for_path(path),
            sort_order=index,
            db_path=db_path,
        )

    campaign_ids: list[int] = []
    queued_jobs: list[dict] = []
    skipped: list[dict] = []
    stagger_offset = 0  # global ordering of targets across profiles
    # All aliases (including rows with no targets yet) share mutable calendars.
    allocation_time = now_fn().astimezone(timezone.utc).replace(tzinfo=None)
    account_keys: dict[str, str] = {}
    booked_slots = _load_booked_slots(db_path, account_keys=account_keys, now=allocation_time)

    # Content rating is derived from the filenames — the operator's rule is
    # that only a name starting or ending with "sfw" is SFW, everything else is
    # NSFW. It is applied *here*, after account resolution, because this is the
    # one function every publish entry point (web UI, inbox one-click, MCP)
    # funnels through. A request cannot loosen it: options.sfwFlag may only
    # tighten the result.
    rating = content_rating.rating_for_media(
        media_file_paths, explicit=(options or {}).get("sfwFlag")
    )
    media_roles = {_media_role_for_path(path) for path in media_file_paths}
    has_video = media_group_store.ROLE_VIDEO in media_roles
    has_image = media_group_store.ROLE_IMAGE in media_roles
    if not has_video and not has_image:
        raise ValueError("Selected media contains no supported image or video files")

    for profile_id in profile_ids:
        profile = profile_registry.get_profile(int(profile_id), db_path=db_path)
        accounts = _resolve_accounts(profile.id, selected_account_ids, db_path=db_path)
        if not accounts:
            skipped.append({"profileId": profile.id, "reason": "no_enabled_accounts"})
            continue
        try:
            accounts = content_rating.restrict_accounts(accounts, rating)
        except ValueError:
            # Every selected account is on a platform that bans nudity. Skip
            # the profile rather than raise: another profile in the same
            # request may still be publishable, and the skipped list tells the
            # caller exactly what happened.
            skipped.append({"profileId": profile.id, "reason": "nsfw_no_adult_safe_account"})
            continue

        compatible_accounts = []
        for account in accounts:
            if account.platform == "youtube" and not has_video:
                skipped.append({
                    "profileId": profile.id,
                    "accountId": account.id,
                    "accountName": account.nickname or account.account_name,
                    "platform": account.platform,
                    "reason": "YouTube publishing requires video media; this selection contains no video.",
                })
            elif account.platform == "tiktok" and not has_video and not bool((options or {}).get("tiktokDirectPost")):
                skipped.append({
                    "profileId": profile.id,
                    "accountId": account.id,
                    "accountName": account.nickname or account.account_name,
                    "platform": account.platform,
                    "reason": "TikTok photo publishing requires Direct Post; enable it or remove this destination.",
                })
            else:
                compatible_accounts.append(account)
        accounts = compatible_accounts
        if not accounts:
            continue

        request_data = _request_data_for_options(
            brief=brief, options=options, profile=profile
        )
        platforms = {account.platform for account in accounts}
        if (options or {}).get("_backupCompatiblePlatforms") is not None:
            request_data["_backupCompatiblePlatforms"] = options["_backupCompatiblePlatforms"]
        from myUtils.preparation_context import configure_request
        request_data, _backup_contexts = configure_request(accounts, platforms, request_data,
                                                         settings=profile.settings, has_video=has_video)
        backup_platforms = platform_limits.backup_compatible_platforms(request_data) & platforms
        campaign = campaign_store.create_campaign(
            profile.id,
            media_group.id,
            status=campaign_store.CAMPAIGN_PREPARING,
            selected_account_ids=[account.id for account in accounts],
            metadata={
                "title": request_data.get("title", ""),
                "notes": request_data.get("notes", ""),
                "requestedPlatforms": sorted({account.platform for account in accounts}),
                "source": "publish-center",
            },
            db_path=db_path,
        )
        campaign_ids.append(campaign.id)

        media_files = _load_media_group_files_via_callback(
            media_group.id, db_path=db_path
        )
        publishable_files = _files_with_roles(media_files)
        try:
            media_context = prepare_artifacts(
                campaign.id,
                profile,
                media_files,
                request_data,
                selected_platforms={account.platform for account in accounts},
                db_path=db_path,
            )
        except Exception as exc:  # noqa: BLE001
            campaign_store.update_campaign(
                campaign.id,
                status=campaign_store.CAMPAIGN_NEEDS_REVIEW,
                last_error=f"artifact prep failed: {exc}",
                db_path=db_path,
            )
            skipped.append({"profileId": profile.id, "reason": f"artifact prep failed: {exc}"})
            continue
        # Persist what the media actually is, so routing (now and on any later
        # re-prep) does not have to guess from a generated title.
        _language, _language_source = content_language.resolve(
            media_context=media_context, request_data=request_data
        )
        try:
            campaign_store.record_content_language(
                campaign.id, _language, _language_source, db_path=db_path
            )
        except Exception:  # noqa: BLE001 - bookkeeping must not block a publish
            _logger.warning("could not record content language for campaign %s",
                            campaign.id, exc_info=True)

        # group accounts by platform so each platform gets its own
        # campaign_posts/job spec
        grouped_accounts: dict[str, list[profile_registry.Account]] = {}
        for account in accounts:
            grouped_accounts.setdefault(account.platform, []).append(account)

        link_in_first_comment = bool((options or {}).get("linkInFirstComment"))
        # Publish publicly by default. The frontend toggle can still request a
        # draft (inbox) upload, but the historical default of False meant every
        # TikTok post silently landed in the drafts and was invisible on the
        # profile. An unreviewed app rejects DIRECT_POST and the universal
        # Sociamonials fallback then catches it.
        tiktok_direct_post = bool((options or {}).get("tiktokDirectPost", True))

        artifacts = [
            artifact.to_dict()
            for artifact in campaign_store.list_campaign_artifacts(campaign.id, db_path=db_path)
        ]

        for platform, platform_accounts in grouped_accounts.items():
            supports_first_comment = platform_capabilities.platform_supports_first_comment(platform)
            supports_multi_media = platform_capabilities.platform_supports_multi_media(platform)

            try:
                post_groups = _prepared_post_groups(
                    artifacts, platform, publishable_files, supports_multi_media=supports_multi_media,
                    artifact_payloads_for_platform=artifact_payloads_for_platform,
                    artifact_part_groups_for_platform=artifact_part_groups_for_platform,
                    account_config=request_data["_preparationAccountConfigs"].get(platform),
                    backup_compatible=platform in backup_platforms,
                    image_limits=(request_data.get("_imagePreparationLimits") or {}).get(platform),
                    video_limits=(request_data.get("_preparationLimits") or {}).get(platform),
                )
            except (ValueError, OSError) as exc:
                skipped.append({"profileId": profile_id, "platform": platform,
                                "reason": f"prepared media validation failed: {exc}"})
                continue
            for account in platform_accounts:
                # Cross-job duplicate guard. The target table is unique on
                # (job_id, account_ref, file_ref), so the same media re-submitted
                # under a new job used to queue again - that is how one account
                # accumulated 662 targets for 356 files. Skip (and report) rather
                # than silently piling on more scheduled sends.
                if _already_queued_for_media(
                    account.id, media_group.id, db_path=db_path, account_keys=account_keys
                ):
                    skipped.append({
                        "profileId": profile_id,
                        "accountId": account.id,
                        "accountName": account.nickname or account.account_name,
                        "platform": account.platform,
                        "reason": "this media is already queued for this account "
                                  "(directly, or through another profile's row for "
                                  "the same publishing destination)",
                    })
                    continue
                routing_skip = language_routing_skip_reason(
                    _content_language(media_context, request_data, campaign.metadata),
                    account,
                    profile,
                    include_zh_for_english=bool((options or {}).get("includeZhForEnglish")),
                )
                if routing_skip:
                    skipped.append({
                        "profileId": profile_id,
                        "accountId": account.id,
                        "accountName": account.nickname or account.account_name,
                        "platform": account.platform,
                        "reason": routing_skip,
                    })
                    continue
                draft_override = account_drafts.get(str(account.id)) or account_drafts.get(account.id)
                if isinstance(draft_override, dict) and draft_override.get("message"):
                    # The override arrives from the client, and a model-backed
                    # preview can leave the draft as a stringified JSON object or
                    # a "Title: ... / Description: ..." blob. Normalise here, at
                    # the one place the override is consumed, so every caller —
                    # the web UI, the inbox one-click route and MCP — gets the
                    # same clean copy the preview showed the operator.
                    draft = content_rules.normalize_draft_fields(dict(draft_override))
                else:
                    try:
                        # Draft content is account-specific (language, voice,
                        # and connected identity may differ even on one platform).
                        request_for_account = dict(request_data)
                        effective_language = str(
                            (account.config or {}).get("audience_language")
                            or (account.config or {}).get("audienceLanguage")
                            or (profile.settings or {}).get("default_language")
                            or (profile.settings or {}).get("defaultLanguage")
                            or ""
                        ).strip()
                        if platform == "twitter" and len(effective_language.replace(",", " ").replace("+", " ").split()) > 1:
                            raise ValueError("X requires one language per account; use distinct account settings instead of a bilingual language list")
                        if effective_language:
                            request_for_account["_accountLanguage"] = effective_language
                        draft = dict(generate_account_draft(
                            account, profile, media_group, request_for_account, media_context
                        ))
                    except Exception as exc:  # noqa: BLE001
                        account_config = account.config or {}
                        account_settings = profile.settings or {}
                        if (account_config.get("audience_language")
                                or account_config.get("audienceLanguage")
                                or account_settings.get("default_language")
                                or account_settings.get("defaultLanguage")):
                            skipped.append({
                                "profileId": profile_id,
                                "accountId": account.id,
                                "reason": f"language-specific draft generation failed: {exc}",
                            })
                            continue
                        if content_rules.is_usable_copy(brief):
                            draft = {
                                "message": (brief or "").strip(),
                                "hashtags": [],
                                "firstComment": "",
                                "error": str(exc),
                            }
                        else:
                            # No real copy and no usable brief: skip rather than
                            # publish the media-group name / generic batch brief.
                            skipped.append({
                                "profileId": profile_id,
                                "accountId": account.id,
                                "reason": f"draft generation failed and no usable brief: {exc}",
                            })
                            continue

                if not link_in_first_comment or not supports_first_comment:
                    # If the platform does not support a first-comment
                    # convention, fold any link back into the body so the
                    # user does not lose it.
                    if draft.get("firstComment") and not supports_first_comment:
                        message = (draft.get("message") or "").rstrip()
                        comment = (draft.get("firstComment") or "").strip()
                        if comment and comment not in message:
                            draft["message"] = (message + "\n\n" + comment).strip()
                        draft["firstComment"] = ""
                    elif not link_in_first_comment:
                        draft["firstComment"] = ""

                # The complete generation was checked before any post is added.
                # Every video part uses the same draft/options and the existing
                # queue, with source order followed by part order.
                part_base = base_time or allocation_time
                previous_part_slot = None
                for part_index, (part_artifacts, file_ids) in enumerate(post_groups, start=1):
                    post = campaign_store.add_campaign_post(
                        campaign.id, platform, account_ids=[account.id], draft=draft,
                        file_record_ids=file_ids, status=campaign_store.CAMPAIGN_POST_READY, db_path=db_path,
                    )
                    account_tt_settings = tiktok_post_settings.get(str(account.id)) or tiktok_post_settings.get(account.id)
                    payload = _build_payload(
                        campaign, post, draft, part_artifacts, platform,
                        tiktok_direct_post=tiktok_direct_post,
                        tiktok_post_settings=account_tt_settings if isinstance(account_tt_settings, dict) else None,
                    )
                    if file_ids is not None:
                        payload["fileRecordIds"] = file_ids
                    if len(post_groups) > 1:
                        payload["partIndex"] = part_index
                        payload["partCount"] = len(post_groups)
                    part_slot = _next_free_slot(account.id, part_base, stagger_offset, booked_slots,
                                                not_before=previous_part_slot)
                    targets = [(f"account:{account.id}", f"campaign_post:{post.id}", part_slot)]
                    previous_part_slot = part_slot
                    stagger_offset += 1
                    queued_jobs.append(_enqueue_post(
                        platform, payload, targets, campaign, post, job_to_payload, db_path))

        # A prep that queued nothing must not regress a campaign that already has
        # queued posts (see campaigns.campaign_has_queued_posts).
        already_queued = False
        if not queued_jobs:
            try:
                already_queued = campaign_store.campaign_has_queued_posts(
                    campaign.id, db_path=db_path
                )
            except Exception:  # noqa: BLE001 - status must never break prep
                already_queued = False
        succeeded = bool(queued_jobs) or already_queued
        campaign_store.update_campaign(
            campaign.id,
            status=campaign_store.CAMPAIGN_PUBLISHING if succeeded else campaign_store.CAMPAIGN_NEEDS_REVIEW,
            prepared_at=now_fn().isoformat(timespec="seconds"),
            published_at=None,
            last_error=None if succeeded else "No publishable posts queued",
            db_path=db_path,
        )

    return SubmitResult(campaign_ids=campaign_ids, jobs=queued_jobs, skipped=skipped)


def _compute_schedule_at(base_time: datetime | None, offset_index: int) -> datetime | None:
    if base_time is None and offset_index == 0:
        return None
    anchor = base_time or datetime.now(tz=timezone.utc).replace(tzinfo=None)
    return anchor + timedelta(minutes=STAGGER_MINUTES * offset_index)


@contextmanager
def slot_reservation_lock(db_path: Path | str):
    """Serialize finalizers and guarded recovery before their queue writes.

    The separate SQLite write reservation avoids holding the main database
    throughout preparation. A timeout or unavailable reservation is a refusal,
    never permission to allocate against a concurrently stale calendar.
    Missing databases/parents are not created by this helper.
    """
    path = Path(str(db_path)).resolve()
    if not path.is_file():
        raise FileNotFoundError("Slot reservation requires an existing regular database")
    lock_path = path.with_name(path.name + ".slots.lock")
    connection = None
    try:
        connection = sqlite3.connect(str(lock_path), timeout=30)
        connection.execute("BEGIN IMMEDIATE")
    except (sqlite3.Error, OSError, ValueError) as exc:
        if connection is not None:
            connection.close()
        raise destination_pacing.PacingRefused(
            "Slot reservation unavailable; retry after the current finalizer or recovery finishes"
        ) from exc
    try:
        yield
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


def _parse_schedule(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _load_booked_slots(
    db_path: Path | str, *, account_keys: dict[str, str] | None = None,
    now: datetime | str | None = None,
) -> dict[int, list[str]]:
    """Row aliases share one mutable destination calendar from a read-only snapshot.

    Include booked, active, unresolved and succeeded history. Missing databases,
    incompatible schema or corrupt history fail closed, never allocate as empty.
    ``account_keys`` optionally receives the same union for finalizer guards.
    """
    with destination_pacing.connect_readonly(db_path) as conn:
        conn.execute("BEGIN")
        keys = canonical_account_keys(conn)
        aliases: dict[str, list[str]] = {}
        for ref, key in keys.items():
            aliases.setdefault(key, []).append(ref)
        grouped = destination_pacing.booked_slots(conn, aliases, now=now)
    if account_keys is not None:
        account_keys.update(keys)
    booked: dict[int, list[str]] = {}
    for ref, key in keys.items():
        identifier = structured_account_id(ref)
        if identifier is not None:
            booked[identifier] = grouped.setdefault(key, [])
    return booked


def _next_free_slot(
    account_id: int,
    base_time: datetime | None,
    offset_index: int,
    booked: dict[int, list[str]],
    *,
    min_gap_minutes: int | None = None,
    max_per_day: int | None = None,
    now_fn: Callable | None = None,
    not_before: datetime | None = None,
) -> datetime:
    """Safe UTC slot for scheduled AND publish-now work at this destination.

    Requests may only tighten the configured daily cap/gap. The daily budget
    uses the operator timezone; corrupt calendars or an exhausted finite horizon
    refuse allocation. The existing row-ID interface retains shared alias lists.
    """
    return destination_pacing.allocate_slot(
        booked.setdefault(int(account_id), []), base_time, offset_index,
        configured_gap=MIN_GAP_MINUTES, configured_cap=MAX_POSTS_PER_ACCOUNT_PER_DAY,
        requested_gap=min_gap_minutes, requested_cap=max_per_day, stagger_minutes=STAGGER_MINUTES,
        operator_timezone=_operator_timezone(), now_fn=now_fn, not_before=not_before,
    )


def _build_payload(
    campaign, post, draft, artifact_payloads, platform,
    *,
    tiktok_direct_post: bool = False,
    tiktok_post_settings: dict | None = None,
):
    payload = {
        "campaignId": campaign.id,
        "campaignPostId": post.id,
        "platform": platform,
        "draft": draft,
        "message": draft.get("message", ""),
        "sheetRow": {},
        "artifacts": artifact_payloads,
    }
    if platform == "tiktok":
        # publish_tiktok_sync reads payload.tiktokDirectPost first, then
        # falls back to account.config.publishMode. The explicit per-publish
        # toggle (with confirmation modal) is what TikTok review requires.
        payload["tiktokDirectPost"] = tiktok_direct_post
        # Per-post TikTok settings from the frontend (privacy, interactions,
        # content disclosure). These override account-level defaults.
        if tiktok_post_settings:
            payload["tiktokPostSettings"] = tiktok_post_settings
    return payload


def _enqueue_post(platform, payload, targets, campaign, post, job_to_payload, db_path):
    metadata = campaign.metadata or {}
    lease = metadata.get(campaign_store.PREP_LEASE_KEY) or {}
    prep_fence = ((str(lease.get("owner") or ""), int(metadata.get(campaign_store.PREP_ATTEMPTS_KEY) or 0))
                  if lease else None)
    with campaign_store._connect(db_path) as conn:
        row = conn.execute("SELECT workspace_id FROM campaigns WHERE id=?", (campaign.id,)).fetchone()
        workspace_id = row[0] if row else None
    job = job_runtime.enqueue_job(
        job_runtime.JobSpec(
            platform=platform,
            payload=payload,
            targets=targets,
            profile_id=campaign.profile_id,
            idempotency_key=f"publish-center-campaign-{campaign.id}-post-{post.id}",
        ),
        db_path=db_path,
        workspace_id=workspace_id,
        campaign_post_id=post.id,
        prep_fence=prep_fence,
    )
    return job_to_payload(job)


def _load_media_group_files_via_callback(media_group_id: int, *, db_path: Path) -> list[dict]:
    import sqlite3
    with _db_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                mgi.id,
                mgi.media_group_id,
                mgi.file_record_id,
                mgi.role,
                mgi.sort_order,
                fr.filename,
                fr.file_path,
                fr.filesize
            FROM media_group_items AS mgi
            JOIN file_records AS fr ON fr.id = mgi.file_record_id
            WHERE mgi.media_group_id = ?
            ORDER BY mgi.sort_order, mgi.id
            """,
            (media_group_id,),
        ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]
