"""Profile and Account model + platform registry.

A Profile represents a person or brand. Each Profile owns many Accounts, and a
Profile may own more than one account on the same platform (e.g. two Medium
identities under one brand).

This module is the single source of truth for resolving a cookie/storage-state
file from a (profile, platform, account_name) tuple. Both the Flask backend and
the `sau` CLI go through it so account files stop drifting between the two.

The implementation is deliberately self-contained: SQLite + dataclasses, no ORM.
That keeps the dependency surface flat and matches the rest of the project.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from utils.conf_defaults import BASE_DIR

DB_PATH = Path(BASE_DIR) / "db" / "database.db"
COOKIE_ROOT = Path(BASE_DIR) / "cookies"


# Platform identifiers used everywhere (DB rows, CLI, REST). Adding a platform
# here is the only change required to make it visible to the registry.
PLATFORM_DOUYIN = "douyin"
PLATFORM_KUAISHOU = "kuaishou"
PLATFORM_XIAOHONGSHU = "xiaohongshu"
PLATFORM_TENCENT = "tencent"
PLATFORM_BILIBILI = "bilibili"
PLATFORM_TIKTOK = "tiktok"
PLATFORM_BAIJIAHAO = "baijiahao"
PLATFORM_MEDIUM = "medium"
PLATFORM_SUBSTACK = "substack"
PLATFORM_TWITTER = "twitter"
PLATFORM_FACEBOOK = "facebook"
PLATFORM_INSTAGRAM = "instagram"
PLATFORM_REDDIT = "reddit"
PLATFORM_TELEGRAM = "telegram"
PLATFORM_YOUTUBE = "youtube"
PLATFORM_THREADS = "threads"
PLATFORM_DISCORD = "discord"
PLATFORM_PATREON = "patreon"
PLATFORM_TEACHING_BLOG = "teaching_blog"
PLATFORM_NW_SW_BLOG = "nw_sw_blog"

SUPPORTED_PLATFORMS: tuple[str, ...] = (
    PLATFORM_DOUYIN,
    PLATFORM_KUAISHOU,
    PLATFORM_XIAOHONGSHU,
    PLATFORM_TENCENT,
    PLATFORM_BILIBILI,
    PLATFORM_TIKTOK,
    PLATFORM_BAIJIAHAO,
    PLATFORM_MEDIUM,
    PLATFORM_SUBSTACK,
    PLATFORM_TWITTER,
    PLATFORM_FACEBOOK,
    PLATFORM_INSTAGRAM,
    PLATFORM_REDDIT,
    PLATFORM_TELEGRAM,
    PLATFORM_YOUTUBE,
    PLATFORM_THREADS,
    PLATFORM_DISCORD,
    PLATFORM_PATREON,
    PLATFORM_TEACHING_BLOG,
    PLATFORM_NW_SW_BLOG,
)

# Legacy numeric platform codes still used by the Flask backend's `type` column.
# Kept here so a future cleanup can remove them in one place.
LEGACY_PLATFORM_CODE_TO_SLUG = {
    1: PLATFORM_XIAOHONGSHU,
    2: PLATFORM_TENCENT,
    3: PLATFORM_DOUYIN,
    4: PLATFORM_KUAISHOU,
    5: PLATFORM_MEDIUM,
    6: PLATFORM_SUBSTACK,
    7: PLATFORM_TWITTER,
}
LEGACY_PLATFORM_SLUG_TO_CODE = {v: k for k, v in LEGACY_PLATFORM_CODE_TO_SLUG.items()}


_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    cleaned = _SLUG_PATTERN.sub("-", value.strip().lower()).strip("-")
    if not cleaned:
        raise ValueError(f"Cannot derive slug from value: {value!r}")
    return cleaned


@dataclass(slots=True)
class Profile:
    id: int
    name: str
    slug: str
    description: str = ""
    settings: dict | None = None
    created_at: str | None = None
    updated_at: str | None = None
    # SocialUpload extended fields
    default_language: str = "en"
    timezone: str = "UTC"
    system_prompt: str = ""
    writing_style_prompt: str = ""
    contact_details: str = ""
    default_cta: str = ""
    default_hashtags: str = ""
    default_link: str = ""
    watermark_config_id: int | None = None
    google_sheet_folder_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class Account:
    id: int
    profile_id: int
    platform: str
    account_name: str
    cookie_path: str
    auth_type: str = "cookie"
    config: dict | None = None
    enabled: bool = True
    status: int = 0
    last_checked_at: str | None = None
    created_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@contextmanager
def _connect(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


_PROFILE_FIELDS = {f.name for f in fields(Profile)}
_ACCOUNT_FIELDS = {f.name for f in fields(Account)}


def _row_to_profile(row: sqlite3.Row) -> Profile:
    payload = {key: row[key] for key in row.keys()}
    payload["settings"] = json.loads(payload.pop("settings_json", "{}") or "{}")
    # Filter to declared fields so schema additions (e.g. workspace_id) don't
    # break construction — the tenancy columns are handled by the ORM layer.
    payload = {k: v for k, v in payload.items() if k in _PROFILE_FIELDS}
    return Profile(**payload)


def _row_to_account(row: sqlite3.Row) -> Account:
    payload = {key: row[key] for key in row.keys()}
    payload["config"] = json.loads(payload.pop("config_json", "{}") or "{}")
    payload["enabled"] = bool(payload.get("enabled", 1))
    payload = {k: v for k, v in payload.items() if k in _ACCOUNT_FIELDS}
    return Account(**payload)


DIRECT_PUBLISH_PLATFORMS: frozenset[str] = frozenset(
    {
        PLATFORM_DOUYIN,
        PLATFORM_KUAISHOU,
        PLATFORM_XIAOHONGSHU,
        PLATFORM_TENCENT,
        PLATFORM_BILIBILI,
        PLATFORM_TIKTOK,
        PLATFORM_BAIJIAHAO,
        PLATFORM_MEDIUM,
        PLATFORM_SUBSTACK,
        PLATFORM_TWITTER,
        PLATFORM_FACEBOOK,
        PLATFORM_INSTAGRAM,
        PLATFORM_REDDIT,
        PLATFORM_TELEGRAM,
        PLATFORM_YOUTUBE,
        PLATFORM_THREADS,
        PLATFORM_DISCORD,
        PLATFORM_TEACHING_BLOG,
        PLATFORM_NW_SW_BLOG,
    }
)

SHEET_EXPORT_PLATFORMS: frozenset[str] = frozenset(
    {
        PLATFORM_FACEBOOK,
        PLATFORM_INSTAGRAM,
        PLATFORM_TWITTER,
        PLATFORM_YOUTUBE,
        PLATFORM_TIKTOK,
        PLATFORM_THREADS,
        PLATFORM_REDDIT,
    }
)

COOKIE_REQUIRED_PLATFORMS: frozenset[str] = frozenset(
    {
        PLATFORM_DOUYIN,
        PLATFORM_KUAISHOU,
        PLATFORM_XIAOHONGSHU,
        PLATFORM_TENCENT,
        PLATFORM_BILIBILI,
        PLATFORM_BAIJIAHAO,
        PLATFORM_MEDIUM,
        PLATFORM_SUBSTACK,
        PLATFORM_PATREON,
    }
)

OAUTH_DEFAULT_PLATFORMS: frozenset[str] = frozenset(
    {
        PLATFORM_TIKTOK,
        PLATFORM_FACEBOOK,
        PLATFORM_INSTAGRAM,
        PLATFORM_THREADS,
        PLATFORM_YOUTUBE,
        PLATFORM_TWITTER,
    }
)


def platform_requires_cookie(platform: str) -> bool:
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform!r}")
    return platform in COOKIE_REQUIRED_PLATFORMS


def platform_defaults_to_oauth(platform: str) -> bool:
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform!r}")
    return platform in OAUTH_DEFAULT_PLATFORMS


def platform_supports_direct_publish(platform: str) -> bool:
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform!r}")
    return platform in DIRECT_PUBLISH_PLATFORMS


def platform_supports_sheet_export(platform: str) -> bool:
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform!r}")
    return platform in SHEET_EXPORT_PLATFORMS


def resolve_cookie_path(platform: str, profile_slug: str, account_name: str) -> Path:
    """Filesystem location for a (platform, profile, account) cookie file.

    Layout: cookies/<platform>/<profile_slug>/<account_name>.json
    """

    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform!r}")
    safe_account = slugify(account_name)
    path = COOKIE_ROOT / platform / profile_slug / f"{safe_account}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------- Profile CRUD ---------------------------


def create_profile(
    name: str,
    description: str = "",
    *,
    settings: dict | None = None,
    workspace_id: str | None = None,
    db_path: Path = DB_PATH,
) -> Profile:
    slug = slugify(name)
    with _connect(db_path) as conn:
        if workspace_id is not None:
            cursor = conn.execute(
                """
                INSERT INTO profiles (name, slug, description, settings_json, workspace_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    name.strip(),
                    slug,
                    description.strip(),
                    json.dumps(settings or {}, ensure_ascii=False),
                    workspace_id,
                ),
            )
        else:
            cursor = conn.execute(
                """
                INSERT INTO profiles (name, slug, description, settings_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    name.strip(),
                    slug,
                    description.strip(),
                    json.dumps(settings or {}, ensure_ascii=False),
                ),
            )
        conn.commit()
        profile_id = cursor.lastrowid
    return get_profile(profile_id, workspace_id=workspace_id, db_path=db_path)


def get_profile(profile_id: int, *, workspace_id: str | None = None, db_path: Path = DB_PATH) -> Profile:
    """Fetch a profile. When ``workspace_id`` is given, a profile that belongs to
    another workspace is treated as not found (tenant isolation)."""
    with _connect(db_path) as conn:
        if workspace_id is not None:
            row = conn.execute(
                "SELECT * FROM profiles WHERE id = ? AND workspace_id = ?",
                (profile_id, workspace_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM profiles WHERE id = ?", (profile_id,)
            ).fetchone()
    if row is None:
        raise LookupError(f"Profile not found: id={profile_id}")
    return _row_to_profile(row)


def get_profile_by_slug(slug: str, *, db_path: Path = DB_PATH) -> Profile:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise LookupError(f"Profile not found: slug={slug!r}")
    return _row_to_profile(row)


def list_profiles(*, workspace_id: str | None = None, db_path: Path = DB_PATH) -> list[Profile]:
    with _connect(db_path) as conn:
        if workspace_id is not None:
            rows = conn.execute(
                "SELECT * FROM profiles WHERE workspace_id = ? ORDER BY name",
                (workspace_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM profiles ORDER BY name").fetchall()
    return [_row_to_profile(row) for row in rows]


def update_profile(
    profile_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    settings: dict | None = None,
    workspace_id: str | None = None,
    db_path: Path = DB_PATH,
    **extra_fields,
) -> Profile:
    """Update a profile. Supports name, description, settings, and any extended column.

    When ``workspace_id`` is given, a profile owned by another workspace raises
    ``LookupError`` and is left untouched (tenant isolation)."""
    _EXTENDED_FIELDS = {
        "default_language", "timezone", "system_prompt", "writing_style_prompt",
        "contact_details", "default_cta", "default_hashtags", "default_link",
        "watermark_config_id", "google_sheet_folder_id",
    }
    current = get_profile(profile_id, workspace_id=workspace_id, db_path=db_path)
    next_name = current.name if name is None else name.strip()
    next_description = current.description if description is None else description.strip()
    next_settings = current.settings if settings is None else settings
    next_slug = current.slug if name is None else slugify(next_name)
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")

    # Build SET clause with extended fields
    set_parts = ["name = ?", "slug = ?", "description = ?", "settings_json = ?", "updated_at = ?"]
    values = [next_name, next_slug, next_description, json.dumps(next_settings or {}, ensure_ascii=False), now]
    for field, value in extra_fields.items():
        if field in _EXTENDED_FIELDS:
            set_parts.append(f"{field} = ?")
            values.append(value)
    values.append(profile_id)

    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE profiles SET {', '.join(set_parts)} WHERE id = ?",
            values,
        )
        conn.commit()
    return get_profile(profile_id, workspace_id=workspace_id, db_path=db_path)


def delete_profile(profile_id: int, *, workspace_id: str | None = None, db_path: Path = DB_PATH) -> None:
    """Delete a profile. When ``workspace_id`` is given, a profile owned by
    another workspace raises ``LookupError`` and is left intact."""
    if workspace_id is not None:
        # Confirm ownership before deleting (foreign profile -> LookupError -> 404).
        get_profile(profile_id, workspace_id=workspace_id, db_path=db_path)
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        conn.commit()


# --------------------------- Account CRUD ---------------------------


def add_account(
    profile_id: int,
    platform: str,
    account_name: str,
    *,
    cookie_path: str | Path | None = None,
    auth_type: str = "cookie",
    config: dict | None = None,
    enabled: bool = True,
    status: int = 0,
    db_path: Path = DB_PATH,
) -> Account:
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform!r}")

    profile = get_profile(profile_id, db_path=db_path)
    if cookie_path is not None:
        resolved_path = str(Path(cookie_path))
    elif auth_type == "cookie" or platform_requires_cookie(platform):
        resolved_path = str(resolve_cookie_path(platform, profile.slug, account_name))
    else:
        resolved_path = ""

    with _connect(db_path) as conn:
        # Accounts inherit their parent profile's workspace so ownership stays
        # consistent regardless of which mode created the profile. NULL for
        # legacy profiles (assigned later by the backfill).
        parent_ws_row = conn.execute(
            "SELECT workspace_id FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        parent_workspace_id = parent_ws_row[0] if parent_ws_row else None
        cursor = conn.execute(
            """
            INSERT INTO accounts (
                profile_id,
                platform,
                account_name,
                cookie_path,
                auth_type,
                config_json,
                enabled,
                status,
                workspace_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile_id,
                platform,
                account_name.strip(),
                resolved_path,
                auth_type,
                json.dumps(config or {}, ensure_ascii=False),
                int(enabled),
                status,
                parent_workspace_id,
            ),
        )
        conn.commit()
        account_id = cursor.lastrowid
    return get_account(account_id, db_path=db_path)


def get_account(account_id: int, *, workspace_id: str | None = None, db_path: Path = DB_PATH) -> Account:
    """Fetch an account. When ``workspace_id`` is given, an account that belongs
    to another workspace is treated as not found (tenant isolation)."""
    with _connect(db_path) as conn:
        if workspace_id is not None:
            row = conn.execute(
                "SELECT * FROM accounts WHERE id = ? AND workspace_id = ?",
                (account_id, workspace_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
    if row is None:
        raise LookupError(f"Account not found: id={account_id}")
    return _row_to_account(row)


def find_account(
    profile_id: int, platform: str, account_name: str, *, db_path: Path = DB_PATH
) -> Account | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM accounts
            WHERE profile_id = ? AND platform = ? AND account_name = ?
            """,
            (profile_id, platform, account_name),
        ).fetchone()
    return _row_to_account(row) if row else None


def list_accounts(
    *,
    profile_id: int | None = None,
    platform: str | None = None,
    enabled: bool | None = None,
    workspace_id: str | None = None,
    db_path: Path = DB_PATH,
) -> list[Account]:
    query = "SELECT * FROM accounts"
    clauses: list[str] = []
    params: list[object] = []
    if profile_id is not None:
        clauses.append("profile_id = ?")
        params.append(profile_id)
    if platform is not None:
        clauses.append("platform = ?")
        params.append(platform)
    if enabled is not None:
        clauses.append("enabled = ?")
        params.append(int(enabled))
    if workspace_id is not None:
        clauses.append("workspace_id = ?")
        params.append(workspace_id)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY platform, account_name"
    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_account(row) for row in rows]


def update_account_status(
    account_id: int, status: int, *, workspace_id: str | None = None, db_path: Path = DB_PATH
) -> None:
    if workspace_id is not None:
        get_account(account_id, workspace_id=workspace_id, db_path=db_path)
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE accounts SET status = ?, last_checked_at = ? WHERE id = ?",
            (status, now, account_id),
        )
        conn.commit()


def update_account(
    account_id: int,
    *,
    profile_id: int | None = None,
    account_name: str | None = None,
    cookie_path: str | Path | None = None,
    auth_type: str | None = None,
    config: dict | None = None,
    enabled: bool | None = None,
    status: int | None = None,
    workspace_id: str | None = None,
    db_path: Path = DB_PATH,
) -> Account:
    """Update an account. When ``workspace_id`` is given, an account owned by
    another workspace raises ``LookupError`` and is left untouched."""
    current = get_account(account_id, workspace_id=workspace_id, db_path=db_path)
    next_profile_id = current.profile_id if profile_id is None else profile_id
    next_account_name = current.account_name if account_name is None else account_name.strip()
    next_auth_type = current.auth_type if auth_type is None else auth_type
    next_config = current.config if config is None else config
    next_enabled = current.enabled if enabled is None else enabled
    next_status = current.status if status is None else status
    next_cookie_path = current.cookie_path
    if cookie_path is not None:
        next_cookie_path = str(Path(cookie_path))
    elif not next_cookie_path and (next_auth_type == "cookie" or platform_requires_cookie(current.platform)):
        profile = get_profile(next_profile_id, db_path=db_path)
        next_cookie_path = str(
            resolve_cookie_path(current.platform, profile.slug, next_account_name)
        )

    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE accounts
            SET profile_id = ?, account_name = ?, cookie_path = ?, auth_type = ?, config_json = ?,
                enabled = ?, status = ?
            WHERE id = ?
            """,
            (
                next_profile_id,
                next_account_name,
                next_cookie_path,
                next_auth_type,
                json.dumps(next_config or {}, ensure_ascii=False),
                int(next_enabled),
                next_status,
                account_id,
            ),
        )
        conn.commit()
    return get_account(account_id, db_path=db_path)


def delete_account(account_id: int, *, workspace_id: str | None = None, db_path: Path = DB_PATH) -> None:
    """Delete an account. When ``workspace_id`` is given, an account owned by
    another workspace raises ``LookupError`` and is left intact."""
    if workspace_id is not None:
        get_account(account_id, workspace_id=workspace_id, db_path=db_path)
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        conn.commit()


def ensure_account(
    profile_id: int,
    platform: str,
    account_name: str,
    *,
    db_path: Path = DB_PATH,
) -> Account:
    """Get-or-create an account; the cookie path is determined deterministically."""

    existing = find_account(profile_id, platform, account_name, db_path=db_path)
    if existing is not None:
        return existing
    return add_account(profile_id, platform, account_name, db_path=db_path)


def iter_accounts_for_publish(
    profile_id: int, platform: str, account_names: Iterable[str] | None = None,
    *, db_path: Path = DB_PATH,
) -> list[Account]:
    """Resolve a publish target list to concrete Account rows.

    If ``account_names`` is None, every account on this platform under the
    profile is returned.
    """

    if account_names is None:
        return list_accounts(profile_id=profile_id, platform=platform, db_path=db_path)
    accounts = []
    for name in account_names:
        match = find_account(profile_id, platform, name, db_path=db_path)
        if match is None:
            raise LookupError(
                f"Account '{name}' on platform '{platform}' is not registered "
                f"under profile id={profile_id}"
            )
        accounts.append(match)
    return accounts
