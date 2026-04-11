import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from queue import Empty, Queue
from flask_cors import CORS
from flask import Flask, request, jsonify, Response, render_template, send_from_directory
from werkzeug.utils import secure_filename
from conf import BASE_DIR
from myUtils.login import get_tencent_cookie, douyin_cookie_gen, get_ks_cookie, xiaohongshu_cookie_gen
from myUtils.postVideo import post_video_tencent, post_video_DouYin, post_video_ks, post_video_xhs
from utils.profile_pipeline import (
    delete_profile as delete_profile_record,
    ensure_profile_tables,
    export_profiles_yaml,
    generate_profile_batch_content,
    generate_profile_content,
    get_google_service_account_config,
    get_profile_backup_config,
    get_profile_config_example_yaml,
    get_profile,
    import_profiles_yaml,
    list_profiles,
    migrate_uploaded_asset,
    preview_profiles_yaml_import,
    run_scheduled_profile_backup_if_due,
    run_profile_backup,
    save_profile,
    save_profile_backup_config,
    save_google_service_account_config,
    validate_google_sheet_connection,
)
from utils.direct_publishers import get_direct_publishers_config, save_direct_publishers_config
from utils.account_validation import (
    delete_validation_result,
    get_validation_result,
    get_validation_results,
    merge_account_validation,
    validate_account,
    validate_accounts,
)
from utils.publish_jobs import ensure_publish_job_tables
from utils.publish_jobs import (
    cancel_publish_job,
    complete_manual_publish_job,
    execute_due_publish_jobs,
    generate_publish_batch_drafts,
    get_publish_calendar_entries,
    list_publish_jobs,
    regenerate_publish_job_content,
    run_publish_job_now,
    save_publish_jobs,
    sync_publish_job_statuses,
    update_publish_job_content,
)
from utils.account_registry import (
    default_auth_mode_for_platform,
    ensure_account_tables,
    get_platform_config,
    merge_sensitive_account_metadata,
    normalize_platform_key,
    parse_metadata,
    platform_key_from_type,
    platform_type_from_key,
    sanitize_account_metadata,
    serialize_account_row,
)

active_queues = {}
app = Flask(__name__)
profile_backup_scheduler_started = False
publish_scheduler_started = False

#允许所有来源跨域访问
CORS(app)

# 限制上传文件大小为160MB
app.config['MAX_CONTENT_LENGTH'] = 160 * 1024 * 1024

# 获取当前目录（假设 index.html 和 assets 在这里）
current_dir = os.path.dirname(os.path.abspath(__file__))


def get_db_path():
    db_path = Path(BASE_DIR / "db" / "database.db")
    ensure_account_tables(db_path)
    ensure_profile_tables(db_path)
    ensure_publish_job_tables(db_path)
    return db_path


def _sanitize_storage_leaf_name(value: str, *, suffix: str | None = None, default_stem: str = "upload") -> str:
    raw_name = Path(str(value or "").strip()).name
    safe_name = secure_filename(raw_name)
    if suffix:
        normalized_suffix = suffix if suffix.startswith(".") else f".{suffix}"
        if Path(safe_name).suffix.lower() != normalized_suffix.lower():
            safe_name = f"{Path(safe_name).stem or default_stem}{normalized_suffix}"
    if not safe_name:
        fallback_suffix = suffix or ""
        safe_name = f"{default_stem}{fallback_suffix}"
    return safe_name


def _resolve_safe_child_path(base_dir: Path, relative_name: str) -> Path:
    candidate = (base_dir / _sanitize_storage_leaf_name(relative_name, default_stem="file")).resolve()
    base_path = base_dir.resolve()
    if not candidate.is_relative_to(base_path):
        raise ValueError("非法檔案路徑")
    return candidate


def _sanitize_account_response(account: dict) -> dict:
    if not isinstance(account, dict):
        return account
    sanitized = dict(account)
    sanitized["metadata"] = sanitize_account_metadata(
        sanitized.get("metadata") or {},
        sanitized.get("platformKey") or "",
        include_sensitive=False,
    )
    return sanitized


def start_profile_backup_scheduler() -> None:
    global profile_backup_scheduler_started
    if profile_backup_scheduler_started:
        return

    def backup_scheduler_loop():
        while True:
            try:
                run_scheduled_profile_backup_if_due(Path(BASE_DIR), get_db_path())
            except Exception as exc:
                print(f"排程備份失敗: {exc}")
            time.sleep(60)

    thread = threading.Thread(target=backup_scheduler_loop, daemon=True)
    thread.start()
    profile_backup_scheduler_started = True


def start_publish_scheduler() -> None:
    global publish_scheduler_started
    if publish_scheduler_started:
        return

    def publish_scheduler_loop():
        while True:
            try:
                execute_due_publish_jobs(get_db_path(), Path(BASE_DIR))
                sync_publish_job_statuses(get_db_path(), Path(BASE_DIR), limit=20)
            except Exception as exc:
                print(f"排程發布失敗: {exc}")
            time.sleep(60)

    thread = threading.Thread(target=publish_scheduler_loop, daemon=True)
    thread.start()
    publish_scheduler_started = True


def build_cookie_storage_name(platform_key: str) -> str:
    key = normalize_platform_key(platform_key) or "account"
    return f"{key}_{uuid.uuid4().hex}.json"

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
        # 保存文件到指定位置
        uuid_v1 = uuid.uuid1()
        print(f"UUID v1: {uuid_v1}")
        filepath = Path(BASE_DIR / "videoFile" / f"{uuid_v1}_{file.filename}")
        file.save(filepath)
        return jsonify({"code":200,"msg": "File uploaded successfully", "data": f"{uuid_v1}_{file.filename}"}), 200
    except Exception as e:
        return jsonify({"code":500,"msg": str(e),"data":None}), 500

@app.route('/getFile', methods=['GET'])
def get_file():
    # 获取 filename 参数
    filename = request.args.get('filename')

    if not filename:
        return jsonify({"code": 400, "msg": "filename is required", "data": None}), 400

    # 防止路径穿越攻击
    if '..' in filename or filename.startswith('/'):
        return jsonify({"code": 400, "msg": "Invalid filename", "data": None}), 400

    # 拼接完整路径
    file_path = str(Path(BASE_DIR / "videoFile"))

    # 返回文件
    return send_from_directory(file_path,filename)


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
    original_name = _sanitize_storage_leaf_name(file.filename, default_stem="upload")
    custom_filename = request.form.get('filename', None)
    if custom_filename:
        extension = Path(original_name).suffix
        filename = _sanitize_storage_leaf_name(custom_filename, suffix=extension or None, default_stem="upload")
    else:
        filename = original_name

    try:
        # 生成 UUID v1
        uuid_v1 = uuid.uuid1()
        print(f"UUID v1: {uuid_v1}")

        # 构造文件名和路径
        final_filename = f"{uuid_v1}_{filename}"
        filepath = _resolve_safe_child_path(Path(BASE_DIR / "videoFile"), f"{uuid_v1}_{filename}")

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
        return jsonify({
            "code": 500,
            "msg": str("get file failed!"),
            "data": None
        }), 500


@app.route('/getProfiles', methods=['GET'])
def get_profiles():
    try:
        data = list_profiles(get_db_path())
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": data
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/saveProfile', methods=['POST'])
def save_profile_route():
    data = request.get_json() or {}
    try:
        profile = save_profile(get_db_path(), data)
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": profile
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/deleteProfile', methods=['GET'])
def delete_profile_route():
    profile_id = request.args.get('id')
    if not profile_id or not profile_id.isdigit():
        return jsonify({
            "code": 400,
            "msg": "Invalid or missing profile ID",
            "data": None
        }), 400

    try:
        delete_profile_record(get_db_path(), int(profile_id))
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": None
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/exportProfilesYaml', methods=['GET'])
def export_profiles_yaml_route():
    try:
        yaml_text = export_profiles_yaml(get_db_path())
        filename = f"profiles-{time.strftime('%Y%m%d-%H%M%S')}.yaml"
        response = Response(yaml_text, mimetype='application/x-yaml')
        response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/importProfilesYaml', methods=['POST'])
def import_profiles_yaml_route():
    yaml_text = ''
    if 'file' in request.files:
        file = request.files['file']
        if not file or not file.filename:
            return jsonify({
                "code": 400,
                "msg": "YAML file is required",
                "data": None
            }), 400
        try:
            yaml_text = file.read().decode('utf-8')
        except UnicodeDecodeError:
            return jsonify({
                "code": 400,
                "msg": "YAML file must be UTF-8 encoded",
                "data": None
            }), 400
    else:
        data = request.get_json(silent=True) or {}
        yaml_text = data.get('yamlContent', '')

    if not str(yaml_text or '').strip():
        return jsonify({
            "code": 400,
            "msg": "yamlContent is required",
            "data": None
        }), 400

    try:
        result = import_profiles_yaml(get_db_path(), yaml_text)
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/previewImportProfilesYaml', methods=['POST'])
def preview_import_profiles_yaml_route():
    yaml_text = ''
    if 'file' in request.files:
        file = request.files['file']
        if not file or not file.filename:
            return jsonify({
                "code": 400,
                "msg": "YAML file is required",
                "data": None
            }), 400
        try:
            yaml_text = file.read().decode('utf-8')
        except UnicodeDecodeError:
            return jsonify({
                "code": 400,
                "msg": "YAML file must be UTF-8 encoded",
                "data": None
            }), 400
    else:
        data = request.get_json(silent=True) or {}
        yaml_text = data.get('yamlContent', '')

    if not str(yaml_text or '').strip():
        return jsonify({
            "code": 400,
            "msg": "yamlContent is required",
            "data": None
        }), 400

    try:
        result = preview_profiles_yaml_import(get_db_path(), yaml_text)
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/downloadProfileConfigExample', methods=['GET'])
def download_profile_config_example_route():
    try:
        yaml_text = get_profile_config_example_yaml(Path(BASE_DIR))
        response = Response(yaml_text, mimetype='application/x-yaml')
        response.headers['Content-Disposition'] = f'attachment; filename="profile-config.example.yaml"'
        return response
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/getProfileBackupConfig', methods=['GET'])
def get_profile_backup_config_route():
    try:
        result = get_profile_backup_config(Path(BASE_DIR), get_db_path())
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/saveProfileBackupConfig', methods=['POST'])
def save_profile_backup_config_route():
    data = request.get_json() or {}
    try:
        result = save_profile_backup_config(Path(BASE_DIR), get_db_path(), data)
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/runProfileBackup', methods=['POST'])
def run_profile_backup_route():
    try:
        result = run_profile_backup(Path(BASE_DIR), get_db_path())
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/generateProfileContent', methods=['POST'])
def generate_profile_content_route():
    data = request.get_json() or {}
    try:
        result = generate_profile_content(get_db_path(), Path(BASE_DIR), data)
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/generateProfileBatchContent', methods=['POST'])
def generate_profile_batch_content_route():
    data = request.get_json() or {}
    try:
        result = generate_profile_batch_content(get_db_path(), Path(BASE_DIR), data)
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/generatePublishBatchDrafts', methods=['POST'])
def generate_publish_batch_drafts_route():
    data = request.get_json() or {}
    try:
        result = generate_publish_batch_drafts(get_db_path(), Path(BASE_DIR), data)
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/savePublishJobs', methods=['POST'])
def save_publish_jobs_route():
    data = request.get_json() or {}
    try:
        result = save_publish_jobs(get_db_path(), Path(BASE_DIR), data)
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/getPublishJobs', methods=['GET'])
def get_publish_jobs_route():
    filters = {
        "status": request.args.get("status"),
        "deliveryMode": request.args.get("deliveryMode"),
        "platformKey": request.args.get("platformKey"),
        "profileId": request.args.get("profileId"),
        "dateFrom": request.args.get("dateFrom"),
        "dateTo": request.args.get("dateTo"),
    }
    batch_id = request.args.get("batchId")
    if batch_id:
        filters["batchId"] = batch_id
    try:
        sync_publish_job_statuses(get_db_path(), Path(BASE_DIR), limit=20)
        result = list_publish_jobs(get_db_path(), filters)
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/getPublishCalendarEntries', methods=['GET'])
def get_publish_calendar_entries_route():
    start_date = request.args.get("startDate", "")
    end_date = request.args.get("endDate", "")
    if not start_date or not end_date:
        return jsonify({
            "code": 400,
            "msg": "startDate and endDate are required",
            "data": None
        }), 400
    filters = {
        "status": request.args.get("status"),
        "deliveryMode": request.args.get("deliveryMode"),
        "platformKey": request.args.get("platformKey"),
        "profileId": request.args.get("profileId"),
    }
    try:
        sync_publish_job_statuses(get_db_path(), Path(BASE_DIR), limit=20)
        result = get_publish_calendar_entries(get_db_path(), start_date, end_date, filters)
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/updatePublishJob', methods=['POST'])
def update_publish_job_route():
    data = request.get_json() or {}
    try:
        result = update_publish_job_content(get_db_path(), data)
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/regeneratePublishJob', methods=['POST'])
def regenerate_publish_job_route():
    data = request.get_json() or {}
    try:
        result = regenerate_publish_job_content(get_db_path(), Path(BASE_DIR), data)
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/runPublishJobNow', methods=['POST'])
def run_publish_job_now_route():
    data = request.get_json() or {}
    job_id = data.get("jobId")
    if not job_id:
        return jsonify({
            "code": 400,
            "msg": "jobId is required",
            "data": None
        }), 400
    try:
        result = run_publish_job_now(get_db_path(), Path(BASE_DIR), int(job_id))
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/cancelPublishJob', methods=['POST'])
def cancel_publish_job_route():
    data = request.get_json() or {}
    job_id = data.get("jobId")
    if not job_id:
        return jsonify({
            "code": 400,
            "msg": "jobId is required",
            "data": None
        }), 400
    try:
        result = cancel_publish_job(get_db_path(), int(job_id))
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/completeManualPublishJob', methods=['POST'])
def complete_manual_publish_job_route():
    data = request.get_json() or {}
    job_id = data.get("jobId")
    if not job_id:
        return jsonify({
            "code": 400,
            "msg": "jobId is required",
            "data": None
        }), 400
    try:
        result = complete_manual_publish_job(get_db_path(), int(job_id))
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/getGoogleSheetConfig', methods=['GET'])
def get_google_sheet_config_route():
    try:
        result = get_google_service_account_config(Path(BASE_DIR))
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/getDirectPublishersConfig', methods=['GET'])
def get_direct_publishers_config_route():
    try:
        result = get_direct_publishers_config(Path(BASE_DIR), include_sensitive=False)
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": {"targets": []}
        }), 500


@app.route('/saveDirectPublishersConfig', methods=['POST'])
def save_direct_publishers_config_route():
    data = request.get_json() or {}
    try:
        result = save_direct_publishers_config(Path(BASE_DIR), data)
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": {"targets": []}
        }), 500


@app.route('/saveGoogleSheetConfig', methods=['POST'])
def save_google_sheet_config_route():
    data = request.get_json() or {}
    try:
        result = save_google_service_account_config(
            Path(BASE_DIR),
            data.get("serviceAccountJson", ""),
        )
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/validateGoogleSheetConfig', methods=['POST'])
def validate_google_sheet_config_route():
    data = request.get_json() or {}
    try:
        result = validate_google_sheet_connection(
            Path(BASE_DIR),
            data.get("spreadsheetId", ""),
        )
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route('/migrateProfileMedia', methods=['POST'])
def migrate_profile_media_route():
    data = request.get_json() or {}
    profile_id = data.get('profileId')
    relative_path = data.get('relativePath')
    target_storage = data.get('targetStorage') or {}

    if not profile_id:
        return jsonify({
            "code": 400,
            "msg": "profileId is required",
            "data": None
        }), 400

    if not relative_path:
        return jsonify({
            "code": 400,
            "msg": "relativePath is required",
            "data": None
        }), 400

    try:
        profile = get_profile(get_db_path(), int(profile_id))
        result = migrate_uploaded_asset(profile, relative_path, target_storage)
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str(e),
            "data": None
        }), 500


@app.route("/getAccounts", methods=['GET'])
def getAccounts():
    """快速获取所有账号信息，不进行cookie验证"""
    try:
        with sqlite3.connect(get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
            SELECT * FROM user_info ORDER BY id DESC''')
            rows = cursor.fetchall()
            rows_list = [
                _sanitize_account_response(
                    merge_account_validation(
                        serialize_account_row(row),
                        get_validation_result(Path(BASE_DIR), row["id"]),
                    )
                )
                for row in rows
            ]

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
def getValidAccounts():
    try:
        results = [_sanitize_account_response(item) for item in validate_accounts(Path(BASE_DIR), get_db_path())]
        return jsonify(
                        {
                            "code": 200,
                            "msg": None,
                            "data": results
                        }),200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": f"驗證帳號狀態失敗: {str(e)}",
            "data": None
        }), 500


@app.route("/validateAccount", methods=['POST'])
def validate_account_route():
    data = request.get_json() or {}
    account_id = data.get("accountId") or data.get("id")
    if not account_id:
        return jsonify({
            "code": 400,
            "msg": "accountId is required",
            "data": None
        }), 400

    try:
        result = _sanitize_account_response(validate_account(Path(BASE_DIR), get_db_path(), int(account_id)))
        return jsonify({
            "code": 200,
            "msg": "account validated",
            "data": result
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": f"account validation failed: {str(e)}",
            "data": None
        }), 500


@app.route("/getValidationResults", methods=['GET'])
def get_validation_results_route():
    try:
        return jsonify({
            "code": 200,
            "msg": None,
            "data": get_validation_results(Path(BASE_DIR)).get("results", {})
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": f"failed to load validation results: {str(e)}",
            "data": None
        }), 500

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
        return jsonify({
            "code": 500,
            "msg": str("delete failed!"),
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
                cookie_file_path = _resolve_safe_child_path(Path(BASE_DIR / "cookiesFile"), record['filePath'])
                if cookie_file_path.exists():
                    try:
                        cookie_file_path.unlink()
                        print(f"✅ Cookie文件已删除: {cookie_file_path}")
                    except Exception as e:
                        print(f"⚠️ 删除Cookie文件失败: {e}")

            # 删除数据库记录
            cursor.execute("DELETE FROM user_info WHERE id = ?", (account_id,))
            conn.commit()
        delete_validation_result(Path(BASE_DIR), account_id)

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
    thread = threading.Thread(target=run_async_function, args=(type,id,status_queue), daemon=True)
    thread.start()
    response = Response(sse_stream(status_queue, id), mimetype='text/event-stream')
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
    desc = data.get('desc', '')
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
                                   start_days, desc)
            case 2:
                post_video_tencent(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                                   start_days, is_draft)
            case 3:
                post_video_DouYin(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                          start_days, thumbnail_path, productLink, productTitle, desc)
            case 4:
                post_video_ks(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                          start_days, desc)
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


@app.route('/account', methods=['POST'])
def create_account():
    data = request.get_json() or {}
    user_name = (data.get('userName') or data.get('name') or '').strip()
    platform_key = normalize_platform_key(data.get('platformKey') or data.get('platform'))
    auth_mode = (data.get('authMode') or '').strip()
    metadata = merge_sensitive_account_metadata(
        platform_key,
        parse_metadata(data.get('metadata') or data.get('metadataJson') or {}),
        {},
    )
    metadata_json = json.dumps(metadata, ensure_ascii=False)

    if not user_name:
        return jsonify({
            "code": 400,
            "msg": "userName is required",
            "data": None
        }), 400
    if not platform_key:
        return jsonify({
            "code": 400,
            "msg": "platformKey is required",
            "data": None
        }), 400

    account_type = platform_type_from_key(platform_key)
    resolved_auth_mode = auth_mode or default_auth_mode_for_platform(platform_key, account_type)
    file_path = _sanitize_storage_leaf_name(data.get('filePath') or '', suffix=".json", default_stem=platform_key or "cookie") if data.get('filePath') else ''
    if not file_path and get_platform_config(platform_key, account_type)["supportsCookieUpload"]:
        file_path = build_cookie_storage_name(platform_key)
    status = int(data.get('status', 0))

    try:
        with sqlite3.connect(get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO user_info (type, filePath, userName, status, platform_key, auth_mode, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (account_type, file_path, user_name, status, platform_key, resolved_auth_mode, metadata_json)
            )
            conn.commit()
            account_id = cursor.lastrowid
            delete_validation_result(Path(BASE_DIR), account_id)
            cursor.execute('SELECT * FROM user_info WHERE id = ?', (account_id,))
            record = cursor.fetchone()

        return jsonify({
            "code": 200,
            "msg": "account created successfully",
            "data": _sanitize_account_response(serialize_account_row(record))
        }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": f"create account failed: {str(e)}",
            "data": None
        }), 500


@app.route('/updateUserinfo', methods=['POST'])
def updateUserinfo():
    # 获取JSON数据
    data = request.get_json()

    # 从JSON数据中提取 type 和 userName
    user_id = data.get('id')
    type = data.get('type')
    userName = (data.get('userName') or data.get('name') or '').strip()
    platform_key = normalize_platform_key(data.get('platformKey') or data.get('platform')) or platform_key_from_type(type)
    auth_mode = (data.get('authMode') or '').strip()
    incoming_metadata = parse_metadata(data.get('metadata') or data.get('metadataJson') or {})
    file_path = data.get('filePath')

    if not user_id or not userName or not platform_key:
        return jsonify({
            "code": 400,
            "msg": "id, userName, and platformKey are required",
            "data": None
        }), 400

    account_type = platform_type_from_key(platform_key) if type in (None, "", 0, "0") else int(type)
    resolved_auth_mode = auth_mode or default_auth_mode_for_platform(platform_key, account_type)
    try:
        # 获取数据库连接
        with sqlite3.connect(get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM user_info WHERE id = ?', (user_id,))
            existing_record = cursor.fetchone()
            if not existing_record:
                return jsonify({
                    "code": 404,
                    "msg": "account not found",
                    "data": None
                }), 404

            resolved_file_path = (
                _sanitize_storage_leaf_name(file_path, suffix=".json", default_stem=platform_key or "cookie")
                if file_path is not None
                else str(existing_record["filePath"] or "").strip()
            )
            if not resolved_file_path and get_platform_config(platform_key, account_type)["supportsCookieUpload"]:
                resolved_file_path = build_cookie_storage_name(platform_key)
            metadata_json = json.dumps(
                merge_sensitive_account_metadata(
                    platform_key,
                    incoming_metadata,
                    parse_metadata(existing_record["metadata_json"] or "{}"),
                ),
                ensure_ascii=False,
            )

            # 更新数据库记录
            if file_path is None:
                cursor.execute(
                    '''
                    UPDATE user_info
                    SET type = ?,
                        userName = ?,
                        platform_key = ?,
                        auth_mode = ?,
                        filePath = ?,
                        metadata_json = ?
                    WHERE id = ?;
                    ''',
                    (account_type, userName, platform_key, resolved_auth_mode, resolved_file_path, metadata_json, user_id)
                )
            else:
                cursor.execute(
                    '''
                    UPDATE user_info
                    SET type = ?,
                        filePath = ?,
                        userName = ?,
                        platform_key = ?,
                        auth_mode = ?,
                        metadata_json = ?
                    WHERE id = ?;
                    ''',
                    (account_type, resolved_file_path, userName, platform_key, resolved_auth_mode, metadata_json, user_id)
                )
            conn.commit()
            cursor.execute('SELECT * FROM user_info WHERE id = ?', (user_id,))
            record = cursor.fetchone()
        delete_validation_result(Path(BASE_DIR), user_id)

        return jsonify({
            "code": 200,
            "msg": "account update successfully",
            "data": _sanitize_account_response(serialize_account_row(record)) if record else None
        }), 200

    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": f"update failed: {str(e)}",
            "data": None
        }), 500

@app.route('/postVideoBatch', methods=['POST'])
def postVideoBatch():
    data_list = request.get_json()

    if not isinstance(data_list, list):
        return jsonify({"code": 400, "msg": "Expected a JSON array", "data": None}), 400
    for data in data_list:
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
        desc = data.get('desc', '')
        is_draft = data.get('isDraft', False)

        videos_per_day = data.get('videosPerDay')
        daily_times = data.get('dailyTimes')
        start_days = data.get('startDays')
        # 打印获取到的数据（仅作为示例）
        print("File List:", file_list)
        print("Account List:", account_list)
        match type:
            case 1:
                post_video_xhs(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                               start_days, desc)
            case 2:
                post_video_tencent(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                                   start_days, is_draft)
            case 3:
                post_video_DouYin(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                          start_days, '', productLink, productTitle, desc)
            case 4:
                post_video_ks(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                          start_days, desc)
    # 返回响应给客户端
    return jsonify(
        {
            "code": 200,
            "msg": None,
            "data": None
        }), 200

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

        # 保存上传的Cookie文件到对应路径
        cookie_file_path = _resolve_safe_child_path(Path(BASE_DIR / "cookiesFile"), result['filePath'])
        cookie_file_path.parent.mkdir(parents=True, exist_ok=True)

        file.save(str(cookie_file_path))
        delete_validation_result(Path(BASE_DIR), account_id)

        # 更新数据库中的账号信息（可选，比如更新更新时间）
        # 这里可以根据需要添加额外的处理逻辑

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
        cookie_file_path = _resolve_safe_child_path(Path(BASE_DIR / "cookiesFile"), file_path)
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

        # 返回文件
        return send_from_directory(
            directory=str(cookie_file_path.parent),
            path=cookie_file_path.name,
            as_attachment=True
        )

    except Exception as e:
        print(f"下载Cookie文件时出错: {str(e)}")
        return jsonify({
            "code": 500,
            "msg": f"下载Cookie文件失败: {str(e)}",
            "data": None
        }), 500


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

# SSE 流生成器函数
def sse_stream(status_queue, queue_id):
    try:
        while True:
            try:
                msg = status_queue.get(timeout=0.5)
            except Empty:
                continue
            yield f"data: {msg}\n\n"
            if msg in {'200', '500'}:
                break
    finally:
        active_queues.pop(queue_id, None)

if __name__ == '__main__':
    start_profile_backup_scheduler()
    start_publish_scheduler()
    app.run(host='0.0.0.0' ,port=5409)
