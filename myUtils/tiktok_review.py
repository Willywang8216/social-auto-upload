"""Durable TikTok review / callback persistence."""

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
class TikTokOAuthRequest:
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


@dataclass(slots=True)
class TikTokReviewEvent:
    id: int
    event_type: str
    status: str
    account_id: int | None
    account_name: str | None
    signature_verified: bool | None
    signature_status: str | None
    payload: dict | None = None
    headers: dict | None = None
    metadata: dict | None = None
    received_at: str | None = None

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


def _row_to_request(row: sqlite3.Row) -> TikTokOAuthRequest:
    return TikTokOAuthRequest(
        state_token=row["state_token"],
        profile_id=row["profile_id"],
        account_id=row["account_id"],
        account_name=row["account_name"],
        redirect_uri=row["redirect_uri"],
        scopes=_json_load(row["scopes_json"], []),
        status=row["status"],
        requested_at=row["requested_at"],
        completed_at=row["completed_at"],
        error_text=row["error_text"],
        result=_json_load(row["result_json"], {}),
    )


def _row_to_event(row: sqlite3.Row) -> TikTokReviewEvent:
    signature_verified = row["signature_verified"]
    return TikTokReviewEvent(
        id=row["id"],
        event_type=row["event_type"],
        status=row["status"],
        account_id=row["account_id"],
        account_name=row["account_name"],
        signature_verified=None if signature_verified is None else bool(signature_verified),
        signature_status=row["signature_status"],
        payload=_json_load(row["payload_json"], {}),
        headers=_json_load(row["headers_json"], {}),
        metadata=_json_load(row["metadata_json"], {}),
        received_at=row["received_at"],
    )


def create_oauth_request(
    *,
    state_token: str,
    profile_id: int | None,
    account_id: int | None,
    account_name: str | None,
    redirect_uri: str,
    scopes: list[str],
    db_path: Path | None = None,
) -> TikTokOAuthRequest:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO tiktok_oauth_requests (
                state_token, profile_id, account_id, account_name,
                redirect_uri, scopes_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state_token,
                profile_id,
                account_id,
                account_name,
                redirect_uri,
                json.dumps(scopes, ensure_ascii=False),
                "started",
            ),
        )
        conn.commit()
    return get_oauth_request(state_token, db_path=db_path)


def get_oauth_request(state_token: str, *, db_path: Path | None = None) -> TikTokOAuthRequest | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM tiktok_oauth_requests WHERE state_token = ?",
            (state_token,),
        ).fetchone()
    return _row_to_request(row) if row else None


def complete_oauth_request(
    state_token: str,
    *,
    status: str,
    error_text: str | None = None,
    result: dict | None = None,
    db_path: Path | None = None,
) -> TikTokOAuthRequest:
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE tiktok_oauth_requests
            SET status = ?, error_text = ?, result_json = ?, completed_at = CURRENT_TIMESTAMP
            WHERE state_token = ?
            """,
            (
                status,
                error_text,
                json.dumps(result or {}, ensure_ascii=False),
                state_token,
            ),
        )
        conn.commit()
    request = get_oauth_request(state_token, db_path=db_path)
    if request is None:
        raise LookupError(f"TikTok OAuth request not found: {state_token}")
    return request


def add_review_event(
    *,
    event_type: str,
    status: str,
    account_id: int | None = None,
    account_name: str | None = None,
    signature_verified: bool | None = None,
    signature_status: str | None = None,
    payload: dict | None = None,
    headers: dict | None = None,
    metadata: dict | None = None,
    db_path: Path | None = None,
) -> TikTokReviewEvent:
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO tiktok_review_events (
                event_type, status, account_id, account_name,
                signature_verified, signature_status,
                payload_json, headers_json, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                status,
                account_id,
                account_name,
                None if signature_verified is None else int(signature_verified),
                signature_status,
                json.dumps(payload or {}, ensure_ascii=False),
                json.dumps(headers or {}, ensure_ascii=False),
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
        event_id = int(cursor.lastrowid)
    event = get_review_event(event_id, db_path=db_path)
    if event is None:
        raise LookupError(f"TikTok review event not found: id={event_id}")
    return event


def get_review_event(event_id: int, *, db_path: Path | None = None) -> TikTokReviewEvent | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM tiktok_review_events WHERE id = ?",
            (event_id,),
        ).fetchone()
    return _row_to_event(row) if row else None


def latest_review_event(event_type: str, *, db_path: Path | None = None) -> TikTokReviewEvent | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM tiktok_review_events
            WHERE event_type = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (event_type,),
        ).fetchone()
    return _row_to_event(row) if row else None


def list_recent_review_events(*, limit: int = 25, db_path: Path | None = None) -> list[TikTokReviewEvent]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM tiktok_review_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_row_to_event(row) for row in rows]
