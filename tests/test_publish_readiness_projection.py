"""R8 stored-readiness regressions: isolated SQLite and mocked provider writes."""
from __future__ import annotations

import copy
import json
import socket
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from myUtils import publish_readiness as readiness, publish_health as health, pipeline_monitor as monitor, token_state

NOW = datetime(2030, 4, 5, 12, tzinfo=timezone.utc)


def stamp(seconds=0):
    return (NOW + timedelta(seconds=seconds)).isoformat()


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(readiness, '_now', lambda: NOW)
    monkeypatch.setattr(token_state, 'utc_now', lambda: NOW)
    monkeypatch.setattr(socket.socket, 'connect', Mock(side_effect=AssertionError('provider calls forbidden')))
    monkeypatch.setenv('SAU_TESTING', '1')
    monkeypatch.setenv('SAU_SOCIAMONIALS_API_KEY', 'mock-managed-secret')
    monkeypatch.delenv('SAU_CONFIG_ENCRYPTION_KEY', raising=False)


@pytest.fixture
def account():
    return {'id': 1, 'profile_id': 1, 'workspace_id': 'tenant-a', 'platform': 'twitter',
            'enabled': True, 'cookie_path': '', 'config': {
                'accessToken': 'mock-native-secret', 'twitterUserId': 'native-123',
                'accessTokenExpiresAt': stamp(864000), 'requiredCapabilities': ['video'],
                'displayName': 'Historical name', 'connectedAt': stamp(-864000)}}


@pytest.fixture
def db(tmp_path, account):
    path = tmp_path / 'readiness.db'
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript('''
            CREATE TABLE profiles(id INTEGER PRIMARY KEY,workspace_id TEXT,settings_json TEXT);
            CREATE TABLE accounts(id INTEGER PRIMARY KEY,profile_id INTEGER,workspace_id TEXT,
                platform TEXT,enabled INTEGER,cookie_path TEXT,config_json TEXT,nickname TEXT,account_name TEXT);
            CREATE TABLE publish_destination_readiness(account_id INTEGER PRIMARY KEY,workspace_id TEXT,
                provider TEXT,config_fingerprint TEXT,status TEXT,reason TEXT,evidence_json TEXT,
                checked_at TEXT,expires_at TEXT);
            CREATE TABLE publish_dependency_states(scope_key TEXT PRIMARY KEY,config_fingerprint TEXT,
                kind TEXT,status TEXT,reason TEXT,blocked_until TEXT,last_checked_at TEXT,updated_at TEXT);
            CREATE TABLE publish_jobs(id INTEGER PRIMARY KEY,platform TEXT,status TEXT,created_at TEXT,
                workspace_id TEXT,payload_json TEXT);
            CREATE TABLE publish_job_targets(id INTEGER PRIMARY KEY,job_id INTEGER,account_ref TEXT,
                file_ref TEXT,status TEXT,schedule_at TEXT,next_attempt_at TEXT,started_at TEXT,finished_at TEXT,
                last_error TEXT,error_category TEXT,delivery_state TEXT,lease_expires_at TEXT,
                lease_token TEXT,lease_owner TEXT,checkpoint_json TEXT);
            CREATE TABLE campaign_posts(id INTEGER PRIMARY KEY,campaign_id INTEGER);
            CREATE TABLE campaigns(id INTEGER PRIMARY KEY,status TEXT);
            CREATE TABLE worker_heartbeats(worker_id TEXT,heartbeat_at TEXT);
        ''')
        conn.execute("INSERT INTO profiles VALUES (1,'tenant-a','{}')")
        conn.execute('INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?)',
                     (1, 1, 'tenant-a', 'twitter', 1, '', json.dumps(account['config']), 'Name', 'Handle'))
    return path


def save_account(db, account):
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute('UPDATE accounts SET config_json=?,enabled=?,workspace_id=?,profile_id=? WHERE id=?',
                     (json.dumps(account['config']), int(account['enabled']), account['workspace_id'], account['profile_id'], account['id']))


def proof(account, provider='direct'):
    context = readiness._context(account, {})
    permissions = {'posts.create': True}
    if provider == 'sociamonials':
        permissions.update({'posts.read': True, 'profiles.read': True, 'posts.publish_direct': True})
    return {'verified': True, 'source': 'provider_verification', 'live_check': True,
            'mapping' if provider == 'sociamonials' else 'identity': copy.deepcopy(context['identity']),
            'config_fingerprint': context['fingerprint'],
            'permissions': permissions, 'capabilities': ['video', 'text'], 'requires_approval': False,
            'verified_at': stamp(-5), 'limits': {'max_video_bytes': 1000},
            'raw_response': {'token': 'must-not-persist', 'url': 'https://private.invalid'}}


def record(db, account, **kwargs):
    provider = account['config'].get('publishingProvider', 'direct')
    return readiness.record_destination_readiness(account, {}, proof=kwargs.pop('proof', proof(account, provider)),
                                                 provider=provider, db_path=db, **kwargs)


def test_name_timestamp_and_future_token_are_not_authorization(db, account):
    body = readiness.destination_readiness(account, db_path=db)
    assert body['phase'] == body['reasonCode'] == 'configured_unverified'
    assert not body['ready'] and not body['evidenceFresh']
    assert body['nativeHealth']['state'] == 'unknown'
    account['config'].update(_nativeCredentialState='verified', _nativeCredentialCheckedAt=stamp(-50))
    save_account(db, account)
    assert readiness.destination_readiness(account, db_path=db)['phase'] == 'configured_unverified'


def test_record_is_safe_fresh_exact_and_explicitly_expires(db, account, monkeypatch):
    result = record(db, account, max_age_seconds=60)
    assert result['ready'] and result['evidenceFresh']
    assert result['expiresAt'] == stamp(55)
    with closing(sqlite3.connect(db)) as conn:
        serialized = conn.execute('SELECT evidence_json FROM publish_destination_readiness').fetchone()[0]
    assert not any(secret in serialized + json.dumps(result) for secret in ('mock-native-secret', 'must-not-persist', 'private.invalid', 'raw_response'))
    monkeypatch.setattr(readiness, '_now', lambda: NOW + timedelta(seconds=60))
    assert readiness.destination_readiness(account, db_path=db)['reasonCode'] == 'evidence_expired'


@pytest.mark.parametrize('mutation', [
    {'verified': False}, {'ready': True, 'verified': None}, {'live_check': False},
    {'source': 'display_name'}, {'identity': {}}, {'identity': {'platform': 'twitter', 'id': '*'}},
    {'identity': {'platform': 'twitter', 'id': 'another-user'}}, {'permissions': {}},
    {'permissions': {'posts.create': 1}}, {'requires_approval': True}, {'requires_approval': None},
    {'config_fingerprint': None}, {'config_fingerprint': 'old-credential-digest'}, {'expires_at': 'invalid'},
    {'capabilities': ['text']}, {'capabilities': ['video', 'mobile_app_handoff']},
    {'verified_at': stamp(5)}, {'verified_at': stamp(-1000)}, {'expires_at': stamp(-1)},
])
def test_bad_proof_does_not_write(db, account, mutation):
    evidence = proof(account)
    evidence.update(mutation)
    with pytest.raises(ValueError):
        record(db, account, proof=evidence)
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM publish_destination_readiness').fetchone()[0] == 0


def test_missing_db_and_tables_get_never_creates(tmp_path, account, db):
    missing = tmp_path / 'absent' / 'missing.db'
    assert not readiness.destination_readiness(account, db_path=missing)['ready']
    assert not missing.parent.exists()
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute('DROP TABLE publish_destination_readiness')
    before = db.read_bytes()
    assert not readiness.destination_readiness(account, db_path=db)['ready']
    assert db.read_bytes() == before


@pytest.mark.parametrize('field,value', [('accessToken', 'new-secret'), ('twitterUserId', 'other-id'), ('scope', 'reduced-grants')])
def test_current_config_and_stale_account_invalidates_proof(db, account, field, value):
    record(db, account)
    old = copy.deepcopy(account)
    account['config'][field] = value
    save_account(db, account)
    assert readiness.destination_readiness(account, db_path=db)['reasonCode'] == 'configuration_changed'
    assert not readiness.destination_readiness(old, db_path=db)['ready']
    with pytest.raises(ValueError):
        record(db, old)


def test_profile_policy_and_tenant_changes_invalidate_proof(db, account):
    record(db, account)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute('UPDATE profiles SET settings_json=?', (json.dumps({'publishing': {'requiredCapabilities': ['story']}}),))
    assert not readiness.destination_readiness(account, db_path=db)['ready']
    foreign = {**account, 'workspace_id': 'tenant-b'}
    assert not readiness.destination_readiness(foreign, db_path=db)['ready']
    with pytest.raises(ValueError):
        record(db, foreign)


@pytest.mark.parametrize('config,reason', [
    ({'_needsReconnect': True}, 'revoked'),
    ({'_nativeCredentialError': {'code': 'invalid_token'}}, 'revoked'),
    ({'accessTokenExpiresAt': stamp(-1)}, 'expired'),
    ({'_nativePublishingError': {'kind': 'budget', 'reason': 'token=raw-secret'}}, 'native_hold'),
    ({'_nativeCredentialError': {'kind': 'configuration', 'reason': 'https://secret.invalid'}}, 'native_hold'),
])
def test_native_holds_and_revocation_override_stored_readiness(db, account, config, reason):
    record(db, account)
    account['config'].update(config)
    save_account(db, account)
    result = readiness.destination_readiness(account, db_path=db)
    assert not result['ready'] and result['reasonCode'] == reason
    assert 'raw-secret' not in json.dumps(result) and 'secret.invalid' not in json.dumps(result)


def test_managed_primary_does_not_repair_native_oauth(db, account, monkeypatch):
    from myUtils import sociamonials_fallback as managed
    monkeypatch.setenv(managed.API_KEY_ENV, 'managed-mock-secret')
    account['config'].update(publishingProvider='sociamonials', _needsReconnect=True,
                             sociamonials={'network': 'tw', 'profile_refs': ['exact-profile'], 'workspace_id': '123'})
    save_account(db, account)
    assert readiness.destination_readiness(account, db_path=db)['phase'] == 'configured_unverified'
    result = record(db, account)
    assert result['ready'] and result['provider'] == 'sociamonials'
    assert result['nativeHealth']['reconnectRequired']
    invalid = proof(account, 'sociamonials')
    invalid['mapping']['workspace_id'] = '456'
    with pytest.raises(ValueError):
        record(db, account, proof=invalid)


def test_teaching_selector_never_falls_back_to_default(db, account, monkeypatch):
    from myUtils import sociamonials_fallback as managed
    monkeypatch.setenv(managed.API_KEY_ENV, 'adult-mock-secret')
    monkeypatch.delenv(managed.TEACHING_API_KEY_ENV, raising=False)
    account['config'].update(publishingProvider='sociamonials', sociamonials={
        'network': 'tw', 'profile_refs': ['teaching-profile'], 'workspace_id': managed.DEFAULT_TEACHING_WORKSPACE_ID})
    save_account(db, account)
    assert not readiness.destination_readiness(account, db_path=db)['ready']
    assert not readiness._context(account, {})['configured']


def test_dependency_hold_is_exact_scoped_and_config_bound(db, account):
    record(db, account)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute('INSERT INTO publish_dependency_states VALUES (?,?,?,?,?,?,?,?)',
                     (readiness.destination_scope_key(account), readiness.config_fingerprint(account),
                      'provider', 'cooldown', 'secret=do-not-emit', stamp(60), stamp(-1), stamp(-1)))
    result = readiness.destination_readiness(account, db_path=db)
    assert result['phase'] == 'waiting_dependency' and result['nextAttemptAt'] == stamp(60)
    assert 'do-not-emit' not in json.dumps(result)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE publish_dependency_states SET config_fingerprint='old-config'")
    assert readiness.destination_readiness(account, db_path=db)['ready']


def test_invalid_stored_proof_and_disabled_never_ready(db, account):
    record(db, account)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE publish_destination_readiness SET evidence_json='{bad'")
    assert readiness.destination_readiness(account, db_path=db)['reasonCode'] == 'evidence_invalid'
    account['enabled'] = False
    save_account(db, account)
    assert readiness.destination_readiness(account, db_path=db)['reasonCode'] == 'disabled'


def stage(phase='ready'):
    return {'phase': phase, 'verifiedAt': stamp(-1), 'artifactFingerprint': 'a' * 64,
            'destinationFingerprint': 'd' * 64, 'provider': 'direct',
            'reasonCode': 'dependency_quota' if phase == 'waiting_dependency' else 'ready',
            'reason': 'Wait for /media/private/file.mp4 https://private.invalid token=mock-secret',
            'nextAttemptAt': stamp(60), 'partProgress': {'completeParts': 2, 'totalParts': 2}}


def add_target(db, number, payload=None, checkpoint=None, status='pending', workspace='tenant-a'):
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute('INSERT INTO publish_jobs VALUES (?,?,?,?,?,?)',
                     (number, 'twitter', 'pending', stamp(-3600), workspace,
                      payload if isinstance(payload, str) else json.dumps(payload or {})))
        conn.execute('INSERT INTO publish_job_targets (id,job_id,account_ref,file_ref,status,checkpoint_json,delivery_state) VALUES (?,?,?,?,?,?,?)',
                     (number, number, 'account:1', 'file:1', status,
                      checkpoint if isinstance(checkpoint, str) else json.dumps(checkpoint) if checkpoint is not None else None,
                      'awaiting_delivery' if status == 'awaiting_delivery' else None))


def view(db, monkeypatch, **kwargs):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(health, 'datetime', Clock)
    monkeypatch.setattr(health, '_worker_snapshot', lambda *_: {'ok': True})
    return health.publish_health_snapshot(db, **kwargs)


def test_health_legacy_counts_do_not_claim_verified_media_and_receipts_ignore_it(db, monkeypatch):
    add_target(db, 1)
    add_target(db, 2, {'publishReadiness': stage('waiting_media')}, status='awaiting_delivery')
    result = view(db, monkeypatch)
    assert result.queue['due'] == 1 and result.queue['verifiedMediaReady'] == 0
    assert result.queue['readinessUnverified'] == 1
    assert result.queue['deliveryPollsDue'] == 1
    assert result.totals['acceptedPending'] == 1 and result.totals['delivered'] == 0
    assert result.readiness['targets'][0]['phase'] == 'configured_unverified'
    problems = monitor._publishing_problems(result)
    assert 'overdue' in problems and 'awaiting-delivery' in problems
    assert 'publish-readiness' not in problems


def test_health_fresh_stage_checkpoint_precedence_safe_blocker_and_tenant(db, monkeypatch):
    add_target(db, 1, {'publishReadiness': stage()})
    add_target(db, 2, {'publishReadiness': stage()}, {'readiness': stage('waiting_dependency')})
    add_target(db, 3, {'publishReadiness': stage()}, workspace='tenant-b')
    result = view(db, monkeypatch, workspace_id='tenant-a')
    assert result.queue['verifiedMediaReady'] == result.queue['waitingDependency'] == 1
    assert result.queue['due'] == 1 and result.totals['total'] == 2
    blocked = next(t for t in result.readiness['targets'] if t['targetId'] == 2)
    assert blocked['phase'] == 'waiting_dependency' and blocked['nextAttemptAt']
    assert blocked['partProgress'] == {'completeParts': 2, 'totalParts': 2}
    notices = monitor._publishing_problems(result)['publish-readiness']
    assert 'dependency_quota' in notices[0] and 'next retry/reset' in notices[0]
    assert not any(v in json.dumps(result.to_dict()) + str(notices) for v in ('private/file', 'private.invalid', 'mock-secret'))


@pytest.mark.parametrize('payload,checkpoint', [
    ('{bad', None), ({'publishReadiness': {'phase': 'ready'}}, None),
    ({'publishReadiness': stage()}, '{bad'), ({'publishReadiness': stage()}, {'readiness': 'bad'}),
    ({'publishReadiness': {**stage(), 'verifiedAt': stamp(-1000)}}, None),
    ({'publishReadiness': {**stage(), 'verifiedAt': stamp(100)}}, None),
])
def test_health_json_and_freshness_fail_closed(db, monkeypatch, payload, checkpoint):
    add_target(db, 1, payload, checkpoint)
    result = view(db, monkeypatch)
    assert result.queue['verifiedMediaReady'] == 0
    assert result.readiness['targets'][0]['phase'] == 'configured_unverified'


def test_token_without_identity_and_name_without_token_never_ready(db, account):
    account['config'].pop('twitterUserId')
    save_account(db, account)
    assert readiness.destination_readiness(account, db_path=db)['phase'] == 'configured_unverified'
    account['config'].pop('accessToken')
    save_account(db, account)
    assert not readiness.destination_readiness(account, db_path=db)['ready']


def test_effective_selected_credential_rotation_during_verification_rejected(db, account, monkeypatch):
    account['config'].pop('accessToken')
    account['config']['accessTokenEnv'] = 'MOCK_EXACT_NATIVE_TOKEN'
    monkeypatch.setenv('MOCK_EXACT_NATIVE_TOKEN', 'old-mocked-token')
    save_account(db, account)
    verified = proof(account)
    monkeypatch.setenv('MOCK_EXACT_NATIVE_TOKEN', 'new-mocked-token')
    with pytest.raises(ValueError, match='effective credential'):
        record(db, account, proof=verified)


def test_health_backlog_projection_is_bounded_and_not_sampled(db, monkeypatch):
    for number in range(1, 121):
        add_target(db, number, {'publishReadiness': stage('waiting_capacity')})
    result = view(db, monkeypatch)
    assert result.queue['waitingCapacity'] == 120
    assert len(result.readiness['targets']) == 100
    assert sum(group['count'] for group in result.readiness['providers']) == 120
