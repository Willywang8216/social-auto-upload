"""Media-group persistence.

A media group is a user-defined bundle of uploaded files that should be treated
as one campaign input (for example: one primary video plus several images).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from conf import BASE_DIR

DB_PATH = Path(BASE_DIR) / "db" / "database.db"

ROLE_VIDEO = "video"
ROLE_IMAGE = "image"
ROLE_THUMBNAIL = "thumbnail"
ROLE_ATTACHMENT = "attachment"

SUPPORTED_ITEM_ROLES = {
    ROLE_VIDEO,
    ROLE_IMAGE,
    ROLE_THUMBNAIL,
    ROLE_ATTACHMENT,
}

_UNSET = object()


@dataclass(slots=True)
class MediaGroup:
    id: int
    name: str
    notes: str
    primary_video_file_id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class MediaGroupItem:
    id: int
    media_group_id: int
    file_record_id: int
    role: str
    sort_order: int
    created_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _resolve_db_path(db_path: Path | None) -> Path:
    return db_path if db_path is not None else DB_PATH


@contextmanager
def _connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    resolved = _resolve_db_path(db_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _row_to_media_group(row: sqlite3.Row) -> MediaGroup:
    return MediaGroup(**{key: row[key] for key in row.keys()})


def _row_to_media_group_item(row: sqlite3.Row) -> MediaGroupItem:
    return MediaGroupItem(**{key: row[key] for key in row.keys()})


def _assert_role(role: str) -> None:
    if role not in SUPPORTED_ITEM_ROLES:
        raise ValueError(f"Unsupported media-group item role: {role!r}")


def create_media_group(
    name: str,
    *,
    notes: str = "",
    primary_video_file_id: int | None = None,
    db_path: Path | None = None,
) -> MediaGroup:
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO media_groups (name, notes, primary_video_file_id)
            VALUES (?, ?, ?)
            """,
            (name.strip(), notes.strip(), primary_video_file_id),
        )
        conn.commit()
        media_group_id = cursor.lastrowid
    return get_media_group(media_group_id, db_path=db_path)


def get_media_group(media_group_id: int, *, db_path: Path | None = None) -> MediaGroup:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM media_groups WHERE id = ?",
            (media_group_id,),
        ).fetchone()
    if row is None:
        raise LookupError(f"Media group not found: id={media_group_id}")
    return _row_to_media_group(row)


def list_media_groups(*, db_path: Path | None = None) -> list[MediaGroup]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM media_groups ORDER BY id DESC"
        ).fetchall()
    return [_row_to_media_group(row) for row in rows]


def update_media_group(
    media_group_id: int,
    *,
    name: str | None = None,
    notes: str | None = None,
    primary_video_file_id: int | None | object = _UNSET,
    db_path: Path | None = None,
) -> MediaGroup:
    current = get_media_group(media_group_id, db_path=db_path)
    next_name = current.name if name is None else name.strip()
    next_notes = current.notes if notes is None else notes.strip()
    next_primary_video = (
        current.primary_video_file_id
        if primary_video_file_id is _UNSET
        else primary_video_file_id
    )
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE media_groups
            SET name = ?, notes = ?, primary_video_file_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (next_name, next_notes, next_primary_video, _now_iso(), media_group_id),
        )
        conn.commit()
    return get_media_group(media_group_id, db_path=db_path)


def delete_media_group(media_group_id: int, *, db_path: Path | None = None) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM media_groups WHERE id = ?", (media_group_id,))
        conn.commit()


def add_media_group_item(
    media_group_id: int,
    file_record_id: int,
    *,
    role: str = ROLE_ATTACHMENT,
    sort_order: int | None = None,
    db_path: Path | None = None,
) -> MediaGroupItem:
    _assert_role(role)
    with _connect(db_path) as conn:
        if sort_order is None:
            row = conn.execute(
                """
                SELECT COALESCE(MAX(sort_order), -1) AS max_sort_order
                FROM media_group_items
                WHERE media_group_id = ?
                """,
                (media_group_id,),
            ).fetchone()
            sort_order = int(row["max_sort_order"]) + 1
        cursor = conn.execute(
            """
            INSERT INTO media_group_items (media_group_id, file_record_id, role, sort_order)
            VALUES (?, ?, ?, ?)
            """,
            (media_group_id, file_record_id, role, sort_order),
        )
        conn.commit()
        item_id = cursor.lastrowid
    return get_media_group_item(item_id, db_path=db_path)


def get_media_group_item(item_id: int, *, db_path: Path | None = None) -> MediaGroupItem:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM media_group_items WHERE id = ?",
            (item_id,),
        ).fetchone()
    if row is None:
        raise LookupError(f"Media group item not found: id={item_id}")
    return _row_to_media_group_item(row)


def list_media_group_items(
    media_group_id: int,
    *,
    db_path: Path | None = None,
) -> list[MediaGroupItem]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM media_group_items
            WHERE media_group_id = ?
            ORDER BY sort_order, id
            """,
            (media_group_id,),
        ).fetchall()
    return [_row_to_media_group_item(row) for row in rows]


def replace_media_group_items(
    media_group_id: int,
    items: Sequence[tuple[int, str]],
    *,
    db_path: Path | None = None,
) -> list[MediaGroupItem]:
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM media_group_items WHERE media_group_id = ?",
            (media_group_id,),
        )
        for index, (file_record_id, role) in enumerate(items):
            _assert_role(role)
            conn.execute(
                """
                INSERT INTO media_group_items (media_group_id, file_record_id, role, sort_order)
                VALUES (?, ?, ?, ?)
                """,
                (media_group_id, file_record_id, role, index),
            )
        conn.commit()
    return list_media_group_items(media_group_id, db_path=db_path)
