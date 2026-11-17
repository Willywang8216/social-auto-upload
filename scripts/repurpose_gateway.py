"""Private existing-SSH gateway. Not an HTTP endpoint; no unsigned release authority."""
from __future__ import annotations
import base64
import hashlib
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from myUtils.repurpose.contracts import RepurposeError, json_bytes, loads, require, validate_manifest
from myUtils.repurpose.files import private_path, read_file, verify_package
from myUtils.repurpose.store import Store

MAX_CHUNK = 65536


def scope(store):
    with store.connect() as conn:
        profile = conn.execute('SELECT workspace_id FROM profiles WHERE id=4').fetchone()
    require(profile is not None, 'teaching_profile_missing')
    return profile['workspace_id'] or 'legacy'


def run(command, store, workspace):
    require(isinstance(command, dict) and isinstance(command.get('op'), str), 'gateway_fields')
    operation = command['op']
    if operation == 'prepare':
        require(set(command) == {'op','request_id','source','theme','deliveries'}, 'gateway_fields')
        require(os.environ.get('SAU_TEACHING_REPURPOSE_ENABLED') == '1', 'repurpose_disabled')
        from myUtils.repurpose.brand import default_kit
        version = store.add_kit(workspace, default_kit(command['theme']))
        task = store.enqueue(workspace, command['request_id'], command['source'], version, deliveries=command['deliveries'])
        return {'protocol_version':2,'task_id':task['id'],'state':task['state'],'package_version':task['package_version']}
    if operation == 'lookup':
        require(set(command) == {'op','request_id'} and isinstance(command['request_id'], str), 'gateway_fields')
        with store.connect() as conn:
            task = conn.execute('SELECT id,state,package_version FROM repurpose_tasks WHERE workspace_id=? AND request_id=?', (workspace, command['request_id'])).fetchone()
        require(task is not None, 'preparation_request_unknown')
        return {'protocol_version':2,'task_id':task['id'],'state':task['state'],'package_version':task['package_version']}
    if operation == 'dispatch':
        require(set(command) == {'op','task_id','release_id','review_version','signed_dispatch'} and type(command['task_id']) is int and command['task_id'] > 0, 'gateway_fields')
        from myUtils.repurpose.release import identities
        identities(command['release_id'],command['review_version'])
        task=store.task(workspace,command['task_id'])
        envelope=command['signed_dispatch']
        require(isinstance(envelope,dict) and isinstance(envelope.get('ticket'),dict)
                and envelope['ticket'].get('task_id')==task['id'] and envelope['ticket'].get('release_id')==command['release_id']
                and envelope['ticket'].get('review_version')==command['review_version'],'gateway_dispatch_binding')
        require(os.environ.get('SAU_TEACHING_REPURPOSE_RELEASE_ENABLED')=='1','repurpose_release_disabled')
        from myUtils.repurpose.dispatch import install
        return install(store,workspace,envelope,allowed_signers=os.environ.get('SAU_TEACHING_OWNER_ALLOWED_SIGNERS',''),
                       private_root=os.environ.get('SAU_TEACHING_OWNER_RECEIPTS_ROOT',''))
    if operation in {'task','asset','release'}:
        required = {'op','task_id'} if operation == 'task' else {'op','task_id','name','offset'} if operation == 'asset' else {'op','task_id','release_id','review_version','signed_handoff'}
        require(set(command) == required and type(command['task_id']) is int and command['task_id'] > 0, 'gateway_fields')
        task = store.task(workspace, command['task_id'])
        if operation == 'task':
            result = {'protocol_version':2,'task_id':task['id'],'state':task['state'],'reason_code':task['reason_code'],'package_version':task['package_version']}
            if task['state'] == 'prepared':result['manifest'] = verify_package(task['manifest_path'])
            return result
        if operation == 'asset':
            require(task['state'] == 'prepared', 'package_not_prepared')
            manifest = validate_manifest(loads(read_file(task['manifest_path'],512*1024)))
            require(manifest['package_version'] == task['package_version'], 'gateway_package_changed')
            asset = next((a for a in manifest['material']['assets'] if a['name'] == command['name']), None)
            require(asset is not None and type(command['offset']) is int and 0 <= command['offset'] < asset['size_bytes'], 'gateway_asset_scope')
            path = private_path(Path(task['manifest_path']).parent / asset['name'])
            with path.open('rb') as stream:
                before = os.fstat(stream.fileno()); require(before.st_size == asset['size_bytes'], 'gateway_asset_changed')
                stream.seek(command['offset']); chunk = stream.read(min(MAX_CHUNK,asset['size_bytes']-command['offset']))
                after = os.fstat(stream.fileno())
            require((before.st_ino,before.st_size,before.st_mtime_ns)==(after.st_ino,after.st_size,after.st_mtime_ns), 'gateway_asset_changed')
            return {'protocol_version':2,'name':asset['name'],'sha256':asset['sha256'],'size_bytes':asset['size_bytes'],'mime':asset['mime'],'offset':command['offset'],'data':base64.b64encode(chunk).decode('ascii'),'chunk_sha256':hashlib.sha256(chunk).hexdigest()}
        from myUtils.repurpose.release import release, inspect
        with store.connect() as conn:
            old = conn.execute('SELECT 1 FROM repurpose_releases WHERE release_id=? AND workspace_id=?', (command['release_id'], workspace)).fetchone()
        if old:
            return release(store,workspace,task['id'],command['release_id'],command['review_version'],authorize=None,media_root='',public_base_url='')
        require(task['state'] == 'prepared', 'package_not_prepared')
        require(os.environ.get('SAU_TEACHING_REPURPOSE_RELEASE_ENABLED') == '1', 'repurpose_release_disabled')
        manifest = verify_package(task['manifest_path'])
        from myUtils.repurpose.authority import install_signed, file_authorizer
        root = os.environ.get('SAU_TEACHING_OWNER_RECEIPTS_ROOT', '')
        signers = os.environ.get('SAU_TEACHING_OWNER_ALLOWED_SIGNERS', '')
        proof = install_signed(command['signed_handoff'],manifest,allowed_signers=signers,private_root=root)
        require(proof.release_id == command['release_id'] and proof.review_version == command['review_version'] and command['signed_handoff']['ticket']['task_id'] == task['id'], 'gateway_owner_binding')
        authorize = file_authorizer(store,workspace,allowed_signers=signers,private_root=root)
        return release(store,workspace,task['id'],command['release_id'],command['review_version'],authorize=authorize,
                       media_root=os.environ.get('SAU_TEACHING_REPURPOSE_MEDIA_ROOT','/app/data/publish'),public_base_url=os.environ.get('SAU_TEACHING_REPURPOSE_PUBLIC_BASE_URL',''))
    if operation == 'inspect':
        require(set(command) == {'op','release_id'}, 'gateway_fields')
        from myUtils.repurpose.release import inspect
        return inspect(store,workspace,command['release_id'])
    raise RepurposeError('gateway_operation_forbidden')


def main():
    require(os.environ.get('SAU_REPURPOSE_PRIVATE_GATEWAY') == '1', 'gateway_private_transport_required')
    raw = sys.stdin.buffer.read(512*1024+1)
    require(len(raw) <= 512*1024, 'gateway_request_limit')
    store = Store(os.environ.get('SAU_DB_PATH','/app/db/database.db'),os.environ.get('SAU_TEACHING_REPURPOSE_ROOT',''))
    result = run(loads(raw),store,scope(store))
    sys.stdout.buffer.write(json_bytes(result)+b'\n')


if __name__ == '__main__':
    try:main()
    except RepurposeError as exc:
        sys.stdout.buffer.write(json_bytes({'protocol_version':2,'state':'held','errors':[exc.code]})+b'\n')
        raise SystemExit(1)
