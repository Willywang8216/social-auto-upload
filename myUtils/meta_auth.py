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


from myUtils import token_state
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
    # Allows deleting published IG media via DELETE /{ig-media-id}
    # (requires a Facebook User access token; dev-mode grant is enough for
    # admin/test accounts). Needed to unpublish test posts programmatically.
    "instagram_manage_contents",
    "business_management",
)
CLIENT_ID_ENV = "META_APP_ID"
CLIENT_SECRET_ENV = "META_APP_SECRET"
REDIRECT_URI_ENV = "META_REDIRECT_URI"


class MetaOAuthError(token_state.CredentialError):
    """Safe structured Meta OAuth failure (including code/subcode)."""


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
    return token_state.response_payload(response, error_class=MetaOAuthError)


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
    return token_state.response_payload(response, error_class=MetaOAuthError)


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
    try:
        return token_state.response_payload(response, error_class=MetaOAuthError)
    except MetaOAuthError as exc:
        if exc.kind == 'transient':
            raise
        # Facebook Login tokens may be rejected by the IG-specific endpoint.
        # The FB exchange still has to succeed; it cannot hide 429/5xx errors.
        return exchange_for_long_lived_token(access_token=access_token, session=session)


def fetch_managed_pages(*, access_token: str, session=None, max_pages: int = 25) -> dict[str, Any]:
    """Fetch the complete bounded Page list, never a partial first-page result.

    Follow cursors on our fixed endpoint, not a provider-supplied arbitrary URL.
    Validate next links, reject cycles, conflicting identities and truncation.
    """
    import re
    http = _get_session(session)
    params = {
        'access_token': access_token, 'limit': 100,
        'fields': 'id,name,access_token,picture.type(large),instagram_business_account{id,username,profile_picture_url}',
    }
    pages, seen_ids, seen_cursors = [], set(), set()
    for _ in range(max(1, min(int(max_pages), 25))):
        response = http.get(META_ME_ACCOUNTS_URL, params=dict(params), timeout=30)
        payload = token_state.response_payload(response, error_class=MetaOAuthError)
        rows = payload.get('data')
        if not isinstance(rows, list):
            raise MetaOAuthError('Meta returned an unreadable Page list')
        if len(pages) + len(rows) > 2500:
            raise MetaOAuthError('Meta Page enumeration exceeded the account safety limit')
        for page in rows:
            if not isinstance(page, dict) or not str(page.get('id') or '').strip():
                raise MetaOAuthError('Meta returned a Page without a stable identity')
            page_id = str(page['id'])
            if page_id in seen_ids:
                raise MetaOAuthError('Meta returned ambiguous duplicate Page identities')
            seen_ids.add(page_id)
            pages.append(page)
        paging = payload.get('paging') or {}
        if not isinstance(paging, dict):
            raise MetaOAuthError('Meta returned invalid pagination')
        next_url = paging.get('next')
        if not next_url:
            return {'data': pages}
        try:
            url = urllib.parse.urlsplit(str(next_url))
            valid = (url.scheme == 'https' and url.hostname == 'graph.facebook.com'
                     and url.port in (None, 443) and not url.username and not url.password
                     and not url.fragment and re.fullmatch(r'/(?:v\d+\.\d+/)?(?:me|\d+)/accounts', url.path))
            query = urllib.parse.parse_qs(url.query)
            after = (query.get('after') or [None])[0]
            cursor = (paging.get('cursors') or {}).get('after')
        except (ValueError, TypeError, AttributeError):
            valid, after, cursor = False, None, None
        if not valid or (after and cursor and after != cursor):
            raise MetaOAuthError('Meta returned an unsafe pagination link')
        cursor = after or cursor
        if not isinstance(cursor, str) or not cursor or len(cursor) > 4096 or cursor in seen_cursors:
            raise MetaOAuthError('Meta returned a missing or repeated pagination cursor')
        seen_cursors.add(cursor)
        params['after'] = cursor
    raise MetaOAuthError('Meta Page enumeration exceeded the safety limit; no credentials changed')


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


# Token and data-access expiry are separate provider facts. A non-expiring token
# still needs periodic validation: scheduled expiry says nothing about revocation.
DEBUG_TOKEN_URL = f"{META_GRAPH_ROOT}/debug_token"
# Compatibility sentinel for consumers which still require an ISO expiry. Only
# affirmative valid introspection with explicit zero may receive this sentinel.
NON_EXPIRING_EXPIRY_YEARS = 10


def _validate_debug_data(data: dict) -> dict:
    if data.get('is_valid') is False or data.get('error'):
        verdict = token_state.provider_error(data.get('error'), invalid=data.get('is_valid') is False)
        raise MetaOAuthError(str(verdict), code=verdict.error_code, subcode=verdict.error_subcode,
                             terminal=verdict.terminal, kind=verdict.kind)
    return data


def debug_token_info(*, access_token: str, session=None) -> dict[str, Any]:
    """Return introspection data; reject explicit invalid/error verdicts.

    Missing app introspection credentials yield unknown, never a valid verdict.
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
    payload = token_state.response_payload(response, error_class=MetaOAuthError)
    data = payload.get('data')
    return _validate_debug_data(data) if isinstance(data, dict) else {}


def _far_future_expiry_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        + datetime.timedelta(days=365 * NON_EXPIRING_EXPIRY_YEARS)
    ).isoformat(timespec="seconds")


def _epoch(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
        if number >= 0 and str(number) == str(value):
            return number
    except (TypeError, ValueError):
        pass
    return None


def _epoch_iso(epoch: int | None) -> str:
    if not epoch:
        return ''
    try:
        return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')
    except (ValueError, OverflowError, OSError):
        return ''


def resolve_access_token_expiry(*, access_token: str, expires_in: Any = None,
                                debug_data: dict | None = None, session=None) -> dict[str, Any]:
    """Resolve expiring / verified-never / unknown, plus data-access expiry.

    Valid explicit zero outranks expires_in. Unknown without a duration remains
    unknown, and a provider-invalid token can never become a ten-year token.
    All epoch arithmetic uses aware UTC (including the duration fallback).
    """
    if debug_data is None:
        try:
            data = debug_token_info(access_token=access_token, **({'session': session} if session is not None else {}))
        except MetaOAuthError:
            raise
        except Exception:  # transport failure: duration is still a usable fact
            data = {}
    else:
        data = debug_data
    data = _validate_debug_data(data if isinstance(data, dict) else {})
    raw_exp = _epoch(data.get('expires_at'))
    data_exp = _epoch(data.get('data_access_expires_at'))
    resolved = {
        'expires_at_iso': '', 'mode': 'unknown', 'expires_at_epoch': None,
        'data_access_expires_at_iso': _epoch_iso(data_exp),
        'data_access_expires_at_epoch': data_exp,
        'is_valid': data.get('is_valid') if isinstance(data.get('is_valid'), bool) else None,
    }
    if raw_exp == 0 and data.get('is_valid') is True:
        resolved.update(expires_at_iso=_far_future_expiry_iso(), mode='never', expires_at_epoch=0)
    elif raw_exp and _epoch_iso(raw_exp):
        resolved.update(expires_at_iso=_epoch_iso(raw_exp), mode='expiring', expires_at_epoch=raw_exp)
    else:
        duration = _epoch(expires_in)
        if duration:
            try:
                expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=duration)
                resolved.update(expires_at_iso=expires.replace(tzinfo=None).isoformat(timespec='seconds'),
                                mode='expiring', expires_at_epoch=int(expires.timestamp()))
            except (OverflowError, ValueError):
                pass
    return resolved
