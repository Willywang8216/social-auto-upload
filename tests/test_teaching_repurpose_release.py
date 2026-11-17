"""Real enqueue/admission on private fixture DBs. Provider receipts below are unit fixtures."""
import copy
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from myUtils import delivery, jobs
from myUtils.repurpose import contracts, release
from tests.repurpose_fixtures import produce_verified_fixture, prepare_local_store


@pytest.fixture(scope='module')
def media_fixture(tmp_path_factory):
    return produce_verified_fixture(tmp_path_factory)


@pytest.fixture
def local(tmp_path,media_fixture,monkeypatch):
    monkeypatch.delenv('SAU_CONFIG_ENCRYPTION_KEY',raising=False)
    monkeypatch.setenv('SAU_TEACHING_REPURPOSE_RELEASE_ENABLED','1')
    monkeypatch.setenv('META_APP_ID','local-meta-app');monkeypatch.setenv('META_APP_SECRET','local-meta-secret')
    monkeypatch.setenv('THREADS_APP_ID','local-threads-app');monkeypatch.setenv('THREADS_APP_SECRET','local-threads-secret')
    return prepare_local_store(tmp_path,media_fixture)


def install(local,*,identifier='local-release',authorize=True):
    selected,task,manifest,media=local; source=manifest['material']['source']
    # Owner observation happens before dispatch, not after the verifier's now sample.
    observed = datetime.now(timezone.utc)
    def proof(rid,version,package):
        return release.ReleaseAuthority(rid,version,package,source['page_id'],source['article_sha256'],
                                        'https://iamwillywang.com/post/'+source['slug'],observed)
    return release.release(selected,'legacy',task['id'],identifier,'b'*64,authorize=proof if authorize is True else authorize,
                           media_root=media,public_base_url='https://repurpose-fixture.invalid')


def counts(selected):
    with selected.connect() as conn:
        return tuple(conn.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in ('publish_jobs','publish_job_targets','repurpose_releases','repurpose_deliveries'))


def unit_receipt(selected,outcome,*,confirmed=True,changes=None):
    """Emulate a provider result solely in a fixture DB; never live delivery evidence."""
    with selected.connect(write=True) as conn:
        row=conn.execute('SELECT checkpoint_json FROM publish_job_targets WHERE id=?',(outcome['target_ids'][0],)).fetchone()
        checkpoint=json.loads(row['checkpoint_json']); platform=conn.execute('SELECT platform FROM publish_jobs WHERE id=?',(outcome['job_id'],)).fetchone()[0]
        checkpoint.update(submission_started=True,provider_receipt={'provider':platform,'id':'local-unit-receipt-'+str(outcome['job_id']),
                            'status':'delivered' if confirmed else 'processing','delivered_at':datetime.now(timezone.utc).isoformat()})
        routes=checkpoint.get('publish_routes') or {}
        routes['active']='native';routes['native']={**routes.get('native',{}),'attempts':1,'max_attempts':3,'outcome':'delivered' if confirmed else 'attempting'}
        checkpoint['publish_routes']=routes
        if changes:checkpoint.update(changes)
        conn.execute("UPDATE publish_job_targets SET status=?,delivery_state=?,attempts=1,checkpoint_json=? WHERE id=?",
                     ('succeeded' if confirmed else 'awaiting_delivery','delivered' if confirmed else 'awaiting_delivery',json.dumps(checkpoint),outcome['target_ids'][0]))


def test_actual_atomic_six_format_install_never_claims_delivery(local):
    result=install(local); selected,task,manifest,media=local
    assert counts(selected)==(6,6,1,6)
    assert result['receipt_valid'] is True and result['all_deliveries_confirmed'] is False and result['state']=='active'
    assert [o['delivery_id'] for o in result['outcomes']]==contracts.DEFAULT_DELIVERIES
    assert len({o['job_id'] for o in result['outcomes']})==6 and len({o['target_ids'][0] for o in result['outcomes']})==6
    with selected.connect() as conn:
        assert all(r['attempts']==0 for r in conn.execute('SELECT attempts FROM publish_job_targets'))
        for outcome in result['outcomes']:
            job=jobs.get_job(outcome['job_id'],db_path=selected.db_path)
            planned=next(d for d in manifest['material']['deliveries'] if d['id']==outcome['delivery_id'])
            assert job.profile_id==4 and job.payload['message']==planned['message'] and job.payload['useLlm'] is False
            assert job.payload['_preserveApprovedText'] is True and job.payload['allowLlmFallback'] is False
            assert job.payload['articleRepurpose']['package_version']==manifest['package_version']
            assert all(a['metadata']['article_repurpose']['article_sha256']==manifest['material']['source']['article_sha256'] for a in job.payload['artifacts'])
    for account in (43,58,61,125):
        slots=sorted(datetime.fromisoformat(o['schedule_at']) for o in result['outcomes'] if o['account_id']==account)
        assert all((b-a).total_seconds()>=1800 for a,b in zip(slots,slots[1:]))


def test_failed_atomic_install_leaves_no_jobs_or_partial_intents(local):
    selected,*_=local; original=jobs.enqueue_job_in_transaction; calls=0
    def interrupted(*args,**kwargs):
        nonlocal calls
        calls+=1
        if calls==4:raise OSError('local simulated transaction interruption')
        return original(*args,**kwargs)
    with patch.object(jobs,'enqueue_job_in_transaction',side_effect=interrupted):
        with pytest.raises(OSError):install(local)
    assert counts(selected)==(0,0,0,0)
    assert install(local)['receipt_valid'] is True and counts(selected)==(6,6,1,6)


def test_lost_response_inspects_original_without_files_or_authority(local,monkeypatch):
    selected,task,manifest,media=local
    with patch.object(release,'inspect',side_effect=OSError('local lost response after commit')):
        with pytest.raises(OSError):install(local)
    assert counts(selected)==(6,6,1,6)
    os.unlink(task['manifest_path']);monkeypatch.setenv('SAU_TEACHING_REPURPOSE_RELEASE_ENABLED','0')
    def forbidden(*args):raise AssertionError('Original installed release must not request new approval')
    result=install(local,authorize=forbidden)
    assert result['all_deliveries_confirmed'] is False and counts(selected)==(6,6,1,6)


@pytest.mark.parametrize('authorize',[None,lambda *args:{'approved':True}])
def test_json_or_missing_owner_cannot_install(local,authorize):
    with pytest.raises(contracts.RepurposeError):install(local,authorize=authorize)
    assert counts(local[0])==(0,0,0,0)


def test_new_release_id_cannot_repost_known_article(local):
    install(local)
    with pytest.raises(contracts.RepurposeError,match='released_source_requires_reconciliation_not_repost'):
        install(local,identifier='another-local-release')
    assert counts(local[0])==(6,6,1,6)


def test_partial_and_accepted_are_not_all_deliveries_confirmed(local):
    result=install(local);selected=local[0]
    unit_receipt(selected,result['outcomes'][0])
    unit_receipt(selected,result['outcomes'][1],confirmed=False)
    value=release.inspect(selected,'legacy','local-release')
    assert value['outcomes'][0]['actual_delivery_confirmed'] is True
    assert value['outcomes'][1]['actual_delivery_confirmed'] is False and value['all_deliveries_confirmed'] is False
    for outcome in result['outcomes']:unit_receipt(selected,outcome)
    assert release.inspect(selected,'legacy','local-release')['all_deliveries_confirmed'] is True
    assert counts(selected)==(6,6,1,6)


@pytest.mark.parametrize('receipt',[
    [], {'provider':[],'id':'local-unit','status':'delivered'},
    {'provider':'instagram','id':True,'status':'delivered'}, {'provider':'instagram','id':-1,'status':'delivered'},
    {'provider':'instagram','id':'confirmed','status':'delivered'},
    {'provider':'instagram','id':'local-unit','status':'delivered','delivered_at':'2026-11-16T00:00:00'},
])
def test_malformed_provider_data_is_unknown_not_success_or_new_send(local,receipt):
    result=install(local);selected=local[0]
    unit_receipt(selected,result['outcomes'][0],changes={'provider_receipt':receipt})
    value=release.inspect(selected,'legacy','local-release')
    assert value['outcomes'][0]['actual_delivery_confirmed'] is False and value['all_deliveries_confirmed'] is False
    assert counts(selected)==(6,6,1,6)


def test_mutated_caption_and_job_target_bindings_refuse(local):
    result=install(local);selected=local[0];identifier=result['outcomes'][0]['job_id']
    with selected.connect(write=True) as conn:
        original=conn.execute('SELECT payload_json FROM publish_jobs WHERE id=?',(identifier,)).fetchone()[0]
        payload=json.loads(original);payload['draft']=[]
        conn.execute('UPDATE publish_jobs SET payload_json=? WHERE id=?',(json.dumps(payload),identifier))
    with pytest.raises(contracts.RepurposeError,match='delivery_payload_changed'):release.inspect(selected,'legacy','local-release')
    with pytest.raises(contracts.RepurposeError,match='release_not_found'):release.inspect(selected,'foreign','local-release')
    assert counts(selected)==(6,6,1,6)


def test_effect_guard_does_not_persist_a_new_request_when_owner_unavailable():
    from myUtils.media_restore import PrerequisiteUnavailable
    payload={'articleRepurpose':{'release_id':'local'}};saved=[];payload['_save_checkpoint']=lambda value:saved.append(value)
    with pytest.raises(PrerequisiteUnavailable):delivery.starting(payload,operation='local-unit-create')
    assert saved==[] and delivery.state(payload)=={}
    payload['_delivery']={'provider_receipt':{'provider':'instagram','id':'local-unit-receipt','status':'processing'}}
    delivery.confirmed(payload,provider='instagram',receipt_id='local-unit-receipt')
    assert delivery.state(payload)['provider_receipt']['status']=='delivered'


def test_fresh_signed_dispatch_only_authorizes_original_slots_and_jobs(local,tmp_path,monkeypatch):
    import base64
    import hashlib
    import shutil
    import subprocess
    from pathlib import Path
    from myUtils.repurpose import authority,dispatch
    selected,task,manifest,media=local;result=install(local);source=manifest['material']['source']
    program=shutil.which('ssh-keygen');assert program
    key=tmp_path/'dispatch-fixture-key';subprocess.run([program,'-q','-t','ed25519','-N','','-f',str(key)],check=True,capture_output=True,timeout=15)
    root=tmp_path/'owner-receipts';root.mkdir(mode=0o700);signers=tmp_path/'dispatch-signers'
    signers.write_text(authority.PRINCIPAL+' '+Path(str(key)+'.pub').read_text().strip()+'\n',encoding='utf-8');signers.chmod(0o600)
    for name,value in {'SAU_TEACHING_REPURPOSE_ROOT':str(selected.root),'SAU_TEACHING_OWNER_ALLOWED_SIGNERS':str(signers),'SAU_TEACHING_OWNER_RECEIPTS_ROOT':str(root)}.items():monkeypatch.setenv(name,value)
    outcome=result['outcomes'][0];target=jobs.get_target(outcome['target_ids'][0],db_path=selected.db_path);payload=jobs.get_job(target.job_id,db_path=selected.db_path).payload
    due=datetime.fromisoformat(outcome['schedule_at']).replace(tzinfo=timezone.utc);observed=due-timedelta(seconds=5)
    data={'kind':dispatch.KIND,'schema_version':2,'release_id':'local-release','review_version':'b'*64,'package_version':manifest['package_version'],
          'task_id':task['id'],'page_id':source['page_id'],'article_sha256':source['article_sha256'],'canonical_url':'https://iamwillywang.com/post/'+source['slug'],
          'observed_at':observed.isoformat(),'intent_id':'a'*32,'website_commit':'c'*40,'website_evidence_sha256':'d'*64,
          'queue_slot_at':(observed-timedelta(hours=1)).isoformat(),'approval_time':(observed-timedelta(days=3)).isoformat(),'source_guard_sha256':'e'*64,
          'deliveries':[{k:o[k] for k in dispatch.RECEIPT_KEYS} for o in result['outcomes']]}
    def sign(value):
        run=subprocess.run([program,'-Y','sign','-f',str(key),'-n',authority.NAMESPACE],input=contracts.json_bytes(value),check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
        return {'ticket':value,'signature':base64.b64encode(run.stdout).decode('ascii')}
    def grant(value):return dispatch.install(selected,'legacy',sign(value),allowed_signers=signers,private_root=root,now=datetime.fromisoformat(value['observed_at']))
    with pytest.raises(contracts.RepurposeError,match='dispatch_authority_not_ready'):dispatch.assert_permission(payload,target,db_path=selected.db_path,now=due)
    grant(data)
    with pytest.raises(contracts.RepurposeError,match='dispatch_before_original_slot'):dispatch.assert_permission(payload,target,db_path=selected.db_path,now=due-timedelta(seconds=1))
    dispatch.assert_permission(payload,target,db_path=selected.db_path,now=due)
    changed=copy.deepcopy(payload);changed['message']='changed derivative'
    with pytest.raises(contracts.RepurposeError,match='dispatch_runtime_payload_changed'):dispatch.assert_permission(changed,target,db_path=selected.db_path,now=due)
    with pytest.raises(contracts.RepurposeError,match='owner_authority_stale'):dispatch.assert_permission(payload,target,db_path=selected.db_path,now=due+timedelta(seconds=61))
    bad=copy.deepcopy(data);bad['deliveries'][0]['job_id']=True
    with pytest.raises(contracts.RepurposeError):grant(bad)
    history={p.name:p.read_bytes() for p in root.glob('dispatch-*.json')}
    newer={**data,'observed_at':(observed+timedelta(seconds=1)).isoformat()};grant(newer)
    assert len(list(root.glob('dispatch-*.json')))==len(history)+1 and all((root/name).read_bytes()==raw for name,raw in history.items())
    assert counts(selected)==(6,6,1,6)
    with selected.connect() as conn:assert all(r[0]==0 for r in conn.execute('SELECT attempts FROM publish_job_targets'))


def test_private_api_previews_regeneration_and_scopes(local,monkeypatch):
    import hashlib
    from flask import Flask,g
    from sau_app.repurpose import register
    from sau_app.auth.permissions import PUBLISH_WRITE
    from sau_app.tenancy.context import AuthContext
    from sau_app.repurpose import routes
    selected,task,manifest,media=local;auth=[AuthContext.legacy_token()]
    app=Flask(__name__);app.config.update(SAU_DB_PATH=str(selected.db_path),TEACHING_REPURPOSE_ROOT=str(selected.root));register(app)
    @app.before_request
    def authenticate():g.auth_ctx=auth[0]
    client=app.test_client();base='/api/publish/teaching-repurpose';monkeypatch.setenv('SAU_TEACHING_REPURPOSE_ENABLED','1')
    listed=client.get(base+'/tasks').get_json()['data'];assert 'manuscript' not in listed[0]['source'] and listed[0]['article_read_only'] is True
    details=client.get(base+'/tasks/'+str(task['id'])).get_json()['data'];assert details['source']['manuscript']==manifest['material']['source']['manuscript']
    image=client.get(details['asset_base']+'deck-01.jpg');descriptor=next(a for a in manifest['material']['assets'] if a['name']=='deck-01.jpg')
    assert image.status_code==200 and image.mimetype=='image/jpeg' and hashlib.sha256(image.data).hexdigest()==descriptor['sha256']
    assert image.headers['Cache-Control']=='private, no-store' and image.headers['X-Content-Type-Options']=='nosniff'
    with patch.object(routes,'read_file',return_value=b'x'*descriptor['size_bytes']):
        assert client.get(details['asset_base']+'deck-01.jpg').status_code==409
    route=base+'/tasks/'+str(task['id'])+'/regenerate';request={'request_id':'local-new-review','kit_version':details['kit_version'],'deliveries':details['delivery_ids']}
    response=client.post(route,json=request);assert response.status_code==202 and response.get_json()['code']==200
    new_id=response.get_json()['data']['id'];assert selected.task('legacy',new_id)['state']=='queued' and selected.task('legacy',task['id'])['state']=='prepared'
    assert json.loads(selected.task('legacy',new_id)['request_json'])['source']==manifest['material']['source']
    assert client.post(route,json={**request,'source':{'manuscript':'unapproved'}}).status_code==409
    auth[0]=AuthContext(True,'google_session',workspace_id='foreign',permissions=frozenset({PUBLISH_WRITE}))
    assert client.get(base+'/tasks/'+str(task['id'])).status_code==403
    auth[0]=AuthContext.anonymous();assert client.get(base+'/status').status_code==401
    auth[0]=AuthContext.legacy_token();installed=install(local)
    assert client.post(route,json={**request,'request_id':'forbidden-repost'}).status_code==409
    os.unlink(task['manifest_path'])
    public='/teaching-repurpose-media/'+manifest['package_version']+'/deck-01.jpg'
    response=client.get(public);assert response.status_code==200 and hashlib.sha256(response.data).hexdigest()==descriptor['sha256']
    assert client.get('/teaching-repurpose-media/'+manifest['package_version']+'/manuscript.md').status_code==404
    monkeypatch.setenv('SAU_TEACHING_REPURPOSE_RELEASE_ENABLED','0')
    assert client.get(public).status_code==404
    assert client.get(base+'/releases/local-release').get_json()['data']['all_deliveries_confirmed'] is False
    assert counts(selected)==(6,6,1,6)


def test_target_status_without_consumed_provider_budget_is_not_confirmation(local):
    result=install(local);selected=local[0];outcome=result['outcomes'][0]
    unit_receipt(selected,outcome,changes={'publish_routes':{'active':'native','native':{'attempts':0,'max_attempts':3}}})
    assert release.inspect(selected,'legacy','local-release')['outcomes'][0]['actual_delivery_confirmed'] is False


def test_callback_is_execution_only_and_missing_owner_defers_before_provider(local):
    from myUtils import worker
    from myUtils.media_restore import PrerequisiteUnavailable
    result=install(local);selected=local[0];outcome=result['outcomes'][0]
    target=jobs.get_target(outcome['target_ids'][0],db_path=selected.db_path);job=jobs.get_job(target.job_id,db_path=selected.db_path)
    payload=copy.deepcopy(job.payload);payload['_teaching_dispatch_check']=lambda:None
    assert '_teaching_dispatch_check' not in worker._publication_payload(payload)
    with patch.object(worker,'_resolve_structured_account',side_effect=AssertionError('No provider/media I/O before Teaching authority')):
        with pytest.raises(PrerequisiteUnavailable):worker._prepare_target_prerequisites(target,job,payload,db_path=selected.db_path)
    with selected.connect() as conn:assert all(r[0]==0 for r in conn.execute('SELECT attempts FROM publish_job_targets'))


def test_delivery_capacity_hold_preserves_assets_and_has_no_install(local):
    from myUtils.media_restore import PrerequisiteUnavailable
    with patch('myUtils.repurpose.pipeline.check_capacity',side_effect=PrerequisiteUnavailable('capacity')):
        with pytest.raises(contracts.RepurposeError,match='repurpose_delivery_capacity'):install(local)
    assert counts(local[0])==(0,0,0,0) and list(local[3].iterdir())==[]
    assert local[1]['attempts']==1


def test_default_off_status_needs_no_private_storage_or_database(tmp_path,monkeypatch):
    from flask import Flask,g
    from sau_app.repurpose import register
    from sau_app.tenancy.context import AuthContext
    database=tmp_path/'must-not-be-created.sqlite3'
    app=Flask(__name__);app.config['SAU_DB_PATH']=str(database);register(app)
    @app.before_request
    def authenticate():g.auth_ctx=AuthContext.legacy_token()
    monkeypatch.delenv('SAU_TEACHING_REPURPOSE_ROOT',raising=False)
    monkeypatch.setenv('SAU_TEACHING_REPURPOSE_ENABLED','0')
    data=app.test_client().get('/api/publish/teaching-repurpose/status').get_json()['data']
    assert data['enabled'] is False and data['storage_configured'] is False
    assert data['account_ids']==[43,58,61,125] and not database.exists()
