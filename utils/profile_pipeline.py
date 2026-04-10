import json
import mimetypes
import os
import posixpath
import random
import re
import shutil
import sqlite3
import subprocess
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
NON_SHEET_PLATFORMS = ["telegram", "patreon"]
CONTENT_ACCOUNT_PLATFORMS = EXPORT_PLATFORMS + NON_SHEET_PLATFORMS + ["reddit"]
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
GOOGLE_SERVICE_ACCOUNT_FILENAME = "google_service_account.json"


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
    else:
        generated_posts = generate_posts(runtime_profile, transcript, material, upload_result["publicUrl"])
        normalized_posts = normalize_posts(generated_posts)
        rows = build_google_sheet_rows(runtime_profile, normalized_posts, upload_result, payload.get("scheduleAt"))

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


def append_rows_to_google_sheet(profile: dict[str, Any], rows: list[list[str]], schedule_at: str | None = None) -> dict[str, Any]:
    if not rows:
        return {"appended": 0, "worksheet": None}

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

    worksheet.append_rows(rows, value_input_option="USER_ENTERED")
    return {
        "appended": len(rows),
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
        data, error = _parse_google_service_account_json(path.read_text(encoding="utf-8"), str(path))
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
        data, error = _parse_google_service_account_json(stored_path.read_text(encoding="utf-8"), str(stored_path))
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
        "Return valid JSON only with these keys: twitter, threads, instagram, facebook, youtube, tiktok, telegram, patreon. "
        "Do not wrap the JSON in markdown. "
        "Requirements: twitter and threads must include emoji and exactly 3 hashtags, plus natural contact details and a CTA. "
        "instagram and facebook should be long-form posts with contact details and a CTA woven naturally into the copy. "
        "youtube should be a post with a strong hook plus a useful description-style body. "
        "tiktok should be short, direct, and include a description-ready CTA. "
        "telegram should be concise and readable. "
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


def _normalize_profile_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    normalized = deepcopy(settings or {})
    normalized["contentAccounts"] = _normalize_content_accounts(normalized.get("contentAccounts"))
    return normalized


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
        return json.loads(path.read_text(encoding="utf-8"))

    candidate_paths = []
    if base_dir is not None:
        candidate_paths.append(get_google_service_account_storage_path(base_dir))
    candidate_paths.extend([
        Path("db") / GOOGLE_SERVICE_ACCOUNT_FILENAME,
        Path(GOOGLE_SERVICE_ACCOUNT_FILENAME),
    ])
    for path in candidate_paths:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    return None


def _parse_google_service_account_json(raw_value: str, source_name: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        return None, f"{source_name} is not valid JSON: {exc.msg}"

    if not isinstance(data, dict):
        return None, f"{source_name} must contain a JSON object"

    return data, None


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
    for line in env_path.read_text(encoding="utf-8").splitlines():
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
    opacity = float(watermark.get("opacity", 0.45) or 0.45)
    position = _resolve_image_watermark_position(watermark)
    watermark_type = (watermark.get("type") or "text").strip()

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
        draw = ImageDraw.Draw(overlay)
        font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), text, font=font)
        text_size = (bbox[2] - bbox[0], bbox[3] - bbox[1])
        x, y = _resolve_overlay_position(base.size, text_size, position)
        draw.text((x, y), text, fill=(255, 255, 255, int(255 * opacity)), font=font)

    output = Image.alpha_composite(base, overlay).convert("RGB")
    output.save(output_path)
    return output_path


def _apply_video_watermark(source_path: Path, output_path: Path, watermark: dict[str, Any]) -> Path:
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError("ffmpeg is required for video watermarking")

    position = (watermark.get("position") or "bottom-right").strip()
    mode = (watermark.get("mode") or "static").strip()
    opacity = float(watermark.get("opacity", 0.45) or 0.45)
    watermark_type = (watermark.get("type") or "text").strip()
    duration = _get_video_duration_seconds(source_path) if mode == "dynamic" else None

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
