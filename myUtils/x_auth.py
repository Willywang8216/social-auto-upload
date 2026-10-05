"""Twitter/X OAuth 2.0 PKCE helpers."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import urllib.parse
from typing import Any

import logging

_log = logging.getLogger(__name__)

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover
    requests = None

X_AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
X_TOKEN_URL = "https://api.x.com/2/oauth2/token"
X_ME_URL = "https://api.x.com/2/users/me"
# media.write is not optional: the media upload goes to
# upload.twitter.com/1.1/media/upload.json, and an OAuth2 user token without it
# is rejected with 401 "Invalid or expired token" (code 89) - for every account,
# including a freshly re-authorised one, which makes it look like the login
# failed. X_UPLOAD_URL in prepared_publishers.py is that endpoint.
DEFAULT_SCOPES = ("tweet.read", "tweet.write", "media.write", "users.read", "offline.access")
CLIENT_ID_ENV = "X_CLIENT_ID"
CLIENT_SECRET_ENV = "X_CLIENT_SECRET"
REDIRECT_URI_ENV = "SAU_TWITTER_CALLBACK_URL"
DEFAULT_REDIRECT_URI = "https://socialupload.iamwillywang.com/oauth/twitter/callback"


class TwitterOAuthError(RuntimeError):
    """Raised when Twitter OAuth cannot complete with safe provider details."""

    def __init__(self, message: str, *, status_code: int | None = None, error_code: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


def _get_session(session=None):
    if session is not None:
        return session
    if requests is None:
        raise RuntimeError("requests is required for Twitter OAuth helpers")
    return requests.Session()


def _required_env(name: str) -> str:
    value = str(os.environ.get(name, "") or "").strip()
    if not value:
        raise TwitterOAuthError(f"Missing required environment variable: {name}")
    return value


def default_redirect_uri() -> str:
    return str(os.environ.get(REDIRECT_URI_ENV) or DEFAULT_REDIRECT_URI).strip()


def build_state_token() -> str:
    return secrets.token_urlsafe(24)


def _pkce_challenge() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    scopes: tuple[str, ...] | list[str] = DEFAULT_SCOPES,
    code_challenge: str,
) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "state": state,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{X_AUTHORIZE_URL}?{query}"


def build_authorize_url_from_env(
    *,
    state: str,
    redirect_uri: str | None = None,
    scopes: tuple[str, ...] | list[str] = DEFAULT_SCOPES,
    client_id_env: str = CLIENT_ID_ENV,
) -> tuple[str, str, str]:
    """Return (authorize_url, code_verifier, state)."""
    verifier, challenge = _pkce_challenge()
    url = build_authorize_url(
        client_id=_required_env(client_id_env),
        redirect_uri=redirect_uri or default_redirect_uri(),
        state=state,
        scopes=scopes,
        code_challenge=challenge,
    )
    return url, verifier, state


def _basic_auth_header(client_id: str, client_secret: str) -> dict[str, str]:
    """Return Authorization header for HTTP Basic auth with client_id:client_secret."""
    token = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def exchange_code_for_token(
    *,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    client_id_env: str = CLIENT_ID_ENV,
    client_secret_env: str | None = CLIENT_SECRET_ENV,
    session=None,
) -> dict[str, Any]:
    http = _get_session(session)
    client_id = _required_env(client_id_env)
    client_secret = os.environ.get(client_secret_env or "", "") if client_secret_env else ""

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    # Twitter requires HTTP Basic auth (client_id:client_secret) for the
    # token endpoint — passing client_secret as a body param returns 401
    # "unauthorized_client: Missing valid authorization header".
    if client_secret:
        headers.update(_basic_auth_header(client_id, client_secret))

    _log.info("Exchanging Twitter OAuth code: POST %s with Basic auth=%s", X_TOKEN_URL, bool(client_secret))
    response = http.post(X_TOKEN_URL, data=data, headers=headers, timeout=120)
    if not response.ok:
        _log.error("Twitter token exchange failed: %s %s", response.status_code, response.text[:500])
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise TwitterOAuthError(str(payload.get("error_description") or payload["error"]))
    return payload


def _safe_refresh_error(response) -> TwitterOAuthError:
    """Build a credential-free X refresh error from an HTTP response."""
    status = getattr(response, "status_code", None)
    code = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                code = str(error.get("code") or error.get("error") or "").strip()
            else:
                code = str(error or "").strip()
    except Exception:  # noqa: BLE001
        pass
    # Provider descriptions can echo submitted values. Keep only a constrained
    # code and a generic status in persisted events; never store raw body or URL.
    allowed_codes = {"invalid_grant", "invalid_client", "invalid_request", "unauthorized_client", "unsupported_grant_type"}
    safe_code = code.lower() if code.lower() in allowed_codes else ""
    suffix = f" ({safe_code})" if safe_code else ""
    safe_status = status if isinstance(status, int) else "unknown"
    return TwitterOAuthError(
        f"X OAuth refresh failed: HTTP {safe_status}{suffix}",
        status_code=status if isinstance(status, int) else None,
        error_code=safe_code,
    )


def refresh_access_token(
    *,
    refresh_token: str,
    client_id_env: str = CLIENT_ID_ENV,
    client_secret_env: str | None = CLIENT_SECRET_ENV,
    session=None,
) -> dict[str, Any]:
    http = _get_session(session)
    client_id = _required_env(client_id_env)
    client_secret = os.environ.get(client_secret_env or "", "") if client_secret_env else ""

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if client_secret:
        headers.update(_basic_auth_header(client_id, client_secret))

    try:
        response = http.post(
            X_TOKEN_URL,
            data=data,
            headers=headers,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001 — suppress request URLs that may expose form values
        raise TwitterOAuthError("X OAuth refresh failed due to a transport error") from exc
    if not getattr(response, "ok", True):
        raise _safe_refresh_error(response)
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 — avoid raw response/URL in persisted events
        raise TwitterOAuthError("X OAuth refresh returned an unreadable response") from exc
    if payload.get("error"):
        code = str(payload.get("error") or "").lower()
        safe_code = code if code in {"invalid_grant", "invalid_client", "invalid_request", "unauthorized_client", "unsupported_grant_type"} else ""
        raise TwitterOAuthError(
            f"X OAuth refresh failed{f' ({safe_code})' if safe_code else ''}",
            status_code=getattr(response, "status_code", None),
            error_code=safe_code,
        )
    return payload


def fetch_user_info(*, access_token: str, session=None) -> dict[str, Any]:
    http = _get_session(session)
    response = http.get(
        X_ME_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"user.fields": "profile_image_url"},
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise TwitterOAuthError(str(payload["errors"]))
    return payload.get("data", payload)
