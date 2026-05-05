"""Durable Threads OAuth request persistence."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from conf import BASE_DIR

DB_PATH = Path(BASE_DIR) / 'db' / 'database.db'


@dataclass(slots=True)
class ThreadsOAuthRequest:
    state_token: str
    profile_id: int | None
    account_id: int | None
    account_name: str | None
    redirect_uri: str
    scopes: list[str]
    status: str
    requested_at: str | None = None
    completed_at: str | None = None
    error_text: str | None = None
    result: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@contextmanager
def _connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    resolved = db_path or DB_PATH
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    try:
        yield conn
    finally:
        conn.close()


def _json_load(raw: str | None, fallback):
    if not raw:
        return fallback
    return json.loads(raw)


def _row_to_request(row: sqlite3.Row) -> ThreadsOAuthRequest:
    return ThreadsOAuthRequest(
        state_token=row['state_token'],
        profile_id=row['profile_id'],
        account_id=row['account_id'],
        account_name=row['account_name'],
        redirect_uri=row['redirect_uri'],
        scopes=_json_load(row['scopes_json'], []),
        status=row['status'],
        requested_at=row['requested_at'],
        completed_at=row['completed_at'],
        error_text=row['error_text'],
        result=_json_load(row['result_json'], {}),
    )


def create_oauth_request(*, state_token: str, profile_id: int | None, account_id: int | None, account_name: str | None, redirect_uri: str, scopes: list[str], db_path: Path | None = None) -> ThreadsOAuthRequest:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO threads_oauth_requests (
                state_token, profile_id, account_id, account_name,
                redirect_uri, scopes_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (state_token, profile_id, account_id, account_name, redirect_uri, json.dumps(scopes, ensure_ascii=False), 'started'),
        )
        conn.commit()
    return get_oauth_request(state_token, db_path=db_path)


def get_oauth_request(state_token: str, *, db_path: Path | None = None) -> ThreadsOAuthRequest | None:
    with _connect(db_path) as conn:
        row = conn.execute('SELECT * FROM threads_oauth_requests WHERE state_token = ?', (state_token,)).fetchone()
    return _row_to_request(row) if row else None


def complete_oauth_request(state_token: str, *, status: str, error_text: str | None = None, result: dict | None = None, db_path: Path | None = None) -> ThreadsOAuthRequest:
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE threads_oauth_requests
            SET status = ?, error_text = ?, result_json = ?, completed_at = CURRENT_TIMESTAMP
            WHERE state_token = ?
            """,
            (status, error_text, json.dumps(result or {}, ensure_ascii=False), state_token),
        )
        conn.commit()
    request = get_oauth_request(state_token, db_path=db_path)
    if request is None:
        raise LookupError(f'Threads OAuth request not found: {state_token}')
    return request


def latest_oauth_request(*, account_id: int | None = None, db_path: Path | None = None) -> ThreadsOAuthRequest | None:
    query = 'SELECT * FROM threads_oauth_requests'
    params: list[object] = []
    if account_id is not None:
        query += ' WHERE account_id = ?'
        params.append(account_id)
    query += ' ORDER BY requested_at DESC, state_token DESC LIMIT 1'
    with _connect(db_path) as conn:
        row = conn.execute(query, params).fetchone()
    return _row_to_request(row) if row else None
