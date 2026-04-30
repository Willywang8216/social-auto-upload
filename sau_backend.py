# Heal a possibly-incomplete user-supplied ``conf.py`` before any other
# module imports ``conf``. A stripped-down ``conf.py`` (e.g. one that
# defines only ``LOCAL_CHROME_PATH`` and gets bind-mounted into a
# container over the shipped one) used to crash every consumer with
# ``ImportError: cannot import name 'BASE_DIR' from 'conf'``. The backfill
# is idempotent and never overwrites an attribute the user explicitly set.
from conf_defaults import apply_conf_defaults
apply_conf_defaults()

import asyncio
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from queue import Queue
from flask_cors import CORS
from myUtils.auth import check_cookie
from flask import Flask, request, jsonify, Response, render_template, send_from_directory
from conf import BASE_DIR
from myUtils import jobs as job_runtime
from myUtils.login import get_tencent_cookie, douyin_cookie_gen, get_ks_cookie, xiaohongshu_cookie_gen
from myUtils.postVideo import post_video_tencent, post_video_DouYin, post_video_ks, post_video_xhs
from myUtils.security import (
    extract_bearer_token,
    load_policy,
)
from myUtils.worker import default_executor, run_worker_drain
from utils.files_times import generate_schedule_time_next_day

# Map the legacy numeric `type` field to the platform slug used by the job runtime.
LEGACY_PLATFORM_CODES = {1: "xiaohongshu", 2: "tencent", 3: "douyin", 4: "kuaishou"}

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
        # 使用 with 自动管理数据库连接
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
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
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
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
        # 获取数据库连接
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 查询要删除的记录
            cursor.execute("SELECT * FROM user_info WHERE id = ?", (account_id,))
            record = cursor.fetchone()

            if not record:
                return jsonify({
                    "code": 404,
                    "msg": "account not found",
                    "data": None
                }), 404

            record = dict(record)

            # 删除关联的cookie文件
            if record.get('filePath'):
                cookie_file_path = Path(BASE_DIR / "cookiesFile" / record['filePath'])
                if cookie_file_path.exists():
                    try:
                        cookie_file_path.unlink()
                        print(f"✅ Cookie文件已删除: {cookie_file_path}")
                    except Exception as e:
                        print(f"⚠️ 删除Cookie文件失败: {e}")

            # 删除数据库记录
            cursor.execute("DELETE FROM user_info WHERE id = ?", (account_id,))
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
        match type:
            case 1:
                post_video_xhs(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                                   start_days)
            case 2:
                post_video_tencent(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                                   start_days, is_draft)
            case 3:
                post_video_DouYin(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                          start_days, thumbnail_path, productLink, productTitle)
            case 4:
                post_video_ks(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                          start_days)
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
            match type:
                case 1:
                    post_video_xhs(title, file_list, tags, account_list, category, enableTimer,
                                   videos_per_day, daily_times, start_days)
                case 2:
                    post_video_tencent(title, file_list, tags, account_list, category, enableTimer,
                                       videos_per_day, daily_times, start_days, is_draft)
                case 3:
                    # NOTE: keyword args used here on purpose. The earlier positional
                    # call dropped `thumbnail_path` and silently bound `productLink`
                    # to the thumbnail parameter.
                    post_video_DouYin(title, file_list, tags, account_list,
                                      category=category, enableTimer=enableTimer,
                                      videos_per_day=videos_per_day, daily_times=daily_times,
                                      start_days=start_days,
                                      thumbnail_path=thumbnail_path,
                                      productLink=productLink, productTitle=productTitle)
                case 4:
                    post_video_ks(title, file_list, tags, account_list, category, enableTimer,
                                  videos_per_day, daily_times, start_days)
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

        # 从数据库获取账号的文件路径
        with sqlite3.connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT filePath FROM user_info WHERE id = ?', (account_id,))
            result = cursor.fetchone()

        if not result:
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
        cookie_file_path = Path(BASE_DIR / "cookiesFile" / result['filePath'])
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

        # 验证文件路径的安全性，防止路径遍历攻击
        cookie_file_path = Path(BASE_DIR / "cookiesFile" / file_path).resolve()
        base_path = Path(BASE_DIR / "cookiesFile").resolve()

        if not cookie_file_path.is_relative_to(base_path):
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


def _normalise_publish_payload(data: dict) -> tuple[str, dict, list[tuple[str, str, datetime | None]]]:
    """Pull a /postVideo-shaped payload apart into (platform, payload, targets).

    Accepts either the legacy numeric ``type`` field or an explicit ``platform``
    string slug. Targets are the cartesian product of fileList × accountList,
    with optional schedule times derived from the timer fields.
    """

    if "platform" in data and isinstance(data["platform"], str):
        platform = data["platform"]
    else:
        platform_code = data.get("type")
        if platform_code not in LEGACY_PLATFORM_CODES:
            raise ValueError(f"Unsupported platform code: {platform_code!r}")
        platform = LEGACY_PLATFORM_CODES[platform_code]

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

    targets: list[tuple[str, str, datetime | None]] = []
    for index, file_ref in enumerate(file_list):
        scheduled = schedules[index] if index < len(schedules) else None
        for account_ref in account_list:
            targets.append((account_ref, file_ref, scheduled))

    return platform, payload, targets


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
