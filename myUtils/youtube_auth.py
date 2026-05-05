"""YouTube/Google OAuth helpers."""

from __future__ import annotations

import os
import secrets
import urllib.parse
from typing import Any

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - environment-specific
    requests = None

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)
CLIENT_ID_ENV = "YT_CLIENT_ID"
CLIENT_SECRET_ENV = "YT_CLIENT_SECRET"
REDIRECT_URI_ENV = "YOUTUBE_REDIRECT_URI"


class YouTubeOAuthError(RuntimeError):
    """Raised when YouTube OAuth cannot complete."""


def _get_session(session=None):
    if session is not None:
        return session
    if requests is None:
        raise RuntimeError("requests is required for YouTube OAuth helpers")
    return requests.Session()


def _required_env(name: str) -> str:
    value = str(os.environ.get(name, "") or "").strip()
    if not value:
        raise YouTubeOAuthError(f"Missing required environment variable: {name}")
    return value


def default_redirect_uri() -> str:
    return str(os.environ.get(REDIRECT_URI_ENV) or "https://up.iamwillywang.com/oauth/youtube/callback").strip()


def build_state_token() -> str:
    return secrets.token_urlsafe(24)


def build_authorize_url(*, client_id: str, redirect_uri: str, state: str, scopes: tuple[str, ...] | list[str] = DEFAULT_SCOPES) -> str:
    query = urllib.parse.urlencode({
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'access_type': 'offline',
        'prompt': 'consent',
        'include_granted_scopes': 'true',
        'scope': ' '.join(scopes),
        'state': state,
    })
    return f"{GOOGLE_AUTHORIZE_URL}?{query}"


def build_authorize_url_from_env(*, state: str, redirect_uri: str | None = None, scopes: tuple[str, ...] | list[str] = DEFAULT_SCOPES, client_id_env: str = CLIENT_ID_ENV) -> str:
    return build_authorize_url(
        client_id=_required_env(client_id_env),
        redirect_uri=redirect_uri or default_redirect_uri(),
        state=state,
        scopes=scopes,
    )


def exchange_code_for_token(*, code: str, redirect_uri: str, client_id_env: str = CLIENT_ID_ENV, client_secret_env: str = CLIENT_SECRET_ENV, session=None) -> dict[str, Any]:
    http = _get_session(session)
    response = http.post(
        GOOGLE_TOKEN_URL,
        data={
            'client_id': _required_env(client_id_env),
            'client_secret': _required_env(client_secret_env),
            'code': code,
            'grant_type': 'authorization_code',
            'redirect_uri': redirect_uri,
        },
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get('error'):
        raise YouTubeOAuthError(str(payload.get('error_description') or payload['error']))
    return payload


def fetch_my_channels(*, access_token: str, session=None) -> dict[str, Any]:
    http = _get_session(session)
    response = http.get(
        YOUTUBE_CHANNELS_URL,
        headers={'Authorization': f'Bearer {access_token}'},
        params={'part': 'id,snippet', 'mine': 'true'},
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get('error'):
        message = payload.get('error', {}).get('message') if isinstance(payload.get('error'), dict) else payload.get('error')
        raise YouTubeOAuthError(str(message or 'YouTube API error'))
    return payload
