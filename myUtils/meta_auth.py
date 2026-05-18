"""Meta OAuth helpers for Facebook and Instagram business flows."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import urllib.parse
from typing import Any

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - environment-specific
    requests = None

META_GRAPH_ROOT = "https://graph.facebook.com/v25.0"
META_AUTHORIZE_URL = "https://www.facebook.com/v25.0/dialog/oauth"
META_TOKEN_URL = f"{META_GRAPH_ROOT}/oauth/access_token"
META_ME_ACCOUNTS_URL = f"{META_GRAPH_ROOT}/me/accounts"
META_INSTAGRAM_THREADS_URL_TEMPLATE = f"{META_GRAPH_ROOT}/{{ig_user_id}}"
DEFAULT_FACEBOOK_SCOPES = (
    "pages_show_list",
    "pages_manage_posts",
    "pages_read_engagement",
    "pages_manage_metadata",
    "business_management",
)
DEFAULT_INSTAGRAM_SCOPES = (
    "pages_show_list",
    "instagram_basic",
    "instagram_content_publish",
    "business_management",
)
CLIENT_ID_ENV = "META_APP_ID"
CLIENT_SECRET_ENV = "META_APP_SECRET"
REDIRECT_URI_ENV = "META_REDIRECT_URI"


class MetaOAuthError(RuntimeError):
    """Raised when Meta OAuth cannot complete."""


def _get_session(session=None):
    if session is not None:
        return session
    if requests is None:
        raise RuntimeError("requests is required for Meta OAuth helpers")
    return requests.Session()


def _required_env(name: str) -> str:
    aliases = {
        CLIENT_ID_ENV: (CLIENT_ID_ENV, 'FACEBOOK_APP_ID', 'FB_APP_ID'),
        CLIENT_SECRET_ENV: (CLIENT_SECRET_ENV, 'FACEBOOK_APP_SECRET', 'FB_APP_SECRET'),
        REDIRECT_URI_ENV: (REDIRECT_URI_ENV,),
    }
    for candidate in aliases.get(name, (name,)):
        value = str(os.environ.get(candidate, '') or '').strip()
        if value:
            return value
    raise MetaOAuthError(f"Missing required environment variable: {name}")


def default_redirect_uri() -> str:
    return str(os.environ.get(REDIRECT_URI_ENV) or 'https://socialupload.iamwillywang.com/oauth/meta/callback').strip()


def build_state_token() -> str:
    return secrets.token_urlsafe(24)


def default_scopes_for_platform(platform: str) -> tuple[str, ...]:
    return DEFAULT_INSTAGRAM_SCOPES if platform == 'instagram' else DEFAULT_FACEBOOK_SCOPES


def build_authorize_url(*, client_id: str, redirect_uri: str, state: str, scopes: tuple[str, ...] | list[str]) -> str:
    query = urllib.parse.urlencode({
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': ','.join(scopes),
        'state': state,
        'auth_type': 'reauthenticate',
    })
    return f"{META_AUTHORIZE_URL}?{query}"


def build_authorize_url_from_env(*, state: str, redirect_uri: str | None = None, scopes: tuple[str, ...] | list[str], client_id_env: str = CLIENT_ID_ENV) -> str:
    return build_authorize_url(
        client_id=_required_env(client_id_env),
        redirect_uri=redirect_uri or default_redirect_uri(),
        state=state,
        scopes=scopes,
    )


def exchange_code_for_token(*, code: str, redirect_uri: str, client_id_env: str = CLIENT_ID_ENV, client_secret_env: str = CLIENT_SECRET_ENV, session=None) -> dict[str, Any]:
    http = _get_session(session)
    response = http.get(
        META_TOKEN_URL,
        params={
            'client_id': _required_env(client_id_env),
            'client_secret': _required_env(client_secret_env),
            'redirect_uri': redirect_uri,
            'code': code,
        },
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get('error'):
        message = payload.get('error', {}).get('message') if isinstance(payload.get('error'), dict) else payload.get('error')
        raise MetaOAuthError(str(message or 'Meta token exchange failed'))
    return payload


def exchange_for_long_lived_token(*, access_token: str, client_id_env: str = CLIENT_ID_ENV, client_secret_env: str = CLIENT_SECRET_ENV, session=None) -> dict[str, Any]:
    http = _get_session(session)
    response = http.get(
        META_TOKEN_URL,
        params={
            'grant_type': 'fb_exchange_token',
            'client_id': _required_env(client_id_env),
            'client_secret': _required_env(client_secret_env),
            'fb_exchange_token': access_token,
        },
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get('error'):
        message = payload.get('error', {}).get('message') if isinstance(payload.get('error'), dict) else payload.get('error')
        raise MetaOAuthError(str(message or 'Meta long-lived token exchange failed'))
    return payload


def fetch_managed_pages(*, access_token: str, session=None) -> dict[str, Any]:
    http = _get_session(session)
    response = http.get(
        META_ME_ACCOUNTS_URL,
        params={
            'access_token': access_token,
            'fields': 'id,name,access_token,instagram_business_account{id,username}',
        },
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get('error'):
        message = payload.get('error', {}).get('message') if isinstance(payload.get('error'), dict) else payload.get('error')
        raise MetaOAuthError(str(message or 'Meta pages query failed'))
    return payload


def parse_signed_request(signed_request: str, *, app_secret: str) -> dict[str, Any]:
    """Decode and verify a Meta signed_request payload.

    Meta sends signed_request parameters for deauthorize and data-deletion
    callbacks. The format is ``base64url(encoded_signature).base64url(payload)``
    where the signature is HMAC-SHA256 of the payload using the app secret.
    """
    if not signed_request or '.' not in signed_request:
        raise ValueError('signed_request must contain a dot-separated signature and payload')

    encoded_sig, encoded_payload = signed_request.split('.', 1)
    # Pad base64url to standard base64
    def _b64url_decode(s: str) -> bytes:
        padding = 4 - len(s) % 4
        if padding != 4:
            s += '=' * padding
        return base64.urlsafe_b64decode(s)

    payload_bytes = _b64url_decode(encoded_payload)
    expected_sig = hmac.new(app_secret.encode(), payload_bytes, hashlib.sha256).digest()
    actual_sig = _b64url_decode(encoded_sig)

    if not hmac.compare_digest(expected_sig, actual_sig):
        raise ValueError('signed_request signature mismatch')

    return json.loads(payload_bytes.decode('utf-8'))
