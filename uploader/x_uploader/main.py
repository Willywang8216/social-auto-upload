import base64
import hashlib
import json
import mimetypes
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

import conf


X_AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
X_TOKEN_ENDPOINTS = (
    "https://api.x.com/2/oauth2/token",
    "https://api.twitter.com/2/oauth2/token",
)
X_API_BASES = (
    "https://api.x.com/2",
    "https://api.twitter.com/2",
)
DEFAULT_X_SCOPES = (
    "tweet.read",
    "tweet.write",
    "users.read",
    "offline.access",
    "media.write",
)


class XAPIError(Exception):
    pass


def _get_client_id():
    return getattr(conf, "X_CLIENT_ID", "")


def _get_client_secret():
    return getattr(conf, "X_CLIENT_SECRET", "")


def _get_redirect_uri():
    return getattr(conf, "X_REDIRECT_URI", "")


def _now_ts():
    return int(time.time())


def _safe_json(response):
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text}


def _raise_response_error(response, fallback_message):
    payload = _safe_json(response)
    detail = payload.get("detail") or payload.get("title") or payload.get("error_description") or payload.get("error") or fallback_message
    raise XAPIError(f"{detail} (status={response.status_code})")


def _request_json(method, urls, *, headers=None, data=None, params=None, files=None, json_body=None, timeout=30):
    last_error = None
    with httpx.Client(timeout=timeout) as client:
        for url in urls:
            try:
                response = client.request(
                    method,
                    url,
                    headers=headers,
                    data=data,
                    params=params,
                    files=files,
                    json=json_body,
                )
            except httpx.HTTPError as exc:
                last_error = exc
                continue
            if response.status_code >= 400:
                payload = _safe_json(response)
                message = payload.get("detail") or payload.get("title") or payload.get("error_description") or payload.get("error")
                last_error = XAPIError(f"{message or 'X API request failed'} (status={response.status_code}, url={url})")
                continue
            return _safe_json(response)
    if last_error:
        raise XAPIError(str(last_error))
    raise XAPIError("X API request failed")


def build_pkce_pair():
    code_verifier = secrets.token_urlsafe(64)
    challenge = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    code_challenge = base64.urlsafe_b64encode(challenge).decode("utf-8").rstrip("=")
    return code_verifier, code_challenge


def build_authorize_url(state, code_challenge, redirect_uri=None, client_id=None, scopes=None):
    redirect_uri = redirect_uri or _get_redirect_uri()
    client_id = client_id or _get_client_id()
    scopes = scopes or DEFAULT_X_SCOPES
    if not redirect_uri or not client_id:
        raise XAPIError("X OAuth configuration is incomplete")

    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{X_AUTHORIZE_URL}?{query}"


def _token_headers(client_id, client_secret):
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if client_secret:
        encoded = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")
        headers["Authorization"] = f"Basic {encoded}"
    return headers


def exchange_code_for_tokens(code, code_verifier, redirect_uri=None, client_id=None, client_secret=None):
    redirect_uri = redirect_uri or _get_redirect_uri()
    client_id = client_id or _get_client_id()
    client_secret = client_secret if client_secret is not None else _get_client_secret()
    if not code or not code_verifier:
        raise XAPIError("Missing X OAuth code or PKCE verifier")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    if not client_secret:
        data["client_id"] = client_id

    token_data = _request_json(
        "POST",
        X_TOKEN_ENDPOINTS,
        headers=_token_headers(client_id, client_secret),
        data=data,
    )
    token_data["expires_at"] = _now_ts() + int(token_data.get("expires_in", 0))
    return token_data


def refresh_access_token(refresh_token, client_id=None, client_secret=None):
    client_id = client_id or _get_client_id()
    client_secret = client_secret if client_secret is not None else _get_client_secret()
    if not refresh_token:
        raise XAPIError("Missing X refresh token")

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if not client_secret:
        data["client_id"] = client_id

    token_data = _request_json(
        "POST",
        X_TOKEN_ENDPOINTS,
        headers=_token_headers(client_id, client_secret),
        data=data,
    )
    token_data["expires_at"] = _now_ts() + int(token_data.get("expires_in", 0))
    return token_data


def get_current_user(access_token):
    payload = _request_json(
        "GET",
        [f"{base}/users/me" for base in X_API_BASES],
        headers={"Authorization": f"Bearer {access_token}"},
        params={"user.fields": "id,name,username,profile_image_url"},
    )
    user = payload.get("data")
    if not user:
        raise XAPIError("X identity response did not include user data")
    return user


def create_x_credentials(token_data, user_data):
    return {
        "platform": "x",
        "auth_mode": "oauth2",
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token", ""),
        "token_type": token_data.get("token_type", "bearer"),
        "scope": token_data.get("scope", " ".join(DEFAULT_X_SCOPES)),
        "expires_at": token_data.get("expires_at", 0),
        "created_at": _now_ts(),
        "external_user_id": user_data.get("id", ""),
        "external_username": user_data.get("username", ""),
        "external_name": user_data.get("name", ""),
        "profile_image_url": user_data.get("profile_image_url", ""),
    }


def load_x_credentials(file_path):
    with open(file_path, "r", encoding="utf-8") as fp:
        return json.load(fp)


def save_x_credentials(file_path, credentials):
    file_path = Path(file_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as fp:
        json.dump(credentials, fp, ensure_ascii=False, indent=2)


def validate_x_credentials(credentials):
    credentials = dict(credentials)
    expires_at = int(credentials.get("expires_at", 0) or 0)
    refresh_token = credentials.get("refresh_token", "")
    if expires_at and expires_at <= _now_ts() + 60:
        if not refresh_token:
            raise XAPIError("X access token has expired and no refresh token is available")
        refreshed = refresh_access_token(refresh_token)
        credentials["access_token"] = refreshed["access_token"]
        credentials["refresh_token"] = refreshed.get("refresh_token", refresh_token)
        credentials["token_type"] = refreshed.get("token_type", credentials.get("token_type", "bearer"))
        credentials["scope"] = refreshed.get("scope", credentials.get("scope", " ".join(DEFAULT_X_SCOPES)))
        credentials["expires_at"] = refreshed.get("expires_at", credentials.get("expires_at", 0))

    user_data = get_current_user(credentials["access_token"])
    credentials["external_user_id"] = user_data.get("id", credentials.get("external_user_id", ""))
    credentials["external_username"] = user_data.get("username", credentials.get("external_username", ""))
    credentials["external_name"] = user_data.get("name", credentials.get("external_name", ""))
    credentials["profile_image_url"] = user_data.get("profile_image_url", credentials.get("profile_image_url", ""))
    credentials["validated_at"] = _now_ts()
    return credentials


def _guess_media_category(file_path):
    mime_type, _ = mimetypes.guess_type(str(file_path))
    if not mime_type:
        return None
    if mime_type.startswith("image/"):
        return "tweet_image"
    if mime_type.startswith("video/"):
        return "tweet_video"
    if mime_type == "image/gif":
        return "tweet_gif"
    return None


def upload_media(access_token, file_path):
    file_path = Path(file_path)
    if not file_path.exists():
        raise XAPIError(f"Media file does not exist: {file_path}")

    media_category = _guess_media_category(file_path)
    mime_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    with open(file_path, "rb") as media_fp:
        files = {
            "media": (file_path.name, media_fp, mime_type),
        }
        data = {}
        if media_category:
            data["media_category"] = media_category
        payload = _request_json(
            "POST",
            [f"{base}/media/upload" for base in X_API_BASES],
            headers={"Authorization": f"Bearer {access_token}"},
            data=data,
            files=files,
            timeout=120,
        )
    media_data = payload.get("data", {})
    media_id = media_data.get("id") or media_data.get("media_id")
    if not media_id:
        raise XAPIError("X media upload succeeded but did not return a media id")
    return media_id


def create_post(access_token, text, media_ids=None):
    payload = {"text": text}
    if media_ids:
        payload["media"] = {"media_ids": media_ids}
    response_data = _request_json(
        "POST",
        [f"{base}/tweets" for base in X_API_BASES],
        headers={"Authorization": f"Bearer {access_token}"},
        json_body=payload,
    )
    if "data" not in response_data:
        raise XAPIError("X post creation response did not include data")
    return response_data["data"]


def publish_post(credentials, text, media_paths=None):
    validated_credentials = validate_x_credentials(credentials)
    media_ids = []
    for media_path in media_paths or []:
        media_ids.append(upload_media(validated_credentials["access_token"], media_path))
    post_data = create_post(validated_credentials["access_token"], text, media_ids=media_ids)
    return post_data, validated_credentials
