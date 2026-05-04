"""Structured account validation helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from myUtils import prepared_publishers
from myUtils import profiles


@dataclass(slots=True)
class AccountValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SUPPORTED_VALIDATION_PLATFORMS = {
    profiles.PLATFORM_FACEBOOK,
    profiles.PLATFORM_INSTAGRAM,
    profiles.PLATFORM_REDDIT,
    profiles.PLATFORM_TELEGRAM,
    profiles.PLATFORM_YOUTUBE,
    profiles.PLATFORM_TIKTOK,
    profiles.PLATFORM_THREADS,
    profiles.PLATFORM_DISCORD,
    profiles.PLATFORM_PATREON,
    profiles.PLATFORM_TWITTER,
    profiles.PLATFORM_DOUYIN,
    profiles.PLATFORM_KUAISHOU,
    profiles.PLATFORM_TENCENT,
    profiles.PLATFORM_XIAOHONGSHU,
    profiles.PLATFORM_BILIBILI,
    profiles.PLATFORM_BAIJIAHAO,
    profiles.PLATFORM_MEDIUM,
    profiles.PLATFORM_SUBSTACK,
}


def _config_value(config: dict[str, Any], key: str) -> Any:
    direct = config.get(key)
    if direct not in (None, ""):
        return direct
    env_name = config.get(f"{key}Env")
    if env_name:
        return {"env": str(env_name)}
    return ""


def _present(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value.get("env"))
    if isinstance(value, list):
        return len(value) > 0
    return value not in (None, "")


def validate_structured_account_config(
    *,
    platform: str,
    auth_type: str,
    config: dict[str, Any] | None,
    cookie_path: str | None = None,
    profile_settings: dict[str, Any] | None = None,
    perform_live_checks: bool = False,
    session=None,
) -> AccountValidationResult:
    config = dict(config or {})
    profile_settings = dict(profile_settings or {})
    errors: list[str] = []
    warnings: list[str] = []
    metadata: dict[str, Any] = {}

    if platform not in SUPPORTED_VALIDATION_PLATFORMS:
        errors.append(f"Unsupported platform: {platform}")
        return AccountValidationResult(valid=False, errors=errors, warnings=warnings, metadata=metadata)

    if profiles.platform_requires_cookie(platform):
        if auth_type != "cookie":
            warnings.append(f"{platform} 目前主要依賴 cookie / browser session，建議 authType 使用 cookie")
        if auth_type == "cookie" and not cookie_path:
            warnings.append("未指定 cookiePath；後端會自動產生預設路徑")

    if platform == profiles.PLATFORM_PATREON:
        warnings.append("Patreon 目前僅支援內容產生 / 匯出，不支援直接 API 發佈")

    if platform == profiles.PLATFORM_REDDIT:
        if not _present(config.get("subreddits")):
            errors.append("Reddit 帳號需要至少一個 subreddit")
        if not _present(_config_value(config, "clientId")):
            errors.append("Reddit 帳號缺少 clientId 或 clientIdEnv")
        if not _present(_config_value(config, "clientSecret")):
            errors.append("Reddit 帳號缺少 clientSecret 或 clientSecretEnv")
        if not _present(_config_value(config, "refreshToken")):
            errors.append("Reddit 帳號缺少 refreshToken 或 refreshTokenEnv")

    if platform == profiles.PLATFORM_TELEGRAM:
        if not _present(config.get("chatId")):
            errors.append("Telegram 帳號缺少 chatId")
        if not _present(_config_value(config, "botToken")):
            errors.append("Telegram 帳號缺少 botToken 或 botTokenEnv")

    if platform == profiles.PLATFORM_YOUTUBE:
        if not _present(config.get("channelId")):
            errors.append("YouTube 帳號缺少 channelId")
        has_access_token = _present(_config_value(config, "accessToken"))
        has_oauth_triplet = all(
            _present(_config_value(config, key))
            for key in ("clientId", "clientSecret", "refreshToken")
        )
        if not has_access_token and not has_oauth_triplet:
            errors.append("YouTube 帳號需要 accessToken 或 clientId/clientSecret/refreshToken")

    if platform == profiles.PLATFORM_FACEBOOK:
        if not _present(config.get("pageId")):
            errors.append("Facebook 帳號缺少 pageId")
        if not _present(_config_value(config, "accessToken")):
            errors.append("Facebook 帳號缺少 accessToken 或 accessTokenEnv")

    if platform == profiles.PLATFORM_INSTAGRAM:
        if not _present(config.get("igUserId")):
            errors.append("Instagram 帳號缺少 igUserId")
        if not _present(_config_value(config, "accessToken")):
            errors.append("Instagram 帳號缺少 accessToken 或 accessTokenEnv")

    if platform == profiles.PLATFORM_THREADS:
        if not _present(config.get("threadUserId") or config.get("userId")):
            errors.append("Threads 帳號缺少 threadUserId")
        if not _present(_config_value(config, "accessToken")):
            errors.append("Threads 帳號缺少 accessToken 或 accessTokenEnv")

    if platform == profiles.PLATFORM_DISCORD:
        if not _present(_config_value(config, "webhookUrl")):
            errors.append("Discord 帳號缺少 webhookUrl 或 webhookUrlEnv")

    if platform == profiles.PLATFORM_TIKTOK:
        if not _present(_config_value(config, "accessToken")):
            errors.append("TikTok 帳號缺少 accessToken 或 accessTokenEnv")
        publish_mode = str(config.get("publishMode") or "direct").strip().lower()
        if publish_mode not in {"direct", "draft"}:
            errors.append("TikTok publishMode 只支援 direct 或 draft")
        if profile_settings.get("watermark"):
            warnings.append("TikTok 會改用原始未加浮水印素材；請確認原始素材符合 TikTok 發佈規範")

    if perform_live_checks and not errors:
        try:
            if platform == profiles.PLATFORM_TELEGRAM:
                metadata["telegram"] = prepared_publishers.validate_telegram_config_live(config, session=session)
            elif platform == profiles.PLATFORM_REDDIT:
                metadata["reddit"] = prepared_publishers.validate_reddit_config_live(config, session=session)
            elif platform == profiles.PLATFORM_YOUTUBE:
                metadata["youtube"] = prepared_publishers.validate_youtube_config_live(config, session=session)
            elif platform == profiles.PLATFORM_FACEBOOK:
                metadata["facebook"] = prepared_publishers.validate_facebook_config_live(config, session=session)
            elif platform == profiles.PLATFORM_INSTAGRAM:
                metadata["instagram"] = prepared_publishers.validate_instagram_config_live(config, session=session)
            elif platform == profiles.PLATFORM_THREADS:
                metadata["threads"] = prepared_publishers.validate_threads_config_live(config, session=session)
            elif platform == profiles.PLATFORM_DISCORD:
                metadata["discord"] = prepared_publishers.validate_discord_config_live(config, session=session)
            elif platform == profiles.PLATFORM_TIKTOK:
                metadata["creator_info"] = prepared_publishers.query_tiktok_creator_info(config, session=session)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{platform} live 驗證失敗: {exc}")

    return AccountValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        metadata=metadata,
    )
