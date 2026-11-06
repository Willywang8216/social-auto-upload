"""Framework-free identities for queue exclusion and destination pacing.

Only stable, non-secret provider IDs, lexical cookie paths and exact managed
routes are identities. Credentials and display names are never used, decrypted
or verified. Native field precedence remains compatible with historical keys.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import sqlite3
from collections.abc import Iterable, Mapping
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


def json_object(text: str) -> dict:
    """Strict metadata decoding: ambiguous JSON is not an empty configuration."""
    def invalid(*_args):
        raise ValueError("Invalid identity metadata")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                invalid()
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid)
        if isinstance(value, dict):
            return value
    except (TypeError, ValueError, RecursionError):
        pass
    raise ValueError("Invalid identity metadata; expected a JSON object")


def _config(account: Any) -> Mapping | None:
    config = _value(account, "config")
    if isinstance(config, Mapping):
        return config
    try:
        return json_object(_value(account, "config_json", "{}"))
    except ValueError:
        return None


def external_account_identity(account: Any) -> tuple[str, str] | None:
    """Return ``(platform, stable_provider_id)`` for a row or Account object.

    Encrypted credential values are deliberately not decrypted or inspected.
    Unknown providers/identities use the cookie-path/row fallback below.
    """
    platform = _platform(account)
    config = _config(account)
    if config is None:
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


def _opaque_key(prefix: str, identity: tuple) -> str:
    blob = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    return prefix + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _native_account_key(account_ref: str, row: Any = None) -> str:
    if row is None:
        identity = ("reference", _path_identity(str(account_ref)))
    else:
        external = external_account_identity(row)
        if external is not None:
            identity = ("provider", *external)
        elif str(_value(row, "cookie_path", "") or "").strip():
            identity = ("cookie", _platform(row), _path_identity(_value(row, "cookie_path")))
        else:
            identity = ("row", _platform(row), str(_value(row, "id")))
    return _opaque_key("account-", identity)


def managed_account_identity(account: Any, *, settings: Mapping | None = None) -> tuple[str, str, str] | None:
    """Exact ``(workspace, network, profile_ref)`` for a configured backup/primary.

    Resolve only the existing pure mapping policy, never credentials or live
    verification. Multi-destination, malformed and mismatched-platform mappings
    cannot bridge aliases. Missing network IDs are not inferred from a platform.
    Built-in routes may use their configured default workspace, just as publishing
    does; an invalid explicit workspace never falls through to another one.
    """
    config = _config(account)
    if config is None:
        return None
    from myUtils import sociamonials_fallback as managed

    platform = _platform(account)
    identifier = _value(account, "id")
    try:
        mapping = managed.resolve_mapping(
            identifier, account={"id": identifier, "config": config}, settings=settings,
            platform=None,  # Do not manufacture a missing network identity.
        )
        network = managed._platform_network(platform)
        if (not mapping or not network or mapping.get("network") != network
                or len(mapping.get("profile_refs", [])) != 1):
            return None
        if "workspace_id" in mapping:
            workspace = mapping["workspace_id"]
        elif mapping.get("api_key_env") == managed.TEACHING_API_KEY_ENV:
            workspace = managed.get_teaching_workspace_id()
        else:
            workspace = managed.get_workspace_id()
        text = str(workspace).strip()
        if (isinstance(workspace, bool) or not text.isascii() or not text.isdecimal()
                or len(text) > 19 or not 0 < int(text) <= 2**63 - 1):
            return None
        workspace = str(int(text))
        if (mapping.get("api_key_env") == managed.TEACHING_API_KEY_ENV
                and workspace != managed.get_teaching_workspace_id()):
            return None
        return workspace, network, mapping["profile_refs"][0]
    except (TypeError, ValueError, KeyError):
        return None


def canonical_account_keys(
    conn: sqlite3.Connection, account_refs: Iterable[str] = (),
) -> dict[str, str]:
    """Build one deterministic native/managed union on the caller's snapshot.

    Include ALL current rows, even aliases without target history, plus historical
    and caller-supplied references. Build once per claim/allocation pass, not once
    per target. No returned value contains account configuration or credentials.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    if not {"id", "platform", "config_json"} <= columns:
        raise sqlite3.OperationalError("Incompatible account identity schema")
    selected = [name for name in ("id", "platform", "config_json", "cookie_path", "profile_id")
                if name in columns]
    cursor = conn.execute("SELECT " + ",".join(selected) + " FROM accounts ORDER BY id")
    accounts = {row[0]: dict(zip(selected, row)) for row in cursor}
    profile_settings: dict[int, Mapping | None] = {}
    if "profile_id" in columns:
        for row in conn.execute("SELECT id,settings_json FROM profiles"):
            try:
                profile_settings[row[0]] = json_object(row[1])
            except ValueError:
                profile_settings[row[0]] = None
    refs = set(account_refs) | {f"account:{identifier}" for identifier in accounts}
    target_columns = {row[1] for row in conn.execute("PRAGMA table_info(publish_job_targets)")}
    if target_columns:
        refs.update(row[0] for row in conn.execute("SELECT DISTINCT account_ref FROM publish_job_targets"))
    if any(not isinstance(ref, str) or not ref.strip() for ref in refs):
        raise ValueError("Invalid account reference history")

    parents: dict[str, str] = {}

    def root(key: str) -> str:
        parents.setdefault(key, key)
        leader = key
        while parents[leader] != leader:
            leader = parents[leader]
        while parents[key] != key:
            parent = parents[key]
            parents[key] = leader
            key = parent
        return leader

    native: dict[str, str] = {}
    for ref in sorted(refs):
        row = accounts.get(structured_account_id(ref))
        key = native[ref] = _native_account_key(ref, row)
        root(key)
        if row is None:
            continue
        settings = profile_settings.get(row.get("profile_id"), None if "profile_id" in columns else {})
        if settings is None:  # Unreadable profile policy must not use a built-in.
            continue
        destination = managed_account_identity(row, settings=settings)
        if destination is not None:
            left, right = root(key), root(_opaque_key("managed-", destination))
            parents[max(left, right)] = min(left, right)
    return {ref: root(key) for ref, key in native.items()}


def canonical_account_key(
    conn: sqlite3.Connection, account_ref: str, *, keys: Mapping[str, str] | None = None,
) -> str:
    """Opaque union key; unshared native/legacy hashes retain their old format.

    Pass a prebuilt ``canonical_account_keys`` map for batch lookups. The default
    builds the same union, so scheduling, recovery and worker exclusion agree.
    """
    if keys is None:
        keys = canonical_account_keys(conn, (account_ref,))
    return keys.get(account_ref) or _native_account_key(account_ref)
