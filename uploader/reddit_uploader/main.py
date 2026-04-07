import base64
import json
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

import conf


REDDIT_AUTHORIZE_URL = "https://www.reddit.com/api/v1/authorize"
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_API_BASE_URL = "https://oauth.reddit.com"
DEFAULT_REDDIT_SCOPES = ("identity", "submit")


class RedditAPIError(Exception):
    pass


def _get_client_id():
    return getattr(conf, "REDDIT_CLIENT_ID", "")


def _get_client_secret():
    return getattr(conf, "REDDIT_CLIENT_SECRET", "")


def _get_redirect_uri():
    return getattr(conf, "REDDIT_REDIRECT_URI", "")


def _get_user_agent():
    app_base_url = getattr(conf, "APP_BASE_URL", "")
    suffix = app_base_url or "local"
    return f"social-auto-upload/1.0 ({suffix})"


def _now_ts():
    return int(time.time())


def _safe_json(response):
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text}


def _basic_auth_header(client_id, client_secret):
    encoded = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")
    return f"Basic {encoded}"


def _request_json(method, url, *, headers=None, data=None, params=None, timeout=30):
    try:
        response = httpx.request(method, url, headers=headers, data=data, params=params, timeout=timeout)
    except httpx.HTTPError as exc:
        raise RedditAPIError(str(exc)) from exc

    if response.status_code >= 400:
        payload = _safe_json(response)
        message = payload.get("message") or payload.get("error_description") or payload.get("error") or payload.get("reason") or "Reddit API request failed"
        raise RedditAPIError(f"{message} (status={response.status_code})")

    return _safe_json(response)


def build_authorize_url(state, redirect_uri=None, client_id=None, scopes=None, duration="permanent"):
    redirect_uri = redirect_uri or _get_redirect_uri()
    client_id = client_id or _get_client_id()
    scopes = scopes or DEFAULT_REDDIT_SCOPES
    if not redirect_uri or not client_id:
        raise RedditAPIError("Reddit OAuth configuration is incomplete")

    query = urlencode(
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


def exchange_code_for_tokens(code, redirect_uri=None, client_id=None, client_secret=None):
    redirect_uri = redirect_uri or _get_redirect_uri()
    client_id = client_id or _get_client_id()
    client_secret = client_secret if client_secret is not None else _get_client_secret()
    if not client_id or not client_secret:
        raise RedditAPIError("Reddit OAuth client_id/client_secret are required")
    if not code:
        raise RedditAPIError("Missing Reddit OAuth code")

    token_data = _request_json(
        "POST",
        REDDIT_TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(client_id, client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": _get_user_agent(),
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
    )
    token_data["expires_at"] = _now_ts() + int(token_data.get("expires_in", 0))
    return token_data


def refresh_access_token(refresh_token, client_id=None, client_secret=None):
    client_id = client_id or _get_client_id()
    client_secret = client_secret if client_secret is not None else _get_client_secret()
    if not client_id or not client_secret:
        raise RedditAPIError("Reddit OAuth client_id/client_secret are required")
    if not refresh_token:
        raise RedditAPIError("Missing Reddit refresh token")

    token_data = _request_json(
        "POST",
        REDDIT_TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(client_id, client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": _get_user_agent(),
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    token_data["expires_at"] = _now_ts() + int(token_data.get("expires_in", 0))
    return token_data


def _oauth_headers(access_token):
    return {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": _get_user_agent(),
    }


def get_current_user(access_token):
    payload = _request_json(
        "GET",
        f"{REDDIT_API_BASE_URL}/api/v1/me",
        headers=_oauth_headers(access_token),
    )
    if not payload:
        raise RedditAPIError("Reddit identity response was empty")
    return payload


def create_reddit_credentials(token_data, user_data):
    return {
        "platform": "reddit",
        "auth_mode": "oauth2",
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token", ""),
        "token_type": token_data.get("token_type", "bearer"),
        "scope": token_data.get("scope", " ".join(DEFAULT_REDDIT_SCOPES)),
        "expires_at": token_data.get("expires_at", 0),
        "created_at": _now_ts(),
        "external_user_id": user_data.get("id", ""),
        "external_username": user_data.get("name", ""),
        "external_name": user_data.get("subreddit", {}).get("title", ""),
        "icon_img": user_data.get("icon_img", ""),
    }


def load_reddit_credentials(file_path):
    with open(file_path, "r", encoding="utf-8") as fp:
        return json.load(fp)


def save_reddit_credentials(file_path, credentials):
    file_path = Path(file_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as fp:
        json.dump(credentials, fp, ensure_ascii=False, indent=2)


def validate_reddit_credentials(credentials):
    credentials = dict(credentials)
    expires_at = int(credentials.get("expires_at", 0) or 0)
    refresh_token = credentials.get("refresh_token", "")
    if expires_at and expires_at <= _now_ts() + 60:
        if not refresh_token:
            raise RedditAPIError("Reddit access token has expired and no refresh token is available")
        refreshed = refresh_access_token(refresh_token)
        credentials["access_token"] = refreshed["access_token"]
        credentials["refresh_token"] = refreshed.get("refresh_token", refresh_token)
        credentials["token_type"] = refreshed.get("token_type", credentials.get("token_type", "bearer"))
        credentials["scope"] = refreshed.get("scope", credentials.get("scope", " ".join(DEFAULT_REDDIT_SCOPES)))
        credentials["expires_at"] = refreshed.get("expires_at", credentials.get("expires_at", 0))

    user_data = get_current_user(credentials["access_token"])
    credentials["external_user_id"] = user_data.get("id", credentials.get("external_user_id", ""))
    credentials["external_username"] = user_data.get("name", credentials.get("external_username", ""))
    credentials["external_name"] = user_data.get("subreddit", {}).get("title", credentials.get("external_name", ""))
    credentials["icon_img"] = user_data.get("icon_img", credentials.get("icon_img", ""))
    credentials["validated_at"] = _now_ts()
    return credentials


def _normalize_subreddit(subreddit):
    subreddit = (subreddit or "").strip()
    if subreddit.lower().startswith("r/"):
        return subreddit[2:]
    return subreddit


def submit_post(credentials, subreddit, title, post_kind, body="", url="", send_replies=True, nsfw=False, spoiler=False, resubmit=True, media_paths=None):
    if media_paths:
        raise RedditAPIError("Reddit native media posting is not implemented in phase 1")

    post_kind = (post_kind or "").strip().lower()
    subreddit = _normalize_subreddit(subreddit)
    if post_kind not in {"self", "link"}:
        raise RedditAPIError(f"Unsupported Reddit post kind: {post_kind}")
    if not subreddit:
        raise RedditAPIError("Subreddit is required")
    if not title:
        raise RedditAPIError("Reddit title is required")
    if post_kind == "self" and not body:
        raise RedditAPIError("Reddit self posts require body content")
    if post_kind == "link" and not url:
        raise RedditAPIError("Reddit link posts require a URL")

    validated_credentials = validate_reddit_credentials(credentials)
    payload = {
        "api_type": "json",
        "sr": subreddit,
        "kind": post_kind,
        "title": title,
        "sendreplies": "true" if send_replies else "false",
        "nsfw": "true" if nsfw else "false",
        "spoiler": "true" if spoiler else "false",
        "resubmit": "true" if resubmit else "false",
    }
    if post_kind == "self":
        payload["text"] = body
    else:
        payload["url"] = url

    response_data = _request_json(
        "POST",
        f"{REDDIT_API_BASE_URL}/api/submit",
        headers={
            **_oauth_headers(validated_credentials["access_token"]),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=payload,
    )
    json_payload = response_data.get("json", response_data)
    errors = json_payload.get("errors", [])
    if errors:
        raise RedditAPIError(str(errors))
    return json_payload.get("data", {}), validated_credentials
