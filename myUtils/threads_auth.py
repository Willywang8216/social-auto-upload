"""Threads OAuth helpers."""

from __future__ import annotations

import os
import secrets
import urllib.parse
from typing import Any

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - environment-specific
    requests = None

THREADS_AUTHORIZE_URL = "https://threads.net/oauth/authorize"
THREADS_TOKEN_URL = "https://graph.threads.net/oauth/access_token"
THREADS_LONG_LIVED_TOKEN_URL = "https://graph.threads.net/access_token"
THREADS_REFRESH_TOKEN_URL = "https://graph.threads.net/refresh_access_token"
THREADS_ME_URL = "https://graph.threads.net/v1.0/me"
DEFAULT_SCOPES = ("threads_basic", "threads_content_publish")
CLIENT_ID_ENV = "THREADS_APP_ID"
CLIENT_SECRET_ENV = "THREADS_APP_SECRET"
REDIRECT_URI_ENV = "THREADS_REDIRECT_URI"


class ThreadsOAuthError(RuntimeError):
    """Raised when Threads OAuth cannot complete."""


def _get_session(session=None):
    if session is not None:
        return session
    if requests is None:
        raise RuntimeError('requests is required for Threads OAuth helpers')
    return requests.Session()


def _required_env(name: str) -> str:
    aliases = {
        CLIENT_ID_ENV: (CLIENT_ID_ENV, 'THREADS_CLIENT_ID'),
        CLIENT_SECRET_ENV: (CLIENT_SECRET_ENV, 'THREADS_CLIENT_SECRET'),
        REDIRECT_URI_ENV: (REDIRECT_URI_ENV,),
    }
    for candidate in aliases.get(name, (name,)):
        value = str(os.environ.get(candidate, '') or '').strip()
        if value:
            return value
    raise ThreadsOAuthError(f'Missing required environment variable: {name}')


def default_redirect_uri() -> str:
    return str(os.environ.get(REDIRECT_URI_ENV) or 'https://up.iamwillywang.com/oauth/threads/callback').strip()


def build_state_token() -> str:
    return secrets.token_urlsafe(24)


def build_authorize_url(*, client_id: str, redirect_uri: str, state: str, scopes: tuple[str, ...] | list[str] = DEFAULT_SCOPES) -> str:
    query = urllib.parse.urlencode({
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'scope': ','.join(scopes),
        'response_type': 'code',
        'state': state,
    })
    return f"{THREADS_AUTHORIZE_URL}?{query}"


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
        THREADS_TOKEN_URL,
        data={
            'client_id': _required_env(client_id_env),
            'client_secret': _required_env(client_secret_env),
            'grant_type': 'authorization_code',
            'redirect_uri': redirect_uri,
            'code': code,
        },
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get('error'):
        message = payload.get('error', {}).get('message') if isinstance(payload.get('error'), dict) else payload.get('error')
        raise ThreadsOAuthError(str(message or 'Threads token exchange failed'))
    return payload


def exchange_for_long_lived_token(*, access_token: str, client_secret_env: str = CLIENT_SECRET_ENV, session=None) -> dict[str, Any]:
    http = _get_session(session)
    response = http.get(
        THREADS_LONG_LIVED_TOKEN_URL,
        params={
            'grant_type': 'th_exchange_token',
            'client_secret': _required_env(client_secret_env),
            'access_token': access_token,
        },
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get('error'):
        message = payload.get('error', {}).get('message') if isinstance(payload.get('error'), dict) else payload.get('error')
        raise ThreadsOAuthError(str(message or 'Threads long-lived token exchange failed'))
    return payload


def refresh_long_lived_token(*, access_token: str, session=None) -> dict[str, Any]:
    http = _get_session(session)
    response = http.get(
        THREADS_REFRESH_TOKEN_URL,
        params={
            'grant_type': 'th_refresh_token',
            'access_token': access_token,
        },
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get('error'):
        message = payload.get('error', {}).get('message') if isinstance(payload.get('error'), dict) else payload.get('error')
        raise ThreadsOAuthError(str(message or 'Threads long-lived token refresh failed'))
    return payload


def fetch_me(*, access_token: str, fields: tuple[str, ...] | list[str] = ('id', 'username'), session=None) -> dict[str, Any]:
    http = _get_session(session)
    response = http.get(
        THREADS_ME_URL,
        params={'fields': ','.join(fields), 'access_token': access_token},
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get('error'):
        message = payload.get('error', {}).get('message') if isinstance(payload.get('error'), dict) else payload.get('error')
        raise ThreadsOAuthError(str(message or 'Threads profile query failed'))
    return payload
