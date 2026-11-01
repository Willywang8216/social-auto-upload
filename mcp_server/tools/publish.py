"""Publish MCP tools — submit / preview / regenerate.

Submissions use the Flask-free durable admission service and always return
before media preparation. Reuse ``submission_id`` after a timeout; changing
the intent under that ID is rejected rather than publishing twice.
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
        submission_id: str | None = None,
    ) -> dict[str, Any]:
        from myUtils import publish_services

        try:
            result = publish_services.submit_publish(
                profile_ids=profile_ids,
                selected_account_ids=selected_account_ids,
                media_file_paths=media_file_paths,
                brief=brief or "",
                options=options or {},
                schedule=schedule,
                account_drafts=account_drafts or {},
                tiktok_post_settings=tiktok_post_settings or {},
                db_path=resolve_db_path(db_path),
                submission_id=submission_id,
                source="mcp",
            )
            status = "preparing" if result.campaign_ids else "needs_review"
            return {
                "campaignIds": result.campaign_ids,
                "status": status,
                "jobs": result.jobs,
                "skipped": result.skipped,
                "submissionId": submission_id,
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
        media_file_paths: list[str] | None = None,
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
        from myUtils import content_rating
        from myUtils import profiles as profile_registry

        resolved = resolve_db_path(db_path)
        try:
            results: list[dict[str, Any]] = []
            media_group_stub = _preview_media_group_stub(
                name=(brief or "")[:50] or "publish-center-preview"
            )
            media_context = _build_preview_media_context(brief or "")
            options = options or {}
            
            # Calculate content rating from media files (or default to NSFW if no files provided)
            sfw_flag = options.get("sfwFlag")
            rating = content_rating.rating_for_media(media_file_paths or [], explicit=sfw_flag) if media_file_paths else content_rating.RATING_NSFW
            
            for raw_pid in profile_ids:
                profile = _publish_center_load_profile(int(raw_pid), db_path=resolved)
                all_accounts = _publish_center_load_accounts(
                    profile.id,
                    [int(v) for v in selected_account_ids] if selected_account_ids else None,
                    db_path=resolved,
                )
                
                # Apply content rating restrictions
                try:
                    accounts = content_rating.restrict_accounts(all_accounts, rating)
                except ValueError:
                    # All accounts would be dropped - still show them but mark as dropped
                    accounts = []
                
                # Track which accounts were dropped
                kept_ids = {a.id for a in accounts}
                dropped_accounts = [
                    {"accountId": a.id, "accountName": a.account_name, "platform": a.platform, "reason": "nsfw_platform_restriction"}
                    for a in all_accounts if a.id not in kept_ids
                ]
                
                request_data = publish_orchestrator._request_data_for_options(
                    brief=brief or "", options=options, profile=profile
                )
                drafts_by_account: dict[int, dict[str, Any]] = {}
                for account in accounts:
                    try:
                        draft = _generate_account_draft(
                            account, profile, media_group_stub, request_data, media_context
                        )
                    except Exception as exc:  # noqa: BLE001
                        from sau_backend import _account_audience_language
                        language_required = bool(_account_audience_language(account, profile))
                        draft = {
                            "message": "" if language_required else (brief or "").strip()[:1000],
                            "hashtags": [],
                            "firstComment": "",
                            "charCount": 0 if language_required else len((brief or "").strip()),
                            "error": str(exc),
                        }
                    drafts_by_account[account.id] = dict(draft)
                results.append(
                    _publish_center_preview_payload(
                        profile=profile, accounts=accounts, drafts_by_account=drafts_by_account,
                        content_rating=rating, dropped_accounts=dropped_accounts
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
