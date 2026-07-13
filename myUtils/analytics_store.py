"""Database operations for video analytics tables.

Follows the same sqlite3 + dataclass pattern as myUtils/profiles.py.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from utils.conf_defaults import BASE_DIR

DB_PATH = Path(BASE_DIR) / "db" / "database.db"


@dataclass(frozen=True, slots=True)
class AnalyticsVideo:
    id: int
    account_id: int
    platform: str
    platform_video_id: str
    file_record_id: int | None
    title: str
    description: str
    thumbnail_url: str
    published_at: str | None
    duration_seconds: int
    last_synced_at: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class AnalyticsSnapshot:
    id: int
    account_id: int
    platform: str
    platform_video_id: str
    file_record_id: int | None
    title: str
    thumbnail_url: str
    published_at: str | None
    snapshot_at: str
    views: int
    likes: int
    comments: int
    shares: int
    watch_time_seconds: int
    engagement_rate: float
    raw_metrics_json: str


@dataclass(frozen=True, slots=True)
class SyncLogEntry:
    id: int
    account_id: int
    platform: str
    status: str
    videos_synced: int
    error_text: str | None
    started_at: str
    finished_at: str | None


@contextmanager
def _connect(db_path: Path = DB_PATH):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_video(
    *,
    account_id: int,
    platform: str,
    platform_video_id: str,
    title: str = "",
    description: str = "",
    thumbnail_url: str = "",
    published_at: str | None = None,
    duration_seconds: int = 0,
    file_record_id: int | None = None,
    db_path: Path = DB_PATH,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO video_analytics_videos
               (account_id, platform, platform_video_id, title, description,
                thumbnail_url, published_at, duration_seconds, file_record_id, last_synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(platform_video_id) DO UPDATE SET
                account_id = excluded.account_id,
                title = excluded.title,
                description = excluded.description,
                thumbnail_url = excluded.thumbnail_url,
                published_at = excluded.published_at,
                duration_seconds = excluded.duration_seconds,
                file_record_id = COALESCE(excluded.file_record_id, video_analytics_videos.file_record_id),
                last_synced_at = excluded.last_synced_at
            """,
            (account_id, platform, platform_video_id, title, description,
             thumbnail_url, published_at, duration_seconds, file_record_id, now),
        )


def record_snapshot(
    *,
    account_id: int,
    platform: str,
    platform_video_id: str,
    views: int = 0,
    likes: int = 0,
    comments: int = 0,
    shares: int = 0,
    watch_time_seconds: int = 0,
    engagement_rate: float = 0.0,
    title: str = "",
    thumbnail_url: str = "",
    published_at: str | None = None,
    file_record_id: int | None = None,
    raw_metrics: dict | None = None,
    db_path: Path = DB_PATH,
) -> int:
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO video_analytics_snapshots
               (account_id, platform, platform_video_id, views, likes, comments, shares,
                watch_time_seconds, engagement_rate, title, thumbnail_url, published_at,
                file_record_id, raw_metrics_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (account_id, platform, platform_video_id, views, likes, comments, shares,
             watch_time_seconds, engagement_rate, title, thumbnail_url, published_at,
             file_record_id, json.dumps(raw_metrics or {})),
        )
        return cursor.lastrowid


def cleanup_orphaned_snapshots(db_path: Path = DB_PATH) -> int:
    """Remove snapshots whose account no longer exists. Returns count deleted."""
    with _connect(db_path) as conn:
        result = conn.execute(
            """DELETE FROM video_analytics_snapshots
               WHERE account_id NOT IN (SELECT id FROM accounts)"""
        )
        return result.rowcount


def cleanup_orphaned_videos(db_path: Path = DB_PATH) -> int:
    """Remove video records whose account no longer exists. Returns count deleted."""
    with _connect(db_path) as conn:
        result = conn.execute(
            """DELETE FROM video_analytics_videos
               WHERE account_id NOT IN (SELECT id FROM accounts)"""
        )
        return result.rowcount


def remove_stale_videos(account_id: int, platform: str, current_video_ids: set[str], db_path: Path = DB_PATH) -> int:
    """Remove videos for an account that are no longer in the current API response.

    After syncing, call this with the set of video IDs returned by the platform API.
    Videos in the DB for this account that are NOT in current_video_ids will be deleted
    along with their snapshots.
    """
    with _connect(db_path) as conn:
        # Get existing video IDs for this account+platform
        rows = conn.execute(
            "SELECT platform_video_id FROM video_analytics_videos WHERE account_id = ? AND platform = ?",
            (account_id, platform),
        ).fetchall()
        existing_ids = {r["platform_video_id"] for r in rows}
        stale_ids = existing_ids - current_video_ids
        if not stale_ids:
            return 0
        placeholders = ",".join("?" * len(stale_ids))
        conn.execute(
            f"DELETE FROM video_analytics_snapshots WHERE platform_video_id IN ({placeholders}) AND account_id = ?",
            (*stale_ids, account_id),
        )
        conn.execute(
            f"DELETE FROM video_analytics_videos WHERE platform_video_id IN ({placeholders}) AND account_id = ?",
            (*stale_ids, account_id),
        )
        logger.info("Removed %d stale %s videos for account %d", len(stale_ids), platform, account_id)
        return len(stale_ids)


def get_video_thumbnail(platform_video_id: str, db_path: Path = DB_PATH) -> str | None:
    """Return the stored thumbnail_url for a video, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT thumbnail_url FROM video_analytics_videos WHERE platform_video_id = ?",
            (platform_video_id,),
        ).fetchone()
        if row and row["thumbnail_url"]:
            return row["thumbnail_url"]
        row = conn.execute(
            "SELECT thumbnail_url FROM video_analytics_snapshots WHERE platform_video_id = ? ORDER BY snapshot_at DESC LIMIT 1",
            (platform_video_id,),
        ).fetchone()
        return row["thumbnail_url"] if row and row["thumbnail_url"] else None


def _workspace_account_clause(column: str) -> str:
    """SQL predicate restricting ``column`` (an account_id) to a workspace.

    Analytics rows are anchored on ``account_id`` (NOT NULL, FK to accounts);
    accounts carry ``workspace_id``. Isolation therefore derives from the owning
    account rather than a separate workspace_id column on each analytics row, so
    it holds for data synced before tenancy was enabled.
    """
    return f"{column} IN (SELECT id FROM accounts WHERE workspace_id = ?)"


def get_latest_snapshots(
    *,
    platform: str | None = None,
    account_id: int | None = None,
    limit: int = 100,
    workspace_id: str | None = None,
    db_path: Path = DB_PATH,
) -> list[dict]:
    """Get the most recent snapshot for each video, with optional filters."""
    conditions = []
    params: list[Any] = []
    if platform:
        conditions.append("s.platform = ?")
        params.append(platform)
    if account_id:
        conditions.append("v.account_id = ?")
        params.append(account_id)
    if workspace_id is not None:
        conditions.append(_workspace_account_clause("s.account_id"))
        params.append(workspace_id)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    conjunction = "AND" if conditions else "WHERE"
    params.append(limit)

    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""SELECT s.*, v.description, v.duration_seconds
                FROM video_analytics_snapshots s
                LEFT JOIN video_analytics_videos v
                  ON s.platform_video_id = v.platform_video_id
                {where}
                {conjunction} s.id = (
                    SELECT s2.id FROM video_analytics_snapshots s2
                    WHERE s2.platform_video_id = s.platform_video_id
                    ORDER BY s2.snapshot_at DESC LIMIT 1
                )
                ORDER BY s.snapshot_at DESC
                LIMIT ?""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_snapshot_history(
    platform_video_id: str,
    days: int = 30,
    workspace_id: str | None = None,
    db_path: Path = DB_PATH,
) -> list[dict]:
    """Time series of snapshots for one video."""
    extra = ""
    params: list[Any] = [platform_video_id, f"-{days} days"]
    if workspace_id is not None:
        extra = " AND " + _workspace_account_clause("account_id")
        params.append(workspace_id)
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""SELECT snapshot_at, views, likes, comments, shares,
                      watch_time_seconds, engagement_rate
               FROM video_analytics_snapshots
               WHERE platform_video_id = ?
                 AND snapshot_at >= datetime('now', ?)
                 {extra}
               ORDER BY snapshot_at ASC""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_aggregate_stats(
    *,
    platform: str | None = None,
    account_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    workspace_id: str | None = None,
    db_path: Path = DB_PATH,
) -> dict:
    """Aggregated stats using the latest snapshot per video."""
    conditions = []
    params: list[Any] = []
    if platform:
        conditions.append("s.platform = ?")
        params.append(platform)
    if account_id:
        conditions.append("s.account_id = ?")
        params.append(account_id)
    if workspace_id is not None:
        conditions.append(_workspace_account_clause("s.account_id"))
        params.append(workspace_id)
    if date_from:
        conditions.append("s.snapshot_at >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("s.snapshot_at <= ?")
        params.append(date_to)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    conjunction = "AND" if conditions else "WHERE"

    with _connect(db_path) as conn:
        row = conn.execute(
            f"""SELECT
                COALESCE(SUM(s.views), 0) as total_views,
                COALESCE(SUM(s.likes), 0) as total_likes,
                COALESCE(SUM(s.comments), 0) as total_comments,
                COALESCE(SUM(s.shares), 0) as total_shares,
                COALESCE(AVG(s.engagement_rate), 0.0) as avg_engagement_rate,
                COUNT(DISTINCT s.platform_video_id) as video_count
            FROM video_analytics_snapshots s
            {where}
            {conjunction} s.id = (
                SELECT s2.id FROM video_analytics_snapshots s2
                WHERE s2.platform_video_id = s.platform_video_id
                AND s2.account_id = s.account_id
                ORDER BY s2.snapshot_at DESC LIMIT 1
            )""",
            params,
        ).fetchone()

        result = dict(row) if row else {
            "total_views": 0, "total_likes": 0, "total_comments": 0,
            "total_shares": 0, "avg_engagement_rate": 0.0, "video_count": 0,
        }

        # Per-platform breakdown
        plat_rows = conn.execute(
            f"""SELECT
                s.platform,
                COALESCE(SUM(s.views), 0) as total_views,
                COALESCE(SUM(s.likes), 0) as total_likes,
                COALESCE(SUM(s.comments), 0) as total_comments,
                COALESCE(SUM(s.shares), 0) as total_shares,
                COALESCE(AVG(s.engagement_rate), 0.0) as avg_engagement_rate,
                COUNT(DISTINCT s.platform_video_id) as video_count
            FROM video_analytics_snapshots s
            {where}
            {conjunction} s.id = (
                SELECT s2.id FROM video_analytics_snapshots s2
                WHERE s2.platform_video_id = s.platform_video_id
                AND s2.account_id = s.account_id
                ORDER BY s2.snapshot_at DESC LIMIT 1
            )
            GROUP BY s.platform""",
            params,
        ).fetchall()

        result["per_platform"] = {r["platform"]: dict(r) for r in plat_rows}
        return result


def get_top_videos(
    *,
    platform: str | None = None,
    account_id: int | None = None,
    metric: str = "views",
    limit: int = 10,
    workspace_id: str | None = None,
    db_path: Path = DB_PATH,
) -> list[dict]:
    allowed_metrics = {"views", "likes", "comments", "shares", "engagement_rate"}
    if metric not in allowed_metrics:
        metric = "views"

    conditions = []
    params: list[Any] = []
    if platform:
        conditions.append("s.platform = ?")
        params.append(platform)
    if account_id:
        conditions.append("v.account_id = ?")
        params.append(account_id)
    if workspace_id is not None:
        conditions.append(_workspace_account_clause("s.account_id"))
        params.append(workspace_id)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    conjunction = "AND" if conditions else "WHERE"
    params.append(limit)

    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""SELECT s.platform, s.platform_video_id, s.title, s.thumbnail_url,
                       s.published_at, s.views, s.likes, s.comments, s.shares,
                       s.engagement_rate, v.description, v.duration_seconds
                FROM video_analytics_snapshots s
                LEFT JOIN video_analytics_videos v
                  ON s.platform_video_id = v.platform_video_id
                {where}
                {conjunction} s.id = (
                    SELECT s2.id FROM video_analytics_snapshots s2
                    WHERE s2.platform_video_id = s.platform_video_id
                    ORDER BY s2.snapshot_at DESC LIMIT 1
                )
                ORDER BY s.{metric} DESC
                LIMIT ?""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_trends(
    *,
    platform: str | None = None,
    account_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    metric: str = "views",
    workspace_id: str | None = None,
    db_path: Path = DB_PATH,
) -> list[dict]:
    """Daily aggregated metric trends."""
    allowed_metrics = {"views", "likes", "comments", "shares", "engagement_rate"}
    if metric not in allowed_metrics:
        metric = "views"

    conditions = []
    params: list[Any] = []
    if platform:
        conditions.append("platform = ?")
        params.append(platform)
    if account_id:
        conditions.append("account_id = ?")
        params.append(account_id)
    if workspace_id is not None:
        conditions.append(_workspace_account_clause("account_id"))
        params.append(workspace_id)
    if date_from:
        conditions.append("snapshot_at >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("snapshot_at <= ?")
        params.append(date_to)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    agg = f"SUM({metric})" if metric != "engagement_rate" else f"AVG({metric})"

    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""SELECT DATE(snapshot_at) as date, {agg} as value
                FROM video_analytics_snapshots
                {where}
                GROUP BY DATE(snapshot_at)
                ORDER BY date ASC""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


# --- Sync log management ---

def record_sync_start(
    account_id: int,
    platform: str,
    db_path: Path = DB_PATH,
) -> int:
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """INSERT INTO analytics_sync_log (account_id, platform, status)
               VALUES (?, ?, 'running')""",
            (account_id, platform),
        )
        return cursor.lastrowid


def record_sync_finish(
    sync_id: int,
    *,
    status: str = "completed",
    videos_synced: int = 0,
    error_text: str | None = None,
    db_path: Path = DB_PATH,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            """UPDATE analytics_sync_log
               SET status = ?, videos_synced = ?, error_text = ?, finished_at = ?
               WHERE id = ?""",
            (status, videos_synced, error_text, now, sync_id),
        )


def list_sync_log(
    account_id: int | None = None,
    limit: int = 20,
    workspace_id: str | None = None,
    db_path: Path = DB_PATH,
) -> list[dict]:
    conditions = []
    params: list[Any] = []
    if account_id:
        conditions.append("account_id = ?")
        params.append(account_id)
    if workspace_id is not None:
        conditions.append(_workspace_account_clause("account_id"))
        params.append(workspace_id)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""SELECT * FROM analytics_sync_log
                {where}
                ORDER BY started_at DESC
                LIMIT ?""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]
