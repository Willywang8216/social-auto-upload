"""Bounded native grant checks for non-OAuth publishing destinations.

These checks never create a post, upload media or alter account credentials.
Authenticated sessions are not delivery receipts. Returned facts are allowlisted;
configured identity and effective credential selection remain independently pinned.
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import quote, urlsplit

from myUtils import prepared_publishers as publishers

SUPPORTED_PLATFORMS = frozenset({"telegram", "bluesky", "nw_sw_blog", "teaching_blog"})
MAX_TELEGRAM_DESTINATIONS = 8
TELEGRAM_CHECK_SECONDS = 80


def _value(config, key, default_env=None):
    return str(publishers._config_value(config, key, default_env=default_env) or "").strip()


def configured_destination(platform, config):
    """Pure configured selection, not live authorization or publication proof.

    Secrets are returned ONLY for the caller's fingerprint calculation. This
    object must never be logged, serialized over the API or stored as evidence.
    """
    identity, selection = None, {}
    if platform == "bluesky":
        handle = _value(config, "handle").lower()
        password = _value(config, "appPassword", "BLUESKY_APP_PASSWORD")
        service = str(config.get("service") or publishers.BLUESKY_DEFAULT_SERVICE).strip().rstrip("/")
        parsed = urlsplit(service)
        if (not handle or parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            return None, {}, False
        identity = {"platform": platform, "handle": handle, "service": service}
        if config.get("did"):
            identity["did"] = str(config["did"]).strip()
        selection = {"appPassword": password}
        configured = bool(password)
    elif platform in {"nw_sw_blog", "teaching_blog"}:
        owner, repo = _value(config, "repoOwner"), _value(config, "repoName")
        default_env = "SAU_NW_SW_BLOG_GITHUB_TOKEN" if platform == "nw_sw_blog" else "SAU_TEACHING_BLOG_GITHUB_TOKEN"
        token = _value(config, "githubToken", default_env)
        branch = _value(config, "branch") or "main"
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo)
                or not branch or any(x in branch for x in ("..", "@{", "\\"))):
            return None, {}, False
        identity = {"platform": platform, "repository": f"{owner}/{repo}", "branch": branch}
        selection = {"githubToken": token}
        configured = bool(token)
    elif platform == "telegram":
        chats = publishers._telegram_resolve_chat_ids(config)
        if (not chats or len(chats) > MAX_TELEGRAM_DESTINATIONS or len(chats) != len(set(chats))
                or any(not re.fullmatch(r"-?[0-9]+|@[A-Za-z0-9_]{5,64}", chat) for chat in chats)):
            return None, {}, False
        mtproto = publishers._telegram_is_mtproto(config)
        identity = {"platform": platform, "mode": "mtproto" if mtproto else "bot", "chat_ids": chats}
        if mtproto:
            api_id, api_hash = _value(config, "apiId"), _value(config, "apiHash")
            try:
                session = publishers._telegram_mtproto_session_string(config)
            except (ValueError, OSError, AttributeError, publishers.PreparedPublishError):
                session = ""
            selection = {"apiId": api_id, "apiHash": api_hash, "sessionString": session}
            configured = bool(api_id.isdecimal() and int(api_id) > 0 and api_hash and session)
        else:
            token = _value(config, "botToken", "TELEGRAM_BOT_TOKEN")
            selection = {"botToken": token}
            configured = bool(token)
        if _value(config, "userId"):
            identity["user_id"] = _value(config, "userId")
        if not mtproto and _value(config, "botId"):
            identity["bot_id"] = _value(config, "botId")
    else:
        return None, {}, False
    return identity, selection, configured


def assert_effective_destination(platform, config, payload=None):
    """Per-post selectors cannot borrow another destination's account proof."""
    if platform != "telegram" or payload is None:
        return
    if publishers._telegram_resolve_chat_ids(config, payload) != publishers._telegram_resolve_chat_ids(config):
        raise ValueError("Per-post Telegram chats differ from the exact configured destination; review required")


def _body(response):
    publishers._raise_for_status(response)
    body = publishers._response_payload(response)
    if not isinstance(body, dict):
        raise ValueError("Destination verification returned invalid metadata")
    return body


def _telegram_result(response):
    body = _body(response)
    if body.get("ok") is not True or not isinstance(body.get("result"), dict):
        raise ValueError("Telegram verification did not confirm the requested operation")
    return body["result"]


def _matches_telegram_chat(requested, entity, identifier):
    if requested.startswith("@"):
        return str(getattr(entity, "username", "") or "").casefold() == requested[1:].casefold()
    return str(identifier) == requested


def _bluesky_grant(config, identity, http):
    cfg = publishers._bluesky_config(config)
    # App-password authentication permits repository writes, not just public
    # profile reads. An inactive/taken-down account cannot supply that grant.
    response = http.post(f"{cfg['service']}/xrpc/com.atproto.server.createSession",
                         json={"identifier": cfg["handle"], "password": cfg["password"]})
    auth = _body(response)
    if (not auth.get("accessJwt") or not str(auth.get("did") or "").startswith("did:")
            or auth.get("active") is False or auth.get("status")
            or str(auth.get("handle") or "").casefold() != identity["handle"]
            or (identity.get("did") and auth["did"] != identity["did"])):
        raise ValueError("Bluesky authentication does not match the active configured account")
    current = _body(http.get(f"{cfg['service']}/xrpc/com.atproto.server.getSession",
                            headers={"Authorization": f"Bearer {auth['accessJwt']}"}))
    if (current.get("did") != auth["did"] or current.get("active") is False or current.get("status")
            or str(current.get("handle") or "").casefold() != identity["handle"]):
        raise ValueError("Bluesky active repository authorization is unverified")
    return ["text", "image", "carousel", "video", "alt_text"]


def _github_grant(config, identity, selection, http):
    repository = identity["repository"]
    headers = {"Authorization": f"Bearer {selection['githubToken']}",
               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    response = http.get(f"{publishers.GITHUB_API_ROOT}/repos/{repository}", headers=headers)
    repo = _body(response)
    if (str(repo.get("full_name") or "").casefold() != repository.casefold()
            or repo.get("archived") is not False or repo.get("disabled") is True
            or not isinstance(repo.get("permissions"), dict) or repo["permissions"].get("push") is not True):
        raise ValueError("GitHub repository identity or content-write permission is unverified")
    scopes = {v.strip() for k, value in (getattr(response, "headers", {}) or {}).items()
              if k.lower() == "x-oauth-scopes" for v in str(value).split(",") if v.strip()}
    # User repository permissions alone do NOT prove a fine-grained PAT has
    # Contents:write. Without an introspectable classic/OAuth write scope the
    # route remains unverified; do not manufacture permission from config flags.
    if "repo" not in scopes and not ("public_repo" in scopes and repo.get("private") is False):
        raise ValueError("GitHub token content-write scope cannot be verified without operator-provided grant evidence")
    branch = _body(http.get(f"{publishers.GITHUB_API_ROOT}/repos/{repository}/branches/{quote(identity['branch'], safe='')}",
                           headers=headers))
    if branch.get("name") != identity["branch"] or branch.get("protected") is not False:
        raise ValueError("GitHub destination branch requires approval or has unverified write restrictions")
    return ["text", "image", "article"]


def _telegram_bot_grant(identity, selection, http):
    root = publishers.TELEGRAM_API_ROOT
    token = selection["botToken"]
    bot = _telegram_result(http.post(root.format(token=token, method="getMe"), data={}))
    if type(bot.get("id")) is not int or bot.get("is_bot") is not True:
        raise ValueError("Telegram bot authorization is unverified")
    if any(identity.get(key) and str(bot["id"]) != identity[key] for key in ("bot_id", "user_id")):
        raise ValueError("Telegram authenticated bot differs from the configured sender")
    for requested in identity["chat_ids"]:
        chat = _telegram_result(http.post(root.format(token=token, method="getChat"), data={"chat_id": requested}))
        chat_id = chat.get("id")
        if type(chat_id) is not int or (requested.startswith("@") and str(chat.get("username") or "").casefold() != requested[1:].casefold()) or (not requested.startswith("@") and str(chat_id) != requested):
            raise ValueError("Telegram verified chat differs from the configured destination")
        member = _telegram_result(http.post(root.format(token=token, method="getChatMember"),
                                           data={"chat_id": chat_id, "user_id": bot["id"]}))
        if (member.get("user") or {}).get("id") != bot["id"]:
            raise ValueError("Telegram chat grant belongs to another sender")
        role = member.get("status")
        if chat.get("type") == "channel":
            if role != "creator" and not (role == "administrator" and member.get("can_post_messages") is True):
                raise ValueError("Telegram channel does not grant message creation")
        elif chat.get("type") in {"group", "supergroup"}:
            if role not in {"creator", "administrator"}:
                if role != "member":
                    raise ValueError("Telegram group sender is restricted or not a member")
                rights = chat.get("permissions") or {}
                if any(rights.get(key) is not True for key in ("can_send_messages", "can_send_photos", "can_send_videos")):
                    raise ValueError("Telegram group content permissions are unverified")
        else:
            raise ValueError("Telegram destination type has no implemented grant verifier")
    return ["text", "image", "carousel", "video"]


def _telegram_mtproto_grant(identity, selection):
    if publishers.TelegramClient is None:
        raise ValueError("Telegram MTProto authorization requires Telethon")

    async def verify():
        client = publishers.TelegramClient(publishers.StringSession(selection["sessionString"]),
                                           int(selection["apiId"]), selection["apiHash"],
                                           connection_retries=0, request_retries=0, timeout=15)
        try:
            await client.connect()  # Never start(), login(), phone-code or 2FA.
            if not await client.is_user_authorized():
                raise ValueError("Telegram saved session is no longer authorized")
            me = await client.get_me()
            if (not me or getattr(me, "deleted", False)
                    or (identity.get("user_id") and str(me.id) != identity["user_id"])):
                raise ValueError("Telegram saved session does not match the configured sender")
            for requested in identity["chat_ids"]:
                entity = await client.get_entity(int(requested) if not requested.startswith("@") else requested)
                from telethon import utils
                if not _matches_telegram_chat(requested, entity, utils.get_peer_id(entity)):
                    raise ValueError("Telegram verified peer differs from the configured chat")
                rights = await client.get_permissions(entity, me)
                creator, admin = getattr(rights, "is_creator", False), getattr(rights, "is_admin", False)
                if getattr(entity, "broadcast", False):
                    posting = getattr(getattr(getattr(rights, "participant", None), "admin_rights", None), "post_messages", False)
                    if creator is not True and not (admin is True and posting is True):
                        raise ValueError("Telegram channel has no verified post permission")
                elif getattr(entity, "megagroup", False) or entity.__class__.__name__ == "Chat":
                    if creator is not True and admin is not True and any(
                            getattr(rights, field, None) is not True for field in ("send_messages", "send_media")):
                        raise ValueError("Telegram group content permissions are unverified")
                else:
                    raise ValueError("Telegram destination type has no implemented grant verifier")
        finally:
            await asyncio.wait_for(client.disconnect(), timeout=5)

    asyncio.run(asyncio.wait_for(verify(), timeout=TELEGRAM_CHECK_SECONDS))
    return ["text", "image", "carousel", "video"]


def verify_grant(platform, config, identity, http):
    current, selection, configured = configured_destination(platform, config)
    if not configured or current != identity:
        raise ValueError("Exact native destination or selected credentials are incomplete")
    if platform == "bluesky":
        return _bluesky_grant(config, identity, http)
    if platform in {"nw_sw_blog", "teaching_blog"}:
        return _github_grant(config, identity, selection, http)
    if platform == "telegram":
        return (_telegram_mtproto_grant(identity, selection) if identity["mode"] == "mtproto"
                else _telegram_bot_grant(identity, selection, http))
    raise ValueError("Unsupported native destination grant verifier")
