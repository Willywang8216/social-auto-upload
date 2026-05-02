"""Campaign persistence and state transitions."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from conf import BASE_DIR

DB_PATH = Path(BASE_DIR) / "db" / "database.db"

CAMPAIGN_DRAFT = "draft"
CAMPAIGN_PREPARING = "preparing"
CAMPAIGN_NEEDS_REVIEW = "needs_review"
CAMPAIGN_PREPARED = "prepared"
CAMPAIGN_PUBLISHING = "publishing"
CAMPAIGN_PUBLISHED = "published"
CAMPAIGN_FAILED = "failed"

CAMPAIGN_POST_DRAFT = "draft"
CAMPAIGN_POST_READY = "ready"
CAMPAIGN_POST_QUEUED = "queued"
CAMPAIGN_POST_PUBLISHED = "published"
CAMPAIGN_POST_FAILED = "failed"

_UNSET = object()


@dataclass(slots=True)
class Campaign:
    id: int
    profile_id: int
    media_group_id: int
    status: str
    selected_account_ids: list[int]
    sheet_spreadsheet_id: str | None = None
    sheet_title: str | None = None
    metadata: dict | None = None
    created_at: str | None = None
    prepared_at: str | None = None
    published_at: str | None = None
    last_error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class CampaignArtifact:
    id: int
    campaign_id: int
    source_file_record_id: int | None
    artifact_kind: str
    local_path: str | None = None
    public_url: str | None = None
    remote_path: str | None = None
    metadata: dict | None = None
    created_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class CampaignPost:
    id: int
    campaign_id: int
    platform: str
    account_ids: list[int]
    draft: dict | None = None
    sheet_row: dict | None = None
    status: str = CAMPAIGN_POST_DRAFT
    last_published_job_id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None

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


def _json_load(raw: str | None, fallback):
    if not raw:
        return fallback
    return json.loads(raw)


def _row_to_campaign(row: sqlite3.Row) -> Campaign:
    return Campaign(
        id=row["id"],
        profile_id=row["profile_id"],
        media_group_id=row["media_group_id"],
        status=row["status"],
        selected_account_ids=_json_load(row["selected_account_ids_json"], []),
        sheet_spreadsheet_id=row["sheet_spreadsheet_id"],
        sheet_title=row["sheet_title"],
        metadata=_json_load(row["metadata_json"], {}),
        created_at=row["created_at"],
        prepared_at=row["prepared_at"],
        published_at=row["published_at"],
        last_error=row["last_error"],
    )


def _row_to_campaign_artifact(row: sqlite3.Row) -> CampaignArtifact:
    return CampaignArtifact(
        id=row["id"],
        campaign_id=row["campaign_id"],
        source_file_record_id=row["source_file_record_id"],
        artifact_kind=row["artifact_kind"],
        local_path=row["local_path"],
        public_url=row["public_url"],
        remote_path=row["remote_path"],
        metadata=_json_load(row["metadata_json"], {}),
        created_at=row["created_at"],
    )


def _row_to_campaign_post(row: sqlite3.Row) -> CampaignPost:
    return CampaignPost(
        id=row["id"],
        campaign_id=row["campaign_id"],
        platform=row["platform"],
        account_ids=_json_load(row["account_ids_json"], []),
        draft=_json_load(row["draft_json"], {}),
        sheet_row=_json_load(row["sheet_row_json"], {}),
        status=row["status"],
        last_published_job_id=row["last_published_job_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def create_campaign(
    profile_id: int,
    media_group_id: int,
    *,
    status: str = CAMPAIGN_DRAFT,
    selected_account_ids: list[int] | None = None,
    metadata: dict | None = None,
    sheet_spreadsheet_id: str | None = None,
    sheet_title: str | None = None,
    db_path: Path | None = None,
) -> Campaign:
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO campaigns (
                profile_id,
                media_group_id,
                status,
                selected_account_ids_json,
                sheet_spreadsheet_id,
                sheet_title,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile_id,
                media_group_id,
                status,
                json.dumps(selected_account_ids or [], ensure_ascii=False),
                sheet_spreadsheet_id,
                sheet_title,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
        campaign_id = cursor.lastrowid
    return get_campaign(campaign_id, db_path=db_path)


def get_campaign(campaign_id: int, *, db_path: Path | None = None) -> Campaign:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()
    if row is None:
        raise LookupError(f"Campaign not found: id={campaign_id}")
    return _row_to_campaign(row)


def list_campaigns(
    *,
    profile_id: int | None = None,
    status: str | None = None,
    db_path: Path | None = None,
) -> list[Campaign]:
    query = "SELECT * FROM campaigns"
    clauses: list[str] = []
    params: list[object] = []
    if profile_id is not None:
        clauses.append("profile_id = ?")
        params.append(profile_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id DESC"
    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_campaign(row) for row in rows]


def update_campaign(
    campaign_id: int,
    *,
    status: str | None = None,
    selected_account_ids: list[int] | None = None,
    metadata: dict | None = None,
    sheet_spreadsheet_id: str | None | object = _UNSET,
    sheet_title: str | None | object = _UNSET,
    prepared_at: str | None | object = _UNSET,
    published_at: str | None | object = _UNSET,
    last_error: str | None | object = _UNSET,
    db_path: Path | None = None,
) -> Campaign:
    current = get_campaign(campaign_id, db_path=db_path)
    next_status = current.status if status is None else status
    next_selected_account_ids = (
        current.selected_account_ids
        if selected_account_ids is None
        else selected_account_ids
    )
    next_metadata = current.metadata if metadata is None else metadata
    next_sheet_spreadsheet_id = (
        current.sheet_spreadsheet_id
        if sheet_spreadsheet_id is _UNSET
        else sheet_spreadsheet_id
    )
    next_sheet_title = (
        current.sheet_title
        if sheet_title is _UNSET
        else sheet_title
    )
    next_prepared_at = (
        current.prepared_at
        if prepared_at is _UNSET
        else prepared_at
    )
    next_published_at = (
        current.published_at
        if published_at is _UNSET
        else published_at
    )
    next_last_error = (
        current.last_error
        if last_error is _UNSET
        else last_error
    )
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE campaigns
            SET status = ?, selected_account_ids_json = ?, metadata_json = ?,
                sheet_spreadsheet_id = ?, sheet_title = ?, prepared_at = ?,
                published_at = ?, last_error = ?
            WHERE id = ?
            """,
            (
                next_status,
                json.dumps(next_selected_account_ids, ensure_ascii=False),
                json.dumps(next_metadata or {}, ensure_ascii=False),
                next_sheet_spreadsheet_id,
                next_sheet_title,
                next_prepared_at,
                next_published_at,
                next_last_error,
                campaign_id,
            ),
        )
        conn.commit()
    return get_campaign(campaign_id, db_path=db_path)


def delete_campaign(campaign_id: int, *, db_path: Path | None = None) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
        conn.commit()


def add_campaign_artifact(
    campaign_id: int,
    *,
    artifact_kind: str,
    source_file_record_id: int | None = None,
    local_path: str | None = None,
    public_url: str | None = None,
    remote_path: str | None = None,
    metadata: dict | None = None,
    db_path: Path | None = None,
) -> CampaignArtifact:
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO campaign_artifacts (
                campaign_id,
                source_file_record_id,
                artifact_kind,
                local_path,
                public_url,
                remote_path,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                campaign_id,
                source_file_record_id,
                artifact_kind,
                local_path,
                public_url,
                remote_path,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
        artifact_id = cursor.lastrowid
    return get_campaign_artifact(artifact_id, db_path=db_path)


def get_campaign_artifact(
    artifact_id: int,
    *,
    db_path: Path | None = None,
) -> CampaignArtifact:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM campaign_artifacts WHERE id = ?",
            (artifact_id,),
        ).fetchone()
    if row is None:
        raise LookupError(f"Campaign artifact not found: id={artifact_id}")
    return _row_to_campaign_artifact(row)


def list_campaign_artifacts(
    campaign_id: int,
    *,
    artifact_kind: str | None = None,
    db_path: Path | None = None,
) -> list[CampaignArtifact]:
    query = "SELECT * FROM campaign_artifacts WHERE campaign_id = ?"
    params: list[object] = [campaign_id]
    if artifact_kind is not None:
        query += " AND artifact_kind = ?"
        params.append(artifact_kind)
    query += " ORDER BY id"
    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_campaign_artifact(row) for row in rows]


def add_campaign_post(
    campaign_id: int,
    platform: str,
    *,
    account_ids: list[int] | None = None,
    draft: dict | None = None,
    sheet_row: dict | None = None,
    status: str = CAMPAIGN_POST_DRAFT,
    last_published_job_id: int | None = None,
    db_path: Path | None = None,
) -> CampaignPost:
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO campaign_posts (
                campaign_id,
                platform,
                account_ids_json,
                draft_json,
                sheet_row_json,
                status,
                last_published_job_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                campaign_id,
                platform,
                json.dumps(account_ids or [], ensure_ascii=False),
                json.dumps(draft or {}, ensure_ascii=False),
                json.dumps(sheet_row or {}, ensure_ascii=False),
                status,
                last_published_job_id,
            ),
        )
        conn.commit()
        post_id = cursor.lastrowid
    return get_campaign_post(post_id, db_path=db_path)


def get_campaign_post(post_id: int, *, db_path: Path | None = None) -> CampaignPost:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM campaign_posts WHERE id = ?",
            (post_id,),
        ).fetchone()
    if row is None:
        raise LookupError(f"Campaign post not found: id={post_id}")
    return _row_to_campaign_post(row)


def list_campaign_posts(
    campaign_id: int,
    *,
    platform: str | None = None,
    status: str | None = None,
    db_path: Path | None = None,
) -> list[CampaignPost]:
    query = "SELECT * FROM campaign_posts WHERE campaign_id = ?"
    params: list[object] = [campaign_id]
    if platform is not None:
        query += " AND platform = ?"
        params.append(platform)
    if status is not None:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY id"
    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_campaign_post(row) for row in rows]


def update_campaign_post(
    post_id: int,
    *,
    account_ids: list[int] | None = None,
    draft: dict | None = None,
    sheet_row: dict | None = None,
    status: str | None = None,
    last_published_job_id: int | None | object = _UNSET,
    db_path: Path | None = None,
) -> CampaignPost:
    current = get_campaign_post(post_id, db_path=db_path)
    next_account_ids = current.account_ids if account_ids is None else account_ids
    next_draft = current.draft if draft is None else draft
    next_sheet_row = current.sheet_row if sheet_row is None else sheet_row
    next_status = current.status if status is None else status
    next_last_published_job_id = (
        current.last_published_job_id
        if last_published_job_id is _UNSET
        else last_published_job_id
    )
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE campaign_posts
            SET account_ids_json = ?, draft_json = ?, sheet_row_json = ?,
                status = ?, last_published_job_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(next_account_ids, ensure_ascii=False),
                json.dumps(next_draft or {}, ensure_ascii=False),
                json.dumps(next_sheet_row or {}, ensure_ascii=False),
                next_status,
                next_last_published_job_id,
                _now_iso(),
                post_id,
            ),
        )
        conn.commit()
    return get_campaign_post(post_id, db_path=db_path)
