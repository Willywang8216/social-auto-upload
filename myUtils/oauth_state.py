"""Single-use Meta/Threads OAuth transitions on the existing SQLite tables.

Only server-verified candidates live in result_json (encrypted with the same
serializer as account config). The browser submits state + candidate ID, never
credentials. Final state consumption and credential CAS share one transaction.
"""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

from myUtils import config_crypto, profiles, token_state

OAUTH_TTL_SECONDS = 15 * 60
_TABLES = {'meta': 'meta_oauth_requests', 'threads': 'threads_oauth_requests'}
PENDING_STATUSES = ('pending_page_selection', 'pending_ig_selection')


class OAuthStateError(token_state.CredentialError):
    pass


def _table(provider):
    return _TABLES[provider]


def _encode(payload):
    return json.dumps(config_crypto.encrypt_config_secrets(payload), ensure_ascii=False)


def _read_locked(conn, provider, state, statuses):
    row = conn.execute(f'SELECT * FROM {_table(provider)} WHERE state_token = ?', (state,)).fetchone()
    if row is None:
        raise OAuthStateError('Unknown OAuth state; start a new authorization')
    requested = token_state.parse_time(row['requested_at'])
    age = token_state.utc_now() - requested if requested else None
    if age is None or age < timedelta(seconds=-60) or age >= timedelta(seconds=OAUTH_TTL_SECONDS):
        raise OAuthStateError('OAuth state expired; start a new authorization')
    if row['status'] not in statuses:
        raise OAuthStateError('OAuth state already used or not ready for this operation')
    data = dict(row)
    data['result'] = config_crypto.decrypt_config_secrets(json.loads(data.pop('result_json') or '{}'))
    data['scopes'] = json.loads(data.pop('scopes_json') or '[]')
    return SimpleNamespace(**data)


def _bound_account(conn, state, *, authorize=False, workspace_id=None, principal_id=None):
    binding = (state.result or {}).get('binding') or {}
    row = conn.execute('SELECT * FROM accounts WHERE id = ?', (state.account_id,)).fetchone()
    if row is None:
        raise OAuthStateError('OAuth target account no longer exists')
    account = profiles._row_to_account(row)
    if (not binding.get('fingerprint') or state.profile_id != account.profile_id
            or binding.get('accountId') != account.id or binding.get('profileId') != account.profile_id
            or binding.get('workspaceId') != account.workspace_id
            or binding.get('platform') != account.platform
            or token_state.fingerprint(account) != binding['fingerprint']):
        raise token_state.StaleCredentialUpdate()
    if authorize:
        if workspace_id is not None and account.workspace_id != workspace_id:
            raise LookupError('OAuth target account not found')
        if binding.get('principalId') is not None and binding['principalId'] != principal_id:
            raise LookupError('OAuth target account not found')
    return account


def create(provider, account, *, state_token, redirect_uri, scopes, db_path, principal_id=None):
    table = _table(provider)
    if account.platform not in ({'facebook', 'instagram'} if provider == 'meta' else {'threads'}):
        raise OAuthStateError('OAuth target platform mismatch')
    with profiles._connect(db_path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM accounts WHERE id = ?', (account.id,)).fetchone()
        if row is None or token_state.fingerprint(profiles._row_to_account(row)) != token_state.fingerprint(account):
            raise token_state.StaleCredentialUpdate()
        # Opportunistically erase expired candidate secrets, without a new
        # scheduler or schema. Expiry checks still reject them before any I/O.
        conn.execute(f"UPDATE {table} SET status = 'expired', result_json = '{{}}' "
                     "WHERE status IN ('started', 'processing', 'pending_page_selection', 'pending_ig_selection') "
                     "AND datetime(requested_at) <= datetime('now', ?)",
                     (f'-{OAUTH_TTL_SECONDS} seconds',))
        binding = {'accountId': account.id, 'profileId': account.profile_id,
                   'workspaceId': account.workspace_id, 'platform': account.platform,
                   'principalId': principal_id, 'fingerprint': token_state.fingerprint(account)}
        columns = ['state_token', 'profile_id', 'account_id', 'account_name', 'redirect_uri',
                   'scopes_json', 'status', 'result_json']
        values = [state_token, account.profile_id, account.id, account.account_name, redirect_uri,
                  json.dumps(scopes), 'started', _encode({'binding': binding})]
        if provider == 'meta':
            columns.append('platform')
            values.append(account.platform)
        conn.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in values)})", values)
        conn.commit()


def claim(provider, state_token, *, db_path):
    """Consume a code callback once, before any token exchange starts."""
    with profiles._connect(db_path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        state = _read_locked(conn, provider, state_token, ('started',))
        account = _bound_account(conn, state)
        conn.execute(f"UPDATE {_table(provider)} SET status = 'processing' WHERE state_token = ?", (state_token,))
        conn.commit()
    state.status = 'processing'
    return state, account


def pending(provider, state_token, *, db_path, workspace_id=None, principal_id=None):
    with profiles._connect(db_path) as conn:
        state = _read_locked(conn, provider, state_token, PENDING_STATUSES)
        account = _bound_account(conn, state, authorize=True, workspace_id=workspace_id, principal_id=principal_id)
    return state, account


def store_candidates(state_token, *, candidates, user_token, user_expiry, db_path):
    with profiles._connect(db_path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        state = _read_locked(conn, 'meta', state_token, ('processing',))
        account = _bound_account(conn, state)
        status = 'pending_page_selection' if account.platform == 'facebook' else 'pending_ig_selection'
        result = {'binding': state.result['binding'], 'candidates': candidates,
                  'userAccessToken': user_token, 'userExpiry': user_expiry,
                  'availableCount': len(candidates)}
        conn.execute('UPDATE meta_oauth_requests SET status = ?, result_json = ? WHERE state_token = ?',
                     (status, _encode(result), state_token))
        conn.commit()


def public_result(account, state_token):
    cfg = account.config or {}
    result = {'state': state_token, 'status': 'ok', 'accountId': account.id,
              'profileId': account.profile_id, 'platform': account.platform,
              'accountName': account.account_name}
    for key in ('pageId', 'facebookPageName', 'igUserId', 'instagramUserName',
                'threadUserId', 'threadsUserName', 'avatarUrl'):
        if cfg.get(key):
            result[key] = cfg[key]
    return result


def finish(provider, state_token, changes, *, db_path, selection=False, workspace_id=None, principal_id=None):
    with profiles._connect(db_path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        statuses = PENDING_STATUSES if selection else ('processing',)
        state = _read_locked(conn, provider, state_token, statuses)
        account = _bound_account(conn, state, authorize=selection, workspace_id=workspace_id, principal_id=principal_id)
        updated = token_state.require_saved(token_state._merge_locked(
            conn, account_id=account.id, expected=state.result['binding']['fingerprint'],
            changes=changes, success=True,
        ))
        result = public_result(updated, state_token)
        # Candidates and all OAuth secrets disappear on consumption. The only
        # permanent secret copy is the encrypted account configuration.
        conn.execute(f"UPDATE {_table(provider)} SET status = 'completed', result_json = ?, error_text = NULL, completed_at = CURRENT_TIMESTAMP WHERE state_token = ?",
                     (_encode(result), state_token))
        conn.commit()
    return updated, result


def fail(provider, state_token, exc, *, db_path):
    reason = token_state.safe_error(exc)
    with profiles._connect(db_path) as conn:
        # Never overwrite a completed/pending flow on duplicate callbacks.
        conn.execute(f"UPDATE {_table(provider)} SET status = 'error', result_json = ?, error_text = ?, completed_at = CURRENT_TIMESTAMP WHERE state_token = ? AND status = 'processing'",
                     (_encode({'state': state_token, 'error': reason}), reason, state_token))
        conn.commit()
