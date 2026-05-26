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
  stagger their ``schedule_at`` 5 minutes apart;
* deterministic base-time scheduling so a deferred "Publish at" timestamp
  applies uniformly across every target the user just queued.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from myUtils import campaigns as campaign_store
from myUtils import jobs as job_runtime
from myUtils import media_groups as media_group_store
from myUtils import platform_capabilities
from myUtils import profiles as profile_registry


STAGGER_MINUTES = 5


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


def _resolve_base_time(schedule: dict | None, *, now_fn=datetime.now) -> datetime | None:
    """Resolve the user-requested base publish time.

    Returns ``None`` when the user asked for an immediate publish so the
    worker can pick targets up as soon as it sees them. Otherwise returns
    a timezone-naive UTC datetime so all downstream stagger arithmetic is
    consistent with what ``publish_job_targets.schedule_at`` expects.
    """
    if not schedule or not isinstance(schedule, dict):
        return None
    if schedule.get("publishNow"):
        return None
    candidate = _to_datetime(schedule.get("startAt") or schedule.get("scheduledAt"))
    if candidate is None:
        return None
    if candidate.tzinfo is not None:
        candidate = candidate.astimezone(timezone.utc).replace(tzinfo=None)
    return candidate


def _media_role_for_path(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    image_suffixes = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    if suffix in image_suffixes:
        return media_group_store.ROLE_IMAGE
    return media_group_store.ROLE_VIDEO


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
    """
    options = options or {}
    profile_settings = profile.settings or {}
    request_data: dict = {
        "notes": brief or "",
        "title": (options.get("title") or "").strip(),
        "transcribe": bool(options.get("transcribe", False)),
        "useLlm": bool(options.get("useLlm", True)),
    }

    if options.get("watermark"):
        request_data["watermark"] = options.get("watermarkOverride") or profile_settings.get("watermark")
    else:
        # An empty string short-circuits _derive_watermark_spec's defaults
        # so the prep pipeline skips the watermark step entirely.
        request_data["watermark"] = ""

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

    # Default to local-only artifacts so the Publish Center flow does
    # not silently depend on a configured rclone remote. Existing
    # /campaigns/prepare callers keep their env-driven default.
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


def _files_with_roles(media_files: list[dict]) -> list[dict]:
    """Pick out only ``video`` and ``image`` items so intro/outro auxiliary
    files don't accidentally end up in a single-media split.
    """
    return [
        item
        for item in media_files
        if item.get("role") in {media_group_store.ROLE_VIDEO, media_group_store.ROLE_IMAGE}
    ]


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

    for profile_id in profile_ids:
        profile = profile_registry.get_profile(int(profile_id), db_path=db_path)
        accounts = _resolve_accounts(profile.id, selected_account_ids, db_path=db_path)
        if not accounts:
            skipped.append({"profileId": profile.id, "reason": "no_enabled_accounts"})
            continue

        request_data = _request_data_for_options(
            brief=brief, options=options, profile=profile
        )
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

        # group accounts by platform so each platform gets its own
        # campaign_posts/job spec
        grouped_accounts: dict[str, list[profile_registry.Account]] = {}
        for account in accounts:
            grouped_accounts.setdefault(account.platform, []).append(account)

        # cache for per-platform fallback drafts so we don't re-call the
        # LLM when several accounts on the same platform share a draft
        platform_draft_cache: dict[str, dict] = {}

        link_in_first_comment = bool((options or {}).get("linkInFirstComment"))
        tiktok_direct_post = bool((options or {}).get("tiktokDirectPost"))

        artifacts = [
            artifact.to_dict()
            for artifact in campaign_store.list_campaign_artifacts(campaign.id, db_path=db_path)
        ]

        for platform, platform_accounts in grouped_accounts.items():
            supports_first_comment = platform_capabilities.platform_supports_first_comment(platform)
            supports_multi_media = platform_capabilities.platform_supports_multi_media(platform)

            for account in platform_accounts:
                draft_override = account_drafts.get(str(account.id)) or account_drafts.get(account.id)
                if isinstance(draft_override, dict) and draft_override.get("message"):
                    draft = dict(draft_override)
                else:
                    cached = platform_draft_cache.get(platform)
                    if cached is None:
                        try:
                            cached = generate_account_draft(
                                account, profile, media_group, request_data, media_context
                            )
                        except Exception as exc:  # noqa: BLE001
                            cached = {
                                "message": (brief or "").strip()[:280],
                                "hashtags": [],
                                "firstComment": "",
                                "error": str(exc),
                            }
                        platform_draft_cache[platform] = cached
                    draft = dict(cached)

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

                if supports_multi_media or len(publishable_files) <= 1:
                    post = campaign_store.add_campaign_post(
                        campaign.id,
                        platform,
                        account_ids=[account.id],
                        draft=draft,
                        status=campaign_store.CAMPAIGN_POST_READY,
                        db_path=db_path,
                    )
                    account_tt_settings = tiktok_post_settings.get(str(account.id)) or tiktok_post_settings.get(account.id)
                    payload = _build_payload(
                        campaign, post, draft, artifacts, platform,
                        artifact_payloads_for_platform,
                        tiktok_direct_post=tiktok_direct_post,
                        tiktok_post_settings=account_tt_settings if isinstance(account_tt_settings, dict) else None,
                    )
                    targets = [
                        (
                            f"account:{account.id}",
                            f"campaign_post:{post.id}",
                            _compute_schedule_at(base_time, stagger_offset),
                        )
                    ]
                    stagger_offset += 1
                    queued_jobs.append(
                        _enqueue_post(
                            platform, payload, targets, campaign, post, job_to_payload, db_path
                        )
                    )
                else:
                    for media_file in publishable_files:
                        single_id = int(media_file["file_record_id"])
                        post = campaign_store.add_campaign_post(
                            campaign.id,
                            platform,
                            account_ids=[account.id],
                            draft=draft,
                            file_record_ids=[single_id],
                            status=campaign_store.CAMPAIGN_POST_READY,
                            db_path=db_path,
                        )
                        account_tt_settings = tiktok_post_settings.get(str(account.id)) or tiktok_post_settings.get(account.id)
                        payload = _build_payload(
                            campaign, post, draft, artifacts, platform,
                            artifact_payloads_for_platform,
                            tiktok_direct_post=tiktok_direct_post,
                            tiktok_post_settings=account_tt_settings if isinstance(account_tt_settings, dict) else None,
                        )
                        payload["fileRecordIds"] = [single_id]
                        targets = [
                            (
                                f"account:{account.id}",
                                f"campaign_post:{post.id}",
                                _compute_schedule_at(base_time, stagger_offset),
                            )
                        ]
                        stagger_offset += 1
                        queued_jobs.append(
                            _enqueue_post(
                                platform, payload, targets, campaign, post, job_to_payload, db_path
                            )
                        )

        campaign_store.update_campaign(
            campaign.id,
            status=campaign_store.CAMPAIGN_PUBLISHING if queued_jobs else campaign_store.CAMPAIGN_NEEDS_REVIEW,
            prepared_at=now_fn().isoformat(timespec="seconds"),
            published_at=now_fn().isoformat(timespec="seconds") if queued_jobs else None,
            last_error=None if queued_jobs else "No publishable posts queued",
            db_path=db_path,
        )

    return SubmitResult(campaign_ids=campaign_ids, jobs=queued_jobs, skipped=skipped)


def _compute_schedule_at(base_time: datetime | None, offset_index: int) -> datetime | None:
    if base_time is None and offset_index == 0:
        return None
    anchor = base_time or datetime.now(tz=timezone.utc).replace(tzinfo=None)
    return anchor + timedelta(minutes=STAGGER_MINUTES * offset_index)


def _build_payload(
    campaign, post, draft, artifacts, platform, artifact_payloads_for_platform,
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
        "artifacts": artifact_payloads_for_platform(artifacts, platform),
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
    job = job_runtime.enqueue_job(
        job_runtime.JobSpec(
            platform=platform,
            payload=payload,
            targets=targets,
            profile_id=campaign.profile_id,
            idempotency_key=f"publish-center-campaign-{campaign.id}-post-{post.id}",
        ),
        db_path=db_path,
    )
    campaign_store.update_campaign_post(
        post.id,
        status=campaign_store.CAMPAIGN_POST_QUEUED,
        last_published_job_id=job.id,
        db_path=db_path,
    )
    return job_to_payload(job)


def _load_media_group_files_via_callback(media_group_id: int, *, db_path: Path) -> list[dict]:
    import sqlite3
    with sqlite3.connect(db_path) as conn:
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
