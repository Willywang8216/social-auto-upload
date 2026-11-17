"""Isolated core tests. No Notion, credentials, native publish or real queue reads."""
import copy
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from myUtils.repurpose import contracts, files, store, writing
from myUtils.repurpose.brand import default_kit
from myUtils.repurpose.cards import render_card
from myUtils.repurpose.release import ReleaseAuthority, authority_check
from myUtils.repurpose.video import timing
from tests.repurpose_fixtures import source, board


@pytest.fixture
def local_store(tmp_path):
    root = tmp_path / 'private'; root.mkdir()
    db = tmp_path / 'queue.sqlite3'
    with sqlite3.connect(db) as conn:
        store.install_schema(conn)
    return store.Store(db, root)


def kit():
    try:
        return default_kit()
    except contracts.RepurposeError as exc:
        if exc.code == 'teaching_cjk_font_missing':
            pytest.skip('CJK renderer dependency not available on this local test host')
        raise


def test_source_preserves_exact_article_bytes():
    value = source()
    assert contracts.validate_source(value) == value
    changed = {**value, 'manuscript': value['manuscript'] + '\n'}
    with pytest.raises(contracts.RepurposeError, match='source_hash'):
        contracts.validate_source(changed)


@pytest.mark.parametrize('key,value', [('database_id','elsewhere'), ('page_id','not-a-uuid'), ('slug','../unsafe'), ('article_version','published')])
def test_source_identity_refuses(key, value):
    original = source(); original[key] = value
    with pytest.raises(contracts.RepurposeError):
        contracts.validate_source(original)


def test_source_refs_bind_actual_quotes_and_order():
    value = board()
    assert contracts.validate_storyboard(value, source()) == value
    changed = copy.deepcopy(value); changed['deck'][0]['source_refs'][0]['quote'] = '編出來的數字'
    with pytest.raises(contracts.RepurposeError, match='unverified_source_reference'):
        contracts.validate_storyboard(changed, source())
    changed = copy.deepcopy(value); changed['video_slide_ids'].reverse()
    with pytest.raises(contracts.RepurposeError, match='video_must_cover'):
        contracts.validate_storyboard(changed, source())


def test_bluesky_is_not_a_silently_truncated_deck():
    value = board(); value['bluesky'] = value['bluesky'][:3]
    with pytest.raises(contracts.RepurposeError, match='slide_count'):
        contracts.validate_storyboard(value, source())


@pytest.mark.parametrize('plan', [['43:cards'], contracts.DEFAULT_DELIVERIES + ['109:video'], contracts.DEFAULT_DELIVERIES + ['43:cards']])
def test_destination_scope(plan):
    with pytest.raises(contracts.RepurposeError):
        contracts.validate_deliveries(plan)


def test_request_id_is_material_identity_not_permission(local_store):
    config = kit(); version = local_store.add_kit('teaching', config)
    first = local_store.enqueue('teaching', 'stable-request', source(), version)
    assert local_store.enqueue('teaching', 'stable-request', source(), version)['id'] == first['id']
    other = source(); other['article_version'] = 'b' * 64
    with pytest.raises(contracts.RepurposeError, match='request_id_material_changed'):
        local_store.enqueue('teaching', 'stable-request', other, version)
    with pytest.raises(contracts.RepurposeError, match='kit_not_found'):
        local_store.kit('other-workspace', version)


def test_one_render_lease_and_crash_hold_keep_attempts(local_store):
    version = local_store.add_kit('teaching', kit())
    first = local_store.enqueue('teaching', 'first', source(), version)
    local_store.enqueue('teaching', 'second', source(), version)
    now = datetime(2026,11,16,tzinfo=timezone.utc)
    lease = local_store.claim(now=now, lease_seconds=60)
    assert lease['attempts'] == 1 and lease['id'] == first['id']
    assert local_store.claim(now=now) is None
    other = local_store.claim(now=now + timedelta(seconds=61))
    interrupted = local_store.task('teaching', first['id'])
    assert interrupted['state'] == 'held' and interrupted['reason_code'] == 'preparation_interrupted' and interrupted['attempts'] == 1
    assert other['id'] != lease['id']
    with pytest.raises(contracts.RepurposeError, match='lease_lost'):
        local_store.finish(lease, reason='invalid', now=now + timedelta(seconds=62))


def test_cancelled_worker_cannot_finish(local_store):
    version = local_store.add_kit('teaching', kit())
    task = local_store.enqueue('teaching', 'cancel-me', source(), version)
    lease = local_store.claim()
    local_store.cancel('teaching', task['id'])
    with pytest.raises(contracts.RepurposeError, match='lease_lost'):
        local_store.finish(lease, reason='not-success')
    assert local_store.task('teaching', task['id'])['attempts'] == 1


def test_persistence_does_not_mutate_publish_tables(local_store):
    with local_store.connect(write=True) as conn:
        conn.execute('CREATE TABLE publish_jobs(id INTEGER PRIMARY KEY,payload_json TEXT)')
        conn.execute("INSERT INTO publish_jobs VALUES(1,'unchanged')")
    version = local_store.add_kit('teaching', kit())
    local_store.enqueue('teaching', 'new', source(), version)
    with local_store.connect() as conn:
        assert tuple(conn.execute('SELECT * FROM publish_jobs').fetchone()) == (1,'unchanged')


def test_no_rules_means_no_generation(tmp_path):
    with pytest.raises(contracts.RepurposeError):
        writing.Rules(tmp_path / 'missing')


def test_generated_copy_voice_and_fidelity_refuse():
    data = board(); data['deck'][0]['lines'] = ['正確率是 98%。']
    with pytest.raises(contracts.RepurposeError, match='unsupported_number'):
        writing.check_fidelity(data)
    data = board(); data['deck'][0]['lines'] = ['$f(x)=3x+1$']
    with pytest.raises(contracts.RepurposeError):
        writing.check_fidelity(data)
    data = board(); data['deck'][0]['lines'] = ['我記得學生突然懂了']
    with pytest.raises(contracts.RepurposeError, match='unsupported_personal_experience'):
        writing.check_fidelity(data)


def test_private_file_is_create_only(tmp_path):
    target = tmp_path / 'fixed.txt'; files.write_new(target, b'exact')
    with pytest.raises(FileExistsError):
        files.write_new(target, b'replacement')
    assert target.read_bytes() == b'exact'


def test_private_paths_reject_repository_and_symlink(tmp_path):
    with pytest.raises(contracts.RepurposeError):
        files.private_path(Path(contracts.__file__).parent / 'private.json')
    with pytest.raises(contracts.RepurposeError):
        files.private_path('../private')


def test_cjk_card_real_dimensions_and_overflow(tmp_path):
    value = board()['deck'][0]
    path = tmp_path / 'card.png'
    facts = render_card(value, 1, 6, kit(), path)
    assert facts['cjk_glyphs_verified'] and (facts['width'],facts['height']) == (1080,1350)
    changed = copy.deepcopy(value); changed['lines'] = ['文字' * 800]
    with pytest.raises(contracts.RepurposeError, match='overflow'):
        render_card(changed, 1, 6, kit(), tmp_path / 'bad.png')
    assert not (tmp_path / 'bad.png').exists()


def test_video_timing_requires_full_readable_summary():
    times = timing(board()['deck'])
    assert len(times) == 6 and 45 <= sum(times) <= 90
    crowded = copy.deepcopy(board()['deck']); crowded[0]['lines'] = ['很多文字' * 100]
    with pytest.raises(contracts.RepurposeError, match='summary_reading_time_overflow'):
        timing(crowded)


def test_json_approval_and_stale_authority_cannot_release():
    s = source(); m = {'package_version':'c'*64,'material':{'source':s}}
    now = datetime(2026,11,16,tzinfo=timezone.utc)
    with pytest.raises(contracts.RepurposeError, match='independent_owner_authority_required'):
        authority_check(lambda *args:{'approved':True}, 'release', 'b'*64, m, now)
    proof = ReleaseAuthority('release','b'*64,'c'*64,s['page_id'],s['article_sha256'],'https://iamwillywang.com/post/'+s['slug'],now)
    assert authority_check(lambda *args:proof,'release','b'*64,m,now) == proof
    with pytest.raises(contracts.RepurposeError, match='owner_authority_stale'):
        authority_check(lambda *args:proof,'release','b'*64,m,now+timedelta(seconds=61))


def test_duplicate_json_keys_and_nonfinite_refuse():
    for raw in ('{"x":1,"x":2}', '{"x":NaN}'):
        with pytest.raises(contracts.RepurposeError):
            contracts.loads(raw)


def test_cancelled_lease_cannot_do_more_preparation(local_store):
    version=local_store.add_kit('teaching',kit())
    task=local_store.enqueue('teaching','cancel-checkpoint',source(),version)
    lease=local_store.claim()
    local_store.assert_lease(lease)
    local_store.cancel('teaching',task['id'])
    with pytest.raises(contracts.RepurposeError,match='lease_lost'):
        local_store.assert_lease(lease)


def test_deadline_refuses_before_creating_output(tmp_path):
    from myUtils.repurpose import pipeline
    with patch.object(pipeline.time,'monotonic',side_effect=[100,102]):
        with pytest.raises(contracts.RepurposeError,match='preparation_deadline_exceeded'):
            pipeline.produce(source(),kit(),tmp_path/'not-created',rules_root=tmp_path,db_path=tmp_path/'absent.db',deadline_seconds=1)
    assert not (tmp_path/'not-created').exists()
