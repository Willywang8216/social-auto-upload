"""TikTok Login Kit for Web helpers."""

from __future__ import annotations

import os
import secrets
import urllib.parse
from typing import Any

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - environment-specific
    requests = None

TIKTOK_AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_OAUTH_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_USER_INFO_URL = "https://open.tiktokapis.com/v2/user/info/"
DEFAULT_SCOPES = ("user.info.basic", "video.publish")
CLIENT_KEY_ENV = "TIKTOK_CLIENT_KEY"
CLIENT_SECRET_ENV = "TIKTOK_CLIENT_SECRET"
REDIRECT_URI_ENV = "TIKTOK_REDIRECT_URI"


class TikTokOAuthError(RuntimeError):
    """Raised when the TikTok OAuth flow cannot complete."""


def _get_session(session=None):
    if session is not None:
        return session
    if requests is None:
        raise RuntimeError("requests is required for TikTok OAuth helpers")
    return requests.Session()


def _required_env(name: str) -> str:
    value = str(os.environ.get(name, "") or "").strip()
    if not value:
        raise TikTokOAuthError(f"Missing required environment variable: {name}")
    return value


def default_redirect_uri() -> str:
    return str(
        os.environ.get(REDIRECT_URI_ENV)
        or "https://up.iamwillywang.com/oauth/tiktok/callback"
    ).strip()


def build_state_token() -> str:
    return secrets.token_urlsafe(24)


def build_authorize_url(
    *,
    client_key: str,
    redirect_uri: str,
    state: str,
    scopes: tuple[str, ...] | list[str] = DEFAULT_SCOPES,
) -> str:
    query = urllib.parse.urlencode(
        {
            "client_key": client_key,
            "response_type": "code",
            "scope": ",".join(scopes),
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{TIKTOK_AUTHORIZE_URL}?{query}"


def build_authorize_url_from_env(
    *,
    state: str,
    redirect_uri: str | None = None,
    scopes: tuple[str, ...] | list[str] = DEFAULT_SCOPES,
) -> str:
    return build_authorize_url(
        client_key=_required_env(CLIENT_KEY_ENV),
        redirect_uri=redirect_uri or default_redirect_uri(),
        state=state,
        scopes=scopes,
    )


def exchange_code_for_token(
    *,
    code: str,
    redirect_uri: str,
    session=None,
) -> dict[str, Any]:
    http = _get_session(session)
    response = http.post(
        TIKTOK_OAUTH_TOKEN_URL,
        data={
            "client_key": _required_env(CLIENT_KEY_ENV),
            "client_secret": _required_env(CLIENT_SECRET_ENV),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise TikTokOAuthError(str(payload.get("error_description") or payload["error"]))
    return payload


def fetch_user_info(
    *,
    access_token: str,
    fields: tuple[str, ...] | list[str] = ("open_id", "display_name", "avatar_url"),
    session=None,
) -> dict[str, Any]:
    http = _get_session(session)
    response = http.get(
        TIKTOK_USER_INFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"fields": ",".join(fields)},
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise TikTokOAuthError(str(payload.get("error").get("message") or payload["error"]))
    return payload
