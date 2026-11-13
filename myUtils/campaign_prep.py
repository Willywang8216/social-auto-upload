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

Synchronous and asynchronous paths share ``publication_groups.finalize_campaign``;
only artifact preparation and the caller-owned campaign completion differ. Slow
preparation/draft/provider checks never hold the destination slot reservation.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from myUtils import campaigns as campaign_store
from myUtils import content_rating
from myUtils import media_groups as media_group_store
from myUtils import profiles as profile_registry
from myUtils import publish_orchestrator
from myUtils.publication_groups import finalize_campaign


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
    from myUtils.preparation_context import configure_request
    if options.get("_backupCompatiblePlatforms") is not None:
        request_data["_backupCompatiblePlatforms"] = options["_backupCompatiblePlatforms"]
    request_data, _backup_contexts = configure_request(
        accounts, {account.platform for account in accounts}, request_data,
        settings=profile.settings,
        has_video=any(item.get("role") == media_group_store.ROLE_VIDEO for item in media_files),
        db_path=db_path,
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
    """Build the worker runner using Flask-free shared application services."""

    def _runner(campaign: campaign_store.Campaign, db_path: Path) -> dict:
        from myUtils.publish_services import (
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
