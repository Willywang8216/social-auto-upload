"""Campaign persistence and state transitions."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from utils.conf_defaults import BASE_DIR

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

# Async prep-queue bookkeeping. It lives inside ``campaigns.metadata_json`` so
# no schema migration is required: the submit payload is stored under
# ``prepRequest`` and the worker's in-flight lease under ``_prepLease`` (the
# attempt counter is ``_prepAttempts``). See ``logs/async-prep-queue-notes.md``.
PREP_REQUEST_KEY = "prepRequest"
PREP_LEASE_KEY = "_prepLease"
PREP_ATTEMPTS_KEY = "_prepAttempts"

# A prep may legitimately run for many minutes (ffmpeg). The lease window is
# deliberately generous so it never fires on a slow-but-alive prep; it only
# recovers a worker that died mid-run. Mirrors jobs.requeue_stale_running.
DEFAULT_PREP_LEASE_MINUTES = 120

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
    file_record_ids: list[int] | None = None
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
    # Wait out a concurrent writer (the publish worker) instead of failing the
    # read outright; see the matching note in myUtils.jobs._connect.
    conn = sqlite3.connect(resolved, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    try:
        yield conn
    finally:
        conn.close()


def _chunk(values: Sequence[int], size: int = 400) -> Iterator[Sequence[int]]:
    """Split ids for ``IN (...)`` clauses; SQLite's variable limit is the cap."""
    for start in range(0, len(values), size):
        yield values[start:start + size]


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
    file_record_ids = None
    try:
        raw = row["file_record_ids_json"]
    except (IndexError, KeyError):
        raw = None
    if raw:
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, list):
                file_record_ids = [int(v) for v in decoded]
        except (TypeError, ValueError):
            file_record_ids = None
    return CampaignPost(
        id=row["id"],
        campaign_id=row["campaign_id"],
        platform=row["platform"],
        account_ids=_json_load(row["account_ids_json"], []),
        draft=_json_load(row["draft_json"], {}),
        sheet_row=_json_load(row["sheet_row_json"], {}),
        status=row["status"],
        last_published_job_id=row["last_published_job_id"],
        file_record_ids=file_record_ids,
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
    workspace_id: str | None = None,
    db_path: Path | None = None,
) -> Campaign:
    with _connect(db_path) as conn:
        if workspace_id is not None:
            cursor = conn.execute(
                """
                INSERT INTO campaigns (
                    profile_id,
                    media_group_id,
                    status,
                    selected_account_ids_json,
                    sheet_spreadsheet_id,
                    sheet_title,
                    metadata_json,
                    workspace_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    media_group_id,
                    status,
                    json.dumps(selected_account_ids or [], ensure_ascii=False),
                    sheet_spreadsheet_id,
                    sheet_title,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    workspace_id,
                ),
            )
        else:
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
    return get_campaign(campaign_id, workspace_id=workspace_id, db_path=db_path)


def get_campaign(
    campaign_id: int, *, workspace_id: str | None = None, db_path: Path | None = None
) -> Campaign:
    """Fetch a campaign. When ``workspace_id`` is given, a campaign owned by
    another workspace is treated as not found (tenant isolation)."""
    with _connect(db_path) as conn:
        if workspace_id is not None:
            row = conn.execute(
                "SELECT * FROM campaigns WHERE id = ? AND workspace_id = ?",
                (campaign_id, workspace_id),
            ).fetchone()
        else:
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
    workspace_id: str | None = None,
    db_path: Path | None = None,
) -> list[Campaign]:
    query = "SELECT * FROM campaigns"
    clauses: list[str] = []
    params: list[object] = []
    if workspace_id is not None:
        clauses.append("workspace_id = ?")
        params.append(workspace_id)
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


def _parse_metadata(raw: str | None) -> dict:
    """Best-effort decode of a campaign's ``metadata_json`` column."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _lease_is_live(metadata: dict, *, cutoff: str) -> bool:
    lease = metadata.get(PREP_LEASE_KEY)
    if not isinstance(lease, dict):
        return False
    claimed_at = str(lease.get("claimedAt") or "")
    return bool(claimed_at) and claimed_at >= cutoff


def get_prep_request(campaign: Campaign) -> dict | None:
    """The persisted submit payload for a campaign, or ``None`` for legacy rows."""
    request = (campaign.metadata or {}).get(PREP_REQUEST_KEY)
    return request if isinstance(request, dict) else None


def has_preparing_campaigns(*, db_path: Path | None = None) -> bool:
    """Cheap existence check for any ``preparing`` campaign (leased or not)."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM campaigns WHERE status = ? LIMIT 1",
            (CAMPAIGN_PREPARING,),
        ).fetchone()
    return row is not None


def has_claimable_preparing_campaigns(
    *,
    stale_after_minutes: int = DEFAULT_PREP_LEASE_MINUTES,
    db_path: Path | None = None,
) -> bool:
    """True when at least one ``preparing`` campaign is free to claim.

    Unlike :func:`has_preparing_campaigns`, a campaign currently leased by a
    live worker does not count. The worker's drain loop uses this so it does
    not spin forever waiting on a campaign another worker is already running
    (which would never become claimable from this process).
    """
    now_dt = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    cutoff = (now_dt - timedelta(minutes=int(stale_after_minutes))).isoformat(
        timespec="seconds"
    )
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT metadata_json FROM campaigns WHERE status = ?",
            (CAMPAIGN_PREPARING,),
        ).fetchall()
    for row in rows:
        metadata = _parse_metadata(row["metadata_json"] or "{}")
        if not _lease_is_live(metadata, cutoff=cutoff):
            return True
    return False


def claim_next_preparing_campaign(
    *,
    owner: str,
    stale_after_minutes: int = DEFAULT_PREP_LEASE_MINUTES,
    db_path: Path | None = None,
) -> Campaign | None:
    """Atomically claim a ``preparing`` campaign for async prep.

    The claim is a compare-and-swap on the campaign's ``metadata_json``: the
    row is only taken when its status is still ``preparing`` and its metadata
    is byte-for-byte what this caller observed. Two workers racing the same
    campaign therefore cannot both win - the loser's UPDATE matches zero rows.

    A campaign whose existing lease is older than ``stale_after_minutes`` is
    reclaimable, so a crashed predecessor never strands the row (see
    :func:`requeue_stale_preparing` for the explicit sweep). The claim records
    ``_prepLease.owner`` / ``_prepLease.claimedAt`` and bumps ``_prepAttempts``.
    """
    now_dt = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    now = now_dt.isoformat(timespec="seconds")
    cutoff = (now_dt - timedelta(minutes=int(stale_after_minutes))).isoformat(
        timespec="seconds"
    )
    claimed_id: int | None = None

    with _connect(db_path) as conn:
        conn.isolation_level = None  # explicit transaction
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT * FROM campaigns WHERE status = ? ORDER BY id",
                (CAMPAIGN_PREPARING,),
            ).fetchall()
            for row in rows:
                raw = row["metadata_json"] or "{}"
                metadata = _parse_metadata(raw)
                if _lease_is_live(metadata, cutoff=cutoff):
                    continue  # a live worker holds it
                attempts = int(metadata.get(PREP_ATTEMPTS_KEY) or 0) + 1
                next_metadata = dict(metadata)
                next_metadata[PREP_LEASE_KEY] = {"owner": owner, "claimedAt": now}
                next_metadata[PREP_ATTEMPTS_KEY] = attempts
                cursor = conn.execute(
                    """
                    UPDATE campaigns
                    SET metadata_json = ?
                    WHERE id = ? AND status = ? AND metadata_json = ?
                    """,
                    (
                        json.dumps(next_metadata, ensure_ascii=False),
                        row["id"],
                        CAMPAIGN_PREPARING,
                        raw,
                    ),
                )
                if cursor.rowcount == 1:
                    claimed_id = row["id"]
                    break
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    if claimed_id is None:
        return None
    return get_campaign(claimed_id, db_path=db_path)


def requeue_stale_preparing(
    *,
    older_than_minutes: int = DEFAULT_PREP_LEASE_MINUTES,
    max_attempts: int = 3,
    db_path: Path | None = None,
) -> int:
    """Recover campaigns abandoned mid-prep by a dead worker.

    Mirrors :func:`myUtils.jobs.requeue_stale_running`. A campaign still
    ``preparing`` with a lease older than the cutoff is either released for
    another attempt or, once ``_prepAttempts`` reaches ``max_attempts``, moved
    to ``needs_review`` with an explanatory ``last_error`` so it cannot loop
    forever. Returns the number of rows moved.
    """
    now_dt = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    cutoff = (now_dt - timedelta(minutes=int(older_than_minutes))).isoformat(
        timespec="seconds"
    )
    moved = 0

    with _connect(db_path) as conn:
        conn.isolation_level = None  # explicit transaction
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT * FROM campaigns WHERE status = ? ORDER BY id",
                (CAMPAIGN_PREPARING,),
            ).fetchall()
            for row in rows:
                raw = row["metadata_json"] or "{}"
                metadata = _parse_metadata(raw)
                lease = metadata.get(PREP_LEASE_KEY)
                if not isinstance(lease, dict):
                    continue
                claimed_at = str(lease.get("claimedAt") or "")
                if claimed_at and claimed_at >= cutoff:
                    continue  # still live
                attempts = int(metadata.get(PREP_ATTEMPTS_KEY) or 0)
                next_metadata = dict(metadata)
                next_metadata.pop(PREP_LEASE_KEY, None)
                if attempts >= int(max_attempts):
                    cursor = conn.execute(
                        """
                        UPDATE campaigns
                        SET status = ?, metadata_json = ?, last_error = ?
                        WHERE id = ? AND status = ? AND metadata_json = ?
                        """,
                        (
                            CAMPAIGN_NEEDS_REVIEW,
                            json.dumps(next_metadata, ensure_ascii=False),
                            f"prep lease expired after {attempts} attempt(s); "
                            "worker died mid-prep",
                            row["id"],
                            CAMPAIGN_PREPARING,
                            raw,
                        ),
                    )
                else:
                    cursor = conn.execute(
                        """
                        UPDATE campaigns
                        SET metadata_json = ?
                        WHERE id = ? AND status = ? AND metadata_json = ?
                        """,
                        (
                            json.dumps(next_metadata, ensure_ascii=False),
                            row["id"],
                            CAMPAIGN_PREPARING,
                            raw,
                        ),
                    )
                if cursor.rowcount == 1:
                    moved += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return moved


def finish_campaign_prep(
    campaign_id: int,
    *,
    owner: str,
    status: str,
    prepared_at: str | None | object = _UNSET,
    published_at: str | None | object = _UNSET,
    last_error: str | None | object = _UNSET,
    db_path: Path | None = None,
) -> bool:
    """Lease-guarded terminal transition for a claimed prep.

    Returns ``True`` only when this caller still owns the lease and the CAS
    succeeds. ``False`` means another worker reclaimed the campaign (typically
    after this one stalled past the lease window); the caller must discard its
    result rather than clobber the new owner's state.
    """
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"Campaign not found: id={campaign_id}")
        raw = row["metadata_json"] or "{}"
        metadata = _parse_metadata(raw)
        lease = metadata.get(PREP_LEASE_KEY)
        if not isinstance(lease, dict) or str(lease.get("owner") or "") != str(owner):
            return False
        next_metadata = dict(metadata)
        next_metadata.pop(PREP_LEASE_KEY, None)
        sets = ["status = ?", "metadata_json = ?"]
        params: list[object] = [status, json.dumps(next_metadata, ensure_ascii=False)]
        if prepared_at is not _UNSET:
            sets.append("prepared_at = ?")
            params.append(prepared_at)
        if published_at is not _UNSET:
            sets.append("published_at = ?")
            params.append(published_at)
        if last_error is not _UNSET:
            sets.append("last_error = ?")
            params.append(last_error)
        params.extend((campaign_id, CAMPAIGN_PREPARING, raw))
        cursor = conn.execute(
            f"UPDATE campaigns SET {', '.join(sets)} "
            "WHERE id = ? AND status = ? AND metadata_json = ?",
            params,
        )
        conn.commit()
        return cursor.rowcount == 1


def delete_campaign(
    campaign_id: int, *, workspace_id: str | None = None, db_path: Path | None = None
) -> None:
    with _connect(db_path) as conn:
        if workspace_id is not None:
            conn.execute(
                "DELETE FROM campaigns WHERE id = ? AND workspace_id = ?",
                (campaign_id, workspace_id),
            )
        else:
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
    file_record_ids: list[int] | None = None,
    db_path: Path | None = None,
) -> CampaignPost:
    file_ids_json: str | None = None
    if file_record_ids is not None:
        file_ids_json = json.dumps([int(v) for v in file_record_ids], ensure_ascii=False)
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
                last_published_job_id,
                file_record_ids_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                campaign_id,
                platform,
                json.dumps(account_ids or [], ensure_ascii=False),
                json.dumps(draft or {}, ensure_ascii=False),
                json.dumps(sheet_row or {}, ensure_ascii=False),
                status,
                last_published_job_id,
                file_ids_json,
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


def get_campaigns_by_ids(
    campaign_ids: Iterable[int],
    *,
    workspace_id: str | None = None,
    db_path: Path | None = None,
) -> dict[int, Campaign]:
    """Batch form of :func:`get_campaign`; missing ids are absent from the map."""
    ids = sorted({int(campaign_id) for campaign_id in campaign_ids})
    if not ids:
        return {}
    found: dict[int, Campaign] = {}
    with _connect(db_path) as conn:
        for chunk in _chunk(ids):
            placeholders = ",".join("?" * len(chunk))
            query = f"SELECT * FROM campaigns WHERE id IN ({placeholders})"
            params: list = list(chunk)
            if workspace_id is not None:
                query += " AND workspace_id = ?"
                params.append(workspace_id)
            for row in conn.execute(query, params):
                campaign = _row_to_campaign(row)
                found[campaign.id] = campaign
    return found


def list_posts_for_campaigns(
    campaign_ids: Iterable[int], *, db_path: Path | None = None
) -> dict[int, list[CampaignPost]]:
    """Batch form of :func:`list_campaign_posts`; every id gets a list."""
    ids = sorted({int(campaign_id) for campaign_id in campaign_ids})
    grouped: dict[int, list[CampaignPost]] = {campaign_id: [] for campaign_id in ids}
    if not ids:
        return grouped
    with _connect(db_path) as conn:
        for chunk in _chunk(ids):
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT * FROM campaign_posts WHERE campaign_id IN ({placeholders}) "
                "ORDER BY campaign_id, id",
                chunk,
            ).fetchall()
            for row in rows:
                post = _row_to_campaign_post(row)
                grouped.setdefault(post.campaign_id, []).append(post)
    return grouped


def list_artifacts_for_campaigns(
    campaign_ids: Iterable[int], *, db_path: Path | None = None
) -> dict[int, list[CampaignArtifact]]:
    """Batch form of :func:`list_campaign_artifacts`; every id gets a list."""
    ids = sorted({int(campaign_id) for campaign_id in campaign_ids})
    grouped: dict[int, list[CampaignArtifact]] = {campaign_id: [] for campaign_id in ids}
    if not ids:
        return grouped
    with _connect(db_path) as conn:
        for chunk in _chunk(ids):
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT * FROM campaign_artifacts WHERE campaign_id IN ({placeholders}) "
                "ORDER BY campaign_id, id",
                chunk,
            ).fetchall()
            for row in rows:
                artifact = _row_to_campaign_artifact(row)
                grouped.setdefault(artifact.campaign_id, []).append(artifact)
    return grouped


def update_campaign_post(
    post_id: int,
    *,
    account_ids: list[int] | None = None,
    draft: dict | None = None,
    sheet_row: dict | None = None,
    status: str | None = None,
    last_published_job_id: int | None | object = _UNSET,
    file_record_ids: list[int] | None | object = _UNSET,
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
    if file_record_ids is _UNSET:
        next_file_record_ids = current.file_record_ids
    else:
        next_file_record_ids = (
            None
            if file_record_ids is None
            else [int(v) for v in file_record_ids]
        )
    file_ids_json = (
        None
        if next_file_record_ids is None
        else json.dumps(next_file_record_ids, ensure_ascii=False)
    )
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE campaign_posts
            SET account_ids_json = ?, draft_json = ?, sheet_row_json = ?,
                status = ?, last_published_job_id = ?,
                file_record_ids_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(next_account_ids, ensure_ascii=False),
                json.dumps(next_draft or {}, ensure_ascii=False),
                json.dumps(next_sheet_row or {}, ensure_ascii=False),
                next_status,
                next_last_published_job_id,
                file_ids_json,
                _now_iso(),
                post_id,
            ),
        )
        conn.commit()
    return get_campaign_post(post_id, db_path=db_path)
