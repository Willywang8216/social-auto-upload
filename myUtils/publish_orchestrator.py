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

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from myUtils import campaigns as campaign_store
from myUtils import content_rating
from myUtils import content_rules
from myUtils import jobs as job_runtime
from myUtils import media_groups as media_group_store
from myUtils import platform_capabilities
from myUtils import profiles as profile_registry


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
    booked_slots = _load_booked_slots(db_path)

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

                if supports_multi_media or len(publishable_files) <= 1:
                    # One source video longer than a platform cap becomes
                    # several part-posts; anything within the cap is a single
                    # group, identical to before.
                    if artifact_part_groups_for_platform is not None:
                        part_groups = artifact_part_groups_for_platform(artifacts, platform)
                    else:
                        part_groups = [artifact_payloads_for_platform(artifacts, platform)]
                    for part_index, part_artifacts in enumerate(part_groups, start=1):
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
                            campaign, post, draft, part_artifacts, platform,
                            tiktok_direct_post=tiktok_direct_post,
                            tiktok_post_settings=account_tt_settings if isinstance(account_tt_settings, dict) else None,
                        )
                        if len(part_groups) > 1:
                            payload["partIndex"] = part_index
                            payload["partCount"] = len(part_groups)
                        targets = [
                            (
                                f"account:{account.id}",
                                f"campaign_post:{post.id}",
                                _next_free_slot(account.id, base_time, stagger_offset, booked_slots),
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
                            campaign, post, draft,
                            artifact_payloads_for_platform(artifacts, platform),
                            platform,
                            tiktok_direct_post=tiktok_direct_post,
                            tiktok_post_settings=account_tt_settings if isinstance(account_tt_settings, dict) else None,
                        )
                        payload["fileRecordIds"] = [single_id]
                        targets = [
                            (
                                f"account:{account.id}",
                                f"campaign_post:{post.id}",
                                _next_free_slot(account.id, base_time, stagger_offset, booked_slots),
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


def _load_booked_slots(db_path: Path | str) -> dict[int, list[str]]:
    """Account id -> schedule_at strings already queued for that account.

    Read-only and best-effort: a missing/older schema must never break a
    publish submit, so any failure falls back to an empty booking map.
    """
    booked: dict[int, list[str]] = {}
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT account_ref, schedule_at FROM publish_job_targets "
                "WHERE status IN ('pending', 'retrying') "
                "AND schedule_at IS NOT NULL AND account_ref LIKE 'account:%'"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return booked
    for account_ref, schedule_at in rows:
        try:
            account_id = int(str(account_ref).split(":", 1)[1])
        except (IndexError, ValueError):
            continue
        booked.setdefault(account_id, []).append(str(schedule_at))
    for times in booked.values():
        times.sort()
    return booked


def _next_free_slot(
    account_id: int,
    base_time: datetime | None,
    offset_index: int,
    booked: dict[int, list[str]],
    *,
    min_gap_minutes: int | None = None,
    max_per_day: int | None = None,
) -> datetime | None:
    """A collision-free schedule slot for one target of ``account_id``.

    ``base_time is None`` (publish-now) keeps the historical behaviour: the
    first target is immediate, later ones are nudged by the stagger so a
    single-media fan-out does not fire all at once. A scheduled submit is
    walked forward until it clears every existing booking for the account.
    ``min_gap_minutes`` / ``max_per_day`` override the module defaults (used
    by the recovery tool, which admits a few extra posts a day).
    """
    candidate = _compute_schedule_at(base_time, offset_index)
    if candidate is None or base_time is None:
        return candidate

    gap = timedelta(
        minutes=max(1, min_gap_minutes if min_gap_minutes is not None else MIN_GAP_MINUTES)
    )
    cap = max(
        0,
        max_per_day if max_per_day is not None else MAX_POSTS_PER_ACCOUNT_PER_DAY,
    )
    taken = booked.setdefault(int(account_id), [])
    slot_time = candidate.time()  # keep the intended wall-clock slot when rolling a day
    while True:
        existing_times = [t for t in (_parse_schedule(v) for v in taken) if t is not None]
        same_day = sum(1 for t in existing_times if t.date() == candidate.date())
        conflict = any(
            abs((t - candidate).total_seconds()) < gap.total_seconds()
            for t in existing_times
        )
        if not conflict and not (cap and same_day >= cap):
            break
        if cap and same_day >= cap:
            # The day is full: roll to the same wall-clock slot tomorrow.
            candidate = datetime.combine(candidate.date() + timedelta(days=1), slot_time)
        else:
            candidate = candidate + gap
    taken.append(candidate.isoformat(timespec="seconds"))
    return candidate


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
