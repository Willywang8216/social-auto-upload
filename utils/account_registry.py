import json
import sqlite3
from pathlib import Path

MASKED_SECRET_VALUE = "********"

ACCOUNT_SECRET_FIELDS = {
    "twitter": {"apiKey", "apiKeySecret", "accessToken", "accessTokenSecret"},
    "threads": {"accessToken"},
    "facebook": {"accessToken"},
    "reddit": {"clientId", "clientSecret", "refreshToken"},
    "tiktok": {"accessToken"},
    "youtube": {"accessToken"},
}

LEGACY_PLATFORM_MAP = {
    1: {"key": "xiaohongshu", "label": "小紅書", "supportsQrLogin": True, "supportsCookieUpload": True, "supportsValidation": True},
    2: {"key": "channels", "label": "影片號", "supportsQrLogin": True, "supportsCookieUpload": True, "supportsValidation": True},
    3: {"key": "douyin", "label": "抖音", "supportsQrLogin": True, "supportsCookieUpload": True, "supportsValidation": True},
    4: {"key": "kuaishou", "label": "快手", "supportsQrLogin": True, "supportsCookieUpload": True, "supportsValidation": True},
}

INTERNATIONAL_PLATFORM_MAP = {
    "twitter": {"key": "twitter", "label": "X / Twitter", "supportsQrLogin": False, "supportsCookieUpload": False, "supportsValidation": True},
    "threads": {"key": "threads", "label": "Threads", "supportsQrLogin": False, "supportsCookieUpload": False, "supportsValidation": True},
    "facebook": {"key": "facebook", "label": "Facebook", "supportsQrLogin": False, "supportsCookieUpload": False, "supportsValidation": True},
    "reddit": {"key": "reddit", "label": "Reddit", "supportsQrLogin": False, "supportsCookieUpload": False, "supportsValidation": True},
    "tiktok": {"key": "tiktok", "label": "TikTok", "supportsQrLogin": False, "supportsCookieUpload": False, "supportsValidation": True},
    "youtube": {"key": "youtube", "label": "YouTube", "supportsQrLogin": False, "supportsCookieUpload": False, "supportsValidation": True},
}

PLATFORM_LABEL_TO_KEY = {
    value["label"]: value["key"]
    for value in [*LEGACY_PLATFORM_MAP.values(), *INTERNATIONAL_PLATFORM_MAP.values()]
}

PLATFORM_KEY_TO_LABEL = {
    value["key"]: value["label"]
    for value in [*LEGACY_PLATFORM_MAP.values(), *INTERNATIONAL_PLATFORM_MAP.values()]
}

PLATFORM_KEY_TO_TYPE = {value["key"]: key for key, value in LEGACY_PLATFORM_MAP.items()}


def ensure_account_tables(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS user_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type INTEGER NOT NULL DEFAULT 0,
                filePath TEXT NOT NULL DEFAULT '',
                userName TEXT NOT NULL,
                status INTEGER DEFAULT 0,
                platform_key TEXT,
                auth_mode TEXT DEFAULT 'qr_cookie',
                metadata_json TEXT DEFAULT '{}'
            )
            """
        )

        _ensure_column(cursor, "user_info", "platform_key", "TEXT")
        _ensure_column(cursor, "user_info", "auth_mode", "TEXT DEFAULT 'qr_cookie'")
        _ensure_column(cursor, "user_info", "metadata_json", "TEXT DEFAULT '{}'")

        for legacy_type, config in LEGACY_PLATFORM_MAP.items():
            cursor.execute(
                """
                UPDATE user_info
                SET platform_key = COALESCE(NULLIF(TRIM(platform_key), ''), ?),
                    auth_mode = COALESCE(NULLIF(TRIM(auth_mode), ''), 'qr_cookie'),
                    metadata_json = COALESCE(NULLIF(TRIM(metadata_json), ''), '{}')
                WHERE type = ?
                """,
                (config["key"], legacy_type),
            )

        cursor.execute(
            """
            UPDATE user_info
            SET auth_mode = COALESCE(NULLIF(TRIM(auth_mode), ''), 'manual'),
                metadata_json = COALESCE(NULLIF(TRIM(metadata_json), ''), '{}')
            WHERE type = 0
            """
        )
        conn.commit()


def serialize_account_row(
    row: sqlite3.Row | dict,
    pending_validation: bool = False,
    include_sensitive: bool = True,
) -> dict:
    account_type = _coerce_int(row["type"], 0)
    platform_key = normalize_platform_key(row["platform_key"] if "platform_key" in row.keys() else None) or platform_key_from_type(account_type)
    platform_config = get_platform_config(platform_key, account_type)
    status_code = _coerce_int(row["status"], 0)
    if pending_validation and platform_config["supportsValidation"]:
        status_code = -1

    return {
        "id": row["id"],
        "type": account_type,
        "legacyType": account_type,
        "platformKey": platform_key,
        "platform": platform_config["label"],
        "filePath": row["filePath"],
        "name": row["userName"],
        "statusCode": status_code,
        "status": status_label_from_code(status_code),
        "authMode": (row["auth_mode"] if "auth_mode" in row.keys() else "") or default_auth_mode_for_platform(platform_key, account_type),
        "metadata": sanitize_account_metadata(
            parse_metadata(row["metadata_json"] if "metadata_json" in row.keys() else "{}"),
            platform_key,
            include_sensitive=include_sensitive,
        ),
        "supportsQrLogin": platform_config["supportsQrLogin"],
        "supportsCookieUpload": platform_config["supportsCookieUpload"],
        "supportsValidation": platform_config["supportsValidation"],
        "isInternational": account_type == 0,
    }


def parse_metadata(raw_value) -> dict:
    if isinstance(raw_value, dict):
        return raw_value
    try:
        data = json.loads(raw_value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def sanitize_account_metadata(metadata: dict, platform_key: str, include_sensitive: bool = True) -> dict:
    metadata = parse_metadata(metadata)
    if include_sensitive:
        return metadata
    sanitized = dict(metadata)
    for field in ACCOUNT_SECRET_FIELDS.get(normalize_platform_key(platform_key), set()):
        if sanitized.get(field):
            sanitized[field] = MASKED_SECRET_VALUE
    return sanitized


def merge_sensitive_account_metadata(platform_key: str, incoming: dict | None, existing: dict | None) -> dict:
    normalized_key = normalize_platform_key(platform_key)
    merged = parse_metadata(incoming)
    current = parse_metadata(existing)
    for field in ACCOUNT_SECRET_FIELDS.get(normalized_key, set()):
        incoming_value = merged.get(field)
        if incoming_value in (None, "", MASKED_SECRET_VALUE):
            existing_value = current.get(field)
            if existing_value not in (None, ""):
                merged[field] = existing_value
    return merged


def normalize_platform_key(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in PLATFORM_KEY_TO_LABEL:
        return lowered
    return PLATFORM_LABEL_TO_KEY.get(text, "")


def platform_key_from_type(account_type: int) -> str:
    config = LEGACY_PLATFORM_MAP.get(_coerce_int(account_type, 0))
    return config["key"] if config else ""


def platform_type_from_key(platform_key: str) -> int:
    return PLATFORM_KEY_TO_TYPE.get(normalize_platform_key(platform_key), 0)


def get_platform_config(platform_key: str, account_type: int | None = None) -> dict:
    normalized_key = normalize_platform_key(platform_key)
    if normalized_key in INTERNATIONAL_PLATFORM_MAP:
        return INTERNATIONAL_PLATFORM_MAP[normalized_key]
    if normalized_key in PLATFORM_KEY_TO_TYPE:
        return LEGACY_PLATFORM_MAP[PLATFORM_KEY_TO_TYPE[normalized_key]]
    legacy_config = LEGACY_PLATFORM_MAP.get(_coerce_int(account_type, 0))
    if legacy_config:
        return legacy_config
    return {
        "key": normalized_key or "unknown",
        "label": PLATFORM_KEY_TO_LABEL.get(normalized_key, "未知平台"),
        "supportsQrLogin": False,
        "supportsCookieUpload": False,
        "supportsValidation": False,
    }


def default_auth_mode_for_platform(platform_key: str, account_type: int | None = None) -> str:
    platform_config = get_platform_config(platform_key, account_type)
    return "qr_cookie" if platform_config["supportsQrLogin"] else "manual"


def status_label_from_code(status_code: int) -> str:
    if status_code == -1:
        return "驗證中"
    if status_code == 1:
        return "正常"
    return "異常"


def _ensure_column(cursor: sqlite3.Cursor, table_name: str, column_name: str, definition: str) -> None:
    cursor.execute(f"PRAGMA table_info({table_name})")
    existing_columns = {row[1] for row in cursor.fetchall()}
    if column_name not in existing_columns:
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def _coerce_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
