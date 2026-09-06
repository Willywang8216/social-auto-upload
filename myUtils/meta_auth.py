"""Meta OAuth helpers for Facebook and Instagram business flows."""

from __future__ import annotations

import base64
import datetime
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
INSTAGRAM_REFRESH_TOKEN_URL = "https://graph.instagram.com/refresh_access_token"
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


def refresh_instagram_user_token(*, access_token: str, session=None) -> dict[str, Any]:
    """Refresh a long-lived Instagram user access token via the IG-specific endpoint.

    Per Meta docs: https://developers.facebook.com/docs/instagram-platform/reference/refresh_access_token/
    Falls back to fb_exchange_token when the IG endpoint rejects the token
    (e.g. for IG accounts connected via Facebook Login rather than Instagram Login).
    Returns the same payload shape as exchange_for_long_lived_token so callers can
    handle both uniformly.
    """
    http = _get_session(session)
    response = http.get(
        INSTAGRAM_REFRESH_TOKEN_URL,
        params={"grant_type": "ig_refresh_token", "access_token": access_token},
        timeout=120,
    )
    if not getattr(response, "ok", True):
        return exchange_for_long_lived_token(access_token=access_token, session=session)
    try:
        payload = response.json()
    except ValueError:
        return exchange_for_long_lived_token(access_token=access_token, session=session)
    if payload.get("error"):
        return exchange_for_long_lived_token(access_token=access_token, session=session)
    return payload


def fetch_managed_pages(*, access_token: str, session=None) -> dict[str, Any]:
    http = _get_session(session)
    response = http.get(
        META_ME_ACCOUNTS_URL,
        params={
            'access_token': access_token,
            'fields': 'id,name,access_token,picture.type(large),instagram_business_account{id,username,profile_picture_url}',
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


# ---------------------------------------------------------------------------
# Token-expiry resolution
#
# Facebook's ``debug_token`` is the source of truth for how long a token
# lives. It returns ``expires_at`` as an epoch (``> 0``) for a token that
# expires, and ``0`` for a token Facebook reports as *never* expiring. Some
# Meta apps issue exactly such non-expiring tokens, and then also omit
# ``expires_in`` from every token response. SAU's legacy assumption that a
# Meta token is always a 60-day long-lived token that carries ``expires_in``
# made those accounts look "expired / reconnect required" forever even though
# the token never lapses. These helpers persist a *truthful* expiry instead:
# exact ISO for expiring tokens, a far-future sentinel for never-expiring ones.
# ---------------------------------------------------------------------------

DEBUG_TOKEN_URL = f"{META_GRAPH_ROOT}/debug_token"
#: Years ahead used as the "never expires" sentinel (matches how the app's
#: staleness / status code treats a far-future expiry: never stale, never
#: flagged for reconnect).
NON_EXPIRING_EXPIRY_YEARS = 10


def debug_token_info(*, access_token: str, session=None) -> dict[str, Any]:
    """Return the ``data`` block of the Graph ``debug_token`` endpoint.

    The debug call authenticates as the app itself (``app_id|app_secret``),
    so it works for any token this app issued regardless of token type.
    Raises :class:`MetaOAuthError` when Facebook reports the token invalid or
    the request itself fails. Returns ``{}`` when the app's debug credentials
    are not configured (so a resolver can fall back to ``expires_in`` instead
    of treating a config gap as a token verdict).
    """
    http = _get_session(session)
    try:
        app_token = f"{_required_env(CLIENT_ID_ENV)}|{_required_env(CLIENT_SECRET_ENV)}"
    except MetaOAuthError:
        return {}
    response = http.get(
        DEBUG_TOKEN_URL,
        params={"input_token": access_token, "access_token": app_token},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        message = payload.get("error", {}).get("message") if isinstance(payload.get("error"), dict) else payload.get("error")
        raise MetaOAuthError(str(message or "Meta debug_token failed"))
    return payload.get("data") or {}


def _far_future_expiry_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        + datetime.timedelta(days=365 * NON_EXPIRING_EXPIRY_YEARS)
    ).isoformat(timespec="seconds")


def resolve_access_token_expiry(*, access_token: str, expires_in: Any = None) -> dict[str, Any]:
    """Resolve the truthful expiry to persist for a Meta user/page token.

    Returns ``{"expires_at_iso": <naive-UTC ISO>, "mode": <str>,
    "expires_at_epoch": <int>}`` where ``mode`` is:

    * ``"expiring"`` — the token has a real deadline; persist its exact time.
    * ``"never"`` — Facebook reports the token never expires (``expires_at ==
      0``); persist a far-future sentinel so the 60-day refresh model cannot
      mark it "expired / reconnect required".

    ``expires_in`` (from a token response) is only a fallback for when the
    debug call fails transiently; a genuine Facebook rejection
    (:class:`MetaOAuthError`, e.g. an invalid token) is re-raised so callers
    never fake an expiry for a dead credential.
    """
    try:
        data = debug_token_info(access_token=access_token)
    except MetaOAuthError:
        raise
    except Exception:  # noqa: BLE001 — transient network/parse: fall back below
        data = {}

    raw_exp = data.get("expires_at")
    if isinstance(raw_exp, int) and raw_exp > 0:
        exp_dt = datetime.datetime.fromtimestamp(raw_exp, tz=datetime.timezone.utc).replace(tzinfo=None)
        return {
            "expires_at_iso": exp_dt.isoformat(timespec="seconds"),
            "mode": "expiring",
            "expires_at_epoch": raw_exp,
        }
    if expires_in not in (None, "", 0):
        try:
            exp_dt = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(
                seconds=int(expires_in)
            )
            return {
                "expires_at_iso": exp_dt.isoformat(timespec="seconds"),
                "mode": "expiring",
                "expires_at_epoch": int(exp_dt.timestamp()),
            }
        except (TypeError, ValueError):
            pass
    return {
        "expires_at_iso": _far_future_expiry_iso(),
        "mode": "never",
        "expires_at_epoch": 0,
    }
