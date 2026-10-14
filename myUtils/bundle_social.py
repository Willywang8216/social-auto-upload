"""bundle.social client — the Reddit publishing route.

Why this exists
---------------
Reddit revoked the API credentials for both configured accounts (``401
Unauthorized`` / *"Please log in to do that."*), which blocked every Reddit
target. bundle.social holds its **own** Reddit OAuth app for the same
``u/ryanbossom`` account, so the same content can still be posted.

Reddit bans accounts for ignoring a subreddit's rules, and every past Reddit
failure here was knowable in advance (banned-from-sub, link whitelist, NO_SELFS,
missing flair). So this module does not "fire and see": before every submit it

  * reads the subreddit's post requirements (``isFlairRequired`` /
    ``isFlairEnabled``),
  * resolves the post flair when one is required — account config first, then the
    live flair list — and **refuses to post** when a required flair is unknown,
    because posting without it is a rule violation and a wasted submission,
  * refuses media the destination channel does not accept (native video, image or
    gallery), using the live channel metadata,
  * never exceeds the organization's monthly post quota (20 as configured) and
    says so instead of silently dropping posts.

Everything is driven by ``x-api-key`` against ``https://api.bundle.social``.
Docs: https://docs.bundle.social/api-reference/introduction
"""

from __future__ import annotations

import json
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

DEFAULT_API_URL = "https://api.bundle.social"
SIMPLE_UPLOAD_LIMIT = 90 * 1024 * 1024          # bundle.social's simple ceiling
MULTIPART_CHUNK = 64 * 1024 * 1024              # documented fixed part size
DEFAULT_MONTHLY_POST_CAP = 20
HTTP_TIMEOUT = 60.0

API_KEY_ENV = "BUNDLE_SOCIAL_API_KEY"
TEAM_ID_ENV = "BUNDLE_SOCIAL_TEAM_ID"
API_URL_ENV = "BUNDLE_SOCIAL_API_URL"
CAP_ENV = "SAU_BUNDLE_SOCIAL_MONTHLY_POST_CAP"
SUBS_PER_TARGET_ENV = "SAU_BUNDLE_SOCIAL_SUBREDDITS_PER_TARGET"

# The MCP/CLI use BUNDLE_SOCIAL_*; accept a SAU_-prefixed spelling too so a value
# can live next to the other SAU settings without surprising anyone.
_ALT_ENV = {
    API_KEY_ENV: "SAU_BUNDLE_SOCIAL_API_KEY",
    TEAM_ID_ENV: "SAU_BUNDLE_SOCIAL_TEAM_ID",
    API_URL_ENV: "SAU_BUNDLE_SOCIAL_API_URL",
}


class BundleSocialError(RuntimeError):
    """Any bundle.social failure. ``retryable`` drives the worker's backoff."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class BundleSocialNotConfigured(BundleSocialError):
    """No API key is set; the route is simply unavailable."""


class BundleSocialRuleError(BundleSocialError):
    """A subreddit rule forbids this post. Never retry — it cannot succeed."""


class BundleSocialQuotaError(BundleSocialError):
    """The monthly post quota is exhausted. Do not retry this month."""


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        alt = _ALT_ENV.get(name)
        if alt:
            value = os.environ.get(alt)
    value = (value or "").strip()
    return value or None


def api_key() -> str | None:
    return _env(API_KEY_ENV)


def _now_iso() -> str:
    """UTC ISO-8601 with milliseconds and a trailing Z, as bundle.social wants."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def api_base() -> str:
    return (_env(API_URL_ENV) or DEFAULT_API_URL).rstrip("/")


def configured_team_id() -> str | None:
    return _env(TEAM_ID_ENV)


def is_enabled() -> bool:
    """True when bundle.social can be used at all (an API key is present)."""
    return bool(api_key())


def monthly_post_cap() -> int:
    raw = _env(CAP_ENV)
    if raw is None:
        return DEFAULT_MONTHLY_POST_CAP
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MONTHLY_POST_CAP


def max_subreddits_per_target() -> int:
    """How many subreddits one target may post to.

    Defaults to 1 because the organization is capped at 20 posts/month and one
    target mapping to four subreddits would spend a fifth of the month on a
    single video. Raise it deliberately.
    """
    raw = _env(SUBS_PER_TARGET_ENV)
    if raw is None:
        return 1
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def normalize_subreddit(name: str) -> str:
    """``r/NudistMen`` / ``NudistMen`` -> ``NudistMen``; ``u/foo`` -> ``foo``."""
    value = str(name or "").strip()
    for prefix in ("r/", "u/"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
            break
    return value.strip()


def reference_key(target_id: Any, subreddit: str, attempt: int | None = None) -> str:
    """Idempotency key so a retry cannot double-post the same target+subreddit."""
    key = f"sau-reddit-{target_id}-{normalize_subreddit(subreddit).lower()}"
    if attempt is not None:
        key = f"{key}-a{int(attempt)}"
    return key[:128]


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #
def _session(session: Any = None):
    if session is not None:
        return session
    import requests

    return requests.Session()


def _headers() -> dict[str, str]:
    key = api_key()
    if not key:
        raise BundleSocialNotConfigured(
            f"{API_KEY_ENV} is not set; the bundle.social Reddit route is unavailable"
        )
    return {"x-api-key": key}


def _raise_for_status(response: Any, path: str) -> None:
    status = int(getattr(response, "status_code", 0) or 0)
    if status < 400 and status != 0:
        return
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, Mapping):
            detail = str(
                payload.get("message")
                or payload.get("error")
                or json.dumps(payload)[:300]
            )
        else:
            detail = str(payload)[:300]
    except Exception:  # noqa: BLE001 - the body is best-effort context
        detail = str(getattr(response, "text", ""))[:300]
    # A 4xx is the caller's fault and will fail again unless the request changes;
    # 5xx and 429 are worth another attempt. bundle.social returns clear 400s for
    # rule violations, so keep them non-retryable.
    retryable = status >= 500 or status == 429
    raise BundleSocialError(
        f"bundle.social {path} failed (HTTP {status})"
        + (f": {detail}" if detail else ""),
        retryable=retryable,
    )


def _request(
    method: str,
    path: str,
    *,
    params: Mapping[str, Any] | None = None,
    json_body: Any = None,
    files: Any = None,
    session: Any = None,
    timeout: float | None = None,
) -> Any:
    http = _session(session)
    url = f"{api_base()}{path}"
    response = http.request(
        method,
        url,
        headers=_headers(),
        params=dict(params or {}),
        json=json_body,
        files=files,
        timeout=timeout or HTTP_TIMEOUT,
    )
    _raise_for_status(response, path)
    status = int(getattr(response, "status_code", 0) or 0)
    if status == 204:
        return None
    try:
        return response.json()
    except Exception:  # noqa: BLE001 - a 200 with no body is fine
        return None


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
def list_teams(*, session: Any = None) -> list[dict[str, Any]]:
    payload = _request("GET", "/api/v1/team/", params={"limit": 50}, session=session)
    if isinstance(payload, Mapping):
        items = payload.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, Mapping)]
    return []


def resolve_team_id(*, session: Any = None) -> str:
    configured = configured_team_id()
    if configured:
        return configured
    teams = list_teams(session=session)
    if not teams:
        raise BundleSocialError(
            "no bundle.social team is available; set BUNDLE_SOCIAL_TEAM_ID",
            retryable=False,
        )
    if len(teams) > 1:
        raise BundleSocialError(
            "the organization has more than one team; set BUNDLE_SOCIAL_TEAM_ID "
            "(candidates: " + ", ".join(str(t.get("id")) for t in teams) + ")",
            retryable=False,
        )
    return str(teams[0].get("id"))


def get_social_account(platform_type: str, *, team_id: str | None = None, session: Any = None) -> dict[str, Any]:
    team = team_id or resolve_team_id(session=session)
    payload = _request(
        "GET",
        "/api/v1/social-account/by-type",
        params={"type": platform_type, "teamId": team},
        session=session,
    )
    if not isinstance(payload, Mapping):
        raise BundleSocialError(
            f"no {platform_type} account is connected to bundle.social team {team}",
            retryable=False,
        )
    return dict(payload)


def get_reddit_account(*, team_id: str | None = None, session: Any = None) -> dict[str, Any]:
    return get_social_account("REDDIT", team_id=team_id, session=session)


def reddit_channels(account: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Map normalized subreddit name -> channel metadata (allow* flags)."""
    channels: dict[str, dict[str, Any]] = {}
    for channel in account.get("channels") or []:
        if not isinstance(channel, Mapping):
            continue
        username = str(channel.get("username") or "")
        name = normalize_subreddit(username)
        if not name:
            continue
        channels[name.lower()] = {
            "username": username,
            "name": channel.get("name"),
            "metadata": dict(channel.get("metadata") or {}),
        }
    return channels


def reddit_post_requirements(
    subreddit: str, *, team_id: str | None = None, session: Any = None
) -> dict[str, Any]:
    team = team_id or resolve_team_id(session=session)
    payload = _request(
        "GET",
        "/api/v1/misc/reddit/post-requirements",
        params={"teamId": team, "subreddit": normalize_subreddit(subreddit)},
        session=session,
    )
    return dict(payload) if isinstance(payload, Mapping) else {}


def reddit_subreddit_flairs(
    subreddit: str, *, team_id: str | None = None, session: Any = None
) -> list[dict[str, Any]]:
    team = team_id or resolve_team_id(session=session)
    payload = _request(
        "GET",
        "/api/v1/misc/reddit/subreddit-flairs",
        params={"teamId": team, "subreddit": normalize_subreddit(subreddit)},
        session=session,
    )
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    return []


# --------------------------------------------------------------------------- #
# usage / quota
# --------------------------------------------------------------------------- #
def org_posts_usage(*, session: Any = None) -> dict[str, Any]:
    payload = _request("GET", "/api/v1/organization/usage/posts", session=session)
    return dict(payload) if isinstance(payload, Mapping) else {}


def remaining_posts(*, session: Any = None) -> int:
    """Posts still allowed this month, bounded by our own configured cap.

    The organization reports its own ``limit``; ``SAU_BUNDLE_SOCIAL_MONTHLY_POST_CAP``
    lets the operator hold back headroom without changing the plan.
    """
    usage = org_posts_usage(session=session)
    try:
        used = int(usage.get("used") or 0)
    except (TypeError, ValueError):
        used = 0
    provider_limit = usage.get("limit")
    try:
        provider_remaining = int(usage.get("remaining"))
    except (TypeError, ValueError):
        provider_remaining = None
    if provider_remaining is None:
        try:
            provider_remaining = max(0, int(provider_limit) - used)
        except (TypeError, ValueError):
            provider_remaining = monthly_post_cap() - used
    return max(0, min(monthly_post_cap() - used, provider_remaining))


def get_post_by_reference_key(key: str, *, session: Any = None) -> dict[str, Any] | None:
    http = _session(session)
    response = http.get(
        f"{api_base()}/api/v1/post/reference-key/{key}",
        headers=_headers(),
        timeout=HTTP_TIMEOUT,
    )
    if int(getattr(response, "status_code", 0) or 0) == 404:
        return None
    _raise_for_status(response, f"/api/v1/post/reference-key/{key}")
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


# --------------------------------------------------------------------------- #
# media
# --------------------------------------------------------------------------- #
def upload_from_url(url: str, *, team_id: str | None = None, session: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"url": url}
    team = team_id or configured_team_id()
    if team:
        body["teamId"] = team
    payload = _request("POST", "/api/v1/upload/from-url", json_body=body, session=session)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _mime_for(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    suffix = path.suffix.lower()
    if suffix in {".mp4", ".m4v"}:
        return "video/mp4"
    if suffix in {".mov", ".qt"}:
        return "video/quicktime"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".gif":
        return "image/gif"
    return mime or "application/octet-stream"


def upload_file_simple(path: Path, *, team_id: str | None = None, session: Any = None) -> dict[str, Any]:
    http = _session(session)
    data = {}
    team = team_id or configured_team_id()
    if team:
        data["teamId"] = team
    with path.open("rb") as handle:
        files = {"file": (path.name, handle, _mime_for(path))}
        response = http.post(
            f"{api_base()}/api/v1/upload/",
            headers=_headers(),
            data=data,
            files=files,
            timeout=600.0,
        )
    _raise_for_status(response, "/api/v1/upload/")
    return dict(response.json())


def upload_file_multipart(path: Path, *, team_id: str | None = None, session: Any = None) -> dict[str, Any]:
    """64 MiB chunked upload; a dropped chunk costs one chunk, not the file."""
    http = _session(session)
    team = team_id or configured_team_id()
    size = path.stat().st_size
    init_body: dict[str, Any] = {
        "fileName": path.name,
        "mimeType": _mime_for(path),
        "fileSize": size,
    }
    if team:
        init_body["teamId"] = team
    init = _request("POST", "/api/v1/upload/multipart/init", json_body=init_body, session=http)
    upload_id = str(init.get("uploadId") or "")
    object_path = str(init.get("path") or "")
    part_size = int(init.get("partSize") or MULTIPART_CHUNK)
    parts = list(init.get("parts") or [])
    if not upload_id or not object_path or not parts:
        raise BundleSocialError("bundle.social multipart init returned an incomplete plan")
    completed: list[dict[str, Any]] = []
    with path.open("rb") as handle:
        for part in parts:
            number = int(part.get("partNumber"))
            url = str(part.get("url") or "")
            if not url:
                raise BundleSocialError(f"bundle.social multipart part {number} has no url")
            handle.seek((number - 1) * part_size)
            chunk = handle.read(part_size)
            put = http.put(url, data=chunk, timeout=600.0)
            if int(getattr(put, "status_code", 0) or 0) >= 400:
                _raise_for_status(put, f"/upload part {number}")
            etag = put.headers.get("ETag") or put.headers.get("etag")
            if not etag:
                raise BundleSocialError(f"bundle.social upload part {number} returned no ETag")
            completed.append({"partNumber": number, "etag": etag})
    complete_body: dict[str, Any] = {
        "path": object_path,
        "uploadId": upload_id,
        "parts": completed,
    }
    if team:
        complete_body["teamId"] = team
    payload = _request(
        "POST", "/api/v1/upload/multipart/complete", json_body=complete_body, session=http
    )
    return dict(payload) if isinstance(payload, Mapping) else {}


def _url_is_fetchable(url: str, *, session: Any = None, timeout: float = 20.0) -> bool:
    if not str(url or "").strip():
        return False
    http = _session(session)
    try:
        response = http.get(url, timeout=timeout, allow_redirects=True, stream=True)
        status = int(getattr(response, "status_code", 0) or 0)
        response.close()
        return 200 <= status < 400
    except Exception:  # noqa: BLE001 - an unreachable URL is just "not usable"
        return False


def upload_media(
    *,
    path: str | Path | None = None,
    url: str | None = None,
    team_id: str | None = None,
    session: Any = None,
) -> str:
    """Return an upload id for the media, preferring the cheapest safe path."""
    local = Path(path) if path else None
    if url and _url_is_fetchable(str(url), session=session):
        try:
            uploaded = upload_from_url(str(url), team_id=team_id, session=session)
            upload_id = str(uploaded.get("id") or "")
            if upload_id:
                return upload_id
        except BundleSocialError:
            # Fall through to a local upload; a URL bundle.social cannot fetch is
            # exactly the failure mode that produced dead-link posts before.
            pass
    if local is None or not local.exists():
        raise BundleSocialError(
            "bundle.social has no fetchable URL and no local file to upload",
            retryable=True,
        )
    if local.stat().st_size <= SIMPLE_UPLOAD_LIMIT:
        uploaded = upload_file_simple(local, team_id=team_id, session=session)
    else:
        uploaded = upload_file_multipart(local, team_id=team_id, session=session)
    upload_id = str(uploaded.get("id") or "")
    if not upload_id:
        raise BundleSocialError("bundle.social upload returned no id")
    return upload_id


# --------------------------------------------------------------------------- #
# rule enforcement
# --------------------------------------------------------------------------- #
def subreddit_denial(
    channels: Mapping[str, Mapping[str, Any]],
    subreddit: str,
    *,
    media_kind: str | None,
    is_gallery: bool = False,
) -> str | None:
    """Return the rule that forbids this media, or None when it is allowed.

    ``media_kind`` is ``"video"``, ``"image"`` or None (text/link only). Only the
    destination's own published ``allow*`` flags are consulted; an unknown
    channel is *not* denied here (the destination may still be valid) so the
    caller keeps the final say.
    """
    channel = channels.get(normalize_subreddit(subreddit).lower())
    if channel is None:
        return None
    metadata = channel.get("metadata") or {}
    if media_kind == "video" and not metadata.get("allowVideos"):
        return f"r/{normalize_subreddit(subreddit)} does not allow native video posts"
    if media_kind == "image":
        if is_gallery and not metadata.get("allowGalleries"):
            return f"r/{normalize_subreddit(subreddit)} does not allow image galleries"
        if not is_gallery and not metadata.get("allowImages"):
            return f"r/{normalize_subreddit(subreddit)} does not allow image posts"
    return None


def _match_flair(
    flairs: Sequence[Mapping[str, Any]], wanted: str | None
) -> Mapping[str, Any] | None:
    if not flairs:
        return None
    target = str(wanted or "").strip().lower()
    if target:
        for flair in flairs:
            if str(flair.get("text") or "").strip().lower() == target:
                return flair
        for flair in flairs:
            text = str(flair.get("text") or "").strip().lower()
            if text and (target in text or text in target):
                return flair
    available = [f for f in flairs if f.get("id")]
    if len(available) == 1:
        return available[0]
    return None


def resolve_flair_id(
    subreddit: str,
    *,
    requirements: Mapping[str, Any],
    config_flair_ids: Mapping[str, Any] | None = None,
    content_kind: str | None = None,
    team_id: str | None = None,
    session: Any = None,
) -> str | None:
    """Resolve the flair the subreddit requires, checking config then the API.

    Raises :class:`BundleSocialRuleError` when the subreddit *requires* a flair
    and no id can be found — posting anyway is a guaranteed rejection and a rule
    violation, so we refuse instead of guessing.
    """
    sub = normalize_subreddit(subreddit)
    mapping = (config_flair_ids or {}).get(sub)
    if isinstance(mapping, Mapping) and mapping:
        if content_kind:
            for label, flair_id in mapping.items():
                if str(label).strip().lower() == str(content_kind).strip().lower():
                    return str(flair_id)
        # A configured mapping with exactly one entry is unambiguous.
        if len(mapping) == 1:
            return str(next(iter(mapping.values())))
    elif isinstance(mapping, str) and mapping:
        return mapping

    flairs = reddit_subreddit_flairs(sub, team_id=team_id, session=session)
    match = _match_flair(flairs, content_kind)
    if match and match.get("id"):
        return str(match["id"])

    if requirements.get("isFlairRequired"):
        raise BundleSocialRuleError(
            f"r/{sub} requires a post flair but none could be resolved; refusing "
            "to post without it (add the label->id to the account's flairIds, or "
            "set the exact flair text as the content kind)",
            retryable=False,
        )
    return None


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #
def create_reddit_post(
    *,
    subreddit: str,
    title: str,
    body: str = "",
    nsfw: bool = True,
    flair_id: str | None = None,
    upload_ids: Sequence[str] | None = None,
    link: str | None = None,
    team_id: str | None = None,
    status: str = "SCHEDULED",
    scheduled_at: str | None = None,
    reference: str | None = None,
    session: Any = None,
) -> dict[str, Any]:
    """Create one Reddit post. The caller has already enforced the rules."""
    sub = normalize_subreddit(subreddit)
    if not sub:
        raise BundleSocialRuleError("a Reddit post needs a subreddit", retryable=False)
    if not str(title or "").strip():
        raise BundleSocialRuleError("a Reddit post needs a title", retryable=False)
    reddit: dict[str, Any] = {
        "sr": f"r/{sub}",
        "text": str(title).strip()[:300],
        "nsfw": bool(nsfw),
    }
    if body:
        reddit["description"] = str(body)[:30000]
    if upload_ids:
        reddit["uploadIds"] = list(upload_ids)
    if link:
        reddit["link"] = str(link)[:2048]
    if flair_id:
        reddit["flairId"] = str(flair_id)

    team = team_id or resolve_team_id(session=session)
    body_payload: dict[str, Any] = {
        "teamId": team,
        "title": str(title).strip()[:300],
        "status": status if status in {"DRAFT", "SCHEDULED"} else "SCHEDULED",
        "socialAccountTypes": ["REDDIT"],
        "data": {"REDDIT": reddit},
    }
    if reference:
        body_payload["referenceKey"] = reference[:128]
    # bundle.social validates ``postDate`` as required whenever the status is
    # SCHEDULED (a 400 with ``issues:[{path:[postDate],message:Required}]``).
    # Set it to now when the caller did not schedule, which is the immediate
    # publish the publisher wants.
    body_payload["postDate"] = scheduled_at or _now_iso()
    payload = _request("POST", "/api/v1/post/", json_body=body_payload, session=session)
    return dict(payload) if isinstance(payload, Mapping) else {}


def publish_reddit_post(
    *,
    subreddit: str,
    title: str,
    body: str = "",
    media_path: str | Path | None = None,
    media_url: str | None = None,
    media_kind: str | None = None,
    is_gallery: bool = False,
    link: str | None = None,
    content_kind: str | None = None,
    nsfw: bool = True,
    config_flair_ids: Mapping[str, Any] | None = None,
    flair_id: str | None = None,
    reference: str | None = None,
    status: str = "SCHEDULED",
    scheduled_at: str | None = None,
    team_id: str | None = None,
    account: Mapping[str, Any] | None = None,
    session: Any = None,
) -> dict[str, Any]:
    """Enforce every rule, then submit. Raises on any violation.

    This is the only place a Reddit post is created, so the checks cannot be
    bypassed by a caller that forgets one.
    """
    if not is_enabled():
        raise BundleSocialNotConfigured("bundle.social is not configured")
    team = team_id or resolve_team_id(session=session)
    account = account or get_reddit_account(team_id=team, session=session)
    channels = reddit_channels(account)

    denial = subreddit_denial(
        channels, subreddit, media_kind=media_kind, is_gallery=is_gallery
    )
    if denial:
        raise BundleSocialRuleError(denial, retryable=False)

    # A retry must not duplicate a post that already went out. Rediscover an
    # existing submission by reference key first.
    if reference:
        existing = get_post_by_reference_key(reference, session=session)
        if existing:
            existing_status = str(existing.get("status") or "").upper()
            if existing_status in {"POSTED", "SCHEDULED", "PROCESSING", "REVIEW"}:
                return {
                    "post_id": existing.get("id"),
                    "status": existing_status,
                    "deduplicated": True,
                    "subreddit": subreddit,
                }

    if remaining_posts(session=session) <= 0:
        raise BundleSocialQuotaError(
            f"bundle.social monthly post quota reached "
            f"({monthly_post_cap()}/month); not posting to r/{normalize_subreddit(subreddit)}",
            retryable=False,
        )

    upload_ids: list[str] = []
    if media_kind in {"video", "image"}:
        try:
            upload_ids = [
                upload_media(
                    path=media_path, url=media_url, team_id=team, session=session
                )
            ]
        except BundleSocialError:
            raise
    requirements = reddit_post_requirements(subreddit, team_id=team, session=session)
    if flair_id is None:
        flair_id = resolve_flair_id(
            subreddit,
            requirements=requirements,
            config_flair_ids=config_flair_ids,
            content_kind=content_kind,
            team_id=team,
            session=session,
        )
    result = create_reddit_post(
        subreddit=subreddit,
        title=title,
        body=body,
        nsfw=nsfw,
        flair_id=flair_id,
        upload_ids=upload_ids,
        link=link,
        team_id=team,
        status=status,
        scheduled_at=scheduled_at,
        reference=reference,
        session=session,
    )
    return {
        "post_id": result.get("id"),
        "status": str(result.get("status") or status).upper(),
        "subreddit": normalize_subreddit(subreddit),
        "flair_id": flair_id,
        "upload_ids": upload_ids,
    }
