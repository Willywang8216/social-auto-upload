"""Synthetic-only auth regressions: no provider/network/config/runtime DB access."""
from __future__ import annotations

import asyncio
import base64
import json
import multiprocessing
import sqlite3
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from myUtils import meta_auth, threads_auth, profiles, token_state, token_maintenance, oauth_state
from myUtils.worker import PublishWorker


@pytest.fixture
def auth_db(tmp_path, monkeypatch):
    from db.createTable import bootstrap
    import sau_backend as backend
    from myUtils.security import SecurityPolicy
    db = tmp_path / 'auth.db'
    bootstrap(db)
    monkeypatch.setenv('SAU_DB_PATH', str(db))
    monkeypatch.setenv('SAU_CONFIG_ENCRYPTION_KEY', base64.b64encode(b'A' * 32).decode())
    monkeypatch.setattr(backend, '_current_db_path', lambda: db)
    monkeypatch.setitem(backend.app.config, 'SAU_APP_CONFIG', None)
    monkeypatch.setitem(backend.app.config, 'SECURITY_POLICY', SecurityPolicy(tokens=frozenset(), cors_origins=()))
    monkeypatch.setitem(backend.app.config, 'TESTING', True)
    profile = profiles.create_profile('Auth synthetic', db_path=db)
    return SimpleNamespace(db=db, backend=backend, client=backend.app.test_client(), profile=profile)


def add(env, platform='threads', config=None, **kwargs):
    return profiles.add_account(env.profile.id, platform, kwargs.pop('name', platform), auth_type='oauth',
                                config=config or {}, db_path=env.db, **kwargs)


def stored(env, account):
    return profiles.get_account(account.id, db_path=env.db)


def future(days=60):
    return (token_state.utc_now() + timedelta(days=days)).isoformat(timespec='seconds')


def flags():
    return {key: True if key == '_needsReconnect' else 5 if 'Failures' in key else 'old' for key in token_state.FAILURE_KEYS}


def mock_meta(monkeypatch, pages):
    monkeypatch.setattr(meta_auth, 'build_authorize_url_from_env', lambda **kw: 'https://www.facebook.com/dialog/oauth')
    monkeypatch.setattr(meta_auth, 'exchange_code_for_token', lambda **kw: {'access_token': 'oauth-short', 'expires_in': 3600})
    monkeypatch.setattr(meta_auth, 'exchange_for_long_lived_token', lambda **kw: {'access_token': 'oauth-long', 'expires_in': 5184000})
    monkeypatch.setattr(meta_auth, 'debug_token_info', lambda **kw: {'is_valid': True, 'expires_at': 0})
    monkeypatch.setattr(meta_auth, 'fetch_managed_pages', lambda **kw: {'data': pages})


def mock_threads(monkeypatch, identity='thread-1'):
    monkeypatch.setattr(threads_auth, 'build_authorize_url_from_env', lambda **kw: 'https://threads.net/oauth/authorize')
    monkeypatch.setattr(threads_auth, 'exchange_code_for_token', lambda **kw: {'access_token': 'threads-short', 'user_id': identity})
    monkeypatch.setattr(threads_auth, 'exchange_for_long_lived_token', lambda **kw: {'access_token': 'threads-long', 'expires_in': 5184000})
    monkeypatch.setattr(threads_auth, 'refresh_long_lived_token', lambda **kw: {'access_token': 'threads-refreshed', 'expires_in': 5184000})
    monkeypatch.setattr(threads_auth, 'fetch_me', lambda **kw: {'id': identity, 'username': 'same-brand'})


def start(env, account):
    provider = 'meta' if account.platform in {'facebook', 'instagram'} else 'threads'
    response = env.client.post(f'/oauth/{provider}/start', json={'accountId': account.id, 'profileId': account.profile_id})
    assert response.status_code == 200, response.get_json()
    return response.get_json()['data']['state']


def page(page_id, ig_id=None):
    result = {'id': page_id, 'name': 'Synthetic ' + page_id, 'access_token': 'page-secret-' + page_id}
    if ig_id:
        result['instagram_business_account'] = {'id': ig_id, 'username': 'synthetic_' + ig_id}
    return result


@pytest.mark.parametrize('flow', ['oauth', 'manual', 'worker'])
def test_threads_success_clears_all_failure_state_without_enabling(auth_db, monkeypatch, flow):
    mock_threads(monkeypatch)
    account = add(auth_db, config={'accessToken': 'old', 'threadUserId': 'thread-1', **flags()}, enabled=False)
    if flow == 'oauth':
        state = start(auth_db, account)
        response = auth_db.client.get(f'/oauth/threads/callback?state={state}&code=synthetic')
        assert response.status_code == 200
        assert 'threads-long' not in response.get_data(as_text=True)
    elif flow == 'manual':
        response = auth_db.client.post(f'/accounts/{account.id}/refresh-token')
        assert response.status_code == 200, response.get_json()
    else:
        PublishWorker(lambda *a: None, db_path=auth_db.db)._refresh_account(account)
    updated = stored(auth_db, account)
    assert not updated.enabled
    assert all(key not in updated.config for key in token_state.FAILURE_KEYS)
    assert updated.config['_credentialRevision'] == 1
    assert updated.config['_nativeCredentialState'] == 'verified'
    assert updated.config['_lastTokenSelfCheckAt']


@pytest.mark.parametrize('platform,selected', [('facebook', 'p2'), ('instagram', 'i2')])
def test_picker_saves_server_candidates_once_and_never_exposes_tokens(auth_db, monkeypatch, platform, selected):
    pages = [page('p1', 'i1'), page('p2', 'i2')]
    mock_meta(monkeypatch, pages)
    account = add(auth_db, platform)
    state = start(auth_db, account)
    response = auth_db.client.get(f'/oauth/meta/callback?state={state}&code=synthetic')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'sau:meta-oauth-selection' in html
    assert 'ok:true' not in html
    for secret in ['oauth-short', 'oauth-long', 'page-secret-p1', 'page-secret-p2', 'TOKEN_DATA']:
        assert secret not in html
    assert not stored(auth_db, account).config.get('accessToken')
    with sqlite3.connect(auth_db.db) as conn:
        raw = conn.execute('SELECT result_json FROM meta_oauth_requests WHERE state_token = ?', (state,)).fetchone()[0]
    assert 'oauth-long' not in raw and 'page-secret-p2' not in raw
    pending = auth_db.backend.meta_review.get_oauth_request(state, db_path=auth_db.db)
    public = auth_db.backend._oauth_request_to_status(pending)
    assert 'candidates' not in public and 'binding' not in public and 'userAccessToken' not in public
    response = auth_db.client.post('/oauth/meta/select', json={'state': state, 'selectedId': selected})
    assert response.status_code == 200, response.get_json()
    result = response.get_json()['data']
    assert result['accountId'] == account.id and result['status'] == 'ok'
    updated = stored(auth_db, account)
    assert updated.config['pageId'] == 'p2'
    assert updated.config['accessToken'] == 'page-secret-p2'
    if platform == 'instagram':
        assert updated.config['igUserId'] == 'i2'
    assert 'page-secret' not in json.dumps(result)
    revision = updated.config['_credentialRevision']
    assert auth_db.client.post('/oauth/meta/select', json={'state': state, 'selectedId': selected}).status_code == 409
    assert auth_db.client.get(f'/oauth/meta/callback?state={state}&code=synthetic').status_code == 400
    assert stored(auth_db, account).config['_credentialRevision'] == revision
    with sqlite3.connect(auth_db.db) as conn:
        final = conn.execute('SELECT result_json FROM meta_oauth_requests WHERE state_token = ?', (state,)).fetchone()[0]
    assert 'candidates' not in final and 'sau:enc:' not in final


def test_picker_rejects_browser_tokens_unverified_id_wrong_scope_and_auth(auth_db, monkeypatch):
    from myUtils.security import SecurityPolicy
    mock_meta(monkeypatch, [page('p1'), page('p2')])
    account = add(auth_db, 'facebook')
    state = start(auth_db, account)
    auth_db.client.get(f'/oauth/meta/callback?state={state}&code=synthetic')
    body = {'state': state, 'selectedId': 'p1'}
    assert auth_db.client.post('/oauth/meta/select', json={**body, 'accessToken': 'browser-forged'}).status_code == 400
    assert auth_db.client.post('/oauth/meta/select', json={**body, 'selectedId': 'not-verified'}).status_code == 400
    monkeypatch.setattr(auth_db.backend, '_workspace_scope', lambda: 'other-workspace')
    assert auth_db.client.post('/oauth/meta/select', json=body).status_code == 404
    monkeypatch.setattr(auth_db.backend, '_workspace_scope', lambda: None)
    monkeypatch.setitem(auth_db.backend.app.config, 'SECURITY_POLICY', SecurityPolicy(tokens=frozenset({'synthetic-bearer'}), cors_origins=()))
    assert auth_db.client.post('/oauth/meta/select', json=body).status_code == 401
    assert not stored(auth_db, account).config.get('accessToken')


@pytest.mark.parametrize('stage', ['callback', 'selection'])
def test_oauth_state_expiry_and_profile_move_prevent_write(auth_db, monkeypatch, stage):
    mock_meta(monkeypatch, [page('p1'), page('p2')])
    account = add(auth_db, 'facebook')
    state = start(auth_db, account)
    if stage == 'selection':
        auth_db.client.get(f'/oauth/meta/callback?state={state}&code=synthetic')
    with sqlite3.connect(auth_db.db) as conn:
        conn.execute("UPDATE meta_oauth_requests SET requested_at = '2000-01-01' WHERE state_token = ?", (state,))
    response = auth_db.client.get(f'/oauth/meta/callback?state={state}&code=synthetic') if stage == 'callback' else (
        auth_db.client.post('/oauth/meta/select', json={'state': state, 'selectedId': 'p1'}))
    assert response.status_code in {400, 409}
    assert not stored(auth_db, account).config.get('accessToken')
    state2 = start(auth_db, account)
    other = profiles.create_profile('Other profile', db_path=auth_db.db)
    profiles.update_account(account.id, profile_id=other.id, db_path=auth_db.db)
    assert auth_db.client.get(f'/oauth/meta/callback?state={state2}&code=synthetic').status_code == 400
    assert not stored(auth_db, account).config.get('accessToken')


def test_start_requires_exact_profile_and_workspace(auth_db, monkeypatch):
    mock_threads(monkeypatch)
    account = add(auth_db, config={'threadUserId': 'thread-1'})
    assert auth_db.client.post('/oauth/threads/start', json={'accountId': account.id, 'profileId': 999}).status_code == 400
    monkeypatch.setattr(auth_db.backend, '_workspace_scope', lambda: 'other-workspace')
    assert auth_db.client.post('/oauth/threads/start', json={'accountId': account.id}).status_code == 404


@pytest.mark.parametrize('platform', ['facebook', 'instagram'])
@pytest.mark.parametrize('flow', ['oauth', 'manual', 'worker'])
def test_missing_configured_identity_never_retargets(auth_db, monkeypatch, platform, flow):
    mock_meta(monkeypatch, [page('different-page', 'different-ig')])
    config = {'pageId': 'wanted-page', 'accessToken': 'usable-old-page', 'metaUserAccessToken': 'usable-old-user',
              'metaUserAccessTokenExpiresAt': future(), 'accessTokenExpiresAt': future()}
    if platform == 'instagram':
        config['igUserId'] = 'wanted-ig'
    account = add(auth_db, platform, config)
    if flow == 'oauth':
        state = start(auth_db, account)
        assert auth_db.client.get(f'/oauth/meta/callback?state={state}&code=synthetic').status_code == 400
    elif flow == 'manual':
        assert auth_db.client.post(f'/accounts/{account.id}/refresh-token').status_code == 400
    else:
        PublishWorker(lambda *a: None, db_path=auth_db.db)._refresh_account(account)
    updated = stored(auth_db, account)
    for key, value in config.items():
        assert updated.config[key] == value
    assert '_credentialRevision' not in updated.config


def test_threads_identity_mismatch_does_not_replace_good_credentials(auth_db, monkeypatch):
    mock_threads(monkeypatch, identity='wrong-brand')
    account = add(auth_db, config={'threadUserId': 'configured-brand', 'accessToken': 'keep-old'})
    state = start(auth_db, account)
    assert auth_db.client.get(f'/oauth/threads/callback?state={state}&code=synthetic').status_code == 400
    assert stored(auth_db, account).config == account.config


def test_meta_long_lived_failure_is_not_short_token_success(auth_db, monkeypatch):
    mock_meta(monkeypatch, [page('p1')])
    account = add(auth_db, 'facebook', {'pageId': 'p1', 'accessToken': 'keep-page', 'metaUserAccessToken': 'keep-user'})
    def failed(**kw):
        raise TimeoutError('url?access_token=do-not-log')
    monkeypatch.setattr(meta_auth, 'exchange_for_long_lived_token', failed)
    state = start(auth_db, account)
    response = auth_db.client.get(f'/oauth/meta/callback?state={state}&code=synthetic')
    assert response.status_code == 400
    assert 'do-not-log' not in response.get_data(as_text=True)
    assert stored(auth_db, account).config == account.config


def test_page_and_user_expiry_are_independent(auth_db, monkeypatch):
    mock_meta(monkeypatch, [page('p1')])
    data_deadline = int((token_state.utc_now() + timedelta(days=80)).timestamp())
    user_deadline = int((token_state.utc_now() + timedelta(days=60)).timestamp())
    monkeypatch.setattr(meta_auth, 'debug_token_info', lambda *, access_token, **kw: {
        'is_valid': True, 'expires_at': user_deadline if access_token == 'oauth-long' else 0,
        'data_access_expires_at': data_deadline})
    account = add(auth_db, 'facebook')
    state = start(auth_db, account)
    assert auth_db.client.get(f'/oauth/meta/callback?state={state}&code=synthetic').status_code == 200
    cfg = stored(auth_db, account).config
    assert cfg['metaTokenExpiryMode'] == 'expiring' and cfg['accessTokenExpiryMode'] == 'never'
    assert cfg['metaTokenExpiresAtEpoch'] == user_deadline and cfg['accessTokenExpiresAtEpoch'] == 0
    assert cfg['accessTokenExpiresAt'] != cfg['metaUserAccessTokenExpiresAt']
    assert cfg['metaUserDataAccessExpiresAtEpoch'] == data_deadline


def test_page_sync_failure_preserves_all_old_tokens_and_is_not_success(auth_db, monkeypatch):
    mock_meta(monkeypatch, [])
    account = add(auth_db, 'instagram', {'pageId': 'p1', 'igUserId': 'i1', 'accessToken': 'keep-page',
                                        'metaUserAccessToken': 'keep-user', 'metaUserAccessTokenExpiresAt': future(1)})
    monkeypatch.setattr(meta_auth, 'debug_token_info', lambda **kw: {'is_valid': True, 'expires_at': int((token_state.utc_now() + timedelta(days=1)).timestamp())})
    monkeypatch.setattr(meta_auth, 'refresh_instagram_user_token', lambda **kw: {'access_token': 'discard-new-user', 'expires_in': 5184000})
    monkeypatch.setattr(meta_auth, 'fetch_managed_pages', Mock(side_effect=TimeoutError()))
    response = auth_db.client.post(f'/accounts/{account.id}/refresh-token')
    assert response.status_code == 400
    cfg = stored(auth_db, account).config
    assert cfg['accessToken'] == 'keep-page' and cfg['metaUserAccessToken'] == 'keep-user'
    assert not cfg.get('_needsReconnect') and not cfg.get('lastManualRefreshAt')


def _reconnect_in_other_process(db_path, account_id, token):
    # Only the path of this test's synthetic database is passed to the child.
    db = Path(db_path)
    account = profiles.get_account(account_id, db_path=db)
    profiles.update_account(account_id, config_patch={'publishingProvider': 'sociamonials', 'customRoute': 'preserve'}, db_path=db)
    token_state.require_saved(token_state.commit_success(account, {'accessToken': token}, db_path=db))


@pytest.mark.parametrize('same_token', [False, True])
def test_cross_process_reconnect_discards_stale_success_and_failure(auth_db, same_token):
    account = add(auth_db, config={'threadUserId': 'thread-1', 'accessToken': 'old', 'tenantConfig': {'keep': True}})
    new_token = 'old' if same_token else 'reconnected'
    process = multiprocessing.get_context('spawn').Process(target=_reconnect_in_other_process,
        args=(str(auth_db.db), account.id, new_token))
    process.start()
    process.join(20)
    if process.is_alive():
        process.terminate()
        process.join()
    assert process.exitcode == 0
    assert token_state.commit_success(account, {'accessToken': 'stale-success'}, db_path=auth_db.db) is None
    assert token_state.commit_failure(account, token_state.provider_error({'code': 190}), db_path=auth_db.db) is None
    updated = stored(auth_db, account)
    assert updated.config['accessToken'] == new_token
    assert updated.config['publishingProvider'] == 'sociamonials'
    assert updated.config['tenantConfig'] == {'keep': True}
    assert not updated.config.get('_needsReconnect')
    assert updated.config['_credentialRevision'] == 1
    with sqlite3.connect(auth_db.db) as conn:
        raw = conn.execute('SELECT config_json FROM accounts WHERE id = ?', (account.id,)).fetchone()[0]
    assert 'sau:enc:v1:' in raw and 'stale-success' not in raw


@pytest.mark.parametrize('failure', [False, True])
def test_manual_and_worker_inflight_results_cannot_overwrite_reconnect(auth_db, monkeypatch, failure):
    mock_threads(monkeypatch)
    account = add(auth_db, config={'accessToken': 'old', 'threadUserId': 'thread-1'})
    def inflight(**kw):
        current = stored(auth_db, account)
        token_state.commit_success(current, {'accessToken': 'reconnected-new'}, db_path=auth_db.db)
        if failure:
            raise token_state.provider_error({'code': 190, 'error_subcode': 460})
        return {'access_token': 'old-refresh-result', 'expires_in': 5184000}
    monkeypatch.setattr(threads_auth, 'refresh_long_lived_token', inflight)
    assert auth_db.client.post(f'/accounts/{account.id}/refresh-token').status_code == 400
    cfg = stored(auth_db, account).config
    assert cfg['accessToken'] == 'reconnected-new' and not cfg.get('_needsReconnect')
    # Repeat through the independent worker caller.
    PublishWorker(lambda *a: None, db_path=auth_db.db)._refresh_account(stored(auth_db, account))
    cfg = stored(auth_db, account).config
    assert cfg['accessToken'] == 'reconnected-new' and not cfg.get('_needsReconnect')


def test_route_and_disabled_changes_merge_with_slow_refresh(auth_db, monkeypatch):
    mock_threads(monkeypatch)
    account = add(auth_db, config={'accessToken': 'old', 'threadUserId': 'thread-1'})
    def inflight(**kw):
        profiles.update_account(account.id, enabled=False, nickname='New label',
            config_patch={'publishingProvider': 'sociamonials', 'sociamonials': {'network': 'th', 'profile_refs': ['one']}}, db_path=auth_db.db)
        return {'access_token': 'fresh', 'expires_in': 5184000}
    monkeypatch.setattr(threads_auth, 'refresh_long_lived_token', inflight)
    PublishWorker(lambda *a: None, db_path=auth_db.db)._refresh_account(account)
    updated = stored(auth_db, account)
    assert updated.config['accessToken'] == 'fresh'
    assert updated.config['publishingProvider'] == 'sociamonials'
    assert not updated.enabled and updated.nickname == 'New label'


def test_metadata_only_update_cannot_write_a_previously_read_config(auth_db, monkeypatch):
    account = add(auth_db, config={'accessToken': 'old', 'threadUserId': 'thread-1'})
    real_get = profiles.get_account
    injected = False
    def interleaved(*args, **kwargs):
        nonlocal injected
        result = real_get(*args, **kwargs)
        if not injected:
            injected = True
            token_state.commit_success(result, {'accessToken': 'concurrent-token'}, db_path=auth_db.db)
        return result
    monkeypatch.setattr(profiles, 'get_account', interleaved)
    profiles.update_account(account.id, nickname='Keep name', db_path=auth_db.db)
    updated = real_get(account.id, db_path=auth_db.db)
    assert updated.config['accessToken'] == 'concurrent-token'
    assert updated.nickname == 'Keep name'


def test_worker_periodically_reads_never_tokens_without_exchange(auth_db, monkeypatch):
    mock_meta(monkeypatch, [page('p1')])
    exchange = Mock(side_effect=AssertionError('a periodic read must not rotate a valid never token'))
    monkeypatch.setattr(meta_auth, 'exchange_for_long_lived_token', exchange)
    account = add(auth_db, 'facebook', {'pageId': 'p1', 'accessToken': 'page', 'metaUserAccessToken': 'user',
        'metaTokenExpiryMode': 'never', 'metaUserAccessTokenExpiresAt': future(3650),
        '_lastTokenSelfCheckAt': (token_state.utc_now() - timedelta(hours=13)).isoformat()})
    worker = PublishWorker(lambda *a: None, db_path=auth_db.db)
    assert worker._is_account_stale(account)
    asyncio.run(worker._run_maintenance_tick())
    exchange.assert_not_called()
    updated = stored(auth_db, account)
    assert updated.config['_nativeCredentialState'] == 'verified'
    assert not worker._is_account_stale(updated)
    cfg = dict(updated.config, metaUserDataAccessExpiresAt=future(1),
               _lastTokenSelfCheckAt=(token_state.utc_now() - timedelta(hours=2)).isoformat())
    assert token_maintenance.meta_maintenance_due(cfg)


def test_threads_minimum_refresh_age_is_enforced(auth_db, monkeypatch):
    mock_threads(monkeypatch)
    account = add(auth_db, config={'accessToken': 'newly-minted', 'threadUserId': 'thread-1',
        'accessTokenExpiresAt': future(1), 'accessTokenUpdatedAt': token_state.iso_now()})
    assert not PublishWorker(lambda *a: None, db_path=auth_db.db)._is_account_stale(account)
    refresh = Mock(side_effect=AssertionError('too young'))
    monkeypatch.setattr(threads_auth, 'refresh_long_lived_token', refresh)
    response = auth_db.client.post(f'/accounts/{account.id}/refresh-token')
    assert response.status_code == 400
    refresh.assert_not_called()
    assert not stored(auth_db, account).config.get('_needsReconnect')


def test_native_future_revocation_and_managed_configuration_are_separate(auth_db):
    account = add(auth_db, 'facebook', {'pageId': 'p1', 'accessToken': 'invalid-future-token',
        'accessTokenExpiresAt': future(), '_needsReconnect': True,
        '_nativeCredentialError': {'reason': 'Provider rejected credential', 'code': 190, 'subcode': 460},
        'publishingProvider': 'sociamonials', 'sociamonials': {'network': 'fb', 'profile_refs': ['configured-destination']}})
    response = auth_db.client.get('/api/accounts')
    item = next(a for a in response.get_json()['data'] if a['id'] == account.id)
    assert item['nativeCredentials']['state'] == 'reconnect_required'
    assert item['nativeCredentials']['token']['expiresAt'] == account.config['accessTokenExpiresAt']
    assert item['nativeCredentials']['providerSubcode'] == 460
    assert item['publishingRoute']['state'] == 'configured'
    assert item['publishingRoute']['validation'] == 'not_checked'
    assert item['publishingRoute']['availability'] == 'unknown'
    assert item['cookieStatus'] == 'invalid'
    assert 'invalid-future-token' not in response.get_data(as_text=True)


def test_browser_cannot_clear_failure_or_assert_native_validation(auth_db):
    account = add(auth_db, config={'threadUserId': 'thread-1', 'accessToken': 'old', '_needsReconnect': True})
    response = auth_db.client.patch(f'/accounts/{account.id}', json={'config': {
        '_needsReconnect': False, '_credentialRevision': 999, '_nativeCredentialState': 'verified',
        'accessTokenExpiryMode': 'never', 'publishingProvider': 'direct'}})
    assert response.status_code == 200, response.get_json()
    cfg = stored(auth_db, account).config
    assert cfg['_needsReconnect'] is True
    assert cfg.get('_nativeCredentialState') != 'verified'
    assert cfg.get('_credentialRevision') != 999
    assert cfg.get('accessTokenExpiryMode') != 'never'


@pytest.mark.parametrize('status,code,terminal', [(400, 190, True), (400, 10, True), (429, 190, False), (503, 190, False), (400, 999, False)])
def test_threads_http_errors_keep_structured_codes_without_secrets(status, code, terminal):
    response = SimpleNamespace(status_code=status, json=lambda: {'error': {
        'code': code, 'error_subcode': 460, 'message': 'access_token=secret'}}, raise_for_status=lambda: None)
    session = SimpleNamespace(get=lambda *a, **kw: response)
    with pytest.raises(threads_auth.ThreadsOAuthError) as caught:
        threads_auth.refresh_long_lived_token(access_token='synthetic', session=session)
    assert caught.value.terminal is terminal
    assert caught.value.error_code == code and caught.value.error_subcode == 460
    assert 'secret' not in str(caught.value)


def test_worker_five_timeouts_are_bounded_backoff_not_permanent_reauth(auth_db, monkeypatch):
    account = add(auth_db, config={'accessToken': 'still-usable', 'threadUserId': 'thread-1', 'accessTokenExpiresAt': future(3)})
    worker = PublishWorker(lambda *a: None, db_path=auth_db.db)
    alert = Mock()
    monkeypatch.setattr(worker, '_alert_needs_reconnect', alert)
    for _ in range(5):
        snapshot = stored(auth_db, account)
        worker._handle_refresh_failure(snapshot, snapshot.config, TimeoutError('access_token=redact-me'))
    cfg = stored(auth_db, account).config
    assert cfg['_maintenanceFailures'] == 5
    assert not cfg.get('_needsReconnect')
    assert cfg['accessTokenExpiresAt'] == account.config['accessTokenExpiresAt']
    delay = (token_state.parse_time(cfg['_nextMaintenanceAttemptAt']) - token_state.utc_now()).total_seconds()
    assert 0 < delay <= 21600
    assert 'redact-me' not in cfg['_lastMaintenanceError']
    alert.assert_not_called()


@pytest.mark.parametrize('platform', ['facebook', 'instagram'])
def test_publish_time_page_sync_refuses_another_page(auth_db, monkeypatch, platform):
    from myUtils import prepared_publishers
    cfg = {'accessToken': 'old-page', 'metaUserAccessToken': 'user', 'pageId': 'configured-page', 'igUserId': 'same-ig'}
    mock_meta(monkeypatch, [page('different-page', 'same-ig')])
    with pytest.raises(token_state.CredentialError):
        prepared_publishers._rederive_meta_page_token(cfg, platform)
    assert cfg['accessToken'] == 'old-page' and cfg['pageId'] == 'configured-page'


def test_pending_selection_is_bound_to_credential_revision_and_principal(auth_db, monkeypatch):
    mock_meta(monkeypatch, [page('p1'), page('p2')])
    account = add(auth_db, 'facebook')
    monkeypatch.setattr(auth_db.backend, '_oauth_principal_id', lambda: 'owner-a')
    state = start(auth_db, account)
    auth_db.client.get(f'/oauth/meta/callback?state={state}&code=synthetic')
    monkeypatch.setattr(auth_db.backend, '_oauth_principal_id', lambda: 'owner-b')
    assert auth_db.client.post('/oauth/meta/select', json={'state': state, 'selectedId': 'p1'}).status_code == 404
    monkeypatch.setattr(auth_db.backend, '_oauth_principal_id', lambda: 'owner-a')
    token_state.commit_success(account, {'pageId': 'p2', 'accessToken': 'reconnected-page'}, db_path=auth_db.db)
    assert auth_db.client.post('/oauth/meta/select', json={'state': state, 'selectedId': 'p1'}).status_code == 409
    assert stored(auth_db, account).config['accessToken'] == 'reconnected-page'


def test_ambiguous_instagram_candidates_are_not_first_match(monkeypatch):
    with pytest.raises(token_state.CredentialError):
        token_maintenance.meta_candidates([page('p1', 'same-ig'), page('p2', 'same-ig')],
                                           'instagram', {'igUserId': 'same-ig'}, require_identity=True)


def test_failed_introspection_never_becomes_success_in_worker(auth_db, monkeypatch):
    account = add(auth_db, 'facebook', {'pageId': 'p1', 'accessToken': 'page', 'metaUserAccessToken': 'user',
                                      'accessTokenExpiresAt': future(), 'metaUserAccessTokenExpiresAt': future()})
    mock_meta(monkeypatch, [page('p1')])
    monkeypatch.setattr(meta_auth, 'debug_token_info', lambda **kw: {'is_valid': False, 'expires_at': 0,
        'error': {'code': 190, 'error_subcode': 460}})
    PublishWorker(lambda *a: None, db_path=auth_db.db)._refresh_account(account)
    cfg = stored(auth_db, account).config
    assert cfg['_needsReconnect']
    assert cfg['_nativeCredentialError']['subcode'] == 460
    assert cfg['metaUserAccessTokenExpiresAt'] == account.config['metaUserAccessTokenExpiresAt']
    assert not cfg.get('metaTokenExpiryMode')


def test_ig_rate_limit_does_not_fall_back_to_another_exchange(monkeypatch):
    response = SimpleNamespace(status_code=429, json=lambda: {'error': {'code': 4, 'is_transient': True}},
                               raise_for_status=lambda: None)
    fallback = Mock()
    monkeypatch.setattr(meta_auth, 'exchange_for_long_lived_token', fallback)
    with pytest.raises(meta_auth.MetaOAuthError) as caught:
        meta_auth.refresh_instagram_user_token(access_token='synthetic', session=SimpleNamespace(get=lambda *a, **kw: response))
    assert not caught.value.terminal
    fallback.assert_not_called()


def test_connection_check_discards_old_oauth_credential_generation(auth_db, monkeypatch):
    from myUtils import prepared_publishers
    account = add(auth_db, 'twitter', {'accessToken': 'old', 'twitterAuthType': 'api', 'twitterUserId': 'same-user'})
    def checked_old_token(config):
        token_state.commit_success(account, {'accessToken': 'reconnected'}, db_path=auth_db.db)
        return {'data': {'id': 'same-user', 'username': 'old-check-name'}}
    monkeypatch.setattr(prepared_publishers, 'validate_twitter_config_live', checked_old_token)
    response = auth_db.client.post(f'/accounts/{account.id}/check-connection')
    assert response.status_code == 400
    cfg = stored(auth_db, account).config
    assert cfg['accessToken'] == 'reconnected'
    assert not cfg.get('twitterUserName')
    assert not cfg.get('_maintenanceFailures')


def test_oauth_status_scopes_explicit_account_and_auto_discovery(auth_db, monkeypatch):
    mock_meta(monkeypatch, [page('private-page')])
    account = add(auth_db, 'facebook', {'pageId': 'private-page', 'accessToken': 'private-token'}, name='private-account')
    state = start(auth_db, account)
    monkeypatch.setattr(auth_db.backend, '_workspace_scope', lambda: 'another-workspace')
    response = auth_db.client.get(f'/admin/oauth/status?platform=facebook&accountId={account.id}')
    assert response.status_code == 404
    assert state not in response.get_data(as_text=True)
    response = auth_db.client.get('/admin/oauth/status?platform=facebook')
    assert response.status_code == 200
    data = response.get_json()['data']
    assert data['accountId'] is None and data['account'] is None and data['lastRequest'] is None
    assert 'private-account' not in response.get_data(as_text=True)
    assert state not in response.get_data(as_text=True)


def test_oauth_status_rejects_platform_mismatch_and_invalid_account_id(auth_db):
    account = add(auth_db, 'facebook')
    assert auth_db.client.get(f'/admin/oauth/status?platform=threads&accountId={account.id}').status_code == 404
    assert auth_db.client.get('/admin/oauth/status?platform=facebook&accountId=bad-id').status_code == 400


def test_oauth_status_hides_other_principal_live_state_and_event_capabilities(auth_db, monkeypatch):
    mock_meta(monkeypatch, [page('p1')])
    account = add(auth_db, 'facebook')
    monkeypatch.setattr(auth_db.backend, '_oauth_principal_id', lambda: 'owner-a')
    state = start(auth_db, account)
    auth_db.backend.account_events.record_event(
        account_id=account.id, profile_id=account.profile_id, platform='facebook', account_name=account.account_name,
        action='oauth_start', status='ok', summary='Synthetic OAuth start',
        metadata={'state': state, 'authorizeUrl': f'https://example.invalid/authorize?state={state}'}, db_path=auth_db.db)
    monkeypatch.setattr(auth_db.backend, '_oauth_principal_id', lambda: 'owner-b')
    response = auth_db.client.get(f'/admin/oauth/status?platform=facebook&accountId={account.id}')
    assert response.status_code == 200
    assert response.get_json()['data']['lastRequest'] is None
    assert state not in response.get_data(as_text=True)
    monkeypatch.setattr(auth_db.backend, '_oauth_principal_id', lambda: 'owner-a')
    own = auth_db.client.get(f'/admin/oauth/status?platform=facebook&accountId={account.id}').get_json()['data']
    assert own['lastRequest']['state'] == state
    assert state not in json.dumps(own['recentEvents'])


@pytest.mark.parametrize('platform', ['facebook', 'instagram', 'threads'])
def test_oauth_status_respects_reconnect_flag_despite_future_expiry(auth_db, platform):
    cfg = {'accessToken': 'revoked', 'pageId': 'p1', 'igUserId': 'i1', 'threadUserId': 't1',
           'accessTokenExpiresAt': future(), '_needsReconnect': True,
           '_nativeCredentialError': {'reason': 'Provider rejected credential', 'code': 190, 'subcode': 460}}
    account = add(auth_db, platform, cfg)
    response = auth_db.client.get(f'/admin/oauth/status?platform={platform}&accountId={account.id}')
    assert response.status_code == 200
    data = response.get_json()['data']
    assert data['reconnectRequired'] is True and data['recommendedAction'] == 'reconnect'
    assert data['nativeCredentials']['state'] == 'reconnect_required'
    assert data['nativeCredentials']['providerSubcode'] == 460
    assert stored(auth_db, account).config['accessTokenExpiresAt'] == cfg['accessTokenExpiresAt']


def test_oauth_status_uses_data_access_deadline_for_nonexpiring_token(auth_db):
    expired = (token_state.utc_now() - timedelta(days=1)).isoformat()
    account = add(auth_db, 'facebook', {'pageId': 'p1', 'accessToken': 'page',
        'accessTokenExpiresAt': future(3650), 'accessTokenExpiryMode': 'never', 'dataAccessExpiresAt': expired})
    data = auth_db.client.get(f'/admin/oauth/status?platform=facebook&accountId={account.id}').get_json()['data']
    assert data['reconnectRequired'] is True and data['recommendedAction'] == 'reconnect'
    assert data['expiresAt'] == expired and data['nativeCredentials']['token']['mode'] == 'never'


def test_read_check_does_not_discard_inflight_single_use_refresh(auth_db, monkeypatch):
    from myUtils import prepared_publishers
    account = profiles.add_account(auth_db.profile.id, 'twitter', 'synthetic-x', auth_type='api',
        config={'accessToken': 'old-access', 'refreshToken': 'consumed-in-flight', 'twitterAuthType': 'api',
                'twitterUserId': 'same-user', 'accessTokenExpiresAt': future(), '_needsReconnect': True}, db_path=auth_db.db)
    snapshot = token_state.fingerprint(account)
    monkeypatch.setattr(prepared_publishers, 'validate_twitter_config_live', lambda cfg: {
        'data': {'id': 'same-user', 'username': 'same-brand', 'name': 'Synthetic X'}})
    response = auth_db.client.post(f'/accounts/{account.id}/check-connection')
    assert response.status_code == 200, response.get_json()
    checked = stored(auth_db, account)
    assert token_state.fingerprint(checked) == snapshot
    assert checked.auth_type == 'api' and not checked.config.get('_needsReconnect')
    assert checked.config['_nativeCredentialState'] == 'verified'
    refreshed = token_state.commit_success(account, {'accessToken': 'fresh-access', 'refreshToken': 'next-single-use'}, db_path=auth_db.db)
    assert refreshed is not None
    assert refreshed.config['refreshToken'] == 'next-single-use'
    assert refreshed.config['twitterUserName'] == 'same-brand'


def test_validation_that_synchronizes_changed_credentials_still_advances(auth_db):
    account = add(auth_db, config={'accessToken': 'old', 'threadUserId': 'same-thread'})
    changed = token_state.commit_validation(account, {'accessToken': 'changed'}, db_path=auth_db.db)
    assert changed.config['_credentialRevision'] == 1
    assert token_state.commit_success(account, {'accessToken': 'stale'}, db_path=auth_db.db) is None


@pytest.mark.parametrize('http_error', [False, True])
@pytest.mark.parametrize('concurrent_reconnect', [False, True])
def test_x_publish_refresh_rejection_is_failure_not_success(auth_db, monkeypatch, http_error, concurrent_reconnect):
    from myUtils import prepared_publishers
    account = add(auth_db, 'twitter', {'accessToken': 'old-access', 'refreshToken': 'old-refresh',
        'twitterAuthType': 'api', 'twitterUserId': 'same-user', 'accessTokenExpiresAt': '2000-01-01T00:00:00+00:00'})
    rejection = RuntimeError('access_token=synthetic-secret')
    if http_error:
        rejection.response = SimpleNamespace(status_code=400, json=lambda: {'error': 'invalid_grant'})
    else:
        rejection.error_code = 'invalid_grant'
    def reject(*args, **kwargs):
        if concurrent_reconnect:
            token_state.require_saved(token_state.commit_success(account, {
                'accessToken': 'reconnected-access', 'refreshToken': 'reconnected-refresh'}, db_path=auth_db.db))
        raise rejection
    monkeypatch.setattr(prepared_publishers, 'refresh_twitter_token_single_flight', reject)
    session = Mock()
    with pytest.raises(prepared_publishers.PreparedPublishError):
        prepared_publishers.publish_twitter_sync(account, {'_db_path': str(auth_db.db), 'message': 'synthetic'}, session=session)
    cfg = stored(auth_db, account).config
    if concurrent_reconnect:
        assert cfg['accessToken'] == 'reconnected-access' and cfg['refreshToken'] == 'reconnected-refresh'
        assert not cfg.get('_needsReconnect') and cfg['_nativeCredentialState'] == 'verified'
    else:
        assert cfg['_needsReconnect'] is True and cfg['_nativeCredentialState'] == 'reconnect_required'
        assert cfg['_nativeCredentialError']['code'] == 'invalid_grant'
        assert cfg['accessToken'] == 'old-access' and cfg['refreshToken'] == 'old-refresh'
        assert cfg['accessTokenExpiresAt'] == account.config['accessTokenExpiresAt']
        assert not cfg.get('_credentialRevision')
    assert 'synthetic-secret' not in json.dumps(cfg)
    session.get.assert_not_called()
    session.post.assert_not_called()


def test_debug_token_subcode_alias_is_retained():
    verdict = token_state.provider_error({'code': 190, 'subcode': 460}, invalid=True)
    assert verdict.terminal and verdict.error_code == 190 and verdict.error_subcode == 460
