"""Async campaign prep runner.

This module owns the "second half" of a Publish Center submission: given a
campaign that the HTTP request already created in ``preparing`` state - and
whose submit payload was persisted in ``campaigns.metadata_json`` - it runs
the slow artifact preparation (ffmpeg watermark/intro/outro/screenshots) and
*then* the per-account draft generation + job enqueue.

That ordering is the whole reason this lives off the request path. Draft
generation consumes the ``media_context`` returned by artifact prep, so it
cannot be scheduled before the artifacts exist. When a single HTTP request did
both it routinely exceeded 100 s and died at Cloudflare's 524; an off-request
worker picks the campaign up from the prep queue instead (see
``myUtils.worker`` / ``myUtils.campaigns``).

The logic here mirrors the per-profile block of
``publish_orchestrator.submit_publish``. The intended follow-up is to make the
orchestrator call :func:`finalize_campaign` directly so the two cannot drift;
that requires a small edit to the currently-held ``publish_orchestrator.py``
and the exact diff is recorded in ``logs/async-prep-queue-notes.md``. Until
then this module reuses the orchestrator's existing module-level helpers
(``_request_data_for_options``, ``_build_payload``, ``_enqueue_post``, ...) so
only the orchestration loop itself is mirrored, not the leaf logic.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from myUtils import campaigns as campaign_store
from myUtils import content_rating
from myUtils import content_rules
from myUtils import media_groups as media_group_store
from myUtils import platform_capabilities
from myUtils import profiles as profile_registry
from myUtils import publish_orchestrator


def _resolve_campaign_accounts(
    campaign: campaign_store.Campaign, *, db_path: Path
) -> list[profile_registry.Account]:
    """Enabled accounts for a campaign, honouring its selected id set.

    The submit request already dropped accounts that were incompatible with the
    selected media (content rating, YouTube-requires-video, ...) and persisted
    only the survivors in ``selected_account_ids``. Re-resolving through the
    same filter keeps a since-disabled account out of the run.
    """
    return publish_orchestrator._resolve_accounts(
        campaign.profile_id, campaign.selected_account_ids, db_path=db_path
    )


def finalize_campaign(
    campaign: campaign_store.Campaign,
    *,
    profile: profile_registry.Profile,
    accounts: list[profile_registry.Account],
    media_context: dict,
    media_files: list[dict],
    request_data: dict,
    options: dict,
    account_drafts: dict,
    tiktok_post_settings: dict,
    db_path: Path,
    artifact_payloads_for_platform: Callable,
    generate_account_draft: Callable,
    artifact_part_groups_for_platform: Callable | None = None,
    job_to_payload: Callable,
    base_time: datetime | None = None,
) -> dict:
    """Generate drafts from *already prepared* artifacts and enqueue posts.

    Returns ``{"jobs": [...], "skipped": [...]}``. The caller owns the final
    campaign status transition (publishing when jobs were queued, otherwise
    needs_review) so the same helper can serve both the synchronous submit
    path and the async worker.
    """
    if not accounts:
        return {"jobs": [], "skipped": []}

    profile_id = profile.id
    media_group = media_group_store.get_media_group(
        campaign.media_group_id, db_path=db_path
    )
    publishable_files = publish_orchestrator._files_with_roles(media_files)

    queued_jobs: list[dict] = []
    skipped: list[dict] = []
    stagger_offset = 0
    booked_slots = publish_orchestrator._load_booked_slots(db_path)
    if base_time is None:
        base_time = _base_time(campaign.metadata or {})
    link_in_first_comment = bool((options or {}).get("linkInFirstComment"))
    # Publish publicly by default; the frontend toggle may request a draft.
    tiktok_direct_post = bool((options or {}).get("tiktokDirectPost", True))

    artifacts = [
        artifact.to_dict()
        for artifact in campaign_store.list_campaign_artifacts(
            campaign.id, db_path=db_path
        )
    ]

    grouped_accounts: dict[str, list[profile_registry.Account]] = {}
    for account in accounts:
        grouped_accounts.setdefault(account.platform, []).append(account)

    for platform, platform_accounts in grouped_accounts.items():
        supports_first_comment = platform_capabilities.platform_supports_first_comment(platform)
        supports_multi_media = platform_capabilities.platform_supports_multi_media(platform)

        for account in platform_accounts:
            # Cross-job duplicate guard, identical to submit_publish.
            if publish_orchestrator._already_queued_for_media(
                account.id, media_group.id, db_path=db_path
            ):
                skipped.append({
                    "profileId": profile_id,
                    "accountId": account.id,
                    "accountName": account.nickname or account.account_name,
                    "platform": account.platform,
                    "reason": "this media is already queued for this account",
                })
                continue

            draft_override = account_drafts.get(str(account.id)) or account_drafts.get(account.id)
            if isinstance(draft_override, dict) and draft_override.get("message"):
                draft = content_rules.normalize_draft_fields(dict(draft_override))
            else:
                try:
                    request_for_account = dict(request_data)
                    effective_language = str(
                        (account.config or {}).get("audience_language")
                        or (account.config or {}).get("audienceLanguage")
                        or (profile.settings or {}).get("default_language")
                        or (profile.settings or {}).get("defaultLanguage")
                        or ""
                    ).strip()
                    if platform == "twitter" and len(
                        effective_language.replace(",", " ").replace("+", " ").split()
                    ) > 1:
                        raise ValueError(
                            "X requires one language per account; use distinct "
                            "account settings instead of a bilingual language list"
                        )
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
                    if content_rules.is_usable_copy(request_data.get("notes") or ""):
                        draft = {
                            "message": str(request_data.get("notes") or "").strip(),
                            "hashtags": [],
                            "firstComment": "",
                            "error": str(exc),
                        }
                    else:
                        skipped.append({
                            "profileId": profile_id,
                            "accountId": account.id,
                            "reason": f"draft generation failed and no usable brief: {exc}",
                        })
                        continue

            if not link_in_first_comment or not supports_first_comment:
                if draft.get("firstComment") and not supports_first_comment:
                    message = (draft.get("message") or "").rstrip()
                    comment = (draft.get("firstComment") or "").strip()
                    if comment and comment not in message:
                        draft["message"] = (message + "\n\n" + comment).strip()
                    draft["firstComment"] = ""
                elif not link_in_first_comment:
                    draft["firstComment"] = ""

            if supports_multi_media or len(publishable_files) <= 1:
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
                    account_tt_settings = (
                        tiktok_post_settings.get(str(account.id))
                        or tiktok_post_settings.get(account.id)
                    )
                    payload = publish_orchestrator._build_payload(
                        campaign, post, draft, part_artifacts, platform,
                        tiktok_direct_post=tiktok_direct_post,
                        tiktok_post_settings=(
                            account_tt_settings
                            if isinstance(account_tt_settings, dict)
                            else None
                        ),
                    )
                    if len(part_groups) > 1:
                        payload["partIndex"] = part_index
                        payload["partCount"] = len(part_groups)
                    targets = [
                        (
                            f"account:{account.id}",
                            f"campaign_post:{post.id}",
                            publish_orchestrator._next_free_slot(
                                account.id, _base_time(campaign.metadata or {}),
                                stagger_offset, booked_slots,
                            ),
                        )
                    ]
                    stagger_offset += 1
                    queued_jobs.append(
                        publish_orchestrator._enqueue_post(
                            platform, payload, targets, campaign, post,
                            job_to_payload, db_path,
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
                    account_tt_settings = (
                        tiktok_post_settings.get(str(account.id))
                        or tiktok_post_settings.get(account.id)
                    )
                    payload = publish_orchestrator._build_payload(
                        campaign, post, draft,
                        artifact_payloads_for_platform(artifacts, platform),
                        platform,
                        tiktok_direct_post=tiktok_direct_post,
                        tiktok_post_settings=(
                            account_tt_settings
                            if isinstance(account_tt_settings, dict)
                            else None
                        ),
                    )
                    payload["fileRecordIds"] = [single_id]
                    targets = [
                        (
                            f"account:{account.id}",
                            f"campaign_post:{post.id}",
                            publish_orchestrator._next_free_slot(
                                account.id, _base_time(campaign.metadata or {}),
                                stagger_offset, booked_slots,
                            ),
                        )
                    ]
                    stagger_offset += 1
                    queued_jobs.append(
                        publish_orchestrator._enqueue_post(
                            platform, payload, targets, campaign, post,
                            job_to_payload, db_path,
                        )
                    )

    return {"jobs": queued_jobs, "skipped": skipped}


def _base_time(metadata: dict) -> datetime | None:
    """Resolve the persisted schedule back into a base time.

    The submit payload stores the raw ``schedule`` dict (as received from the
    client), so the same ``_resolve_base_time`` used on the request path can
    reconstruct it. A campaign with no persisted schedule publishes now.
    """
    request = metadata.get(campaign_store.PREP_REQUEST_KEY)
    if not isinstance(request, dict):
        return None
    return publish_orchestrator._resolve_base_time(request.get("schedule"))


def run_campaign_prep(
    campaign: campaign_store.Campaign,
    *,
    db_path: Path,
    prepare_artifacts: Callable,
    generate_account_draft: Callable,
    artifact_payloads_for_platform: Callable,
    artifact_part_groups_for_platform: Callable | None = None,
    job_to_payload: Callable,
) -> dict:
    """Run artifact prep then finalize for a single claimed campaign.

    Raises on any prep failure; the worker is responsible for recording
    ``needs_review``. Returns the same ``{"jobs", "skipped"}`` envelope as
    :func:`finalize_campaign`.
    """
    request = campaign_store.get_prep_request(campaign)
    if request is None:
        raise ValueError(
            f"campaign {campaign.id} has no persisted prep request "
            f"({campaign_store.PREP_REQUEST_KEY!r}); submit it through the "
            "publish-center async path"
        )

    options = request.get("options") or {}
    brief = str(request.get("brief") or "")
    account_drafts = request.get("accountDrafts") or {}
    tiktok_post_settings = request.get("tiktokPostSettings") or {}

    profile = profile_registry.get_profile(campaign.profile_id, db_path=db_path)
    accounts = _resolve_campaign_accounts(campaign, db_path=db_path)
    if not accounts:
        return {"jobs": [], "skipped": [{"profileId": profile.id, "reason": "no_enabled_accounts"}]}

    request_data = publish_orchestrator._request_data_for_options(
        brief=brief, options=options, profile=profile
    )
    media_files = publish_orchestrator._load_media_group_files_via_callback(
        campaign.media_group_id, db_path=db_path
    )

    media_context = prepare_artifacts(
        campaign.id,
        profile,
        media_files,
        request_data,
        selected_platforms={account.platform for account in accounts},
        db_path=db_path,
    )

    return finalize_campaign(
        campaign,
        profile=profile,
        accounts=accounts,
        media_context=media_context,
        media_files=media_files,
        request_data=request_data,
        options=options,
        account_drafts=account_drafts,
        tiktok_post_settings=tiktok_post_settings,
        db_path=db_path,
        artifact_payloads_for_platform=artifact_payloads_for_platform,
        generate_account_draft=generate_account_draft,
        artifact_part_groups_for_platform=artifact_part_groups_for_platform,
        job_to_payload=job_to_payload,
    )


def submit_publish_async(
    *,
    profile_ids: list[int],
    selected_account_ids: list[int] | None,
    media_file_paths: list[str],
    brief: str,
    options: dict,
    schedule: dict | None,
    account_drafts: dict | None,
    tiktok_post_settings: dict | None = None,
    db_path: Path,
    ensure_file_record_for_path: Callable,
    source: str = "publish-center",
    now_fn: Callable = datetime.now,
) -> publish_orchestrator.SubmitResult:
    """Materialise the submit's campaigns in ``preparing``, then stop.

    This is the asynchronous twin of ``publish_orchestrator.submit_publish``.
    It performs every step that must happen inside the request - register the
    file records, create the shared media group, resolve/filter the accounts,
    create one campaign per profile in ``preparing`` and persist the raw submit
    payload under ``PREP_REQUEST_KEY`` - and then returns with ``jobs=[]``.

    The heavy second half (artifact prep + draft generation + job enqueue) runs
    off-request in ``myUtils.worker`` via :func:`run_campaign_prep`, which reads
    the persisted request back. That is what keeps a 900 s ffmpeg prep from
    blowing Cloudflare's ~100 s origin cap.

    The two entry points must agree on the pre-campaign account filtering. The
    exact orchestrator-side diff that collapses them into one function is
    recorded in ``logs/async-prep-wire-notes.md`` (``publish_orchestrator.py``
    is a held file for this session).
    """
    if not profile_ids:
        raise ValueError("At least one profile must be selected")
    if not media_file_paths:
        raise ValueError("At least one media file is required")

    account_drafts = account_drafts or {}
    tiktok_post_settings = tiktok_post_settings or {}
    options = options or {}

    # 1. Materialise file_records + one media group shared by every profile.
    #    Identical to the synchronous path so the worker's
    #    ``_load_media_group_files_via_callback`` finds the same rows.
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
                if publish_orchestrator._media_role_for_path(path)
                == media_group_store.ROLE_VIDEO
            ),
            None,
        ),
        db_path=db_path,
    )
    for index, (record_id, path) in enumerate(zip(file_record_ids, media_file_paths)):
        media_group_store.add_media_group_item(
            media_group.id,
            record_id,
            role=publish_orchestrator._media_role_for_path(path),
            sort_order=index,
            db_path=db_path,
        )

    campaign_ids: list[int] = []
    skipped: list[dict] = []

    # Content rating is derived from the filenames exactly as on the sync path;
    # only the survivors are persisted in ``selected_account_ids`` so the worker
    # cannot resurrect a filtered account.
    rating = content_rating.rating_for_media(
        media_file_paths, explicit=options.get("sfwFlag")
    )
    media_roles = {
        publish_orchestrator._media_role_for_path(path) for path in media_file_paths
    }
    has_video = media_group_store.ROLE_VIDEO in media_roles
    has_image = media_group_store.ROLE_IMAGE in media_roles
    if not has_video and not has_image:
        raise ValueError("Selected media contains no supported image or video files")

    for profile_id in profile_ids:
        profile = profile_registry.get_profile(int(profile_id), db_path=db_path)
        accounts = publish_orchestrator._resolve_accounts(
            profile.id, selected_account_ids, db_path=db_path
        )
        if not accounts:
            skipped.append({"profileId": profile.id, "reason": "no_enabled_accounts"})
            continue
        try:
            accounts = content_rating.restrict_accounts(accounts, rating)
        except ValueError:
            skipped.append(
                {"profileId": profile.id, "reason": "nsfw_no_adult_safe_account"}
            )
            continue

        compatible_accounts: list[profile_registry.Account] = []
        for account in accounts:
            if account.platform == "youtube" and not has_video:
                skipped.append({
                    "profileId": profile.id,
                    "accountId": account.id,
                    "accountName": account.nickname or account.account_name,
                    "platform": account.platform,
                    "reason": "YouTube publishing requires video media; this selection contains no video.",
                })
            elif (
                account.platform == "tiktok"
                and not has_video
                and not bool(options.get("tiktokDirectPost"))
            ):
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

        request_data = publish_orchestrator._request_data_for_options(
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
                "requestedPlatforms": sorted(
                    {account.platform for account in accounts}
                ),
                "source": source,
                # The worker cannot recover these from the request context, so
                # the whole submit payload is persisted here. ``run_campaign_prep``
                # reads it back via ``get_prep_request``; ``_base_time`` reads
                # the ``schedule`` key.
                campaign_store.PREP_REQUEST_KEY: {
                    "brief": brief,
                    "options": options,
                    "schedule": schedule,
                    "accountDrafts": account_drafts,
                    "tiktokPostSettings": tiktok_post_settings,
                    "mediaFilePaths": [str(path) for path in media_file_paths],
                },
            },
            db_path=db_path,
        )
        campaign_ids.append(campaign.id)

    return publish_orchestrator.SubmitResult(
        campaign_ids=campaign_ids,
        jobs=[],
        skipped=skipped,
    )


def make_default_prep_runner() -> Callable[[campaign_store.Campaign, Path], dict]:
    """Build the production prep runner used by the worker process.

    The heavy callbacks live in ``sau_backend``; they are imported lazily so
    importing this module (and the worker) never drags in the Flask app on its
    own. In the in-process worker the module is already loaded, so this is a
    no-op import; the standalone worker only pays for it when a campaign
    actually needs preparing.
    """

    def _runner(campaign: campaign_store.Campaign, db_path: Path) -> dict:
        from sau_backend import (
            _artifact_part_groups_for_platform,
            _artifact_payloads_for_platform,
            _generate_account_draft,
            _job_to_payload,
            _notify_tg_review,
            _prepare_campaign_media_artifacts,
        )

        result = run_campaign_prep(
            campaign,
            db_path=db_path,
            prepare_artifacts=_prepare_campaign_media_artifacts,
            generate_account_draft=_generate_account_draft,
            artifact_payloads_for_platform=_artifact_payloads_for_platform,
            artifact_part_groups_for_platform=_artifact_part_groups_for_platform,
            job_to_payload=_job_to_payload,
        )
        # Async-prep Telegram review cards. The submit request returns before
        # jobs exist, so it could not build the approve/edit cards there; now
        # that finalize has generated the drafts and enqueued the jobs, the
        # cards can be composed from the real job rows. ``_notify_tg_review``
        # is itself fail-soft and does nothing when jobs are empty.
        try:
            jobs = (result or {}).get("jobs") or []
            if jobs:
                _notify_tg_review(jobs=jobs, db_path=db_path)
        except Exception:  # noqa: BLE001 - a card must never fail a prep
            import logging

            logging.getLogger(__name__).debug(
                "async prep: tg review notify skipped", exc_info=True
            )
        return result

    return _runner
