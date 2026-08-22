"""Publish MCP tools — submit / preview / regenerate.

These are the heavy ones. They delegate to ``myUtils.publish_orchestrator``
for the actual fan-out + enqueue logic and reuse the heavy callbacks that
live in ``sau_backend``:

* ``_prepare_campaign_media_artifacts`` — watermark / intro / outro /
  screenshots / remote hosting.
* ``_generate_account_draft`` — per-account LLM-generated copy.
* ``_ensure_file_record_for_path`` — register the file under videoFile/.
* ``_artifact_payloads_for_platform`` — platform-specific artifact
  selection (TikTok gets the raw remote / local artefact; other platforms
  get the watermarked preview).
* ``_job_to_payload`` — jobs.Job -> dict envelope.

``preview`` and ``regenerate`` reuse ``_build_preview_media_context`` and
``_preview_media_group_stub`` so the LLM-facing inputs match the
``/publish-center/preview`` and ``/publish-center/regenerate`` HTTP routes
byte-for-byte.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from mcp_server._shared import error_payload, resolve_db_path


def register(mcp: FastMCP) -> None:
    from myUtils import publish_orchestrator

    @mcp.tool(
        name="publish_submit",
        description=(
            "Submit a Publish Center request: fan out across the given "
            "`profile_ids`, prepare per-account drafts, and enqueue one "
            "job per (account, file) pair. Returns `{campaignIds, jobs, "
            "skipped}`. Pass `schedule` (`{publishNow: true}` for immediate, "
            "or `{startAt: 'YYYY-MM-DDTHH:MM:SSZ'}` for deferred) to schedule "
            "rather than publish immediately."
        ),
    )
    def publish_submit(
        profile_ids: list[int],
        media_file_paths: list[str],
        brief: str,
        selected_account_ids: list[int] | None = None,
        options: dict[str, Any] | None = None,
        schedule: dict[str, Any] | None = None,
        account_drafts: dict[str, dict[str, Any]] | None = None,
        tiktok_post_settings: dict[str, dict[str, Any]] | None = None,
        db_path: str | None = None,
    ) -> dict[str, Any]:
        from sau_backend import (
            _artifact_payloads_for_platform,
            _ensure_file_record_for_path,
            _generate_account_draft,
            _job_to_payload,
            _prepare_campaign_media_artifacts,
        )

        try:
            result = publish_orchestrator.submit_publish(
                profile_ids=[int(p) for p in profile_ids],
                selected_account_ids=[int(v) for v in selected_account_ids]
                if selected_account_ids
                else None,
                media_file_paths=[str(p) for p in media_file_paths],
                brief=str(brief or ""),
                options=options or {},
                schedule=schedule,
                account_drafts=account_drafts or {},
                tiktok_post_settings=tiktok_post_settings or {},
                db_path=resolve_db_path(db_path),
                prepare_artifacts=_prepare_campaign_media_artifacts,
                generate_account_draft=_generate_account_draft,
                ensure_file_record_for_path=_ensure_file_record_for_path,
                artifact_payloads_for_platform=_artifact_payloads_for_platform,
                job_to_payload=_job_to_payload,
            )
            return {
                "campaignIds": result.campaign_ids,
                "jobs": result.jobs,
                "skipped": result.skipped,
            }
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="publish_preview",
        description=(
            "Dry-run a publish submission: build per-account drafts "
            "without enqueuing jobs. Same response shape as "
            "``/publish-center/preview`` — one entry per profile with "
            "the accounts and the generated drafts."
        ),
    )
    def publish_preview(
        profile_ids: list[int],
        brief: str,
        options: dict[str, Any] | None = None,
        selected_account_ids: list[int] | None = None,
        db_path: str | None = None,
    ) -> dict[str, Any]:
        from sau_backend import (
            _build_preview_media_context,
            _generate_account_draft,
            _preview_media_group_stub,
            _publish_center_load_accounts,
            _publish_center_load_profile,
            _publish_center_preview_payload,
        )
        from myUtils import profiles as profile_registry

        resolved = resolve_db_path(db_path)
        try:
            results: list[dict[str, Any]] = []
            media_group_stub = _preview_media_group_stub(
                name=(brief or "")[:50] or "publish-center-preview"
            )
            media_context = _build_preview_media_context(brief or "")
            options = options or {}
            for raw_pid in profile_ids:
                profile = _publish_center_load_profile(int(raw_pid), db_path=resolved)
                accounts = _publish_center_load_accounts(
                    profile.id,
                    [int(v) for v in selected_account_ids] if selected_account_ids else None,
                    db_path=resolved,
                )
                request_data = publish_orchestrator._request_data_for_options(
                    brief=brief or "", options=options, profile=profile
                )
                drafts_by_account: dict[int, dict[str, Any]] = {}
                cached_per_platform: dict[str, dict[str, Any]] = {}
                for account in accounts:
                    cached = cached_per_platform.get(account.platform)
                    if cached is None:
                        try:
                            cached = _generate_account_draft(
                                account, profile, media_group_stub, request_data, media_context
                            )
                        except Exception as exc:  # noqa: BLE001
                            cached = {
                                "message": (brief or "").strip()[:1000],
                                "hashtags": [],
                                "firstComment": "",
                                "charCount": len((brief or "").strip()),
                                "error": str(exc),
                            }
                        cached_per_platform[account.platform] = cached
                    drafts_by_account[account.id] = dict(cached)
                results.append(
                    _publish_center_preview_payload(
                        profile=profile, accounts=accounts, drafts_by_account=drafts_by_account
                    )
                )
            return {"profiles": results}
        except LookupError as exc:
            return error_payload(exc)
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)

    @mcp.tool(
        name="publish_regenerate",
        description=(
            "Re-run the LLM draft for a single (profile, account) pair. "
            "Mirrors ``/publish-center/regenerate`` and forces the LLM "
            "to produce a fresh variant."
        ),
    )
    def publish_regenerate(
        profile_id: int,
        account_id: int,
        brief: str,
        options: dict[str, Any] | None = None,
        db_path: str | None = None,
    ) -> dict[str, Any]:
        from sau_backend import (
            _build_preview_media_context,
            _generate_account_draft,
            _preview_media_group_stub,
            _publish_center_load_profile,
        )
        from myUtils import profiles as profile_registry

        resolved = resolve_db_path(db_path)
        try:
            profile = _publish_center_load_profile(int(profile_id), db_path=resolved)
            account = profile_registry.get_account(int(account_id), db_path=resolved)
            if account.profile_id != profile.id:
                raise ValueError("Account does not belong to the specified profile")
            request_data = publish_orchestrator._request_data_for_options(
                brief=brief or "", options=options or {}, profile=profile
            )
            media_group_stub = _preview_media_group_stub(
                name=(brief or "")[:50] or "publish-center-regenerate"
            )
            media_context = _build_preview_media_context(brief or "")
            draft = _generate_account_draft(
                account, profile, media_group_stub, request_data, media_context, regenerate=True
            )
            return {"draft": draft}
        except LookupError as exc:
            return error_payload(exc)
        except Exception as exc:  # noqa: BLE001
            return error_payload(exc)
