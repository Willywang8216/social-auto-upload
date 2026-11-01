"""Framework-free provider account identities for durable queue exclusion.

Only stable, non-secret identifiers are read. In particular, access tokens are
never identities: rotating credentials must not permit a second concurrent
publisher. Meta field precedence matches publish_orchestrator's historical
``_external_account_identity`` helper.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import sqlite3
from collections.abc import Mapping
from typing import Any


_IDENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "facebook": ("pageId", "page_id"),
    "instagram": ("igUserId", "ig_user_id"),
    "threads": ("threadUserId", "userId", "thread_user_id", "user_id"),
    "youtube": ("channelId", "channel_id"),
    "tiktok": ("openId", "open_id"),
    "twitter": ("twitterUserId", "userId", "user_id"),
    "telegram": ("botId", "chatId", "chat_id"),
    "bluesky": ("did",),
    "reddit": ("userId", "user_id"),
}


def _value(account: Any, key: str, default=None):
    if isinstance(account, (Mapping, sqlite3.Row)):
        return account[key] if key in account.keys() else default
    return getattr(account, key, default)


def _platform(account: Any) -> str:
    platform = str(_value(account, "platform", "") or "").strip().lower()
    return "twitter" if platform == "x" else platform


def external_account_identity(account: Any) -> tuple[str, str] | None:
    """Return ``(platform, stable_provider_id)`` for a row or Account object.

    Encrypted credential values are deliberately not decrypted or inspected.
    Unknown providers/identities use the cookie-path/row fallback below.
    """
    platform = _platform(account)
    config = _value(account, "config")
    if not isinstance(config, dict):
        try:
            config = json.loads(_value(account, "config_json", "{}") or "{}")
        except (TypeError, ValueError):
            config = {}
    if not isinstance(config, dict):
        return None
    for key in _IDENTITY_FIELDS.get(platform, ()):
        value = config.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            text = str(value).strip()
            if text and not text.startswith("sau:enc:"):
                return platform, text
    return None


def structured_account_id(account_ref: str) -> int | None:
    """Parse the queue's ``account:<id>`` references; legacy paths return None."""
    if not isinstance(account_ref, str) or not account_ref.startswith("account:"):
        return None
    suffix = account_ref[len("account:"):]
    if not suffix.isascii() or not suffix.isdecimal() or len(suffix) > 19:
        return None
    value = int(suffix)
    return value if 0 < value <= 2**63 - 1 else None


def _path_identity(value: str) -> str:
    # Pure lexical normalization: do not touch the filesystem or read cookies.
    text = posixpath.normpath(value.replace("\\", "/"))
    if len(text) > 1 and text[1] == ":":
        text = text.casefold()  # Windows cookie paths are case-insensitive.
    return text


def canonical_account_key(conn: sqlite3.Connection, account_ref: str) -> str:
    """Internal, opaque exclusion key, shared across profiles and workspaces.

    Duplicate account rows with the same provider identity (or, failing that,
    cookie path) serialize together. Legacy references serialize by reference;
    an unidentifiable structured row serializes by its row id. This function
    never returns account configuration or credentials.
    """
    account_id = structured_account_id(account_ref)
    row = None
    if account_id is not None:
        cursor = conn.execute(
            "SELECT id, platform, cookie_path, config_json FROM accounts WHERE id = ?",
            (account_id,),
        )
        fetched = cursor.fetchone()
        if fetched is not None:
            row = dict(zip((column[0] for column in cursor.description), fetched))
    if row is None:
        identity = ("reference", _path_identity(str(account_ref)))
    else:
        external = external_account_identity(row)
        if external is not None:
            identity = ("provider", *external)
        elif str(row["cookie_path"] or "").strip():
            identity = ("cookie", _platform(row), _path_identity(row["cookie_path"]))
        else:
            identity = ("row", _platform(row), str(row["id"]))
    blob = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    return "account-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
