import asyncio
import hashlib
import json
import hmac
import logging
import os
import sqlite3
import threading
import time
import uuid
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Queue

# Install browserless patch BEFORE any playwright/patchright imports
from myUtils.browser_helper import install_browserless_patch
install_browserless_patch()

from flask_cors import CORS
from myUtils.auth import check_cookie
from myUtils import account_validation
from flask import Flask, request, jsonify, Response, render_template, send_from_directory, current_app, url_for, redirect, g
from utils.conf_defaults import BASE_DIR
from myUtils import campaigns as campaign_store
from myUtils import content_rules
from myUtils import google_sheets
from myUtils import jobs as job_runtime
from myUtils import llm_client
from myUtils import media_groups as media_group_store
from myUtils import media_pipeline
from myUtils import platform_capabilities
from myUtils import profiles as profile_registry
from myUtils import prepared_publishers
from myUtils import publish_orchestrator
from myUtils import publish_templates as template_store
from myUtils import account_events
from myUtils import rclone_storage
from myUtils import media_remote_storage
from myUtils import analytics_store
from myUtils import analytics_sync
from myUtils import analytics_advisor
from myUtils import meta_auth
from myUtils import meta_review
from myUtils import reddit_auth
from myUtils import reddit_review
from myUtils import youtube_auth
from myUtils import youtube_review
from myUtils import threads_auth
from myUtils import threads_review
from myUtils import x_auth
from myUtils import x_review
from myUtils import tiktok_auth
from myUtils import tiktok_review
from myUtils import do_spaces
from myUtils import patreon_auth
from myUtils import watermark_service
from myUtils import media_asset_service
from myUtils import content_generator
from myUtils import sheet_export_service
from myUtils.login import get_tencent_cookie, douyin_cookie_gen, get_ks_cookie, xiaohongshu_cookie_gen
from myUtils.login import reddit_cookie_gen, twitter_cookie_gen, twitter_cookie_gen_legacy
from uploader.patreon_uploader.main import patreon_cookie_gen
from myUtils.postVideo import (
    post_video_DouYin,
    post_video_ks,
    post_video_tencent,
    post_video_twitter,
    post_video_xhs,
)
from myUtils.security import (
    extract_bearer_token,
    load_policy,
)
from myUtils.worker import default_executor, run_worker_drain
from utils.files_times import generate_schedule_time_next_day

_worker_drain_lock = threading.Lock()

# Platforms that publish by handing the platform a public media URL (the
# platform fetches the bytes itself), so they require a publicly-reachable
# HTTPS URL. Other platforms (cookie/browser uploaders, byte-upload APIs)
# use the local file directly and do not need remote hosting.
_REMOTE_URL_PLATFORMS = {
    profile_registry.PLATFORM_TIKTOK,
    profile_registry.PLATFORM_FACEBOOK,
    profile_registry.PLATFORM_INSTAGRAM,
    profile_registry.PLATFORM_THREADS,
}

# Map the legacy numeric `type` field to the platform slug used by the job runtime.
LEGACY_PLATFORM_CODES = {
    1: "xiaohongshu",
    2: "tencent",
    3: "douyin",
    4: "kuaishou",
    7: "twitter",
}
TWITTER_PLATFORM_ALIASES = {"twitter", "x"}
COOKIE_ACQUISITION_GENERATORS = {
    "reddit": reddit_cookie_gen,
    "twitter": twitter_cookie_gen,
    "patreon": patreon_cookie_gen,
}

active_queues = {}
_ACCOUNT_MAINTENANCE_STATE = {
    'enabled': False,
    'intervalSeconds': 0,
    'running': False,
    'lastStartedAt': None,
    'lastFinishedAt': None,
    'lastResult': None,
    'lastError': None,
}
_ACCOUNT_MAINTENANCE_THREAD = None
app = Flask(__name__)

# Security policy is loaded once from the environment at import time. Tests
# can rebind ``app.config['SECURITY_POLICY']`` to override the live policy
# without touching the env.
_security_policy = load_policy()
app.config['SECURITY_POLICY'] = _security_policy

if _security_policy.open_mode:
    logging.getLogger(__name__).warning(
        "[security] running in open mode — set SAU_API_TOKENS to require a bearer token"
    )

# Restricted CORS: explicit origin allow-list, plus the headers the frontend
# actually uses. Credentials stay disabled because we authenticate via the
# Authorization header rather than cookies.
CORS(
    app,
    resources={r"/*": {"origins": list(_security_policy.cors_origins)}},
    allow_headers=["Authorization", "Content-Type"],
    supports_credentials=False,
)

# Phase 1 factory extensions: request/correlation IDs, structured request
# logging, a standard JSON error schema, config validation, and the health
# blueprint (/healthz, /readyz). Registered here — before the auth gate below —
# so the request-id middleware runs first. Idempotent, so sau_app.create_app()
# (the Gunicorn entrypoint) can call it again safely. The database readiness
# probe is registered near the end of this module, once _get_legacy_db_path is
# defined.
from sau_app import init_extensions, register_readiness_check  # noqa: E402
init_extensions(app)

LEGACY_DB_REQUIRED_TABLES = frozenset({"user_info", "file_records"})


def _get_legacy_db_path() -> Path:
    """Resolve the legacy SQLite path from the current BASE_DIR value."""

    return Path(BASE_DIR) / "db" / "database.db"


def _tiktok_client_key() -> str:
    return str(
        os.environ.get("TIKTOK_CLIENT_KEY")
        or os.environ.get("TIKTOK_APP_CLIENT_KEY")
        or os.environ.get("TIKTOK_APP_KEY")
        or ""
    ).strip()


def _tiktok_client_secret() -> str:
    return str(
        os.environ.get("TIKTOK_CLIENT_SECRET")
        or os.environ.get("SAU_TIKTOK_CLIENT_SECRET")
        or os.environ.get("TIKTOK_APP_SECRET")
        or ""
    ).strip()


def _youtube_callback_base_url() -> str:
    configured = str(os.environ.get("SAU_YOUTUBE_CALLBACK_URL") or "").strip()
    if configured:
        return configured.rstrip("/")
    return "https://socialupload.iamwillywang.com/oauth/youtube/callback"


def _meta_callback_base_url() -> str:
    configured = str(os.environ.get("SAU_META_CALLBACK_URL") or "").strip()
    if configured:
        return configured.rstrip("/")
    return "https://socialupload.iamwillywang.com/oauth/meta/callback"


def _reddit_callback_base_url() -> str:
    configured = str(os.environ.get("SAU_REDDIT_CALLBACK_URL") or "").strip()
    if configured:
        return configured.rstrip("/")
    return "https://socialupload.iamwillywang.com/oauth/reddit/callback"


def _twitter_callback_base_url() -> str:
    configured = str(os.environ.get("SAU_TWITTER_CALLBACK_URL") or "").strip()
    if configured:
        return configured.rstrip("/")
    return "https://socialupload.iamwillywang.com/oauth/twitter/callback"


def _threads_callback_base_url() -> str:
    configured = str(os.environ.get("SAU_THREADS_CALLBACK_URL") or "").strip()
    if configured:
        return configured.rstrip("/")
    return "https://socialupload.iamwillywang.com/oauth/threads/callback"


def _tiktok_callback_base_url() -> str:
    configured = str(os.environ.get("SAU_TIKTOK_CALLBACK_URL") or "").strip()
    if configured:
        return configured.rstrip("/")
    return "https://socialupload.iamwillywang.com/oauth/tiktok/callback"


def _patreon_callback_base_url() -> str:
    configured = str(os.environ.get("SAU_PATREON_CALLBACK_URL") or "").strip()
    if configured:
        return configured.rstrip("/")
    return "https://socialupload.iamwillywang.com/oauth/patreon/callback"


def _tiktok_webhook_log_path() -> Path:
    path = Path(BASE_DIR) / "logs" / "webhooks" / "tiktok-events.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append_tiktok_webhook_event(event: dict) -> None:
    log_path = _tiktok_webhook_log_path()
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _parse_tiktok_signature(header_value: str | None) -> tuple[str | None, str | None]:
    if not header_value:
        return None, None
    timestamp = None
    signature = None
    for part in header_value.split(","):
        key, _, value = part.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key == "t":
            timestamp = value
        elif key == "s":
            signature = value
    return timestamp, signature


def _verify_tiktok_signature(raw_body: bytes, header_value: str | None) -> tuple[bool, str]:
    timestamp, signature = _parse_tiktok_signature(header_value)
    secret = _tiktok_client_secret()
    if not timestamp or not signature:
        return False, "missing_signature"
    if not secret:
        return False, "missing_secret"
    signed_payload = f"{timestamp}.".encode("utf-8") + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False, "signature_mismatch"
    try:
        age_seconds = abs(int(time.time()) - int(timestamp))
    except ValueError:
        return False, "invalid_timestamp"
    if age_seconds > 300:
        return False, "stale_timestamp"
    return True, "verified"


def _legacy_db_missing_required_tables(db_path: Path) -> bool:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name IN (?, ?)",
            tuple(sorted(LEGACY_DB_REQUIRED_TABLES)),
        ).fetchall()
    existing_tables = {row[0] for row in rows}
    return not LEGACY_DB_REQUIRED_TABLES.issubset(existing_tables)


def _ensure_legacy_db_ready() -> Path:
    """Create the legacy DB/schema on first run or after partial init."""

    db_path = _get_legacy_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if not db_path.exists() or _legacy_db_missing_required_tables(db_path):
        from db.createTable import bootstrap

        bootstrap(db_path)
    return db_path


# ---------------------------------------------------------------------------
# Shared upload/file safety helpers
# ---------------------------------------------------------------------------

def _safe_upload_filename(raw: str) -> str:
    """Sanitize an upload filename using werkzeug.secure_filename.

    Raises ValueError if the sanitized name is empty (e.g. all-special-chars input).
    """
    from werkzeug.utils import secure_filename
    safe = secure_filename(raw or "")
    if not safe:
        raise ValueError("invalid filename: resolves to empty after sanitization")
    return safe


def _is_valid_upload_key(key: str) -> bool:
    """Validate that an upload key matches the expected pattern.

    Expected format: uploads/<uuid>_<safe_filename>
    Rejects: path traversal, control characters, absolute paths.
    """
    import re
    if not key or not isinstance(key, str):
        return False
    # Reject path traversal and control characters
    if '..' in key:
        return False
    if key.startswith('/'):
        return False
    if any(ord(c) < 32 for c in key):
        return False
    # Must match uploads/<uuid>_<filename> pattern
    pattern = r'^uploads/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}_[^/\\]+$'
    return bool(re.match(pattern, key))


def _resolve_video_file_path_safely(file_path: str) -> Path | None:
    """Resolve a file path under videoFile, rejecting anything that escapes.

    Returns the resolved Path if it's inside videoFile, None otherwise.
    """
    if not file_path or not isinstance(file_path, str):
        return None
    base = (Path(BASE_DIR) / "videoFile").resolve()
    try:
        resolved = (base / file_path).resolve()
    except (ValueError, OSError):
        return None
    # Check that the resolved path is inside the base directory
    try:
        resolved.relative_to(base)
    except ValueError:
        return None
    return resolved


def _oauth_post_message_origin() -> str:
    """Return the safe target origin for OAuth postMessage callbacks.

    Reads SAU_APP_ORIGIN from env. Falls back to the production frontend origin.
    """
    return str(os.environ.get("SAU_APP_ORIGIN") or "https://up.iamwillywang.com").strip()


@app.before_request
def _enforce_auth():
    """Bearer-token gate for every non-public route.

    Skipped entirely when the policy is in open mode (no SAU_API_TOKENS set).
    The login SSE endpoint pulls the token from a query parameter because
    the browser EventSource API cannot send custom headers.
    """

    policy = app.config['SECURITY_POLICY']
    if policy.open_mode:
        return None
    if request.method == "OPTIONS":
        return None  # let the CORS layer answer preflight
    if policy.is_public_path(request.path):
        return None

    # Hybrid/oidc: a valid Google session (resolved by the tenancy middleware,
    # which runs before this hook) satisfies the gate. This flag is only ever
    # set when Google login is enabled, so legacy-mode behavior is unchanged.
    if getattr(g, "sau_session_authenticated", False):
        return None

    is_sse = request.path == "/login"
    token = extract_bearer_token(
        request.headers,
        request.args,
        is_sse=is_sse,
    )
    if not policy.token_is_valid(token):
        return jsonify({"code": 401, "msg": "unauthorized", "data": None}), 401
    return None


@app.route('/whoami', methods=['GET'])
def whoami():
    """Cheap health check used by the frontend login screen to validate a token."""

    policy = app.config['SECURITY_POLICY']
    return jsonify({
        "code": 200,
        "msg": "ok",
        "data": {
            "openMode": policy.open_mode,
            "authenticated": policy.open_mode or True,  # passed the gate
        }
    }), 200


# 限制上传文件大小为160MB
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB

# Frontend static asset resolution.
#
# The maintained SPA source now lives in sau_frontend/ and production builds
# emit into sau_frontend/dist/. Older deployments expected index.html/assets at
# the repository root, so we keep a conservative fallback chain.
current_dir = os.path.dirname(os.path.abspath(__file__))
_frontend_source_dir = Path(current_dir) / 'sau_frontend'
_frontend_dist_dir = _frontend_source_dir / 'dist'
_frontend_public_dir = _frontend_source_dir / 'public'


def _frontend_index_dir() -> Path:
    if (_frontend_dist_dir / 'index.html').exists():
        return _frontend_dist_dir
    if (_frontend_source_dir / 'index.html').exists():
        return _frontend_source_dir
    return Path(current_dir)


def _frontend_assets_dir() -> Path:
    if (_frontend_dist_dir / 'assets').exists():
        return _frontend_dist_dir / 'assets'
    if (_frontend_public_dir / 'assets').exists():
        return _frontend_public_dir / 'assets'
    return Path(current_dir) / 'assets'


def _frontend_public_asset(filename: str) -> tuple[Path, str]:
    dist_candidate = _frontend_dist_dir / filename
    if dist_candidate.exists():
        return _frontend_dist_dir, filename
    public_candidate = _frontend_public_dir / filename
    if public_candidate.exists():
        return _frontend_public_dir, filename
    return _frontend_index_dir(), filename


@app.route('/assets/<path:filename>')
def custom_static(filename):
    return send_from_directory(str(_frontend_assets_dir()), filename)


@app.route('/favicon.ico')
def favicon():
    directory, filename = _frontend_public_asset('vite.svg')
    return send_from_directory(str(directory), filename)


@app.route('/vite.svg')
def vite_svg():
    directory, filename = _frontend_public_asset('vite.svg')
    return send_from_directory(str(directory), filename)


@app.route('/socialupload-app-icon.png')
def app_icon():
    directory, filename = _frontend_public_asset('socialupload-app-icon.png')
    return send_from_directory(str(directory), filename)


@app.route('/')
def index():  # put application's code here
    return send_from_directory(str(_frontend_index_dir()), 'index.html')


@app.route('/privacy')
@app.route('/privacy/')
@app.route('/privacy-policy.html')
def privacy_page():
    directory, filename = _frontend_public_asset('privacy-policy.html')
    return send_from_directory(str(directory), filename)


@app.route('/terms')
@app.route('/terms/')
@app.route('/terms-of-service.html')
def terms_page():
    directory, filename = _frontend_public_asset('terms-of-service.html')
    return send_from_directory(str(directory), filename)


@app.route('/data-deletion')
@app.route('/data-deletion/')
def data_deletion_page():
    """Serve the data deletion instructions page (SPA route)."""
    return send_from_directory(str(_frontend_index_dir()), 'index.html')


@app.route('/api/data-deletion-request', methods=['POST'])
def api_data_deletion_request():
    """Handle data deletion request form submissions."""
    data = request.get_json(silent=True) or {}
    name = data.get('name', '').strip()
    email = data.get('email', '').strip()
    account = data.get('account', '').strip()
    details = data.get('details', '').strip()

    if not name or not email:
        return jsonify({'code': 400, 'msg': 'Name and email are required'}), 400

    # Log the deletion request
    logging.getLogger(__name__).warning(
        '[data-deletion] request received: name=%s email=%s account=%s details=%s',
        name, email, account, details
    )

    # Store in a simple file for tracking
    import json as _json
    from datetime import datetime
    db_path = _current_db_path()
    request_entry = {
        'timestamp': datetime.now().isoformat(),
        'name': name,
        'email': email,
        'account': account,
        'details': details,
    }
    deletion_log = db_path.parent / 'data_deletion_requests.jsonl'
    try:
        with open(deletion_log, 'a') as f:
            f.write(_json.dumps(request_entry) + '\n')
    except Exception:
        pass

    return jsonify({'code': 200, 'msg': 'Deletion request submitted successfully'}), 200

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({
            "code": 400,
            "data": None,
            "msg": "No file part in the request"
        }), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({
            "code": 400,
            "data": None,
            "msg": "No selected file"
        }), 400
    try:
        from werkzeug.utils import secure_filename
        safe_name = secure_filename(file.filename) or "file"
        # UUIDv4 — random, no MAC/timestamp leak. UUIDv1 (the legacy choice
        # here) embeds the host MAC address and creation time in the
        # filename, which the upload directory exposes via /getFile.
        file_uuid = uuid.uuid4()
        final_filename = f"{file_uuid}_{safe_name}"
        filepath = Path(BASE_DIR / "videoFile" / final_filename)
        file.save(filepath)
        return jsonify({"code": 200, "msg": "File uploaded successfully",
                        "data": final_filename}), 200
    except Exception as e:
        return jsonify({"code": 500, "msg": str(e), "data": None}), 500


@app.route('/upload/direct', methods=['POST'])
def upload_direct():
    """Generate a presigned upload URL for direct client-to-DO-Spaces upload.

    The client POSTs {filename, content_type} and receives {upload_url, public_url, key}.
    The client then PUTs the file directly to upload_url, bypassing the Flask server.
    """
    try:
        data = _read_json_body()
        filename = str(data.get("filename", "")).strip()
        content_type = str(data.get("content_type", "video/mp4")).strip()
        if not filename:
            return jsonify({"code": 400, "msg": "filename is required", "data": None}), 400

        safe_name = _safe_upload_filename(filename)
        import uuid as _uuid
        key = f"uploads/{_uuid.uuid4()}_{safe_name}"
        from myUtils import do_spaces
        result = do_spaces.generate_presigned_upload_url(key, content_type)
        return jsonify({"code": 200, "msg": "ok", "data": result}), 200
    except ValueError as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    except Exception as exc:
        logging.getLogger(__name__).exception("upload_direct failed")
        return jsonify({"code": 500, "msg": str(exc), "data": None}), 500


@app.route('/upload/file', methods=['POST'])
def upload_file_to_spaces():
    """Upload a file to DO Spaces via the backend (proxied upload).

    Accepts multipart/form-data with a 'file' field.
    Returns {key, public_url} on success.
    This avoids CORS issues with direct browser-to-Spaces uploads.
    """
    try:
        if 'file' not in request.files:
            return jsonify({"code": 400, "msg": "No file provided", "data": None}), 400

        f = request.files['file']
        if not f.filename:
            return jsonify({"code": 400, "msg": "Empty filename", "data": None}), 400

        import uuid as _uuid
        import tempfile
        import os as _os
        from werkzeug.utils import secure_filename
        safe_name = secure_filename(f.filename) or "file"
        key = f"uploads/{_uuid.uuid4()}_{safe_name}"

        # Save to temp file, then upload to Spaces
        with tempfile.NamedTemporaryFile(delete=False, suffix='.upload') as tmp:
            tmp_path = tmp.name
            f.save(tmp_path)

        try:
            from myUtils import do_spaces
            content_type = f.content_type or 'application/octet-stream'
            public_url = do_spaces.upload_file(tmp_path, key, content_type)
            filesize_mb = round(_os.path.getsize(tmp_path) / (1024 * 1024), 2)

            # Save record to file_records so it appears in 素材庫
            try:
                db_path = _ensure_legacy_db_ready()
                with sqlite3.connect(db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "INSERT INTO file_records (filename, filesize, file_path, storage_key, storage_cdn_url) VALUES (?, ?, ?, ?, ?)",
                        (safe_name, filesize_mb, key, key, public_url),
                    )
            except Exception:
                logging.getLogger(__name__).warning("Failed to save file_records entry for %s", key)

            return jsonify({
                "code": 200,
                "msg": "ok",
                "data": {"key": key, "public_url": public_url},
            }), 200
        finally:
            _os.unlink(tmp_path)
    except Exception as exc:
        logging.getLogger(__name__).exception("upload_file_to_spaces failed")
        return jsonify({"code": 500, "msg": str(exc), "data": None}), 500


@app.route('/upload/register', methods=['POST'])
def upload_register():
    """Register a file uploaded directly to DO Spaces in file_records.

    Called after a direct presigned PUT succeeds, so the file appears in 素材庫.
    POST {filename, key, public_url, size}
    """
    try:
        data = _read_json_body()
        filename = str(data.get("filename", "")).strip()
        key = str(data.get("key", "")).strip()
        public_url = str(data.get("public_url", "")).strip()
        size = data.get("size", 0)
        if not key or not filename:
            return jsonify({"code": 400, "msg": "filename and key required", "data": None}), 400

        # Strict key validation: must match uploads/<uuid>_<safe_filename>
        if not _is_valid_upload_key(key):
            return jsonify({"code": 400, "msg": "invalid key format: must be uploads/<uuid>_<filename>", "data": None}), 400

        safe_name = _safe_upload_filename(filename)
        try:
            filesize_mb = round(float(size) / (1024 * 1024), 2)
        except (TypeError, ValueError):
            filesize_mb = 0

        db_path = _ensure_legacy_db_ready()
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO file_records (filename, filesize, file_path, storage_key, storage_cdn_url) VALUES (?, ?, ?, ?, ?)",
                (safe_name, filesize_mb, key, key, public_url),
            )
        return jsonify({"code": 200, "msg": "ok", "data": None}), 200
    except ValueError as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    except Exception as exc:
        logging.getLogger(__name__).exception("upload_register failed")
        return jsonify({"code": 500, "msg": str(exc), "data": None}), 500


@app.route('/upload/multipart/init', methods=['POST'])
def multipart_upload_init():
    """Initiate a multipart upload to DO Spaces.

    POST {filename, content_type, size}
    Returns {upload_id, key, part_size, part_count}
    """
    try:
        data = _read_json_body()
        filename = str(data.get("filename", "")).strip()
        content_type = str(data.get("content_type", "video/mp4")).strip()
        size = int(data.get("size", 0))
        if not filename:
            return jsonify({"code": 400, "msg": "filename is required", "data": None}), 400

        import uuid as _uuid
        safe_name = _safe_upload_filename(filename)
        key = f"uploads/{_uuid.uuid4()}_{safe_name}"

        from myUtils import do_spaces
        client = do_spaces._default_client()
        result = client.create_multipart_upload(key, content_type)

        # Calculate part size: 10MB per part, minimum 5MB (S3 requirement)
        part_size = max(10 * 1024 * 1024, 5 * 1024 * 1024)
        # For very large files, use larger parts to keep part count reasonable
        if size > 500 * 1024 * 1024:
            part_size = 25 * 1024 * 1024
        elif size > 100 * 1024 * 1024:
            part_size = 15 * 1024 * 1024
        part_count = (size + part_size - 1) // part_size if size > 0 else 1

        return jsonify({
            "code": 200,
            "msg": "ok",
            "data": {
                "upload_id": result["upload_id"],
                "key": key,
                "part_size": part_size,
                "part_count": part_count,
                "public_url": client.cdn_url_for(key),
            },
        }), 200
    except Exception as exc:
        logging.getLogger(__name__).exception("multipart_upload_init failed")
        return jsonify({"code": 500, "msg": str(exc), "data": None}), 500


@app.route('/upload/multipart/presign', methods=['POST'])
def multipart_upload_presign():
    """Generate presigned URLs for one or more parts.

    POST {key, upload_id, part_numbers: [1, 2, 3, ...]}
    Returns {urls: {1: "https://...", 2: "https://...", ...}}
    """
    try:
        data = _read_json_body()
        key = str(data.get("key", "")).strip()
        upload_id = str(data.get("upload_id", "")).strip()
        part_numbers = data.get("part_numbers", [])
        if not key or not upload_id or not part_numbers:
            return jsonify({"code": 400, "msg": "key, upload_id, part_numbers required", "data": None}), 400

        from myUtils import do_spaces
        client = do_spaces._default_client()
        urls = {}
        for pn in part_numbers:
            urls[pn] = client.generate_presigned_part_url(key, upload_id, int(pn))

        return jsonify({"code": 200, "msg": "ok", "data": {"urls": urls}}), 200
    except Exception as exc:
        logging.getLogger(__name__).exception("multipart_upload_presign failed")
        return jsonify({"code": 500, "msg": str(exc), "data": None}), 500


@app.route('/upload/multipart/complete', methods=['POST'])
def multipart_upload_complete():
    """Complete a multipart upload.

    POST {key, upload_id, parts: [{PartNumber: 1, ETag: "..."}, ...]}
    Returns {public_url}
    """
    try:
        data = _read_json_body()
        key = str(data.get("key", "")).strip()
        upload_id = str(data.get("upload_id", "")).strip()
        parts = data.get("parts", [])
        if not key or not upload_id or not parts:
            return jsonify({"code": 400, "msg": "key, upload_id, parts required", "data": None}), 400

        # Validate key pattern
        import re as _re
        if not _re.match(r'^uploads/[0-9a-f-]{36}_', key):
            return jsonify({"code": 400, "msg": "Invalid key format", "data": None}), 400

        from myUtils import do_spaces
        client = do_spaces._default_client()
        public_url = client.complete_multipart_upload(key, upload_id, parts)

        # Save record to file_records so it appears in 素材庫
        try:
            filename = key.split('_', 1)[1] if '_' in key else key.split('/')[-1]
            size = data.get("size", 0)
            filesize_mb = round(size / (1024 * 1024), 2) if size else 0
            db_path = _ensure_legacy_db_ready()
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO file_records (filename, filesize, file_path, storage_key, storage_cdn_url) VALUES (?, ?, ?, ?, ?)",
                    (filename, filesize_mb, key, key, public_url),
                )
        except Exception:
            logging.getLogger(__name__).warning("Failed to save file_records entry for multipart %s", key)

        return jsonify({"code": 200, "msg": "ok", "data": {"public_url": public_url}}), 200
    except Exception as exc:
        logging.getLogger(__name__).exception("multipart_upload_complete failed")
        return jsonify({"code": 500, "msg": str(exc), "data": None}), 500


@app.route('/upload/multipart/part-proxy', methods=['POST'])
def multipart_upload_part_proxy():
    """Proxy a single multipart part upload through the backend.

    Accepts multipart/form-data with 'file' field plus query params:
      key, upload_id, part_number
    Returns {etag, part_number}
    Used as fallback when direct presigned PUT to DO Spaces fails (CORS).
    """
    try:
        if 'file' not in request.files:
            return jsonify({"code": 400, "msg": "No file provided", "data": None}), 400

        key = request.form.get('key', '').strip()
        upload_id = request.form.get('upload_id', '').strip()
        part_number = int(request.form.get('part_number', 0))
        if not key or not upload_id or not part_number:
            return jsonify({"code": 400, "msg": "key, upload_id, part_number required", "data": None}), 400

        # Validate key pattern (must start with uploads/ and contain a UUID)
        import re as _re
        if not _re.match(r'^uploads/[0-9a-f-]{36}_', key):
            return jsonify({"code": 400, "msg": "Invalid key format", "data": None}), 400
        # Validate part_number range (S3 allows 1-10000)
        if not (1 <= part_number <= 10000):
            return jsonify({"code": 400, "msg": "part_number must be 1-10000", "data": None}), 400

        import tempfile, os
        f = request.files['file']
        with tempfile.NamedTemporaryFile(delete=False, suffix='.part') as tmp:
            tmp_path = tmp.name
            f.save(tmp_path)

        try:
            from myUtils import do_spaces
            client = do_spaces._default_client()
            s3 = client._get_client()
            with open(tmp_path, 'rb') as fp:
                resp = s3.upload_part(
                    Bucket=client.bucket,
                    Key=key,
                    PartNumber=part_number,
                    UploadId=upload_id,
                    Body=fp,
                )
            etag = resp["ETag"]
            return jsonify({
                "code": 200,
                "msg": "ok",
                "data": {"etag": etag, "part_number": part_number},
            }), 200
        finally:
            os.unlink(tmp_path)
    except Exception as exc:
        logging.getLogger(__name__).exception("multipart_upload_part_proxy failed")
        return jsonify({"code": 500, "msg": str(exc), "data": None}), 500


@app.route('/getFile', methods=['GET'])
def get_file():
    filename = request.args.get('filename')

    if not filename:
        return jsonify({"code": 400, "msg": "filename is required", "data": None}), 400

    # Robust path-traversal guard: resolve the requested path inside the video
    # directory and reject anything that escapes the base directory. This
    # handles `..`, absolute paths, Windows backslashes and percent-encoded
    # variants (which Werkzeug already decoded into `filename`).
    base_dir = Path(BASE_DIR / "videoFile").resolve()
    try:
        target = (base_dir / filename).resolve()
    except (OSError, ValueError):
        return jsonify({"code": 400, "msg": "Invalid filename", "data": None}), 400

    if not target.is_relative_to(base_dir):
        return jsonify({"code": 400, "msg": "Invalid filename", "data": None}), 400
    if not target.is_file():
        # Try redirecting to remote storage CDN
        cdn = _get_cdn_url_for_file(filename)
        if not cdn and '/' not in filename:
            # Also try matching by storage_key (full path like uploads/uuid_name.mp4)
            cdn = _get_cdn_url_for_file_by_key(filename)
        if cdn:
            return redirect(cdn, code=302)
        return jsonify({"code": 404, "msg": "File not found", "data": None}), 404

    return send_from_directory(str(base_dir), target.name)


def _resolve_cookie_path(raw_path: str) -> Path:
    requested = Path(raw_path)
    if requested.is_absolute():
        return requested.resolve()
    return Path(BASE_DIR / "cookiesFile" / raw_path).resolve()


def _allowed_cookie_roots() -> tuple[Path, ...]:
    return (
        Path(BASE_DIR / "cookiesFile").resolve(),
        Path(BASE_DIR / "cookies").resolve(),
    )


def _cookie_path_is_allowed(cookie_path: Path) -> bool:
    return any(cookie_path.is_relative_to(root) for root in _allowed_cookie_roots())


def _lookup_account_cookie_path(account_id: int) -> tuple[Path, str] | None:
    db_path = _current_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        legacy = conn.execute(
            "SELECT filePath FROM user_info WHERE id = ?",
            (account_id,),
        ).fetchone()
        if legacy is not None:
            return _resolve_cookie_path(legacy["filePath"]), "legacy"

        structured = conn.execute(
            "SELECT cookie_path FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if structured is not None and structured["cookie_path"]:
            return _resolve_cookie_path(structured["cookie_path"]), "structured"
    return None


@app.route('/uploadSave', methods=['POST'])
def upload_save():
    if 'file' not in request.files:
        return jsonify({
            "code": 400,
            "data": None,
            "msg": "No file part in the request"
        }), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({
            "code": 400,
            "data": None,
            "msg": "No selected file"
        }), 400

    try:
        from werkzeug.utils import secure_filename
        # 获取表单中的自定义文件名（可选）
        custom_filename = request.form.get('filename', None)
        if custom_filename:
            safe_custom = secure_filename(custom_filename) or "file"
            # Preserve original extension
            orig_ext = Path(file.filename).suffix
            filename = f"{safe_custom}{orig_ext}"
        else:
            filename = secure_filename(file.filename) or "file"

        # UUIDv4 — see /upload for the rationale (no MAC/timestamp leak).
        file_uuid = uuid.uuid4()
        final_filename = f"{file_uuid}_{filename}"
        filepath = Path(BASE_DIR / "videoFile" / final_filename)

        # 保存文件
        file.save(filepath)

        db_path = Path(BASE_DIR / "db" / "database.db")
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                                INSERT INTO file_records (filename, filesize, file_path)
            VALUES (?, ?, ?)
                                ''', (filename, round(float(os.path.getsize(filepath)) / (1024 * 1024),2), final_filename))
            file_record_id = cursor.lastrowid
            conn.commit()
            print("✅ 上传文件已记录")

        # Upload to remote storage in background
        threading.Thread(
            target=_upload_file_to_storage,
            args=(filepath, final_filename, file_record_id),
            kwargs={"db_path": db_path},
            daemon=True,
        ).start()

        return jsonify({
            "code": 200,
            "msg": "File uploaded and saved successfully",
            "data": {
                "filename": filename,
                "filepath": final_filename
            }
        }), 200

    except Exception as e:
        print(f"Upload failed: {e}")
        return jsonify({
            "code": 500,
            "msg": f"upload failed: {e}",
            "data": None
        }), 500

@app.route('/getFiles', methods=['GET'])
def get_all_files():
    try:
        db_path = _ensure_legacy_db_ready()
        # 使用 with 自动管理数据库连接
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row  # 允许通过列名访问结果
            cursor = conn.cursor()

            # 查询所有记录
            cursor.execute("SELECT * FROM file_records")
            rows = cursor.fetchall()

            # 将结果转为字典列表，并提取UUID
            data = []
            for row in rows:
                row_dict = dict(row)
                # 从 file_path 中提取 UUID (文件名的第一部分，下划线前)
                if row_dict.get('file_path'):
                    file_path_parts = row_dict['file_path'].split('_', 1)  # 只分割第一个下划线
                    if len(file_path_parts) > 0:
                        row_dict['uuid'] = file_path_parts[0]  # UUID 部分
                    else:
                        row_dict['uuid'] = ''
                else:
                    row_dict['uuid'] = ''
                data.append(row_dict)

            return jsonify({
                "code": 200,
                "msg": "success",
                "data": data
            }), 200
    except Exception as e:
        print(f"get files failed: {e}")
        return jsonify({
            "code": 500,
            "msg": f"get file failed: {e}",
            "data": None
        }), 500


@app.route("/getAccounts", methods=['GET'])
def getAccounts():
    """快速获取所有账号信息，不进行cookie验证"""
    try:
        db_path = _ensure_legacy_db_ready()
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
            SELECT * FROM user_info''')
            rows = cursor.fetchall()
            rows_list = [list(row) for row in rows]

            print("\n📋 当前数据表内容（快速获取）：")
            for row in rows:
                print(row)

            return jsonify(
                {
                    "code": 200,
                    "msg": None,
                    "data": rows_list
                }), 200
    except Exception as e:
        print(f"获取账号列表时出错: {str(e)}")
        return jsonify({
            "code": 500,
            "msg": f"获取账号列表失败: {str(e)}",
            "data": None
        }), 500


@app.route("/getValidAccounts",methods=['GET'])
async def getValidAccounts():
    with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
        cursor = conn.cursor()
        cursor.execute('''
        SELECT * FROM user_info''')
        rows = cursor.fetchall()
        rows_list = [list(row) for row in rows]
        print("\n📋 当前数据表内容：")
        for row in rows:
            print(row)
        for row in rows_list:
            flag = await check_cookie(row[1],row[2])
            if not flag:
                row[4] = 0
                cursor.execute('''
                UPDATE user_info 
                SET status = ? 
                WHERE id = ?
                ''', (0,row[0]))
                conn.commit()
                print("✅ 用户状态已更新")
        for row in rows:
            print(row)
        return jsonify(
                        {
                            "code": 200,
                            "msg": None,
                            "data": rows_list
                        }),200

@app.route('/deleteFile', methods=['GET'])
def delete_file():
    file_id = request.args.get('id')

    if not file_id or not file_id.isdigit():
        return jsonify({
            "code": 400,
            "msg": "Invalid or missing file ID",
            "data": None
        }), 400

    try:
        # 获取数据库连接
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 查询要删除的记录
            cursor.execute("SELECT * FROM file_records WHERE id = ?", (file_id,))
            record = cursor.fetchone()

            if not record:
                return jsonify({
                    "code": 404,
                    "msg": "File not found",
                    "data": None
                }), 404

            record = dict(record)

            # 获取文件路径并删除实际文件 (safe path resolution)
            file_path = _resolve_video_file_path_safely(record['file_path'])
            if file_path and file_path.exists():
                try:
                    file_path.unlink()  # 删除文件
                    print(f"✅ 实际文件已删除: {file_path}")
                except Exception as e:
                    print(f"⚠️ 删除实际文件失败: {e}")
                    # 即使删除文件失败，也要继续删除数据库记录，避免数据不一致
            else:
                print(f"⚠️ 实际文件不存在或路径不安全: {record.get('file_path')}")

            # Delete from remote storage if applicable
            if record.get('storage_key') and record.get('storage_backend_id'):
                _delete_file_from_storage(record['storage_key'], record['storage_backend_id'], db_path=Path(BASE_DIR / "db" / "database.db"))

            # 删除数据库记录
            cursor.execute("DELETE FROM file_records WHERE id = ?", (file_id,))
            conn.commit()

        return jsonify({
            "code": 200,
            "msg": "File deleted successfully",
            "data": {
                "id": record['id'],
                "filename": record['filename']
            }
        }), 200

    except Exception as e:
        print(f"delete file failed: {e}")
        return jsonify({
            "code": 500,
            "msg": f"delete failed: {e}",
            "data": None
        }), 500

@app.route('/deleteFiles', methods=['POST'])
def delete_files_batch():
    data = _read_json_body()
    ids = data.get('ids', [])
    if not ids:
        return jsonify({'code': 400, 'msg': 'No IDs provided', 'data': None}), 400

    succeeded = []
    failed = []
    try:
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            for file_id in ids:
                try:
                    cursor.execute("SELECT * FROM file_records WHERE id = ?", (int(file_id),))
                    record = cursor.fetchone()
                    if not record:
                        failed.append({'id': file_id, 'error': 'Not found'})
                        continue
                    record = dict(record)
                    file_path = _resolve_video_file_path_safely(record['file_path'])
                    if file_path and file_path.exists():
                        try:
                            file_path.unlink()
                        except Exception:
                            pass
                    if record.get('storage_key') and record.get('storage_backend_id'):
                        _delete_file_from_storage(record['storage_key'], record['storage_backend_id'], db_path=Path(BASE_DIR / "db" / "database.db"))
                    cursor.execute("DELETE FROM file_records WHERE id = ?", (record['id'],))
                    succeeded.append(record['id'])
                except Exception as e:
                    failed.append({'id': file_id, 'error': str(e)})
            conn.commit()
    except Exception as e:
        return jsonify({'code': 500, 'msg': f'Batch delete failed: {e}', 'data': None}), 500

    return jsonify({
        'code': 200,
        'msg': f'Deleted {len(succeeded)} files',
        'data': {
            'succeeded': len(succeeded),
            'failed': len(failed),
            'details': {'succeeded': succeeded, 'failed': failed}
        }
    }), 200

@app.route('/deleteAccount', methods=['GET'])
def delete_account():
    account_id = request.args.get('id')

    if not account_id or not account_id.isdigit():
        return jsonify({
            "code": 400,
            "msg": "Invalid or missing account ID",
            "data": None
        }), 400

    account_id = int(account_id)

    try:
        db_path = _current_db_path()
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            legacy = cursor.execute(
                "SELECT * FROM user_info WHERE id = ?",
                (account_id,),
            ).fetchone()
            if legacy is not None:
                record = dict(legacy)
                if record.get('filePath'):
                    cookie_file_path = _resolve_cookie_path(record['filePath'])
                    if cookie_file_path.exists():
                        try:
                            cookie_file_path.unlink()
                            print(f"✅ Cookie文件已删除: {cookie_file_path}")
                        except Exception as e:
                            print(f"⚠️ 删除Cookie文件失败: {e}")
                cursor.execute("DELETE FROM user_info WHERE id = ?", (account_id,))
                conn.commit()
                return jsonify({
                    "code": 200,
                    "msg": "account deleted successfully",
                    "data": None
                }), 200

            structured = cursor.execute(
                "SELECT cookie_path FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
            if structured is None:
                return jsonify({
                    "code": 404,
                    "msg": "account not found",
                    "data": None
                }), 404

            cookie_path = structured['cookie_path']
            if cookie_path:
                resolved_cookie_path = _resolve_cookie_path(cookie_path)
                if _cookie_path_is_allowed(resolved_cookie_path) and resolved_cookie_path.exists():
                    try:
                        resolved_cookie_path.unlink()
                        print(f"✅ Cookie文件已删除: {resolved_cookie_path}")
                    except Exception as e:
                        print(f"⚠️ 删除Cookie文件失败: {e}")

            cursor.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            conn.commit()

        return jsonify({
            "code": 200,
            "msg": "account deleted successfully",
            "data": None
        }), 200

    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": f"delete failed: {str(e)}",
            "data": None
        }), 500


# SSE 登录接口
@app.route('/login')
def login():
    # When the browser simply visits /login without SSE parameters, serve the
    # SPA so that hash-based client-side routing takes over.
    if not request.args.get('type') and not request.args.get('id') and not request.args.get('accountId'):
        return send_from_directory(str(_frontend_index_dir()), 'index.html')

    account_id_raw = request.args.get('accountId')

    # New cookie-acquire flow for structured accounts (Reddit, Twitter, etc.)
    if account_id_raw:
        db_path = _current_db_path()
        try:
            account = profile_registry.get_account(int(account_id_raw), db_path=db_path)
        except (ValueError, LookupError):
            return jsonify({"code": 404, "msg": "Account not found", "data": None}), 404

        generator = COOKIE_ACQUISITION_GENERATORS.get(account.platform)
        if generator is None:
            return jsonify({"code": 400, "msg": f"No browser cookie flow for {account.platform}", "data": None}), 400

        cookie_path = account.cookie_path
        if not cookie_path:
            profile = profile_registry.get_profile(account.profile_id, db_path=db_path)
            cookie_path = str(profile_registry.resolve_cookie_path(account.platform, profile.slug, account.account_name))
            profile_registry.update_account(account.id, cookie_path=cookie_path, db_path=db_path)

        queue_id = f"cookie:{account.platform}:{account.id}"
        status_queue = Queue()
        active_queues[queue_id] = status_queue

        thread = threading.Thread(
            target=lambda gen, path, q: asyncio.new_event_loop().run_until_complete(gen(path, q)),
            args=(generator, str(cookie_path), status_queue),
            daemon=True,
        )
        thread.start()
        response = Response(sse_stream(status_queue, login_id=queue_id), mimetype='text/event-stream')
        response.headers['Cache-Control'] = 'no-cache'
        response.headers['X-Accel-Buffering'] = 'no'
        response.headers['Content-Type'] = 'text/event-stream'
        response.headers['Connection'] = 'keep-alive'
        return response

    # 1 小红书 2 视频号 3 抖音 4 快手
    type = request.args.get('type')
    # 账号名
    id = request.args.get('id')

    # 模拟一个用于异步通信的队列
    status_queue = Queue()
    active_queues[id] = status_queue

    # 启动异步任务线程
    thread = threading.Thread(target=run_async_function, args=(type, id, status_queue), daemon=True)
    thread.start()
    response = Response(sse_stream(status_queue, login_id=id), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'  # 关键：禁用 Nginx 缓冲
    response.headers['Content-Type'] = 'text/event-stream'
    response.headers['Connection'] = 'keep-alive'
    return response

@app.route('/postVideo', methods=['POST'])
def postVideo():
    # 获取JSON数据
    data = request.get_json()

    if not data:
        return jsonify({"code": 400, "msg": "请求数据不能为空", "data": None}), 400

    # 从JSON数据中提取fileList和accountList
    file_list = data.get('fileList', [])
    account_list = data.get('accountList', [])
    type = data.get('type')
    title = data.get('title')
    tags = data.get('tags')
    category = data.get('category')
    enableTimer = data.get('enableTimer')
    if category == 0:
        category = None
    productLink = data.get('productLink', '')
    productTitle = data.get('productTitle', '')
    thumbnail_path = data.get('thumbnail', '')
    is_draft = data.get('isDraft', False)  # 新增参数：是否保存为草稿

    videos_per_day = data.get('videosPerDay')
    daily_times = data.get('dailyTimes')
    start_days = data.get('startDays')

    # 参数校验
    if not file_list:
        return jsonify({"code": 400, "msg": "文件列表不能为空", "data": None}), 400
    if not account_list:
        return jsonify({"code": 400, "msg": "账号列表不能为空", "data": None}), 400
    if not type:
        return jsonify({"code": 400, "msg": "平台类型不能为空", "data": None}), 400
    if not title:
        return jsonify({"code": 400, "msg": "标题不能为空", "data": None}), 400

    # 打印获取到的数据（仅作为示例）
    print("File List:", file_list)
    print("Account List:", account_list)

    try:
        platform = _resolve_publish_platform(type)
        match platform:
            case "xiaohongshu":
                post_video_xhs(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                               start_days)
            case "tencent":
                post_video_tencent(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                                   start_days, is_draft)
            case "douyin":
                post_video_DouYin(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                                  start_days, thumbnail_path, productLink, productTitle)
            case "kuaishou":
                post_video_ks(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                              start_days)
            case "twitter":
                post_video_twitter(title, file_list, tags, account_list)
            case _:
                return jsonify({"code": 400, "msg": f"不支持的平台类型: {type}", "data": None}), 400

        # 返回响应给客户端
        return jsonify(
            {
                "code": 200,
                "msg": "发布任务已提交",
                "data": None
            }), 200
    except Exception as e:
        print(f"发布视频时出错: {str(e)}")
        return jsonify({
            "code": 500,
            "msg": f"发布失败: {str(e)}",
            "data": None
        }), 500


@app.route('/updateUserinfo', methods=['POST'])
def updateUserinfo():
    # 获取JSON数据
    data = request.get_json()
    if not data:
        return jsonify({"code": 400, "msg": "请求数据不能为空", "data": None}), 400

    # 从JSON数据中提取 type 和 userName
    user_id = data.get('id')
    type = data.get('type')
    userName = data.get('userName')
    try:
        # 获取数据库连接
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 更新数据库记录
            cursor.execute('''
                           UPDATE user_info
                           SET type     = ?,
                               userName = ?
                           WHERE id = ?;
                           ''', (type, userName, user_id))
            conn.commit()

        return jsonify({
            "code": 200,
            "msg": "account update successfully",
            "data": None
        }), 200

    except Exception as e:
        print(f"update userinfo failed: {e}")
        return jsonify({
            "code": 500,
            "msg": f"update failed: {e}",
            "data": None
        }), 500

@app.route('/postVideoBatch', methods=['POST'])
def postVideoBatch():
    data_list = request.get_json()

    if not isinstance(data_list, list):
        return jsonify({"code": 400, "msg": "Expected a JSON array", "data": None}), 400

    failures = []
    for index, data in enumerate(data_list):
        file_list = data.get('fileList', [])
        account_list = data.get('accountList', [])
        type = data.get('type')
        title = data.get('title')
        tags = data.get('tags')
        category = data.get('category')
        enableTimer = data.get('enableTimer')
        if category == 0:
            category = None
        productLink = data.get('productLink', '')
        productTitle = data.get('productTitle', '')
        thumbnail_path = data.get('thumbnail', '')
        is_draft = data.get('isDraft', False)

        videos_per_day = data.get('videosPerDay')
        daily_times = data.get('dailyTimes')
        start_days = data.get('startDays')
        print("File List:", file_list)
        print("Account List:", account_list)

        try:
            platform = _resolve_publish_platform(type)
            match platform:
                case "xiaohongshu":
                    post_video_xhs(title, file_list, tags, account_list, category, enableTimer,
                                   videos_per_day, daily_times, start_days)
                case "tencent":
                    post_video_tencent(title, file_list, tags, account_list, category, enableTimer,
                                       videos_per_day, daily_times, start_days, is_draft)
                case "douyin":
                    # NOTE: keyword args used here on purpose. The earlier positional
                    # call dropped `thumbnail_path` and silently bound `productLink`
                    # to the thumbnail parameter.
                    post_video_DouYin(title, file_list, tags, account_list,
                                      category=category, enableTimer=enableTimer,
                                      videos_per_day=videos_per_day, daily_times=daily_times,
                                      start_days=start_days,
                                      thumbnail_path=thumbnail_path,
                                      productLink=productLink, productTitle=productTitle)
                case "kuaishou":
                    post_video_ks(title, file_list, tags, account_list, category, enableTimer,
                                  videos_per_day, daily_times, start_days)
                case "twitter":
                    post_video_twitter(title, file_list, tags, account_list)
                case _:
                    failures.append({"index": index, "msg": f"unsupported platform type: {type}"})
        except Exception as exc:
            print(f"batch publish item {index} failed: {exc}")
            failures.append({"index": index, "msg": str(exc)})

    if failures:
        return jsonify({"code": 207, "msg": "partial success", "data": {"failures": failures}}), 207
    return jsonify({"code": 200, "msg": "ok", "data": None}), 200

# Cookie文件上传API
@app.route('/uploadCookie', methods=['POST'])
def upload_cookie():
    try:
        if 'file' not in request.files:
            return jsonify({
                "code": 400,
                "msg": "没有找到Cookie文件",
                "data": None
            }), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({
                "code": 400,
                "msg": "Cookie文件名不能为空",
                "data": None
            }), 400

        if not file.filename.endswith('.json'):
            return jsonify({
                "code": 400,
                "msg": "Cookie文件必须是JSON格式",
                "data": None
            }), 400

        # 获取账号信息
        account_id = request.form.get('id')
        platform = request.form.get('platform')

        if not account_id or not platform:
            return jsonify({
                "code": 400,
                "msg": "缺少账号ID或平台信息",
                "data": None
            }), 400

        account_cookie = _lookup_account_cookie_path(int(account_id))
        if not account_cookie:
            return jsonify({
                "code": 404,
                "msg": "账号不存在",
                "data": None
            }), 404

        # Validate the uploaded JSON looks like a Playwright storage_state file
        # BEFORE we touch the destination path, so a malformed upload cannot
        # silently overwrite a valid cookie file.
        try:
            payload_bytes = file.stream.read()
            from myUtils.security import (
                CookieValidationError,
                validate_storage_state,
            )
            validate_storage_state(payload_bytes)
        except CookieValidationError as exc:
            return jsonify({
                "code": 400,
                "msg": f"Cookie文件无效: {exc}",
                "data": None
            }), 400

        # Save the validated bytes via the cookie_storage helper so the file
        # lands encrypted on disk when SAU_COOKIE_ENCRYPTION_KEY is set, or
        # plaintext (with the same atomic temp-file + rename semantics) when
        # encryption is disabled.
        from myUtils.cookie_storage import write_cookie
        cookie_file_path = account_cookie[0]
        if not _cookie_path_is_allowed(cookie_file_path):
            return jsonify({
                "code": 400,
                "msg": "非法Cookie文件路径",
                "data": None
            }), 400
        write_cookie(cookie_file_path, payload_bytes)

        return jsonify({
            "code": 200,
            "msg": "Cookie文件上传成功",
            "data": None
        }), 200

    except Exception as e:
        print(f"上传Cookie文件时出错: {str(e)}")
        return jsonify({
            "code": 500,
            "msg": f"上传Cookie文件失败: {str(e)}",
            "data": None
        }), 500


@app.route('/accounts/<int:account_id>/import-cookies', methods=['POST'])
def import_cookies(account_id):
    """Import cookies from browser DevTools (tab-separated) or JSON array."""
    try:
        account = profile_registry.get_account(
            account_id, workspace_id=_workspace_scope(), db_path=_current_db_path()
        )
    except (ValueError, LookupError):
        return jsonify({"code": 404, "msg": "Account not found", "data": None}), 404

    body = request.get_json(silent=True) or {}
    raw = str(body.get('cookies') or '').strip()
    if not raw:
        return jsonify({"code": 400, "msg": "No cookie data provided", "data": None}), 400

    cookies = []
    raw = raw.strip()

    # Try JSON array first (EditThisCookie / Cookie-Editor export)
    if raw.startswith('['):
        try:
            import json as _json
            arr = _json.loads(raw)
            if isinstance(arr, list):
                for item in arr:
                    if not isinstance(item, dict) or 'name' not in item or 'value' not in item:
                        continue
                    cookies.append({
                        'name': str(item['name']),
                        'value': str(item['value']),
                        'domain': str(item.get('domain') or '.reddit.com'),
                        'path': str(item.get('path') or '/'),
                        'expires': item.get('expires', -1),
                        'httpOnly': bool(item.get('httpOnly', False)),
                        'secure': bool(item.get('secure', False)),
                        'sameSite': str(item.get('sameSite') or 'Lax'),
                    })
        except Exception:
            pass

    # Tab-separated: Chrome DevTools "Copy all" format
    if not cookies:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split('\t')
            # Skip header row
            if len(parts) < 2 or parts[0].lower() in ('name', 'key'):
                continue
            if len(parts) >= 2:
                name, value = parts[0], parts[1]
                domain = parts[2] if len(parts) > 2 else '.reddit.com'
                path = parts[3] if len(parts) > 3 else '/'
                expires_raw = parts[4] if len(parts) > 4 else '-1'
                try:
                    expires = int(float(expires_raw))
                except (ValueError, TypeError):
                    expires = -1
                http_only = parts[5].lower() == 'true' if len(parts) > 5 else False
                secure = parts[6].lower() == 'true' if len(parts) > 6 else False
                same_site = parts[7] if len(parts) > 7 else 'Lax'
                cookies.append({
                    'name': name,
                    'value': value,
                    'domain': domain,
                    'path': path,
                    'expires': expires,
                    'httpOnly': http_only,
                    'secure': secure,
                    'sameSite': same_site or 'Lax',
                })

    if not cookies:
        return jsonify({"code": 400, "msg": "No valid cookies found in input", "data": None}), 400

    storage_state = {"cookies": cookies, "origins": []}
    import json as _json
    payload_bytes = _json.dumps(storage_state, ensure_ascii=False).encode('utf-8')

    try:
        from myUtils.security import CookieValidationError, validate_storage_state
        validate_storage_state(payload_bytes)
    except CookieValidationError as exc:
        return jsonify({"code": 400, "msg": f"Invalid cookie data: {exc}", "data": None}), 400

    cookie_path = account.cookie_path
    if not cookie_path:
        profile = profile_registry.get_profile(account.profile_id, db_path=_current_db_path())
        cookie_path = str(profile_registry.resolve_cookie_path(account.platform, profile.slug, account.account_name))
        profile_registry.update_account(account.id, cookie_path=cookie_path, db_path=_current_db_path())

    if not _cookie_path_is_allowed(Path(cookie_path)):
        return jsonify({"code": 400, "msg": "Invalid cookie path", "data": None}), 400

    from myUtils.cookie_storage import write_cookie
    write_cookie(Path(cookie_path), payload_bytes)

    profile_registry.update_account(account.id, status=1, db_path=_current_db_path())

    return jsonify({
        "code": 200,
        "msg": f"Imported {len(cookies)} cookies",
        "data": {"cookieCount": len(cookies)},
    }), 200


# Cookie文件下载API
@app.route('/downloadCookie', methods=['GET'])
def download_cookie():
    try:
        file_path = request.args.get('filePath')
        if not file_path:
            return jsonify({
                "code": 400,
                "msg": "缺少文件路径参数",
                "data": None
            }), 400

        cookie_file_path = _resolve_cookie_path(file_path)
        if not _cookie_path_is_allowed(cookie_file_path):
            return jsonify({
                "code": 400,
                "msg": "非法文件路径",
                "data": None
            }), 400

        if not cookie_file_path.exists():
            return jsonify({
                "code": 404,
                "msg": "Cookie文件不存在",
                "data": None
            }), 404

        # Always serve plaintext to the user — they're typically exporting
        # the session for use in another tool. cookie_storage.read_cookie
        # transparently decrypts when SAU_COOKIE_ENCRYPTION_KEY is set and
        # returns the file verbatim otherwise.
        from myUtils.cookie_storage import CookieEncryptionError, read_cookie
        try:
            payload = read_cookie(cookie_file_path)
        except CookieEncryptionError as exc:
            return jsonify({
                "code": 500,
                "msg": f"无法解密 Cookie 文件: {exc}",
                "data": None,
            }), 500

        download_name = cookie_file_path.name
        if not download_name.endswith(".json"):
            download_name = f"{download_name}.json"
        return Response(
            payload,
            mimetype="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{download_name}"',
            },
        )

    except Exception as e:
        print(f"下载Cookie文件时出错: {str(e)}")
        return jsonify({
            "code": 500,
            "msg": f"下载Cookie文件失败: {str(e)}",
            "data": None
        }), 500


# ---------------------------------------------------------------------------
# Job runtime API
#
# These endpoints are the modern publish surface. They enqueue work into the
# `publish_jobs` table and return immediately; the worker (`POST /jobs/run` in
# single-process mode, or a separate process in production) drains the queue.
# The legacy `/postVideo` endpoint is preserved so the existing frontend keeps
# working unchanged.
# ---------------------------------------------------------------------------


def _resolve_publish_platform(value) -> str:
    if isinstance(value, str):
        stripped = value.strip().lower()
        if not stripped:
            raise ValueError("平台类型不能为空")
        if stripped.isdigit():
            value = int(stripped)
        elif stripped in TWITTER_PLATFORM_ALIASES:
            return "twitter"
        elif stripped in LEGACY_PLATFORM_CODES.values():
            return stripped
        else:
            raise ValueError(f"Unsupported platform code: {value!r}")

    if isinstance(value, int):
        if value not in LEGACY_PLATFORM_CODES:
            raise ValueError(f"Unsupported platform code: {value!r}")
        return LEGACY_PLATFORM_CODES[value]

    raise ValueError(f"Unsupported platform code: {value!r}")


def _normalise_publish_payload(data: dict) -> tuple[str, dict, list[tuple[str, str, datetime | None]]]:
    """Pull a /postVideo-shaped payload apart into (platform, payload, targets).

    Accepts either the legacy numeric ``type`` field or an explicit ``platform``
    string slug. Targets are the cartesian product of fileList × accountList,
    with optional schedule times derived from the timer fields.
    """

    platform = _resolve_publish_platform(data.get("platform", data.get("type")))

    file_list = data.get("fileList", [])
    account_list = data.get("accountList", [])
    if not file_list:
        raise ValueError("fileList must not be empty")
    if not account_list:
        raise ValueError("accountList must not be empty")

    enable_timer = bool(data.get("enableTimer"))
    if enable_timer:
        schedules = generate_schedule_time_next_day(
            len(file_list),
            data.get("videosPerDay", 1) or 1,
            data.get("dailyTimes") or None,
            start_days=data.get("startDays", 0) or 0,
        )
    else:
        schedules = [None] * len(file_list)

    payload = {
        "title": data.get("title", ""),
        "tags": data.get("tags") or [],
        "category": data.get("category"),
        "isDraft": bool(data.get("isDraft", False)),
        "thumbnail": data.get("thumbnail", "") or "",
        "productLink": data.get("productLink", "") or "",
        "productTitle": data.get("productTitle", "") or "",
    }
    if platform == "twitter":
        payload["threadFileRefs"] = list(file_list)

    targets: list[tuple[str, str, datetime | None]] = []
    if platform == "twitter":
        root_file_ref = str(file_list[0])
        for account_ref in account_list:
            targets.append((account_ref, root_file_ref, None))
        return platform, payload, targets

    for index, file_ref in enumerate(file_list):
        scheduled = schedules[index] if index < len(schedules) else None
        for account_ref in account_list:
            targets.append((account_ref, file_ref, scheduled))

    return platform, payload, targets


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}


def _current_db_path() -> Path:
    return _ensure_legacy_db_ready()


def _workspace_scope() -> str | None:
    """Workspace id to scope tenant queries by, or ``None`` for no scoping.

    Returns ``None`` in the default ``single``/``shadow`` tenancy modes and for
    legacy-token callers (single-tenant/admin path). In ``enforced`` mode a
    Google-session caller is scoped to their active workspace, so cross-workspace
    resources resolve as 404. Wired route-by-route (Phase 6); unscoped by default
    so existing behavior is unchanged.
    """
    config = app.config.get("SAU_APP_CONFIG")
    if config is None or getattr(config, "tenancy_mode", "single") != "enforced":
        return None
    ctx = getattr(g, "auth_ctx", None)
    if ctx is None or getattr(ctx, "auth_method", None) != "google_session":
        return None
    return getattr(ctx, "workspace_id", None)


def _profile_payload(profile: profile_registry.Profile) -> dict:
    return profile.to_dict()


def _account_payload(account: profile_registry.Account) -> dict:
    payload = account.to_dict()
    if account.platform == "tiktok":
        payload["isSandbox"] = os.environ.get("TIKTOK_CLIENT_KEY", "").startswith("sb")
    return payload


def _media_group_payload(
    media_group: media_group_store.MediaGroup,
    *,
    items: list[media_group_store.MediaGroupItem] | None = None,
) -> dict:
    payload = media_group.to_dict()
    if items is not None:
        payload["items"] = [item.to_dict() for item in items]
    return payload


def _campaign_payload(campaign: campaign_store.Campaign, *, db_path: Path) -> dict:
    payload = campaign.to_dict()
    payload["artifacts"] = [
        artifact.to_dict()
        for artifact in campaign_store.list_campaign_artifacts(campaign.id, db_path=db_path)
    ]
    payload["posts"] = [
        post.to_dict()
        for post in campaign_store.list_campaign_posts(campaign.id, db_path=db_path)
    ]
    return payload


def _artifact_payloads_for_platform(artifacts: list[dict], platform: str) -> list[dict]:
    if platform != profile_registry.PLATFORM_TIKTOK:
        return [artifact for artifact in artifacts if artifact.get("artifact_kind") not in {"raw_remote_upload", "raw_local"}]

    grouped: dict[tuple[object, object], list[dict]] = {}
    passthrough: list[dict] = []
    for artifact in artifacts:
        kind = artifact.get("artifact_kind") or ""
        source_id = artifact.get("source_file_record_id")
        if kind in {"remote_upload", "raw_remote_upload", "local", "raw_local"} and source_id is not None:
            role = ((artifact.get("metadata") or {}).get("role"))
            grouped.setdefault((source_id, role), []).append(artifact)
        elif kind not in {"watermarked_image", "watermarked_video"}:
            passthrough.append(artifact)

    selected = []
    for items in grouped.values():
        raw = next((item for item in items if item.get("artifact_kind") in {"raw_remote_upload", "raw_local"}), None)
        selected.append(raw or items[0])
    return [*passthrough, *selected]


def _read_json_body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    return data


def _account_profile_settings(profile_id: int | None, *, db_path: Path) -> dict:
    if not profile_id:
        return {}
    try:
        return profile_registry.get_profile(profile_id, db_path=db_path).settings or {}
    except LookupError:
        return {}


def _append_tiktok_review_event(
    event_type: str,
    payload: dict,
    *,
    account_id: int | None = None,
    account_name: str | None = None,
    signature_verified: bool | None = None,
    signature_status: str | None = None,
    status: str | None = None,
    metadata: dict | None = None,
    db_path: Path | None = None,
) -> dict:
    event = tiktok_review.add_review_event(
        event_type=event_type,
        status=status or str(payload.get('status') or 'received'),
        account_id=account_id,
        account_name=account_name,
        signature_verified=signature_verified,
        signature_status=signature_status,
        payload=payload,
        headers=payload.get('headers') if isinstance(payload.get('headers'), dict) else {},
        metadata=metadata,
        db_path=db_path,
    )
    result = event.to_dict()
    result['receivedAt'] = result.pop('received_at', None)
    result['type'] = result.pop('event_type')
    result['signatureVerified'] = result.pop('signature_verified')
    result['signatureStatus'] = result.pop('signature_status')
    return result


def _event_payload_to_status(event: tiktok_review.TikTokReviewEvent | None) -> dict | None:
    if event is None:
        return None
    payload = dict(event.payload or {})
    payload.setdefault('status', event.status)
    payload['receivedAt'] = event.received_at
    payload['type'] = event.event_type
    payload['signatureVerified'] = event.signature_verified
    payload['signatureStatus'] = event.signature_status
    if event.account_id is not None:
        payload.setdefault('accountId', event.account_id)
    if event.account_name:
        payload.setdefault('accountName', event.account_name)
    return payload


def _oauth_request_to_status(request_state) -> dict | None:
    if request_state is None:
        return None
    payload = dict(request_state.result or {})
    payload.setdefault('state', request_state.state_token)
    payload['status'] = request_state.status
    payload['requestedAt'] = request_state.requested_at
    payload['completedAt'] = request_state.completed_at
    payload['error'] = request_state.error_text
    payload['redirectUri'] = request_state.redirect_uri
    payload['scopes'] = request_state.scopes
    if getattr(request_state, 'account_id', None) is not None:
        payload.setdefault('accountId', request_state.account_id)
    if getattr(request_state, 'account_name', None):
        payload.setdefault('accountName', request_state.account_name)
    if getattr(request_state, 'platform', None):
        payload.setdefault('platform', request_state.platform)
    return payload


def _run_account_connection_check(*, account_id: int, db_path: Path):
    account = profile_registry.get_account(account_id, db_path=db_path)
    config = dict(account.config or {})
    now = datetime.now().isoformat(timespec='seconds')

    try:
        if account.platform == profile_registry.PLATFORM_FACEBOOK:
            result = prepared_publishers.validate_facebook_config_live(config)
            config['facebookPageName'] = result.get('name', config.get('facebookPageName', ''))
            summary = f"Facebook page: {config.get('facebookPageName') or account.account_name}"
        elif account.platform == profile_registry.PLATFORM_INSTAGRAM:
            result = prepared_publishers.validate_instagram_config_live(config)
            config['instagramUserName'] = result.get('username', config.get('instagramUserName', ''))
            summary = f"Instagram user: {config.get('instagramUserName') or account.account_name}"
        elif account.platform == profile_registry.PLATFORM_THREADS:
            result = prepared_publishers.validate_threads_config_live(config)
            config['threadsUserName'] = result.get('username', config.get('threadsUserName', ''))
            summary = f"Threads user: {config.get('threadsUserName') or account.account_name}"
        elif account.platform == profile_registry.PLATFORM_TELEGRAM:
            result = prepared_publishers.validate_telegram_config_live(config)
            config['telegramBotName'] = result.get('bot', {}).get('result', {}).get('username', config.get('telegramBotName', ''))
            config['telegramChatTitle'] = result.get('chat', {}).get('result', {}).get('title', config.get('telegramChatTitle', '')) or result.get('chat', {}).get('result', {}).get('username', config.get('telegramChatTitle', ''))
            summary = f"Telegram chat: {config.get('telegramChatTitle') or account.account_name}"
        elif account.platform == profile_registry.PLATFORM_DISCORD:
            result = prepared_publishers.validate_discord_config_live(config)
            config['discordWebhookName'] = result.get('name', config.get('discordWebhookName', ''))
            config['discordWebhookChannel'] = result.get('channel_id', config.get('discordWebhookChannel', ''))
            summary = f"Discord webhook: {config.get('discordWebhookName') or account.account_name}"
        elif account.platform == profile_registry.PLATFORM_TWITTER:
            twitter_auth_type = str(config.get("twitterAuthType") or account.auth_type or "cookie").strip().lower()
            if twitter_auth_type == "cookie":
                summary = f"Twitter cookie account: {account.account_name}"
            else:
                result = prepared_publishers.validate_twitter_config_live(config)
                twitter_data = result.get('data', {})
                config['twitterUserId'] = twitter_data.get('id', config.get('twitterUserId', ''))
                config['twitterUserName'] = twitter_data.get('username', config.get('twitterUserName', ''))
                config['twitterDisplayName'] = twitter_data.get('name', config.get('twitterDisplayName', ''))
                summary = f"Twitter user: @{config.get('twitterUserName') or account.account_name}"
        else:
            raise ValueError('Connection check is implemented only for Facebook, Instagram, Threads, Telegram, Discord, and Twitter')

        config['lastConnectionCheckAt'] = now
        updated = profile_registry.update_account(
            account_id,
            config=config,
            auth_type=account.auth_type,
            db_path=db_path,
        )
        account_events.record_event(
            account_id=updated.id,
            profile_id=updated.profile_id,
            platform=updated.platform,
            account_name=updated.account_name,
            action='check_connection',
            status='ok',
            summary=summary,
            metadata={'timestamp': now},
            db_path=db_path,
        )
        return updated
    except Exception as exc:
        account_events.record_event(
            account_id=account.id,
            profile_id=account.profile_id,
            platform=account.platform,
            account_name=account.account_name,
            action='check_connection',
            status='error',
            summary='Connection check failed',
            error_text=str(exc),
            metadata={'timestamp': now},
            db_path=db_path,
        )
        raise


def _run_account_token_refresh(*, account_id: int, db_path: Path, mode: str = "manual"):
    account = profile_registry.get_account(account_id, db_path=db_path)
    config = dict(account.config or {})
    now = datetime.now().isoformat(timespec='seconds')

    try:
        if account.platform == profile_registry.PLATFORM_TIKTOK:
            refresh_token = str(config.get('refreshToken') or '').strip()
            if not refresh_token:
                raise ValueError('TikTok account is missing refreshToken')
            token_payload = tiktok_auth.refresh_access_token(refresh_token=refresh_token)
            access_token = str(token_payload.get('access_token') or '')
            user_info = tiktok_auth.fetch_user_info(access_token=access_token) if access_token else {}
            config = prepared_publishers._apply_tiktok_token_payload(config, token_payload, user_info)
            config.update({
                'openId': token_payload.get('open_id') or config.get('openId') or '',
                'scope': token_payload.get('scope') or config.get('scope') or '',
                'displayName': user_info.get('data', {}).get('user', {}).get('display_name') or config.get('displayName') or '',
                'avatarUrl': user_info.get('data', {}).get('user', {}).get('avatar_url') or config.get('avatarUrl') or '',
                ('lastAutoRefreshAt' if mode == 'auto' else 'lastManualRefreshAt'): now,
            })
            updated = profile_registry.update_account(
                account_id,
                config=config,
                auth_type='oauth',
                status=1,
                db_path=db_path,
            )
            _append_tiktok_review_event(
                'refresh',
                {
                    'status': 'ok',
                    'mode': mode,
                    'accountId': account_id,
                    'accountName': updated.account_name,
                    'openId': config.get('openId', ''),
                    'scope': config.get('scope', ''),
                    'displayName': config.get('displayName', ''),
                    'avatarUrl': config.get('avatarUrl', ''),
                },
                account_id=account_id,
                account_name=updated.account_name,
                status='ok',
                metadata={'mode': mode},
                db_path=db_path,
            )
            summary = f"TikTok refreshed: {config.get('displayName') or updated.account_name}"
        elif account.platform == profile_registry.PLATFORM_REDDIT:
            if str(config.get('redditAuthType') or '') == 'cookie':
                raise ValueError('Cannot refresh token for a cookie-based Reddit account. Switch to OAuth auth type first.')
            refreshed = prepared_publishers.refresh_reddit_access_token(config)
            config.update({
                'accessToken': refreshed['access_token'],
                'scope': refreshed.get('scope', config.get('scope', '')),
                'accessTokenUpdatedAt': now,
                ('lastAutoRefreshAt' if mode == 'auto' else 'lastManualRefreshAt'): now,
                'redditUserName': refreshed.get('me', {}).get('name', config.get('redditUserName', '')),
            })
            expires_in = refreshed.get('expires_in')
            if expires_in:
                config['accessTokenExpiresAt'] = (
                    datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
                ).isoformat(timespec='seconds')
            updated = profile_registry.update_account(
                account_id,
                config=config,
                auth_type='oauth',
                status=1,
                db_path=db_path,
            )
            summary = f"Reddit refreshed: {config.get('redditUserName') or updated.account_name}"
        elif account.platform == profile_registry.PLATFORM_YOUTUBE:
            refreshed = prepared_publishers.refresh_youtube_access_token(config)
            config.update({
                'accessToken': refreshed['access_token'],
                'accessTokenUpdatedAt': now,
                ('lastAutoRefreshAt' if mode == 'auto' else 'lastManualRefreshAt'): now,
            })
            expires_in = refreshed.get('expires_in')
            if expires_in:
                config['accessTokenExpiresAt'] = (
                    datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
                ).isoformat(timespec='seconds')
            channel_items = refreshed.get('channel', {}).get('items', []) if isinstance(refreshed.get('channel'), dict) else []
            if channel_items:
                snippet = channel_items[0].get('snippet', {}) if isinstance(channel_items[0], dict) else {}
                config['channelTitle'] = snippet.get('title', config.get('channelTitle', ''))
                avatar_url = snippet.get('thumbnails', {}).get('default', {}).get('url')
                if avatar_url:
                    config['avatarUrl'] = avatar_url
            updated = profile_registry.update_account(
                account_id,
                config=config,
                auth_type='oauth',
                status=1,
                db_path=db_path,
            )
            summary = f"YouTube refreshed: {config.get('channelTitle') or updated.account_name}"
        elif account.platform == profile_registry.PLATFORM_THREADS:
            access_token = str(config.get('accessToken') or '').strip()
            if not access_token:
                raise ValueError('Threads account is missing accessToken')
            refreshed = threads_auth.refresh_long_lived_token(access_token=access_token)
            next_access_token = str(refreshed.get('access_token') or access_token)
            me_payload = threads_auth.fetch_me(access_token=next_access_token, fields=('id', 'username', 'threads_profile_picture_url')) if next_access_token else {}
            config.update({
                'accessToken': next_access_token,
                'accessTokenUpdatedAt': now,
                ('lastAutoRefreshAt' if mode == 'auto' else 'lastManualRefreshAt'): now,
                'threadUserId': str(me_payload.get('id') or config.get('threadUserId') or config.get('userId') or ''),
                'userId': str(me_payload.get('id') or config.get('userId') or config.get('threadUserId') or ''),
                'threadsUserName': str(me_payload.get('username') or config.get('threadsUserName') or ''),
            })
            if me_payload.get('threads_profile_picture_url'):
                config['avatarUrl'] = str(me_payload.get('threads_profile_picture_url'))
            expires_in = refreshed.get('expires_in')
            if expires_in:
                config['accessTokenExpiresAt'] = (
                    datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
                ).isoformat(timespec='seconds')
            updated = profile_registry.update_account(
                account_id,
                config=config,
                auth_type='oauth',
                status=1,
                db_path=db_path,
            )
            summary = f"Threads refreshed: {config.get('threadsUserName') or updated.account_name}"
        elif account.platform in {profile_registry.PLATFORM_FACEBOOK, profile_registry.PLATFORM_INSTAGRAM}:
            meta_user_access_token = str(config.get('metaUserAccessToken') or '').strip()
            if not meta_user_access_token:
                raise ValueError(f"{account.platform.capitalize()} account is missing metaUserAccessToken; reconnect required")
            meta_expiry = prepared_publishers._parse_iso_datetime(str(config.get('metaUserAccessTokenExpiresAt') or config.get('accessTokenExpiresAt') or ''))
            if meta_expiry is not None and meta_expiry <= prepared_publishers._utc_now():
                raise ValueError(f"{account.platform.capitalize()} Meta user access token expired; reconnect required")
            pages_payload = meta_auth.fetch_managed_pages(access_token=meta_user_access_token)
            pages = pages_payload.get('data', []) if isinstance(pages_payload, dict) else []
            if not isinstance(pages, list):
                pages = []
            config['accessTokenUpdatedAt'] = now
            config[('lastAutoRefreshAt' if mode == 'auto' else 'lastManualRefreshAt')] = now
            if account.platform == profile_registry.PLATFORM_FACEBOOK:
                wanted_page_id = str(config.get('pageId') or '').strip()
                selected_page = next((page for page in pages if str(page.get('id') or '') == wanted_page_id), None) if wanted_page_id else None
                if selected_page is None and pages:
                    selected_page = pages[0]
                if selected_page is None:
                    raise ValueError('Meta refresh did not return any manageable Facebook Pages')
                config['pageId'] = str(selected_page.get('id') or config.get('pageId') or '')
                config['facebookPageName'] = str(selected_page.get('name') or config.get('facebookPageName') or '')
                config['accessToken'] = str(selected_page.get('access_token') or config.get('accessToken') or '')
                if config.get('metaUserAccessTokenExpiresAt'):
                    config['accessTokenExpiresAt'] = str(config.get('metaUserAccessTokenExpiresAt'))
                # Fetch Facebook page profile picture
                try:
                    import requests as _requests
                    pic_resp = _requests.get(f"{meta_auth.META_GRAPH_ROOT}/{config['pageId']}", params={"fields": "picture.type(large)", "access_token": config['accessToken']}, timeout=15)
                    pic_url = pic_resp.json().get("picture", {}).get("data", {}).get("url")
                    if pic_url:
                        config['avatarUrl'] = pic_url
                except Exception:
                    pass
                updated = profile_registry.update_account(account_id, config=config, auth_type='oauth', status=1, db_path=db_path)
                summary = f"Facebook credentials re-synced: {config.get('facebookPageName') or updated.account_name}"
            else:
                wanted_ig_user_id = str(config.get('igUserId') or '').strip()
                selected_pair = None
                for page in pages:
                    ig_user = page.get('instagram_business_account') if isinstance(page, dict) else None
                    if not isinstance(ig_user, dict):
                        continue
                    if wanted_ig_user_id and str(ig_user.get('id') or '') != wanted_ig_user_id:
                        continue
                    selected_pair = (page, ig_user)
                    break
                if selected_pair is None:
                    for page in pages:
                        ig_user = page.get('instagram_business_account') if isinstance(page, dict) else None
                        if isinstance(ig_user, dict):
                            selected_pair = (page, ig_user)
                            break
                if selected_pair is None:
                    raise ValueError('Meta refresh did not return a page-linked Instagram business account')
                page, ig_user = selected_pair
                config['pageId'] = str(page.get('id') or config.get('pageId') or '')
                config['facebookPageName'] = str(page.get('name') or config.get('facebookPageName') or '')
                config['igUserId'] = str(ig_user.get('id') or config.get('igUserId') or '')
                config['instagramUserName'] = str(ig_user.get('username') or config.get('instagramUserName') or '')
                if ig_user.get('profile_picture_url'):
                    config['avatarUrl'] = str(ig_user.get('profile_picture_url'))
                config['accessToken'] = str(page.get('access_token') or config.get('accessToken') or '')
                if config.get('metaUserAccessTokenExpiresAt'):
                    config['accessTokenExpiresAt'] = str(config.get('metaUserAccessTokenExpiresAt'))
                updated = profile_registry.update_account(account_id, config=config, auth_type='oauth', status=1, db_path=db_path)
                summary = f"Instagram credentials re-synced: {config.get('instagramUserName') or updated.account_name}"
        elif account.platform == profile_registry.PLATFORM_TWITTER:
            twitter_auth_type = str(config.get("twitterAuthType") or account.auth_type or "cookie").strip().lower()
            if twitter_auth_type == "cookie":
                raise ValueError("Twitter cookie accounts do not support token refresh; use OAuth 2.0 API mode instead")
            refreshed = prepared_publishers.refresh_twitter_access_token(config)
            config.update({
                'accessToken': refreshed['access_token'],
                'refreshToken': refreshed.get('refresh_token', config.get('refreshToken', '')),
                'scope': refreshed.get('scope', config.get('scope', '')),
                'tokenType': refreshed.get('token_type', config.get('tokenType', 'bearer')),
                'accessTokenUpdatedAt': now,
                ('lastAutoRefreshAt' if mode == 'auto' else 'lastManualRefreshAt'): now,
            })
            user_data = refreshed.get('me', {}).get('data', {}) if isinstance(refreshed.get('me'), dict) else {}
            if isinstance(user_data, dict):
                config['twitterUserId'] = user_data.get('id', config.get('twitterUserId', ''))
                config['twitterUserName'] = user_data.get('username', config.get('twitterUserName', ''))
                config['twitterDisplayName'] = user_data.get('name', config.get('twitterDisplayName', ''))
                if user_data.get('profile_image_url'):
                    config['avatarUrl'] = str(user_data['profile_image_url'])
            expires_in = refreshed.get('expires_in')
            if expires_in:
                config['accessTokenExpiresAt'] = (
                    datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
                ).isoformat(timespec='seconds')
            updated = profile_registry.update_account(
                account_id,
                config=config,
                auth_type='oauth',
                status=1,
                db_path=db_path,
            )
            summary = f"Twitter refreshed: @{config.get('twitterUserName') or updated.account_name}"
        else:
            raise ValueError('Refresh is implemented only for TikTok, Reddit, YouTube, Threads, Facebook, Instagram, and Twitter')

        account_events.record_event(
            account_id=updated.id,
            profile_id=updated.profile_id,
            platform=updated.platform,
            account_name=updated.account_name,
            action='refresh_token',
            status='ok',
            summary=summary,
            metadata={'timestamp': now, 'mode': mode},
            db_path=db_path,
        )
        return updated
    except Exception as exc:
        if account.platform == profile_registry.PLATFORM_TIKTOK:
            _append_tiktok_review_event(
                'refresh',
                {'status': 'error', 'mode': mode, 'accountId': account_id, 'accountName': account.account_name, 'error': str(exc)},
                account_id=account_id,
                account_name=account.account_name,
                status='error',
                metadata={'mode': mode},
                db_path=db_path,
            )
        account_events.record_event(
            account_id=account.id,
            profile_id=account.profile_id,
            platform=account.platform,
            account_name=account.account_name,
            action='refresh_token',
            status='error',
            summary='Token refresh failed',
            error_text=str(exc),
            metadata={'timestamp': now, 'mode': mode},
            db_path=db_path,
        )
        raise


def _batch_account_operation(*, account_ids: list[int], db_path: Path, operation: str) -> dict:
    results = []
    succeeded = 0
    failed = 0
    updated_accounts = []

    for account_id in account_ids:
        try:
            updated = (
                _run_account_connection_check(account_id=account_id, db_path=db_path)
                if operation == 'check'
                else _run_account_token_refresh(account_id=account_id, db_path=db_path)
            )
            payload = _account_payload(updated)
            updated_accounts.append(payload)
            results.append({
                'accountId': account_id,
                'platform': updated.platform,
                'accountName': updated.account_name,
                'status': 'ok',
                'account': payload,
            })
            succeeded += 1
        except LookupError:
            failed += 1
            results.append({'accountId': account_id, 'status': 'error', 'error': 'Account not found'})
        except Exception as exc:  # noqa: BLE001
            failed += 1
            results.append({'accountId': account_id, 'status': 'error', 'error': str(exc)})

    return {
        'operation': operation,
        'total': len(account_ids),
        'succeeded': succeeded,
        'failed': failed,
        'results': results,
        'accounts': updated_accounts,
    }


_REFRESHABLE_PLATFORMS = (
    profile_registry.PLATFORM_TIKTOK,
    profile_registry.PLATFORM_REDDIT,
    profile_registry.PLATFORM_YOUTUBE,
    profile_registry.PLATFORM_THREADS,
    profile_registry.PLATFORM_FACEBOOK,
    profile_registry.PLATFORM_INSTAGRAM,
    profile_registry.PLATFORM_TWITTER,
)


def _is_refreshable_account_stale(account: profile_registry.Account, *, skew_seconds: int = 300) -> bool:
    config = dict(account.config or {})
    if account.platform == profile_registry.PLATFORM_TIKTOK:
        return prepared_publishers._is_tiktok_access_token_stale(config, skew_seconds=skew_seconds)
    if account.platform in {profile_registry.PLATFORM_FACEBOOK, profile_registry.PLATFORM_INSTAGRAM}:
        meta_user_access_token = str(config.get('metaUserAccessToken') or '').strip()
        if not meta_user_access_token:
            return False
        access_token = str(config.get('accessToken') or '').strip()
        if not access_token:
            return True
        expires_at = prepared_publishers._parse_iso_datetime(str(config.get('metaUserAccessTokenExpiresAt') or config.get('accessTokenExpiresAt') or ''))
        if expires_at is None:
            return False
        return expires_at <= (prepared_publishers._utc_now() + timedelta(seconds=skew_seconds))
    # Cookie-based accounts don't have API tokens to refresh
    auth_type = str(config.get("twitterAuthType") or account.auth_type or "").strip().lower()
    if account.platform == profile_registry.PLATFORM_TWITTER and auth_type == "cookie":
        return False
    access_token = str(config.get('accessToken') or '').strip()
    if not access_token:
        return True
    expires_at = prepared_publishers._parse_iso_datetime(str(config.get('accessTokenExpiresAt') or ''))
    if expires_at is None:
        return False
    return expires_at <= (prepared_publishers._utc_now() + timedelta(seconds=skew_seconds))


def _run_refreshable_account_maintenance(
    *,
    db_path: Path,
    dry_run: bool = False,
    expiring_within_seconds: int = 300,
    max_accounts: int = 50,
    profile_id: int | None = None,
    account_ids: list[int] | None = None,
    platforms: list[str] | None = None,
    enabled_only: bool = True,
    mode: str = 'auto',
) -> dict:
    accounts = profile_registry.list_accounts(
        profile_id=profile_id,
        enabled=True if enabled_only else None,
        db_path=db_path,
    )
    allowed_platforms = {platform.lower() for platform in (platforms or _REFRESHABLE_PLATFORMS)}
    allowed_platforms &= set(_REFRESHABLE_PLATFORMS)
    if account_ids is not None:
        allowed_ids = set(account_ids)
        accounts = [account for account in accounts if account.id in allowed_ids]
    accounts = [account for account in accounts if account.platform in allowed_platforms]

    results = []
    updated_accounts = []
    refreshed = 0
    stale = 0
    skipped = 0
    examined = 0

    for account in accounts[:max_accounts]:
        examined += 1
        config = dict(account.config or {})
        is_stale = _is_refreshable_account_stale(account, skew_seconds=expiring_within_seconds)
        if not is_stale:
            skipped += 1
            results.append({'accountId': account.id, 'platform': account.platform, 'accountName': account.account_name, 'status': 'up_to_date'})
            continue
        stale += 1
        if account.platform == profile_registry.PLATFORM_TIKTOK and not str(config.get('refreshToken') or '').strip():
            skipped += 1
            results.append({'accountId': account.id, 'platform': account.platform, 'accountName': account.account_name, 'status': 'missing_refresh_token'})
            continue
        if dry_run:
            results.append({'accountId': account.id, 'platform': account.platform, 'accountName': account.account_name, 'status': 'would_refresh'})
            continue
        try:
            updated = _run_account_token_refresh(account_id=account.id, db_path=db_path, mode=mode)
            refreshed += 1
            updated_accounts.append(_account_payload(updated))
            results.append({'accountId': updated.id, 'platform': updated.platform, 'accountName': updated.account_name, 'status': 'refreshed'})
        except Exception as exc:  # noqa: BLE001
            skipped += 1
            results.append({'accountId': account.id, 'platform': account.platform, 'accountName': account.account_name, 'status': 'error', 'error': str(exc)})

    return {
        'dryRun': dry_run,
        'mode': mode,
        'examined': examined,
        'stale': stale,
        'refreshed': refreshed,
        'skipped': skipped,
        'results': results,
        'accounts': updated_accounts,
    }


def _account_maintenance_loop(interval_seconds: int, *, expiring_within_seconds: int, max_accounts: int) -> None:
    while True:
        time.sleep(interval_seconds)
        db_path = _current_db_path()
        _ACCOUNT_MAINTENANCE_STATE['running'] = True
        _ACCOUNT_MAINTENANCE_STATE['lastStartedAt'] = datetime.now().isoformat(timespec='seconds')
        _ACCOUNT_MAINTENANCE_STATE['lastError'] = None
        try:
            result = _run_refreshable_account_maintenance(
                db_path=db_path,
                dry_run=False,
                expiring_within_seconds=expiring_within_seconds,
                max_accounts=max_accounts,
                mode='auto',
            )
            _ACCOUNT_MAINTENANCE_STATE['lastResult'] = result
        except Exception as exc:  # noqa: BLE001
            _ACCOUNT_MAINTENANCE_STATE['lastError'] = str(exc)
        finally:
            _ACCOUNT_MAINTENANCE_STATE['running'] = False
            _ACCOUNT_MAINTENANCE_STATE['lastFinishedAt'] = datetime.now().isoformat(timespec='seconds')


def _maybe_start_account_maintenance_scheduler() -> None:
    global _ACCOUNT_MAINTENANCE_THREAD
    if _ACCOUNT_MAINTENANCE_THREAD is not None:
        return
    try:
        interval_seconds = int(os.environ.get('SAU_ACCOUNT_MAINTENANCE_INTERVAL_SECONDS', '0') or '0')
    except ValueError:
        interval_seconds = 0
    if interval_seconds <= 0:
        return
    try:
        expiring_within_seconds = int(os.environ.get('SAU_ACCOUNT_MAINTENANCE_EXPIRING_WITHIN_SECONDS', '300') or '300')
    except ValueError:
        expiring_within_seconds = 300
    try:
        max_accounts = int(os.environ.get('SAU_ACCOUNT_MAINTENANCE_MAX_ACCOUNTS', '50') or '50')
    except ValueError:
        max_accounts = 50
    _ACCOUNT_MAINTENANCE_STATE['enabled'] = True
    _ACCOUNT_MAINTENANCE_STATE['intervalSeconds'] = interval_seconds
    _ACCOUNT_MAINTENANCE_THREAD = threading.Thread(
        target=_account_maintenance_loop,
        args=(interval_seconds,),
        kwargs={'expiring_within_seconds': expiring_within_seconds, 'max_accounts': max_accounts},
        daemon=True,
        name='account-maintenance-scheduler',
    )
    _ACCOUNT_MAINTENANCE_THREAD.start()


def _validate_account_payload(data: dict, *, db_path: Path, profile_id: int | None = None, perform_live_checks: bool = False):
    platform = str(data.get("platform", "") or "").strip().lower()
    # Default to 'oauth' for platforms that support it, 'cookie' otherwise
    _default_auth = "oauth" if (platform and profile_registry.platform_defaults_to_oauth(platform)) else "cookie"
    auth_type = str(data.get("authType", _default_auth) or _default_auth)
    config = data.get("config") if isinstance(data.get("config"), dict) else {}
    cookie_path = str(data.get("cookiePath", "") or "")
    return account_validation.validate_structured_account_config(
        platform=platform,
        auth_type=auth_type,
        config=config,
        cookie_path=cookie_path,
        profile_settings=_account_profile_settings(profile_id, db_path=db_path),
        perform_live_checks=perform_live_checks,
    )


def _load_file_record(file_record_id: int, *, db_path: Path) -> dict:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM file_records WHERE id = ?",
            (file_record_id,),
        ).fetchone()
    if row is None:
        raise LookupError(f"File record not found: id={file_record_id}")
    return {key: row[key] for key in row.keys()}


def _load_default_storage_backend(*, db_path: Path) -> dict | None:
    """Load the default storage_backends row, or None if not configured."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM storage_backends WHERE is_default = 1 AND enabled = 1"
        ).fetchone()
    return dict(row) if row else None


def _get_cdn_url_for_file(filename: str) -> str | None:
    """Look up a file's CDN URL from file_records. Returns None if not found."""
    try:
        db_path = _ensure_legacy_db_ready()
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT storage_cdn_url, storage_key, storage_backend_id FROM file_records WHERE file_path = ?",
                (filename,),
            ).fetchone()
        if not row:
            return None
        if row["storage_cdn_url"]:
            return row["storage_cdn_url"]
        if row["storage_key"] and row["storage_backend_id"]:
            backend = _load_storage_backend_by_id(row["storage_backend_id"], db_path=db_path)
            if backend:
                from myUtils.do_spaces import client_from_row
                client = client_from_row(backend)
                return client.cdn_url_for(row["storage_key"])
    except Exception:
        pass
    return None


def _get_cdn_url_for_file_by_key(filename: str) -> str | None:
    """Look up a file's CDN URL by matching storage_key ending with filename."""
    try:
        db_path = _ensure_legacy_db_ready()
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT storage_cdn_url, storage_key FROM file_records WHERE storage_key LIKE ?",
                (f"%{filename}",),
            ).fetchone()
        if row and row["storage_cdn_url"]:
            return row["storage_cdn_url"]
    except Exception:
        pass
    return None


def _load_storage_backend_by_id(backend_id: int, *, db_path: Path) -> dict | None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM storage_backends WHERE id = ?", (backend_id,)
        ).fetchone()
    return dict(row) if row else None


def _upload_file_to_storage(file_path: Path, final_filename: str, file_record_id: int, *, db_path: Path) -> None:
    """Upload a file to the default storage backend in the background."""
    try:
        backend_row = _load_default_storage_backend(db_path=db_path)
        if not backend_row:
            return
        from myUtils.do_spaces import client_from_row
        from datetime import datetime
        client = client_from_row(backend_row)
        now = datetime.utcnow()
        storage_key = f"uploads/{now:%Y/%m}/{final_filename}"
        content_type = ""
        ext = file_path.suffix.lower()
        if ext in (".mp4", ".mov", ".avi", ".mkv"):
            content_type = "video/mp4"
        elif ext in (".jpg", ".jpeg"):
            content_type = "image/jpeg"
        elif ext in (".png",):
            content_type = "image/png"
        elif ext in (".webp",):
            content_type = "image/webp"
        cdn = client.upload_file(file_path, storage_key, content_type)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE file_records SET storage_backend_id = ?, storage_key = ?, storage_cdn_url = ? WHERE id = ?",
                (backend_row["id"], storage_key, cdn, file_record_id),
            )
            conn.commit()
        logger.info("Uploaded %s to storage: %s", final_filename, storage_key)
    except Exception:
        logger.exception("Failed to upload %s to storage", final_filename)


def _download_file_from_storage(file_path: str, *, db_path: Path) -> Path | None:
    """Download a file from storage if it has a storage_key or CDN URL. Returns local path or None."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT storage_key, storage_backend_id, storage_cdn_url FROM file_records WHERE file_path = ?",
            (file_path,),
        ).fetchone()
    if not row:
        return None

    local_path = _resolve_video_file_path_safely(file_path)
    if not local_path:
        # Try resolving under uploads/ directory for DO Spaces keys
        uploads_base = (Path(BASE_DIR) / "uploads").resolve()
        try:
            resolved = (uploads_base / Path(file_path).name).resolve()
            if resolved.is_relative_to(uploads_base):
                local_path = resolved
        except (ValueError, OSError):
            pass
    if not local_path:
        logger.warning("Refusing to download to unsafe path: %s", file_path)
        return None
    if local_path.exists():
        return local_path

    local_path.parent.mkdir(parents=True, exist_ok=True)

    # Try 1: Download via storage backend client
    if row["storage_key"] and row["storage_backend_id"]:
        try:
            backend_row = _load_storage_backend_by_id(row["storage_backend_id"], db_path=db_path)
            if backend_row:
                from myUtils.do_spaces import client_from_row
                client = client_from_row(backend_row)
                client.download_file(row["storage_key"], local_path)
                return local_path
        except Exception:
            logger.exception("Failed to download %s from storage backend", file_path)

    # Try 2: Download via public CDN URL
    if row["storage_cdn_url"]:
        try:
            import requests as _requests
            resp = _requests.get(row["storage_cdn_url"], timeout=60)
            resp.raise_for_status()
            local_path.write_bytes(resp.content)
            return local_path
        except Exception:
            logger.exception("Failed to download %s from CDN", file_path)

    return None


def _delete_file_from_storage(storage_key: str, backend_id: int, *, db_path: Path) -> None:
    """Best-effort delete a file from remote storage."""
    try:
        backend_row = _load_storage_backend_by_id(backend_id, db_path=db_path)
        if not backend_row:
            return
        from myUtils.do_spaces import client_from_row
        client = client_from_row(backend_row)
        client.delete_object(storage_key)
    except Exception:
        logger.warning("Failed to delete %s from storage", storage_key)


def _cleanup_local_files(*, db_path: Path, max_age_hours: int = 24) -> int:
    """Remove local copies of files that have been uploaded to remote storage."""
    removed = 0
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT id, file_path FROM file_records
                WHERE storage_key IS NOT NULL
                  AND local_cleaned_at IS NULL
                  AND upload_time < datetime('now', ?)
            """, (f"-{max_age_hours} hours",)).fetchall()
            for row in rows:
                local = _resolve_video_file_path_safely(row["file_path"])
                if local and local.exists():
                    try:
                        local.unlink()
                        removed += 1
                    except Exception:
                        continue
                conn.execute(
                    "UPDATE file_records SET local_cleaned_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (row["id"],),
                )
            conn.commit()
    except Exception:
        logger.exception("Local file cleanup failed")
    if removed:
        logger.info("Cleaned up %d local files already in storage", removed)
    return removed


def _ensure_file_record_for_path(file_path: str, *, db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM file_records WHERE file_path = ?",
            (file_path,),
        ).fetchone()
        if row is not None:
            return int(row[0])

        absolute_path = _resolve_video_file_path_safely(file_path)
        filename = Path(file_path).name
        filesize = None
        if absolute_path and absolute_path.exists():
            filesize = round(float(absolute_path.stat().st_size) / (1024 * 1024), 2)
        cursor = conn.execute(
            """
            INSERT INTO file_records (filename, filesize, file_path)
            VALUES (?, ?, ?)
            """,
            (filename, filesize, file_path),
        )
        conn.commit()
        return int(cursor.lastrowid)


def _load_media_group_files(media_group_id: int, *, db_path: Path) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                mgi.id,
                mgi.media_group_id,
                mgi.file_record_id,
                mgi.role,
                mgi.sort_order,
                fr.filename,
                fr.file_path,
                fr.filesize
            FROM media_group_items AS mgi
            JOIN file_records AS fr ON fr.id = mgi.file_record_id
            WHERE mgi.media_group_id = ?
            ORDER BY mgi.sort_order, mgi.id
            """,
            (media_group_id,),
        ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def _is_image_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_SUFFIXES


def _is_video_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in VIDEO_SUFFIXES


def _derive_watermark_spec(profile: profile_registry.Profile, data: dict) -> dict:
    watermark = data.get("watermark")
    if watermark is None:
        watermark = (profile.settings or {}).get("watermark")
    if isinstance(watermark, str) and watermark.strip():
        return {"text": watermark.strip(), "style": "static", "position": "random", "angle": -30, "opacity": 0.5, "fontSize": 24, "color": "white"}
    if isinstance(watermark, dict):
        spec = dict(watermark)
        spec.setdefault("style", "static")
        spec.setdefault("position", "random")
        spec.setdefault("angle", -30)
        spec.setdefault("opacity", 0.5)
        spec.setdefault("fontSize", 24)
        spec.setdefault("color", "white")
        return spec
    return {}


def _resolve_file_record_path(file_record_id: int, db_path: Path) -> str | None:
    """Look up a file_record's file_path by ID."""
    import sqlite3
    conn = None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT file_path FROM file_records WHERE id = ?", (file_record_id,)
        ).fetchone()
        if row:
            return row["file_path"]
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()
    return None


def _prepare_campaign_media_artifacts(
    campaign_id: int,
    profile: profile_registry.Profile,
    media_files: list[dict],
    request_data: dict,
    *,
    selected_platforms: set[str] | None = None,
    db_path: Path,
) -> dict:
    watermark_spec = _derive_watermark_spec(profile, request_data)
    selected_platforms = set(selected_platforms or set())
    tiktok_only = selected_platforms == {profile_registry.PLATFORM_TIKTOK}
    # Remote-host media only when a URL-fetch platform is targeted (TikTok/
    # Meta/Threads pull media by URL). An explicit uploadToRemote overrides;
    # otherwise enable automatically when such a platform is present and a
    # public storage backend (share/DO Spaces/rclone) is configured.
    needs_public_url = bool(selected_platforms & _REMOTE_URL_PLATFORMS)
    _explicit_remote = request_data.get("uploadToRemote")
    if _explicit_remote is not None:
        upload_to_remote = bool(_explicit_remote)
    else:
        upload_to_remote = needs_public_url and media_remote_storage.is_any_backend_configured()

    # Resolve intro/outro file paths from profile settings
    profile_settings = profile.settings or {}
    intro_ids = request_data.get("intros") or profile_settings.get("intros") or []
    outro_ids = request_data.get("outros") or profile_settings.get("outros") or []
    intro_paths = []
    outro_paths = []
    def _resolve_aux_path(file_record_id: int) -> str | None:
        raw = _resolve_file_record_path(int(file_record_id), db_path)
        if not raw:
            return None
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            videofile_candidate = Path(BASE_DIR) / "videoFile" / candidate
            if videofile_candidate.exists():
                candidate = videofile_candidate
            else:
                # Try downloading from remote storage
                downloaded = _download_file_from_storage(raw, db_path=db_path)
                if downloaded:
                    candidate = downloaded
        return str(candidate.resolve())
    for fid in intro_ids:
        p = _resolve_aux_path(fid)
        if p:
            intro_paths.append(p)
    for fid in outro_ids:
        p = _resolve_aux_path(fid)
        if p:
            outro_paths.append(p)

    artifacts_context = {
        "imageUrls": [],
        "videoUrl": "",
        "imageLocalPaths": [],
        "videoLocalPath": "",
        "rawImageUrls": [],
        "rawVideoUrl": "",
        "rawImageLocalPaths": [],
        "rawVideoLocalPath": "",
        "screenshotPaths": [],
        "screenshotUrls": [],
        "transcriptText": str(request_data.get("transcriptText", "") or "").strip(),
    }

    screenshots_spec = request_data.get("screenshots") if isinstance(request_data.get("screenshots"), dict) else {}
    screenshots_enabled = bool(screenshots_spec.get("enabled"))
    screenshots_count = int(screenshots_spec.get("count") or 0) if screenshots_enabled else 0
    screenshots_timestamps = screenshots_spec.get("timestamps") if isinstance(screenshots_spec.get("timestamps"), list) else None

    for media_file in media_files:
        raw_path = Path(media_file["file_path"]).expanduser()
        if not raw_path.is_absolute():
            # file_records stores filenames relative to videoFile/ (the
            # /upload endpoint writes there). Resolve to the canonical
            # absolute location so downstream ffmpeg / Pillow calls work
            # regardless of the worker's current working directory.
            candidate = Path(BASE_DIR) / "videoFile" / raw_path
            if candidate.exists():
                raw_path = candidate
            else:
                # Try downloading from remote storage
                downloaded = _download_file_from_storage(media_file["file_path"], db_path=db_path)
                if downloaded:
                    raw_path = downloaded
        source_path = raw_path.resolve()
        publish_path = source_path
        artifact_kind = None

        # Concat intro/outro for videos before watermarking
        if _is_video_file(source_path) and (intro_paths or outro_paths):
            concat_parts = intro_paths + [str(source_path)] + outro_paths
            concat_output = media_pipeline.prepare_campaign_artifact_path(
                campaign_id,
                source_path,
                artifact_kind="concat",
            )
            try:
                media_pipeline.concat_videos(concat_parts, concat_output)
                source_path = concat_output
                publish_path = concat_output
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Intro/outro concat failed for campaign %d, file %s: %s",
                    campaign_id, source_path, exc,
                )

        if watermark_spec and not tiktok_only and _is_image_file(source_path):
            artifact_kind = "watermarked_image"
            publish_path = media_pipeline.prepare_campaign_artifact_path(
                campaign_id,
                source_path,
                artifact_kind=artifact_kind,
            )
            media_pipeline.apply_image_watermark(
                source_path,
                publish_path,
                watermark_text=watermark_spec.get("text"),
                watermark_image_path=watermark_spec.get("imagePath"),
                seed=campaign_id * 1000 + int(media_file["file_record_id"]),
                opacity=int(float(watermark_spec.get("opacity", 0.5)) * 255),
                style=watermark_spec.get("style", "static"),
                angle=float(watermark_spec.get("angle", -30)),
                color=watermark_spec.get("color", "white"),
            )
        elif watermark_spec and not tiktok_only and _is_video_file(source_path):
            artifact_kind = "watermarked_video"
            publish_path = media_pipeline.prepare_campaign_artifact_path(
                campaign_id,
                source_path,
                artifact_kind=artifact_kind,
            )
            media_pipeline.apply_video_watermark(
                source_path,
                publish_path,
                watermark_text=watermark_spec.get("text"),
                watermark_image_path=watermark_spec.get("imagePath"),
                seed=campaign_id * 1000 + int(media_file["file_record_id"]),
                duration_seconds=request_data.get("durationSeconds"),
                style=watermark_spec.get("style", "static"),
                angle=float(watermark_spec.get("angle", -30)) * 3.14159 / 180,
                opacity=float(watermark_spec.get("opacity", 0.5)),
                fontsize=int(watermark_spec.get("fontSize", 24)),
                color=watermark_spec.get("color", "white"),
            )

        if artifact_kind is not None:
            campaign_store.add_campaign_artifact(
                campaign_id,
                source_file_record_id=media_file["file_record_id"],
                artifact_kind=artifact_kind,
                local_path=str(publish_path),
                metadata={"role": media_file["role"]},
                db_path=db_path,
            )

        if screenshots_enabled and _is_video_file(publish_path):
            try:
                shots_dir = media_pipeline.build_campaign_workspace(campaign_id) / "screenshots"
                shots = media_pipeline.extract_video_screenshots(
                    publish_path,
                    shots_dir,
                    count=screenshots_count if not screenshots_timestamps else None,
                    timestamps=screenshots_timestamps,
                    seed=campaign_id * 1000 + int(media_file["file_record_id"]),
                )
                for shot_path in shots:
                    artifacts_context["screenshotPaths"].append(str(shot_path))
                    public_shot_url = None
                    if upload_to_remote:
                        try:
                            remote_shot = media_remote_storage.upload_artifact(
                                shot_path,
                                campaign_id=campaign_id,
                                artifact_subdir="screenshots",
                            )
                            public_shot_url = remote_shot.public_url
                            if public_shot_url:
                                artifacts_context["screenshotUrls"].append(public_shot_url)
                        except Exception as exc:  # noqa: BLE001
                            logging.getLogger(__name__).warning(
                                "Screenshot remote upload failed for campaign %d: %s",
                                campaign_id, exc,
                            )
                    campaign_store.add_campaign_artifact(
                        campaign_id,
                        source_file_record_id=media_file["file_record_id"],
                        artifact_kind="screenshot",
                        local_path=str(shot_path),
                        public_url=public_shot_url,
                        metadata={
                            "role": media_file["role"],
                            "source_video": str(publish_path),
                        },
                        db_path=db_path,
                    )
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning(
                    "Screenshot extraction failed for campaign %d, file %s: %s",
                    campaign_id, publish_path, exc,
                )

        public_url = None
        raw_public_url = None
        if upload_to_remote:
            remote_source = source_path if tiktok_only else publish_path
            remote_kind = "raw_remote_upload" if tiktok_only else "remote_upload"
            remote_artifact = media_remote_storage.upload_artifact(
                remote_source,
                campaign_id=campaign_id,
                artifact_subdir="videos" if _is_video_file(remote_source) else "images",
            )
            public_url = remote_artifact.public_url
            campaign_store.add_campaign_artifact(
                campaign_id,
                source_file_record_id=media_file["file_record_id"],
                artifact_kind=remote_kind,
                local_path=str(remote_source),
                public_url=remote_artifact.public_url,
                remote_path=remote_artifact.remote_path,
                metadata={"role": media_file["role"]},
                db_path=db_path,
            )
            if not tiktok_only and publish_path != source_path:
                raw_remote_artifact = media_remote_storage.upload_artifact(
                    source_path,
                    campaign_id=campaign_id,
                    artifact_subdir="videos" if _is_video_file(source_path) else "images",
                )
                raw_public_url = raw_remote_artifact.public_url
                campaign_store.add_campaign_artifact(
                    campaign_id,
                    source_file_record_id=media_file["file_record_id"],
                    artifact_kind="raw_remote_upload",
                    local_path=str(source_path),
                    public_url=raw_remote_artifact.public_url,
                    remote_path=raw_remote_artifact.remote_path,
                    metadata={"role": media_file["role"]},
                    db_path=db_path,
                )
        else:
            # No remote upload. Cookie/byte-upload platforms use the local file
            # directly; only emit a /getFile URL when it would actually be
            # reachable. Never hand a URL-fetch platform an unreachable
            # localhost URL (that guarantees a downstream 4xx) — leave it None
            # so the publisher raises a clear "no public media URL" error.
            public_url = None
            try:
                from flask import request as _flask_request
                base_url = _flask_request.host_url.rstrip("/")
                served_filename = Path(publish_path).name
                candidate = f"{base_url}/getFile?filename={served_filename}"
                _is_public = (
                    base_url.startswith("https://")
                    and "localhost" not in base_url
                    and "127.0.0.1" not in base_url
                )
                if needs_public_url and not _is_public:
                    logging.getLogger(__name__).warning(
                        "campaign %d targets URL-fetch platforms but no public "
                        "storage backend produced a URL; suppressing unreachable %s",
                        campaign_id, candidate,
                    )
                else:
                    public_url = candidate
            except RuntimeError:
                public_url = None
            campaign_store.add_campaign_artifact(
                campaign_id,
                source_file_record_id=media_file["file_record_id"],
                artifact_kind="local",
                local_path=str(publish_path),
                public_url=public_url,
                metadata={"role": media_file["role"]},
                db_path=db_path,
            )
            # Also store a raw (un-watermarked) artifact for TikTok when
            # watermarks were applied in a mixed-platform batch.
            if not tiktok_only and publish_path != source_path and profile_registry.PLATFORM_TIKTOK in selected_platforms:
                try:
                    raw_served_filename = Path(source_path).name
                    raw_public_url = f"{base_url}/getFile?filename={raw_served_filename}"
                except (RuntimeError, NameError):
                    raw_public_url = None
                campaign_store.add_campaign_artifact(
                    campaign_id,
                    source_file_record_id=media_file["file_record_id"],
                    artifact_kind="raw_local",
                    local_path=str(source_path),
                    public_url=raw_public_url,
                    metadata={"role": media_file["role"]},
                    db_path=db_path,
                )

        if _is_image_file(publish_path):
            artifacts_context["imageLocalPaths"].append(str(publish_path))
            artifacts_context["rawImageLocalPaths"].append(str(source_path))
            if public_url:
                artifacts_context["imageUrls"].append(public_url)
            if raw_public_url:
                artifacts_context["rawImageUrls"].append(raw_public_url)
        elif _is_video_file(publish_path):
            artifacts_context["videoLocalPath"] = str(publish_path)
            artifacts_context["rawVideoLocalPath"] = str(source_path)
            if public_url:
                artifacts_context["videoUrl"] = public_url
            if raw_public_url:
                artifacts_context["rawVideoUrl"] = raw_public_url

    ai_config = _resolve_ai_config(profile)
    has_ai = bool(
        ai_config["api_base_url"] and ai_config["api_key"]
    ) or bool(
        os.environ.get("SAU_LLM_API_KEY") and os.environ.get("SAU_LLM_API_BASE_URL")
    )
    should_transcribe = bool(
        request_data.get("transcribe", False)
        or (not artifacts_context["transcriptText"] and has_ai)
    )
    primary_video = next((item for item in media_files if item["role"] == "video"), None)
    if should_transcribe and primary_video is not None and not artifacts_context["transcriptText"]:
        raw_video = Path(primary_video["file_path"]).expanduser()
        if not raw_video.is_absolute():
            candidate = Path(BASE_DIR) / "videoFile" / raw_video
            if candidate.exists():
                raw_video = candidate
        source_path = raw_video.resolve()
        try:
            audio_path = media_pipeline.prepare_campaign_artifact_path(
                campaign_id,
                source_path,
                artifact_kind="audio",
                suffix=".wav",
            )
            media_pipeline.extract_video_audio(source_path, audio_path)
            transcribe_kwargs = {}
            if ai_config["api_base_url"]:
                transcribe_kwargs["api_base_url"] = ai_config["api_base_url"]
            if ai_config["api_key"]:
                transcribe_kwargs["api_key"] = ai_config["api_key"]
            transcript = llm_client.transcribe_audio(audio_path, **transcribe_kwargs)
            transcript_path = media_pipeline.prepare_campaign_artifact_path(
                campaign_id,
                source_path,
                artifact_kind="transcript",
                suffix=".txt",
            )
            transcript_path.write_text(transcript.text, encoding="utf-8")
            artifacts_context["transcriptText"] = transcript.text
            campaign_store.add_campaign_artifact(
                campaign_id,
                source_file_record_id=primary_video["file_record_id"],
                artifact_kind="audio",
                local_path=str(audio_path),
                db_path=db_path,
            )
            campaign_store.add_campaign_artifact(
                campaign_id,
                source_file_record_id=primary_video["file_record_id"],
                artifact_kind="transcript",
                local_path=str(transcript_path),
                metadata={"text": transcript.text},
                db_path=db_path,
            )
        except Exception as exc:  # noqa: BLE001
            # Transcription is optional context for the LLM. If the video
            # has no audio stream, or the transcription API rejects the
            # request, we still want the publish to go through.
            logging.getLogger(__name__).warning(
                "Audio extraction / transcription failed for campaign %d: %s",
                campaign_id, exc,
            )

    return artifacts_context


def _fallback_generated_draft(
    platform: str,
    media_group: media_group_store.MediaGroup,
    request_data: dict,
    media_context: dict,
) -> dict:
    headline = str(request_data.get("title") or media_group.name).strip()
    notes = str(request_data.get("notes", "") or "").strip()
    transcript = str(media_context.get("transcriptText", "") or "").strip()
    snippets = [part for part in (headline, notes, transcript[:500]) if part]
    message = "\n\n".join(snippets).strip() or media_group.name
    return {
        "message": message,
        "hashtags": request_data.get("hashtags") or [],
        "firstComment": str(request_data.get("firstComment", "") or "").strip(),
    }


def _build_generation_prompt(
    platform: str,
    profile: profile_registry.Profile,
    media_group: media_group_store.MediaGroup,
    request_data: dict,
    media_context: dict,
) -> tuple[str, str]:
    rule = content_rules.get_platform_rule(platform)
    system_prompt = str((profile.settings or {}).get("systemPrompt", "") or "").strip()
    if not system_prompt:
        system_prompt = "You write concise, platform-native social media copy."
    user_lines = [
        f"Platform: {platform}",
        f"Media group: {media_group.name}",
        f"Max chars: {rule.max_chars if rule.max_chars is not None else 'none'}",
        f"Required hashtag count: {rule.hashtag_count}",
        f"Require emoji: {rule.require_emoji}",
        f"Require contact details: {rule.require_contact_details}",
        f"Require CTA: {rule.require_cta}",
        f"Title: {request_data.get('title', '')}",
        f"Notes: {request_data.get('notes', '')}",
        f"Transcript: {media_context.get('transcriptText', '')}",
        f"Contact details: {request_data.get('contactDetails', '')}",
        f"CTA: {request_data.get('cta', '')}",
    ]
    account_context = str(request_data.get("_accountContext") or "").strip()
    if account_context:
        user_lines.append("")
        user_lines.append("Account context (apply any account-specific rules from the system prompt):")
        user_lines.append(account_context)
    user_lines.append("Return JSON with keys: message, hashtags, firstComment, contactDetails, cta.")
    return system_prompt, "\n".join(user_lines)


def _resolve_ai_config(profile: profile_registry.Profile) -> dict:
    """Resolve AI service config from profile settings. Returns None values if not configured; callers fall back to env vars."""
    ai_services = (profile.settings or {}).get("aiServices") or []
    if ai_services:
        svc = ai_services[0]  # Use first configured service
        return {
            "api_base_url": svc.get("apiBaseUrl") or None,
            "api_key": svc.get("apiKey") or None,
            "model": svc.get("model") or None,
        }
    return {"api_base_url": None, "api_key": None, "model": None}


def _generate_platform_draft(
    platform: str,
    profile: profile_registry.Profile,
    media_group: media_group_store.MediaGroup,
    request_data: dict,
    media_context: dict,
) -> dict:
    ai_config = _resolve_ai_config(profile)
    has_ai = bool(
        ai_config["api_base_url"] and ai_config["api_key"]
    ) or bool(
        os.environ.get("SAU_LLM_API_KEY") and os.environ.get("SAU_LLM_API_BASE_URL")
    )
    should_use_llm = bool(request_data.get("useLlm", True)) and has_ai

    raw_draft = None
    if should_use_llm:
        try:
            system_prompt, user_prompt = _build_generation_prompt(
                platform,
                profile,
                media_group,
                request_data,
                media_context,
            )
            kwargs = {}
            if ai_config["api_base_url"]:
                kwargs["api_base_url"] = ai_config["api_base_url"]
            if ai_config["api_key"]:
                kwargs["api_key"] = ai_config["api_key"]
            if ai_config["model"]:
                kwargs["model"] = ai_config["model"]
            result = llm_client.generate_chat_completion(system_prompt, user_prompt, **kwargs)
            raw_draft = result.parsed_json or {"message": result.content}
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "LLM generation failed for %s: %s",
                platform,
                exc,
            )

    if not isinstance(raw_draft, dict):
        raw_draft = _fallback_generated_draft(platform, media_group, request_data, media_context)

    return content_rules.prepare_platform_draft(
        platform,
        raw_draft,
        contact_details=str(request_data.get("contactDetails", "") or "").strip(),
        cta=str(request_data.get("cta", "") or "").strip(),
        default_hashtags=request_data.get("hashtags") or [],
    )


def _generate_account_draft(
    account: profile_registry.Account,
    profile: profile_registry.Profile,
    media_group: media_group_store.MediaGroup,
    request_data: dict,
    media_context: dict,
    *,
    regenerate: bool = False,
) -> dict:
    """Generate a draft for a specific account, honouring per-account
    rules the user may have written in the profile's system prompt.

    The user's system prompt may include a *Platform specification* (or
    *Account specification*) section that lists per-platform or per-account
    rules. We surface the account context to the LLM so it can apply those
    rules. When ``regenerate`` is true a small randomised nonce is appended
    to the user prompt so the LLM produces a fresh variation.

    The account context is passed via a side channel
    (``_accountContext``) that only the LLM prompt builder reads — never
    via ``notes``, since the fallback path echoes ``notes`` directly into
    the visible message body.
    """
    extended_data = dict(request_data or {})
    nonce_suffix = ""
    if regenerate:
        nonce_suffix = f"\nRegeneration nonce: {uuid.uuid4().hex[:8]}"

    account_context_lines = [
        f"Platform: {account.platform}",
        f"Account name: {account.account_name}",
        "If the system prompt contains a 'Platform specification' or "
        "'Account specification' section with rules for this account, follow them.",
    ]
    extended_data["_accountContext"] = "\n".join(account_context_lines) + nonce_suffix
    return _generate_platform_draft(
        account.platform,
        profile,
        media_group,
        extended_data,
        media_context,
    )


def _default_sheet_title(profile: profile_registry.Profile) -> str:
    return f"{datetime.now().strftime('%Y-%m-%d')}-{profile.slug}"


@app.route('/oauth/meta/start', methods=['POST'])
def meta_oauth_start():
    db_path = _current_db_path()
    try:
        data = _read_json_body()
        raw_account_id = data.get('accountId')
        if raw_account_id in (None, ''):
            raise ValueError('Please save the Meta account before connecting OAuth')
        account_id = int(raw_account_id)
        account = profile_registry.get_account(account_id, db_path=db_path)
        if account.platform not in {profile_registry.PLATFORM_FACEBOOK, profile_registry.PLATFORM_INSTAGRAM}:
            raise ValueError('OAuth connect is only available for Facebook and Instagram accounts on this route')
        state_token = meta_auth.build_state_token()
        redirect_uri = str(data.get('redirectUri') or _meta_callback_base_url() or meta_auth.default_redirect_uri()).strip()
        scopes = data.get('scopes') if isinstance(data.get('scopes'), list) and data.get('scopes') else list(meta_auth.default_scopes_for_platform(account.platform))
        authorize_url = meta_auth.build_authorize_url_from_env(
            state=state_token,
            redirect_uri=redirect_uri,
            scopes=tuple(str(scope) for scope in scopes),
        )
        meta_review.create_oauth_request(
            state_token=state_token,
            profile_id=account.profile_id,
            account_id=account.id,
            account_name=account.account_name,
            platform=account.platform,
            redirect_uri=redirect_uri,
            scopes=[str(scope) for scope in scopes],
            db_path=db_path,
        )
        account_events.record_event(
            account_id=account.id,
            profile_id=account.profile_id,
            platform=account.platform,
            account_name=account.account_name,
            action='oauth_start',
            status='ok',
            summary=f"{account.platform} OAuth flow started",
            metadata={'state': state_token, 'redirectUri': redirect_uri, 'scopes': scopes},
            db_path=db_path,
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({'code': 400, 'msg': str(exc), 'data': None}), 400
    return jsonify({'code': 200, 'msg': 'ok', 'data': {'authorizeUrl': authorize_url, 'state': state_token}}), 200


@app.route('/oauth/meta/callback', methods=['GET'])
def meta_oauth_callback():
    db_path = _current_db_path()
    state_token = str(request.args.get('state', '') or '')
    code = str(request.args.get('code', '') or '')
    error = str(request.args.get('error', '') or '')
    request_state = meta_review.get_oauth_request(state_token, db_path=db_path)
    if not request_state:
        return Response('Unknown Meta OAuth state', status=400, mimetype='text/plain')

    print(f"🔍 Meta callback: state={state_token[:12]}... status={request_state.status} account_id={request_state.account_id} platform={request_state.platform}")

    # Idempotency: if already completed (e.g. duplicate callback), return success
    if request_state.status in ('completed', 'pending_page_selection', 'pending_ig_selection'):
        # For picker statuses, re-render the picker HTML so the duplicate
        # callback doesn't kill the popup with a postMessage containing empty data.
        if request_state.status in ('pending_page_selection', 'pending_ig_selection'):
            result = {}
            try:
                result = json.loads(request_state.result_json or '{}')
            except Exception:
                pass
            picker_html = result.get('picker_html', '')
            if picker_html:
                return Response(picker_html, mimetype='text/html')
            # Fallback: just tell the user to use the existing picker
            return Response('<html><body><p style="font-family:sans-serif;padding:20px;text-align:center">Please make your selection in the previous window. You may close this tab.</p></body></html>', mimetype='text/html')
        result = {}
        try:
            result = json.loads(request_state.result_json or '{}')
        except Exception:
            pass
        # Remove picker_html from the result before sending to frontend
        result.pop('picker_html', None)
        html = f"""<html><body><script>
        if (window.opener) {{
          window.opener.postMessage({{ type: 'sau:meta-oauth', ok: true, data: {json.dumps(result, ensure_ascii=False)} }}, '*');
        }}
        window.close();
        </script><p>Already completed. You may close this window.</p></body></html>"""
        return Response(html, mimetype='text/html')

    if error:
        meta_review.complete_oauth_request(state_token, status='error', error_text=error, result={'state': state_token, 'error': error}, db_path=db_path)
        if request_state.account_id:
            account_events.record_event(
                account_id=request_state.account_id,
                profile_id=request_state.profile_id,
                platform=request_state.platform,
                account_name=request_state.account_name or '',
                action='oauth_callback',
                status='error',
                summary='Meta OAuth callback failed',
                error_text=error,
                metadata={'state': state_token},
                db_path=db_path,
            )
        return Response("""<html><body><script>
            if (window.opener) {
              window.opener.postMessage({ type: 'sau:meta-oauth', ok: false, error: %r }, '*');
            }
            window.close();
            </script><p>Meta authorization failed. You may close this window.</p></body></html>""" % error, mimetype='text/html')

    try:
        if not request_state.account_id:
            raise ValueError('Meta OAuth request is missing accountId')
        account = profile_registry.get_account(int(request_state.account_id), db_path=db_path)
        config = dict(account.config or {})
        token_payload = meta_auth.exchange_code_for_token(code=code, redirect_uri=request_state.redirect_uri)
        short_lived_access_token = str(token_payload.get('access_token') or '')
        try:
            long_lived_payload = meta_auth.exchange_for_long_lived_token(access_token=short_lived_access_token) if short_lived_access_token else {}
        except Exception as ll_exchange_err:
            print(f"⚠️ Meta long-lived token exchange failed: {ll_exchange_err}")
            long_lived_payload = {}
        user_access_token = str(long_lived_payload.get('access_token') or short_lived_access_token)
        print(f"🔍 Meta token exchange: short_lived={'present' if short_lived_access_token else 'MISSING'} long_lived={'present' if long_lived_payload.get('access_token') else 'FALLBACK_TO_SHORT'} expires_in={long_lived_payload.get('expires_in') or token_payload.get('expires_in') or 'NOT_SET'}")
        pages_payload = meta_auth.fetch_managed_pages(access_token=user_access_token) if user_access_token else {}
        pages = pages_payload.get('data', []) if isinstance(pages_payload, dict) else []
        if not isinstance(pages, list):
            pages = []
        selected_page = None
        if account.platform == profile_registry.PLATFORM_FACEBOOK:
            wanted_page_id = str(config.get('pageId') or '').strip()
            if wanted_page_id:
                selected_page = next((page for page in pages if str(page.get('id') or '') == wanted_page_id), None)
            if selected_page is None and len(pages) == 1:
                selected_page = pages[0]
            if selected_page is None and len(pages) > 1:
                savable_pages = [
                    {'id': str(p.get('id') or ''), 'name': str(p.get('name') or ''), 'access_token': str(p.get('access_token') or ''), 'pictureUrl': str(p.get('picture', {}).get('data', {}).get('url') or '')}
                    for p in pages if isinstance(p, dict)
                ]
                pages_json = json.dumps(savable_pages)
                html = f"""<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
            <style>body{{font-family:-apple-system,system-ui,sans-serif;padding:20px;max-width:560px;margin:0 auto;background:#f4f6f8}}
            h2{{text-align:center;color:#1d2129}}.card{{background:#fff;border:2px solid #e4e6eb;border-radius:12px;padding:16px;margin:10px 0;cursor:pointer;transition:all .15s}}
            .card:hover{{border-color:#1877f2;box-shadow:0 2px 8px rgba(24,119,242,.15)}}
            .card h3{{margin:0 0 4px;color:#1877f2}}.card p{{margin:0;color:#65676b;font-size:13px}}</style></head><body>
            <h2>{len(savable_pages)} pages available</h2><p style="text-align:center;color:#65676b">Select the page to connect:</p>
            <div id="cards"></div>
            <script>
            var PAGES = {pages_json};
            var TOKEN_DATA = {{userAccessToken: {json.dumps(user_access_token)}, expiresIn: {json.dumps(long_lived_payload.get('expires_in') or token_payload.get('expires_in') or '')}}};
            var ACCOUNT_ID = {request_state.account_id};
            var cards = document.getElementById('cards');
            PAGES.forEach(function(p) {{
              var card = document.createElement('div');
              card.className = 'card';
              card.innerHTML = '<h3>' + p.name + '</h3><p>ID ' + p.id + '</p>';
              card.onclick = function() {{ selectPage(p); }};
              cards.appendChild(card);
            }});
            function selectPage(p) {{
              if (window.opener) {{
                window.opener.postMessage({{type:'sau:meta-oauth',ok:true,data:{{platform:'facebook',accountId:ACCOUNT_ID,selectedPage:p,pages:PAGES,tokenData:TOKEN_DATA}}}}, '*');
              }}
              window.close();
            }}
            </script>
            </body></html>"""
                meta_review.complete_oauth_request(state_token, status='pending_page_selection', result={'available_page_count': len(savable_pages), 'picker_html': html}, db_path=db_path)
                return Response(html, mimetype='text/html')
            if selected_page is None and not pages:
                raise ValueError('Meta OAuth did not return any manageable Facebook Pages')
            merged_config = dict(config)
            merged_config['pageId'] = str(selected_page.get('id') or merged_config.get('pageId') or '')
            merged_config['facebookPageName'] = str(selected_page.get('name') or merged_config.get('facebookPageName') or '')
            merged_config['accessToken'] = str(selected_page.get('access_token') or user_access_token or '')
            # Fetch Facebook page profile picture
            try:
                import requests as _requests
                page_token = selected_page.get('access_token') or user_access_token
                pic_resp = _requests.get(f"{meta_auth.META_GRAPH_ROOT}/{merged_config['pageId']}", params={"fields": "picture.type(large)", "access_token": page_token}, timeout=15)
                pic_data = pic_resp.json()
                pic_url = pic_data.get("picture", {}).get("data", {}).get("url")
                if pic_url:
                    merged_config['avatarUrl'] = pic_url
            except Exception:
                pass
            merged_config['metaUserAccessToken'] = user_access_token
            now_utc = datetime.now(tz=timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')
            merged_config['accessTokenUpdatedAt'] = now_utc
            expires_in = long_lived_payload.get('expires_in') or token_payload.get('expires_in')
            if expires_in not in (None, ''):
                expiry = (datetime.now(tz=timezone.utc).replace(tzinfo=None) + timedelta(seconds=int(expires_in))).isoformat(timespec='seconds')
                merged_config['metaUserAccessTokenExpiresAt'] = expiry
                merged_config['accessTokenExpiresAt'] = expiry
            merged_config['connectedAt'] = merged_config.get('connectedAt') or now_utc
            updated = profile_registry.update_account(account.id, config=merged_config, auth_type='oauth', status=1, db_path=db_path)
            callback_payload = {
                'platform': updated.platform,
                'state': state_token,
                'status': 'ok',
                'accountId': updated.id,
                'accountName': updated.account_name,
                'pageId': merged_config.get('pageId', ''),
                'facebookPageName': merged_config.get('facebookPageName', ''),
                'accessToken': merged_config.get('accessToken', ''),
                'accessTokenUpdatedAt': merged_config.get('accessTokenUpdatedAt', ''),
                'connectedAt': merged_config.get('connectedAt', ''),
                'avatarUrl': merged_config.get('avatarUrl', ''),
            }
            persisted = {
                'platform': updated.platform,
                'state': state_token,
                'status': 'ok',
                'accountId': updated.id,
                'accountName': updated.account_name,
                'pageId': merged_config.get('pageId', ''),
                'facebookPageName': merged_config.get('facebookPageName', ''),
                'avatarUrl': merged_config.get('avatarUrl', ''),
            }
            summary = f"Facebook connected: {merged_config.get('facebookPageName') or updated.account_name}"
        else:
            # Collect all Instagram business accounts from pages
            ig_accounts = []
            for page in pages:
                ig_user = page.get('instagram_business_account') if isinstance(page, dict) else None
                if not isinstance(ig_user, dict):
                    continue
                ig_accounts.append({
                    'igUserId': str(ig_user.get('id') or ''),
                    'instagramUserName': str(ig_user.get('username') or ''),
                    'profilePictureUrl': str(ig_user.get('profile_picture_url') or ''),
                    'pageId': str(page.get('id') or ''),
                    'facebookPageName': str(page.get('name') or ''),
                    'pageAccessToken': str(page.get('access_token') or ''),
                })

            wanted_ig_user_id = str(config.get('igUserId') or '').strip()
            selected_ig = None
            if wanted_ig_user_id:
                selected_ig = next((ig for ig in ig_accounts if ig['igUserId'] == wanted_ig_user_id), None)
            if selected_ig is None and len(ig_accounts) == 1:
                selected_ig = ig_accounts[0]

            if selected_ig is None and len(ig_accounts) > 1:
                # Show picker page (same pattern as Facebook page selector)
                ig_json = json.dumps(ig_accounts)
                _picker_expires_in = long_lived_payload.get('expires_in') or token_payload.get('expires_in')
                _token_data_expires_json = json.dumps((datetime.now(tz=timezone.utc).replace(tzinfo=None) + timedelta(seconds=int(_picker_expires_in))).isoformat(timespec='seconds')) if _picker_expires_in else json.dumps('')
                html = f"""<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
            <style>body{{font-family:-apple-system,system-ui,sans-serif;padding:20px;max-width:560px;margin:0 auto;background:#f4f6f8}}
            h2{{text-align:center;color:#1d2129}}.card{{background:#fff;border:2px solid #e4e6eb;border-radius:12px;padding:16px;margin:10px 0;cursor:pointer;transition:all .15s}}
            .card:hover{{border-color:#E1306C;box-shadow:0 2px 8px rgba(225,48,108,.15)}}
            .card h3{{margin:0 0 4px;color:#E1306C}}.card p{{margin:0;color:#65676b;font-size:13px}}</style></head><body>
            <h2>{len(ig_accounts)} Instagram accounts available</h2><p style="text-align:center;color:#65676b">Select the Instagram account to connect:</p>
            <div id="cards"></div>
            <script>
            var IG_ACCOUNTS = {ig_json};
            var TOKEN_DATA = {{userAccessToken: {json.dumps(user_access_token)}, metaUserAccessTokenExpiresAt: {_token_data_expires_json}}};
            var ACCOUNT_ID = {request_state.account_id};
            var cards = document.getElementById('cards');
            IG_ACCOUNTS.forEach(function(ig) {{
              var card = document.createElement('div');
              card.className = 'card';
              card.innerHTML = '<h3>@' + ig.instagramUserName + '</h3><p>' + ig.facebookPageName + ' (Page ID: ' + ig.pageId + ')</p>';
              card.onclick = function() {{ selectIG(ig); }};
              cards.appendChild(card);
            }});
            function selectIG(ig) {{
              if (window.opener) {{
                window.opener.postMessage({{type:'sau:meta-oauth',ok:true,data:{{platform:'instagram',accountId:ACCOUNT_ID,selectedPage:{{id:ig.pageId,name:ig.facebookPageName,access_token:ig.pageAccessToken,igUserId:ig.igUserId,instagramUserName:ig.instagramUserName}},tokenData:TOKEN_DATA}}}}, '*');
              }}
              window.close();
            }}
            </script>
            </body></html>"""
                meta_review.complete_oauth_request(state_token, status='pending_ig_selection', result={'available_ig_count': len(ig_accounts), 'picker_html': html}, db_path=db_path)
                return Response(html, mimetype='text/html')

            if selected_ig is None:
                raise ValueError('Meta OAuth did not return a page-linked Instagram business account')

            merged_config = dict(config)
            merged_config['pageId'] = selected_ig['pageId']
            merged_config['facebookPageName'] = selected_ig['facebookPageName']
            merged_config['igUserId'] = selected_ig['igUserId']
            merged_config['instagramUserName'] = selected_ig['instagramUserName']
            if selected_ig.get('profilePictureUrl'):
                merged_config['avatarUrl'] = selected_ig['profilePictureUrl']
            merged_config['accessToken'] = selected_ig['pageAccessToken'] or user_access_token
            merged_config['metaUserAccessToken'] = user_access_token
            now_utc = datetime.now(tz=timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')
            merged_config['accessTokenUpdatedAt'] = now_utc
            expires_in = long_lived_payload.get('expires_in') or token_payload.get('expires_in')
            if expires_in not in (None, ''):
                expiry = (datetime.now(tz=timezone.utc).replace(tzinfo=None) + timedelta(seconds=int(expires_in))).isoformat(timespec='seconds')
                merged_config['metaUserAccessTokenExpiresAt'] = expiry
                merged_config['accessTokenExpiresAt'] = expiry
            merged_config['connectedAt'] = merged_config.get('connectedAt') or now_utc
            print(f"🔍 Meta callback IG: saving config for account {account.id}: igUserId={merged_config.get('igUserId')} accessToken={'present' if merged_config.get('accessToken') else 'MISSING'} keys={list(merged_config.keys())}")
            updated = profile_registry.update_account(account.id, config=merged_config, auth_type='oauth', status=1, db_path=db_path)
            print(f"✅ Meta callback IG: saved account {updated.id} status={updated.status} config_keys={list((updated.config or {}).keys())}")
            callback_payload = {
                'platform': updated.platform,
                'state': state_token,
                'status': 'ok',
                'accountId': updated.id,
                'accountName': updated.account_name,
                'pageId': merged_config.get('pageId', ''),
                'facebookPageName': merged_config.get('facebookPageName', ''),
                'igUserId': merged_config.get('igUserId', ''),
                'instagramUserName': merged_config.get('instagramUserName', ''),
                'accessToken': merged_config.get('accessToken', ''),
                'accessTokenUpdatedAt': merged_config.get('accessTokenUpdatedAt', ''),
                'connectedAt': merged_config.get('connectedAt', ''),
                'avatarUrl': merged_config.get('avatarUrl', ''),
            }
            persisted = {
                'platform': updated.platform,
                'state': state_token,
                'status': 'ok',
                'accountId': updated.id,
                'accountName': updated.account_name,
                'pageId': merged_config.get('pageId', ''),
                'facebookPageName': merged_config.get('facebookPageName', ''),
                'igUserId': merged_config.get('igUserId', ''),
                'instagramUserName': merged_config.get('instagramUserName', ''),
            }
            summary = f"Instagram connected: {merged_config.get('instagramUserName') or updated.account_name}"

        meta_review.complete_oauth_request(state_token, status='completed', result=persisted, db_path=db_path)
        account_events.record_event(
            account_id=updated.id,
            profile_id=updated.profile_id,
            platform=updated.platform,
            account_name=updated.account_name,
            action='oauth_callback',
            status='ok',
            summary=summary,
            metadata={'state': state_token},
            db_path=db_path,
        )
        html = f"""<html><body><script>
        if (window.opener) {{
          window.opener.postMessage({{ type: 'sau:meta-oauth', ok: true, data: {json.dumps(callback_payload, ensure_ascii=False)} }}, '*');
        }}
        window.close();
        </script><p>Meta authorization completed. You may close this window.</p></body></html>"""
        return Response(html, mimetype='text/html')
    except Exception as exc:  # noqa: BLE001
        meta_review.complete_oauth_request(state_token, status='error', error_text=str(exc), result={'state': state_token, 'error': str(exc)}, db_path=db_path)
        if request_state.account_id:
            account_events.record_event(
                account_id=request_state.account_id,
                profile_id=request_state.profile_id,
                platform=request_state.platform,
                account_name=request_state.account_name or '',
                action='oauth_callback',
                status='error',
                summary='Meta OAuth callback failed',
                error_text=str(exc),
                metadata={'state': state_token},
                db_path=db_path,
            )
        return Response(f"<html><body><p>Meta callback failed: {exc}</p></body></html>", status=500, mimetype='text/html')


@app.route('/oauth/youtube/start', methods=['POST'])
def youtube_oauth_start():
    db_path = _current_db_path()
    try:
        data = _read_json_body()
        raw_account_id = data.get('accountId')
        if raw_account_id in (None, ''):
            raise ValueError('Please save the YouTube account before connecting OAuth')
        account_id = int(raw_account_id)
        account = profile_registry.get_account(account_id, db_path=db_path)
        if account.platform != profile_registry.PLATFORM_YOUTUBE:
            raise ValueError('OAuth connect is only available for YouTube accounts on this route')
        state_token = youtube_auth.build_state_token()
        redirect_uri = str(data.get('redirectUri') or _youtube_callback_base_url() or youtube_auth.default_redirect_uri()).strip()
        scopes = data.get('scopes') if isinstance(data.get('scopes'), list) and data.get('scopes') else list(youtube_auth.DEFAULT_SCOPES)
        config = dict(account.config or {})
        client_id_env = str(config.get('clientIdEnv') or youtube_auth.CLIENT_ID_ENV)
        authorize_url = youtube_auth.build_authorize_url_from_env(
            state=state_token,
            redirect_uri=redirect_uri,
            scopes=tuple(str(scope) for scope in scopes),
            client_id_env=client_id_env,
        )
        youtube_review.create_oauth_request(
            state_token=state_token,
            profile_id=account.profile_id,
            account_id=account.id,
            account_name=account.account_name,
            redirect_uri=redirect_uri,
            scopes=[str(scope) for scope in scopes],
            db_path=db_path,
        )
        account_events.record_event(
            account_id=account.id,
            profile_id=account.profile_id,
            platform=account.platform,
            account_name=account.account_name,
            action='oauth_start',
            status='ok',
            summary='YouTube OAuth flow started',
            metadata={'state': state_token, 'redirectUri': redirect_uri, 'scopes': scopes},
            db_path=db_path,
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({'code': 400, 'msg': str(exc), 'data': None}), 400
    return jsonify({'code': 200, 'msg': 'ok', 'data': {'authorizeUrl': authorize_url, 'state': state_token}}), 200


@app.route('/oauth/youtube/callback', methods=['GET'])
def youtube_oauth_callback():
    db_path = _current_db_path()
    state_token = str(request.args.get('state', '') or '')
    code = str(request.args.get('code', '') or '')
    error = str(request.args.get('error', '') or '')
    request_state = youtube_review.get_oauth_request(state_token, db_path=db_path)
    if not request_state:
        return Response('Unknown YouTube OAuth state', status=400, mimetype='text/plain')

    if error:
        youtube_review.complete_oauth_request(state_token, status='error', error_text=error, result={'state': state_token, 'error': error}, db_path=db_path)
        if request_state.account_id:
            account_events.record_event(
                account_id=request_state.account_id,
                profile_id=request_state.profile_id,
                platform=profile_registry.PLATFORM_YOUTUBE,
                account_name=request_state.account_name or '',
                action='oauth_callback',
                status='error',
                summary='YouTube OAuth callback failed',
                error_text=error,
                metadata={'state': state_token},
                db_path=db_path,
            )
        return Response("""<html><body><script>
            if (window.opener) {
              window.opener.postMessage({ type: 'sau:youtube-oauth', ok: false, error: %r }, '*');
            }
            window.close();
            </script><p>YouTube authorization failed. You may close this window.</p></body></html>""" % error, mimetype='text/html')

    try:
        if not request_state.account_id:
            raise ValueError('YouTube OAuth request is missing accountId')
        account = profile_registry.get_account(int(request_state.account_id), db_path=db_path)
        config = dict(account.config or {})
        client_id_env = str(config.get('clientIdEnv') or youtube_auth.CLIENT_ID_ENV)
        client_secret_env = str(config.get('clientSecretEnv') or youtube_auth.CLIENT_SECRET_ENV)
        token_payload = youtube_auth.exchange_code_for_token(
            code=code,
            redirect_uri=request_state.redirect_uri,
            client_id_env=client_id_env,
            client_secret_env=client_secret_env,
        )
        access_token = str(token_payload.get('access_token') or '')
        refresh_token = str(token_payload.get('refresh_token') or '')
        channels = youtube_auth.fetch_my_channels(access_token=access_token) if access_token else {}
        items = channels.get('items', []) if isinstance(channels, dict) else []
        first = items[0] if items and isinstance(items[0], dict) else {}
        snippet = first.get('snippet', {}) if isinstance(first, dict) else {}
        merged_config = dict(config)
        if access_token:
            merged_config['accessToken'] = access_token
        if refresh_token:
            merged_config['refreshToken'] = refresh_token
        merged_config['accessTokenUpdatedAt'] = datetime.now().isoformat(timespec='seconds')
        expires_in = token_payload.get('expires_in')
        if expires_in not in (None, ''):
            merged_config['accessTokenExpiresAt'] = (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat(timespec='seconds')
        if first.get('id'):
            merged_config['channelId'] = first.get('id')
        if snippet.get('title'):
            merged_config['channelTitle'] = snippet.get('title')
        avatar_url = snippet.get('thumbnails', {}).get('default', {}).get('url')
        if avatar_url:
            merged_config['avatarUrl'] = avatar_url
        merged_config['scope'] = str(token_payload.get('scope') or merged_config.get('scope') or ' '.join(request_state.scopes))
        merged_config['connectedAt'] = merged_config.get('connectedAt') or datetime.now().isoformat(timespec='seconds')
        updated = profile_registry.update_account(account.id, config=merged_config, auth_type='oauth', status=1, db_path=db_path)
        callback_payload = {
            'state': state_token,
            'status': 'ok',
            'accountId': updated.id,
            'accountName': updated.account_name,
            'channelId': merged_config.get('channelId', ''),
            'channelTitle': merged_config.get('channelTitle', ''),
            'scope': merged_config.get('scope', ''),
            'accessTokenExpiresAt': merged_config.get('accessTokenExpiresAt', ''),
            'accessTokenUpdatedAt': merged_config.get('accessTokenUpdatedAt', ''),
            'connectedAt': merged_config.get('connectedAt', ''),
        }
        youtube_review.complete_oauth_request(
            state_token,
            status='completed',
            result={
                'state': state_token,
                'status': 'ok',
                'accountId': updated.id,
                'accountName': updated.account_name,
                'channelId': merged_config.get('channelId', ''),
                'channelTitle': merged_config.get('channelTitle', ''),
                'scope': merged_config.get('scope', ''),
            },
            db_path=db_path,
        )
        account_events.record_event(
            account_id=updated.id,
            profile_id=updated.profile_id,
            platform=updated.platform,
            account_name=updated.account_name,
            action='oauth_callback',
            status='ok',
            summary=f"YouTube connected: {merged_config.get('channelTitle') or updated.account_name}",
            metadata={'state': state_token, 'scope': merged_config.get('scope', '')},
            db_path=db_path,
        )
        html = f"""<html><body><script>
        if (window.opener) {{
          window.opener.postMessage({{ type: 'sau:youtube-oauth', ok: true, data: {json.dumps(callback_payload, ensure_ascii=False)} }}, '*');
        }}
        window.close();
        </script><p>YouTube authorization completed. You may close this window.</p></body></html>"""
        return Response(html, mimetype='text/html')
    except Exception as exc:  # noqa: BLE001
        youtube_review.complete_oauth_request(state_token, status='error', error_text=str(exc), result={'state': state_token, 'error': str(exc)}, db_path=db_path)
        if request_state.account_id:
            account_events.record_event(
                account_id=request_state.account_id,
                profile_id=request_state.profile_id,
                platform=profile_registry.PLATFORM_YOUTUBE,
                account_name=request_state.account_name or '',
                action='oauth_callback',
                status='error',
                summary='YouTube OAuth callback failed',
                error_text=str(exc),
                metadata={'state': state_token},
                db_path=db_path,
            )
        return Response(f"<html><body><p>YouTube callback failed: {exc}</p></body></html>", status=500, mimetype='text/html')


@app.route('/oauth/reddit/start', methods=['POST'])
def reddit_oauth_start():
    db_path = _current_db_path()
    try:
        data = _read_json_body()
        raw_account_id = data.get('accountId')
        if raw_account_id in (None, ''):
            raise ValueError('Please save the Reddit account before connecting OAuth')
        account_id = int(raw_account_id)
        account = profile_registry.get_account(account_id, db_path=db_path)
        if account.platform != profile_registry.PLATFORM_REDDIT:
            raise ValueError('OAuth connect is only available for Reddit accounts on this route')
        state_token = reddit_auth.build_state_token()
        redirect_uri = str(data.get('redirectUri') or _reddit_callback_base_url() or reddit_auth.default_redirect_uri()).strip()
        scopes = data.get('scopes') if isinstance(data.get('scopes'), list) and data.get('scopes') else list(reddit_auth.DEFAULT_SCOPES)
        config = dict(account.config or {})
        client_id_env = str(config.get('clientIdEnv') or reddit_auth.CLIENT_ID_ENV)
        authorize_url = reddit_auth.build_authorize_url_from_env(
            state=state_token,
            redirect_uri=redirect_uri,
            scopes=tuple(str(scope) for scope in scopes),
            client_id_env=client_id_env,
        )
        reddit_review.create_oauth_request(
            state_token=state_token,
            profile_id=account.profile_id,
            account_id=account.id,
            account_name=account.account_name,
            redirect_uri=redirect_uri,
            scopes=[str(scope) for scope in scopes],
            db_path=db_path,
        )
        account_events.record_event(
            account_id=account.id,
            profile_id=account.profile_id,
            platform=account.platform,
            account_name=account.account_name,
            action='oauth_start',
            status='ok',
            summary='Reddit OAuth flow started',
            metadata={'state': state_token, 'redirectUri': redirect_uri, 'scopes': scopes},
            db_path=db_path,
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({'code': 400, 'msg': str(exc), 'data': None}), 400
    return jsonify({'code': 200, 'msg': 'ok', 'data': {'authorizeUrl': authorize_url, 'state': state_token}}), 200


@app.route('/oauth/reddit/callback', methods=['GET'])
def reddit_oauth_callback():
    db_path = _current_db_path()
    state_token = str(request.args.get('state', '') or '')
    code = str(request.args.get('code', '') or '')
    error = str(request.args.get('error', '') or '')
    request_state = reddit_review.get_oauth_request(state_token, db_path=db_path)
    if not request_state:
        return Response('Unknown Reddit OAuth state', status=400, mimetype='text/plain')

    if error:
        reddit_review.complete_oauth_request(
            state_token,
            status='error',
            error_text=error,
            result={'state': state_token, 'error': error},
            db_path=db_path,
        )
        if request_state.account_id:
            account_events.record_event(
                account_id=request_state.account_id,
                profile_id=request_state.profile_id,
                platform=profile_registry.PLATFORM_REDDIT,
                account_name=request_state.account_name or '',
                action='oauth_callback',
                status='error',
                summary='Reddit OAuth callback failed',
                error_text=error,
                metadata={'state': state_token},
                db_path=db_path,
            )
        return Response(
            """<html><body><script>
            if (window.opener) {
              window.opener.postMessage({ type: 'sau:reddit-oauth', ok: false, error: %r }, '*');
            }
            window.close();
            </script><p>Reddit authorization failed. You may close this window.</p></body></html>""" % error,
            mimetype='text/html',
        )

    try:
        if not request_state.account_id:
            raise ValueError('Reddit OAuth request is missing accountId')
        account = profile_registry.get_account(int(request_state.account_id), db_path=db_path)
        config = dict(account.config or {})
        client_id_env = str(config.get('clientIdEnv') or reddit_auth.CLIENT_ID_ENV)
        client_secret_env = str(config.get('clientSecretEnv') or reddit_auth.CLIENT_SECRET_ENV)
        user_agent = str(config.get('userAgent') or '').strip() or None
        token_payload = reddit_auth.exchange_code_for_token(
            code=code,
            redirect_uri=request_state.redirect_uri,
            client_id_env=client_id_env,
            client_secret_env=client_secret_env,
            user_agent=user_agent,
        )
        access_token = str(token_payload.get('access_token') or '')
        refresh_token = str(token_payload.get('refresh_token') or '')
        user_info = reddit_auth.fetch_user_info(access_token=access_token, user_agent=user_agent) if access_token else {}
        merged_config = dict(config)
        if access_token:
            merged_config['accessToken'] = access_token
        if refresh_token:
            merged_config['refreshToken'] = refresh_token
        merged_config['accessTokenUpdatedAt'] = datetime.now().isoformat(timespec='seconds')
        expires_in = token_payload.get('expires_in')
        if expires_in not in (None, ''):
            merged_config['accessTokenExpiresAt'] = (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat(timespec='seconds')
        merged_config['redditUserName'] = str(user_info.get('name') or merged_config.get('redditUserName') or '')
        merged_config['scope'] = str(token_payload.get('scope') or merged_config.get('scope') or ' '.join(request_state.scopes))
        merged_config['connectedAt'] = merged_config.get('connectedAt') or datetime.now().isoformat(timespec='seconds')
        updated = profile_registry.update_account(
            account.id,
            config=merged_config,
            auth_type='oauth',
            db_path=db_path,
        )
        callback_payload = {
            'state': state_token,
            'status': 'ok',
            'accountId': updated.id,
            'accountName': updated.account_name,
            'redditUserName': merged_config.get('redditUserName', ''),
            'scope': merged_config.get('scope', ''),
            'accessTokenExpiresAt': merged_config.get('accessTokenExpiresAt', ''),
            'accessTokenUpdatedAt': merged_config.get('accessTokenUpdatedAt', ''),
            'connectedAt': merged_config.get('connectedAt', ''),
        }
        reddit_review.complete_oauth_request(
            state_token,
            status='completed',
            result={
                'state': state_token,
                'status': 'ok',
                'accountId': updated.id,
                'accountName': updated.account_name,
                'redditUserName': merged_config.get('redditUserName', ''),
                'scope': merged_config.get('scope', ''),
            },
            db_path=db_path,
        )
        account_events.record_event(
            account_id=updated.id,
            profile_id=updated.profile_id,
            platform=updated.platform,
            account_name=updated.account_name,
            action='oauth_callback',
            status='ok',
            summary=f"Reddit connected: {merged_config.get('redditUserName') or updated.account_name}",
            metadata={'state': state_token, 'scope': merged_config.get('scope', '')},
            db_path=db_path,
        )
        html = f"""<html><body><script>
        if (window.opener) {{
          window.opener.postMessage({{ type: 'sau:reddit-oauth', ok: true, data: {json.dumps(callback_payload, ensure_ascii=False)} }}, '*');
        }}
        window.close();
        </script><p>Reddit authorization completed. You may close this window.</p></body></html>"""
        return Response(html, mimetype='text/html')
    except Exception as exc:  # noqa: BLE001
        reddit_review.complete_oauth_request(
            state_token,
            status='error',
            error_text=str(exc),
            result={'state': state_token, 'error': str(exc)},
            db_path=db_path,
        )
        if request_state.account_id:
            account_events.record_event(
                account_id=request_state.account_id,
                profile_id=request_state.profile_id,
                platform=profile_registry.PLATFORM_REDDIT,
                account_name=request_state.account_name or '',
                action='oauth_callback',
                status='error',
                summary='Reddit OAuth callback failed',
                error_text=str(exc),
                metadata={'state': state_token},
                db_path=db_path,
            )
        return Response(f"<html><body><p>Reddit callback failed: {exc}</p></body></html>", status=500, mimetype='text/html')


@app.route('/oauth/twitter/start', methods=['POST'])
def twitter_oauth_start():
    db_path = _current_db_path()
    try:
        data = _read_json_body()
        raw_account_id = data.get('accountId')
        if raw_account_id in (None, ''):
            raise ValueError('Please save the Twitter account before connecting OAuth')
        account_id = int(raw_account_id)
        account = profile_registry.get_account(account_id, db_path=db_path)
        if account.platform != profile_registry.PLATFORM_TWITTER:
            raise ValueError('OAuth connect is only available for Twitter accounts on this route')
        state_token = x_auth.build_state_token()
        redirect_uri = str(data.get('redirectUri') or _twitter_callback_base_url() or x_auth.default_redirect_uri()).strip()
        scopes = data.get('scopes') if isinstance(data.get('scopes'), list) and data.get('scopes') else list(x_auth.DEFAULT_SCOPES)
        authorize_url, code_verifier, _ = x_auth.build_authorize_url_from_env(
            state=state_token,
            redirect_uri=redirect_uri,
            scopes=tuple(str(scope) for scope in scopes),
        )
        x_review.create_oauth_request(
            state_token=state_token,
            profile_id=account.profile_id,
            account_id=account.id,
            account_name=account.account_name,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
            scopes=[str(scope) for scope in scopes],
            db_path=db_path,
        )
        account_events.record_event(
            account_id=account.id,
            profile_id=account.profile_id,
            platform=account.platform,
            account_name=account.account_name,
            action='oauth_start',
            status='ok',
            summary='Twitter OAuth flow started',
            metadata={'state': state_token, 'redirectUri': redirect_uri, 'scopes': scopes},
            db_path=db_path,
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({'code': 400, 'msg': str(exc), 'data': None}), 400
    return jsonify({'code': 200, 'msg': 'ok', 'data': {'authorizeUrl': authorize_url, 'state': state_token}}), 200


@app.route('/oauth/twitter/callback', methods=['GET'])
def twitter_oauth_callback():
    db_path = _current_db_path()
    state_token = str(request.args.get('state', '') or '')
    code = str(request.args.get('code', '') or '')
    error = str(request.args.get('error', '') or '')
    request_state = x_review.get_oauth_request(state_token, db_path=db_path)
    if not request_state:
        return Response('Unknown Twitter OAuth state', status=400, mimetype='text/plain')

    if error:
        x_review.complete_oauth_request(
            state_token,
            status='error',
            error_text=error,
            result={'state': state_token, 'error': error},
            db_path=db_path,
        )
        if request_state.account_id:
            account_events.record_event(
                account_id=request_state.account_id,
                profile_id=request_state.profile_id,
                platform=profile_registry.PLATFORM_TWITTER,
                account_name=request_state.account_name or '',
                action='oauth_callback',
                status='error',
                summary='Twitter OAuth callback failed',
                error_text=error,
                metadata={'state': state_token},
                db_path=db_path,
            )
        return Response(
            """<html><body><script>
            if (window.opener) {
              window.opener.postMessage({ type: 'sau:twitter-oauth', ok: false, error: %r }, '*');
            }
            window.close();
            </script><p>Twitter authorization failed. You may close this window.</p></body></html>""" % error,
            mimetype='text/html',
        )

    try:
        if not request_state.account_id:
            raise ValueError('Twitter OAuth request is missing accountId')
        account = profile_registry.get_account(int(request_state.account_id), db_path=db_path)
        config = dict(account.config or {})
        token_payload = x_auth.exchange_code_for_token(
            code=code,
            redirect_uri=request_state.redirect_uri,
            code_verifier=request_state.code_verifier,
        )
        access_token = str(token_payload.get('access_token') or '')
        refresh_token = str(token_payload.get('refresh_token') or '')
        user_info = x_auth.fetch_user_info(access_token=access_token) if access_token else {}
        merged_config = dict(config)
        if access_token:
            merged_config['accessToken'] = access_token
        if refresh_token:
            merged_config['refreshToken'] = refresh_token
        merged_config['accessTokenUpdatedAt'] = datetime.now().isoformat(timespec='seconds')
        merged_config['tokenType'] = token_payload.get('token_type', 'bearer')
        expires_in = token_payload.get('expires_in')
        if expires_in not in (None, ''):
            merged_config['accessTokenExpiresAt'] = (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat(timespec='seconds')
        user_data = user_info.get('data', user_info) if isinstance(user_info, dict) else {}
        if isinstance(user_data, dict):
            merged_config['twitterUserId'] = str(user_data.get('id') or merged_config.get('twitterUserId') or '')
            merged_config['twitterUserName'] = str(user_data.get('username') or merged_config.get('twitterUserName') or '')
            merged_config['twitterDisplayName'] = str(user_data.get('name') or merged_config.get('twitterDisplayName') or '')
            if user_data.get('profile_image_url'):
                merged_config['avatarUrl'] = str(user_data['profile_image_url'])
        merged_config['scope'] = str(token_payload.get('scope') or merged_config.get('scope') or ' '.join(request_state.scopes))
        merged_config['connectedAt'] = merged_config.get('connectedAt') or datetime.now().isoformat(timespec='seconds')
        merged_config['twitterAuthType'] = 'api'
        updated = profile_registry.update_account(
            account.id,
            config=merged_config,
            auth_type='oauth',
            status=1,
            db_path=db_path,
        )
        callback_payload = {
            'state': state_token,
            'status': 'ok',
            'accountId': updated.id,
            'accountName': updated.account_name,
            'twitterUserName': merged_config.get('twitterUserName', ''),
            'twitterDisplayName': merged_config.get('twitterDisplayName', ''),
            'twitterUserId': merged_config.get('twitterUserId', ''),
            'scope': merged_config.get('scope', ''),
            'accessTokenExpiresAt': merged_config.get('accessTokenExpiresAt', ''),
            'accessTokenUpdatedAt': merged_config.get('accessTokenUpdatedAt', ''),
            'connectedAt': merged_config.get('connectedAt', ''),
            'avatarUrl': merged_config.get('avatarUrl', ''),
        }
        x_review.complete_oauth_request(
            state_token,
            status='completed',
            result={
                'state': state_token,
                'status': 'ok',
                'accountId': updated.id,
                'accountName': updated.account_name,
                'twitterUserName': merged_config.get('twitterUserName', ''),
                'scope': merged_config.get('scope', ''),
            },
            db_path=db_path,
        )
        account_events.record_event(
            account_id=updated.id,
            profile_id=updated.profile_id,
            platform=updated.platform,
            account_name=updated.account_name,
            action='oauth_callback',
            status='ok',
            summary=f"Twitter connected: @{merged_config.get('twitterUserName') or updated.account_name}",
            metadata={'state': state_token, 'scope': merged_config.get('scope', '')},
            db_path=db_path,
        )
        html = f"""<html><body><script>
        if (window.opener) {{
          window.opener.postMessage({{ type: 'sau:twitter-oauth', ok: true, data: {json.dumps(callback_payload, ensure_ascii=False)} }}, '*');
        }}
        window.close();
        </script><p>Twitter authorization completed. You may close this window.</p></body></html>"""
        return Response(html, mimetype='text/html')
    except Exception as exc:  # noqa: BLE001
        x_review.complete_oauth_request(
            state_token,
            status='error',
            error_text=str(exc),
            result={'state': state_token, 'error': str(exc)},
            db_path=db_path,
        )
        if request_state.account_id:
            account_events.record_event(
                account_id=request_state.account_id,
                profile_id=request_state.profile_id,
                platform=profile_registry.PLATFORM_TWITTER,
                account_name=request_state.account_name or '',
                action='oauth_callback',
                status='error',
                summary='Twitter OAuth callback failed',
                error_text=str(exc),
                metadata={'state': state_token},
                db_path=db_path,
            )
        return Response(f"<html><body><p>Twitter callback failed: {exc}</p></body></html>", status=500, mimetype='text/html')


def _default_scopes_for_platform(platform: str) -> list[str]:
    """Return the current default OAuth scopes for a platform."""
    if platform == profile_registry.PLATFORM_TIKTOK:
        return list(tiktok_auth.DEFAULT_SCOPES)
    if platform == profile_registry.PLATFORM_REDDIT:
        return list(reddit_auth.DEFAULT_SCOPES)
    if platform == profile_registry.PLATFORM_YOUTUBE:
        return list(youtube_auth.DEFAULT_SCOPES)
    if platform == profile_registry.PLATFORM_THREADS:
        return list(threads_auth.DEFAULT_SCOPES)
    return []


@app.route('/admin/oauth/status', methods=['GET'])
def oauth_admin_status():
    db_path = _current_db_path()
    platform = str(request.args.get('platform', '') or '').strip().lower()
    raw_account_id = request.args.get('accountId')
    account_id = int(raw_account_id) if raw_account_id not in (None, '') and str(raw_account_id).isdigit() else None
    if not platform:
        return jsonify({'code': 200, 'msg': 'ok', 'data': {}}), 200

    # Auto-discover account when none specified
    if account_id is None and platform:
        try:
            all_accounts = profile_registry.list_accounts(db_path=db_path)
            for acc in all_accounts:
                if acc.platform == platform and acc.auth_type == 'oauth':
                    account_id = acc.id
                    break
        except Exception:
            pass
    if platform not in {
        profile_registry.PLATFORM_REDDIT,
        profile_registry.PLATFORM_YOUTUBE,
        profile_registry.PLATFORM_FACEBOOK,
        profile_registry.PLATFORM_INSTAGRAM,
        profile_registry.PLATFORM_THREADS,
        profile_registry.PLATFORM_TWITTER,
        profile_registry.PLATFORM_TIKTOK,
    }:
        return jsonify({'code': 400, 'msg': 'Unsupported platform for OAuth status', 'data': None}), 400

    redirect_uri = ''
    request_state = None
    products = []
    if platform == profile_registry.PLATFORM_REDDIT:
        redirect_uri = _reddit_callback_base_url()
        request_state = reddit_review.latest_oauth_request(account_id=account_id, db_path=db_path)
        products = ['Reddit OAuth']
    elif platform == profile_registry.PLATFORM_YOUTUBE:
        redirect_uri = _youtube_callback_base_url()
        request_state = youtube_review.latest_oauth_request(account_id=account_id, db_path=db_path)
        products = ['Google OAuth', 'YouTube Data API']
    elif platform in {profile_registry.PLATFORM_FACEBOOK, profile_registry.PLATFORM_INSTAGRAM}:
        redirect_uri = _meta_callback_base_url()
        request_state = meta_review.latest_oauth_request(account_id=account_id, db_path=db_path)
        products = ['Meta Graph API OAuth']
    elif platform == profile_registry.PLATFORM_THREADS:
        redirect_uri = _threads_callback_base_url()
        request_state = threads_review.latest_oauth_request(account_id=account_id, db_path=db_path)
        products = ['Threads API OAuth']
    elif platform == profile_registry.PLATFORM_TWITTER:
        redirect_uri = _twitter_callback_base_url()
        request_state = x_review.latest_oauth_request(account_id=account_id, db_path=db_path)
        products = ['Twitter/X OAuth 2.0']
    elif platform == profile_registry.PLATFORM_TIKTOK:
        redirect_uri = _tiktok_callback_base_url() or tiktok_auth.default_redirect_uri()
        request_state = tiktok_review.latest_oauth_request(account_id=account_id, db_path=db_path)
        products = ['TikTok Login Kit', 'Content Posting API']

    events = account_events.list_events(limit=25, account_id=account_id, platform=platform, db_path=db_path)
    last_start = next((event for event in events if event.action == 'oauth_start'), None)
    last_callback = next((event for event in events if event.action == 'oauth_callback'), None)
    last_refresh = next((event for event in events if event.action == 'refresh_token'), None)

    account_payload = None
    expiry = None
    recommended_action = ''
    reconnect_required = False
    if account_id is not None:
        try:
            account = profile_registry.get_account(account_id, db_path=db_path)
            account_payload = _account_payload(account)
            config = dict(account.config or {})
            if platform in {profile_registry.PLATFORM_FACEBOOK, profile_registry.PLATFORM_INSTAGRAM}:
                expiry = str(config.get('metaUserAccessTokenExpiresAt') or config.get('accessTokenExpiresAt') or '')
                expires_at = prepared_publishers._parse_iso_datetime(expiry)
                reconnect_required = bool(expires_at and expires_at <= prepared_publishers._utc_now())
                recommended_action = 'reconnect' if reconnect_required else ('refresh' if config.get('metaUserAccessToken') else 'connect')
            else:
                expiry = str(config.get('accessTokenExpiresAt') or '')
                expires_at = prepared_publishers._parse_iso_datetime(expiry)
                if expiry and expires_at and expires_at <= prepared_publishers._utc_now():
                    recommended_action = 'reconnect'
                    reconnect_required = True
                elif expiry and expires_at:
                    recommended_action = 'refresh'
                else:
                    recommended_action = 'refresh' if config.get('accessToken') else 'connect'
        except LookupError:
            account_payload = None

    return jsonify({
        'code': 200,
        'msg': 'ok',
        'data': {
            'platform': platform,
            'accountId': account_id,
            'redirectUri': redirect_uri,
            'selectedProducts': products,
            'selectedScopes': request_state.scopes if request_state and request_state.scopes else _default_scopes_for_platform(platform),
            'lastRequest': _oauth_request_to_status(request_state),
            'lastStart': last_start.to_dict() if last_start else None,
            'lastCallback': last_callback.to_dict() if last_callback else None,
            'lastRefresh': last_refresh.to_dict() if last_refresh else None,
            'account': account_payload,
            'expiresAt': expiry,
            'reconnectRequired': reconnect_required,
            'recommendedAction': recommended_action,
            'recentEvents': [event.to_dict() for event in events],
        },
    }), 200


@app.route('/oauth/threads/start', methods=['POST'])
def threads_oauth_start():
    db_path = _current_db_path()
    try:
        data = _read_json_body()
        raw_account_id = data.get('accountId')
        if raw_account_id in (None, ''):
            raise ValueError('Please save the Threads account before connecting OAuth')
        account_id = int(raw_account_id)
        account = profile_registry.get_account(account_id, db_path=db_path)
        if account.platform != profile_registry.PLATFORM_THREADS:
            raise ValueError('OAuth connect is only available for Threads accounts on this route')
        state_token = threads_auth.build_state_token()
        redirect_uri = str(data.get('redirectUri') or _threads_callback_base_url() or threads_auth.default_redirect_uri()).strip()
        scopes = data.get('scopes') if isinstance(data.get('scopes'), list) and data.get('scopes') else list(threads_auth.DEFAULT_SCOPES)
        authorize_url = threads_auth.build_authorize_url_from_env(
            state=state_token,
            redirect_uri=redirect_uri,
            scopes=tuple(str(scope) for scope in scopes),
        )
        threads_review.create_oauth_request(
            state_token=state_token,
            profile_id=account.profile_id,
            account_id=account.id,
            account_name=account.account_name,
            redirect_uri=redirect_uri,
            scopes=[str(scope) for scope in scopes],
            db_path=db_path,
        )
        account_events.record_event(
            account_id=account.id,
            profile_id=account.profile_id,
            platform=account.platform,
            account_name=account.account_name,
            action='oauth_start',
            status='ok',
            summary='Threads OAuth flow started',
            metadata={'state': state_token, 'redirectUri': redirect_uri, 'scopes': scopes},
            db_path=db_path,
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({'code': 400, 'msg': str(exc), 'data': None}), 400
    return jsonify({'code': 200, 'msg': 'ok', 'data': {'authorizeUrl': authorize_url, 'state': state_token}}), 200


@app.route('/oauth/threads/callback', methods=['GET'])
def threads_oauth_callback():
    db_path = _current_db_path()
    state_token = str(request.args.get('state', '') or '')
    code = str(request.args.get('code', '') or '')
    error = str(request.args.get('error', '') or '')
    request_state = threads_review.get_oauth_request(state_token, db_path=db_path)
    if not request_state:
        return Response('Unknown Threads OAuth state', status=400, mimetype='text/plain')

    if error:
        threads_review.complete_oauth_request(state_token, status='error', error_text=error, result={'state': state_token, 'error': error}, db_path=db_path)
        if request_state.account_id:
            account_events.record_event(
                account_id=request_state.account_id,
                profile_id=request_state.profile_id,
                platform=profile_registry.PLATFORM_THREADS,
                account_name=request_state.account_name or '',
                action='oauth_callback',
                status='error',
                summary='Threads OAuth callback failed',
                error_text=error,
                metadata={'state': state_token},
                db_path=db_path,
            )
        return Response("""<html><body><script>
            if (window.opener) {
              window.opener.postMessage({ type: 'sau:threads-oauth', ok: false, error: %r }, '*');
            }
            window.close();
            </script><p>Threads authorization failed. You may close this window.</p></body></html>""" % error, mimetype='text/html')

    try:
        if not request_state.account_id:
            raise ValueError('Threads OAuth request is missing accountId')
        account = profile_registry.get_account(int(request_state.account_id), db_path=db_path)
        token_payload = threads_auth.exchange_code_for_token(code=code, redirect_uri=request_state.redirect_uri)
        short_lived_access_token = str(token_payload.get('access_token') or '')
        long_lived_payload = threads_auth.exchange_for_long_lived_token(access_token=short_lived_access_token) if short_lived_access_token else {}
        access_token = str(long_lived_payload.get('access_token') or short_lived_access_token)
        me_payload = threads_auth.fetch_me(access_token=access_token, fields=('id', 'username', 'threads_profile_picture_url')) if access_token else {}
        merged_config = dict(account.config or {})
        if access_token:
            merged_config['accessToken'] = access_token
        user_id = token_payload.get('user_id') or me_payload.get('id')
        if user_id:
            merged_config['threadUserId'] = str(user_id)
            merged_config['userId'] = str(user_id)
        if me_payload.get('username'):
            merged_config['threadsUserName'] = str(me_payload.get('username') or '')
        if me_payload.get('threads_profile_picture_url'):
            merged_config['avatarUrl'] = str(me_payload.get('threads_profile_picture_url'))
        merged_config['accessTokenUpdatedAt'] = datetime.now().isoformat(timespec='seconds')
        expires_in = long_lived_payload.get('expires_in') or token_payload.get('expires_in')
        if expires_in not in (None, ''):
            merged_config['accessTokenExpiresAt'] = (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat(timespec='seconds')
        merged_config['scope'] = str(merged_config.get('scope') or ' '.join(request_state.scopes))
        merged_config['connectedAt'] = merged_config.get('connectedAt') or datetime.now().isoformat(timespec='seconds')
        updated = profile_registry.update_account(account.id, config=merged_config, auth_type='oauth', status=1, db_path=db_path)
        callback_payload = {
            'state': state_token,
            'status': 'ok',
            'accountId': updated.id,
            'accountName': updated.account_name,
            'accessToken': merged_config.get('accessToken', ''),
            'threadUserId': merged_config.get('threadUserId', ''),
            'threadsUserName': merged_config.get('threadsUserName', ''),
            'accessTokenExpiresAt': merged_config.get('accessTokenExpiresAt', ''),
            'accessTokenUpdatedAt': merged_config.get('accessTokenUpdatedAt', ''),
            'connectedAt': merged_config.get('connectedAt', ''),
            'avatarUrl': merged_config.get('avatarUrl', ''),
        }
        threads_review.complete_oauth_request(
            state_token,
            status='completed',
            result={
                'state': state_token,
                'status': 'ok',
                'accountId': updated.id,
                'accountName': updated.account_name,
                'threadUserId': merged_config.get('threadUserId', ''),
                'threadsUserName': merged_config.get('threadsUserName', ''),
            },
            db_path=db_path,
        )
        account_events.record_event(
            account_id=updated.id,
            profile_id=updated.profile_id,
            platform=updated.platform,
            account_name=updated.account_name,
            action='oauth_callback',
            status='ok',
            summary=f"Threads connected: {merged_config.get('threadsUserName') or updated.account_name}",
            metadata={'state': state_token},
            db_path=db_path,
        )
        html = f"""<html><body><script>
        if (window.opener) {{
          window.opener.postMessage({{ type: 'sau:threads-oauth', ok: true, data: {json.dumps(callback_payload, ensure_ascii=False)} }}, '*');
        }}
        window.close();
        </script><p>Threads authorization completed. You may close this window.</p></body></html>"""
        return Response(html, mimetype='text/html')
    except Exception as exc:  # noqa: BLE001
        threads_review.complete_oauth_request(state_token, status='error', error_text=str(exc), result={'state': state_token, 'error': str(exc)}, db_path=db_path)
        if request_state.account_id:
            account_events.record_event(
                account_id=request_state.account_id,
                profile_id=request_state.profile_id,
                platform=profile_registry.PLATFORM_THREADS,
                account_name=request_state.account_name or '',
                action='oauth_callback',
                status='error',
                summary='Threads OAuth callback failed',
                error_text=str(exc),
                metadata={'state': state_token},
                db_path=db_path,
            )
        return Response(f"<html><body><p>Threads callback failed: {exc}</p></body></html>", status=500, mimetype='text/html')


@app.route('/oauth/tiktok/start', methods=['POST'])
def tiktok_oauth_start():
    db_path = _current_db_path()
    try:
        data = _read_json_body()
        state_token = tiktok_auth.build_state_token()
        redirect_uri = str(data.get('redirectUri') or _tiktok_callback_base_url() or tiktok_auth.default_redirect_uri()).strip()
        scopes = data.get('scopes') if isinstance(data.get('scopes'), list) and data.get('scopes') else list(tiktok_auth.DEFAULT_SCOPES)
        authorize_url = tiktok_auth.build_authorize_url_from_env(
            state=state_token,
            redirect_uri=redirect_uri,
            scopes=tuple(str(scope) for scope in scopes),
        )
        tiktok_review.create_oauth_request(
            state_token=state_token,
            profile_id=int(data.get('profileId')) if data.get('profileId') not in (None, '') else None,
            account_id=int(data.get('accountId')) if data.get('accountId') not in (None, '') else None,
            account_name=data.get('accountName'),
            redirect_uri=redirect_uri,
            scopes=[str(scope) for scope in scopes],
            db_path=db_path,
        )
        _append_tiktok_review_event(
            'start',
            {
                'state': state_token,
                'redirectUri': redirect_uri,
                'scopes': scopes,
                'accountName': data.get('accountName'),
            },
            account_id=int(data.get('accountId')) if data.get('accountId') not in (None, '') else None,
            account_name=data.get('accountName'),
            status='started',
            db_path=db_path,
        )
        _start_account_id = int(data.get('accountId')) if data.get('accountId') not in (None, '') else None
        if _start_account_id:
            account_events.record_event(
                account_id=_start_account_id,
                profile_id=int(data.get('profileId')) if data.get('profileId') not in (None, '') else None,
                platform=profile_registry.PLATFORM_TIKTOK,
                account_name=data.get('accountName'),
                action='oauth_start',
                status='started',
                summary=f"TikTok OAuth started: {data.get('accountName')}",
                metadata={'state': state_token, 'scopes': scopes},
                db_path=db_path,
            )
    except Exception as exc:  # noqa: BLE001
        return jsonify({'code': 400, 'msg': str(exc), 'data': None}), 400
    return jsonify({'code': 200, 'msg': 'ok', 'data': {'authorizeUrl': authorize_url, 'state': state_token}}), 200


@app.route('/oauth/tiktok/callback', methods=['GET'])
def tiktok_oauth_callback():
    db_path = _current_db_path()
    state_token = str(request.args.get('state', '') or '')
    code = str(request.args.get('code', '') or '')
    error = str(request.args.get('error', '') or '')
    request_state = tiktok_review.get_oauth_request(state_token, db_path=db_path)
    if not request_state:
        return Response('Unknown TikTok OAuth state', status=400, mimetype='text/plain')

    # Idempotency: if already completed (duplicate callback), return stored result
    if request_state.status == 'completed' and request_state.result:
        result = request_state.result
        html = f"""<html><body><script>
        if (window.opener) {{
          window.opener.postMessage({{ type: 'sau:tiktok-oauth', ok: true, data: {json.dumps(result, ensure_ascii=False)} }}, '*');
        }}
        window.close();
        </script><p>TikTok authorization completed. You may close this window.</p></body></html>"""
        return Response(html, mimetype='text/html')

    if error:
        tiktok_review.complete_oauth_request(
            state_token,
            status='error',
            error_text=error,
            result={'state': state_token, 'error': error},
            db_path=db_path,
        )
        _append_tiktok_review_event(
            'callback',
            {
                'state': state_token,
                'status': 'error',
                'error': error,
            },
            account_id=request_state.account_id,
            account_name=request_state.account_name,
            status='error',
            db_path=db_path,
        )
        if request_state.account_id:
            account_events.record_event(
                account_id=int(request_state.account_id),
                profile_id=request_state.profile_id,
                platform=profile_registry.PLATFORM_TIKTOK,
                account_name=request_state.account_name,
                action='oauth_callback',
                status='error',
                summary=f"TikTok OAuth error: {error[:100]}",
                metadata={'state': state_token, 'error': error},
                db_path=db_path,
            )
        return Response(
            """<html><body><script>
            if (window.opener) {
              window.opener.postMessage({ type: 'sau:tiktok-oauth', ok: false, error: %r }, '*');
            }
            window.close();
            </script><p>TikTok authorization failed. You may close this window.</p></body></html>""" % error,
            mimetype='text/html',
        )

    try:
        token_payload = tiktok_auth.exchange_code_for_token(
            code=code,
            redirect_uri=request_state.redirect_uri,
        )
        if not isinstance(token_payload, dict):
            token_payload = {}
        access_token = str(token_payload.get('access_token') or '')
        refresh_token = str(token_payload.get('refresh_token') or '')
        user_info = tiktok_auth.fetch_user_info(access_token=access_token) if access_token else {}
        if not isinstance(user_info, dict):
            user_info = {}
        account_id = request_state.account_id
        if account_id:
            account = profile_registry.get_account(int(account_id), db_path=db_path)
            merged_config = dict(account.config or {})
            merged_config = prepared_publishers._apply_tiktok_token_payload(merged_config, token_payload, user_info)
            merged_config.update({
                'openId': token_payload.get('open_id') or user_info.get('data', {}).get('user', {}).get('open_id') or merged_config.get('openId') or '',
                'scope': token_payload.get('scope') or ','.join(request_state.scopes),
                'connectedAt': merged_config.get('connectedAt') or datetime.now().isoformat(timespec='seconds'),
            })
            profile_registry.update_account(
                int(account_id),
                config=merged_config,
                auth_type='oauth',
                db_path=db_path,
            )
        callback_payload = {
            'state': state_token,
            'status': 'ok',
            'accountId': account_id,
            'accountName': request_state.account_name,
            'accessToken': access_token,
            'refreshToken': refresh_token,
            'openId': token_payload.get('open_id') or user_info.get('data', {}).get('user', {}).get('open_id') or '',
            'scope': token_payload.get('scope') or ','.join(request_state.scopes),
            'displayName': user_info.get('data', {}).get('user', {}).get('display_name') or '',
            'avatarUrl': user_info.get('data', {}).get('user', {}).get('avatar_url') or '',
            'accessTokenExpiresAt': merged_config.get('accessTokenExpiresAt', '') if account_id else '',
            'refreshTokenExpiresAt': merged_config.get('refreshTokenExpiresAt', '') if account_id else '',
            'accessTokenUpdatedAt': merged_config.get('accessTokenUpdatedAt', '') if account_id else '',
            'connectedAt': merged_config.get('connectedAt', '') if account_id else '',
        }
        persisted_callback_payload = {
            'state': state_token,
            'status': 'ok',
            'accountId': account_id,
            'accountName': request_state.account_name,
            'openId': callback_payload['openId'],
            'scope': callback_payload['scope'],
            'displayName': callback_payload['displayName'],
            'avatarUrl': callback_payload['avatarUrl'],
        }
        tiktok_review.complete_oauth_request(
            state_token,
            status='completed',
            result=callback_payload,
            db_path=db_path,
        )
        _append_tiktok_review_event(
            'callback',
            persisted_callback_payload,
            account_id=account_id,
            account_name=request_state.account_name,
            status='ok',
            db_path=db_path,
        )
        if account_id:
            account_events.record_event(
                account_id=int(account_id),
                profile_id=request_state.profile_id,
                platform=profile_registry.PLATFORM_TIKTOK,
                account_name=request_state.account_name,
                action='oauth_callback',
                status='ok',
                summary=f"TikTok connected: {callback_payload.get('displayName') or request_state.account_name}",
                metadata={'state': state_token, 'openId': callback_payload.get('openId', ''), 'scope': callback_payload.get('scope', '')},
                db_path=db_path,
            )
        html = f"""<html><body><script>
        if (window.opener) {{
          window.opener.postMessage({{ type: 'sau:tiktok-oauth', ok: true, data: {json.dumps(callback_payload, ensure_ascii=False)} }}, '*');
        }}
        window.close();
        </script><p>TikTok authorization completed. You may close this window.</p></body></html>"""
        return Response(html, mimetype='text/html')
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).exception("TikTok OAuth callback failed for state=%s", state_token)
        tiktok_review.complete_oauth_request(
            state_token,
            status='error',
            error_text=str(exc),
            result={'state': state_token, 'error': str(exc)},
            db_path=db_path,
        )
        _append_tiktok_review_event(
            'callback',
            {
                'state': state_token,
                'status': 'error',
                'error': str(exc),
            },
            account_id=request_state.account_id if request_state else None,
            account_name=request_state.account_name if request_state else None,
            status='error',
            db_path=db_path,
        )
        if request_state and request_state.account_id:
            account_events.record_event(
                account_id=int(request_state.account_id),
                profile_id=request_state.profile_id,
                platform=profile_registry.PLATFORM_TIKTOK,
                account_name=request_state.account_name,
                action='oauth_callback',
                status='error',
                summary=f"TikTok OAuth failed: {str(exc)[:100]}",
                metadata={'state': state_token, 'error': str(exc)},
                db_path=db_path,
            )
        return Response(
            f"<html><body><p>TikTok callback failed: {exc}</p></body></html>",
            status=500,
            mimetype='text/html',
        )


@app.route('/webhooks/tiktok', methods=['GET', 'POST'])
def tiktok_webhook():
    if request.method == 'GET':
        challenge = request.args.get('challenge')
        if challenge:
            _append_tiktok_review_event('webhook', {'status': 'challenge', 'challenge': challenge}, status='challenge', db_path=_current_db_path())
            return Response(challenge, mimetype='text/plain')
        return jsonify({'code': 200, 'msg': 'ok', 'data': {'service': 'tiktok-webhook', 'status': 'ready', 'path': '/webhooks/tiktok'}}), 200

    raw_body = request.get_data()
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {'raw': raw_body.decode('utf-8', errors='replace')}
    signature_header = request.headers.get('Tiktok-Signature', '')
    signature_verified, signature_reason = _verify_tiktok_signature(raw_body, signature_header)
    event_payload = {
        'status': 'received',
        'headers': {
            'Tiktok-Signature': signature_header,
            'Content-Type': request.headers.get('Content-Type', ''),
        },
        'signatureVerified': signature_verified,
        'signatureStatus': signature_reason,
        'payload': payload,
    }
    _append_tiktok_review_event(
        'webhook',
        event_payload,
        signature_verified=signature_verified,
        signature_status=signature_reason,
        status='received',
        db_path=_current_db_path(),
    )
    _append_tiktok_webhook_event(event_payload)
    return jsonify({'code': 200, 'msg': 'received', 'data': {'signatureVerified': signature_verified, 'signatureStatus': signature_reason}}), 200


_MESSENGER_VERIFY_TOKEN = "LJFi34r834fwhfwqfhiOGourwihuq3u2839590fj"


@app.route('/oauth/messenger/callback', methods=['GET', 'POST'])
def messenger_webhook():
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        challenge = request.args.get('hub.challenge')
        token = request.args.get('hub.verify_token', '')
        if mode == 'subscribe' and token == _MESSENGER_VERIFY_TOKEN:
            return Response(str(challenge) if challenge else 'ok', mimetype='text/plain', status=200)
        return Response('Forbidden', status=403)
    # POST – incoming webhook events from Messenger
    payload = request.get_json(silent=True) or {}
    return jsonify({'code': 200, 'msg': 'received'}), 200


@app.route('/oauth/meta/deauthorize', methods=['POST'])
def meta_deauthorize():
    """Handle Facebook/Meta deauthorization callback.

    Meta sends a signed_request when a user deauthorizes the app. We log the
    event so the operator knows the page connection was severed, and return 200
    so Meta doesn't keep retrying.
    """
    signed_request = request.form.get('signed_request', '')
    app_id = current_app.config.get('META_APP_ID', '')
    secret = current_app.config.get('META_APP_SECRET', '')
    try:
        data = meta_auth.parse_signed_request(signed_request, app_secret=secret) if signed_request and secret else {}
    except Exception:
        data = {}
    logging.getLogger(__name__).warning('[meta] deauthorize callback received. userId=%s', data.get('user_id', 'unknown'))
    return jsonify({'code': 200, 'msg': 'received'}), 200


@app.route('/oauth/meta/data-deletion', methods=['POST'])
def meta_data_deletion():
    """Handle Meta data deletion request callback.

    When a user requests data deletion through Meta, this endpoint receives the
    signed_request. We return a confirmation URL and status code. In a
    production deployment you would hook this into your data retention pipeline.
    """
    signed_request = request.form.get('signed_request', '')
    app_id = current_app.config.get('META_APP_ID', '')
    secret = current_app.config.get('META_APP_SECRET', '')
    try:
        data = meta_auth.parse_signed_request(signed_request, app_secret=secret) if signed_request and secret else {}
    except Exception:
        data = {}
    user_id = data.get('user_id', 'unknown')
    logging.getLogger(__name__).warning('[meta] data deletion request received. userId=%s', user_id)
    confirmation_url = url_for('meta_data_deletion_status', user_id=user_id, _external=True)
    return jsonify({
        'url': confirmation_url,
        'confirmation_code': user_id,
    }), 200


@app.route('/oauth/meta/data-deletion/status', methods=['GET'])
def meta_data_deletion_status():
    user_id = request.args.get('user_id', 'unknown')
    return jsonify({
        'code': 200,
        'msg': 'ok',
        'data': {
            'userId': user_id,
            'status': 'pending',
        }
    }), 200


@app.route('/admin/tiktok/status', methods=['GET'])
def tiktok_admin_status():
    db_path = _current_db_path()
    raw_account_id = request.args.get('accountId')
    account_id = int(raw_account_id) if raw_account_id and str(raw_account_id).isdigit() else None
    redirect_uri = _tiktok_callback_base_url() or tiktok_auth.default_redirect_uri()
    parsed_redirect_uri = urlparse(redirect_uri)
    origin = f"{parsed_redirect_uri.scheme}://{parsed_redirect_uri.netloc}" if parsed_redirect_uri.scheme and parsed_redirect_uri.netloc else 'https://socialupload.iamwillywang.com'
    return jsonify({
        'code': 200,
        'msg': 'ok',
        'data': {
            'domain': parsed_redirect_uri.netloc or 'socialupload.iamwillywang.com',
            'redirectUri': redirect_uri,
            'webhookUri': f'{origin}/webhooks/tiktok',
            'selectedProducts': ['Login Kit for Web', 'Content Posting API', 'Webhooks'],
            'selectedScopes': list(tiktok_auth.DEFAULT_SCOPES),
            'accountId': account_id,
            'lastRequest': _oauth_request_to_status(tiktok_review.latest_oauth_request(account_id=account_id, db_path=db_path)),
            'lastCallback': _event_payload_to_status(tiktok_review.latest_review_event('callback', account_id=account_id, db_path=db_path)),
            'lastRefresh': _event_payload_to_status(tiktok_review.latest_review_event('refresh', account_id=account_id, db_path=db_path)),
            'lastWebhook': _event_payload_to_status(tiktok_review.latest_review_event('webhook', db_path=db_path)),
            'recentEvents': [_event_payload_to_status(event) for event in tiktok_review.list_recent_review_events(account_id=account_id, db_path=db_path)],
        },
    }), 200


@app.route('/tiktok/creator-info/<int:account_id>', methods=['GET'])
def tiktok_creator_info(account_id):
    """Return TikTok creator info for the given account.

    Fetches the creator's capabilities (nickname, post limits, privacy
    options, interaction settings) so the frontend can render the
    per-post TikTok settings panel required by TikTok's audit.
    """
    db_path = _current_db_path()
    try:
        account = profile_registry.get_account(account_id, db_path=db_path)
    except (ValueError, KeyError, LookupError) as exc:
        return jsonify({'code': 404, 'msg': str(exc), 'data': None}), 404

    if account.platform != profile_registry.PLATFORM_TIKTOK:
        return jsonify({'code': 400, 'msg': 'Account is not a TikTok account', 'data': None}), 400

    config = dict(account.config or {})
    try:
        access_token, updated_config = prepared_publishers._ensure_tiktok_access_token(config)
    except Exception as exc:
        return jsonify({'code': 401, 'msg': f'Token refresh failed: {exc}', 'data': None}), 401

    # Persist refreshed token if _ensure_tiktok_access_token rotated it.
    if updated_config is not None:
        try:
            profile_registry.update_account(account_id, config=updated_config, db_path=db_path)
        except Exception:
            logging.getLogger(__name__).debug('Could not persist refreshed token for account %s', account_id, exc_info=True)
        config = updated_config

    try:
        info = prepared_publishers.query_tiktok_creator_info(config, access_token=access_token)
    except Exception as exc:
        return jsonify({'code': 502, 'msg': f'TikTok creator_info API error: {exc}', 'data': None}), 502

    return jsonify({'code': 200, 'msg': 'ok', 'data': info}), 200


@app.route('/tiktok/publish-status/<job_id>', methods=['GET'])
def tiktok_publish_status(job_id):
    """Return TikTok publish status rows for a given job."""
    db_path = _current_db_path()
    try:
        statuses = job_runtime.list_tiktok_publish_statuses(job_id, db_path=db_path)
    except Exception as exc:
        return jsonify({'code': 500, 'msg': str(exc), 'data': None}), 500
    return jsonify({'code': 200, 'msg': 'ok', 'data': statuses}), 200


@app.route("/profiles", methods=["GET"])
def profiles_list():
    db_path = _current_db_path()
    items = profile_registry.list_profiles(workspace_id=_workspace_scope(), db_path=db_path)
    return jsonify(
        {"code": 200, "msg": "ok", "data": [_profile_payload(item) for item in items]}
    ), 200


@app.route("/profiles", methods=["POST"])
def profiles_create():
    try:
        data = _read_json_body()
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValueError("name is required")
        profile = profile_registry.create_profile(
            name,
            description=str(data.get("description", "") or ""),
            settings=data.get("settings") if isinstance(data.get("settings"), dict) else None,
            workspace_id=_workspace_scope(),
            db_path=_current_db_path(),
        )
    except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    return jsonify({"code": 200, "msg": "created", "data": _profile_payload(profile)}), 200


@app.route("/profiles/<int:profile_id>", methods=["GET"])
def profiles_get(profile_id):
    try:
        profile = profile_registry.get_profile(
            profile_id, workspace_id=_workspace_scope(), db_path=_current_db_path()
        )
    except LookupError:
        return jsonify({"code": 404, "msg": "Profile not found", "data": None}), 404
    return jsonify({"code": 200, "msg": "ok", "data": _profile_payload(profile)}), 200


@app.route("/profiles/<int:profile_id>", methods=["PATCH"])
def profiles_patch(profile_id):
    try:
        data = _read_json_body()
        profile = profile_registry.update_profile(
            profile_id,
            name=data.get("name"),
            description=data.get("description"),
            settings=data.get("settings") if isinstance(data.get("settings"), dict) else None,
            workspace_id=_workspace_scope(),
            db_path=_current_db_path(),
        )
    except LookupError:
        return jsonify({"code": 404, "msg": "Profile not found", "data": None}), 404
    except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    return jsonify({"code": 200, "msg": "updated", "data": _profile_payload(profile)}), 200


@app.route("/profiles/<int:profile_id>", methods=["DELETE"])
def profiles_delete(profile_id):
    try:
        profile_registry.delete_profile(
            profile_id, workspace_id=_workspace_scope(), db_path=_current_db_path()
        )
    except LookupError:
        return jsonify({"code": 404, "msg": "Profile not found", "data": None}), 404
    except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    return jsonify({"code": 200, "msg": "deleted", "data": None}), 200


@app.route("/profiles/<int:profile_id>/accounts", methods=["GET"])
def profile_accounts_list(profile_id):
    db_path = _current_db_path()
    try:
        profile_registry.get_profile(profile_id, workspace_id=_workspace_scope(), db_path=db_path)
    except LookupError:
        return jsonify({"code": 404, "msg": "Profile not found", "data": None}), 404

    enabled = request.args.get("enabled")
    enabled_filter = None
    if enabled is not None:
        enabled_filter = enabled.lower() in {"1", "true", "yes", "y"}
    items = profile_registry.list_accounts(
        profile_id=profile_id,
        platform=request.args.get("platform"),
        enabled=enabled_filter,
        db_path=db_path,
    )
    return jsonify(
        {"code": 200, "msg": "ok", "data": [_account_payload(item) for item in items]}
    ), 200


@app.route("/accounts/validate-config", methods=["POST"])
def accounts_validate_config():
    db_path = _current_db_path()
    try:
        data = _read_json_body()
        profile_id = data.get("profileId")
        result = _validate_account_payload(
            data,
            db_path=db_path,
            profile_id=int(profile_id) if profile_id not in (None, "") else None,
            perform_live_checks=bool(data.get("performLiveChecks", False)),
        )
    except (ValueError, TypeError) as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    return jsonify({"code": 200, "msg": "ok", "data": result.to_dict()}), 200


@app.route("/profiles/<int:profile_id>/accounts", methods=["POST"])
def profile_accounts_create(profile_id):
    try:
        data = _read_json_body()
        account_name = str(data.get("accountName", "")).strip()
        platform = str(data.get("platform", "")).strip().lower()
        if not account_name:
            raise ValueError("accountName is required")
        if not platform:
            raise ValueError("platform is required")
        # Reject creating an account under a profile owned by another workspace.
        profile_registry.get_profile(profile_id, workspace_id=_workspace_scope(), db_path=_current_db_path())
        validation = _validate_account_payload(data, db_path=_current_db_path(), profile_id=profile_id)
        if not validation.valid:
            raise ValueError("; ".join(validation.errors))
        try:
            account = profile_registry.add_account(
                profile_id,
                platform,
                account_name,
                cookie_path=data.get("cookiePath"),
                auth_type=str(data.get("authType") or ("oauth" if profile_registry.platform_defaults_to_oauth(platform) else "cookie")),
                config=data.get("config") if isinstance(data.get("config"), dict) else None,
                enabled=bool(data.get("enabled", True)),
                status=int(data.get("status", 0) or 0),
                db_path=_current_db_path(),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"Account '{account_name}' already exists for {platform} in this profile. Delete the existing one first or use a different name.")
    except LookupError:
        return jsonify({"code": 404, "msg": "Profile not found", "data": None}), 404
    except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    return jsonify({"code": 200, "msg": "created", "data": _account_payload(account)}), 200


@app.route("/accounts/<int:account_id>/check-connection", methods=["POST"])
def accounts_check_connection(account_id):
    db_path = _current_db_path()
    try:
        # Tenant isolation: 404 for accounts owned by another workspace.
        profile_registry.get_account(account_id, workspace_id=_workspace_scope(), db_path=db_path)
        updated = _run_account_connection_check(account_id=account_id, db_path=db_path)
    except LookupError:
        return jsonify({"code": 404, "msg": "Account not found", "data": None}), 404
    except Exception as exc:  # noqa: BLE001
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400

    return jsonify({"code": 200, "msg": "checked", "data": _account_payload(updated)}), 200


@app.route("/accounts/<int:account_id>/refresh-token", methods=["POST"])
def accounts_refresh_token(account_id):
    db_path = _current_db_path()
    try:
        profile_registry.get_account(account_id, workspace_id=_workspace_scope(), db_path=db_path)
        updated = _run_account_token_refresh(account_id=account_id, db_path=db_path)
    except LookupError:
        return jsonify({"code": 404, "msg": "Account not found", "data": None}), 404
    except Exception as exc:  # noqa: BLE001
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400

    return jsonify({"code": 200, "msg": "refreshed", "data": _account_payload(updated)}), 200


@app.route("/accounts/batch/check-connections", methods=["POST"])
def accounts_batch_check_connections():
    db_path = _current_db_path()
    data = request.get_json(silent=True) or {}
    raw_ids = data.get('accountIds') or []
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({"code": 400, "msg": "accountIds must be a non-empty array", "data": None}), 400
    try:
        account_ids = [int(value) for value in raw_ids if value not in (None, '')]
    except (TypeError, ValueError):
        return jsonify({"code": 400, "msg": "accountIds must contain integers", "data": None}), 400
    result = _batch_account_operation(account_ids=account_ids, db_path=db_path, operation='check')
    return jsonify({"code": 200, "msg": "ok", "data": result}), 200


@app.route("/accounts/batch/refresh-tokens", methods=["POST"])
def accounts_batch_refresh_tokens():
    db_path = _current_db_path()
    data = request.get_json(silent=True) or {}
    raw_ids = data.get('accountIds') or []
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({"code": 400, "msg": "accountIds must be a non-empty array", "data": None}), 400
    try:
        account_ids = [int(value) for value in raw_ids if value not in (None, '')]
    except (TypeError, ValueError):
        return jsonify({"code": 400, "msg": "accountIds must contain integers", "data": None}), 400
    result = _batch_account_operation(account_ids=account_ids, db_path=db_path, operation='refresh')
    return jsonify({"code": 200, "msg": "ok", "data": result}), 200


@app.route("/accounts/events", methods=["GET"])
def accounts_events_list():
    db_path = _current_db_path()
    raw_account_id = request.args.get('accountId')
    raw_profile_id = request.args.get('profileId')
    platform = str(request.args.get('platform') or '').strip().lower() or None
    limit = max(1, min(int(request.args.get('limit', 25) or 25), 200))
    account_id = int(raw_account_id) if raw_account_id not in (None, '') else None
    profile_id = int(raw_profile_id) if raw_profile_id not in (None, '') else None
    events = account_events.list_events(
        limit=limit,
        account_id=account_id,
        profile_id=profile_id,
        platform=platform,
        db_path=db_path,
    )
    return jsonify({"code": 200, "msg": "ok", "data": [event.to_dict() for event in events]}), 200


@app.route("/accounts/health-summary", methods=["GET"])
def accounts_health_summary():
    db_path = _current_db_path()
    accounts = profile_registry.list_accounts(db_path=db_path)
    refresh_platforms = {profile_registry.PLATFORM_TIKTOK, profile_registry.PLATFORM_REDDIT, profile_registry.PLATFORM_YOUTUBE, profile_registry.PLATFORM_THREADS}
    check_platforms = {profile_registry.PLATFORM_FACEBOOK, profile_registry.PLATFORM_INSTAGRAM, profile_registry.PLATFORM_THREADS, profile_registry.PLATFORM_TELEGRAM, profile_registry.PLATFORM_DISCORD}

    def _derive_state(account):
        config = dict(account.config or {})
        detail = ''
        if account.platform == profile_registry.PLATFORM_FACEBOOK:
            detail = config.get('facebookPageName', '')
        elif account.platform == profile_registry.PLATFORM_INSTAGRAM:
            detail = config.get('instagramUserName', '')
        elif account.platform == profile_registry.PLATFORM_THREADS:
            detail = config.get('threadsUserName', '')
        elif account.platform == profile_registry.PLATFORM_TELEGRAM:
            detail = config.get('telegramChatTitle', '') or config.get('telegramBotName', '')
        elif account.platform == profile_registry.PLATFORM_DISCORD:
            detail = config.get('discordWebhookName', '') or config.get('discordWebhookChannel', '')
        elif account.platform == profile_registry.PLATFORM_REDDIT:
            detail = config.get('redditUserName', '')
        elif account.platform == profile_registry.PLATFORM_YOUTUBE:
            detail = config.get('channelTitle', '')
        elif account.platform == profile_registry.PLATFORM_TIKTOK:
            detail = config.get('displayName', '') or config.get('openId', '')
        elif account.platform == profile_registry.PLATFORM_TWITTER:
            twitter_user = config.get('twitterUserName', '')
            detail = f"@{twitter_user}" if twitter_user else ''
        has_credential = bool(config.get('accessToken') or config.get('accessTokenEnv') or config.get('botTokenEnv') or config.get('webhookUrlEnv') or account.cookie_path)
        if detail or config.get('lastConnectionCheckAt') or config.get('lastManualRefreshAt') or config.get('lastAutoRefreshAt') or config.get('connectedAt') or config.get('accessTokenUpdatedAt'):
            return 'ready'
        if has_credential:
            return 'configured'
        return 'missing'

    now_utc = prepared_publishers._utc_now()
    summary = {
        'total': len(accounts),
        'ready': 0,
        'configured': 0,
        'missing': 0,
        'refreshable': 0,
        'checkable': 0,
        'byPlatform': {},
        'expirySummary': {
            'overdue': 0,
            'expiringWithin24h': 0,
            'expiringWithin7d': 0,
            'reconnectRequired': 0,
        },
        'expiringAccounts': [],
    }

    def _account_expiry_snapshot(account):
        config = dict(account.config or {})
        is_meta = account.platform in {profile_registry.PLATFORM_FACEBOOK, profile_registry.PLATFORM_INSTAGRAM}
        if is_meta and not config.get('metaUserAccessToken'):
            return None, False, False
        refreshable = account.platform in refresh_platforms or (is_meta and bool(config.get('metaUserAccessToken')))
        if not refreshable:
            return None, False, False
        expiry_raw = config.get('metaUserAccessTokenExpiresAt') if is_meta else config.get('accessTokenExpiresAt')
        if not expiry_raw:
            expiry_raw = config.get('accessTokenExpiresAt')
        expires_at = prepared_publishers._parse_iso_datetime(str(expiry_raw or ''))
        reconnect_required = bool(is_meta and expires_at is not None and expires_at <= now_utc)
        return expires_at, refreshable, reconnect_required

    for account in accounts:
        state = _derive_state(account)
        summary[state] += 1
        expires_at, is_refreshable, reconnect_required = _account_expiry_snapshot(account)
        if is_refreshable:
            summary['refreshable'] += 1
        if account.platform in check_platforms and not is_refreshable:
            summary['checkable'] += 1
        bucket = summary['byPlatform'].setdefault(account.platform, {'total': 0, 'ready': 0, 'configured': 0, 'missing': 0, 'overdue': 0, 'expiringWithin7d': 0})
        bucket['total'] += 1
        bucket[state] += 1
        if expires_at is not None:
            seconds_remaining = int((expires_at - now_utc).total_seconds())
            if seconds_remaining <= 0:
                summary['expirySummary']['overdue'] += 1
                bucket['overdue'] += 1
                if reconnect_required:
                    summary['expirySummary']['reconnectRequired'] += 1
            elif seconds_remaining <= 24 * 3600:
                summary['expirySummary']['expiringWithin24h'] += 1
                summary['expirySummary']['expiringWithin7d'] += 1
                bucket['expiringWithin7d'] += 1
            elif seconds_remaining <= 7 * 24 * 3600:
                summary['expirySummary']['expiringWithin7d'] += 1
                bucket['expiringWithin7d'] += 1
            if seconds_remaining <= 7 * 24 * 3600:
                summary['expiringAccounts'].append({
                    'accountId': account.id,
                    'profileId': account.profile_id,
                    'platform': account.platform,
                    'accountName': account.account_name,
                    'expiresAt': expires_at.isoformat(),
                    'secondsRemaining': seconds_remaining,
                    'requiresReconnect': reconnect_required,
                    'recommendedAction': 'reconnect' if reconnect_required else 'refresh',
                })

    recent_events = account_events.list_events(limit=10, db_path=db_path)
    event_totals = {'total': len(recent_events), 'ok': 0, 'error': 0}
    for event in recent_events:
        if event.status == 'ok':
            event_totals['ok'] += 1
        elif event.status == 'error':
            event_totals['error'] += 1

    return jsonify({
        "code": 200,
        "msg": "ok",
        "data": {
            **summary,
            'recentEventTotals': event_totals,
            'expiringAccounts': sorted(summary['expiringAccounts'], key=lambda item: item['secondsRemaining'])[:10],
            'recentEvents': [event.to_dict() for event in recent_events],
        },
    }), 200


@app.route("/accounts/maintenance/run", methods=["POST"])
def accounts_maintenance_run():
    db_path = _current_db_path()
    data = request.get_json(silent=True) or {}
    raw_account_ids = data.get('accountIds')
    raw_profile_id = data.get('profileId')
    dry_run = bool(data.get('dryRun', False))
    expiring_within_seconds = int(data.get('expiringWithinSeconds', 300) or 300)
    max_accounts = max(1, min(int(data.get('maxAccounts', 50) or 50), 500))
    platforms = data.get('platforms') if isinstance(data.get('platforms'), list) else None
    account_ids = None
    if isinstance(raw_account_ids, list):
        try:
            account_ids = [int(value) for value in raw_account_ids if value not in (None, '')]
        except (TypeError, ValueError):
            return jsonify({'code': 400, 'msg': 'accountIds must contain integers', 'data': None}), 400
    profile_id = int(raw_profile_id) if raw_profile_id not in (None, '') else None
    result = _run_refreshable_account_maintenance(
        db_path=db_path,
        dry_run=dry_run,
        expiring_within_seconds=expiring_within_seconds,
        max_accounts=max_accounts,
        profile_id=profile_id,
        account_ids=account_ids,
        platforms=platforms,
        mode='auto',
    )
    _ACCOUNT_MAINTENANCE_STATE['lastResult'] = result
    _ACCOUNT_MAINTENANCE_STATE['lastFinishedAt'] = datetime.now().isoformat(timespec='seconds')
    return jsonify({'code': 200, 'msg': 'ok', 'data': result}), 200


@app.route("/accounts/maintenance/status", methods=["GET"])
def accounts_maintenance_status():
    return jsonify({'code': 200, 'msg': 'ok', 'data': dict(_ACCOUNT_MAINTENANCE_STATE)}), 200


@app.route("/accounts/tiktok/refresh-stale", methods=["POST"])
def accounts_refresh_stale_tiktok_tokens():
    db_path = _current_db_path()
    data = request.get_json(silent=True) or {}
    raw_account_id = data.get('accountId')
    raw_profile_id = data.get('profileId')
    dry_run = bool(data.get('dryRun', False))
    expiring_within_seconds = int(data.get('expiringWithinSeconds', 300) or 300)
    max_accounts = max(1, min(int(data.get('maxAccounts', 50) or 50), 200))
    account_ids = [int(raw_account_id)] if raw_account_id not in (None, '') else None
    profile_id = int(raw_profile_id) if raw_profile_id not in (None, '') else None
    result = _run_refreshable_account_maintenance(
        db_path=db_path,
        dry_run=dry_run,
        expiring_within_seconds=expiring_within_seconds,
        max_accounts=max_accounts,
        profile_id=profile_id,
        account_ids=account_ids,
        platforms=[profile_registry.PLATFORM_TIKTOK],
        mode='auto',
    )
    return jsonify({'code': 200, 'msg': 'ok', 'data': result}), 200


@app.route("/accounts/<int:account_id>", methods=["PATCH"])
def accounts_patch(account_id):
    try:
        data = _read_json_body()
        existing = profile_registry.get_account(
            account_id, workspace_id=_workspace_scope(), db_path=_current_db_path()
        )
        new_profile_id = data.get("profileId", existing.profile_id)
        # Merge config: start with existing, overlay with incoming fields
        existing_config = dict(existing.config or {})
        incoming_config = data.get("config")
        if isinstance(incoming_config, dict):
            existing_config.update(incoming_config)
        print(f"🔍 PATCH /accounts/{account_id}: incoming_keys={list(incoming_config.keys()) if isinstance(incoming_config, dict) else 'NONE'} merged_keys={list(existing_config.keys())} authType={data.get('authType')} status={data.get('status')}")
        merged = {
            "platform": existing.platform,
            "authType": data.get("authType", existing.auth_type),
            "config": existing_config,
            "cookiePath": data.get("cookiePath", existing.cookie_path),
        }
        validation = _validate_account_payload(merged, db_path=_current_db_path(), profile_id=new_profile_id)
        if not validation.valid:
            raise ValueError("; ".join(validation.errors))
        resolved_auth_type = data.get("authType", existing.auth_type)
        account = profile_registry.update_account(
            account_id,
            profile_id=new_profile_id,
            account_name=data.get("accountName"),
            cookie_path=data.get("cookiePath"),
            auth_type=resolved_auth_type,
            config=existing_config,
            enabled=data.get("enabled"),
            status=data.get("status"),
            db_path=_current_db_path(),
        )
    except LookupError:
        return jsonify({"code": 404, "msg": "Account not found", "data": None}), 404
    except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    return jsonify({"code": 200, "msg": "updated", "data": _account_payload(account)}), 200


@app.route("/media/video-info", methods=["POST"])
def media_video_info():
    """Return video duration and dimensions for a file.

    Supports both local files (under videoFile/) and DO Spaces keys
    (uploaded via presigned URL, e.g. ``uploads/<uuid>_<name>.mp4``).
    """
    try:
        data = _read_json_body()
        rel_path = str(data.get("file_path", "")).strip()
        if not rel_path:
            raise ValueError("file_path is required")

        import subprocess as _sp

        # Try local file first
        resolved = (BASE_DIR / "videoFile" / rel_path).resolve()
        if str(resolved).startswith(str(BASE_DIR.resolve())) and resolved.exists():
            local_path = str(resolved)
            duration = media_pipeline.probe_video_duration(local_path)
            probe = _sp.run(
                [
                    "ffprobe", "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width,height",
                    "-of", "json",
                    local_path,
                ],
                capture_output=True, text=True,
            )
            streams = json.loads(probe.stdout or "{}").get("streams", [{}])
            stream = streams[0] if streams else {}
            return jsonify({
                "code": 200,
                "msg": "ok",
                "data": {
                    "duration_sec": round(duration, 2),
                    "width": stream.get("width"),
                    "height": stream.get("height"),
                },
            })

        # Not found locally — try DO Spaces
        from myUtils import do_spaces
        if not do_spaces.exists(rel_path):
            return jsonify({"code": 404, "msg": "File not found", "data": None}), 404

        # Download to temp file and probe locally (most reliable)
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            do_spaces.download_file(rel_path, tmp_path)
            duration = media_pipeline.probe_video_duration(tmp_path)
            probe = _sp.run(
                [
                    "ffprobe", "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width,height",
                    "-of", "json",
                    tmp_path,
                ],
                capture_output=True, text=True,
            )
            streams = json.loads(probe.stdout or "{}").get("streams", [{}])
            stream = streams[0] if streams else {}
            return jsonify({
                "code": 200,
                "msg": "ok",
                "data": {
                    "duration_sec": round(duration, 2) if duration else None,
                    "width": stream.get("width"),
                    "height": stream.get("height"),
                },
            })
        finally:
            os.unlink(tmp_path)
    except ValueError as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    except Exception as exc:
        return jsonify({"code": 500, "msg": str(exc), "data": None}), 500


@app.route("/media-groups", methods=["POST"])
def media_groups_create():
    try:
        data = _read_json_body()
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValueError("name is required")
        db_path = _current_db_path()
        group = media_group_store.create_media_group(
            name,
            notes=str(data.get("notes", "") or ""),
            primary_video_file_id=data.get("primaryVideoFileId"),
            workspace_id=_workspace_scope(),
            db_path=db_path,
        )
        items = data.get("items") or []
        if not isinstance(items, list):
            raise ValueError("items must be a list")
        if items:
            media_group_store.replace_media_group_items(
                group.id,
                [
                    (
                        int(item["fileRecordId"])
                        if "fileRecordId" in item
                        else _ensure_file_record_for_path(str(item["filePath"]), db_path=db_path),
                        str(item.get("role", "attachment")),
                    )
                    for item in items
                ],
                db_path=db_path,
            )
        payload = _media_group_payload(
            media_group_store.get_media_group(group.id, db_path=db_path),
            items=media_group_store.list_media_group_items(group.id, db_path=db_path),
        )
    except (LookupError, ValueError, TypeError, sqlite3.IntegrityError, KeyError) as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    return jsonify({"code": 200, "msg": "created", "data": payload}), 200


@app.route("/media-groups", methods=["GET"])
def media_groups_list():
    db_path = _current_db_path()
    groups = media_group_store.list_media_groups(workspace_id=_workspace_scope(), db_path=db_path)
    data = []
    for group in groups:
        items = media_group_store.list_media_group_items(group.id, db_path=db_path)
        data.append(_media_group_payload(group, items=items))
    return jsonify({"code": 200, "msg": "ok", "data": data}), 200


@app.route("/media-groups/<int:media_group_id>", methods=["GET"])
def media_groups_get(media_group_id):
    db_path = _current_db_path()
    try:
        group = media_group_store.get_media_group(
            media_group_id, workspace_id=_workspace_scope(), db_path=db_path
        )
    except LookupError:
        return jsonify({"code": 404, "msg": "Media group not found", "data": None}), 404
    items = media_group_store.list_media_group_items(media_group_id, db_path=db_path)
    return jsonify(
        {"code": 200, "msg": "ok", "data": _media_group_payload(group, items=items)}
    ), 200


@app.route("/campaigns/prepare", methods=["POST"])
def campaigns_prepare():
    db_path = _current_db_path()
    workspace_id = _workspace_scope()
    try:
        data = _read_json_body()
        profile_id = int(data.get("profileId"))
        media_group_id = int(data.get("mediaGroupId"))
        profile = profile_registry.get_profile(
            profile_id, workspace_id=workspace_id, db_path=db_path
        )
        media_group = media_group_store.get_media_group(
            media_group_id, workspace_id=workspace_id, db_path=db_path
        )
        selected_account_ids = data.get("selectedAccountIds")
        if selected_account_ids is None:
            account_rows = profile_registry.list_accounts(
                profile_id=profile_id,
                enabled=True,
                workspace_id=workspace_id,
                db_path=db_path,
            )
        else:
            if not isinstance(selected_account_ids, list):
                raise ValueError("selectedAccountIds must be a list")
            account_rows = [
                profile_registry.get_account(
                    int(account_id), workspace_id=workspace_id, db_path=db_path
                )
                for account_id in selected_account_ids
            ]
            account_rows = [
                account for account in account_rows
                if account.profile_id == profile_id and account.enabled
            ]
        if not account_rows:
            raise ValueError("No enabled accounts selected for this profile")

        watermark_spec = _derive_watermark_spec(profile, data)
        if any(account.platform in _REMOTE_URL_PLATFORMS for account in account_rows):
            _explicit_remote = data.get("uploadToRemote")
            upload_to_remote = (
                bool(_explicit_remote)
                if _explicit_remote is not None
                else media_remote_storage.is_any_backend_configured()
            )
            if not upload_to_remote:
                raise ValueError(
                    "此發佈需要可公開存取的媒體 URL（TikTok/Meta/Threads 以 URL 抓取媒體）；"
                    "請設定 SAU_SHARE_*（tgstate）或 DO_SPACES_* 物件儲存後再試。"
                )

        invalid_accounts = []
        for account in account_rows:
            validation = account_validation.validate_structured_account_config(
                platform=account.platform,
                auth_type=account.auth_type,
                config=account.config or {},
                cookie_path=account.cookie_path,
                profile_settings=profile.settings or {},
            )
            if not validation.valid:
                invalid_accounts.append(f"{account.account_name} ({account.platform}): {'; '.join(validation.errors)}")
        if invalid_accounts:
            raise ValueError("選取帳號設定不完整：" + " | ".join(invalid_accounts))

        campaign = campaign_store.create_campaign(
            profile_id,
            media_group_id,
            status=campaign_store.CAMPAIGN_PREPARING,
            selected_account_ids=[account.id for account in account_rows],
            metadata={
                "title": data.get("title", ""),
                "notes": data.get("notes", ""),
                "requestedPlatforms": sorted({account.platform for account in account_rows}),
            },
            sheet_spreadsheet_id=data.get("spreadsheetId"),
            sheet_title=str(data.get("sheetTitle", "") or "") or None,
            workspace_id=workspace_id,
            db_path=db_path,
        )

        media_files = _load_media_group_files(media_group_id, db_path=db_path)
        grouped_accounts: dict[str, list[profile_registry.Account]] = {}
        for account in account_rows:
            grouped_accounts.setdefault(account.platform, []).append(account)

        media_context = _prepare_campaign_media_artifacts(
            campaign.id,
            profile,
            media_files,
            data,
            selected_platforms=set(grouped_accounts.keys()),
            db_path=db_path,
        )

        created_posts: list[campaign_store.CampaignPost] = []
        for platform, platform_accounts in grouped_accounts.items():
            draft = _generate_platform_draft(
                platform,
                profile,
                media_group,
                data,
                media_context,
            )
            sheet_row = {}
            if profile_registry.platform_supports_sheet_export(platform):
                sheet_image_urls = media_context["imageUrls"] or None
                sheet_video_url = media_context["videoUrl"]
                if sheet_image_urls and sheet_video_url:
                    # The downstream sheet import format accepts either images
                    # or one video URL per row, never both. Prefer the video
                    # when the group contains mixed media so the row remains
                    # importable.
                    sheet_image_urls = None
                sheet_row = content_rules.build_sheet_row(
                    message=draft["message"],
                    platform=platform,
                    link=str(data.get("link", "") or ""),
                    image_urls=sheet_image_urls,
                    video_url=sheet_video_url,
                    schedule=data.get("schedule"),
                    watermark="Default" if _derive_watermark_spec(profile, data) else "",
                    first_comment=str(draft.get("firstComment", "") or ""),
                    alt_text=str(data.get("altText", "") or ""),
                    post_preset=str((platform_accounts[0].config or {}).get("sheetPostPreset", "") or ""),
                )
            created_posts.append(
                campaign_store.add_campaign_post(
                    campaign.id,
                    platform,
                    account_ids=[account.id for account in platform_accounts],
                    draft=draft,
                    sheet_row=sheet_row,
                    status=campaign_store.CAMPAIGN_POST_READY,
                    db_path=db_path,
                )
            )

        sheet_title = campaign.sheet_title or _default_sheet_title(profile)
        spreadsheet_id = campaign.sheet_spreadsheet_id
        if data.get("exportToSheet", True):
            exportable_rows = [
                post.sheet_row
                for post in created_posts
                if post.sheet_row
            ]
            if exportable_rows:
                export_result = google_sheets.GoogleSheetsClient.from_env().export_rows(
                    sheet_title=sheet_title,
                    rows=exportable_rows,
                    spreadsheet_id=spreadsheet_id,
                )
                spreadsheet_id = export_result.spreadsheet_id
                sheet_title = export_result.sheet_title

        campaign = campaign_store.update_campaign(
            campaign.id,
            status=campaign_store.CAMPAIGN_PREPARED,
            metadata={
                **(campaign.metadata or {}),
                "transcriptText": media_context.get("transcriptText", ""),
            },
            sheet_spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            prepared_at=datetime.now().isoformat(timespec="seconds"),
            last_error=None,
            db_path=db_path,
        )
    except LookupError as exc:
        return jsonify({"code": 404, "msg": str(exc), "data": None}), 404
    except Exception as exc:  # noqa: BLE001
        if "campaign" in locals():
            campaign_store.update_campaign(
                campaign.id,
                status=campaign_store.CAMPAIGN_NEEDS_REVIEW,
                last_error=str(exc),
                db_path=db_path,
            )
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400

    return jsonify({"code": 200, "msg": "prepared", "data": _campaign_payload(campaign, db_path=db_path)}), 200


@app.route("/campaigns/<int:campaign_id>", methods=["GET"])
def campaigns_get(campaign_id):
    db_path = _current_db_path()
    try:
        campaign = campaign_store.get_campaign(
            campaign_id, workspace_id=_workspace_scope(), db_path=db_path
        )
    except LookupError:
        return jsonify({"code": 404, "msg": "Campaign not found", "data": None}), 404
    return jsonify(
        {"code": 200, "msg": "ok", "data": _campaign_payload(campaign, db_path=db_path)}
    ), 200


@app.route("/campaigns/<int:campaign_id>/posts/<int:post_id>", methods=["PATCH"])
def campaigns_posts_patch(campaign_id, post_id):
    db_path = _current_db_path()
    try:
        campaign_store.get_campaign(
            campaign_id, workspace_id=_workspace_scope(), db_path=db_path
        )
        post = campaign_store.get_campaign_post(post_id, db_path=db_path)
        if post.campaign_id != campaign_id:
            raise LookupError("Campaign post not found")
        data = _read_json_body()
        updated = campaign_store.update_campaign_post(
            post_id,
            account_ids=data.get("accountIds") if isinstance(data.get("accountIds"), list) else None,
            draft=data.get("draft") if isinstance(data.get("draft"), dict) else None,
            sheet_row=data.get("sheetRow") if isinstance(data.get("sheetRow"), dict) else None,
            status=data.get("status"),
            last_published_job_id=data.get("lastPublishedJobId", campaign_store._UNSET),
            db_path=db_path,
        )
    except LookupError:
        return jsonify({"code": 404, "msg": "Campaign post not found", "data": None}), 404
    except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    return jsonify({"code": 200, "msg": "updated", "data": updated.to_dict()}), 200


@app.route("/campaigns/<int:campaign_id>/publish", methods=["POST"])
def campaigns_publish(campaign_id):
    db_path = _current_db_path()
    workspace_id = _workspace_scope()
    try:
        campaign = campaign_store.get_campaign(
            campaign_id, workspace_id=workspace_id, db_path=db_path
        )
    except LookupError:
        return jsonify({"code": 404, "msg": "Campaign not found", "data": None}), 404

    posts = campaign_store.list_campaign_posts(campaign.id, db_path=db_path)
    artifacts = [
        artifact.to_dict()
        for artifact in campaign_store.list_campaign_artifacts(campaign.id, db_path=db_path)
    ]
    queued_jobs: list[dict] = []
    skipped: list[dict] = []
    for post in posts:
        if not profile_registry.platform_supports_direct_publish(post.platform):
            skipped.append({"platform": post.platform, "reason": "content_only"})
            continue

        accounts = [
            profile_registry.get_account(account_id, db_path=db_path)
            for account_id in post.account_ids
        ]
        enabled_accounts = [account for account in accounts if account.enabled]
        if not enabled_accounts:
            skipped.append({"platform": post.platform, "reason": "no_enabled_accounts"})
            continue

        payload = {
            "campaignId": campaign.id,
            "campaignPostId": post.id,
            "platform": post.platform,
            "draft": post.draft,
            "message": post.draft.get("message", ""),
            "sheetRow": post.sheet_row,
            "artifacts": _artifact_payloads_for_platform(artifacts, post.platform),
        }
        targets = [
            (f"account:{account.id}", f"campaign_post:{post.id}", None)
            for account in enabled_accounts
        ]
        job = job_runtime.enqueue_job(
            job_runtime.JobSpec(
                platform=post.platform,
                payload=payload,
                targets=targets,
                profile_id=campaign.profile_id,
                idempotency_key=f"campaign-{campaign.id}-post-{post.id}",
            ),
            workspace_id=workspace_id,
            db_path=db_path,
        )
        queued_jobs.append(_job_to_payload(job))
        campaign_store.update_campaign_post(
            post.id,
            status=campaign_store.CAMPAIGN_POST_QUEUED,
            last_published_job_id=job.id,
            db_path=db_path,
        )

    campaign = campaign_store.update_campaign(
        campaign.id,
        status=campaign_store.CAMPAIGN_PUBLISHING if queued_jobs else campaign_store.CAMPAIGN_NEEDS_REVIEW,
        published_at=datetime.now().isoformat(timespec="seconds") if queued_jobs else campaign.published_at,
        last_error=None if queued_jobs else "No publishable posts were queued",
        db_path=db_path,
    )
    return jsonify(
        {
            "code": 200,
            "msg": "queued",
            "data": {
                "campaign": _campaign_payload(campaign, db_path=db_path),
                "jobs": queued_jobs,
                "skipped": skipped,
            },
        }
    ), 200


@app.route("/jobs", methods=["POST"])
def jobs_create():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"code": 400, "msg": "Expected a JSON object", "data": None}), 400

    try:
        platform, payload, targets = _normalise_publish_payload(data)
    except (ValueError, TypeError) as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400

    spec = job_runtime.JobSpec(
        platform=platform,
        payload=payload,
        targets=targets,
        profile_id=data.get("profileId"),
        idempotency_key=data.get("idempotencyKey"),
    )
    try:
        job = job_runtime.enqueue_job(spec, workspace_id=_workspace_scope())
    except Exception as exc:  # noqa: BLE001
        print(f"jobs_create failed: {exc}")
        return jsonify({"code": 500, "msg": str(exc), "data": None}), 500

    return jsonify({"code": 200, "msg": "queued", "data": _job_to_payload(job)}), 200


@app.route("/jobs", methods=["GET"])
def jobs_list():
    status = request.args.get("status")
    platform = request.args.get("platform")
    raw_limit = request.args.get("limit", "50")
    try:
        # ValueError covers non-numeric input; the data layer ValueError covers
        # zero/negative/oversized limits and surfaces a precise message.
        items = job_runtime.list_jobs(
            status=status,
            platform=platform,
            limit=int(raw_limit),
            workspace_id=_workspace_scope(),
        )
    except ValueError as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    return jsonify({"code": 200, "msg": "ok",
                    "data": [_job_to_payload(item) for item in items]}), 200


@app.route("/jobs/<int:job_id>", methods=["GET"])
def jobs_get(job_id):
    try:
        job = job_runtime.get_job(job_id, workspace_id=_workspace_scope())
    except LookupError:
        return jsonify({"code": 404, "msg": "Job not found", "data": None}), 404
    targets = job_runtime.list_targets(job_id)
    body = _job_to_payload(job)
    body["targets"] = [_target_to_payload(target) for target in targets]
    return jsonify({"code": 200, "msg": "ok", "data": body}), 200


@app.route("/jobs/<int:job_id>/cancel", methods=["POST"])
def jobs_cancel(job_id):
    try:
        # Tenant isolation: 404 for a job owned by another workspace.
        job_runtime.get_job(job_id, workspace_id=_workspace_scope())
        job = job_runtime.cancel_job(job_id)
    except LookupError:
        return jsonify({"code": 404, "msg": "Job not found", "data": None}), 404
    return jsonify({"code": 200, "msg": "cancelled", "data": _job_to_payload(job)}), 200


@app.route("/jobs/run", methods=["POST"])
def jobs_run():
    """Drain the queue synchronously in this process.

    Useful for dev mode and single-instance deployments where running a
    separate worker process would be overkill. Production should run a
    dedicated worker via ``python -m myUtils.worker`` instead and skip this
    endpoint entirely.
    """

    try:
        run_worker_drain(default_executor)
    except Exception as exc:  # noqa: BLE001
        print(f"jobs_run failed: {exc}")
        return jsonify({"code": 500, "msg": str(exc), "data": None}), 500
    return jsonify({"code": 200, "msg": "drained", "data": None}), 200


def _job_to_payload(job: job_runtime.Job) -> dict:
    return {
        "id": job.id,
        "idempotencyKey": job.idempotency_key,
        "platform": job.platform,
        "profileId": job.profile_id,
        "status": job.status,
        "totalTargets": job.total_targets,
        "completedTargets": job.completed_targets,
        "failedTargets": job.failed_targets,
        "createdAt": job.created_at,
        "startedAt": job.started_at,
        "finishedAt": job.finished_at,
        "payload": job.payload,
    }


def _target_to_payload(target: job_runtime.Target) -> dict:
    return {
        "id": target.id,
        "jobId": target.job_id,
        "accountRef": target.account_ref,
        "fileRef": target.file_ref,
        "scheduleAt": target.schedule_at,
        "status": target.status,
        "attempts": target.attempts,
        "lastError": target.last_error,
        "startedAt": target.started_at,
        "finishedAt": target.finished_at,
    }


# 包装函数：在线程中运行异步函数
def run_async_function(type,id,status_queue):
    match type:
        case '1':
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(xiaohongshu_cookie_gen(id, status_queue))
            loop.close()
        case '2':
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(get_tencent_cookie(id,status_queue))
            loop.close()
        case '3':
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(douyin_cookie_gen(id,status_queue))
            loop.close()
        case '4':
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(get_ks_cookie(id,status_queue))
            loop.close()
        case '7':
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(twitter_cookie_gen_legacy(id, status_queue))
            loop.close()

# SSE stream generator. Exits as soon as a terminal status ("200" or "500")
# is emitted so the worker thread does not pin a Python thread per login flow.
# A safety timeout closes the stream after ~5 minutes if no message arrives.
TERMINAL_SSE_PAYLOADS = {"200", "500"}
SSE_IDLE_TIMEOUT_SECONDS = 300


def sse_stream(status_queue, login_id=None, idle_timeout=SSE_IDLE_TIMEOUT_SECONDS):
    last_activity = time.time()
    try:
        while True:
            try:
                msg = status_queue.get(timeout=0.5)
            except Exception:
                if time.time() - last_activity > idle_timeout:
                    yield "data: 500\n\n"
                    return
                continue

            last_activity = time.time()
            yield f"data: {msg}\n\n"
            if str(msg) in TERMINAL_SSE_PAYLOADS:
                return
    finally:
        if login_id is not None:
            active_queues.pop(login_id, None)


# ---------------------------------------------------------------------------
# Publish Center — preview / regenerate / submit + template CRUD
# ---------------------------------------------------------------------------

def _publish_center_preview_payload(*, profile, accounts, drafts_by_account):
    profile_payload = {
        "profileId": profile.id,
        "profileName": profile.name,
        "profileSlug": profile.slug,
        "accounts": [],
    }
    for account in accounts:
        draft = drafts_by_account.get(account.id) or {}
        try:
            rule = content_rules.get_platform_rule(account.platform)
        except ValueError:
            rule = None
        profile_payload["accounts"].append({
            "accountId": account.id,
            "accountName": account.account_name,
            "platform": account.platform,
            "draft": draft,
            "maxChars": rule.max_chars if rule is not None else None,
            "supportsFirstComment": platform_capabilities.platform_supports_first_comment(account.platform),
            "supportsMultiMedia": platform_capabilities.platform_supports_multi_media(account.platform),
            "configSubreddits": (account.config or {}).get("subreddits", []) if account.platform == "reddit" else [],
        })
    return profile_payload


def _publish_center_load_profile(profile_id: int, *, db_path: Path) -> profile_registry.Profile:
    return profile_registry.get_profile(int(profile_id), db_path=db_path)


def _publish_center_load_accounts(profile_id: int, selected: list[int] | None, *, db_path: Path) -> list[profile_registry.Account]:
    rows = profile_registry.list_accounts(profile_id=profile_id, enabled=True, db_path=db_path)
    if not selected:
        return list(rows)
    allowed = {int(a) for a in selected}
    return [account for account in rows if account.id in allowed]


def _build_preview_media_context(brief: str) -> dict:
    return {
        "imageUrls": [],
        "videoUrl": "",
        "imageLocalPaths": [],
        "videoLocalPath": "",
        "rawImageUrls": [],
        "rawVideoUrl": "",
        "rawImageLocalPaths": [],
        "rawVideoLocalPath": "",
        "screenshotPaths": [],
        "screenshotUrls": [],
        "transcriptText": "",
        "brief": brief or "",
    }


def _preview_media_group_stub(name: str):
    """Build a transient MediaGroup-shaped object for preview prompts.

    No DB rows are written; this only carries the name forwarded to the
    LLM prompt so the user sees a sensible preview without committing
    to a media group on every preview keystroke.
    """
    from dataclasses import dataclass as _dc

    @_dc(slots=True)
    class _Stub:
        id: int = 0
        name: str = ""
        notes: str = ""
        primary_video_file_id: int | None = None
        created_at: str | None = None
        updated_at: str | None = None
    return _Stub(id=0, name=name)


@app.route("/publish-center/preview", methods=["POST"])
def publish_center_preview():
    db_path = _current_db_path()
    try:
        data = _read_json_body()
        profile_ids = data.get("profileIds") or []
        if not profile_ids:
            raise ValueError("profileIds is required")
        selected_account_ids = data.get("selectedAccountIds") or None
        brief = str(data.get("brief", "") or "")
        options = data.get("options") or {}

        results = []
        media_group_stub = _preview_media_group_stub(name=brief[:50] or "publish-center-preview")
        media_context = _build_preview_media_context(brief)
        for profile_id in profile_ids:
            profile = _publish_center_load_profile(profile_id, db_path=db_path)
            accounts = _publish_center_load_accounts(
                profile.id, selected_account_ids, db_path=db_path
            )
            request_data = publish_orchestrator._request_data_for_options(
                brief=brief, options=options, profile=profile
            )

            drafts_by_account: dict[int, dict] = {}
            cached_per_platform: dict[str, dict] = {}
            for account in accounts:
                cached = cached_per_platform.get(account.platform)
                if cached is None:
                    try:
                        cached = _generate_account_draft(
                            account, profile, media_group_stub, request_data, media_context
                        )
                    except Exception as exc:  # noqa: BLE001
                        rule = content_rules.PLATFORM_RULES.get(account.platform)
                        max_chars = (rule.max_chars if rule is not None else None) or 1000
                        cached = {
                            "message": brief.strip()[:max_chars] or brief.strip(),
                            "hashtags": [],
                            "firstComment": "",
                            "charCount": len(brief.strip()),
                            "error": str(exc),
                        }
                    cached_per_platform[account.platform] = cached
                drafts_by_account[account.id] = dict(cached)

            results.append(
                _publish_center_preview_payload(
                    profile=profile, accounts=accounts, drafts_by_account=drafts_by_account
                )
            )
    except LookupError as exc:
        return jsonify({"code": 404, "msg": str(exc), "data": None}), 404
    except Exception as exc:  # noqa: BLE001
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    return jsonify({"code": 200, "msg": "ok", "data": {"profiles": results}}), 200


@app.route("/publish-center/regenerate", methods=["POST"])
def publish_center_regenerate():
    db_path = _current_db_path()
    try:
        data = _read_json_body()
        profile_id = int(data.get("profileId"))
        account_id = int(data.get("accountId"))
        brief = str(data.get("brief", "") or "")
        options = data.get("options") or {}
        profile = _publish_center_load_profile(profile_id, db_path=db_path)
        account = profile_registry.get_account(account_id, db_path=db_path)
        if account.profile_id != profile.id:
            raise ValueError("Account does not belong to the specified profile")

        request_data = publish_orchestrator._request_data_for_options(
            brief=brief, options=options, profile=profile
        )
        media_group_stub = _preview_media_group_stub(name=brief[:50] or "publish-center-regenerate")
        media_context = _build_preview_media_context(brief)
        draft = _generate_account_draft(
            account, profile, media_group_stub, request_data, media_context, regenerate=True,
        )
    except LookupError as exc:
        return jsonify({"code": 404, "msg": str(exc), "data": None}), 404
    except Exception as exc:  # noqa: BLE001
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    return jsonify({"code": 200, "msg": "ok", "data": {"draft": draft}}), 200


def _start_worker_drain_thread():
    """Spawn a daemon thread to drain queued jobs (non-blocking)."""
    if _worker_drain_lock.locked():
        return  # worker already running
    def _run():
        with _worker_drain_lock:
            try:
                run_worker_drain(default_executor)
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).exception("Background worker drain failed")
    threading.Thread(target=_run, daemon=True).start()


@app.route("/publish-center/submit", methods=["POST"])
def publish_center_submit():
    db_path = _current_db_path()
    try:
        data = _read_json_body()
        profile_ids = [int(v) for v in (data.get("profileIds") or []) if v]
        if not profile_ids:
            raise ValueError("profileIds is required")
        media_file_paths = data.get("mediaFilePaths") or []
        if not media_file_paths:
            raise ValueError("mediaFilePaths is required")
        selected_account_ids = data.get("selectedAccountIds") or None
        brief = str(data.get("brief", "") or "")
        options = data.get("options") or {}
        schedule = data.get("schedule") or None
        account_drafts = data.get("accountDrafts") or {}
        tiktok_post_settings = data.get("tiktokPostSettings") or {}

        result = publish_orchestrator.submit_publish(
            profile_ids=profile_ids,
            selected_account_ids=[int(v) for v in selected_account_ids] if selected_account_ids else None,
            media_file_paths=[str(p) for p in media_file_paths],
            brief=brief,
            options=options,
            schedule=schedule,
            account_drafts=account_drafts,
            tiktok_post_settings=tiktok_post_settings,
            db_path=db_path,
            prepare_artifacts=_prepare_campaign_media_artifacts,
            generate_account_draft=_generate_account_draft,
            ensure_file_record_for_path=_ensure_file_record_for_path,
            artifact_payloads_for_platform=_artifact_payloads_for_platform,
            job_to_payload=_job_to_payload,
        )
    except LookupError as exc:
        return jsonify({"code": 404, "msg": str(exc), "data": None}), 404
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).exception("publish-center submit failed")
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    # Auto-trigger the worker in a daemon thread so jobs don't stay stuck at "queued".
    if result.jobs:
        _start_worker_drain_thread()

    return jsonify({
        "code": 200,
        "msg": "queued",
        "data": {
            "campaignIds": result.campaign_ids,
            "jobs": result.jobs,
            "skipped": result.skipped,
        },
    }), 200


def _template_payload(template):
    return {
        "id": template.id,
        "name": template.name,
        "slug": template.slug,
        "description": template.description,
        "config": template.config or {},
        "includedSettings": template.included_settings or [],
        "createdAt": template.created_at,
        "updatedAt": template.updated_at,
    }


@app.route("/publish-templates", methods=["GET"])
def publish_templates_list():
    db_path = _current_db_path()
    rows = template_store.list_templates(
        workspace_id=_workspace_scope(), db_path=db_path
    )
    return jsonify({
        "code": 200,
        "msg": "ok",
        "data": {"templates": [_template_payload(t) for t in rows]},
    }), 200


@app.route("/publish-templates", methods=["POST"])
def publish_templates_create():
    db_path = _current_db_path()
    try:
        data = _read_json_body()
        template = template_store.create_template(
            name=str(data.get("name") or "").strip(),
            description=str(data.get("description") or "").strip(),
            config=data.get("config") if isinstance(data.get("config"), dict) else {},
            included_settings=data.get("includedSettings") if isinstance(data.get("includedSettings"), list) else None,
            workspace_id=_workspace_scope(),
            db_path=db_path,
        )
    except ValueError as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    return jsonify({"code": 200, "msg": "created", "data": _template_payload(template)}), 200


@app.route("/publish-templates/<int:template_id>", methods=["GET"])
def publish_templates_get(template_id):
    db_path = _current_db_path()
    try:
        template = template_store.get_template(
            template_id, workspace_id=_workspace_scope(), db_path=db_path
        )
    except LookupError as exc:
        return jsonify({"code": 404, "msg": str(exc), "data": None}), 404
    return jsonify({"code": 200, "msg": "ok", "data": _template_payload(template)}), 200


@app.route("/publish-templates/<int:template_id>", methods=["PATCH"])
def publish_templates_update(template_id):
    db_path = _current_db_path()
    try:
        # Ownership gate: a template in another workspace is 404 before update.
        template_store.get_template(
            template_id, workspace_id=_workspace_scope(), db_path=db_path
        )
        data = _read_json_body()
        kwargs: dict = {"db_path": db_path}
        if "name" in data:
            kwargs["name"] = str(data.get("name") or "").strip()
        if "description" in data:
            kwargs["description"] = str(data.get("description") or "").strip()
        if "config" in data:
            kwargs["config"] = data.get("config") if isinstance(data.get("config"), dict) else {}
        if "includedSettings" in data:
            kwargs["included_settings"] = data.get("includedSettings") if isinstance(data.get("includedSettings"), list) else []
        template = template_store.update_template(template_id, **kwargs)
    except LookupError as exc:
        return jsonify({"code": 404, "msg": str(exc), "data": None}), 404
    except ValueError as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    return jsonify({"code": 200, "msg": "updated", "data": _template_payload(template)}), 200


@app.route("/publish-templates/<int:template_id>", methods=["DELETE"])
def publish_templates_delete(template_id):
    db_path = _current_db_path()
    try:
        template_store.get_template(
            template_id, workspace_id=_workspace_scope(), db_path=db_path
        )
        template_store.delete_template(template_id, db_path=db_path)
    except LookupError as exc:
        return jsonify({"code": 404, "msg": str(exc), "data": None}), 404
    return jsonify({"code": 200, "msg": "deleted", "data": None}), 200


# ---- Patreon OAuth ----

_PATREON_OAUTH_REQUESTS: dict[str, dict] = {}


@app.route('/oauth/patreon/start', methods=['POST'])
def patreon_oauth_start():
    db_path = _current_db_path()
    try:
        data = _read_json_body()
        raw_account_id = data.get('accountId')
        if raw_account_id in (None, ''):
            raise ValueError('Please save the Patreon account before connecting OAuth')
        account_id = int(raw_account_id)
        account = profile_registry.get_account(account_id, db_path=db_path)
        if account.platform != profile_registry.PLATFORM_PATREON:
            raise ValueError('OAuth connect is only available for Patreon accounts on this route')
        state_token = patreon_auth.build_state_token()
        redirect_uri = str(data.get('redirectUri') or _patreon_callback_base_url() or patreon_auth.default_redirect_uri()).strip()
        scopes = data.get('scopes') if isinstance(data.get('scopes'), list) and data.get('scopes') else list(patreon_auth.DEFAULT_SCOPES)
        config = dict(account.config or {})
        client_id_env = str(config.get('clientIdEnv') or patreon_auth.CLIENT_ID_ENV)
        authorize_url = patreon_auth.build_authorize_url_from_env(
            state=state_token,
            redirect_uri=redirect_uri,
            scopes=tuple(str(scope) for scope in scopes),
            client_id_env=client_id_env,
        )
        _PATREON_OAUTH_REQUESTS[state_token] = {
            'state_token': state_token,
            'profile_id': account.profile_id,
            'account_id': account.id,
            'account_name': account.account_name,
            'redirect_uri': redirect_uri,
            'scopes': [str(scope) for scope in scopes],
        }
        account_events.record_event(
            account_id=account.id,
            profile_id=account.profile_id,
            platform=account.platform,
            account_name=account.account_name,
            action='oauth_start',
            status='ok',
            summary='Patreon OAuth flow started',
            metadata={'state': state_token, 'redirectUri': redirect_uri, 'scopes': scopes},
            db_path=db_path,
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({'code': 400, 'msg': str(exc), 'data': None}), 400
    return jsonify({'code': 200, 'msg': 'ok', 'data': {'authorizeUrl': authorize_url, 'state': state_token}}), 200


@app.route('/oauth/patreon/callback', methods=['GET'])
def patreon_oauth_callback():
    db_path = _current_db_path()
    state_token = str(request.args.get('state', '') or '')
    code = str(request.args.get('code', '') or '')
    error = str(request.args.get('error', '') or '')
    request_state = _PATREON_OAUTH_REQUESTS.pop(state_token, None)
    if not request_state:
        return Response('Unknown Patreon OAuth state', status=400, mimetype='text/plain')

    if error:
        if request_state.get('account_id'):
            account_events.record_event(
                account_id=request_state['account_id'],
                profile_id=request_state.get('profile_id'),
                platform=profile_registry.PLATFORM_PATREON,
                account_name=request_state.get('account_name', ''),
                action='oauth_callback',
                status='error',
                summary='Patreon OAuth callback failed',
                error_text=error,
                metadata={'state': state_token},
                db_path=db_path,
            )
        return Response(
            """<html><body><script>
            if (window.opener) {
              window.opener.postMessage({ type: 'sau:patreon-oauth', ok: false, error: %r }, '*');
            }
            window.close();
            </script><p>Patreon authorization failed. You may close this window.</p></body></html>""" % error,
            mimetype='text/html',
        )

    try:
        if not request_state.get('account_id'):
            raise ValueError('Patreon OAuth request is missing accountId')
        account = profile_registry.get_account(int(request_state['account_id']), db_path=db_path)
        config = dict(account.config or {})
        client_id_env = str(config.get('clientIdEnv') or patreon_auth.CLIENT_ID_ENV)
        client_secret_env = str(config.get('clientSecretEnv') or patreon_auth.CLIENT_SECRET_ENV)
        token_payload = patreon_auth.exchange_code_for_token(
            code=code,
            redirect_uri=request_state['redirect_uri'],
            client_id_env=client_id_env,
            client_secret_env=client_secret_env,
        )
        access_token = str(token_payload.get('access_token') or '')
        refresh_token = str(token_payload.get('refresh_token') or '')
        user_info = patreon_auth.fetch_identity(access_token=access_token) if access_token else {}
        campaigns_info = patreon_auth.fetch_campaigns(access_token=access_token) if access_token else {}

        merged_config = dict(config)
        if access_token:
            merged_config['accessToken'] = access_token
        if refresh_token:
            merged_config['refreshToken'] = refresh_token
        merged_config['accessTokenUpdatedAt'] = datetime.now().isoformat(timespec='seconds')
        expires_in = token_payload.get('expires_in')
        if expires_in not in (None, ''):
            merged_config['accessTokenExpiresAt'] = (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat(timespec='seconds')

        # Extract user info from Patreon identity response
        user_data = (user_info.get('data') or {}).get('attributes') or {}
        merged_config['patreonUserName'] = str(user_data.get('full_name') or merged_config.get('patreonUserName') or '')
        merged_config['patreonUserId'] = str((user_info.get('data') or {}).get('id') or merged_config.get('patreonUserId') or '')

        # Auto-fill campaignId if available and not already set
        campaigns_data = campaigns_info.get('data') or []
        if campaigns_data and not merged_config.get('campaignId'):
            first_campaign = campaigns_data[0]
            merged_config['campaignId'] = str(first_campaign.get('id') or '')

        merged_config['patreonAuthType'] = 'api'
        merged_config['connectedAt'] = merged_config.get('connectedAt') or datetime.now().isoformat(timespec='seconds')
        updated = profile_registry.update_account(
            account.id,
            config=merged_config,
            auth_type='oauth',
            db_path=db_path,
        )
        callback_payload = {
            'state': state_token,
            'status': 'ok',
            'accountId': updated.id,
            'accountName': updated.account_name,
            'accessToken': merged_config.get('accessToken', ''),
            'refreshToken': merged_config.get('refreshToken', ''),
            'patreonUserName': merged_config.get('patreonUserName', ''),
            'patreonUserId': merged_config.get('patreonUserId', ''),
            'campaignId': merged_config.get('campaignId', ''),
            'accessTokenExpiresAt': merged_config.get('accessTokenExpiresAt', ''),
            'accessTokenUpdatedAt': merged_config.get('accessTokenUpdatedAt', ''),
            'connectedAt': merged_config.get('connectedAt', ''),
        }
        account_events.record_event(
            account_id=updated.id,
            profile_id=updated.profile_id,
            platform=updated.platform,
            account_name=updated.account_name,
            action='oauth_callback',
            status='ok',
            summary=f"Patreon connected: {merged_config.get('patreonUserName') or updated.account_name}",
            metadata={'state': state_token, 'campaignId': merged_config.get('campaignId', '')},
            db_path=db_path,
        )
        html = f"""<html><body><script>
        if (window.opener) {{
          window.opener.postMessage({{ type: 'sau:patreon-oauth', ok: true, data: {json.dumps(callback_payload, ensure_ascii=False)} }}, '*');
        }}
        window.close();
        </script><p>Patreon authorization completed. You may close this window.</p></body></html>"""
        return Response(html, mimetype='text/html')
    except Exception as exc:  # noqa: BLE001
        if request_state.get('account_id'):
            account_events.record_event(
                account_id=request_state['account_id'],
                profile_id=request_state.get('profile_id'),
                platform=profile_registry.PLATFORM_PATREON,
                account_name=request_state.get('account_name', ''),
                action='oauth_callback',
                status='error',
                summary='Patreon OAuth callback failed',
                error_text=str(exc),
                metadata={'state': state_token},
                db_path=db_path,
            )
        return Response(f"<html><body><p>Patreon callback failed: {exc}</p></body></html>", status=500, mimetype='text/html')


# ---------------------------------------------------------------------------
#  Analytics API routes
# ---------------------------------------------------------------------------


_sync_jobs = {}
_sync_jobs_lock = threading.Lock()


@app.route('/analytics/sync', methods=['POST'])
def analytics_sync_route():
    """Trigger analytics sync for all accounts or a specific account (async)."""
    db_path = _current_db_path()
    body = request.get_json(silent=True) or {}
    account_id = body.get('accountId')

    with _sync_jobs_lock:
        running = any(j['status'] == 'running' for j in _sync_jobs.values())
        if running:
            return jsonify({"code": 409, "msg": "A sync is already running", "data": None}), 409

    job_id = uuid.uuid4().hex[:8]
    with _sync_jobs_lock:
        _sync_jobs[job_id] = {'status': 'running', 'result': None, 'started_at': datetime.now().isoformat()}

    def _run_sync():
        try:
            if account_id:
                result = analytics_sync.sync_account_analytics(int(account_id), db_path=db_path)
            else:
                result = analytics_sync.sync_all_analytics(db_path=db_path)
            with _sync_jobs_lock:
                _sync_jobs[job_id] = {'status': 'completed', 'result': result, 'finished_at': datetime.now().isoformat()}
        except Exception as exc:
            logging.getLogger(__name__).exception("Analytics sync job %s failed", job_id)
            with _sync_jobs_lock:
                _sync_jobs[job_id] = {'status': 'error', 'result': {'error': str(exc)}, 'finished_at': datetime.now().isoformat()}

    threading.Thread(target=_run_sync, daemon=True).start()
    return jsonify({"code": 200, "msg": "sync started", "data": {"jobId": job_id}})


@app.route('/analytics/sync/job', methods=['GET'])
def analytics_sync_job_status():
    """Poll sync job status."""
    job_id = request.args.get('jobId', '')
    with _sync_jobs_lock:
        job = _sync_jobs.get(job_id)
    if not job:
        return jsonify({"code": 404, "msg": "Job not found", "data": None}), 404
    return jsonify({"code": 200, "msg": "ok", "data": job})


@app.route('/analytics/sync/status', methods=['GET'])
def analytics_sync_status_route():
    """Get recent sync log entries."""
    db_path = _current_db_path()
    account_id = request.args.get('accountId', type=int)
    limit = request.args.get('limit', 20, type=int)
    try:
        entries = analytics_store.list_sync_log(account_id=account_id, limit=limit, db_path=db_path)
        return jsonify({"code": 200, "msg": "ok", "data": entries})
    except Exception as exc:
        return jsonify({"code": 500, "msg": str(exc), "data": None}), 500


@app.route('/analytics/overview', methods=['GET'])
def analytics_overview_route():
    """Get aggregated analytics stats."""
    db_path = _current_db_path()
    platform = request.args.get('platform')
    account_id = request.args.get('accountId', type=int)
    date_from = request.args.get('dateFrom')
    date_to = request.args.get('dateTo')
    try:
        stats = analytics_store.get_aggregate_stats(
            platform=platform, account_id=account_id,
            date_from=date_from, date_to=date_to,
            db_path=db_path,
        )
        return jsonify({"code": 200, "msg": "ok", "data": stats})
    except Exception as exc:
        return jsonify({"code": 500, "msg": str(exc), "data": None}), 500


@app.route('/analytics/videos', methods=['GET'])
def analytics_videos_route():
    """List videos with latest metrics."""
    db_path = _current_db_path()
    platform = request.args.get('platform')
    account_id = request.args.get('accountId', type=int)
    limit = request.args.get('limit', 100, type=int)
    try:
        videos = analytics_store.get_latest_snapshots(
            platform=platform, account_id=account_id,
            limit=limit, db_path=db_path,
        )
        return jsonify({"code": 200, "msg": "ok", "data": videos})
    except Exception as exc:
        return jsonify({"code": 500, "msg": str(exc), "data": None}), 500


@app.route('/analytics/videos/<platform_video_id>/history', methods=['GET'])
def analytics_video_history_route(platform_video_id):
    """Get time series for one video."""
    db_path = _current_db_path()
    days = request.args.get('days', 30, type=int)
    try:
        history = analytics_store.get_snapshot_history(platform_video_id, days=days, db_path=db_path)
        return jsonify({"code": 200, "msg": "ok", "data": history})
    except Exception as exc:
        return jsonify({"code": 500, "msg": str(exc), "data": None}), 500


@app.route('/analytics/top-videos', methods=['GET'])
def analytics_top_videos_route():
    """Get top performing videos."""
    db_path = _current_db_path()
    platform = request.args.get('platform')
    account_id = request.args.get('accountId', type=int)
    metric = request.args.get('metric', 'views')
    limit = request.args.get('limit', 10, type=int)
    try:
        videos = analytics_store.get_top_videos(
            platform=platform, account_id=account_id,
            metric=metric, limit=limit, db_path=db_path,
        )
        return jsonify({"code": 200, "msg": "ok", "data": videos})
    except Exception as exc:
        return jsonify({"code": 500, "msg": str(exc), "data": None}), 500


@app.route('/analytics/trends', methods=['GET'])
def analytics_trends_route():
    """Get daily aggregated metric trends."""
    db_path = _current_db_path()
    platform = request.args.get('platform')
    account_id = request.args.get('accountId', type=int)
    date_from = request.args.get('dateFrom')
    date_to = request.args.get('dateTo')
    metric = request.args.get('metric', 'views')
    try:
        trends = analytics_store.get_trends(
            platform=platform, account_id=account_id,
            date_from=date_from, date_to=date_to,
            metric=metric, db_path=db_path,
        )
        return jsonify({"code": 200, "msg": "ok", "data": trends})
    except Exception as exc:
        return jsonify({"code": 500, "msg": str(exc), "data": None}), 500


@app.route('/analytics/advice', methods=['POST'])
def analytics_advice_route():
    """Get AI-powered analytics advice."""
    db_path = _current_db_path()
    body = request.get_json(silent=True) or {}
    platform = body.get('platform')
    account_id = body.get('accountId')
    date_from = body.get('dateFrom')
    date_to = body.get('dateTo')
    try:
        advice = analytics_advisor.generate_advice(
            platform=platform, account_id=account_id,
            date_from=date_from, date_to=date_to,
            db_path=db_path,
        )
        return jsonify({"code": 200, "msg": "ok", "data": advice})
    except Exception as exc:
        return jsonify({"code": 500, "msg": str(exc), "data": None}), 500


@app.route('/analytics/thumbnail/<platform_video_id>')
def analytics_thumbnail_proxy(platform_video_id):
    """Proxy TikTok thumbnails: fetch a fresh signed URL from TikTok's API and redirect.

    TikTok CDN blocks server-side requests. The browser can potentially load
    the image after following the redirect since it has a real browser fingerprint.
    Also checks DO Spaces cache and DB for previously stored thumbnails.
    """
    db_path = _current_db_path()
    try:
        import requests as _requests
        from myUtils import tiktok_auth
        from myUtils import do_spaces
        from myUtils import analytics_store

        # 1. Check DO Spaces cache first
        for ext in ('.webp', '.jpg'):
            key = f"thumbnails/tiktok/{platform_video_id}{ext}"
            if do_spaces.exists(key):
                logging.getLogger(__name__).info("Thumbnail cache hit in DO Spaces for %s", platform_video_id)
                return redirect(do_spaces.cdn_url(key))

        # 2. Check DB for stored thumbnail URL from prior sync
        stored_url = analytics_store.get_video_thumbnail(platform_video_id, db_path=db_path)
        if stored_url:
            if do_spaces.DO_SPACES_CDN_URL and stored_url.startswith(do_spaces.DO_SPACES_CDN_URL):
                logging.getLogger(__name__).info("Thumbnail found in DB (DO Spaces CDN) for %s", platform_video_id)
                return redirect(stored_url)
            # Stored URL is an external TikTok CDN URL — use as fallback below

        # 3. Try fetching a fresh URL from TikTok API
        from myUtils.profiles import list_accounts
        accounts = list_accounts(enabled=True, db_path=db_path)
        tiktok_accounts = [a for a in accounts if a.platform == 'tiktok']
        if not tiktok_accounts:
            logging.getLogger(__name__).warning("Thumbnail proxy: no TikTok accounts found")
            return redirect(stored_url) if stored_url else ('', 404)

        config = dict(tiktok_accounts[0].config or {})
        refresh_token = str(config.get('refreshToken') or '').strip()
        if not refresh_token:
            logging.getLogger(__name__).warning("Thumbnail proxy: no refresh token for TikTok account")
            return redirect(stored_url) if stored_url else ('', 404)

        try:
            data = tiktok_auth.refresh_access_token(refresh_token=refresh_token)
        except Exception as exc:
            logging.getLogger(__name__).warning("Thumbnail proxy: token refresh failed: %s", exc)
            return redirect(stored_url) if stored_url else ('', 502)

        access_token = str(data.get('access_token') or '').strip()
        if not access_token:
            logging.getLogger(__name__).warning("Thumbnail proxy: token refresh returned empty access_token")
            return redirect(stored_url) if stored_url else ('', 502)

        resp = _requests.post(
            'https://open.tiktokapis.com/v2/video/query/',
            headers={
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json; charset=UTF-8',
            },
            params={'fields': 'id,cover_image_url'},
            json={'filters': {'video_ids': [platform_video_id]}},
            timeout=15,
        )
        if not resp.ok:
            logging.getLogger(__name__).warning("Thumbnail proxy: TikTok API returned %d for %s", resp.status_code, platform_video_id)
            return redirect(stored_url) if stored_url else ('', 502)

        body = resp.json()
        videos = body.get('data', {}).get('videos', [])
        if not videos:
            logging.getLogger(__name__).warning("Thumbnail proxy: TikTok API returned no videos for %s", platform_video_id)
            return redirect(stored_url) if stored_url else ('', 404)

        fresh_url = videos[0].get('cover_image_url', '')
        if not fresh_url:
            logging.getLogger(__name__).warning("Thumbnail proxy: empty cover_image_url for %s", platform_video_id)
            return redirect(stored_url) if stored_url else ('', 404)

        # 4. Try to download with auth header and cache in DO Spaces
        try:
            img_resp = _requests.get(
                fresh_url,
                headers={'Authorization': f'Bearer {access_token}'},
                timeout=15,
            )
            if img_resp.ok and len(img_resp.content) > 1000:
                content_type = img_resp.headers.get('content-type', 'image/jpeg')
                ext = '.webp' if 'webp' in content_type else '.jpg'
                key = f"thumbnails/tiktok/{platform_video_id}{ext}"
                cdn_url = do_spaces.upload_bytes(img_resp.content, key, content_type)
                logging.getLogger(__name__).info("Thumbnail cached to DO Spaces for %s", platform_video_id)
                return redirect(cdn_url)
            else:
                logging.getLogger(__name__).warning("Thumbnail proxy: image download failed (status=%d, size=%d) for %s",
                    img_resp.status_code, len(img_resp.content), platform_video_id)
        except Exception as exc:
            logging.getLogger(__name__).warning("Thumbnail proxy: image download error for %s: %s", platform_video_id, exc)

        # 5. Fallback: redirect to fresh TikTok CDN URL
        return redirect(fresh_url)
    except Exception as exc:
        logging.getLogger(__name__).exception("Thumbnail proxy: unexpected error for %s: %s", platform_video_id, exc)
        return '', 502


# ===================== WATERMARK CONFIG ENDPOINTS =====================

@app.route("/api/watermark-configs", methods=["GET"])
def api_list_watermark_configs():
    profile_id = request.args.get("profile_id", type=int)
    configs = watermark_service.list_watermark_configs(profile_id=profile_id)
    return jsonify([c.to_dict() for c in configs])


@app.route("/api/watermark-configs", methods=["POST"])
def api_create_watermark_config():
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "Invalid or missing JSON body"}), 400
    data.pop("db_path", None)  # Never accept from client
    try:
        config = watermark_service.create_watermark_config(**data)
        return jsonify(config.to_dict()), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception("Error creating watermark config")
        return jsonify({"error": "Internal server error"}), 500


@app.route("/api/watermark-configs/<int:config_id>", methods=["GET"])
def api_get_watermark_config(config_id):
    try:
        config = watermark_service.get_watermark_config(config_id)
        return jsonify(config.to_dict())
    except ValueError:
        return jsonify({"error": "Not found"}), 404


@app.route("/api/watermark-configs/<int:config_id>", methods=["PATCH"])
def api_update_watermark_config(config_id):
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "Invalid or missing JSON body"}), 400
    data.pop("db_path", None)  # Never accept from client
    try:
        config = watermark_service.update_watermark_config(config_id, **data)
        return jsonify(config.to_dict())
    except ValueError:
        return jsonify({"error": "Not found"}), 404


@app.route("/api/watermark-configs/<int:config_id>", methods=["DELETE"])
def api_delete_watermark_config(config_id):
    try:
        watermark_service.get_watermark_config(config_id)
    except ValueError:
        return jsonify({"error": "Not found"}), 404
    watermark_service.delete_watermark_config(config_id)
    return jsonify({"ok": True})


# ===================== MEDIA ASSET ENDPOINTS =====================

@app.route("/api/media/assets", methods=["GET"])
def api_list_media_assets():
    media_type = request.args.get("media_type")
    upload_status = request.args.get("upload_status")
    processing_status = request.args.get("processing_status")
    limit = max(1, min(request.args.get("limit", 200, type=int), 1000))
    offset = max(0, request.args.get("offset", 0, type=int))
    assets = media_asset_service.list_media_assets(
        media_type=media_type,
        upload_status=upload_status,
        processing_status=processing_status,
        limit=limit,
        offset=offset,
        workspace_id=_workspace_scope(),
    )
    return jsonify([a.to_dict() for a in assets])


@app.route("/api/media/assets/<int:asset_id>", methods=["GET"])
def api_get_media_asset(asset_id):
    try:
        asset = media_asset_service.get_media_asset(asset_id, workspace_id=_workspace_scope())
        return jsonify(asset.to_dict())
    except ValueError:
        return jsonify({"error": "Not found"}), 404


@app.route("/api/media/assets/<int:asset_id>", methods=["DELETE"])
def api_delete_media_asset(asset_id):
    try:
        media_asset_service.get_media_asset(asset_id, workspace_id=_workspace_scope())
    except ValueError:
        return jsonify({"error": "Not found"}), 404
    media_asset_service.delete_media_asset(asset_id, workspace_id=_workspace_scope())
    return jsonify({"ok": True})


@app.route("/api/media/upload/batch", methods=["POST"])
def api_batch_upload():
    """Upload multiple files and create MediaAsset records."""
    from werkzeug.utils import secure_filename

    ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff',
                          '.mp4', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.webm', '.m4v',
                          '.mp3', '.wav', '.ogg', '.aac', '.flac', '.m4a'}

    uploaded_files = request.files.getlist("files")
    if not uploaded_files:
        return jsonify({"error": "No files provided"}), 400

    upload_dir = Path(BASE_DIR) / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    assets = []
    errors = []

    for f in uploaded_files:
        filename = f.filename or "unnamed"
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            errors.append({"filename": filename, "error": f"Unsupported file type: {ext}"})
            continue
        safe_base = secure_filename(filename) or "unnamed"
        safe_name = f"{uuid.uuid4().hex}_{safe_base}"
        local_path = upload_dir / safe_name
        try:
            f.save(str(local_path))
        except Exception as e:
            errors.append({"filename": filename, "error": str(e)})
            continue

        asset = media_asset_service.create_media_asset(
            workspace_id=_workspace_scope(),
            original_filename=filename,
            local_original_path=str(local_path),
            file_size=local_path.stat().st_size,
        )
        media_asset_service.update_media_asset(
            asset.id,
            upload_status=media_asset_service.UPLOAD_STATUS_UPLOADED,
        )
        assets.append(media_asset_service.get_media_asset(asset.id))

    result = {"assets": [a.to_dict() for a in assets], "count": len(assets)}
    if errors:
        result["errors"] = errors
    return jsonify(result), 201 if assets else 400


@app.route("/api/media/assets/<int:asset_id>/process", methods=["POST"])
def api_process_media_asset(asset_id):
    """Process a media asset: watermark, thumbnail, audio extraction."""
    data = request.get_json(force=True) if request.is_json else {}
    watermark_config_id = data.get("watermark_config_id")

    try:
        asset = media_asset_service.get_media_asset(asset_id, workspace_id=_workspace_scope())
    except ValueError:
        return jsonify({"error": "Asset not found"}), 404

    if not asset.local_original_path or not Path(asset.local_original_path).exists():
        return jsonify({"error": "Local file not found"}), 400

    media_asset_service.update_media_asset(
        asset_id, processing_status=media_asset_service.PROCESSING_STATUS_PROCESSING
    )

    try:
        source = Path(asset.local_original_path)
        processed_dir = Path(BASE_DIR) / "processed"
        processed_dir.mkdir(parents=True, exist_ok=True)

        wm_config = None
        if watermark_config_id:
            wm_config = watermark_service.get_watermark_config(watermark_config_id)
        else:
            configs = watermark_service.list_watermark_configs()
            if configs:
                wm_config = configs[0]

        if asset.media_type == media_asset_service.MEDIA_TYPE_VIDEO:
            if wm_config and wm_config.enabled:
                output_path = processed_dir / f"{source.stem}_watermarked{source.suffix}"
                watermark_service.apply_video_watermark(source, output_path, wm_config)
                media_asset_service.update_media_asset(
                    asset_id, local_processed_path=str(output_path)
                )
            thumb_path = processed_dir / f"{source.stem}_thumb.jpg"
            watermark_service.generate_thumbnail(source, thumb_path)
            media_asset_service.update_media_asset(
                asset_id, thumbnail_public_url=str(thumb_path)
            )
            audio_path = processed_dir / f"{source.stem}_audio.wav"
            try:
                watermark_service.extract_audio(source, audio_path)
            except Exception:
                pass

        elif asset.media_type == media_asset_service.MEDIA_TYPE_IMAGE:
            if wm_config and wm_config.enabled:
                output_path = processed_dir / f"{source.stem}_watermarked{source.suffix}"
                watermark_service.apply_image_watermark(source, output_path, wm_config)
                media_asset_service.update_media_asset(
                    asset_id, local_processed_path=str(output_path)
                )

        media_asset_service.update_media_asset(
            asset_id, processing_status=media_asset_service.PROCESSING_STATUS_PROCESSED
        )
        return jsonify(media_asset_service.get_media_asset(asset_id).to_dict())

    except Exception as e:
        media_asset_service.update_media_asset(
            asset_id, processing_status=media_asset_service.PROCESSING_STATUS_FAILED
        )
        return jsonify({"error": str(e)}), 500


@app.route("/api/media/assets/<int:asset_id>/upload-rclone", methods=["POST"])
def api_upload_asset_rclone(asset_id):
    data = request.get_json(force=True) if request.is_json else {}
    profile_slug = data.get("profile_slug", "default")

    try:
        asset = media_asset_service.get_media_asset(asset_id, workspace_id=_workspace_scope())
    except ValueError:
        return jsonify({"error": "Asset not found"}), 404

    try:
        remote_path = media_asset_service.upload_asset_to_rclone(asset, profile_slug)
        public_url = media_asset_service.resolve_public_url(asset)
        media_asset_service.update_media_asset(asset_id, public_url=public_url)
        return jsonify({"remote_path": remote_path, "public_url": public_url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===================== MEDIA GROUP ENDPOINTS (EXTENDED) =====================

@app.route("/api/media-groups/<int:group_id>", methods=["PATCH"])
def api_update_media_group(group_id):
    try:
        media_group_store.get_media_group(group_id, workspace_id=_workspace_scope(), db_path=_current_db_path())
    except LookupError:
        return jsonify({"error": "Media group not found"}), 404
    data = request.get_json(force=True)
    allowed = {"name", "notes", "profile_id", "group_type", "content_theme", "user_notes", "status"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({"error": "No valid fields"}), 400
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [group_id]
    db_path = Path(BASE_DIR) / "db" / "database.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"UPDATE media_groups SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", values)
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/media-groups/<int:group_id>/items/reorder", methods=["PATCH"])
def api_reorder_media_group_items(group_id):
    try:
        media_group_store.get_media_group(group_id, workspace_id=_workspace_scope(), db_path=_current_db_path())
    except LookupError:
        return jsonify({"error": "Media group not found"}), 404
    data = request.get_json(force=True)
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "No items provided"}), 400
    db_path = Path(BASE_DIR) / "db" / "database.db"
    with sqlite3.connect(db_path) as conn:
        for item in items:
            conn.execute(
                "UPDATE media_group_items SET sort_order = ? WHERE id = ? AND media_group_id = ?",
                (item.get("sort_order", 0), item.get("id"), group_id),
            )
        conn.commit()
    return jsonify({"ok": True})


# ===================== CAMPAIGN EXTENDED ENDPOINTS =====================

@app.route("/api/campaigns/<int:campaign_id>/generate", methods=["POST"])
def api_campaign_generate(campaign_id):
    """Generate platform-specific content for a campaign using LLM."""
    data = request.get_json(force=True) if request.is_json else {}
    platforms = data.get("platforms")

    db_path = Path(BASE_DIR) / "db" / "database.db"

    try:
        campaign = campaign_store.get_campaign(
            campaign_id, workspace_id=_workspace_scope()
        )
    except (ValueError, Exception):
        return jsonify({"error": "Campaign not found"}), 404

    try:
        profile = profile_registry.get_profile(campaign.profile_id)
    except (ValueError, Exception):
        return jsonify({"error": "Profile not found"}), 404

    profile_dict = profile.to_dict()
    profile_settings = profile_dict.get("settings") or {}
    profile_dict.update(profile_settings)

    try:
        mg = media_group_store.get_media_group(campaign.media_group_id)
        mg_dict = mg.to_dict()
    except (ValueError, Exception):
        mg_dict = {"name": "unknown", "notes": ""}

    media_info = {
        "topic": mg_dict.get("content_theme", "") or mg_dict.get("name", ""),
        "key_points": "",
        "transcript": "",
        "mood": "",
    }
    try:
        items = media_group_store.list_items(campaign.media_group_id)
        for item in items:
            assets = media_asset_service.list_media_assets(limit=1)
            if assets:
                analysis = assets[0].content_analysis
                if analysis:
                    media_info.update(analysis)
                if assets[0].transcript_text:
                    media_info["transcript"] = assets[0].transcript_text
                break
    except Exception:
        pass

    account_ids = campaign.selected_account_ids or []
    if not account_ids:
        account_ids = [a.id for a in profile_registry.list_accounts(campaign.profile_id)]

    accounts = []
    for aid in account_ids:
        try:
            acc = profile_registry.get_account(aid)
            if platforms and acc.platform not in platforms:
                continue
            accounts.append(acc)
        except (ValueError, Exception):
            continue

    if not accounts:
        return jsonify({"error": "No accounts found for generation"}), 400

    generated_posts = []
    for acc in accounts:
        context = {
            "user_notes": mg_dict.get("notes", "") or mg_dict.get("user_notes", ""),
            "subreddit": acc.config.get("subreddits", [""])[0] if acc.platform == "reddit" else "",
        }

        system_prompt, user_prompt = content_generator.build_generation_context(
            profile_dict, media_info, acc.platform, context
        )

        try:
            result = llm_client.generate_chat_completion(
                system_prompt, user_prompt, temperature=0.7
            )
            raw_content = result.content
            parsed = content_generator.parse_llm_response(raw_content, acc.platform)
            errors = content_generator.validate_post(acc.platform, parsed)

            post = content_generator.create_prepared_post(
                campaign_id=campaign_id,
                media_group_id=campaign.media_group_id,
                profile_id=campaign.profile_id,
                platform=acc.platform,
                account_id=acc.id,
                target_name=acc.account_name,
                message=parsed.get("message", ""),
                title=parsed.get("title", ""),
                description=parsed.get("description", ""),
                first_comment=parsed.get("first_comment", ""),
                hashtags=json.dumps(parsed.get("hashtags", [])),
                link=profile_dict.get("default_link", ""),
                image_urls=",".join(parsed.get("image_urls", [])),
                video_url=parsed.get("video_url", ""),
                alt_text=parsed.get("alt_text", ""),
                status="needs_review" if errors else "generated",
                validation_errors=errors,
                llm_raw_output={"raw": raw_content, "parsed": parsed},
                char_count=len(parsed.get("message", "")),
            )
            generated_posts.append(post.to_dict())
        except Exception as e:
            generated_posts.append({
                "platform": acc.platform,
                "account_id": acc.id,
                "error": str(e),
                "status": "failed",
            })

    return jsonify({"posts": generated_posts, "count": len(generated_posts)})


@app.route("/api/campaigns/<int:campaign_id>/validate", methods=["POST"])
def api_campaign_validate(campaign_id):
    posts = content_generator.list_prepared_posts(campaign_id=campaign_id)
    results = []
    for post in posts:
        post_data = post.to_dict()
        errors = content_generator.validate_post(post.platform, post_data)
        if errors:
            content_generator.update_prepared_post(post.id, validation_errors=errors, status="needs_review")
        results.append({"id": post.id, "platform": post.platform, "errors": errors, "valid": not errors})
    return jsonify({"results": results, "total": len(results), "valid": sum(1 for r in results if r["valid"])})


@app.route("/api/campaigns/<int:campaign_id>/approve", methods=["POST"])
def api_campaign_approve(campaign_id):
    data = request.get_json(force=True) if request.is_json else {}
    post_ids = data.get("post_ids")

    posts = content_generator.list_prepared_posts(campaign_id=campaign_id)
    approved = 0
    for post in posts:
        if post_ids and post.id not in post_ids:
            continue
        if post.validation_errors:
            continue
        content_generator.update_prepared_post(post.id, status="approved")
        approved += 1

    return jsonify({"approved": approved, "total": len(posts)})


@app.route("/api/campaigns/<int:campaign_id>/posts", methods=["GET"])
def api_campaign_posts(campaign_id):
    platform = request.args.get("platform")
    status = request.args.get("status")
    posts = content_generator.list_prepared_posts(
        campaign_id=campaign_id, platform=platform, status=status
    )
    return jsonify([p.to_dict() for p in posts])


@app.route("/api/campaigns/<int:campaign_id>/posts/<int:post_id>", methods=["PATCH"])
def api_update_prepared_post(campaign_id, post_id):
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "Invalid or missing JSON body"}), 400
    # Prevent clients from directly setting status (must go through validate/approve flow)
    data.pop("status", None)
    try:
        post = content_generator.update_prepared_post(post_id, **data)
        return jsonify(post.to_dict())
    except ValueError:
        return jsonify({"error": "Post not found"}), 404


@app.route("/api/campaigns/<int:campaign_id>/export/google-sheet", methods=["POST"])
def api_campaign_export_sheet(campaign_id):
    data = request.get_json(force=True) if request.is_json else {}
    profile_slug = data.get("profile_slug", "default")
    spreadsheet_id = data.get("spreadsheet_id")

    try:
        campaign = campaign_store.get_campaign(
            campaign_id, workspace_id=_workspace_scope()
        )
    except (ValueError, Exception):
        return jsonify({"error": "Campaign not found"}), 404

    posts = content_generator.list_prepared_posts(campaign_id=campaign_id, status="approved")
    if not posts:
        return jsonify({"error": "No approved posts to export"}), 400

    rows = sheet_export_service.build_sheet_rows(posts)

    all_errors = []
    for i, row in enumerate(rows):
        errors = sheet_export_service.validate_sheet_row(row)
        if errors:
            all_errors.append({"row": i, "errors": errors})

    if all_errors:
        return jsonify({"errors": all_errors, "message": "Validation failed"}), 400

    sheet_name = sheet_export_service.generate_sheet_name(profile_slug)

    try:
        result = sheet_export_service.export_to_google_sheet(
            rows, sheet_name, spreadsheet_id=spreadsheet_id,
            folder_id=data.get("folder_id"),
        )
        export = sheet_export_service.create_sheet_export(
            campaign_id=campaign_id,
            profile_id=campaign.profile_id,
            sheet_name=sheet_name,
            spreadsheet_id=result.get("spreadsheet_id", ""),
            spreadsheet_url=result.get("spreadsheet_url", ""),
            row_count=result.get("row_count", len(rows)),
            status="completed",
        )
        return jsonify(export.to_dict())
    except Exception as e:
        export = sheet_export_service.create_sheet_export(
            campaign_id=campaign_id,
            profile_id=campaign.profile_id,
            sheet_name=sheet_name,
            status="failed",
            error_message=str(e),
        )
        return jsonify({"error": str(e), "export": export.to_dict()}), 500


@app.route("/api/campaigns/<int:campaign_id>/export/csv", methods=["GET"])
def api_campaign_export_csv(campaign_id):
    posts = content_generator.list_prepared_posts(campaign_id=campaign_id, status="approved")
    if not posts:
        return jsonify({"error": "No approved posts to export"}), 400

    rows = sheet_export_service.build_sheet_rows(posts)
    csv_bytes = sheet_export_service.generate_csv_bytes(rows)

    try:
        campaign = campaign_store.get_campaign(campaign_id)
        profile = profile_registry.get_profile(campaign.profile_id)
        filename = f"{sheet_export_service.generate_sheet_name(profile.slug)}.csv"
    except Exception:
        filename = f"campaign-{campaign_id}.csv"

    # Sanitize filename for Content-Disposition header
    from werkzeug.utils import secure_filename
    safe_filename = secure_filename(filename) or f"campaign-{campaign_id}.csv"

    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{safe_filename}"'},
    )


# ===================== SHEET EXPORT HISTORY =====================

@app.route("/api/sheet-exports", methods=["GET"])
def api_list_sheet_exports():
    campaign_id = request.args.get("campaign_id", type=int)
    profile_id = request.args.get("profile_id", type=int)
    exports = sheet_export_service.list_sheet_exports(
        campaign_id=campaign_id, profile_id=profile_id
    )
    return jsonify({"code": 200, "msg": "ok", "data": [e.to_dict() for e in exports]})


_maybe_start_account_maintenance_scheduler()

# Ensure DO Spaces bucket exists on startup
try:
    do_spaces.ensure_bucket()
except Exception:
    logging.getLogger(__name__).warning("Failed to ensure DO Spaces bucket on startup")


def _periodic_local_cleanup():
    """Background thread that cleans up local files already in remote storage."""
    import time
    while True:
        time.sleep(6 * 3600)  # every 6 hours
        try:
            db = Path(BASE_DIR / "db" / "database.db")
            if db.exists():
                _cleanup_local_files(db_path=db)
        except Exception:
            logging.getLogger(__name__).exception("Periodic local cleanup failed")


threading.Thread(target=_periodic_local_cleanup, daemon=True, name="local-cleanup").start()


# ====================================================================
# /api/* aliases — thin wrappers for the redesigned frontend contract
# ====================================================================

def _import_cookies_for_account(platform, account, profile, fmt, payload, *, db_path):
    """Import cookies from JSON or Netscape format for a given platform/account."""
    import json as _json
    cookies = []
    payload = (payload or "").strip()
    if fmt in ("json", "editthiscookie") or payload[:1] in "[{":
        data = _json.loads(payload)
        arr = data if isinstance(data, list) else data.get("cookies", [])
        for item in arr:
            if not isinstance(item, dict) or "name" not in item or "value" not in item:
                continue
            cookies.append({
                "name": str(item["name"]),
                "value": str(item["value"]),
                "domain": str(item.get("domain") or ""),
                "path": str(item.get("path") or "/"),
                "expires": item.get("expires", -1),
                "httpOnly": bool(item.get("httpOnly", False)),
                "secure": bool(item.get("secure", False)),
                "sameSite": str(item.get("sameSite") or "Lax"),
            })
    else:
        # Netscape cookies.txt
        for line in payload.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies.append({
                    "domain": parts[0], "path": parts[2],
                    "secure": parts[3] == "TRUE",
                    "expires": int(parts[4]) if parts[4].isdigit() else -1,
                    "name": parts[5], "value": parts[6],
                })
    if not cookies:
        raise ValueError("No cookies parsed from payload")
    # Write cookies to the cookie store in Playwright storage_state format
    safe_name = f"{profile}__{platform}__{account}".replace("/", "_")
    cookie_dir = Path(BASE_DIR / "cookiesFile")
    cookie_dir.mkdir(parents=True, exist_ok=True)
    cookie_path = cookie_dir / f"{safe_name}.cookie"
    storage_state = {"cookies": cookies, "origins": []}
    payload_bytes = _json.dumps(storage_state, ensure_ascii=False).encode("utf-8")
    from myUtils import cookie_storage
    try:
        cookie_storage.write_cookie(cookie_path, payload_bytes)
    except Exception:
        cookie_path.write_bytes(payload_bytes)
    os.chmod(cookie_path, 0o600)
    return {"ok": True, "account": account, "cookieStatus": "valid", "count": len(cookies)}

@app.route("/api/accounts", methods=["GET"])
def api_accounts():
    """Return all accounts with cookie status and expiry, matching the
    redesigned frontend's expected shape. Workspace-scoped in enforced mode."""
    db_path = _current_db_path()
    rows = profile_registry.list_accounts(workspace_id=_workspace_scope(), db_path=db_path)
    out = []
    for a in rows:
        config = a.config or {}
        platform = a.platform or ""
        account_name = a.account_name or ""
        handle = ""
        if platform == "facebook":
            handle = config.get("facebookPageName") or ""
        elif platform == "instagram":
            handle = config.get("instagramUserName") or ""
        elif platform == "threads":
            handle = config.get("threadsUserName") or ""
        elif platform == "tiktok":
            handle = config.get("displayName") or config.get("openId") or ""
        elif platform == "youtube":
            handle = config.get("channelTitle") or ""
        elif platform == "reddit":
            handle = config.get("redditUserName") or ""
        elif platform == "twitter":
            handle = config.get("twitterUserName") or ""

        # Derive cookie status from expiry
        expiry_raw = ""
        is_meta = platform in ("facebook", "instagram")
        if is_meta:
            expiry_raw = config.get("metaUserAccessTokenExpiresAt") or config.get("accessTokenExpiresAt") or ""
        elif platform in ("tiktok", "reddit", "youtube", "threads", "twitter"):
            expiry_raw = config.get("accessTokenExpiresAt") or ""

        auth_type = getattr(a, "auth_type", "") or "cookie"
        is_oauth = auth_type == "oauth"

        cookie_status = "valid"
        expires_human = "session"
        if expiry_raw:
            try:
                exp_dt = prepared_publishers._parse_iso_datetime(expiry_raw)
                if exp_dt:
                    left = (exp_dt - prepared_publishers._utc_now()).total_seconds()
                    if left <= 0:
                        cookie_status = "expired"
                        expires_human = "expired"
                    else:
                        # OAuth tokens have short lifetimes (1-2h) and auto-refresh.
                        # Only flag as "soon" when actually close to expiring (< 5 min).
                        # Cookie sessions last weeks, so keep the 24h warning window.
                        soon_threshold = 300 if is_oauth else 86400
                        if left <= soon_threshold:
                            cookie_status = "soon"
                        # Human-readable remaining time
                        if left < 3600:
                            expires_human = f"in {int(left // 60)} min"
                        elif left < 86400:
                            expires_human = f"in {int(left // 3600)} hours"
                        else:
                            d = int(left // 86400)
                            expires_human = f"in {d} days"
            except Exception:
                pass
        elif is_meta and config.get("accessToken"):
            # Meta tokens ALWAYS have an expiry. If there's no expiry timestamp
            # but there IS an access token, the token is from an old format
            # that predates proper OAuth — mark as expired to force re-auth.
            cookie_status = "expired"
            expires_human = "reauth needed"

        # Extract avatar URL from config (stored by OAuth callbacks / token refreshes)
        avatar_url = config.get("avatarUrl") or config.get("avatar_url") or config.get("profileImageUrl") or ""

        out.append({
            "id": a.id,
            "platform": platform,
            "name": account_name,
            "handle": handle,
            "avatarUrl": avatar_url,
            "profile": getattr(a, "profile_name", "") or "default",
            "authType": getattr(a, "auth_type", "") or "cookie",
            "posts": 0,
            "cookieStatus": cookie_status,
            "expiresAt": expires_human,
        })
    return jsonify({"code": 200, "data": out, "msg": "ok"})


@app.route("/api/accounts/health", methods=["GET"])
def api_accounts_health():
    """Per-platform readiness summary."""
    db_path = _current_db_path()
    rows = profile_registry.list_accounts(db_path=db_path)
    agg = {}
    for a in rows:
        h = agg.setdefault(a.platform, {"id": a.platform, "ready": 0, "total": 0})
        h["total"] += 1
        if a.enabled and a.status in (0, 1):
            h["ready"] += 1
    for h in agg.values():
        h["pct"] = round(100 * h["ready"] / h["total"]) if h["total"] else 0
    return jsonify({"code": 200, "data": list(agg.values()), "msg": "ok"})


@app.route("/api/accounts/<int:account_id>/check", methods=["POST"])
def api_account_check(account_id):
    """Check an account's connection / cookie validity."""
    try:
        profile_registry.get_account(
            account_id, workspace_id=_workspace_scope(), db_path=_current_db_path()
        )
    except LookupError:
        return jsonify({"code": 404, "msg": "Account not found", "data": None}), 404
    try:
        result = _run_account_connection_check(account_id=account_id, db_path=_current_db_path())
        return jsonify({"code": 200, "data": {"ok": True, "message": "Connection valid"}, "msg": "ok"})
    except Exception as exc:
        return jsonify({"code": 400, "data": {"ok": False, "message": str(exc)}, "msg": str(exc)}), 400


@app.route("/api/auth/cookies/<int:account_id>/export", methods=["GET"])
def api_cookies_export(account_id):
    """Export decrypted cookies for an account (workspace-scoped)."""
    db_path = _current_db_path()
    try:
        # Tenant isolation: cookie material must never cross workspaces.
        account = profile_registry.get_account(
            account_id, workspace_id=_workspace_scope(), db_path=db_path
        )
    except LookupError:
        return jsonify(error="Account not found"), 404
    cookie_path = Path(account.cookie_path) if account.cookie_path else None
    if not cookie_path or not cookie_path.exists():
        return jsonify(error="No cookie file on record"), 404
    try:
        from myUtils import cookie_storage
        data = cookie_storage.read_cookie_file(cookie_path)
    except Exception:
        data = json.loads(cookie_path.read_text(encoding="utf-8"))
    return jsonify({"code": 200, "data": data, "msg": "ok"})


@app.route("/accounts/import-cookies", methods=["POST"])
def accounts_import_cookies():
    """Import cookies for a platform account (JSON or Netscape format).

    Creates the account in the database if it doesn't already exist,
    then writes the parsed cookies to the cookie store.
    """
    try:
        b = request.get_json(force=True) or {}
        platform = b.get("platform")
        account = b.get("account") or "imported"
        profile = b.get("profile") or "default"
        fmt = b.get("format", "json")
        payload = b.get("payload", "")
        if not platform:
            return jsonify({"code": 400, "msg": "platform is required", "data": None}), 400
        if not payload:
            return jsonify({"code": 400, "msg": "No cookie data provided", "data": None}), 400

        db_path = _current_db_path()

        # Find or create the account in the database
        existing = None
        try:
            all_accounts = profile_registry.list_accounts(db_path=db_path)
            for a in all_accounts:
                if a.platform == platform and a.account_name == account:
                    existing = a
                    break
        except Exception:
            pass

        if existing is None:
            # Get or create a default profile
            profiles = profile_registry.list_profiles(db_path=db_path)
            profile_id = profiles[0].id if profiles else 1
            try:
                existing = profile_registry.add_account(
                    profile_id=profile_id,
                    platform=platform,
                    account_name=account,
                    auth_type="cookie",
                    db_path=db_path,
                )
            except Exception as create_exc:
                logging.getLogger(__name__).warning("add_account failed (will retry lookup): %s", create_exc)
                # Account may have been created by a concurrent request
                all_accounts = profile_registry.list_accounts(db_path=db_path)
                for a in all_accounts:
                    if a.platform == platform and a.account_name == account:
                        existing = a
                        break

        if existing is None:
            logging.getLogger(__name__).error("accounts_import_cookies: could not create or find account platform=%s account=%s", platform, account)
            return jsonify({"code": 500, "msg": "Failed to create or find account", "data": None}), 500

        # Import cookies
        result = _import_cookies_for_account(platform, account, profile, fmt, payload, db_path=db_path)
        # Update account cookie_path and status
        safe_name = f"{profile}__{platform}__{account}".replace("/", "_")
        cookie_path = Path(BASE_DIR / "cookiesFile" / f"{safe_name}.cookie")
        profile_registry.update_account(
            existing.id,
            cookie_path=str(cookie_path),
            status=1,
            db_path=db_path,
        )
        result["accountId"] = existing.id
        return jsonify({"code": 200, "data": result, "msg": "ok"})
    except Exception as exc:
        import traceback
        logging.getLogger(__name__).error("Cookie import failed: %s\n%s", exc, traceback.format_exc())
        return jsonify({"code": 500, "msg": str(exc), "data": None}), 500


@app.route("/api/auth/cookies/import", methods=["POST"])
def api_cookies_import():
    """Import cookies for an account (JSON or Netscape format)."""
    b = request.get_json(force=True) or {}
    platform = b.get("platform")
    account = b.get("account") or "imported"
    profile = b.get("profile") or "default"
    fmt = b.get("format", "json")
    payload = b.get("payload", "")
    if not platform:
        return jsonify(error="platform is required"), 400
    try:
        result = _import_cookies_for_account(platform, account, profile, fmt, payload, db_path=_current_db_path())
        return jsonify({"code": 200, "data": result, "msg": "ok"})
    except Exception as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400

def _database_readiness_check():
    """Readiness probe: confirm the legacy SQLite database is reachable.

    Side-effect-free — if the database file does not exist yet (fresh install,
    bootstrapped lazily on the first request) the instance is still considered
    ready. When the file exists we open it and run a trivial query.
    """

    path = _get_legacy_db_path()
    if not path.exists():
        return ("uninitialized", True)
    conn = sqlite3.connect(str(path), timeout=2)
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return ("ok", True)


register_readiness_check(app, "database", _database_readiness_check)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5409, threaded=True)
