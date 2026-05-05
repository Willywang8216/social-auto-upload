"""Reddit OAuth helpers."""

from __future__ import annotations

import os
import secrets
import urllib.parse
from typing import Any

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - environment-specific
    requests = None

REDDIT_AUTHORIZE_URL = "https://www.reddit.com/api/v1/authorize"
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_ME_URL = "https://oauth.reddit.com/api/v1/me"
DEFAULT_SCOPES = ("identity", "submit", "read")
CLIENT_ID_ENV = "REDDIT_CLIENT_ID"
CLIENT_SECRET_ENV = "REDDIT_CLIENT_SECRET"
REDIRECT_URI_ENV = "REDDIT_REDIRECT_URI"
USER_AGENT_ENV = "REDDIT_USER_AGENT"


class RedditOAuthError(RuntimeError):
    """Raised when Reddit OAuth cannot complete."""


def _get_session(session=None):
    if session is not None:
        return session
    if requests is None:
        raise RuntimeError("requests is required for Reddit OAuth helpers")
    return requests.Session()


def _required_env(name: str) -> str:
    value = str(os.environ.get(name, "") or "").strip()
    if not value:
        raise RedditOAuthError(f"Missing required environment variable: {name}")
    return value


def _user_agent(user_agent: str | None = None) -> str:
    return str(user_agent or os.environ.get(USER_AGENT_ENV) or "social-auto-upload/0.1").strip()


def default_redirect_uri() -> str:
    return str(
        os.environ.get(REDIRECT_URI_ENV)
        or "https://up.iamwillywang.com/oauth/reddit/callback"
    ).strip()


def build_state_token() -> str:
    return secrets.token_urlsafe(24)


def build_authorize_url(*, client_id: str, redirect_uri: str, state: str, scopes: tuple[str, ...] | list[str] = DEFAULT_SCOPES, duration: str = "permanent") -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "state": state,
            "redirect_uri": redirect_uri,
            "duration": duration,
            "scope": " ".join(scopes),
        }
    )
    return f"{REDDIT_AUTHORIZE_URL}?{query}"


def build_authorize_url_from_env(*, state: str, redirect_uri: str | None = None, scopes: tuple[str, ...] | list[str] = DEFAULT_SCOPES, client_id_env: str = CLIENT_ID_ENV) -> str:
    return build_authorize_url(
        client_id=_required_env(client_id_env),
        redirect_uri=redirect_uri or default_redirect_uri(),
        state=state,
        scopes=scopes,
    )


def exchange_code_for_token(*, code: str, redirect_uri: str, client_id_env: str = CLIENT_ID_ENV, client_secret_env: str = CLIENT_SECRET_ENV, user_agent: str | None = None, session=None) -> dict[str, Any]:
    http = _get_session(session)
    response = http.post(
        REDDIT_TOKEN_URL,
        auth=(_required_env(client_id_env), _required_env(client_secret_env)),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        headers={"User-Agent": _user_agent(user_agent)},
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RedditOAuthError(str(payload.get("error_description") or payload["error"]))
    return payload


def fetch_user_info(*, access_token: str, user_agent: str | None = None, session=None) -> dict[str, Any]:
    http = _get_session(session)
    response = http.get(
        REDDIT_ME_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "User-Agent": _user_agent(user_agent),
        },
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RedditOAuthError(str(payload.get("message") or payload["error"]))
    return payload
