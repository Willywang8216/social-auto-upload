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
    profiles.PLATFORM_TEACHING_BLOG,
    profiles.PLATFORM_NW_SW_BLOG,
}


def _config_value(config: dict[str, Any], key: str, *, default_env: str | None = None) -> Any:
    direct = config.get(key)
    if direct not in (None, ""):
        return direct
    env_name = config.get(f"{key}Env")
    if env_name:
        return {"env": str(env_name)}
    if default_env:
        return {"env": str(default_env)}
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
        has_access_token = _present(_config_value(config, "accessToken"))
        patreon_auth_type = str(config.get("patreonAuthType") or "cookie").strip().lower()
        if patreon_auth_type == "cookie":
            if not cookie_path:
                warnings.append("未指定 cookiePath；後端會自動產生預設路徑")
        elif patreon_auth_type == "api":
            if not has_access_token:
                warnings.append("Patreon accessToken 可在 OAuth Connect 完成後自動回填")
        else:
            if not has_access_token and not cookie_path:
                errors.append("Patreon 帳號需要 cookiePath 或 OAuth accessToken")

    if platform == profiles.PLATFORM_REDDIT:
        reddit_auth_type = str(config.get("redditAuthType") or "api").strip().lower()
        if reddit_auth_type == "cookie":
            if not _present(config.get("subreddits")):
                warnings.append("Reddit 尚未設定 subreddits；儲存後可在編輯頁面中新增")
            if not cookie_path:
                warnings.append("未指定 cookiePath；後端會自動產生預設路徑")
        else:
            if not _present(config.get("subreddits")):
                if auth_type == 'oauth':
                    warnings.append("Reddit 尚未設定 subreddits；儲存後可在編輯頁面中新增")
                else:
                    errors.append("Reddit 帳號需要至少一個 subreddit")
            has_client_id = _present(_config_value(config, "clientId"))
            has_client_secret = _present(_config_value(config, "clientSecret"))
            has_refresh_token = _present(_config_value(config, "refreshToken"))
            if auth_type == 'oauth' and not has_refresh_token:
                warnings.append("Reddit refreshToken 可在 OAuth Connect 完成後自動回填")
            elif not has_refresh_token:
                errors.append("Reddit 帳號缺少 refreshToken 或 refreshTokenEnv")
            if auth_type == 'oauth' and not has_client_id:
                warnings.append("Reddit clientId 可在 OAuth Connect 時透過 env 變數自動讀取；若有 clientIdEnv 可忽略")
            elif not has_client_id:
                errors.append("Reddit 帳號缺少 clientId 或 clientIdEnv")
            if auth_type == 'oauth' and not has_client_secret:
                warnings.append("Reddit clientSecret 可在 OAuth Connect 時透過 env 變數自動讀取；若有 clientSecretEnv 可忽略")
            elif not has_client_secret:
                errors.append("Reddit 帳號缺少 clientSecret 或 clientSecretEnv")

    if platform == profiles.PLATFORM_TELEGRAM:
        if not _present(config.get("chatId")):
            errors.append("Telegram 帳號缺少 chatId")
        if not _present(_config_value(config, "botToken")):
            errors.append("Telegram 帳號缺少 botToken 或 botTokenEnv")

    if platform == profiles.PLATFORM_YOUTUBE:
        has_channel_id = _present(config.get("channelId"))
        has_access_token = _present(_config_value(config, "accessToken"))
        has_oauth_triplet = all(
            _present(_config_value(config, key))
            for key in ("clientId", "clientSecret")
        )
        has_refresh_token = _present(_config_value(config, "refreshToken"))
        if auth_type == 'oauth':
            if not has_access_token and not has_refresh_token:
                warnings.append("YouTube refreshToken 可在 OAuth Connect 完成後自動回填")
            if not has_oauth_triplet:
                warnings.append("YouTube clientId/clientSecret 可在 OAuth Connect 時透過 env 變數自動讀取；若有 clientIdEnv 可忽略")
            if not has_channel_id:
                warnings.append("YouTube channelId 可在 OAuth Connect 完成後自動回填")
        else:
            if not has_access_token and not (has_oauth_triplet and has_refresh_token):
                errors.append("YouTube 帳號需要 accessToken 或 clientId/clientSecret/refreshToken")
            if not has_channel_id:
                errors.append("YouTube 帳號缺少 channelId")

    if platform == profiles.PLATFORM_FACEBOOK:
        has_page_id = _present(config.get("pageId"))
        has_access_token = _present(_config_value(config, "accessToken"))
        if not has_page_id:
            if auth_type == 'oauth':
                warnings.append("Facebook pageId 可在 OAuth Connect 完成後自動回填")
            else:
                errors.append("Facebook 帳號缺少 pageId")
        if not has_access_token:
            if auth_type == 'oauth':
                warnings.append("Facebook accessToken 可在 OAuth Connect 完成後自動回填")
            else:
                errors.append("Facebook 帳號缺少 accessToken 或 accessTokenEnv")

    if platform == profiles.PLATFORM_INSTAGRAM:
        has_ig_user_id = _present(config.get("igUserId"))
        has_access_token = _present(_config_value(config, "accessToken"))
        if not has_ig_user_id:
            if auth_type == 'oauth':
                warnings.append("Instagram igUserId 可在 OAuth Connect 完成後自動回填")
            else:
                errors.append("Instagram 帳號缺少 igUserId")
        if not has_access_token:
            if auth_type == 'oauth':
                warnings.append("Instagram accessToken 可在 OAuth Connect 完成後自動回填")
            else:
                errors.append("Instagram 帳號缺少 accessToken 或 accessTokenEnv")

    if platform == profiles.PLATFORM_THREADS:
        has_thread_user_id = _present(config.get("threadUserId") or config.get("userId"))
        has_access_token = _present(_config_value(config, "accessToken"))
        if not has_thread_user_id:
            if auth_type == 'oauth':
                warnings.append("Threads threadUserId 可在 OAuth Connect 完成後自動回填")
            else:
                errors.append("Threads 帳號缺少 threadUserId")
        if not has_access_token:
            if auth_type == 'oauth':
                warnings.append("Threads accessToken 可在 OAuth Connect 完成後自動回填")
            else:
                errors.append("Threads 帳號缺少 accessToken 或 accessTokenEnv")

    if platform == profiles.PLATFORM_DISCORD:
        if not _present(_config_value(config, "webhookUrl")):
            errors.append("Discord 帳號缺少 webhookUrl 或 webhookUrlEnv")

    if platform == profiles.PLATFORM_TWITTER:
        twitter_auth_type = str(config.get("twitterAuthType") or "cookie").strip().lower()
        if twitter_auth_type == "api":
            if not _present(_config_value(config, "apiKey", default_env="X_API_KEY")):
                errors.append("Twitter API 模式需要 apiKey / apiKeyEnv，或設定 X_API_KEY env")
            if not _present(_config_value(config, "apiKeySecret", default_env="X_API_KEY_SECRET")):
                errors.append("Twitter API 模式需要 apiKeySecret / apiKeySecretEnv，或設定 X_API_KEY_SECRET env")
            if not _present(_config_value(config, "accessToken", default_env="X_ACCESS_TOKEN")):
                errors.append("Twitter API 模式需要 accessToken / accessTokenEnv，或設定 X_ACCESS_TOKEN env")
            if not _present(_config_value(config, "accessTokenSecret", default_env="X_ACCESS_TOKEN_SECRET")):
                errors.append("Twitter API 模式需要 accessTokenSecret / accessTokenSecretEnv，或設定 X_ACCESS_TOKEN_SECRET env")
        elif twitter_auth_type == "cookie":
            if not cookie_path:
                warnings.append("未指定 cookiePath；後端會自動產生預設路徑")

    if platform == profiles.PLATFORM_TIKTOK:
        has_access_token = _present(_config_value(config, "accessToken"))
        if auth_type == 'oauth':
            if not has_access_token:
                warnings.append("TikTok accessToken 可在 OAuth Connect 完成後自動回填")
        else:
            if not has_access_token:
                errors.append("TikTok 帳號缺少 accessToken 或 accessTokenEnv")
        publish_mode = str(config.get("publishMode") or "direct").strip().lower()
        if publish_mode not in {"direct", "draft"}:
            errors.append("TikTok publishMode 只支援 direct 或 draft")
        if profile_settings.get("watermark"):
            warnings.append("TikTok 會改用原始未加浮水印素材；請確認原始素材符合 TikTok 發佈規範")

    if platform == profiles.PLATFORM_TEACHING_BLOG:
        if not _present(_config_value(config, "repoOwner")):
            errors.append("Teaching Blog 帳號缺少 repoOwner")
        if not _present(_config_value(config, "repoName")):
            errors.append("Teaching Blog 帳號缺少 repoName")
        if not _present(_config_value(config, "githubToken", default_env="SAU_TEACHING_BLOG_GITHUB_TOKEN")):
            errors.append("Teaching Blog 帳號缺少 githubToken 或 githubTokenEnv")

    if platform == profiles.PLATFORM_NW_SW_BLOG:
        if not _present(config.get("apiBase")):
            errors.append("NW/SW Blog 帳號缺少 apiBase")
        if not _present(_config_value(config, "apiToken", default_env="SAU_NW_SW_BLOG_API_TOKEN")):
            errors.append("NW/SW Blog 帳號缺少 apiToken 或 apiTokenEnv")
        persona = str(config.get("persona") or "").strip()
        if persona and persona not in ("sexualwill", "nakedwill"):
            errors.append(f"NW/SW Blog persona 必須為 sexualwill 或 nakedwill，目前為 '{persona}'")
        locale = str(config.get("locale") or "").strip()
        if locale and locale not in ("en", "zh"):
            errors.append(f"NW/SW Blog locale 必須為 en 或 zh，目前為 '{locale}'")

    if perform_live_checks and not errors:
        try:
            if platform == profiles.PLATFORM_TELEGRAM:
                metadata["telegram"] = prepared_publishers.validate_telegram_config_live(config, session=session)
            elif platform == profiles.PLATFORM_REDDIT:
                reddit_auth_type = str(config.get("redditAuthType") or "api").strip().lower()
                if reddit_auth_type == "cookie":
                    warnings.append("Reddit cookie 模式不支援 live API 驗證，請確認 cookie 檔案有效")
                elif _present(_config_value(config, 'refreshToken')):
                    metadata["reddit"] = prepared_publishers.validate_reddit_config_live(config, session=session)
                else:
                    warnings.append('Reddit live 驗證已略過，等待 OAuth Connect 自動填入 refreshToken')
            elif platform == profiles.PLATFORM_YOUTUBE:
                if _present(config.get('channelId')):
                    metadata["youtube"] = prepared_publishers.validate_youtube_config_live(config, session=session)
                else:
                    warnings.append('YouTube live 驗證已略過，等待 OAuth Connect 自動填入 channelId')
            elif platform == profiles.PLATFORM_FACEBOOK:
                if _present(config.get('pageId')) and _present(_config_value(config, 'accessToken')):
                    metadata["facebook"] = prepared_publishers.validate_facebook_config_live(config, session=session)
                else:
                    warnings.append('Facebook live 驗證已略過，等待 OAuth Connect 自動填入 pageId / accessToken')
            elif platform == profiles.PLATFORM_INSTAGRAM:
                if _present(config.get('igUserId')) and _present(_config_value(config, 'accessToken')):
                    metadata["instagram"] = prepared_publishers.validate_instagram_config_live(config, session=session)
                else:
                    warnings.append('Instagram live 驗證已略過，等待 OAuth Connect 自動填入 igUserId / accessToken')
            elif platform == profiles.PLATFORM_THREADS:
                if _present(config.get('threadUserId') or config.get('userId')) and _present(_config_value(config, 'accessToken')):
                    metadata["threads"] = prepared_publishers.validate_threads_config_live(config, session=session)
                else:
                    warnings.append('Threads live 驗證已略過，等待 OAuth Connect 自動填入 threadUserId / accessToken')
            elif platform == profiles.PLATFORM_TWITTER:
                twitter_auth_type = str(config.get("twitterAuthType") or "cookie").strip().lower()
                if twitter_auth_type == "cookie":
                    warnings.append("Twitter cookie 模式不支援 live API 驗證，請確認 cookie 檔案有效")
                elif _present(_config_value(config, 'accessToken')):
                    metadata["twitter"] = prepared_publishers.validate_twitter_config_live(config, session=session)
                else:
                    warnings.append('Twitter live 驗證已略過，等待 OAuth Connect 自動填入 accessToken')
            elif platform == profiles.PLATFORM_DISCORD:
                metadata["discord"] = prepared_publishers.validate_discord_config_live(config, session=session)
            elif platform == profiles.PLATFORM_PATREON:
                patreon_auth_type = str(config.get("patreonAuthType") or "cookie").strip().lower()
                if patreon_auth_type == "api" and _present(_config_value(config, 'accessToken')):
                    metadata["patreon"] = prepared_publishers.validate_patreon_config_live(config, session=session)
                else:
                    warnings.append('Patreon cookie 模式不支援 live API 驗證，請確認 cookie 檔案有效')
            elif platform == profiles.PLATFORM_TIKTOK:
                metadata["creator_info"] = prepared_publishers.query_tiktok_creator_info(config, session=session)
            elif platform == profiles.PLATFORM_TEACHING_BLOG:
                if _present(_config_value(config, 'githubToken', default_env="SAU_TEACHING_BLOG_GITHUB_TOKEN")) and _present(config.get('repoOwner')) and _present(config.get('repoName')):
                    metadata["teaching_blog"] = prepared_publishers.validate_teaching_blog_config_live(config, session=session)
                else:
                    warnings.append('Teaching Blog live 驗證已略過，等待 githubToken / repoOwner / repoName')
            elif platform == profiles.PLATFORM_NW_SW_BLOG:
                if _present(_config_value(config, 'apiToken', default_env="SAU_NW_SW_BLOG_API_TOKEN")) and _present(config.get('apiBase')):
                    metadata["nw_sw_blog"] = prepared_publishers.validate_nw_sw_blog_config_live(config, session=session)
                else:
                    warnings.append('NW/SW Blog live 驗證已略過，等待 apiToken / apiBase')
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{platform} live 驗證失敗: {exc}")

    return AccountValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        metadata=metadata,
    )
