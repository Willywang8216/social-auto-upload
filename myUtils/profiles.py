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

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from conf import BASE_DIR

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
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class Account:
    id: int
    profile_id: int
    platform: str
    account_name: str
    cookie_path: str
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


def _row_to_profile(row: sqlite3.Row) -> Profile:
    return Profile(**{key: row[key] for key in row.keys()})


def _row_to_account(row: sqlite3.Row) -> Account:
    return Account(**{key: row[key] for key in row.keys()})


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


def create_profile(name: str, description: str = "", *, db_path: Path = DB_PATH) -> Profile:
    slug = slugify(name)
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO profiles (name, slug, description) VALUES (?, ?, ?)",
            (name.strip(), slug, description.strip()),
        )
        conn.commit()
        profile_id = cursor.lastrowid
    return get_profile(profile_id, db_path=db_path)


def get_profile(profile_id: int, *, db_path: Path = DB_PATH) -> Profile:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    if row is None:
        raise LookupError(f"Profile not found: id={profile_id}")
    return _row_to_profile(row)


def get_profile_by_slug(slug: str, *, db_path: Path = DB_PATH) -> Profile:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise LookupError(f"Profile not found: slug={slug!r}")
    return _row_to_profile(row)


def list_profiles(*, db_path: Path = DB_PATH) -> list[Profile]:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM profiles ORDER BY name").fetchall()
    return [_row_to_profile(row) for row in rows]


def delete_profile(profile_id: int, *, db_path: Path = DB_PATH) -> None:
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
    status: int = 0,
    db_path: Path = DB_PATH,
) -> Account:
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform!r}")

    profile = get_profile(profile_id, db_path=db_path)
    resolved_path = (
        Path(cookie_path)
        if cookie_path is not None
        else resolve_cookie_path(platform, profile.slug, account_name)
    )

    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO accounts (profile_id, platform, account_name, cookie_path, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (profile_id, platform, account_name.strip(), str(resolved_path), status),
        )
        conn.commit()
        account_id = cursor.lastrowid
    return get_account(account_id, db_path=db_path)


def get_account(account_id: int, *, db_path: Path = DB_PATH) -> Account:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
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
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY platform, account_name"
    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_account(row) for row in rows]


def update_account_status(
    account_id: int, status: int, *, db_path: Path = DB_PATH
) -> None:
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE accounts SET status = ?, last_checked_at = ? WHERE id = ?",
            (status, now, account_id),
        )
        conn.commit()


def delete_account(account_id: int, *, db_path: Path = DB_PATH) -> None:
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
