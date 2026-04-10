import json
import math
import mimetypes
import os
import posixpath
import random
import re
import shutil
import sqlite3
import subprocess
import tarfile
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

SHEET_COLUMNS = [
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

PLATFORM_LIMITS = {
    "twitter": 280,
    "threads": 500,
    "instagram": 2200,
    "facebook": 63206,
    "tiktok": 150,
    "youtube": 1400,
}

EXPORT_PLATFORMS = ["twitter", "threads", "instagram", "facebook", "youtube", "tiktok"]
NON_SHEET_PLATFORMS = ["telegram", "discord", "patreon"]
CONTENT_ACCOUNT_PLATFORMS = EXPORT_PLATFORMS + NON_SHEET_PLATFORMS + ["reddit"]
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
GOOGLE_SERVICE_ACCOUNT_FILENAME = "google_service_account.json"
PROFILE_CONFIG_VERSION = 1
PROFILE_CONFIG_EXAMPLE_FILENAME = "profile-config.example.yaml"
PROFILE_BACKUP_CONFIG_FILENAME = "profile_backup_config.json"
PROFILE_BACKUP_FILENAME_PREFIX = "profiles-backup-"
DEFAULT_PROFILE_BACKUP_SCHEDULE_TIME = "03:00"
DEFAULT_PROFILE_BACKUP_KEEP_COPIES = 3


def ensure_profile_tables(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                system_prompt TEXT DEFAULT '',
                contact_details TEXT DEFAULT '',
                cta TEXT DEFAULT '',
                settings_json TEXT NOT NULL DEFAULT '{}',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS profile_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                UNIQUE(profile_id, account_id),
                FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
                FOREIGN KEY(account_id) REFERENCES user_info(id) ON DELETE CASCADE
            )
            """
        )
        conn.commit()


def list_profiles(db_path: Path) -> list[dict[str, Any]]:
    ensure_profile_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT p.*,
                   GROUP_CONCAT(pa.account_id) AS account_ids
            FROM profiles p
            LEFT JOIN profile_accounts pa ON pa.profile_id = p.id
            GROUP BY p.id
            ORDER BY p.id DESC
            """
        )
        rows = cursor.fetchall()
    return [_serialize_profile_row(row) for row in rows]


def export_profiles_yaml(db_path: Path) -> str:
    profiles = list_profiles(db_path)
    payload = {
        "version": PROFILE_CONFIG_VERSION,
        "profiles": [_serialize_profile_export_item(profile) for profile in profiles],
    }
    yaml = _get_yaml_module()
    return yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def import_profiles_yaml(db_path: Path, yaml_text: str) -> dict[str, Any]:
    profiles_payload = _parse_profiles_yaml_payload(yaml_text)
    if not profiles_payload:
        raise ValueError("No profiles found in YAML")

    existing_profiles_by_name = {
        (item.get("name") or "").strip(): item
        for item in list_profiles(db_path)
        if (item.get("name") or "").strip()
    }

    created = 0
    updated = 0
    imported_profiles = []
    for item in profiles_payload:
        normalized_payload = _normalize_import_profile_payload(item)
        existing_profile = existing_profiles_by_name.get(normalized_payload["name"])
        if existing_profile and not normalized_payload.get("id"):
            normalized_payload["id"] = existing_profile["id"]

        saved_profile = save_profile(db_path, normalized_payload)
        imported_profiles.append(saved_profile)
        existing_profiles_by_name[saved_profile["name"]] = saved_profile

        if existing_profile or normalized_payload.get("id"):
            updated += 1
        else:
            created += 1

    return {
        "version": PROFILE_CONFIG_VERSION,
        "created": created,
        "updated": updated,
        "profiles": imported_profiles,
    }


def preview_profiles_yaml_import(db_path: Path, yaml_text: str) -> dict[str, Any]:
    profiles_payload = _parse_profiles_yaml_payload(yaml_text)
    existing_profiles_by_name = {
        (item.get("name") or "").strip(): _serialize_profile_export_item(item)
        for item in list_profiles(db_path)
        if (item.get("name") or "").strip()
    }

    items = []
    counts = {"create": 0, "update": 0, "unchanged": 0}
    for item in profiles_payload:
        normalized_payload = _normalize_import_profile_payload(item)
        exported_payload = _serialize_profile_export_item(normalized_payload)
        existing_profile = existing_profiles_by_name.get(exported_payload["name"])
        if not existing_profile:
            action = "create"
            changed_fields = ["name", "systemPrompt", "contactDetails", "cta", "accountIds", "settings"]
        else:
            changed_fields = _diff_profile_export_fields(existing_profile, exported_payload)
            action = "unchanged" if not changed_fields else "update"

        counts[action] += 1
        items.append({
            "name": exported_payload["name"],
            "action": action,
            "changedFields": changed_fields,
            "accountCount": len(exported_payload.get("accountIds") or []),
            "contentAccountCount": len(((exported_payload.get("settings") or {}).get("contentAccounts")) or []),
        })

    return {
        "version": PROFILE_CONFIG_VERSION,
        "summary": {
            "total": len(items),
            "create": counts["create"],
            "update": counts["update"],
            "unchanged": counts["unchanged"],
        },
        "items": items,
    }


def get_profile_config_example_yaml(base_dir: Path) -> str:
    path = get_profile_config_example_path(base_dir)
    if not path.exists():
        raise FileNotFoundError(f"Profile config example file not found: {path}")
    return path.read_text(encoding="utf-8")


def get_profile_backup_config(base_dir: Path, db_path: Path) -> dict[str, Any]:
    defaults = _build_default_profile_backup_config(db_path)
    storage_path = get_profile_backup_config_storage_path(base_dir)
    if not storage_path.exists():
        return defaults

    try:
        raw_data = json.loads(storage_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return defaults

    if not isinstance(raw_data, dict):
        return defaults
    return _normalize_profile_backup_config(raw_data, defaults)


def save_profile_backup_config(base_dir: Path, db_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    current = get_profile_backup_config(base_dir, db_path)
    config = _normalize_profile_backup_config(payload, current)
    storage_path = get_profile_backup_config_storage_path(base_dir)
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config


def run_profile_backup(base_dir: Path, db_path: Path) -> dict[str, Any]:
    config = get_profile_backup_config(base_dir, db_path)
    remote_name = (config.get("remoteName") or "").strip()
    remote_path = (config.get("remotePath") or "").strip().strip("/")
    if not remote_name:
        raise ValueError("backup remoteName is required")

    rclone_bin = shutil.which("rclone")
    if not rclone_bin:
        raise RuntimeError("rclone is not installed or not in PATH")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"{PROFILE_BACKUP_FILENAME_PREFIX}{timestamp}.tar.gz"
    local_dir = base_dir / "db" / "profile_backups"
    local_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_dir / filename
    temp_paths = _build_profile_backup_bundle(base_dir, db_path, local_path)

    remote_relative_path = _join_remote_path(remote_path, filename)
    remote_spec = f"{remote_name}:{remote_relative_path}"

    try:
        result = subprocess.run(
            [rclone_bin, "copyto", str(local_path), remote_spec],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "rclone backup upload failed")

        _prune_profile_backup_remote_files(rclone_bin, remote_name, remote_path, int(config.get("keepCopies") or DEFAULT_PROFILE_BACKUP_KEEP_COPIES))
        updated_config = save_profile_backup_config(
            base_dir,
            db_path,
            {
                **config,
                "lastBackupAt": datetime.now().isoformat(timespec="seconds"),
                "lastBackupRemoteSpec": remote_spec,
                "lastBackupStatus": "success",
            },
        )
        return {
            "backup": updated_config,
            "remoteSpec": remote_spec,
            "filename": filename,
        }
    except Exception:
        save_profile_backup_config(
            base_dir,
            db_path,
            {
                **config,
                "lastBackupStatus": "failed",
            },
        )
        raise
    finally:
        try:
            local_path.unlink()
        except OSError:
            pass
        for temp_path in temp_paths:
            try:
                temp_path.unlink()
            except OSError:
                pass


def run_scheduled_profile_backup_if_due(base_dir: Path, db_path: Path, now: datetime | None = None) -> dict[str, Any] | None:
    config = get_profile_backup_config(base_dir, db_path)
    if not config.get("enabled"):
        return None
    if not (config.get("remoteName") or "").strip():
        return None

    current_time = now or datetime.now()
    scheduled_hour, scheduled_minute = _parse_backup_schedule_time(config.get("scheduleTime"))
    if (current_time.hour, current_time.minute) < (scheduled_hour, scheduled_minute):
        return None

    last_backup_at = _parse_datetime(config.get("lastBackupAt"))
    if last_backup_at and last_backup_at.date() == current_time.date():
        return None

    return run_profile_backup(base_dir, db_path)


def save_profile(db_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    ensure_profile_tables(db_path)
    profile_id = payload.get("id")
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("profile name is required")

    settings = _normalize_profile_settings(payload.get("settings") or {})
    account_ids = _normalize_account_ids(payload.get("accountIds"))
    system_prompt = payload.get("systemPrompt") or ""
    contact_details = payload.get("contactDetails") or ""
    cta = payload.get("cta") or ""

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if profile_id:
            cursor.execute(
                """
                UPDATE profiles
                SET name = ?, system_prompt = ?, contact_details = ?, cta = ?, settings_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (name, system_prompt, contact_details, cta, json.dumps(settings, ensure_ascii=False), profile_id),
            )
            if cursor.rowcount == 0:
                raise ValueError("profile not found")
            cursor.execute("DELETE FROM profile_accounts WHERE profile_id = ?", (profile_id,))
        else:
            cursor.execute(
                """
                INSERT INTO profiles (name, system_prompt, contact_details, cta, settings_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (name, system_prompt, contact_details, cta, json.dumps(settings, ensure_ascii=False)),
            )
            profile_id = cursor.lastrowid

        if account_ids:
            cursor.executemany(
                "INSERT OR IGNORE INTO profile_accounts (profile_id, account_id) VALUES (?, ?)",
                [(profile_id, account_id) for account_id in account_ids],
            )

        conn.commit()
        cursor.execute(
            """
            SELECT p.*,
                   GROUP_CONCAT(pa.account_id) AS account_ids
            FROM profiles p
            LEFT JOIN profile_accounts pa ON pa.profile_id = p.id
            WHERE p.id = ?
            GROUP BY p.id
            """,
            (profile_id,),
        )
        row = cursor.fetchone()

    return _serialize_profile_row(row)


def delete_profile(db_path: Path, profile_id: int) -> None:
    ensure_profile_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM profile_accounts WHERE profile_id = ?", (profile_id,))
        cursor.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        conn.commit()
        if cursor.rowcount == 0:
            raise ValueError("profile not found")


def generate_profile_content(db_path: Path, base_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    ensure_profile_tables(db_path)
    profile_id = payload.get("profileId")
    material_id = payload.get("materialId")
    if not profile_id:
        raise ValueError("profileId is required")
    if not material_id:
        raise ValueError("materialId is required")

    profile = get_profile(db_path, int(profile_id))
    material = get_material_record(db_path, int(material_id))
    if not material:
        raise ValueError("material not found")

    source_path = Path(base_dir / "videoFile" / material["file_path"])
    if not source_path.exists():
        raise FileNotFoundError(f"material file not found: {source_path}")

    runtime_profile = deepcopy(profile)
    runtime_profile["selectedAccountIds"] = _resolve_selected_account_ids(runtime_profile, payload.get("selectedAccountIds"))
    runtime_profile["selectedContentAccountIds"] = _resolve_selected_content_account_ids(
        runtime_profile,
        payload.get("selectedContentAccountIds"),
    )
    runtime_social_settings = (((runtime_profile.get("settings") or {}).get("socialImport")) or {})
    if payload.get("link"):
        runtime_social_settings["defaultLink"] = payload.get("link")
        runtime_profile.setdefault("settings", {})["socialImport"] = runtime_social_settings

    processed_media_path = apply_watermark_if_needed(source_path, runtime_profile, base_dir)
    upload_result = upload_media(processed_media_path, profile)
    transcript = transcribe_media(processed_media_path, runtime_profile)
    content_account_results = generate_content_account_posts(runtime_profile, transcript, material, upload_result["publicUrl"])
    if content_account_results:
        normalized_posts = aggregate_account_posts(content_account_results)
        rows = build_google_sheet_rows(
            runtime_profile,
            normalized_posts,
            upload_result,
            payload.get("scheduleAt"),
            content_account_results,
        )
        row_mappings = build_google_sheet_row_mappings(normalized_posts, content_account_results)
    else:
        generated_posts = generate_posts(runtime_profile, transcript, material, upload_result["publicUrl"])
        normalized_posts = normalize_posts(generated_posts)
        rows = build_google_sheet_rows(runtime_profile, normalized_posts, upload_result, payload.get("scheduleAt"))
        row_mappings = build_google_sheet_row_mappings(normalized_posts)

    sheet_result = None
    if payload.get("writeToSheet", True):
        sheet_result = append_rows_to_google_sheet(runtime_profile, rows, payload.get("scheduleAt"))

    return {
        "profile": runtime_profile,
        "material": material,
        "processedMediaPath": str(processed_media_path),
        "storage": upload_result,
        "transcript": transcript,
        "posts": normalized_posts,
        "contentAccountResults": content_account_results,
        "sheetRows": [dict(zip(SHEET_COLUMNS, row)) for row in rows],
        "sheetRowMappings": row_mappings,
        "sheetResult": sheet_result,
    }


def generate_profile_batch_content(db_path: Path, base_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    material_ids = _normalize_account_ids(payload.get("materialIds"))
    if not material_ids:
        raise ValueError("materialIds is required")

    results = []
    for material_id in material_ids:
        item_payload = dict(payload)
        item_payload["materialId"] = material_id
        results.append(generate_profile_content(db_path, base_dir, item_payload))

    return {
        "profile": results[0]["profile"] if results else None,
        "selectedAccountIds": (results[0]["profile"] or {}).get("selectedAccountIds", []) if results else [],
        "selectedContentAccountIds": (results[0]["profile"] or {}).get("selectedContentAccountIds", []) if results else [],
        "results": results,
        "summary": {
            "materials": len(results),
            "sheetRows": sum(len(item.get("sheetRows") or []) for item in results),
            "worksheets": sorted(
                {
                    item.get("sheetResult", {}).get("worksheet")
                    for item in results
                    if item.get("sheetResult", {}).get("worksheet")
                }
            ),
        },
    }


def migrate_uploaded_asset(profile: dict[str, Any], relative_path: str, target_storage: dict[str, Any]) -> dict[str, str]:
    source_storage = ((profile.get("settings") or {}).get("storage") or {})
    source_remote_name = (source_storage.get("remoteName") or "").strip()
    source_remote_path = (source_storage.get("remotePath") or "").strip().strip("/")
    target_remote_name = (target_storage.get("remoteName") or "").strip()
    target_remote_path = (target_storage.get("remotePath") or "").strip().strip("/")
    if not source_remote_name or not target_remote_name:
        raise ValueError("source and target storage remotes are required")

    rclone_bin = shutil.which("rclone")
    if not rclone_bin:
        raise RuntimeError("rclone is not installed or not in PATH")

    relative_path = relative_path.strip().lstrip("/")
    source_spec = f"{source_remote_name}:{_join_remote_path(source_remote_path, relative_path)}"
    target_spec = f"{target_remote_name}:{_join_remote_path(target_remote_path, relative_path)}"
    result = subprocess.run(
        [rclone_bin, "copyto", source_spec, target_spec],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "rclone migration failed")

    public_url = _build_public_url(target_storage, relative_path, target_spec, Path(relative_path).name)
    return {
        "source": source_spec,
        "target": target_spec,
        "publicUrl": public_url,
    }


def get_profile(db_path: Path, profile_id: int) -> dict[str, Any]:
    ensure_profile_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT p.*,
                   GROUP_CONCAT(pa.account_id) AS account_ids
            FROM profiles p
            LEFT JOIN profile_accounts pa ON pa.profile_id = p.id
            WHERE p.id = ?
            GROUP BY p.id
            """,
            (profile_id,),
        )
        row = cursor.fetchone()
    if not row:
        raise ValueError("profile not found")
    return _serialize_profile_row(row)


def get_material_record(db_path: Path, material_id: int) -> dict[str, Any] | None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM file_records WHERE id = ?", (material_id,))
        row = cursor.fetchone()
    return dict(row) if row else None


def normalize_posts(posts: dict[str, Any]) -> dict[str, str]:
    normalized = {}
    for platform in EXPORT_PLATFORMS + NON_SHEET_PLATFORMS:
        value = posts.get(platform) or ""
        limit = PLATFORM_LIMITS.get(platform)
        normalized[platform] = trim_text(value, limit) if limit else value.strip()
    return normalized


def trim_text(value: str, limit: int | None) -> str:
    text = (value or "").strip()
    if not limit or len(text) <= limit:
        return text
    trimmed = text[: limit - 1].rstrip()
    return f"{trimmed}…"


def build_google_sheet_rows(
    profile: dict[str, Any],
    posts: dict[str, str],
    upload_result: dict[str, str],
    schedule_at: str | None,
    content_account_results: list[dict[str, Any]] | None = None,
) -> list[list[str]]:
    settings = profile.get("settings") or {}
    social_settings = settings.get("socialImport") or {}
    post_presets = settings.get("postPresets") or {}

    media_url = upload_result.get("publicUrl") or ""
    media_kind = upload_result.get("mediaKind") or "video"
    schedule_columns = _build_schedule_columns(schedule_at)

    rows = []
    if content_account_results:
        for result in content_account_results:
            account = result.get("account") or {}
            platform = (account.get("platform") or "").strip().lower()
            message = (result.get("content") or "").strip()
            if not message or platform not in EXPORT_PLATFORMS:
                continue

            row = [""] * len(SHEET_COLUMNS)
            row[0] = message
            row[1] = social_settings.get("defaultLink", "")
            row[2] = media_url if media_kind == "image" else ""
            row[3] = media_url if media_kind == "video" else ""
            row[4:9] = schedule_columns
            row[9] = social_settings.get("pinTitle", "")
            row[10] = social_settings.get("category", "")
            row[11] = social_settings.get("watermarkName", "")
            row[12] = social_settings.get("hashtagGroup", "")
            row[13] = social_settings.get("videoThumbnailUrl", "")
            row[14] = social_settings.get("ctaGroup", "")
            row[15] = social_settings.get("firstComment", "")
            row[16] = "Y" if social_settings.get("story") else ""
            row[17] = social_settings.get("pinterestBoard", "")
            row[18] = social_settings.get("altText", "")
            row[19] = (account.get("postPreset") or "").strip() or post_presets.get(platform, "")
            rows.append(row)
        return rows

    for platform in EXPORT_PLATFORMS:
        message = posts.get(platform, "").strip()
        if not message:
            continue

        row = [""] * len(SHEET_COLUMNS)
        row[0] = message
        row[1] = social_settings.get("defaultLink", "")
        row[2] = media_url if media_kind == "image" else ""
        row[3] = media_url if media_kind == "video" else ""
        row[4:9] = schedule_columns
        row[9] = social_settings.get("pinTitle", "")
        row[10] = social_settings.get("category", "")
        row[11] = social_settings.get("watermarkName", "")
        row[12] = social_settings.get("hashtagGroup", "")
        row[13] = social_settings.get("videoThumbnailUrl", "")
        row[14] = social_settings.get("ctaGroup", "")
        row[15] = social_settings.get("firstComment", "")
        row[16] = "Y" if social_settings.get("story") else ""
        row[17] = social_settings.get("pinterestBoard", "")
        row[18] = social_settings.get("altText", "")
        row[19] = post_presets.get(platform, "")
        rows.append(row)

    return rows


def build_google_sheet_row_mappings(
    posts: dict[str, str],
    content_account_results: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    mappings = []
    row_number = 2

    if content_account_results:
        for result in content_account_results:
            account = result.get("account") or {}
            platform = (account.get("platform") or "").strip().lower()
            message = (result.get("content") or "").strip()
            if not message or platform not in EXPORT_PLATFORMS:
                continue

            mappings.append({
                "rowNumber": row_number,
                "platform": platform,
                "accountId": str(account.get("id") or ""),
                "accountName": str(account.get("name") or ""),
                "postPreset": str(account.get("postPreset") or ""),
                "message": message,
            })
            row_number += 1
        return mappings

    for platform in EXPORT_PLATFORMS:
        message = (posts.get(platform) or "").strip()
        if not message:
            continue
        mappings.append({
            "rowNumber": row_number,
            "platform": platform,
            "accountId": "",
            "accountName": "",
            "postPreset": "",
            "message": message,
        })
        row_number += 1

    return mappings


def append_rows_to_google_sheet(profile: dict[str, Any], rows: list[list[str]], schedule_at: str | None = None) -> dict[str, Any]:
    if not rows:
        return {"appended": 0, "worksheet": None, "rowNumbers": []}

    settings = profile.get("settings") or {}
    google_sheet = settings.get("googleSheet") or {}
    spreadsheet_id = (google_sheet.get("spreadsheetId") or "").strip()
    worksheet_name = resolve_google_sheet_worksheet_name(profile, schedule_at)

    if not spreadsheet_id:
        raise ValueError("Google Sheet spreadsheetId is required")

    service_account_data = _load_google_service_account_data()
    if not service_account_data:
        raise RuntimeError("Google service account credentials are not configured")

    try:
        import gspread
    except ImportError as exc:
        raise RuntimeError("gspread is required to export rows to Google Sheets") from exc

    client = gspread.service_account_from_dict(service_account_data)
    spreadsheet = client.open_by_key(spreadsheet_id)
    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=1000, cols=len(SHEET_COLUMNS))

    existing_header = worksheet.row_values(1)
    if existing_header != SHEET_COLUMNS:
        worksheet.update("A1:T1", [SHEET_COLUMNS])

    start_row = max(len(worksheet.col_values(1)) + 1, 2)
    worksheet.append_rows(rows, value_input_option="USER_ENTERED")
    return {
        "appended": len(rows),
        "worksheet": worksheet_name,
        "spreadsheetId": spreadsheet_id,
        "rowNumbers": [start_row + index for index in range(len(rows))],
    }


def delete_rows_from_google_sheet(profile: dict[str, Any], worksheet_name: str, row_numbers: list[int]) -> dict[str, Any]:
    valid_rows = sorted({int(row) for row in (row_numbers or []) if int(row) > 1}, reverse=True)
    if not valid_rows:
        return {"deleted": 0, "worksheet": worksheet_name}

    settings = profile.get("settings") or {}
    google_sheet = settings.get("googleSheet") or {}
    spreadsheet_id = (google_sheet.get("spreadsheetId") or "").strip()
    if not spreadsheet_id:
        raise ValueError("Google Sheet spreadsheetId is required")

    service_account_data = _load_google_service_account_data()
    if not service_account_data:
        raise RuntimeError("Google service account credentials are not configured")

    try:
        import gspread
    except ImportError as exc:
        raise RuntimeError("gspread is required to delete rows from Google Sheets") from exc

    client = gspread.service_account_from_dict(service_account_data)
    spreadsheet = client.open_by_key(spreadsheet_id)
    worksheet = spreadsheet.worksheet(worksheet_name)
    for row_number in valid_rows:
        worksheet.delete_rows(row_number)
    return {
        "deleted": len(valid_rows),
        "worksheet": worksheet_name,
        "spreadsheetId": spreadsheet_id,
    }


def get_google_service_account_config(base_dir: Path) -> dict[str, Any]:
    env_json = _resolve_value(None, "SAU_GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SERVICE_ACCOUNT_JSON")
    if env_json:
        data, error = _parse_google_service_account_json(env_json, "GOOGLE_SERVICE_ACCOUNT_JSON")
        if error:
            return {
                "configured": False,
                "source": "env_json",
                "clientEmail": "",
                "projectId": "",
                "filePath": None,
                "error": error,
            }
        return {
            "configured": True,
            "source": "env_json",
            "clientEmail": data.get("client_email", ""),
            "projectId": data.get("project_id", ""),
            "filePath": None,
        }

    env_file = _resolve_value(None, "SAU_GOOGLE_SERVICE_ACCOUNT_FILE", "GOOGLE_SERVICE_ACCOUNT_FILE")
    if env_file:
        path = Path(env_file)
        if not path.exists():
            return {
                "configured": False,
                "source": "env_file",
                "clientEmail": "",
                "projectId": "",
                "filePath": str(path),
                "error": f"Google service account file not found: {path}",
            }
        raw_content, read_error = _read_text_file_for_config(path)
        if read_error:
            return {
                "configured": False,
                "source": "env_file",
                "clientEmail": "",
                "projectId": "",
                "filePath": str(path),
                "error": read_error,
            }
        data, error = _parse_google_service_account_json(raw_content, str(path))
        if error:
            return {
                "configured": False,
                "source": "env_file",
                "clientEmail": "",
                "projectId": "",
                "filePath": str(path),
                "error": error,
            }
        return {
            "configured": True,
            "source": "env_file",
            "clientEmail": data.get("client_email", ""),
            "projectId": data.get("project_id", ""),
            "filePath": str(path),
        }

    stored_path = get_google_service_account_storage_path(base_dir)
    if stored_path.exists():
        raw_content, read_error = _read_text_file_for_config(stored_path)
        if read_error:
            return {
                "configured": False,
                "source": "stored_file",
                "clientEmail": "",
                "projectId": "",
                "filePath": str(stored_path),
                "error": read_error,
            }
        data, error = _parse_google_service_account_json(raw_content, str(stored_path))
        if error:
            return {
                "configured": False,
                "source": "stored_file",
                "clientEmail": "",
                "projectId": "",
                "filePath": str(stored_path),
                "error": error,
            }
        return {
            "configured": True,
            "source": "stored_file",
            "clientEmail": data.get("client_email", ""),
            "projectId": data.get("project_id", ""),
            "filePath": str(stored_path),
        }

    return {
        "configured": False,
        "source": None,
        "clientEmail": "",
        "projectId": "",
        "filePath": str(stored_path),
        "error": "",
    }


def save_google_service_account_config(base_dir: Path, service_account_json: str) -> dict[str, Any]:
    data = json.loads((service_account_json or "").strip())
    _validate_google_service_account_payload(data)
    path = get_google_service_account_storage_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return get_google_service_account_config(base_dir)


def validate_google_sheet_connection(base_dir: Path, spreadsheet_id: str) -> dict[str, Any]:
    spreadsheet_id = (spreadsheet_id or "").strip()
    if not spreadsheet_id:
        raise ValueError("spreadsheetId is required")

    service_account_data = _load_google_service_account_data(base_dir)
    if not service_account_data:
        raise RuntimeError("Google service account credentials are not configured")

    try:
        import gspread
    except ImportError as exc:
        raise RuntimeError("gspread is required to validate Google Sheets access") from exc

    client = gspread.service_account_from_dict(service_account_data)
    spreadsheet = client.open_by_key(spreadsheet_id)
    worksheets = spreadsheet.worksheets()
    return {
        "spreadsheetId": spreadsheet_id,
        "title": spreadsheet.title,
        "worksheetCount": len(worksheets),
        "worksheets": [sheet.title for sheet in worksheets[:20]],
        "clientEmail": service_account_data.get("client_email", ""),
    }


def upload_media(local_path: Path, profile: dict[str, Any]) -> dict[str, str]:
    settings = profile.get("settings") or {}
    storage = settings.get("storage") or {}
    remote_name = (storage.get("remoteName") or "").strip()
    remote_path = (storage.get("remotePath") or "").strip().strip("/")
    if not remote_name:
        raise ValueError("storage remoteName is required")

    rclone_bin = shutil.which("rclone")
    if not rclone_bin:
        raise RuntimeError("rclone is not installed or not in PATH")

    profile_slug = slugify(profile.get("name") or "profile")
    relative_path = _join_remote_path(profile_slug, local_path.name)
    remote_spec = f"{remote_name}:{_join_remote_path(remote_path, relative_path)}"
    result = subprocess.run(
        [rclone_bin, "copyto", str(local_path), remote_spec],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "rclone upload failed")

    public_url = _build_public_url(storage, relative_path, remote_spec, local_path.name)
    return {
        "remoteSpec": remote_spec,
        "remoteRelativePath": relative_path,
        "publicUrl": public_url,
        "mediaKind": infer_media_kind(local_path),
    }


def transcribe_media(media_path: Path, profile: dict[str, Any]) -> str:
    requests = _get_requests_module()
    settings = profile.get("settings") or {}
    llm_settings = settings.get("llm") or {}
    api_base_url = _resolve_value(llm_settings.get("apiBaseUrl"), "SAU_LLM_API_BASE_URL", "LLM_API_BASE_URL")
    api_key = _resolve_value(None, "SAU_LLM_API_KEY", "LLM_API_KEY")
    model_name = (llm_settings.get("transcriptionModel") or "").strip()

    if not api_base_url:
        raise RuntimeError("LLM API base URL is not configured")
    if not api_key:
        raise RuntimeError("LLM API key is not configured")
    if not model_name:
        raise RuntimeError("transcription model is not configured")

    audio_path = media_path
    if infer_media_kind(media_path) == "video":
        audio_path = extract_audio_for_transcription(media_path)

    content_type = mimetypes.guess_type(audio_path.name)[0] or "application/octet-stream"
    with audio_path.open("rb") as audio_file:
        response = requests.post(
            f"{api_base_url.rstrip('/')}/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            data={"model": model_name},
            files={"file": (audio_path.name, audio_file, content_type)},
            timeout=300,
        )

    if response.status_code >= 400:
        raise RuntimeError(f"transcription request failed: {response.status_code} {response.text}")

    data = response.json()
    text = data.get("text") or data.get("transcript") or ""
    if not text:
        raise RuntimeError("transcription response did not include text")
    return text.strip()


def generate_posts(profile: dict[str, Any], transcript: str, material: dict[str, Any], media_url: str) -> dict[str, Any]:
    requests = _get_requests_module()
    settings = profile.get("settings") or {}
    llm_settings = settings.get("llm") or {}
    api_base_url = _resolve_value(llm_settings.get("apiBaseUrl"), "SAU_LLM_API_BASE_URL", "LLM_API_BASE_URL")
    api_key = _resolve_value(None, "SAU_LLM_API_KEY", "LLM_API_KEY")
    model_name = (llm_settings.get("generationModel") or "").strip()

    if not api_base_url:
        raise RuntimeError("LLM API base URL is not configured")
    if not api_key:
        raise RuntimeError("LLM API key is not configured")
    if not model_name:
        raise RuntimeError("generation model is not configured")

    system_prompt = build_generation_system_prompt(profile)
    user_prompt = build_generation_user_prompt(profile, transcript, material, media_url)

    response = requests.post(
        f"{api_base_url.rstrip('/')}/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model_name,
            "temperature": 0.7,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=300,
    )

    if response.status_code >= 400:
        raise RuntimeError(f"generation request failed: {response.status_code} {response.text}")

    data = response.json()
    content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
    payload = extract_json_payload(content)
    if not isinstance(payload, dict):
        raise RuntimeError("generation response did not contain a valid JSON object")
    return payload


def generate_content_account_posts(
    profile: dict[str, Any],
    transcript: str,
    material: dict[str, Any],
    media_url: str,
) -> list[dict[str, Any]]:
    content_accounts = _get_selected_content_accounts(profile)
    if not content_accounts:
        return []

    results = []
    for account in content_accounts:
        content = generate_post_for_content_account(profile, account, transcript, material, media_url)
        if not content:
            continue
        results.append({
            "account": {
                "id": account.get("id", ""),
                "name": account.get("name", ""),
                "platform": account.get("platform", ""),
                "postPreset": account.get("postPreset", ""),
            },
            "content": content,
        })
    return results


def generate_post_for_content_account(
    profile: dict[str, Any],
    content_account: dict[str, Any],
    transcript: str,
    material: dict[str, Any],
    media_url: str,
) -> str:
    requests = _get_requests_module()
    settings = profile.get("settings") or {}
    llm_settings = settings.get("llm") or {}
    api_base_url = _resolve_value(llm_settings.get("apiBaseUrl"), "SAU_LLM_API_BASE_URL", "LLM_API_BASE_URL")
    api_key = _resolve_value(None, "SAU_LLM_API_KEY", "LLM_API_KEY")
    model_name = (llm_settings.get("generationModel") or "").strip()

    if not api_base_url:
        raise RuntimeError("LLM API base URL is not configured")
    if not api_key:
        raise RuntimeError("LLM API key is not configured")
    if not model_name:
        raise RuntimeError("generation model is not configured")

    platform = (content_account.get("platform") or "").strip().lower()
    system_prompt = build_content_account_system_prompt(profile, content_account)
    user_prompt = build_content_account_user_prompt(profile, content_account, transcript, material, media_url)

    response = requests.post(
        f"{api_base_url.rstrip('/')}/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model_name,
            "temperature": 0.7,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=300,
    )

    if response.status_code >= 400:
        raise RuntimeError(f"generation request failed: {response.status_code} {response.text}")

    data = response.json()
    content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
    payload = extract_json_payload(content)
    if not isinstance(payload, dict):
        raise RuntimeError("generation response did not contain a valid JSON object")

    text = (payload.get("content") or payload.get(platform) or "").strip()
    if not text:
        raise RuntimeError(f"generation response did not contain content for {platform}")
    limit = PLATFORM_LIMITS.get(platform)
    return trim_text(text, limit) if limit else text


def build_generation_system_prompt(profile: dict[str, Any]) -> str:
    custom_prompt = (profile.get("systemPrompt") or "").strip()
    return (
        "You are a social media content strategist. "
        "Return valid JSON only with these keys: twitter, threads, instagram, facebook, youtube, tiktok, telegram, discord, patreon. "
        "Do not wrap the JSON in markdown. "
        "Requirements: twitter and threads must include emoji and exactly 3 hashtags, plus natural contact details and a CTA. "
        "instagram and facebook should be long-form posts with contact details and a CTA woven naturally into the copy. "
        "youtube should be a post with a strong hook plus a useful description-style body. "
        "tiktok should be short, direct, and include a description-ready CTA. "
        "telegram should be concise and readable. "
        "discord should be community-friendly, readable in one screen, and include a soft CTA. "
        "patreon should be long-form and membership-oriented. "
        "Keep each platform within its typical character constraints. "
        f"{custom_prompt}"
    )


def build_content_account_system_prompt(profile: dict[str, Any], content_account: dict[str, Any]) -> str:
    platform = (content_account.get("platform") or "").strip().lower()
    platform_rules = {
        "twitter": "Write one X / Twitter post with emoji and exactly 3 hashtags. Keep it concise and under 280 characters.",
        "threads": "Write one Threads post with emoji and exactly 3 hashtags. It can be slightly more conversational than Twitter.",
        "instagram": "Write one Instagram long-form caption with a strong hook, natural CTA, and exactly 3 hashtags.",
        "facebook": "Write one Facebook long-form post with a natural CTA and exactly 3 hashtags.",
        "youtube": "Write one YouTube community-style post with a useful description tone and exactly 3 hashtags.",
        "tiktok": "Write one TikTok caption-style post with a direct hook, CTA, and exactly 3 hashtags.",
        "telegram": "Write one concise Telegram post that reads naturally and includes a CTA.",
        "discord": "Write one Discord announcement-style post that feels natural for a server channel and includes a soft CTA.",
        "patreon": "Write one Patreon-oriented long-form post that emphasizes membership value and CTA.",
        "reddit": "Write one Reddit post title plus body in a natural community tone without sounding promotional. Keep it readable and specific.",
    }.get(platform, "Write one platform-native social media post.")
    custom_prompt = "\n".join(
        [
            (profile.get("systemPrompt") or "").strip(),
            (content_account.get("prompt") or "").strip(),
        ]
    ).strip()
    return (
        "You are a social media content strategist. "
        "Return valid JSON only in the shape {\"content\": \"...\"}. "
        "Do not wrap the JSON in markdown. "
        f"{platform_rules} "
        f"{custom_prompt}"
    ).strip()


def build_generation_user_prompt(
    profile: dict[str, Any],
    transcript: str,
    material: dict[str, Any],
    media_url: str,
) -> str:
    contact_details = profile.get("contactDetails") or ""
    cta = profile.get("cta") or ""
    return (
        f"Material filename: {material.get('filename', '')}\n"
        f"Media URL: {media_url}\n"
        f"Contact details: {contact_details}\n"
        f"CTA: {cta}\n"
        "Create content in the same language as the transcript unless it is mostly non-linguistic.\n"
        "Base all copy on this transcript:\n"
        f"{transcript}"
    )


def build_content_account_user_prompt(
    profile: dict[str, Any],
    content_account: dict[str, Any],
    transcript: str,
    material: dict[str, Any],
    media_url: str,
) -> str:
    contact_details = (content_account.get("contactDetails") or profile.get("contactDetails") or "").strip()
    cta = (content_account.get("cta") or profile.get("cta") or "").strip()
    return (
        f"Target account platform: {content_account.get('platform', '')}\n"
        f"Target account name: {content_account.get('name', '')}\n"
        f"Material filename: {material.get('filename', '')}\n"
        f"Media URL: {media_url}\n"
        f"Contact details: {contact_details}\n"
        f"CTA: {cta}\n"
        "Create content in the same language as the transcript unless it is mostly non-linguistic.\n"
        "Base all copy on this transcript:\n"
        f"{transcript}"
    )


def extract_json_payload(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if not text:
        raise ValueError("empty content")

    candidates = [text]
    fenced_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced_match:
        candidates.insert(0, fenced_match.group(1))

    object_match = re.search(r"(\{.*\})", text, re.S)
    if object_match:
        candidates.append(object_match.group(1))

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ValueError("no valid JSON object found")


def apply_watermark_if_needed(source_path: Path, profile: dict[str, Any], base_dir: Path) -> Path:
    settings = profile.get("settings") or {}
    watermark = settings.get("watermark") or {}
    if not watermark.get("enabled"):
        return source_path

    media_kind = infer_media_kind(source_path)
    output_dir = base_dir / "generated_media"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{uuid.uuid4().hex}_{source_path.name}"

    if media_kind == "image":
        return _apply_image_watermark(source_path, output_path, watermark)
    if media_kind == "video":
        return _apply_video_watermark(source_path, output_path, watermark)
    return source_path


def infer_media_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    return "file"


def extract_audio_for_transcription(video_path: Path) -> Path:
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError("ffmpeg is required to extract audio from video files")

    output_path = video_path.with_suffix(".mp3")
    result = subprocess.run(
        [ffmpeg_bin, "-y", "-i", str(video_path), "-vn", "-acodec", "mp3", str(output_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "ffmpeg audio extraction failed")
    return output_path


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", (value or "").strip()).strip("-").lower()
    return slug or "profile"


def resolve_google_sheet_worksheet_name(profile: dict[str, Any], schedule_at: str | None = None) -> str:
    if schedule_at:
        try:
            base_date = datetime.fromisoformat(schedule_at)
        except ValueError as exc:
            raise ValueError("scheduleAt must be a valid ISO datetime string") from exc
    else:
        base_date = datetime.now()

    profile_name = (profile.get("name") or "Profile").strip() or "Profile"
    safe_profile_name = re.sub(r"[:\\/?*\[\]]", "-", profile_name)
    worksheet_name = f"{base_date.strftime('%Y-%m-%d')}-{safe_profile_name}"
    return worksheet_name[:100]


def get_google_service_account_storage_path(base_dir: Path) -> Path:
    return base_dir / "db" / GOOGLE_SERVICE_ACCOUNT_FILENAME


def _serialize_profile_row(row: sqlite3.Row | None) -> dict[str, Any]:
    if not row:
        raise ValueError("profile not found")
    settings = {}
    if row["settings_json"]:
        try:
            settings = json.loads(row["settings_json"])
        except json.JSONDecodeError:
            settings = {}
    settings = _normalize_profile_settings(settings)
    account_ids = []
    if row["account_ids"]:
        account_ids = [int(item) for item in str(row["account_ids"]).split(",") if item]
    return {
        "id": row["id"],
        "name": row["name"],
        "systemPrompt": row["system_prompt"] or "",
        "contactDetails": row["contact_details"] or "",
        "cta": row["cta"] or "",
        "settings": settings,
        "accountIds": account_ids,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _serialize_profile_export_item(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": (profile.get("name") or "").strip(),
        "systemPrompt": profile.get("systemPrompt") or "",
        "contactDetails": profile.get("contactDetails") or "",
        "cta": profile.get("cta") or "",
        "accountIds": _normalize_account_ids(profile.get("accountIds")),
        "settings": deepcopy(profile.get("settings") or {}),
    }


def _normalize_profile_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    normalized = deepcopy(settings or {})
    normalized["watermark"] = _normalize_watermark_settings(normalized.get("watermark"))
    normalized["contentAccounts"] = _normalize_content_accounts(normalized.get("contentAccounts"))
    return normalized


def _normalize_watermark_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    settings = deepcopy(settings or {})
    pattern = str(settings.get("pattern") or "single").strip() or "single"
    if pattern not in {"single", "repeat-slanted"}:
        pattern = "single"

    watermark_type = str(settings.get("type") or "text").strip() or "text"
    if watermark_type not in {"text", "image"}:
        watermark_type = "text"

    mode = str(settings.get("mode") or "static").strip() or "static"
    if mode not in {"static", "dynamic"}:
        mode = "static"

    position = str(settings.get("position") or "bottom-right").strip() or "bottom-right"
    if position not in {"top-left", "top-right", "bottom-left", "bottom-right", "center"}:
        position = "bottom-right"

    repeat_lines = _coerce_int(settings.get("repeatLines"), 3)
    repeat_lines = min(max(repeat_lines, 2), 5)

    font_size = _coerce_int(settings.get("fontSize"), 28)
    font_size = min(max(font_size, 12), 120)

    spacing = _coerce_int(settings.get("spacing"), 220)
    spacing = min(max(spacing, 40), 600)

    angle = _coerce_float(settings.get("angle"), -30.0)
    angle = min(max(angle, -85.0), 85.0)

    opacity = _coerce_float(settings.get("opacity"), 0.45)
    opacity = min(max(opacity, 0.1), 1.0)

    color = str(settings.get("color") or "#FFFFFF").strip().upper() or "#FFFFFF"
    if not re.fullmatch(r"#[0-9A-F]{6}", color):
        color = "#FFFFFF"

    return {
        "enabled": bool(settings.get("enabled")),
        "type": watermark_type,
        "mode": mode,
        "templateName": str(settings.get("templateName") or "").strip(),
        "pattern": pattern,
        "repeatLines": repeat_lines,
        "angle": angle,
        "spacing": spacing,
        "fontSize": font_size,
        "color": color,
        "text": str(settings.get("text") or "").strip(),
        "imagePath": str(settings.get("imagePath") or "").strip(),
        "position": position,
        "opacity": opacity,
    }


def _normalize_content_accounts(values: Any) -> list[dict[str, str]]:
    if not isinstance(values, list):
        return []

    normalized = []
    seen_ids = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        platform = str(item.get("platform") or "").strip().lower()
        if platform not in CONTENT_ACCOUNT_PLATFORMS:
            continue

        account_id = str(item.get("id") or "").strip() or uuid.uuid4().hex
        if account_id in seen_ids:
            continue
        seen_ids.add(account_id)

        normalized.append({
            "id": account_id,
            "platform": platform,
            "name": str(item.get("name") or "").strip(),
            "prompt": str(item.get("prompt") or "").strip(),
            "contactDetails": str(item.get("contactDetails") or "").strip(),
            "cta": str(item.get("cta") or "").strip(),
            "postPreset": str(item.get("postPreset") or "").strip(),
            "publisherTargetId": str(item.get("publisherTargetId") or "").strip(),
        })

    return normalized


def _normalize_account_ids(values: Any) -> list[int]:
    if not values:
        return []
    if isinstance(values, str):
        values = [item for item in values.split(",") if item]
    normalized = []
    for value in values:
        try:
            normalized.append(int(value))
        except (TypeError, ValueError):
            continue
    return sorted(set(normalized))


def _extract_profiles_import_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        profiles_payload = payload
    elif isinstance(payload, dict):
        profiles_payload = payload.get("profiles")
    else:
        raise ValueError("YAML root must be an object with a profiles list or a profiles list directly")

    if not isinstance(profiles_payload, list):
        raise ValueError("profiles must be a list")

    normalized = []
    for item in profiles_payload:
        if not isinstance(item, dict):
            raise ValueError("each profile entry must be an object")
        normalized.append(item)
    return normalized


def _parse_profiles_yaml_payload(yaml_text: str) -> list[dict[str, Any]]:
    yaml = _get_yaml_module()
    try:
        payload = yaml.safe_load((yaml_text or "").strip())
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML: {exc}") from exc
    return _extract_profiles_import_payload(payload)


def _normalize_import_profile_payload(payload: dict[str, Any]) -> dict[str, Any]:
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("profile name is required")

    normalized_payload = {
        "id": payload.get("id"),
        "name": name,
        "systemPrompt": str(payload.get("systemPrompt") or ""),
        "contactDetails": str(payload.get("contactDetails") or ""),
        "cta": str(payload.get("cta") or ""),
        "accountIds": _normalize_account_ids(payload.get("accountIds")),
        "settings": _normalize_profile_settings(payload.get("settings") or {}),
    }
    if not normalized_payload["id"]:
        normalized_payload.pop("id")
    return normalized_payload


def _diff_profile_export_fields(existing_profile: dict[str, Any], incoming_profile: dict[str, Any]) -> list[str]:
    changed_fields = []
    for key in ("systemPrompt", "contactDetails", "cta"):
        if (existing_profile.get(key) or "") != (incoming_profile.get(key) or ""):
            changed_fields.append(key)

    if _normalize_account_ids(existing_profile.get("accountIds")) != _normalize_account_ids(incoming_profile.get("accountIds")):
        changed_fields.append("accountIds")

    existing_settings = existing_profile.get("settings") or {}
    incoming_settings = incoming_profile.get("settings") or {}
    for key in sorted(set(existing_settings.keys()) | set(incoming_settings.keys())):
        if existing_settings.get(key) != incoming_settings.get(key):
            changed_fields.append(f"settings.{key}")

    return changed_fields


def _resolve_selected_account_ids(profile: dict[str, Any], values: Any) -> list[int]:
    selected_ids = _normalize_account_ids(values)
    profile_account_ids = set(_normalize_account_ids(profile.get("accountIds")))
    if not selected_ids:
        return sorted(profile_account_ids)
    return [account_id for account_id in selected_ids if account_id in profile_account_ids]


def _resolve_selected_content_account_ids(profile: dict[str, Any], values: Any) -> list[str]:
    selected_ids = _normalize_content_account_ids(values)
    profile_account_ids = {item.get("id", "") for item in _get_profile_content_accounts(profile)}
    if not selected_ids:
        return sorted(profile_account_ids)
    return [account_id for account_id in selected_ids if account_id in profile_account_ids]


def _normalize_content_account_ids(values: Any) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        values = [item for item in values.split(",") if item]
    normalized = []
    seen = set()
    for value in values:
        account_id = str(value or "").strip()
        if not account_id or account_id in seen:
            continue
        seen.add(account_id)
        normalized.append(account_id)
    return normalized


def _get_profile_content_accounts(profile: dict[str, Any]) -> list[dict[str, str]]:
    settings = profile.get("settings") or {}
    return _normalize_content_accounts(settings.get("contentAccounts"))


def _get_selected_content_accounts(profile: dict[str, Any]) -> list[dict[str, str]]:
    content_accounts = _get_profile_content_accounts(profile)
    selected_ids = set(_resolve_selected_content_account_ids(profile, profile.get("selectedContentAccountIds")))
    if not selected_ids:
        return content_accounts
    return [account for account in content_accounts if account.get("id") in selected_ids]


def aggregate_account_posts(content_account_results: list[dict[str, Any]]) -> dict[str, str]:
    aggregated = {platform: "" for platform in CONTENT_ACCOUNT_PLATFORMS}
    for result in content_account_results:
        account = result.get("account") or {}
        platform = (account.get("platform") or "").strip().lower()
        content = (result.get("content") or "").strip()
        if platform in aggregated and content and not aggregated[platform]:
            aggregated[platform] = content
    return aggregated


def _build_schedule_columns(schedule_at: str | None) -> list[str]:
    if not schedule_at:
        return ["", "", "", "", ""]
    try:
        dt = datetime.fromisoformat(schedule_at)
    except ValueError as exc:
        raise ValueError("scheduleAt must be a valid ISO datetime string") from exc
    return [str(dt.month), str(dt.day), str(dt.year), str(dt.hour), str(dt.minute)]


def _join_remote_path(*parts: str) -> str:
    cleaned = [part.strip("/") for part in parts if part and str(part).strip("/")]
    return posixpath.join(*cleaned) if cleaned else ""


def _build_public_url(storage: dict[str, Any], relative_path: str, remote_spec: str, filename: str) -> str:
    template = (storage.get("publicUrlTemplate") or "").strip()
    if template:
        remote_path = (storage.get("remotePath") or "").strip().strip("/")
        return template.format(
            remote_name=storage.get("remoteName", ""),
            remote_path=remote_path,
            relative_path=relative_path,
            filename=filename,
        )

    rclone_bin = shutil.which("rclone")
    if not rclone_bin:
        raise RuntimeError("rclone is required to resolve a public media URL")
    result = subprocess.run(
        [rclone_bin, "link", remote_spec],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "rclone link failed")
    return result.stdout.strip()


def get_profile_config_example_path(base_dir: Path) -> Path:
    return base_dir / "docs" / PROFILE_CONFIG_EXAMPLE_FILENAME


def get_profile_backup_config_storage_path(base_dir: Path) -> Path:
    return base_dir / "db" / PROFILE_BACKUP_CONFIG_FILENAME


def _get_yaml_module():
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required for profile YAML import/export") from exc
    return yaml


def _build_default_profile_backup_config(db_path: Path) -> dict[str, Any]:
    inferred_remote_name = ""
    inferred_remote_path = "profile-backups"
    for profile in list_profiles(db_path):
        storage = ((profile.get("settings") or {}).get("storage") or {})
        remote_name = (storage.get("remoteName") or "").strip()
        remote_path = (storage.get("remotePath") or "").strip().strip("/")
        if remote_name:
            inferred_remote_name = remote_name
            inferred_remote_path = _join_remote_path(remote_path, "backups", "profile-configs") or "profile-backups"
            break

    return {
        "enabled": True,
        "remoteName": inferred_remote_name,
        "remotePath": inferred_remote_path,
        "scheduleTime": DEFAULT_PROFILE_BACKUP_SCHEDULE_TIME,
        "keepCopies": DEFAULT_PROFILE_BACKUP_KEEP_COPIES,
        "lastBackupAt": "",
        "lastBackupRemoteSpec": "",
        "lastBackupStatus": "",
    }


def _normalize_profile_backup_config(payload: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    keep_copies = payload.get("keepCopies", fallback.get("keepCopies", DEFAULT_PROFILE_BACKUP_KEEP_COPIES))
    try:
        keep_copies = int(keep_copies)
    except (TypeError, ValueError):
        keep_copies = fallback.get("keepCopies", DEFAULT_PROFILE_BACKUP_KEEP_COPIES)
    keep_copies = max(1, keep_copies)

    schedule_time = str(payload.get("scheduleTime", fallback.get("scheduleTime", DEFAULT_PROFILE_BACKUP_SCHEDULE_TIME)) or "").strip()
    _parse_backup_schedule_time(schedule_time)

    return {
        "enabled": bool(payload.get("enabled", fallback.get("enabled", True))),
        "remoteName": str(payload.get("remoteName", fallback.get("remoteName", "")) or "").strip(),
        "remotePath": str(payload.get("remotePath", fallback.get("remotePath", "profile-backups")) or "").strip().strip("/"),
        "scheduleTime": schedule_time,
        "keepCopies": keep_copies,
        "lastBackupAt": str(payload.get("lastBackupAt", fallback.get("lastBackupAt", "")) or "").strip(),
        "lastBackupRemoteSpec": str(payload.get("lastBackupRemoteSpec", fallback.get("lastBackupRemoteSpec", "")) or "").strip(),
        "lastBackupStatus": str(payload.get("lastBackupStatus", fallback.get("lastBackupStatus", "")) or "").strip(),
    }


def _parse_backup_schedule_time(value: Any) -> tuple[int, int]:
    text = str(value or "").strip() or DEFAULT_PROFILE_BACKUP_SCHEDULE_TIME
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        raise ValueError("scheduleTime must be in HH:MM format")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("scheduleTime must be a valid 24-hour time")
    return hour, minute


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _prune_profile_backup_remote_files(rclone_bin: str, remote_name: str, remote_path: str, keep_copies: int) -> None:
    remote_dir_spec = f"{remote_name}:{remote_path}" if remote_path else f"{remote_name}:"
    result = subprocess.run(
        [rclone_bin, "lsf", remote_dir_spec, "--files-only"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "rclone backup listing failed")

    filenames = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().startswith(PROFILE_BACKUP_FILENAME_PREFIX) and line.strip().endswith(".tar.gz")
    ]
    if len(filenames) <= keep_copies:
        return

    for filename in sorted(filenames, reverse=True)[keep_copies:]:
        remote_spec = f"{remote_name}:{_join_remote_path(remote_path, filename)}"
        delete_result = subprocess.run(
            [rclone_bin, "deletefile", remote_spec],
            check=False,
            capture_output=True,
            text=True,
        )
        if delete_result.returncode != 0:
            raise RuntimeError(delete_result.stderr.strip() or delete_result.stdout.strip() or "rclone backup cleanup failed")


def _build_profile_backup_bundle(base_dir: Path, db_path: Path, archive_path: Path) -> list[Path]:
    export_path = base_dir / "db" / "profiles.export.yaml"
    export_path.write_text(export_profiles_yaml(db_path), encoding="utf-8")

    files_to_include = [export_path]
    for candidate in [
        base_dir / "db" / "database.db",
        base_dir / "db" / "direct_publishers.json",
        base_dir / "db" / "google_service_account.json",
        base_dir / "db" / PROFILE_BACKUP_CONFIG_FILENAME,
    ]:
        if candidate.exists():
            files_to_include.append(candidate)

    manifest_path = base_dir / "db" / "backup-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "createdAt": datetime.now().isoformat(timespec="seconds"),
                "formatVersion": 2,
                "files": [str(path.relative_to(base_dir)) for path in files_to_include],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    files_to_include.append(manifest_path)

    with tarfile.open(archive_path, "w:gz") as archive:
        for path in files_to_include:
            archive.add(path, arcname=str(path.relative_to(base_dir)))
    return [export_path, manifest_path]


def _load_google_service_account_data(base_dir: Path | None = None) -> dict[str, Any] | None:
    raw_json = _resolve_value(None, "SAU_GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw_json:
        try:
            return json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON") from exc

    file_path = _resolve_value(None, "SAU_GOOGLE_SERVICE_ACCOUNT_FILE", "GOOGLE_SERVICE_ACCOUNT_FILE")
    if file_path:
        path = Path(file_path)
        if not path.exists():
            raise RuntimeError(f"Google service account file not found: {path}")
        return json.loads(_read_text_file(path))

    candidate_paths = []
    if base_dir is not None:
        candidate_paths.append(get_google_service_account_storage_path(base_dir))
    candidate_paths.extend([
        Path("db") / GOOGLE_SERVICE_ACCOUNT_FILENAME,
        Path(GOOGLE_SERVICE_ACCOUNT_FILENAME),
    ])
    for path in candidate_paths:
        if path.exists():
            return json.loads(_read_text_file(path))
    return None


def _parse_google_service_account_json(raw_value: str, source_name: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        return None, f"{source_name} is not valid JSON: {exc.msg}"

    if not isinstance(data, dict):
        return None, f"{source_name} must contain a JSON object"

    return data, None


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Failed to read Google service account file: {path}: {exc}") from exc


def _read_text_file_for_config(path: Path) -> tuple[str | None, str | None]:
    try:
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"Failed to read Google service account file: {path}: {exc}"


def _validate_google_service_account_payload(data: dict[str, Any]) -> None:
    required_fields = [
        "type",
        "project_id",
        "private_key_id",
        "private_key",
        "client_email",
        "client_id",
        "token_uri",
    ]
    missing = [field for field in required_fields if not str(data.get(field) or "").strip()]
    if missing:
        raise ValueError(f"missing required Google service account fields: {', '.join(missing)}")
    if str(data.get("type", "")).strip() != "service_account":
        raise ValueError("Google credential type must be service_account")


def _resolve_value(explicit_value: Any, *env_names: str) -> str:
    if explicit_value:
        return str(explicit_value).strip()
    env_values = _load_env_values()
    for env_name in env_names:
        if os.getenv(env_name):
            return os.getenv(env_name, "").strip()
        if env_values.get(env_name):
            return env_values[env_name].strip()
    return ""


def _load_env_values() -> dict[str, str]:
    env_path = Path(".env")
    if not env_path.exists():
        return {}
    values = {}
    try:
        content = env_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _get_requests_module():
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("requests is required for LLM API calls") from exc
    return requests


def _apply_image_watermark(source_path: Path, output_path: Path, watermark: dict[str, Any]) -> Path:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("Pillow is required for image watermarking") from exc

    base = Image.open(source_path).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (255, 255, 255, 0))
    watermark = _normalize_watermark_settings(watermark)
    opacity = float(watermark.get("opacity", 0.45) or 0.45)
    position = _resolve_image_watermark_position(watermark)
    watermark_type = (watermark.get("type") or "text").strip()
    pattern = (watermark.get("pattern") or "single").strip()

    if watermark_type == "image" and watermark.get("imagePath"):
        mark_path = Path(str(watermark["imagePath"]))
        if not mark_path.exists():
            raise RuntimeError(f"watermark image not found: {mark_path}")
        mark = Image.open(mark_path).convert("RGBA")
        if mark.width > base.width // 3:
            ratio = (base.width // 3) / mark.width
            mark = mark.resize((int(mark.width * ratio), int(mark.height * ratio)))
        alpha = mark.getchannel("A").point(lambda value: int(value * opacity))
        mark.putalpha(alpha)
        x, y = _resolve_overlay_position(base.size, mark.size, position)
        overlay.alpha_composite(mark, (x, y))
    else:
        text = watermark.get("text") or profile_default_watermark_text(source_path)
        if pattern == "repeat-slanted":
            overlay = _build_repeated_text_overlay(base.size, text, watermark)
        else:
            draw = ImageDraw.Draw(overlay)
            font = _load_watermark_font(ImageFont, int(watermark.get("fontSize") or 28))
            bbox = draw.textbbox((0, 0), text, font=font)
            text_size = (bbox[2] - bbox[0], bbox[3] - bbox[1])
            x, y = _resolve_overlay_position(base.size, text_size, position)
            fill = _color_to_rgba(str(watermark.get("color") or "#FFFFFF"), opacity)
            draw.text((x, y), text, fill=fill, font=font)

    output = Image.alpha_composite(base, overlay).convert("RGB")
    output.save(output_path)
    return output_path


def _apply_video_watermark(source_path: Path, output_path: Path, watermark: dict[str, Any]) -> Path:
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError("ffmpeg is required for video watermarking")

    watermark = _normalize_watermark_settings(watermark)
    position = (watermark.get("position") or "bottom-right").strip()
    mode = (watermark.get("mode") or "static").strip()
    opacity = float(watermark.get("opacity", 0.45) or 0.45)
    watermark_type = (watermark.get("type") or "text").strip()
    pattern = (watermark.get("pattern") or "single").strip()
    duration = _get_video_duration_seconds(source_path) if mode == "dynamic" else None

    if watermark_type == "text" and pattern == "repeat-slanted":
        return _apply_video_text_template_watermark(source_path, output_path, watermark, duration)

    if watermark_type == "image" and watermark.get("imagePath"):
        image_path = Path(str(watermark["imagePath"]))
        if not image_path.exists():
            raise RuntimeError(f"watermark image not found: {image_path}")
        if mode == "dynamic":
            filter_complex, output_label = _build_dynamic_image_overlay_filter(duration, opacity)
        else:
            overlay_expr = _build_video_overlay_expression(position, mode)
            filter_complex = f"[1:v]format=rgba,colorchannelmixer=aa={opacity}[wm];[0:v][wm]{overlay_expr}[vout]"
            output_label = "vout"
        command = [
            ffmpeg_bin,
            "-y",
            "-i",
            str(source_path),
            "-i",
            str(image_path),
            "-filter_complex",
            filter_complex,
            "-map",
            f"[{output_label}]",
            "-map",
            "0:a?",
            "-c:a",
            "copy",
            str(output_path),
        ]
    else:
        text = _escape_ffmpeg_text(watermark.get("text") or profile_default_watermark_text(source_path))
        if mode == "dynamic":
            filter_complex = _build_dynamic_drawtext_filter(text, opacity, duration)
        else:
            x_expr, y_expr = _build_drawtext_coordinates(position, mode)
            filter_complex = (
                "drawtext="
                f"text='{text}':"
                "fontcolor=white:"
                f"alpha={opacity}:"
                "fontsize=28:"
                f"x={x_expr}:"
                f"y={y_expr}"
            )
        command = [
            ffmpeg_bin,
            "-y",
            "-i",
            str(source_path),
            "-vf",
            filter_complex,
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:a",
            "copy",
            str(output_path),
        ]

    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "ffmpeg watermark failed")
    return output_path


def profile_default_watermark_text(source_path: Path) -> str:
    return source_path.stem.replace("_", " ")


def _resolve_overlay_position(base_size: tuple[int, int], mark_size: tuple[int, int], position: str) -> tuple[int, int]:
    base_width, base_height = base_size
    mark_width, mark_height = mark_size
    margin = 24
    positions = {
        "top-left": (margin, margin),
        "top-right": (max(margin, base_width - mark_width - margin), margin),
        "bottom-left": (margin, max(margin, base_height - mark_height - margin)),
        "bottom-right": (
            max(margin, base_width - mark_width - margin),
            max(margin, base_height - mark_height - margin),
        ),
        "center": ((base_width - mark_width) // 2, (base_height - mark_height) // 2),
    }
    return positions.get(position, positions["bottom-right"])


def _build_repeated_text_overlay(base_size: tuple[int, int], text: str, watermark: dict[str, Any], seed: int | None = None):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("Pillow is required for repeated watermark rendering") from exc

    width, height = base_size
    diagonal = int(math.sqrt((width * width) + (height * height)))
    canvas_size = diagonal * 2
    canvas = Image.new("RGBA", (canvas_size, canvas_size), (255, 255, 255, 0))
    draw = ImageDraw.Draw(canvas)
    font = _load_watermark_font(ImageFont, int(watermark.get("fontSize") or 28))
    fill = _color_to_rgba(str(watermark.get("color") or "#FFFFFF"), float(watermark.get("opacity") or 0.45))
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = max(1, bbox[2] - bbox[0])
    text_height = max(1, bbox[3] - bbox[1])
    repeat_lines = int(watermark.get("repeatLines") or 3)
    spacing = int(watermark.get("spacing") or 220)
    line_gap = max(12, text_height // 2)
    row_step = max(text_height * repeat_lines + line_gap * max(repeat_lines - 1, 0) + spacing, text_height + spacing)
    column_step = max(text_width + spacing, text_width + 40)
    rng = random.Random(seed)
    offset_x = rng.randint(0, max(column_step - 1, 1)) if watermark.get("mode") == "dynamic" else column_step // 3
    offset_y = rng.randint(0, max(row_step - 1, 1)) if watermark.get("mode") == "dynamic" else row_step // 3

    for base_y in range(-row_step, canvas_size + row_step, row_step):
        for base_x in range(-column_step, canvas_size + column_step, column_step):
            for line_index in range(repeat_lines):
                stagger = (text_width // 4) if line_index % 2 else 0
                draw.text(
                    (base_x + offset_x + stagger, base_y + offset_y + line_index * (text_height + line_gap)),
                    text,
                    fill=fill,
                    font=font,
                )

    rotated = canvas.rotate(float(watermark.get("angle") or -30.0), resample=Image.BICUBIC, expand=True)
    left = max((rotated.width - width) // 2, 0)
    top = max((rotated.height - height) // 2, 0)
    return rotated.crop((left, top, left + width, top + height))


def _load_watermark_font(image_font_module, font_size: int):
    candidate_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for candidate in candidate_paths:
        try:
            return image_font_module.truetype(candidate, font_size)
        except (OSError, AttributeError):
            continue
    return image_font_module.load_default()


def _color_to_rgba(color: str, opacity: float) -> tuple[int, int, int, int]:
    normalized = str(color or "#FFFFFF").strip().lstrip("#")
    if len(normalized) != 6:
        normalized = "FFFFFF"
    try:
        red = int(normalized[0:2], 16)
        green = int(normalized[2:4], 16)
        blue = int(normalized[4:6], 16)
    except ValueError:
        red, green, blue = 255, 255, 255
    return red, green, blue, int(255 * max(0.0, min(opacity, 1.0)))


def _apply_video_text_template_watermark(
    source_path: Path,
    output_path: Path,
    watermark: dict[str, Any],
    duration: float | None,
) -> Path:
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError("ffmpeg is required for video watermarking")

    overlay_paths = []
    command = [ffmpeg_bin, "-y", "-i", str(source_path)]
    try:
        frame_size = _get_video_frame_size(source_path)
        overlay_count = 3 if watermark.get("mode") == "dynamic" else 1
        for index in range(overlay_count):
            overlay_seed = random.randint(0, 1_000_000) if watermark.get("mode") == "dynamic" else index
            overlay_image = _build_repeated_text_overlay(
                frame_size,
                watermark.get("text") or profile_default_watermark_text(source_path),
                watermark,
                overlay_seed,
            )
            overlay_path = output_path.parent / f"{output_path.stem}_wm_{index}.png"
            overlay_image.save(overlay_path)
            overlay_paths.append(overlay_path)
            command.extend(["-loop", "1", "-i", str(overlay_path)])

        filter_parts = []
        previous_label = "0:v"
        output_label = "vout"
        if watermark.get("mode") == "dynamic":
            segments = _build_dynamic_segments(duration or 1.0)
            for index, (start_at, end_at, _) in enumerate(segments):
                input_label = index % len(overlay_paths) + 1
                output_label = f"v{index}"
                filter_parts.append(
                    f"[{previous_label}][{input_label}:v]overlay=x=0:y=0:enable='between(t,{start_at:.3f},{end_at:.3f})'[{output_label}]"
                )
                previous_label = output_label
        else:
            filter_parts.append(f"[0:v][1:v]overlay=x=0:y=0[vout]")
            output_label = "vout"

        command.extend([
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            f"[{output_label}]",
            "-map",
            "0:a?",
            "-c:a",
            "copy",
            str(output_path),
        ])
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "ffmpeg watermark failed")
        return output_path
    finally:
        for overlay_path in overlay_paths:
            try:
                overlay_path.unlink()
            except OSError:
                continue


def _build_video_overlay_expression(position: str, mode: str) -> str:
    if mode == "dynamic":
        return "overlay=x='mod(t*120, max(main_w-overlay_w,1))':y='main_h-overlay_h-40'"

    x_map = {
        "top-left": "40",
        "top-right": "main_w-overlay_w-40",
        "bottom-left": "40",
        "bottom-right": "main_w-overlay_w-40",
        "center": "(main_w-overlay_w)/2",
    }
    y_map = {
        "top-left": "40",
        "top-right": "40",
        "bottom-left": "main_h-overlay_h-40",
        "bottom-right": "main_h-overlay_h-40",
        "center": "(main_h-overlay_h)/2",
    }
    return f"overlay=x='{x_map.get(position, x_map['bottom-right'])}':y='{y_map.get(position, y_map['bottom-right'])}'"


def _build_drawtext_coordinates(position: str, mode: str) -> tuple[str, str]:
    if mode == "dynamic":
        return "mod(t*120, max(w-text_w,1))", "h-text_h-40"

    x_map = {
        "top-left": "40",
        "top-right": "w-text_w-40",
        "bottom-left": "40",
        "bottom-right": "w-text_w-40",
        "center": "(w-text_w)/2",
    }
    y_map = {
        "top-left": "40",
        "top-right": "40",
        "bottom-left": "h-text_h-40",
        "bottom-right": "h-text_h-40",
        "center": "(h-text_h)/2",
    }
    return x_map.get(position, x_map["bottom-right"]), y_map.get(position, y_map["bottom-right"])


def _resolve_image_watermark_position(watermark: dict[str, Any]) -> str:
    position = (watermark.get("position") or "bottom-right").strip()
    if (watermark.get("mode") or "static").strip() == "dynamic":
        return random.choice(["top-left", "top-right", "bottom-left", "bottom-right", "center"])
    return position


def _get_video_frame_size(source_path: Path) -> tuple[int, int]:
    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin:
        raise RuntimeError("ffprobe is required for video watermarking")

    result = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(source_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "ffprobe failed")
    width_text, _, height_text = result.stdout.strip().partition("x")
    try:
        return max(int(width_text), 1), max(int(height_text), 1)
    except ValueError as exc:
        raise RuntimeError("unable to read video size") from exc


def _get_video_duration_seconds(source_path: Path) -> float:
    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin:
        raise RuntimeError("ffprobe is required for dynamic video watermarking")

    result = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(source_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "ffprobe failed")
    try:
        return max(float(result.stdout.strip()), 1.0)
    except ValueError as exc:
        raise RuntimeError("unable to read video duration") from exc


def _build_dynamic_segments(duration: float) -> list[tuple[float, float, str]]:
    segments = []
    current = 0.0
    positions = ["top-left", "top-right", "bottom-left", "bottom-right", "center"]
    while current < duration:
        span = float(random.randint(1, 5))
        end_at = min(duration, current + span)
        segments.append((current, end_at, random.choice(positions)))
        current = end_at
    return segments or [(0.0, duration, "bottom-right")]


def _build_dynamic_drawtext_filter(text: str, opacity: float, duration: float | None) -> str:
    filters = []
    for start_at, end_at, position in _build_dynamic_segments(duration or 1.0):
        x_expr, y_expr = _build_drawtext_coordinates(position, "static")
        filters.append(
            "drawtext="
            f"text='{text}':"
            "fontcolor=white:"
            f"alpha={opacity}:"
            "fontsize=28:"
            f"x={x_expr}:"
            f"y={y_expr}:"
            f"enable='between(t,{start_at:.3f},{end_at:.3f})'"
        )
    return ",".join(filters)


def _build_dynamic_image_overlay_filter(duration: float | None, opacity: float) -> tuple[str, str]:
    filter_parts = [f"[1:v]format=rgba,colorchannelmixer=aa={opacity}[wm]"]
    previous_label = "0:v"
    output_label = "v0"
    for index, (start_at, end_at, position) in enumerate(_build_dynamic_segments(duration or 1.0), start=1):
        x_expr, y_expr = _get_overlay_coordinates(position)
        output_label = f"v{index}"
        filter_parts.append(
            f"[{previous_label}][wm]overlay=x='{x_expr}':y='{y_expr}':enable='between(t,{start_at:.3f},{end_at:.3f})'[{output_label}]"
        )
        previous_label = output_label
    return ";".join(filter_parts), output_label


def _get_overlay_coordinates(position: str) -> tuple[str, str]:
    x_map = {
        "top-left": "40",
        "top-right": "main_w-overlay_w-40",
        "bottom-left": "40",
        "bottom-right": "main_w-overlay_w-40",
        "center": "(main_w-overlay_w)/2",
    }
    y_map = {
        "top-left": "40",
        "top-right": "40",
        "bottom-left": "main_h-overlay_h-40",
        "bottom-right": "main_h-overlay_h-40",
        "center": "(main_h-overlay_h)/2",
    }
    return x_map.get(position, x_map["bottom-right"]), y_map.get(position, y_map["bottom-right"])


def _escape_ffmpeg_text(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
