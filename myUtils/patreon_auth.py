"""Patreon OAuth helpers."""

from __future__ import annotations

import os
import secrets
import urllib.parse
from typing import Any

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - environment-specific
    requests = None

PATREON_AUTHORIZE_URL = "https://www.patreon.com/oauth2/authorize"
PATREON_TOKEN_URL = "https://www.patreon.com/api/oauth2/token"
PATREON_IDENTITY_URL = "https://www.patreon.com/api/oauth2/v2/identity"
PATREON_CAMPAIGNS_URL = "https://www.patreon.com/api/oauth2/v2/campaigns"
DEFAULT_SCOPES = ("identity", "campaigns", "campaigns.posts")
CLIENT_ID_ENV = "PATREON_CLIENT_ID"
CLIENT_SECRET_ENV = "PATREON_CLIENT_SECRET"
REDIRECT_URI_ENV = "SAU_PATREON_CALLBACK_URL"


class PatreonOAuthError(RuntimeError):
    """Raised when Patreon OAuth cannot complete."""


def _get_session(session=None):
    if session is not None:
        return session
    if requests is None:
        raise RuntimeError("requests is required for Patreon OAuth helpers")
    return requests.Session()


def _required_env(name: str) -> str:
    value = str(os.environ.get(name, "") or "").strip()
    if not value:
        raise PatreonOAuthError(f"Missing required environment variable: {name}")
    return value


def default_redirect_uri() -> str:
    return str(
        os.environ.get(REDIRECT_URI_ENV)
        or "https://socialupload.iamwillywang.com/oauth/patreon/callback"
    ).strip()


def build_state_token() -> str:
    return secrets.token_urlsafe(24)


def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    scopes: tuple[str, ...] | list[str] = DEFAULT_SCOPES,
) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "state": state,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
        }
    )
    return f"{PATREON_AUTHORIZE_URL}?{query}"


def build_authorize_url_from_env(
    *,
    state: str,
    redirect_uri: str | None = None,
    scopes: tuple[str, ...] | list[str] = DEFAULT_SCOPES,
    client_id_env: str = CLIENT_ID_ENV,
) -> str:
    return build_authorize_url(
        client_id=_required_env(client_id_env),
        redirect_uri=redirect_uri or default_redirect_uri(),
        state=state,
        scopes=scopes,
    )


def exchange_code_for_token(
    *,
    code: str,
    redirect_uri: str,
    client_id_env: str = CLIENT_ID_ENV,
    client_secret_env: str = CLIENT_SECRET_ENV,
    session=None,
) -> dict[str, Any]:
    http = _get_session(session)
    response = http.post(
        PATREON_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": _required_env(client_id_env),
            "client_secret": _required_env(client_secret_env),
        },
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise PatreonOAuthError(str(payload.get("error_description") or payload["error"]))
    return payload


def refresh_access_token(
    *,
    refresh_token: str,
    client_id_env: str = CLIENT_ID_ENV,
    client_secret_env: str = CLIENT_SECRET_ENV,
    session=None,
) -> dict[str, Any]:
    http = _get_session(session)
    response = http.post(
        PATREON_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": _required_env(client_id_env),
            "client_secret": _required_env(client_secret_env),
        },
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise PatreonOAuthError(str(payload.get("error_description") or payload["error"]))
    return payload


def fetch_identity(*, access_token: str, session=None) -> dict[str, Any]:
    http = _get_session(session)
    response = http.get(
        PATREON_IDENTITY_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"fields[user]": "full_name,url"},
        timeout=120,
    )
    response.raise_for_status()
    return response.json()


def fetch_campaigns(*, access_token: str, session=None) -> dict[str, Any]:
    http = _get_session(session)
    response = http.get(
        PATREON_CAMPAIGNS_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"fields[campaign]": "creation_name,url,patron_count,pledge_sum"},
        timeout=120,
    )
    response.raise_for_status()
    return response.json()
