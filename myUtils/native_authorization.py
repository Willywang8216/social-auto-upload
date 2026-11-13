"""Read-only native publishing authorization for the configured Meta routes.

No refresh, credential mutation, upload grant or post creation. Identity reads
alone are never authorization. Return normalized capabilities and expiry only.
"""
from __future__ import annotations

from datetime import datetime, timezone

from myUtils import meta_auth, prepared_publishers as publishers, threads_auth, token_state

SUPPORTED_PLATFORMS = frozenset({"facebook", "instagram", "threads"})


def _get(http, url, **kwargs):
    response = http.get(url, **kwargs)
    body = token_state.response_payload(response)
    if not isinstance(body, dict):
        raise ValueError("Provider authorization response must be an object")
    return body


def _scopes(value):
    if not isinstance(value, list) or not value or any(not isinstance(v, str) or not v for v in value):
        raise ValueError("Current native publishing scopes are unverified")
    return set(value)


def _grant_expiry(grant):
    if not isinstance(grant, dict):
        raise ValueError("Current native publishing grant is malformed")
    if grant.get("is_valid") is not True:
        raise ValueError("Current native token validity is unverified")
    deadlines = []
    for key in ("expires_at", "data_access_expires_at"):
        value = meta_auth._epoch(grant.get(key))
        if key == "expires_at" and value is None or key in grant and value is None:
            raise ValueError("Current native token expiry is unverified")
        if value:
            try:
                deadline = datetime.fromtimestamp(value, tz=timezone.utc)
            except (ValueError, OverflowError, OSError) as exc:
                raise ValueError("Current native token expiry is invalid") from exc
            if deadline <= token_state.utc_now():
                raise ValueError("Current native token or data access has expired")
            deadlines.append(deadline)
    return min(deadlines).isoformat() if deadlines else None


def _page_grants(grant, scopes, page_id, *, require_target=False):
    granular = grant.get("granular_scopes", [])
    if not isinstance(granular, list) or any(not isinstance(item, dict) for item in granular):
        raise ValueError("Exact Page grants are malformed")
    targeted = {}
    for item in granular:
        scope = item.get("scope")
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("Exact Page grants are malformed")
        if "target_ids" in item:
            ids = item["target_ids"]
            if not isinstance(ids, list) or any(not isinstance(value, str) or not value.strip() for value in ids):
                raise ValueError("Exact Page grants are malformed")
            targeted.setdefault(scope, []).append(ids)
    # Global scopes remain bound by the exact PAGE identity/user Page tasks.
    # An optional capability cannot use a global scope to bypass supplied targets.
    return all((scope in targeted or not (require_target and targeted))
               and all(page_id in ids for ids in targeted.get(scope, [])) for scope in scopes)
def meta_capabilities(config, *, platform, expected_id, token, http):
    if platform not in {"facebook", "instagram"}:
        raise ValueError("Unsupported Meta native route")
    # Instagram's maintained publisher uses Facebook Graph, not IG-login Graph.
    page_id = str(config.get("pageId") or (expected_id if platform == "facebook" else "")).strip()
    if not page_id:
        raise ValueError("Exact Facebook-login Page association is required")
    grant = meta_auth.debug_token_info(access_token=token, session=http)
    expiry = _grant_expiry(grant)
    app_id = meta_auth._required_env(meta_auth.CLIENT_ID_ENV)
    if str(grant.get("app_id") or "") != app_id:
        raise ValueError("Native token audience does not match selected Meta client")
    scopes = _scopes(grant.get("scopes"))
    required = {"pages_manage_posts", "pages_read_engagement"} if platform == "facebook" else {
        "instagram_basic", "instagram_content_publish"}
    if not required <= scopes:
        raise ValueError("Current Meta token lacks required publishing scopes")
    if not _page_grants(grant, required, page_id):
        raise ValueError("Native publishing grant selects a different Page")
    kind = grant.get("type")
    if kind == "PAGE":
        if str(grant.get("profile_id") or "") != page_id:
            raise ValueError("Native Page token does not match exact configured Page")
        page = _get(http, f"{publishers.FACEBOOK_GRAPH_ROOT}/me", params={
            "fields": "id,instagram_business_account" if platform == "instagram" else "id",
            "access_token": token})
    elif kind == "USER" and platform == "instagram":
        if "pages_show_list" not in scopes:
            raise ValueError("Current Instagram user token cannot verify Page tasks")
        body = _get(http, f"{publishers.FACEBOOK_GRAPH_ROOT}/me/accounts", params={
            "fields": "id,tasks,instagram_business_account", "limit": 100, "access_token": token})
        pages = body.get("data")
        if not isinstance(pages, list) or any(not isinstance(p, dict) for p in pages):
            raise ValueError("Exact Instagram Page tasks are unverified")
        matches = [p for p in pages if p.get("id") == page_id]
        if len(matches) != 1:
            raise ValueError("Exact Instagram Page has no verified content-creation task")
        tasks = matches[0].get("tasks")
        if (not isinstance(tasks, list) or any(not isinstance(task, str) or not task.strip() for task in tasks)
                or not {"CREATE_CONTENT", "MANAGE"} & set(tasks)):
            raise ValueError("Exact Instagram Page has no verified content-creation task")
        page = matches[0]
    else:
        raise ValueError("Native token model cannot prove this configured publishing route")
    if page.get("id") != page_id:
        raise ValueError("Verified Page identity differs from selected destination")
    if platform == "facebook":
        capabilities = ["text", "image", "carousel", "video"]
        if "pages_manage_engagement" in scopes and _page_grants(grant, {"pages_manage_engagement"}, page_id, require_target=True):
            capabilities.append("first_comment")
    else:
        association = page.get("instagram_business_account")
        if not isinstance(association, dict) or association.get("id") != expected_id:
            raise ValueError("Verified Page does not own the exact configured Instagram account")
        owned = _get(http, f"{publishers.FACEBOOK_GRAPH_ROOT}/{expected_id}",
                     params={"fields": "id", "access_token": token})
        if owned.get("id") != expected_id:
            raise ValueError("Verified Instagram identity differs from selected destination")
        capabilities = ["image", "carousel", "video"]
        if "instagram_manage_comments" in scopes and _page_grants(grant, {"instagram_manage_comments"}, page_id, require_target=True):
            capabilities.append("first_comment")
    return {"capabilities": capabilities, "expires_at": expiry}


def threads_capabilities(config, *, expected_id, token, http):
    app_id = threads_auth._required_env(threads_auth.CLIENT_ID_ENV)
    app_secret = threads_auth._required_env(threads_auth.CLIENT_SECRET_ENV)
    body = _get(http, "https://graph.threads.net/debug_token", params={
        "input_token": token, "access_token": f"{app_id}|{app_secret}"})
    grant = body.get("data")
    if not isinstance(grant, dict):
        raise ValueError("Current Threads publishing grant is unverified")
    expiry = _grant_expiry(grant)
    if str(grant.get("app_id") or "") != app_id or str(grant.get("user_id") or "") != expected_id:
        raise ValueError("Threads token audience or exact identity differs from selected destination")
    if not {"threads_basic", "threads_content_publish"} <= _scopes(grant.get("scopes")):
        raise ValueError("Current Threads token lacks required publishing scopes")
    user = _get(http, f"{publishers.THREADS_GRAPH_ROOT}/me", params={"fields": "id", "access_token": token})
    if user.get("id") != expected_id:
        raise ValueError("Verified Threads identity differs from selected destination")
    return {"capabilities": ["text", "image", "carousel", "video"], "expires_at": expiry}
