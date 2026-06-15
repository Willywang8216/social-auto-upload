"""Google Sheet and CSV export service.

Handles exporting prepared posts to Google Sheets with the exact 20-column
schema, and CSV download with the same format.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from utils.conf_defaults import BASE_DIR

DB_PATH = Path(BASE_DIR) / "db" / "database.db"

# Exact 20 columns in order
SHEET_COLUMN_ORDER = [
    "Message",
    "Link",
    "ImageURL",
    "VideoURL",
    "Month(1-12)",
    "Day(1-31)",
    "Year",
    "Hour",
    "Minute(0-59)",
    "PinTitle",
    "Category",
    "Watermark",
    "HashtagGroup",
    "VideoThumbnailURL",
    "CTAGroup",
    "FirstComment",
    "Story(YorN)",
    "PinterestBoard",
    "AltText",
    "PostPreset",
]

# Platforms excluded from Google Sheet export by default
SHEET_EXCLUDED_PLATFORMS = {"telegram", "patreon", "discord"}

# Platform message character limits for validation
PLATFORM_CHAR_LIMITS = {
    "facebook": 63206,
    "instagram": 2200,
    "bluesky": 300,
    "linkedin": 3000,
    "twitter": 280,
    "google_my_business": 1500,
    "pinterest": 500,
    "tiktok": 150,
}

# Maximum images per row for sheet export
MAX_IMAGE_URLS = 4


@dataclass(slots=True)
class SheetExport:
    id: int
    campaign_id: int | None
    profile_id: int | None
    sheet_name: str
    spreadsheet_id: str
    spreadsheet_url: str
    exported_at: str | None
    row_count: int
    status: str
    error_message: str
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
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_export(row: sqlite3.Row) -> SheetExport:
    data = {key: row[key] for key in row.keys()}
    # Filter to only valid dataclass fields (protects against schema evolution)
    valid = {f.name for f in fields(SheetExport)}
    data = {k: v for k, v in data.items() if k in valid}
    return SheetExport(**data)


# --------------- CRUD ---------------

def create_sheet_export(
    *,
    campaign_id: int | None = None,
    profile_id: int | None = None,
    sheet_name: str = "",
    spreadsheet_id: str = "",
    spreadsheet_url: str = "",
    row_count: int = 0,
    status: str = "pending",
    error_message: str = "",
    db_path: Path | None = None,
) -> SheetExport:
    now = _now_iso()
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO sheet_exports
            (campaign_id, profile_id, sheet_name, spreadsheet_id, spreadsheet_url,
             exported_at, row_count, status, error_message, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (campaign_id, profile_id, sheet_name, spreadsheet_id, spreadsheet_url,
             now, row_count, status, error_message, now),
        )
        conn.commit()
        return get_sheet_export(cur.lastrowid, db_path=db_path)


def get_sheet_export(export_id: int, *, db_path: Path | None = None) -> SheetExport:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM sheet_exports WHERE id = ?", (export_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"SheetExport {export_id} not found")
        return _row_to_export(row)


def list_sheet_exports(
    *,
    campaign_id: int | None = None,
    profile_id: int | None = None,
    db_path: Path | None = None,
) -> list[SheetExport]:
    with _connect(db_path) as conn:
        query = "SELECT * FROM sheet_exports WHERE 1=1"
        params: list = []
        if campaign_id is not None:
            query += " AND campaign_id = ?"
            params.append(campaign_id)
        if profile_id is not None:
            query += " AND profile_id = ?"
            params.append(profile_id)
        query += " ORDER BY created_at DESC"
        rows = conn.execute(query, params).fetchall()
        return [_row_to_export(r) for r in rows]


def update_sheet_export(
    export_id: int, *, db_path: Path | None = None, **fields
) -> SheetExport:
    allowed = {"sheet_name", "spreadsheet_id", "spreadsheet_url", "row_count", "status", "error_message"}
    updates = {}
    for k, v in fields.items():
        if k in allowed:
            updates[k] = v
    if not updates:
        return get_sheet_export(export_id, db_path=db_path)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [export_id]
    with _connect(db_path) as conn:
        conn.execute(f"UPDATE sheet_exports SET {set_clause} WHERE id = ?", values)
        conn.commit()
    return get_sheet_export(export_id, db_path=db_path)


# --------------- Validation ---------------

def validate_sheet_row(row: dict, platform: str | None = None) -> list[str]:
    """Validate a single sheet row. Returns list of validation errors."""
    errors = []

    # ImageURL and VideoURL mutual exclusion
    image_url = row.get("ImageURL", "").strip()
    video_url = row.get("VideoURL", "").strip()
    if image_url and video_url:
        errors.append("ImageURL and VideoURL cannot both be populated in the same row")

    # Maximum 4 image URLs
    if image_url:
        urls = [u.strip() for u in image_url.split(",") if u.strip()]
        if len(urls) > MAX_IMAGE_URLS:
            errors.append(f"Maximum {MAX_IMAGE_URLS} image URLs allowed (got {len(urls)})")

    # VideoURL should be .mp4
    if video_url and not video_url.lower().endswith(".mp4"):
        errors.append(f"VideoURL should be a direct .mp4 link (got: {video_url})")

    # Platform character limits
    message = row.get("Message", "")
    if platform:
        limit = PLATFORM_CHAR_LIMITS.get(platform)
        if limit and len(message) > limit:
            errors.append(f"Message exceeds {platform} limit of {limit} chars (got {len(message)})")

    # Scheduling fields: all or none
    schedule_fields = ["Month(1-12)", "Day(1-31)", "Year", "Hour", "Minute(0-59)"]
    filled = [(row.get(f) or "").strip() for f in schedule_fields]
    any_filled = any(filled)
    all_filled = all(filled)
    if any_filled and not all_filled:
        errors.append("Scheduling fields must all be filled or all be empty")

    return errors


# --------------- Row building ---------------

def build_sheet_row_from_post(post) -> dict:
    """Convert a PreparedPost (or dict) to a sheet row dict with exact 20 columns."""
    if hasattr(post, "to_dict"):
        post = post.to_dict()

    image_urls = post.get("image_urls", "")
    video_url = post.get("video_url", "")

    # Build image URL string (comma-separated, no spaces)
    if isinstance(image_urls, list):
        image_url_str = ",".join(u.strip() for u in image_urls if u.strip())
    else:
        image_url_str = str(image_urls or "").replace(" ", "")

    row = {
        "Message": post.get("message", ""),
        "Link": post.get("link", ""),
        "ImageURL": image_url_str,
        "VideoURL": video_url or "",
        "Month(1-12)": post.get("scheduled_month", ""),
        "Day(1-31)": post.get("scheduled_day", ""),
        "Year": post.get("scheduled_year", ""),
        "Hour": post.get("scheduled_hour", ""),
        "Minute(0-59)": post.get("scheduled_minute", ""),
        "PinTitle": post.get("title", ""),
        "Category": post.get("category", ""),
        "Watermark": post.get("watermark_name", ""),
        "HashtagGroup": post.get("hashtag_group", ""),
        "VideoThumbnailURL": post.get("video_thumbnail_url", ""),
        "CTAGroup": post.get("cta_group", ""),
        "FirstComment": post.get("first_comment", ""),
        "Story(YorN)": "Y" if post.get("story_flag") else "N",
        "PinterestBoard": post.get("pinterest_board", ""),
        "AltText": post.get("alt_text", ""),
        "PostPreset": post.get("post_preset", ""),
    }
    return row


def build_sheet_rows(
    posts: list,
    *,
    exclude_platforms: set[str] | None = None,
) -> list[dict]:
    """Build sheet rows from a list of PreparedPost objects.

    Excludes Telegram, Patreon, Discord by default.
    """
    if exclude_platforms is None:
        exclude_platforms = SHEET_EXCLUDED_PLATFORMS

    rows = []
    for post in posts:
        platform = post.get("platform", "") if isinstance(post, dict) else getattr(post, "platform", "")
        if platform.lower() in exclude_platforms:
            continue
        rows.append(build_sheet_row_from_post(post))
    return rows


# --------------- CSV export ---------------

def generate_csv(rows: list[dict]) -> str:
    """Generate CSV string with exact 20 columns in order."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=SHEET_COLUMN_ORDER, extrasaction="ignore", restval="")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return output.getvalue()


def generate_csv_bytes(rows: list[dict]) -> bytes:
    """Generate CSV bytes with UTF-8 BOM for Excel compatibility."""
    csv_str = generate_csv(rows)
    return csv_str.encode("utf-8-sig")


# --------------- Google Sheet export ---------------

def export_to_google_sheet(
    rows: list[dict],
    sheet_name: str,
    *,
    spreadsheet_id: str | None = None,
    folder_id: str | None = None,
) -> dict:
    """Export rows to a Google Sheet. Returns {spreadsheet_id, spreadsheet_url, row_count}."""
    from myUtils.google_sheets import (
        SheetExportResult,
        create_or_update_spreadsheet,
        load_service_account_info,
    )

    service_account_info = load_service_account_info()
    result = create_or_update_spreadsheet(
        service_account_info=service_account_info,
        sheet_name=sheet_name,
        rows=rows,
        column_order=SHEET_COLUMN_ORDER,
        spreadsheet_id=spreadsheet_id,
        folder_id=folder_id,
    )
    return result.to_dict() if hasattr(result, "to_dict") else result


def generate_sheet_name(profile_slug: str, *, date: datetime | None = None) -> str:
    """Generate sheet name in format YYYY-MM-DD_PROFILE-SLUG."""
    dt = date or datetime.now(tz=timezone.utc)
    return f"{dt.strftime('%Y-%m-%d')}_{profile_slug}"
