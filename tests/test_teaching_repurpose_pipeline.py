"""Executed local-only preparation and authorization checks; never publish a fixture."""
import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask, g

from myUtils.repurpose import contracts, pipeline, writing
from myUtils.repurpose.brand import default_kit
from myUtils.repurpose.files import verify_package
from sau_app.auth.permissions import PUBLISH_WRITE
from sau_app.repurpose import register
from sau_app.tenancy.context import AuthContext
from scripts.repurpose_rules import bundle
from tests.repurpose_fixtures import source, board


def local_rules(tmp_path):
    configured = os.environ.get('REPURPOSE_TEST_RULES_ROOT')
    if not configured:
        pytest.skip('Real private rules bundle is required for executed humanizer tests')
    return Path(configured)


def test_voice_and_humanizer_run_before_source_comparison(tmp_path):
    rules = writing.Rules(local_rules(tmp_path))
    original = source(); data = board(original); calls = []
    def generate(system, prompt, **kwargs):
        calls.append(system)
        if len(calls) <= 2:
            assert '威威' in system and 'Humanizer' in system
            return SimpleNamespace(content=json.dumps(data,ensure_ascii=False))
        return SimpleNamespace(content=json.dumps({'supported':True,'issues':[]}))
    result, checks = writing.generate_storyboard(original, rules, tmp_path, generate=generate)
    assert result == data and [c['name'] for c in checks] == ['voice','humanizer','source-fidelity']
    assert len(calls) == 3 and 'Mode A' in calls[1]
    assert (tmp_path/'humanizer.log').is_file()
    assert source() == original


def test_failed_humanizer_never_returns_fallback(tmp_path):
    rules = writing.Rules(local_rules(tmp_path)); data=board(); data['deck'][0]['lines']=['這不是輸入，而是輸出。']
    count=0
    def generate(*args,**kwargs):
        nonlocal count
        count+=1
        return SimpleNamespace(content=json.dumps(data if count<=2 else {'supported':True,'issues':[]},ensure_ascii=False))
    with pytest.raises(contracts.RepurposeError, match='voice'):
        writing.generate_storyboard(source(),rules,tmp_path,generate=generate)
    assert not (tmp_path/'manifest.json').exists()


def test_actual_private_cards_video_and_manifest(tmp_path):
    local_rules(tmp_path)
    source_root=Path(__file__).resolve().parents[1]
    db=tmp_path/'isolated.sqlite3'
    env={**os.environ,'SAU_DB_PATH':str(db),'PYTHONDONTWRITEBYTECODE':'1'}
    bootstrap=subprocess.run([sys.executable,'-B',str(source_root/'db/createTable.py')],cwd=source_root,env=env,capture_output=True,timeout=60)
    assert bootstrap.returncode == 0, bootstrap.stderr.decode(errors='replace')
    data=board();calls=0
    def generate(*args,**kwargs):
        nonlocal calls
        calls+=1
        return SimpleNamespace(content=json.dumps(data if calls<=2 else {'supported':True,'issues':[]},ensure_ascii=False))
    package=pipeline.produce(source(),default_kit(),tmp_path/'output',rules_root=local_rules(tmp_path),db_path=db,generate=generate)
    manifest=verify_package(package)
    assert manifest['publication_authorized'] is False
    assert [d['id'] for d in manifest['material']['deliveries']] == contracts.DEFAULT_DELIVERIES
    assert len(next(d for d in manifest['material']['deliveries'] if d['id']=='125:cards')['asset_names']) == 4
    video=next(a for a in manifest['material']['assets'] if a['name']=='summary.mp4')
    assert video['decoded'] and 45<=video['duration_seconds']<=90
    assert video['width']==1080 and video['height']==1920 and video['fps']==30
    assert (package.parent/'manuscript.md').read_bytes() == source()['manuscript'].encode()
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM publish_jobs').fetchone()[0] == 0
    from myUtils.repurpose.store import Store
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT OR IGNORE INTO profiles(id,name,slug) VALUES(4,'Teaching fixture','teaching-fixture')")
    owner=Store(db,tmp_path)
    version=owner.add_kit('legacy',default_kit())
    queued=owner.enqueue('legacy','fixture-registration',source(),version)
    lease=owner.claim()
    assert lease['id']==queued['id']
    owner.finish(lease,manifest_path=package)
    with owner.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM repurpose_assets').fetchone()[0]==28
        assert conn.execute('SELECT COUNT(*) FROM media_assets').fetchone()[0]==28
        assert conn.execute('SELECT COUNT(*) FROM file_records').fetchone()[0]==28
        assert conn.execute('SELECT COUNT(*) FROM publish_jobs').fetchone()[0]==0
        assert all(r['upload_status']=='private' and not r['public_url'] for r in conn.execute('SELECT * FROM media_assets'))
    with pytest.raises(contracts.RepurposeError,match='lease_lost'):
        owner.finish(lease,manifest_path=package)
    assert owner.task('legacy',lease['id'])['attempts']==1
    (package.parent/'deck-01.jpg').write_bytes(b'changed')
    with pytest.raises(contracts.RepurposeError,match='asset_changed'):
        verify_package(package)


def test_authenticated_status_never_enables_release(tmp_path):
    app=Flask(__name__)
    register(app);register(app)
    assert len([rule for rule in app.url_map.iter_rules() if rule.rule.endswith('/teaching-repurpose/status')])==1
    @app.before_request
    def auth():
        g.auth_ctx=AuthContext.legacy_token()
    with patch.dict(os.environ,{'SAU_TEACHING_REPURPOSE_ENABLED':'0','SAU_TEACHING_REPURPOSE_RELEASE_ENABLED':'0'}):
        data=app.test_client().get('/api/publish/teaching-repurpose/status').get_json()['data']
        assert data['enabled'] is data['release_enabled'] is False and data['approval_location']=='notion'


@pytest.mark.parametrize('context,code',[(AuthContext.anonymous(),401),(AuthContext(True,'google_session',workspace_id='other',permissions=frozenset()),403)])
def test_preview_authorization(context,code):
    app=Flask(__name__);register(app)
    @app.before_request
    def auth():g.auth_ctx=context
    response=app.test_client().get('/api/publish/teaching-repurpose/status')
    assert response.status_code==code and 'manuscript' not in response.get_data(as_text=True)


def test_media_surface_refuses_unreleased_package():
    app=Flask(__name__);register(app)
    with patch.dict(os.environ,{'SAU_TEACHING_REPURPOSE_RELEASE_ENABLED':'0'}):
        response=app.test_client().get('/teaching-repurpose-media/'+'a'*64+'/summary.mp4')
        assert response.status_code==404 and response.get_json()['data'] is None
