"""Fresh Manager authority for known scheduled jobs; never enqueue, retry, or refund."""
from __future__ import annotations
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from . import authority
from .contracts import RepurposeError, json_bytes, loads, require, validate_manifest
from .files import private_path, read_file, write_new
from .release import context, identities, inspect, receipt_identity
from .store import Store, utc

KIND = 'teaching-dispatch-grant'
RECEIPT_KEYS = ('delivery_id','account_id','job_id','target_ids','schedule_at')


def bound_ticket(data, store, workspace, *, now=None):
    require(isinstance(data,dict) and set(data)==authority.FIELDS | {'deliveries'} and data['kind']==KIND, 'dispatch_ticket_fields')
    identities(data['release_id'],data['review_version'])
    with store.connect() as conn:
        row=conn.execute('SELECT * FROM repurpose_releases WHERE release_id=? AND workspace_id=?',(data['release_id'],workspace)).fetchone()
    require(row is not None,'dispatch_release_not_found')
    manifest=validate_manifest(loads(row['manifest_json']))
    core={k:v for k,v in data.items() if k!='deliveries'};core['kind']='teaching-owner-handoff'
    proof=authority.ticket(core,manifest,now=now)
    require(row['task_id']==data['task_id'] and row['review_version']==data['review_version'] and row['package_version']==data['package_version'], 'dispatch_owner_binding')
    snapshot=inspect(store,workspace,data['release_id'])
    expected=[{k:o[k] for k in RECEIPT_KEYS} for o in snapshot['outcomes']]
    require(data['deliveries']==expected,'dispatch_known_job_binding')
    for value,planned in zip(data['deliveries'],manifest['material']['deliveries']):
        receipt_identity(value,planned)
    return proof,snapshot


def install(store,workspace,envelope,*,allowed_signers,private_root,now=None):
    data=authority.verify_signature(envelope,allowed_signers=allowed_signers,private_root=private_root)
    proof,snapshot=bound_ticket(data,store,workspace,now=now)
    raw=json_bytes(envelope)+b'\n';digest=hashlib.sha256(raw).hexdigest()
    root=private_path(private_root);path=root/('dispatch-'+digest+'.json')
    if path.exists():require(read_file(path,128*1024)==raw,'immutable_dispatch_grant_conflict')
    else:write_new(path,raw)
    observed=proof.observed_at.isoformat()
    with store.connect(write=True) as conn:
        old=conn.execute('SELECT * FROM repurpose_dispatch_grants WHERE release_id=?',(proof.release_id,)).fetchone()
        require(old is None or old['workspace_id']==workspace,'dispatch_workspace_forbidden')
        require(old is None or old['observed_at']<=observed,'dispatch_observation_stale')
        require(old is None or old['observed_at']!=observed or old['ticket_sha256']==digest,'immutable_dispatch_observation_conflict')
        conn.execute('''INSERT INTO repurpose_dispatch_grants VALUES(?,?,?,?,?,?) ON CONFLICT(release_id) DO UPDATE SET
            ticket_sha256=excluded.ticket_sha256,ticket_path=excluded.ticket_path,observed_at=excluded.observed_at''',
            (proof.release_id,workspace,digest,str(path),observed,utc(now).isoformat()))
    return {'protocol_version':2,'release_id':proof.release_id,'state':'dispatch-authorized','observed_at':observed,
            'package_version':proof.package_version,'known_jobs_only':True}


def assert_permission(payload,target,*,db_path,now=None):
    """Read-only at preparation and each create boundary. Receipt polls bypass it."""
    from myUtils import delivery
    if 'articleRepurpose' not in payload or delivery.state(payload).get('provider_receipt'):
        return
    selected=Store(db_path,os.environ.get('SAU_TEACHING_REPURPOSE_ROOT',''))
    binding=payload.get('articleRepurpose')
    require(isinstance(binding,dict),'dispatch_payload_binding')
    identities(binding.get('release_id'),binding.get('review_version'))
    require(os.environ.get('SAU_TEACHING_REPURPOSE_RELEASE_ENABLED')=='1','repurpose_release_disabled')
    with selected.connect() as conn:
        header=conn.execute('SELECT * FROM repurpose_releases WHERE release_id=?',(binding['release_id'],)).fetchone()
        grant=conn.execute('SELECT * FROM repurpose_dispatch_grants WHERE release_id=?',(binding['release_id'],)).fetchone()
    require(header is not None and grant is not None and header['workspace_id']==grant['workspace_id'],'dispatch_authority_not_ready')
    raw=read_file(private_path(grant['ticket_path'],root=os.environ.get('SAU_TEACHING_OWNER_RECEIPTS_ROOT','')),128*1024)
    require(hashlib.sha256(raw).hexdigest()==grant['ticket_sha256'],'dispatch_grant_changed')
    data=authority.verify_signature(loads(raw),allowed_signers=os.environ.get('SAU_TEACHING_OWNER_ALLOWED_SIGNERS',''),private_root=os.environ.get('SAU_TEACHING_OWNER_RECEIPTS_ROOT',''))
    proof,snapshot=bound_ticket(data,selected,header['workspace_id'],now=now)
    require(proof.observed_at.isoformat()==grant['observed_at'],'dispatch_observation_changed')
    outcome=next((o for o in snapshot['outcomes'] if o['delivery_id']==binding.get('delivery_id')),None)
    require(outcome is not None and target.job_id==outcome['job_id'] and target.id in outcome['target_ids'],'dispatch_target_binding')
    manifest=validate_manifest(loads(header['manifest_json']))
    require(binding==context(header['release_id'],header['review_version'],manifest,outcome['delivery_id']),'dispatch_payload_binding')
    from myUtils import jobs
    approved=jobs.get_job(target.job_id,db_path=db_path).payload
    require(all(payload.get(k)==approved.get(k) for k in ('articleRepurpose','message','draft','artifacts','_preserveApprovedText','useLlm','allowLlmFallback')),
            'dispatch_runtime_payload_changed')
    try:
        slot=datetime.fromisoformat(outcome['schedule_at'].replace('Z','+00:00'))
    except ValueError as exc:
        raise RepurposeError('dispatch_original_slot_invalid') from exc
    slot=slot.replace(tzinfo=timezone.utc) if slot.tzinfo is None else slot.astimezone(timezone.utc)
    require(utc(now)>=slot,'dispatch_before_original_slot')


def check(payload,target,*,db_path):
    """Convert owner/source refusal into existing prerequisite waiting, not fallback."""
    from myUtils.media_restore import PrerequisiteUnavailable
    try:assert_permission(payload,target,db_path=db_path)
    except (RepurposeError,OSError) as exc:
        raise PrerequisiteUnavailable('configuration') from exc
