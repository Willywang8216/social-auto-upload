"""Shared, identity-preserving Meta/Threads maintenance for web and worker.

Provider calls return an auth-only patch. Callers commit via token_state CAS;
partial page-token sync or a rejected identity never persists any new token.
"""
from __future__ import annotations

import os
from datetime import timedelta

from myUtils import meta_auth, threads_auth, token_state


def _hours(name, default):
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def self_check_due(config, *, healthy_hours=None, retry_hours=None) -> bool:
    failed = token_state._count(config.get('_tokenSelfCheckFailures')) > 0
    interval = (retry_hours or _hours('SAU_META_SELFCHECK_RETRY_HOURS', 1)) if failed else (
        healthy_hours or _hours('SAU_META_SELFCHECK_HOURS', 12))
    last = token_state.parse_time(config.get('_lastTokenSelfCheckAt'))
    return last is None or token_state.utc_now() - last >= timedelta(hours=interval)


def meta_maintenance_due(config, *, margin_seconds=7 * 86400) -> bool:
    if config.get('_needsReconnect') or not config.get('metaUserAccessToken'):
        return False
    retry = token_state.parse_time(config.get('_nextMaintenanceAttemptAt'))
    now = token_state.utc_now()
    if retry is not None and retry > now:
        return False
    if self_check_due(config):
        return True
    # Data-access renewal is distinct from token expiry. Near deadlines trigger
    # read validation too, but at most hourly if a read cannot extend them.
    last = token_state.parse_time(config.get('_lastTokenSelfCheckAt'))
    if last and now - last < timedelta(hours=_hours('SAU_META_SELFCHECK_RETRY_HOURS', 1)):
        return False
    if not config.get('accessToken'):
        return True
    for key, mode in (('metaUserAccessTokenExpiresAt', 'metaTokenExpiryMode'),
                      ('accessTokenExpiresAt', 'accessTokenExpiryMode'),
                      ('metaUserDataAccessExpiresAt', None), ('dataAccessExpiresAt', None)):
        if mode and config.get(mode) == 'never':
            continue
        deadline = token_state.parse_time(config.get(key))
        if deadline and deadline <= now + timedelta(seconds=margin_seconds):
            return True
    return False


def threads_refresh_allowed(config) -> bool:
    # Threads only permits refresh for long-lived tokens at least 24h old.
    minted = token_state.parse_time(config.get('accessTokenUpdatedAt') or config.get('connectedAt'))
    return minted is None or token_state.utc_now() - minted >= timedelta(hours=24)


def identity_error(message):
    return token_state.CredentialError(message, code='identity_mismatch', terminal=True, kind='identity')


def meta_candidates(pages, platform, config, *, require_identity=False):
    if not isinstance(pages, list):
        raise meta_auth.MetaOAuthError('Meta returned an unreadable Page list')
    wanted_page = str(config.get('pageId') or '').strip()
    wanted_ig = str(config.get('igUserId') or '').strip()
    wanted = wanted_page if platform == 'facebook' else wanted_ig
    if not wanted and (require_identity or config.get('accessToken') or config.get('metaUserAccessToken')):
        raise identity_error('Existing Meta credentials have no stable identity; configure the exact identity before reconnecting')
    candidates, seen = [], set()
    for page in pages:
        if not isinstance(page, dict) or not str(page.get('id') or '').strip():
            raise meta_auth.MetaOAuthError('Meta returned a Page without a stable identity')
        ig = page.get('instagram_business_account')
        if platform == 'instagram' and (not isinstance(ig, dict) or not ig.get('id')):
            continue
        candidate_id = str(page['id']) if platform == 'facebook' else str(ig['id'])
        if candidate_id in seen:
            raise identity_error('Meta returned ambiguous identities; no credentials changed')
        seen.add(candidate_id)
        if wanted and candidate_id != wanted:
            continue
        if wanted_page and str(page['id']) != wanted_page:
            continue
        candidates.append(page)
    if not candidates:
        raise identity_error('The configured Meta identity is not in the authorized Page/Instagram list; no other identity was selected')
    if wanted and len(candidates) != 1:
        raise identity_error('Meta identity is ambiguous; no credentials changed')
    return candidates


def candidate_id(page, platform):
    return str(page['id']) if platform == 'facebook' else str(page['instagram_business_account']['id'])


def _expiry_patch(resolved, *, user=False):
    keys = ('metaUserAccessTokenExpiresAt', 'metaTokenExpiryMode', 'metaTokenExpiresAtEpoch',
            'metaUserDataAccessExpiresAt', 'metaUserDataAccessExpiresAtEpoch') if user else (
            'accessTokenExpiresAt', 'accessTokenExpiryMode', 'accessTokenExpiresAtEpoch',
            'dataAccessExpiresAt', 'dataAccessExpiresAtEpoch')
    values = (resolved['expires_at_iso'], resolved['mode'], resolved['expires_at_epoch'],
              resolved.get('data_access_expires_at_iso', ''), resolved.get('data_access_expires_at_epoch'))
    return dict(zip(keys, values))


def _usable_expiry(resolved):
    now = token_state.utc_now()
    for key in ('expires_at_iso', 'data_access_expires_at_iso'):
        if key == 'expires_at_iso' and resolved.get('mode') == 'never':
            continue
        deadline = token_state.parse_time(resolved.get(key))
        if deadline and deadline <= now:
            raise token_state.CredentialError('Provider reports a passed token or data-access deadline; reconnect required',
                                              code='data_access_expired' if key.startswith('data_') else 'token_expired',
                                              terminal=True, kind='credential')


def meta_credential_patch(config, platform, *, page, user_token, user_expiry, session=None):
    # The caller's candidate came from a server-side provider response, never
    # from a browser token payload. Recheck the configured stable identity.
    meta_candidates([page], platform, config)
    page_token = str(page.get('access_token') or '').strip()
    if not user_token or not page_token:
        raise meta_auth.MetaOAuthError('Meta did not return both user and Page credentials; no credentials changed')
    page_expiry = meta_auth.resolve_access_token_expiry(access_token=page_token, session=session)
    _usable_expiry(user_expiry)
    _usable_expiry(page_expiry)
    patch = {'accessToken': page_token, 'metaUserAccessToken': user_token,
             'pageId': str(page['id']), 'facebookPageName': str(page.get('name') or ''),
             'accessTokenUpdatedAt': token_state.iso_now()}
    patch.update(_expiry_patch(user_expiry, user=True))
    patch.update(_expiry_patch(page_expiry))
    if platform == 'instagram':
        ig = page['instagram_business_account']
        patch.update(igUserId=str(ig['id']), instagramUserName=str(ig.get('username') or ''))
        avatar = ig.get('profile_picture_url')
    else:
        avatar = (page.get('picture') or {}).get('data', {}).get('url')
    if avatar:
        patch['avatarUrl'] = str(avatar)
    return patch


def maintain_meta(config, platform, *, mode='auto', margin_seconds=7 * 86400, read_only=False, session=None):
    user_token = str(config.get('metaUserAccessToken') or '').strip()
    if not user_token:
        raise token_state.CredentialError('Meta user credential is missing; reconnect required',
                                          code='missing_credential', terminal=True, kind='credential')
    debug = meta_auth.debug_token_info(access_token=user_token, **({'session': session} if session is not None else {}))
    resolved = meta_auth.resolve_access_token_expiry(access_token=user_token, debug_data=debug)
    # Introspection rejects revocation; never exchange an explicitly invalid
    # credential. A periodic check alone does not churn a valid never token.
    deadline = token_state.parse_time(resolved.get('expires_at_iso'))
    if resolved['mode'] == 'unknown':
        deadline = token_state.parse_time(config.get('metaUserAccessTokenExpiresAt'))
    needs_rotate = resolved['mode'] != 'never' and deadline is not None and (
        deadline <= token_state.utc_now() + timedelta(seconds=margin_seconds))
    if needs_rotate and not read_only:
        refreshed = (meta_auth.refresh_instagram_user_token(access_token=user_token, session=session) if platform == 'instagram'
                     else meta_auth.exchange_for_long_lived_token(access_token=user_token, session=session))
        user_token = str(refreshed.get('access_token') or '').strip()
        if not user_token:
            raise meta_auth.MetaOAuthError('Meta long-lived exchange returned no credential; no credentials changed')
        resolved = meta_auth.resolve_access_token_expiry(access_token=user_token, expires_in=refreshed.get('expires_in'), session=session)
    pages = meta_auth.fetch_managed_pages(access_token=user_token, session=session).get('data')
    page = meta_candidates(pages, platform, config, require_identity=True)[0]
    patch = meta_credential_patch(config, platform, page=page, user_token=user_token, user_expiry=resolved, session=session)
    if read_only:
        patch['lastConnectionCheckAt'] = token_state.iso_now()
    else:
        patch['lastAutoRefreshAt' if mode == 'auto' else 'lastManualRefreshAt'] = token_state.iso_now()
    return patch


def threads_credential_patch(config, token_payload, me, *, reconnect=False, short_user_id=None):
    access_token = str(token_payload.get('access_token') or '').strip()
    user_id = str(me.get('id') or '').strip()
    expected_ids = {str(config[key]).strip() for key in ('threadUserId', 'userId') if config.get(key)}
    if not access_token or not user_id:
        raise threads_auth.ThreadsOAuthError('Threads returned no verified credential/identity')
    if (len(expected_ids) > 1 or (expected_ids and user_id not in expected_ids)
            or (short_user_id and str(short_user_id) != user_id)):
        raise identity_error('Threads authorization returned a different identity; no credentials changed')
    if not expected_ids and (not reconnect or config.get('accessToken')):
        raise identity_error('Existing Threads credentials have no stable identity; configure the exact identity before reconnecting')
    expiry = meta_auth.resolve_access_token_expiry(access_token=access_token, expires_in=token_payload.get('expires_in'), debug_data={})
    patch = {'accessToken': access_token, 'threadUserId': user_id, 'userId': user_id,
             'threadsUserName': str(me.get('username') or config.get('threadsUserName') or ''),
             'accessTokenUpdatedAt': token_state.iso_now()}
    patch.update(_expiry_patch(expiry))
    if me.get('threads_profile_picture_url'):
        patch['avatarUrl'] = str(me['threads_profile_picture_url'])
    return patch


def maintain_threads(config, *, mode='auto', read_only=False, session=None):
    access_token = str(config.get('accessToken') or '').strip()
    if not access_token:
        raise threads_auth.ThreadsOAuthError('Threads credential is missing; reconnect required',
                                             code='missing_credential', terminal=True, kind='credential')
    if not read_only and not threads_refresh_allowed(config):
        raise threads_auth.ThreadsOAuthError('Threads tokens must be at least 24 hours old before refresh',
                                             code='refresh_too_early', kind='transient')
    refreshed = {'access_token': access_token} if read_only else threads_auth.refresh_long_lived_token(access_token=access_token, session=session)
    me = threads_auth.fetch_me(access_token=str(refreshed.get('access_token') or ''), session=session,
                               fields=('id', 'username', 'threads_profile_picture_url'))
    patch = threads_credential_patch(config, refreshed, me)
    if read_only:
        # A read does not mint a token or change its known expiry / minimum age.
        for key in _expiry_patch(meta_auth.resolve_access_token_expiry(access_token='', debug_data={})):
            patch.pop(key, None)
        patch.pop('accessTokenUpdatedAt', None)
        patch['lastConnectionCheckAt'] = token_state.iso_now()
    else:
        patch['lastAutoRefreshAt' if mode == 'auto' else 'lastManualRefreshAt'] = token_state.iso_now()
    return patch
