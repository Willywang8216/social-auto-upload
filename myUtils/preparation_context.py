"""Fresh, read-only capability checks before immutable media preparation.

These reads never refresh credentials, request upload grants, or publish. They
only tighten the stored/default compatibility profile; an unavailable lookup
cannot turn a stale allowance into unlimited permission. Returned contexts contain
limits and safe diagnostics, not tokens, names, or raw provider responses.
"""
from __future__ import annotations

import copy

from myUtils import backup_preparation, media_compliance, platform_limits, prepared_publishers
from myUtils.secret_redaction import redact_text


class _BoundedHTTP:
    def __init__(self, session):
        self.session = session

    def get(self, url, **kwargs):
        kwargs["timeout"] = 45
        return self.session.get(url, **kwargs)

    def post(self, url, **kwargs):
        kwargs["timeout"] = 45
        return self.session.post(url, **kwargs)


def account_constraints(account, *, session=None) -> dict:
    """Fresh capability caps, never above the existing preparation profile."""
    platform = account.platform
    config = copy.deepcopy(account.config or {})
    baseline = media_compliance.effective_limits(platform, account_config=config)
    if platform not in {"tiktok", "youtube"}:
        return {"video_limits": baseline, "capability_check": "not_required"}
    allowance, check = {}, "conservative_unverified"
    token = prepared_publishers._config_value(config, "accessToken")
    if token:
        http = _BoundedHTTP(prepared_publishers._get_session(session))
        try:
            if platform == "tiktok":
                result = prepared_publishers.query_tiktok_creator_info(config, access_token=token, session=http)
                data = result.get("data", result)
                if isinstance(data, dict) and "max_video_post_duration_sec" in data:
                    allowance = {"creator_info": {"max_video_post_duration_sec": data["max_video_post_duration_sec"]}}
                    check = "fresh_creator"
            else:
                expected = str(config.get("channelId") or "").strip()
                if expected:
                    response = http.get(prepared_publishers.YOUTUBE_CHANNELS_URL,
                        headers={"Authorization": f"Bearer {token}"}, params={"part": "id,status", "mine": "true"})
                    prepared_publishers._raise_for_status(response)
                    body = prepared_publishers._response_payload(response)
                    items = body.get("items")
                    if isinstance(items, list) and len(items) == 1 and items[0].get("id") == expected:
                        status = items[0].get("status") or {}
                        allowance = {"channelStatus": {"longUploadsStatus": status.get("longUploadsStatus")}}
                        check = "fresh_owned_channel"
        except Exception as exc:
            # Do not record provider bodies/URLs/credentials. The publisher's
            # own credential maintenance remains separate from these reads.
            check = f"conservative_unverified:{type(exc).__name__}"
    fresh = media_compliance.effective_limits(platform, account_config=allowance)
    return {"video_limits": media_compliance.merge_limits([baseline, fresh]), "capability_check": check}


def configure_request(accounts, platforms, request_data: dict, *, settings=None, has_video=True):
    """Resolve viable backups and merge all caps before watermark/encoding."""
    from myUtils.publish_orchestrator import (_preparation_account_configs_for_accounts,
        _preparation_limits_for_accounts, _image_preparation_limits_for_accounts)

    request = dict(request_data)
    adapter = backup_preparation.adapter
    contexts, warnings = {}, {}
    for account in accounts:
        if account.platform not in platforms or getattr(account, "enabled", True) in (False, 0):
            continue
        primary = adapter.prefers_sociamonials(account, platform=account.platform)
        if not adapter.is_enabled():
            if primary:
                raise media_compliance.MediaPreparationError("Configured primary backup is disabled or unavailable")
            continue
        mapping = adapter.resolve_mapping(account.id, platform=account.platform, account=account, settings=settings)
        if not mapping:
            continue
        try:
            contexts[account.id] = backup_preparation.resolve_constraints(platform=account.platform,
                account=account, settings=settings)
        except Exception as exc:
            if primary:
                raise media_compliance.MediaPreparationError("Configured primary backup could not be verified") from exc
            warnings[str(account.id)] = redact_text(str(exc))[:200]
    verified_platforms = {account.platform for account in accounts if account.id in contexts}
    backup_platforms = (platform_limits.backup_compatible_platforms(request) | verified_platforms) & set(platforms)
    request["_backupCompatiblePlatforms"] = sorted(backup_platforms)
    request["_preparationAccountConfigs"] = _preparation_account_configs_for_accounts(accounts, platforms)
    images = _image_preparation_limits_for_accounts(accounts, platforms, request.get("_imagePreparationLimits"),
                                                   backup_compatible_platforms=backup_platforms)
    video = _preparation_limits_for_accounts(accounts, platforms, request.get("_preparationLimits"),
                                             backup_compatible_platforms=backup_platforms) if has_video else {}
    for account in accounts:
        if account.platform in video:
            native = account_constraints(account)
            video[account.platform] = media_compliance.tighten_limits(video[account.platform], native["video_limits"])
        context = contexts.get(account.id)
        if context:
            if account.platform in video:
                video[account.platform] = media_compliance.tighten_limits(video[account.platform], context["video_limits"])
            if account.platform in images and context["image_limits"]:
                images[account.platform] = media_compliance.tighten_limits(images[account.platform], context["image_limits"])
    request["_imagePreparationLimits"] = images
    if has_video:
        request["_preparationLimits"] = video
    request["_backupPreparationWarnings"] = warnings
    return request, contexts
