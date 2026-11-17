"""Execute OpenSSH signatures on ephemeral local fixture keys; never user keys."""
import base64
import copy
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from myUtils.repurpose import authority, contracts
from tests.repurpose_fixtures import produce_verified_fixture

verified_fixture = pytest.fixture(scope='session')(produce_verified_fixture)


@pytest.fixture
def signer(tmp_path, verified_fixture):
    program = shutil.which('ssh-keygen')
    assert program, 'OpenSSH verification is a required release dependency'
    key = tmp_path / 'ephemeral-test-key'
    subprocess.run([program,'-q','-t','ed25519','-N','','-f',str(key)],check=True,capture_output=True,timeout=15)
    root = tmp_path / 'owner-receipts'; root.mkdir(mode=0o700)
    allowed = tmp_path / 'allowed-signers'
    allowed.write_text(authority.PRINCIPAL+' '+Path(str(key)+'.pub').read_text(encoding='utf-8').strip()+'\n',encoding='utf-8')
    allowed.chmod(0o600)
    manifest = verified_fixture[1]; source = manifest['material']['source']; now = datetime.now(timezone.utc)
    data = {'kind':'teaching-owner-handoff','schema_version':2,'release_id':'local-release','review_version':'b'*64,
            'package_version':manifest['package_version'],'task_id':1,'page_id':source['page_id'],'article_sha256':source['article_sha256'],
            'canonical_url':'https://iamwillywang.com/post/'+source['slug'],'observed_at':now.isoformat(),
            'intent_id':'a'*32,'website_commit':'c'*40,'website_evidence_sha256':'d'*64,
            'queue_slot_at':(now-timedelta(minutes=1)).isoformat(),'approval_time':(now-timedelta(days=3)).isoformat(),'source_guard_sha256':'e'*64}
    def sign(body=None, namespace=authority.NAMESPACE):
        body = copy.deepcopy(data if body is None else body)
        result = subprocess.run([program,'-Y','sign','-f',str(key),'-n',namespace],input=contracts.json_bytes(body),
                                check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
        return {'ticket':body,'signature':base64.b64encode(result.stdout).decode('ascii')}
    def verify(envelope, **kwargs):
        return authority.verify_signed(envelope,manifest,allowed_signers=allowed,private_root=root,now=now,**kwargs)
    return sign, verify, data, manifest, root, allowed, now


def test_actual_signature_binds_current_owner_and_exact_package(signer):
    sign,verify,data,manifest,root,allowed,now=signer
    proof=verify(sign())
    assert proof.page_id==data['page_id'] and proof.package_version==manifest['package_version']
    assert proof.observed_at==now and list(root.iterdir())==[]


@pytest.mark.parametrize('kind',['tampered','foreign-namespace','missing-signature','supplied-key'])
def test_signature_is_not_json_authority(signer,kind):
    sign,verify,data,*_=signer; envelope=sign()
    if kind=='tampered':envelope['ticket']['article_sha256']='f'*64
    elif kind=='foreign-namespace':envelope=sign(namespace='unrelated-local-test')
    elif kind=='missing-signature':envelope['signature']='not-base64'
    else:envelope['public_key']='caller-supplied-key'
    with pytest.raises(contracts.RepurposeError):verify(envelope)


@pytest.mark.parametrize('change',[
    {'observed_at':'2000-01-01T00:00:00+00:00'}, {'observed_at':'2026-11-16T01:00:00'},
    {'approval_time':'invalid'}, {'queue_slot_at':False}, {'schema_version':True}, {'task_id':True},
    {'page_id':'00000000-0000-4000-8000-000000000099'}, {'package_version':'f'*64},
    {'canonical_url':'https://unrelated.invalid/post/local'}, {'release_id':['invalid']},
])
def test_valid_signature_does_not_bypass_source_clock_or_typed_fields(signer,change):
    sign,verify,data,*_=signer; data={**data,**change}
    with pytest.raises(contracts.RepurposeError):verify(sign(data))


def test_late_or_missing_verifier_is_a_coded_hold(signer):
    sign,verify,*_=signer
    def unavailable(*args,**kwargs):raise subprocess.TimeoutExpired(args[0],15)
    with pytest.raises(contracts.RepurposeError,match='owner_signature_verifier_unavailable'):
        verify(sign(),runner=unavailable)


def test_owner_receipt_is_create_only_and_cannot_be_renewed(signer):
    sign,verify,data,manifest,root,allowed,now=signer; envelope=sign()
    options={'allowed_signers':allowed,'private_root':root,'now':now}
    authority.install_signed(envelope,manifest,**options)
    path=authority.receipt_path(root,data['release_id'],data['review_version'],data['package_version']); raw=path.read_bytes()
    authority.install_signed(envelope,manifest,**options)
    altered={**data,'intent_id':'f'*32}
    with pytest.raises(contracts.RepurposeError,match='immutable_owner_handoff_conflict'):
        authority.install_signed(sign(altered),manifest,**options)
    with pytest.raises(contracts.RepurposeError,match='owner_authority_stale'):
        authority.install_signed(envelope,manifest,allowed_signers=allowed,private_root=root,now=now+timedelta(seconds=61))
    assert path.read_bytes()==raw
