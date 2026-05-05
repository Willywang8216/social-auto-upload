"""Cross-platform account operation event log."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from conf import BASE_DIR

DB_PATH = Path(BASE_DIR) / "db" / "database.db"


@dataclass(slots=True)
class AccountEvent:
    id: int
    account_id: int | None
    profile_id: int | None
    platform: str
    account_name: str
    action: str
    status: str
    summary: str
    error_text: str | None = None
    metadata: dict | None = None
    created_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@contextmanager
def _connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    resolved = db_path or DB_PATH
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _json_load(raw: str | None, fallback):
    if not raw:
        return fallback
    return json.loads(raw)


def _row_to_event(row: sqlite3.Row) -> AccountEvent:
    return AccountEvent(
        id=row['id'],
        account_id=row['account_id'],
        profile_id=row['profile_id'],
        platform=row['platform'],
        account_name=row['account_name'],
        action=row['action'],
        status=row['status'],
        summary=row['summary'],
        error_text=row['error_text'],
        metadata=_json_load(row['metadata_json'], {}),
        created_at=row['created_at'],
    )


def record_event(*, account_id: int | None, profile_id: int | None, platform: str, account_name: str, action: str, status: str, summary: str, error_text: str | None = None, metadata: dict | None = None, db_path: Path | None = None) -> AccountEvent:
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO account_events (
                account_id, profile_id, platform, account_name,
                action, status, summary, error_text, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id, profile_id, platform, account_name,
                action, status, summary, error_text,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
        event_id = int(cursor.lastrowid)
    return get_event(event_id, db_path=db_path)


def get_event(event_id: int, *, db_path: Path | None = None) -> AccountEvent | None:
    with _connect(db_path) as conn:
        row = conn.execute('SELECT * FROM account_events WHERE id = ?', (event_id,)).fetchone()
    return _row_to_event(row) if row else None


def list_events(*, limit: int = 50, account_id: int | None = None, profile_id: int | None = None, platform: str | None = None, db_path: Path | None = None) -> list[AccountEvent]:
    query = 'SELECT * FROM account_events WHERE 1=1'
    params: list[object] = []
    if account_id is not None:
        query += ' AND account_id = ?'
        params.append(account_id)
    if profile_id is not None:
        query += ' AND profile_id = ?'
        params.append(profile_id)
    if platform:
        query += ' AND platform = ?'
        params.append(platform)
    query += ' ORDER BY id DESC LIMIT ?'
    params.append(limit)
    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_event(row) for row in rows]
