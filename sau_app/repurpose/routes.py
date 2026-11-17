"""Scoped preparation/previews. Notion alone owns approval and article publication."""
from __future__ import annotations
import hashlib
import io
import os
from pathlib import Path
from flask import Blueprint, current_app, jsonify, request, send_file
from myUtils.repurpose.contracts import HASH, RepurposeError, loads, require, validate_manifest
from myUtils.repurpose.files import read_file, verify_package
from myUtils.repurpose.store import Store
from sau_app.tenancy.context import current_auth
from sau_app.auth.permissions import PUBLISH_WRITE

bp = Blueprint('teaching_repurpose', __name__)
BASE = '/api/publish/teaching-repurpose'


def store():
    return Store(current_app.config.get('SAU_DB_PATH') or os.environ.get('SAU_DB_PATH', '/app/db/database.db'),
                 current_app.config.get('TEACHING_REPURPOSE_ROOT') or os.environ.get('SAU_TEACHING_REPURPOSE_ROOT', ''))


def scope():
    auth = current_auth()
    require(auth.authenticated, 'repurpose_authentication_required')
    require(auth.has_permission(PUBLISH_WRITE), 'teaching_permission_forbidden')
    selected = store()
    with selected.connect() as conn:
        profile = conn.execute('SELECT workspace_id FROM profiles WHERE id=4').fetchone()
        require(profile is not None, 'teaching_profile_missing')
        workspace = profile['workspace_id'] or 'legacy'
        require(auth.auth_method == 'legacy_token' or auth.workspace_id == profile['workspace_id'], 'teaching_workspace_forbidden')
    return selected, workspace


def body(allowed, required=()):
    require(request.content_length is not None and request.content_length <= 512 * 1024, 'request_size')
    data = loads(request.get_data())
    require(isinstance(data, dict) and set(data) <= set(allowed) and set(required) <= set(data), 'request_fields')
    return data


def safe_task(task):
    saved = loads(task['request_json'])
    return {k: task[k] for k in ('id','request_id','state','attempts','reason_code','package_version','created_at','updated_at')} | {
        'source': {k:v for k,v in saved['source'].items() if k != 'manuscript'}, 'kit_version': saved['kit_version'],
        'delivery_ids': saved['deliveries'], 'article_read_only': True, 'requires_new_notion_review': True}


@bp.errorhandler(RepurposeError)
def error(exc):
    code = 401 if exc.code == 'repurpose_authentication_required' else 403 if 'forbidden' in exc.code or 'authorit' in exc.code else 404 if 'not_found' in exc.code else 409
    return jsonify({'code':code,'msg':exc.code,'data':None}), code


@bp.route(BASE + '/status')
def status():
    auth = current_auth()
    require(auth.authenticated, 'repurpose_authentication_required')
    require(auth.has_permission(PUBLISH_WRITE), 'teaching_permission_forbidden')
    return jsonify({'code':200,'data':{'enabled':os.environ.get('SAU_TEACHING_REPURPOSE_ENABLED') == '1',
        'release_enabled':os.environ.get('SAU_TEACHING_REPURPOSE_RELEASE_ENABLED') == '1','approval_location':'notion',
        'storage_configured':bool(current_app.config.get('TEACHING_REPURPOSE_ROOT') or os.environ.get('SAU_TEACHING_REPURPOSE_ROOT','')),
        'generation_owner':auth.auth_method == 'legacy_token','profile_id':4,'account_ids':[43,58,61,125]}})


@bp.route(BASE + '/kits', methods=['GET','POST'])
def kits():
    selected, workspace = scope()
    if request.method == 'GET':return jsonify({'code':200,'data':selected.kits(workspace)})
    require(os.environ.get('SAU_TEACHING_REPURPOSE_ENABLED') == '1', 'repurpose_disabled')
    data = body({'theme'}, {'theme'})
    from myUtils.repurpose.brand import default_kit
    return jsonify({'code':200,'data':{'version':selected.add_kit(workspace, default_kit(data['theme']))}})


@bp.route(BASE + '/tasks', methods=['GET','POST'])
def tasks():
    selected, workspace = scope()
    if request.method == 'GET':return jsonify({'code':200,'data':[safe_task(t) for t in selected.tasks(workspace)]})
    require(os.environ.get('SAU_TEACHING_REPURPOSE_ENABLED') == '1', 'repurpose_disabled')
    require(current_auth().auth_method == 'legacy_token', 'notion_source_owner_required')
    data = body({'request_id','source','kit_version','deliveries'}, {'request_id','source','kit_version'})
    task = selected.enqueue(workspace, data['request_id'], data['source'], data['kit_version'], deliveries=data.get('deliveries'))
    return jsonify({'code':200,'data':safe_task(task)}), 202


@bp.route(BASE + '/tasks/<int:task_id>', methods=['GET','DELETE'])
def task(task_id):
    selected, workspace = scope()
    if request.method == 'DELETE':selected.cancel(workspace, task_id)
    value = selected.task(workspace, task_id); result = safe_task(value)
    result['source']['manuscript'] = loads(value['request_json'])['source']['manuscript']
    if value['state'] == 'prepared':
        try:
            result['package'] = verify_package(value['manifest_path'])
            result['asset_base'] = BASE + '/tasks/' + str(task_id) + '/assets/'
        except RepurposeError as exc:
            result['preview_hold'] = exc.code
    with selected.connect() as conn:
        releases = [r['release_id'] for r in conn.execute('SELECT release_id FROM repurpose_releases WHERE task_id=? AND workspace_id=?', (task_id,workspace))]
    from myUtils.repurpose.release import inspect
    result['releases'] = []
    for identifier in releases:
        try:result['releases'].append(inspect(selected,workspace,identifier))
        except RepurposeError as exc:result['releases'].append({'release_id':identifier,'state':'held','errors':[exc.code],'all_deliveries_confirmed':False})
    return jsonify({'code':200,'data':result})


@bp.route(BASE + '/tasks/<int:task_id>/regenerate', methods=['POST'])
def regenerate(task_id):
    selected, workspace = scope()
    require(os.environ.get('SAU_TEACHING_REPURPOSE_ENABLED') == '1', 'repurpose_disabled')
    require(current_auth().auth_method == 'legacy_token', 'notion_source_owner_required')
    data = body({'request_id','kit_version','deliveries'}, {'request_id','kit_version','deliveries'})
    original = loads(selected.task(workspace,task_id)['request_json'])
    with selected.connect() as conn:
        published = conn.execute("SELECT 1 FROM repurpose_releases WHERE workspace_id=? AND json_extract(manifest_json,'$.material.source.page_id')=?", (workspace,original['source']['page_id'])).fetchone()
    require(not published, 'released_source_requires_reconciliation_not_regeneration')
    require(data['request_id'] != selected.task(workspace,task_id)['request_id'], 'regeneration_requires_new_request')
    created = selected.enqueue(workspace,data['request_id'],original['source'],data['kit_version'],deliveries=data['deliveries'])
    return jsonify({'code':200,'data':safe_task(created)}), 202


@bp.route(BASE + '/tasks/<int:task_id>/assets/<name>')
def asset(task_id, name):
    selected, workspace = scope(); value = selected.task(workspace,task_id)
    require(value['state'] == 'prepared', 'package_not_prepared')
    manifest = verify_package(value['manifest_path'])
    descriptor = next((a for a in manifest['material']['assets'] if a['name'] == name), None)
    require(descriptor is not None, 'asset_not_found')
    raw = read_file(Path(value['manifest_path']).parent / name)
    require(len(raw) == descriptor['size_bytes'] and hashlib.sha256(raw).hexdigest() == descriptor['sha256'], 'preview_asset_changed')
    response = send_file(io.BytesIO(raw), mimetype=descriptor['mime'], download_name=name, conditional=True,
                         as_attachment=descriptor['mime'] in {'application/json','text/plain'})
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@bp.route(BASE + '/releases/<release_id>')
def inspect_release(release_id):
    selected, workspace = scope()
    from myUtils.repurpose.release import inspect
    return jsonify({'code':200,'data':inspect(selected,workspace,release_id)})


@bp.route(BASE + '/tasks/<int:task_id>/release', methods=['POST'])
def release_task(task_id):
    selected, workspace = scope()
    require(current_auth().auth_method == 'legacy_token', 'teaching_owner_forbidden')
    data = body({'release_id','review_version'}, {'release_id','review_version'})
    authorize = current_app.config.get('TEACHING_REPURPOSE_AUTHORIZER')
    if authorize is None and os.environ.get('SAU_TEACHING_OWNER_ALLOWED_SIGNERS'):
        from myUtils.repurpose.authority import file_authorizer
        authorize = file_authorizer(selected,workspace,allowed_signers=os.environ['SAU_TEACHING_OWNER_ALLOWED_SIGNERS'],
                                   private_root=os.environ.get('SAU_TEACHING_OWNER_RECEIPTS_ROOT',''))
    from myUtils.repurpose.release import release
    result = release(selected,workspace,task_id,data['release_id'],data['review_version'],authorize=authorize,
                     media_root=current_app.config.get('TEACHING_REPURPOSE_MEDIA_ROOT') or os.environ.get('SAU_TEACHING_REPURPOSE_MEDIA_ROOT','/app/data/publish'),
                     public_base_url=current_app.config.get('TEACHING_REPURPOSE_PUBLIC_BASE_URL') or os.environ.get('SAU_TEACHING_REPURPOSE_PUBLIC_BASE_URL',''))
    return jsonify({'code':200,'data':result})


@bp.route('/teaching-repurpose-media/<version>/<name>')
def released_media(version, name):
    """Public provider fetch: exact approved asset in the protected delivery store only."""
    require(HASH.fullmatch(version) and os.environ.get('SAU_TEACHING_REPURPOSE_RELEASE_ENABLED') == '1', 'release_media_not_found')
    selected = store()
    with selected.connect() as conn:
        rows = conn.execute("SELECT request_json FROM repurpose_deliveries WHERE package_version=? AND state='submitted'", (version,)).fetchall()
        require(any(name in loads(r['request_json'])['asset_names'] for r in rows), 'release_media_not_found')
        header = conn.execute('SELECT manifest_json,media_root FROM repurpose_releases WHERE package_version=?', (version,)).fetchone()
    require(header is not None, 'release_media_not_found')
    manifest = validate_manifest(loads(header['manifest_json']))
    descriptor = next((a for a in manifest['material']['assets'] if a['name'] == name), None)
    require(descriptor and descriptor['mime'] in {'image/png','image/jpeg','video/mp4'}, 'release_media_not_found')
    from myUtils.repurpose.release import delivery_filename
    root = Path(header['media_root']); path = root / delivery_filename(manifest,descriptor)
    require(root.is_absolute() and '..' not in root.parts and all(not p.is_symlink() and not (hasattr(p,'is_junction') and p.is_junction()) for p in (path,*path.parents)), 'release_media_not_found')
    require(path.is_file() and path.stat().st_size == descriptor['size_bytes'], 'release_media_not_found')
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno()); raw = stream.read(descriptor['size_bytes']+1); after = os.fstat(stream.fileno())
    require((before.st_ino,before.st_size,before.st_mtime_ns) == (after.st_ino,after.st_size,after.st_mtime_ns)
            and len(raw) == descriptor['size_bytes'] and hashlib.sha256(raw).hexdigest() == descriptor['sha256'], 'release_media_not_found')
    response = send_file(io.BytesIO(raw),mimetype=descriptor['mime'],download_name=name,conditional=True)
    response.headers['Cache-Control'] = 'no-store'; response.headers['X-Content-Type-Options'] = 'nosniff'
    return response
