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


def configure_request(accounts, platforms, request_data: dict, *, settings=None, has_video=True, db_path=None):
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
                account=account, settings=settings, db_path=db_path)
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


def verify_native_destination(account, *, settings=None, db_path, session=None, payload=None):
    """Actual read-only identity AND publishing-grant verification, no refresh.

    Unsupported native grant models stay configured_unverified; in particular
    a successful X users/me read does not repair/prove native OAuth write scope.
    TikTok's creator-info/query endpoint is documented video.publish-protected;
    combine it with user/info open_id, never nickname/display-name identity.
    YouTube tokeninfo proves current token grant, channels mine exact identity.
    """
    from myUtils import publish_readiness, token_state, native_destinations
    before = publish_readiness._context(account, settings)
    if before["provider"] != "direct" or not before["identity"]:
        raise ValueError("Native verification needs an exact direct identity")
    platform = account.platform
    config = before["config"]
    native_destinations.assert_effective_destination(platform, config, payload)
    token = prepared_publishers._config_value(config, "accessToken")
    non_oauth = platform in native_destinations.SUPPORTED_PLATFORMS
    if not token and not non_oauth:
        raise ValueError("Native verification needs selected credentials")
    http = _BoundedHTTP(prepared_publishers._get_session(session))
    expected = before["identity"].get("id")
    capabilities, limits, expiry = [], {}, None
    if non_oauth:
        capabilities = native_destinations.verify_grant(platform, config, before["identity"], http)
    elif platform == "tiktok":
        response = http.get("https://open.tiktokapis.com/v2/user/info/",
            headers={"Authorization": f"Bearer {token}"}, params={"fields": "open_id"})
        prepared_publishers._raise_for_status(response)
        prepared_publishers._raise_tiktok_error(response)
        body = prepared_publishers._response_payload(response)
        if (body.get("data") or {}).get("user", {}).get("open_id") != expected:
            raise ValueError("TikTok verified identity does not match exact openId")
        creator = prepared_publishers.query_tiktok_creator_info(config, access_token=token, session=http)
        data = creator.get("data") or {}
        if (not isinstance(data.get("privacy_level_options"), list) or not data["privacy_level_options"]
                or type(data.get("max_video_post_duration_sec")) is not int or data["max_video_post_duration_sec"] <= 0):
            raise ValueError("TikTok direct creator capabilities are unverified")
        capabilities = ["video", "image", "carousel"]
        limits["video_max_seconds"] = data["max_video_post_duration_sec"]
    elif platform == "youtube":
        response = http.get("https://oauth2.googleapis.com/tokeninfo", params={"access_token": token})
        prepared_publishers._raise_for_status(response)
        grant = prepared_publishers._response_payload(response)
        scopes = set(str(grant.get("scope") or "").split())
        write_scopes = {"https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube",
                        "https://www.googleapis.com/auth/youtube.force-ssl"}
        if not scopes & write_scopes or int(grant.get("expires_in") or 0) <= 0:
            raise ValueError("YouTube current token has no verified publishing grant")
        client = prepared_publishers._config_value(config, "clientId")
        if client and grant.get("aud") != client:
            raise ValueError("YouTube token audience does not match selected client")
        response = http.get(prepared_publishers.YOUTUBE_CHANNELS_URL,
            headers={"Authorization": f"Bearer {token}"}, params={"part": "id,status", "mine": "true"})
        prepared_publishers._raise_for_status(response)
        items = prepared_publishers._response_payload(response).get("items")
        if not isinstance(items, list) or len(items) != 1 or items[0].get("id") != expected:
            raise ValueError("YouTube verified channel does not match exact channelId")
        capabilities = ["video"]
        if scopes & (write_scopes - {"https://www.googleapis.com/auth/youtube.upload"}):
            capabilities.append("first_comment")
    elif platform in {"facebook", "instagram", "threads"}:
        from myUtils import native_authorization
        if platform == "threads":
            verified = native_authorization.threads_capabilities(config, expected_id=expected, token=token, http=http)
        else:
            verified = native_authorization.meta_capabilities(config, platform=platform, expected_id=expected, token=token, http=http)
        capabilities, expiry = verified["capabilities"], verified["expires_at"]
    else:
        raise ValueError("Native exact publishing grant verifier is not implemented; current trusted proof required")
    # record_destination_readiness performs the existing account/profile/config
    # CAS. No token refresh or nativeCredentials replacement occurs here.
    proof = {"verified": True, "live_check": True, "source": "provider_verification",
             "requires_approval": False, "config_fingerprint": before["fingerprint"],
             "identity": before["identity"], "permissions": {"posts.create": True},
             "capabilities": capabilities, "limits": limits,
             "verified_at": publish_readiness._now().isoformat()}
    if expiry:
        proof["expires_at"] = expiry
    return publish_readiness.record_destination_readiness(account, settings, proof=proof,
        provider="direct", db_path=db_path)


def ensure_destination_ready(account, *, settings=None, db_path, payload=None, session=None):
    """Refresh expired/unverified evidence once, outside any queue transaction.

    Media/intent and enqueue admission remain separate checks. This performs no
    upload, publication, credential refresh, approval override or source encoding.
    """
    from myUtils import publish_readiness, native_destinations, media_restore
    from myUtils import native_authorization
    if str((account.config or {}).get("publishingProvider") or "direct").lower() == "direct":
        native_destinations.assert_effective_destination(account.platform, account.config, payload)
    result = publish_readiness.destination_readiness(account, settings, db_path=db_path)
    if result["ready"]:
        return result
    if result.get("reasonCode") == "dependency_cooldown":
        raise media_restore.PrerequisiteUnavailable("transport", cached_wait=True,
            scope_key=publish_readiness.destination_scope_key(account, settings))
    if result.get("reasonCode") not in {"configured_unverified", "evidence_expired", "evidence_invalid", "configuration_changed"}:
        raise ValueError("Current destination authorization is unavailable; repair configuration or review the account")
    context = publish_readiness._context(account, settings)
    if context["provider"] == "sociamonials":
        backup_preparation.resolve_constraints(platform=account.platform, account=account, settings=settings,
            payload=payload, db_path=db_path, session=session)
    elif context["provider"] == "direct" and account.platform in ({"tiktok", "youtube"}
            | native_authorization.SUPPORTED_PLATFORMS | native_destinations.SUPPORTED_PLATFORMS):
        verify_native_destination(account, settings=settings, db_path=db_path, session=session, payload=payload)
    else:
        raise ValueError("The native destination has no implemented exact publishing-grant verifier")
    result = publish_readiness.destination_readiness(account, settings, db_path=db_path)
    if not result["ready"]:
        raise ValueError("Fresh destination verification did not prove current exact authorization")
    return result
