import asyncio
import hashlib
import hmac
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from queue import Queue
from flask_cors import CORS
from myUtils.auth import check_cookie
from myUtils import account_validation
from flask import Flask, request, jsonify, Response, render_template, send_from_directory
from conf import BASE_DIR
from myUtils import campaigns as campaign_store
from myUtils import content_rules
from myUtils import google_sheets
from myUtils import jobs as job_runtime
from myUtils import llm_client
from myUtils import media_groups as media_group_store
from myUtils import media_pipeline
from myUtils import profiles as profile_registry
from myUtils import prepared_publishers
from myUtils import rclone_storage
from myUtils import tiktok_auth
from myUtils import tiktok_review
from myUtils.login import get_tencent_cookie, douyin_cookie_gen, get_ks_cookie, xiaohongshu_cookie_gen
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

# Map the legacy numeric `type` field to the platform slug used by the job runtime.
LEGACY_PLATFORM_CODES = {
    1: "xiaohongshu",
    2: "tencent",
    3: "douyin",
    4: "kuaishou",
    7: "twitter",
}
TWITTER_PLATFORM_ALIASES = {"twitter", "x"}

active_queues = {}
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


def _tiktok_callback_base_url() -> str:
    configured = str(os.environ.get("SAU_TIKTOK_CALLBACK_URL") or "").strip()
    if configured:
        return configured.rstrip("/")
    return "https://up.iamwillywang.com/oauth/tiktok/callback"


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
app.config['MAX_CONTENT_LENGTH'] = 160 * 1024 * 1024

# 获取当前目录（假设 index.html 和 assets 在这里）
current_dir = os.path.dirname(os.path.abspath(__file__))

# 处理所有静态资源请求（未来打包用）
@app.route('/assets/<filename>')
def custom_static(filename):
    return send_from_directory(os.path.join(current_dir, 'assets'), filename)

# 处理 favicon.ico 静态资源（未来打包用）
@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(current_dir, 'assets'), 'vite.svg')

@app.route('/vite.svg')
def vite_svg():
    return send_from_directory(os.path.join(current_dir, 'assets'), 'vite.svg')

# （未来打包用）
@app.route('/')
def index():  # put application's code here
    return send_from_directory(current_dir, 'index.html')

@app.route('/oauth/tiktok/callback', methods=['GET'])
@app.route('/oauth/tiktok/callback/', methods=['GET'])
def oauth_tiktok_callback():
    error = request.args.get('error')
    error_description = request.args.get('error_description')
    code = request.args.get('code')
    state = request.args.get('state', '')
    if error:
        return Response(
            f"""
            <html><body style='font-family: sans-serif; padding: 24px;'>
            <h1>TikTok OAuth failed</h1>
            <p><strong>Error:</strong> {error}</p>
            <p>{error_description or ''}</p>
            </body></html>
            """,
            mimetype='text/html',
        )
    if not code:
        return jsonify({"code": 400, "msg": "missing code", "data": None}), 400

    payload = {
        "client_key": _tiktok_client_key(),
        "client_secret": _tiktok_client_secret(),
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": _tiktok_callback_base_url(),
    }
    token_result = None
    token_error = None
    if payload["client_key"] and payload["client_secret"]:
        try:
            import requests
            response = requests.post(
                "https://open.tiktokapis.com/v2/oauth/token/",
                data=payload,
                timeout=120,
            )
            response.raise_for_status()
            token_result = response.json()
        except Exception as exc:  # noqa: BLE001
            token_error = str(exc)

    body = {
        "received": True,
        "state": state,
        "code": code,
        "token": token_result,
        "tokenError": token_error,
    }
    return Response(
        f"""
        <html><body style='font-family: sans-serif; padding: 24px;'>
        <h1>TikTok OAuth callback received</h1>
        <p>The authorization code was received successfully for <strong>up.iamwillywang.com</strong>.</p>
        <pre style='white-space: pre-wrap; background: #f5f5f5; padding: 12px; border-radius: 8px;'>{json.dumps(body, ensure_ascii=False, indent=2)}</pre>
        </body></html>
        """,
        mimetype='text/html',
    )


@app.route('/webhooks/tiktok', methods=['GET', 'POST'])
@app.route('/webhooks/tiktok/', methods=['GET', 'POST'])
def webhook_tiktok():
    if request.method == 'GET':
        return jsonify({
            "code": 200,
            "msg": "ok",
            "data": {
                "service": "tiktok-webhook",
                "status": "ready",
            }
        }), 200

    raw_body = request.get_data(cache=False)
    signature_header = request.headers.get('Tiktok-Signature') or request.headers.get('TikTok-Signature')
    signature_valid, signature_reason = _verify_tiktok_signature(raw_body, signature_header)
    try:
        parsed_body = request.get_json(silent=True)
    except Exception:  # noqa: BLE001
        parsed_body = None

    event = {
        "received_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "path": request.path,
        "signature_valid": signature_valid,
        "signature_reason": signature_reason,
        "headers": {
            "Tiktok-Signature": signature_header or "",
            "User-Agent": request.headers.get('User-Agent', ''),
            "Content-Type": request.headers.get('Content-Type', ''),
        },
        "body": parsed_body if parsed_body is not None else raw_body.decode('utf-8', errors='replace'),
    }
    _append_tiktok_webhook_event(event)

    if signature_header and not signature_valid and _tiktok_client_secret():
        return jsonify({"code": 401, "msg": f"invalid webhook signature: {signature_reason}", "data": None}), 401

    return jsonify({
        "code": 200,
        "msg": "accepted",
        "data": {
            "signatureValid": signature_valid,
            "signatureReason": signature_reason,
        }
    }), 200


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
        # UUIDv4 — random, no MAC/timestamp leak. UUIDv1 (the legacy choice
        # here) embeds the host MAC address and creation time in the
        # filename, which the upload directory exposes via /getFile.
        file_uuid = uuid.uuid4()
        filepath = Path(BASE_DIR / "videoFile" / f"{file_uuid}_{file.filename}")
        file.save(filepath)
        return jsonify({"code": 200, "msg": "File uploaded successfully",
                        "data": f"{file_uuid}_{file.filename}"}), 200
    except Exception as e:
        return jsonify({"code": 500, "msg": str(e), "data": None}), 500

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

    # 获取表单中的自定义文件名（可选）
    custom_filename = request.form.get('filename', None)
    if custom_filename:
        filename = custom_filename + "." + file.filename.split('.')[-1]
    else:
        filename = file.filename

    try:
        # UUIDv4 — see /upload for the rationale (no MAC/timestamp leak).
        file_uuid = uuid.uuid4()
        final_filename = f"{file_uuid}_{filename}"
        filepath = Path(BASE_DIR / "videoFile" / final_filename)

        # 保存文件
        file.save(filepath)

        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                                INSERT INTO file_records (filename, filesize, file_path)
            VALUES (?, ?, ?)
                                ''', (filename, round(float(os.path.getsize(filepath)) / (1024 * 1024),2), final_filename))
            conn.commit()
            print("✅ 上传文件已记录")

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

            # 获取文件路径并删除实际文件
            file_path = Path(BASE_DIR / "videoFile" / record['file_path'])
            if file_path.exists():
                try:
                    file_path.unlink()  # 删除文件
                    print(f"✅ 实际文件已删除: {file_path}")
                except Exception as e:
                    print(f"⚠️ 删除实际文件失败: {e}")
                    # 即使删除文件失败，也要继续删除数据库记录，避免数据不一致
            else:
                print(f"⚠️ 实际文件不存在: {file_path}")

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
                "code": 500,
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


# Cookie文件下载API
@app.route('/downloadCookie', methods=['GET'])
def download_cookie():
    try:
        file_path = request.args.get('filePath')
        if not file_path:
            return jsonify({
                "code": 500,
                "msg": "缺少文件路径参数",
                "data": None
            }), 400

        cookie_file_path = _resolve_cookie_path(file_path)
        if not _cookie_path_is_allowed(cookie_file_path):
            return jsonify({
                "code": 500,
                "msg": "非法文件路径",
                "data": None
            }), 400

        if not cookie_file_path.exists():
            return jsonify({
                "code": 500,
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


def _profile_payload(profile: profile_registry.Profile) -> dict:
    return profile.to_dict()


def _account_payload(account: profile_registry.Account) -> dict:
    return account.to_dict()


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
        return [artifact for artifact in artifacts if artifact.get("artifact_kind") != "raw_remote_upload"]

    grouped: dict[tuple[object, object], list[dict]] = {}
    passthrough: list[dict] = []
    for artifact in artifacts:
        kind = artifact.get("artifact_kind") or ""
        source_id = artifact.get("source_file_record_id")
        if kind in {"remote_upload", "raw_remote_upload"} and source_id is not None:
            role = ((artifact.get("metadata") or {}).get("role"))
            grouped.setdefault((source_id, role), []).append(artifact)
        elif kind not in {"watermarked_image", "watermarked_video"}:
            passthrough.append(artifact)

    selected = []
    for items in grouped.values():
        raw = next((item for item in items if item.get("artifact_kind") == "raw_remote_upload"), None)
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


def _oauth_request_to_status(request_state: tiktok_review.TikTokOAuthRequest | None) -> dict | None:
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
    if request_state.account_id is not None:
        payload.setdefault('accountId', request_state.account_id)
    if request_state.account_name:
        payload.setdefault('accountName', request_state.account_name)
    return payload


def _validate_account_payload(data: dict, *, db_path: Path, profile_id: int | None = None, perform_live_checks: bool = False):
    platform = str(data.get("platform", "") or "").strip().lower()
    auth_type = str(data.get("authType", "cookie") or "cookie")
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


def _ensure_file_record_for_path(file_path: str, *, db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM file_records WHERE file_path = ?",
            (file_path,),
        ).fetchone()
        if row is not None:
            return int(row[0])

        absolute_path = Path(BASE_DIR) / "videoFile" / file_path
        filename = Path(file_path).name
        filesize = None
        if absolute_path.exists():
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
        return {"text": watermark.strip()}
    if isinstance(watermark, dict):
        return dict(watermark)
    return {}


def _prepare_campaign_media_artifacts(
    campaign_id: int,
    profile: profile_registry.Profile,
    media_files: list[dict],
    request_data: dict,
    *,
    db_path: Path,
) -> dict:
    upload_to_remote = bool(
        request_data.get("uploadToRemote", os.environ.get("SAU_DEFAULT_RCLONE_REMOTE"))
    )
    watermark_spec = _derive_watermark_spec(profile, request_data)
    artifacts_context = {
        "imageUrls": [],
        "videoUrl": "",
        "imageLocalPaths": [],
        "videoLocalPath": "",
        "rawImageUrls": [],
        "rawVideoUrl": "",
        "rawImageLocalPaths": [],
        "rawVideoLocalPath": "",
        "transcriptText": str(request_data.get("transcriptText", "") or "").strip(),
    }

    for media_file in media_files:
        source_path = Path(media_file["file_path"]).expanduser().resolve()
        publish_path = source_path
        artifact_kind = None

        if watermark_spec and _is_image_file(source_path):
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
            )
        elif watermark_spec and _is_video_file(source_path):
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

        public_url = None
        raw_public_url = None
        if upload_to_remote:
            remote_artifact = rclone_storage.upload_artifact(
                publish_path,
                campaign_id=campaign_id,
                artifact_subdir="videos" if _is_video_file(publish_path) else "images",
            )
            public_url = remote_artifact.public_url
            campaign_store.add_campaign_artifact(
                campaign_id,
                source_file_record_id=media_file["file_record_id"],
                artifact_kind="remote_upload",
                local_path=str(publish_path),
                public_url=remote_artifact.public_url,
                remote_path=remote_artifact.remote_path,
                metadata={"role": media_file["role"]},
                db_path=db_path,
            )
            if publish_path != source_path:
                raw_remote_artifact = rclone_storage.upload_artifact(
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

    should_transcribe = bool(
        request_data.get("transcribe", False)
        or (
            not artifacts_context["transcriptText"]
            and os.environ.get("SAU_LLM_API_KEY")
            and os.environ.get("SAU_LLM_API_BASE_URL")
        )
    )
    primary_video = next((item for item in media_files if item["role"] == "video"), None)
    if should_transcribe and primary_video is not None and not artifacts_context["transcriptText"]:
        source_path = Path(primary_video["file_path"]).expanduser().resolve()
        audio_path = media_pipeline.prepare_campaign_artifact_path(
            campaign_id,
            source_path,
            artifact_kind="audio",
            suffix=".wav",
        )
        media_pipeline.extract_video_audio(source_path, audio_path)
        transcript = llm_client.transcribe_audio(audio_path)
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
    user_prompt = "\n".join(
        [
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
            "Return JSON with keys: message, hashtags, firstComment, contactDetails, cta.",
        ]
    )
    return system_prompt, user_prompt


def _generate_platform_draft(
    platform: str,
    profile: profile_registry.Profile,
    media_group: media_group_store.MediaGroup,
    request_data: dict,
    media_context: dict,
) -> dict:
    should_use_llm = bool(
        request_data.get("useLlm", True)
        and os.environ.get("SAU_LLM_API_KEY")
        and os.environ.get("SAU_LLM_API_BASE_URL")
    )

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
            result = llm_client.generate_chat_completion(system_prompt, user_prompt)
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


def _default_sheet_title(profile: profile_registry.Profile) -> str:
    return f"{datetime.now().strftime('%Y-%m-%d')}-{profile.slug}"


@app.route('/oauth/tiktok/start', methods=['POST'])
def tiktok_oauth_start():
    db_path = _current_db_path()
    try:
        data = _read_json_body()
        state_token = tiktok_auth.build_state_token()
        redirect_uri = str(data.get('redirectUri') or tiktok_auth.default_redirect_uri()).strip()
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
        access_token = str(token_payload.get('access_token') or '')
        refresh_token = str(token_payload.get('refresh_token') or '')
        user_info = tiktok_auth.fetch_user_info(access_token=access_token) if access_token else {}
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
            result=persisted_callback_payload,
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
        html = f"""<html><body><script>
        if (window.opener) {{
          window.opener.postMessage({{ type: 'sau:tiktok-oauth', ok: true, data: {json.dumps(callback_payload, ensure_ascii=False)} }}, '*');
        }}
        window.close();
        </script><p>TikTok authorization completed. You may close this window.</p></body></html>"""
        return Response(html, mimetype='text/html')
    except Exception as exc:  # noqa: BLE001
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
        return jsonify({'code': 200, 'msg': 'ok', 'data': {'path': '/webhooks/tiktok'}}), 200

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


@app.route('/admin/tiktok/status', methods=['GET'])
def tiktok_admin_status():
    db_path = _current_db_path()
    raw_account_id = request.args.get('accountId')
    account_id = int(raw_account_id) if raw_account_id and str(raw_account_id).isdigit() else None
    return jsonify({
        'code': 200,
        'msg': 'ok',
        'data': {
            'domain': 'up.iamwillywang.com',
            'redirectUri': tiktok_auth.default_redirect_uri(),
            'webhookUri': 'https://up.iamwillywang.com/webhooks/tiktok',
            'selectedProducts': ['Login Kit for Web', 'Content Posting API', 'Webhooks'],
            'selectedScopes': ['user.info.basic', 'video.publish'],
            'accountId': account_id,
            'lastRequest': _oauth_request_to_status(tiktok_review.latest_oauth_request(account_id=account_id, db_path=db_path)),
            'lastCallback': _event_payload_to_status(tiktok_review.latest_review_event('callback', account_id=account_id, db_path=db_path)),
            'lastRefresh': _event_payload_to_status(tiktok_review.latest_review_event('refresh', account_id=account_id, db_path=db_path)),
            'lastWebhook': _event_payload_to_status(tiktok_review.latest_review_event('webhook', db_path=db_path)),
            'recentEvents': [_event_payload_to_status(event) for event in tiktok_review.list_recent_review_events(account_id=account_id, db_path=db_path)],
        },
    }), 200


@app.route("/profiles", methods=["GET"])
def profiles_list():
    db_path = _current_db_path()
    items = profile_registry.list_profiles(db_path=db_path)
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
            db_path=_current_db_path(),
        )
    except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    return jsonify({"code": 200, "msg": "created", "data": _profile_payload(profile)}), 200


@app.route("/profiles/<int:profile_id>", methods=["GET"])
def profiles_get(profile_id):
    try:
        profile = profile_registry.get_profile(profile_id, db_path=_current_db_path())
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
            db_path=_current_db_path(),
        )
    except LookupError:
        return jsonify({"code": 404, "msg": "Profile not found", "data": None}), 404
    except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    return jsonify({"code": 200, "msg": "updated", "data": _profile_payload(profile)}), 200


@app.route("/profiles/<int:profile_id>/accounts", methods=["GET"])
def profile_accounts_list(profile_id):
    db_path = _current_db_path()
    try:
        profile_registry.get_profile(profile_id, db_path=db_path)
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
        validation = _validate_account_payload(data, db_path=_current_db_path(), profile_id=profile_id)
        if not validation.valid:
            raise ValueError("; ".join(validation.errors))
        account = profile_registry.add_account(
            profile_id,
            platform,
            account_name,
            cookie_path=data.get("cookiePath"),
            auth_type=str(data.get("authType", "cookie") or "cookie"),
            config=data.get("config") if isinstance(data.get("config"), dict) else None,
            enabled=bool(data.get("enabled", True)),
            status=int(data.get("status", 0) or 0),
            db_path=_current_db_path(),
        )
    except LookupError:
        return jsonify({"code": 404, "msg": "Profile not found", "data": None}), 404
    except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    return jsonify({"code": 200, "msg": "created", "data": _account_payload(account)}), 200


@app.route("/accounts/<int:account_id>/check-connection", methods=["POST"])
def accounts_check_connection(account_id):
    db_path = _current_db_path()
    try:
        account = profile_registry.get_account(account_id, db_path=db_path)
    except LookupError:
        return jsonify({"code": 404, "msg": "Account not found", "data": None}), 404

    config = dict(account.config or {})
    now = datetime.now().isoformat(timespec='seconds')

    try:
        if account.platform == profile_registry.PLATFORM_FACEBOOK:
            result = prepared_publishers.validate_facebook_config_live(config)
            config['facebookPageName'] = result.get('name', config.get('facebookPageName', ''))
        elif account.platform == profile_registry.PLATFORM_INSTAGRAM:
            result = prepared_publishers.validate_instagram_config_live(config)
            config['instagramUserName'] = result.get('username', config.get('instagramUserName', ''))
        elif account.platform == profile_registry.PLATFORM_THREADS:
            result = prepared_publishers.validate_threads_config_live(config)
            config['threadsUserName'] = result.get('username', config.get('threadsUserName', ''))
        else:
            return jsonify({"code": 400, "msg": "Connection check is implemented only for Facebook, Instagram, and Threads", "data": None}), 400

        config['lastConnectionCheckAt'] = now
        updated = profile_registry.update_account(
            account_id,
            config=config,
            auth_type=account.auth_type,
            db_path=db_path,
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400

    return jsonify({"code": 200, "msg": "checked", "data": _account_payload(updated)}), 200


@app.route("/accounts/<int:account_id>/refresh-token", methods=["POST"])
def accounts_refresh_token(account_id):
    db_path = _current_db_path()
    try:
        account = profile_registry.get_account(account_id, db_path=db_path)
    except LookupError:
        return jsonify({"code": 404, "msg": "Account not found", "data": None}), 404

    config = dict(account.config or {})
    now = datetime.now().isoformat(timespec='seconds')

    try:
        if account.platform == profile_registry.PLATFORM_TIKTOK:
            refresh_token = str(config.get('refreshToken') or '').strip()
            if not refresh_token:
                return jsonify({"code": 400, "msg": "TikTok account is missing refreshToken", "data": None}), 400
            token_payload = tiktok_auth.refresh_access_token(refresh_token=refresh_token)
            access_token = str(token_payload.get('access_token') or '')
            user_info = tiktok_auth.fetch_user_info(access_token=access_token) if access_token else {}
            config = prepared_publishers._apply_tiktok_token_payload(config, token_payload, user_info)
            config.update({
                'openId': token_payload.get('open_id') or config.get('openId') or '',
                'scope': token_payload.get('scope') or config.get('scope') or '',
                'displayName': user_info.get('data', {}).get('user', {}).get('display_name') or config.get('displayName') or '',
                'avatarUrl': user_info.get('data', {}).get('user', {}).get('avatar_url') or config.get('avatarUrl') or '',
                'lastManualRefreshAt': now,
            })
            updated = profile_registry.update_account(
                account_id,
                config=config,
                auth_type='oauth',
                db_path=db_path,
            )
            _append_tiktok_review_event(
                'refresh',
                {
                    'status': 'ok',
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
                db_path=db_path,
            )
            return jsonify({"code": 200, "msg": "refreshed", "data": _account_payload(updated)}), 200

        if account.platform == profile_registry.PLATFORM_REDDIT:
            refreshed = prepared_publishers.refresh_reddit_access_token(config)
            config.update({
                'accessToken': refreshed['access_token'],
                'scope': refreshed.get('scope', config.get('scope', '')),
                'accessTokenUpdatedAt': now,
                'lastManualRefreshAt': now,
                'redditUserName': refreshed.get('me', {}).get('name', config.get('redditUserName', '')),
            })
            expires_in = refreshed.get('expires_in')
            if expires_in:
                config['accessTokenExpiresAt'] = (
                    datetime.now() + timedelta(seconds=int(expires_in))
                ).isoformat(timespec='seconds')
            updated = profile_registry.update_account(
                account_id,
                config=config,
                auth_type='oauth',
                db_path=db_path,
            )
            return jsonify({"code": 200, "msg": "refreshed", "data": _account_payload(updated)}), 200

        if account.platform == profile_registry.PLATFORM_YOUTUBE:
            refreshed = prepared_publishers.refresh_youtube_access_token(config)
            config.update({
                'accessToken': refreshed['access_token'],
                'accessTokenUpdatedAt': now,
                'lastManualRefreshAt': now,
            })
            expires_in = refreshed.get('expires_in')
            if expires_in:
                config['accessTokenExpiresAt'] = (
                    datetime.now() + timedelta(seconds=int(expires_in))
                ).isoformat(timespec='seconds')
            channel_items = refreshed.get('channel', {}).get('items', []) if isinstance(refreshed.get('channel'), dict) else []
            if channel_items:
                snippet = channel_items[0].get('snippet', {}) if isinstance(channel_items[0], dict) else {}
                config['channelTitle'] = snippet.get('title', config.get('channelTitle', ''))
            updated = profile_registry.update_account(
                account_id,
                config=config,
                auth_type='oauth',
                db_path=db_path,
            )
            return jsonify({"code": 200, "msg": "refreshed", "data": _account_payload(updated)}), 200

        return jsonify({"code": 400, "msg": "Refresh is implemented only for TikTok, Reddit, and YouTube", "data": None}), 400
    except Exception as exc:  # noqa: BLE001
        if account.platform == profile_registry.PLATFORM_TIKTOK:
            _append_tiktok_review_event(
                'refresh',
                {'status': 'error', 'accountId': account_id, 'accountName': account.account_name, 'error': str(exc)},
                account_id=account_id,
                account_name=account.account_name,
                status='error',
                db_path=db_path,
            )
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400


@app.route("/accounts/tiktok/refresh-stale", methods=["POST"])
def accounts_refresh_stale_tiktok_tokens():
    db_path = _current_db_path()
    data = request.get_json(silent=True) or {}
    raw_account_id = data.get('accountId')
    raw_profile_id = data.get('profileId')
    dry_run = bool(data.get('dryRun', False))
    expiring_within_seconds = int(data.get('expiringWithinSeconds', 300) or 300)
    max_accounts = max(1, min(int(data.get('maxAccounts', 50) or 50), 200))

    account_id = int(raw_account_id) if raw_account_id not in (None, '') else None
    profile_id = int(raw_profile_id) if raw_profile_id not in (None, '') else None

    accounts = profile_registry.list_accounts(
        profile_id=profile_id,
        platform=profile_registry.PLATFORM_TIKTOK,
        enabled=True,
        db_path=db_path,
    )
    if account_id is not None:
        accounts = [account for account in accounts if account.id == account_id]

    results = []
    refreshed = 0
    stale = 0
    skipped = 0
    examined = 0

    for account in accounts[:max_accounts]:
        examined += 1
        config = dict(account.config or {})
        is_stale = prepared_publishers._is_tiktok_access_token_stale(
            config,
            skew_seconds=expiring_within_seconds,
        )
        if not is_stale:
            skipped += 1
            results.append({
                'accountId': account.id,
                'accountName': account.account_name,
                'status': 'up_to_date',
            })
            continue
        stale += 1
        refresh_token = str(config.get('refreshToken') or '').strip()
        if not refresh_token:
            skipped += 1
            results.append({
                'accountId': account.id,
                'accountName': account.account_name,
                'status': 'missing_refresh_token',
            })
            continue
        if dry_run:
            results.append({
                'accountId': account.id,
                'accountName': account.account_name,
                'status': 'would_refresh',
            })
            continue
        try:
            token_payload = tiktok_auth.refresh_access_token(refresh_token=refresh_token)
            access_token = str(token_payload.get('access_token') or '')
            user_info = tiktok_auth.fetch_user_info(access_token=access_token) if access_token else {}
            updated_config = prepared_publishers._apply_tiktok_token_payload(config, token_payload, user_info)
            updated_config['lastAutoRefreshAt'] = datetime.now().isoformat(timespec='seconds')
            updated = profile_registry.update_account(
                account.id,
                config=updated_config,
                auth_type='oauth',
                db_path=db_path,
            )
            refreshed += 1
            _append_tiktok_review_event(
                'refresh',
                {
                    'status': 'ok',
                    'mode': 'auto',
                    'accountId': updated.id,
                    'accountName': updated.account_name,
                    'openId': updated_config.get('openId', ''),
                    'scope': updated_config.get('scope', ''),
                    'displayName': updated_config.get('displayName', ''),
                    'avatarUrl': updated_config.get('avatarUrl', ''),
                },
                account_id=updated.id,
                account_name=updated.account_name,
                status='ok',
                metadata={'mode': 'auto'},
                db_path=db_path,
            )
            results.append({
                'accountId': updated.id,
                'accountName': updated.account_name,
                'status': 'refreshed',
            })
        except Exception as exc:  # noqa: BLE001
            skipped += 1
            _append_tiktok_review_event(
                'refresh',
                {
                    'status': 'error',
                    'mode': 'auto',
                    'accountId': account.id,
                    'accountName': account.account_name,
                    'error': str(exc),
                },
                account_id=account.id,
                account_name=account.account_name,
                status='error',
                metadata={'mode': 'auto'},
                db_path=db_path,
            )
            results.append({
                'accountId': account.id,
                'accountName': account.account_name,
                'status': 'error',
                'error': str(exc),
            })

    return jsonify({
        'code': 200,
        'msg': 'ok',
        'data': {
            'dryRun': dry_run,
            'examined': examined,
            'stale': stale,
            'refreshed': refreshed,
            'skipped': skipped,
            'results': results,
        },
    }), 200


@app.route("/accounts/<int:account_id>", methods=["PATCH"])
def accounts_patch(account_id):
    try:
        data = _read_json_body()
        existing = profile_registry.get_account(account_id, db_path=_current_db_path())
        merged = {
            "platform": existing.platform,
            "authType": data.get("authType", existing.auth_type),
            "config": data.get("config") if isinstance(data.get("config"), dict) else (existing.config or {}),
            "cookiePath": data.get("cookiePath", existing.cookie_path),
        }
        validation = _validate_account_payload(merged, db_path=_current_db_path(), profile_id=existing.profile_id)
        if not validation.valid:
            raise ValueError("; ".join(validation.errors))
        account = profile_registry.update_account(
            account_id,
            account_name=data.get("accountName"),
            cookie_path=data.get("cookiePath"),
            auth_type=data.get("authType"),
            config=data.get("config") if isinstance(data.get("config"), dict) else None,
            enabled=data.get("enabled"),
            status=data.get("status"),
            db_path=_current_db_path(),
        )
    except LookupError:
        return jsonify({"code": 404, "msg": "Account not found", "data": None}), 404
    except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    return jsonify({"code": 200, "msg": "updated", "data": _account_payload(account)}), 200


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
    groups = media_group_store.list_media_groups(db_path=db_path)
    data = []
    for group in groups:
        items = media_group_store.list_media_group_items(group.id, db_path=db_path)
        data.append(_media_group_payload(group, items=items))
    return jsonify({"code": 200, "msg": "ok", "data": data}), 200


@app.route("/media-groups/<int:media_group_id>", methods=["GET"])
def media_groups_get(media_group_id):
    db_path = _current_db_path()
    try:
        group = media_group_store.get_media_group(media_group_id, db_path=db_path)
    except LookupError:
        return jsonify({"code": 404, "msg": "Media group not found", "data": None}), 404
    items = media_group_store.list_media_group_items(media_group_id, db_path=db_path)
    return jsonify(
        {"code": 200, "msg": "ok", "data": _media_group_payload(group, items=items)}
    ), 200


@app.route("/campaigns/prepare", methods=["POST"])
def campaigns_prepare():
    db_path = _current_db_path()
    try:
        data = _read_json_body()
        profile_id = int(data.get("profileId"))
        media_group_id = int(data.get("mediaGroupId"))
        profile = profile_registry.get_profile(profile_id, db_path=db_path)
        media_group = media_group_store.get_media_group(media_group_id, db_path=db_path)
        selected_account_ids = data.get("selectedAccountIds")
        if selected_account_ids is None:
            account_rows = profile_registry.list_accounts(
                profile_id=profile_id,
                enabled=True,
                db_path=db_path,
            )
        else:
            if not isinstance(selected_account_ids, list):
                raise ValueError("selectedAccountIds must be a list")
            account_rows = [
                profile_registry.get_account(int(account_id), db_path=db_path)
                for account_id in selected_account_ids
            ]
            account_rows = [
                account for account in account_rows
                if account.profile_id == profile_id and account.enabled
            ]
        if not account_rows:
            raise ValueError("No enabled accounts selected for this profile")

        watermark_spec = _derive_watermark_spec(profile, data)
        if any(account.platform == profile_registry.PLATFORM_TIKTOK for account in account_rows):
            upload_to_remote = bool(
                data.get("uploadToRemote", os.environ.get("SAU_DEFAULT_RCLONE_REMOTE"))
            )
            if not upload_to_remote:
                raise ValueError(
                    "TikTok direct post 需要可公開存取的媒體 URL；請啟用 OneDrive/rclone 上傳或提供可公開讀取的媒體來源"
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
            db_path=db_path,
        )

        media_files = _load_media_group_files(media_group_id, db_path=db_path)
        media_context = _prepare_campaign_media_artifacts(
            campaign.id,
            profile,
            media_files,
            data,
            db_path=db_path,
        )

        grouped_accounts: dict[str, list[profile_registry.Account]] = {}
        for account in account_rows:
            grouped_accounts.setdefault(account.platform, []).append(account)

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
        campaign = campaign_store.get_campaign(campaign_id, db_path=db_path)
    except LookupError:
        return jsonify({"code": 404, "msg": "Campaign not found", "data": None}), 404
    return jsonify(
        {"code": 200, "msg": "ok", "data": _campaign_payload(campaign, db_path=db_path)}
    ), 200


@app.route("/campaigns/<int:campaign_id>/posts/<int:post_id>", methods=["PATCH"])
def campaigns_posts_patch(campaign_id, post_id):
    db_path = _current_db_path()
    try:
        campaign_store.get_campaign(campaign_id, db_path=db_path)
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
    try:
        campaign = campaign_store.get_campaign(campaign_id, db_path=db_path)
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
        job = job_runtime.enqueue_job(spec)
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
        )
    except ValueError as exc:
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
    return jsonify({"code": 200, "msg": "ok",
                    "data": [_job_to_payload(item) for item in items]}), 200


@app.route("/jobs/<int:job_id>", methods=["GET"])
def jobs_get(job_id):
    try:
        job = job_runtime.get_job(job_id)
    except LookupError:
        return jsonify({"code": 404, "msg": "Job not found", "data": None}), 404
    targets = job_runtime.list_targets(job_id)
    body = _job_to_payload(job)
    body["targets"] = [_target_to_payload(target) for target in targets]
    return jsonify({"code": 200, "msg": "ok", "data": body}), 200


@app.route("/jobs/<int:job_id>/cancel", methods=["POST"])
def jobs_cancel(job_id):
    try:
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

if __name__ == '__main__':
    app.run(host='0.0.0.0' ,port=5409)
