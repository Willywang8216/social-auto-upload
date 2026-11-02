"""Tests for Meta OAuth helpers."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from myUtils import meta_auth


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.ok = True

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(('GET', url, kwargs))
        return self.responses.pop(0)


class MetaAuthTests(unittest.TestCase):
    def test_build_authorize_url_contains_expected_parameters(self):
        url = meta_auth.build_authorize_url(
            client_id='meta-app-id',
            redirect_uri='https://up.iamwillywang.com/oauth/meta/callback',
            state='state123',
            scopes=('pages_show_list', 'business_management'),
        )
        self.assertIn('client_id=meta-app-id', url)
        self.assertIn('response_type=code', url)
        self.assertIn('state=state123', url)
        self.assertIn('pages_show_list%2Cbusiness_management', url)

    def test_exchange_code_for_token_uses_expected_endpoint(self):
        session = _FakeSession([_FakeResponse({'access_token': 'token'})])
        with patch.dict(os.environ, {'META_APP_ID': 'app-id', 'META_APP_SECRET': 'app-secret'}, clear=False):
            payload = meta_auth.exchange_code_for_token(code='auth-code', redirect_uri='https://up.iamwillywang.com/oauth/meta/callback', session=session)
        self.assertEqual(payload['access_token'], 'token')
        self.assertEqual(session.calls[0][1], meta_auth.META_TOKEN_URL)

    def test_fetch_managed_pages_uses_expected_endpoint(self):
        session = _FakeSession([_FakeResponse({'data': [{'id': '123', 'name': 'Brand Page'}]})])
        payload = meta_auth.fetch_managed_pages(access_token='token', session=session)
        self.assertEqual(payload['data'][0]['name'], 'Brand Page')
        self.assertEqual(session.calls[0][1], meta_auth.META_ME_ACCOUNTS_URL)

    def test_refresh_instagram_user_token_uses_ig_endpoint(self):
        session = _FakeSession([_FakeResponse({'access_token': 'ig-tok', 'expires_in': 5183944})])
        payload = meta_auth.refresh_instagram_user_token(access_token='long-lived', session=session)
        self.assertEqual(payload['access_token'], 'ig-tok')
        self.assertEqual(session.calls[0][1], meta_auth.INSTAGRAM_REFRESH_TOKEN_URL)
        self.assertEqual(session.calls[0][2]['params']['grant_type'], 'ig_refresh_token')
        self.assertEqual(session.calls[0][2]['params']['access_token'], 'long-lived')

    def test_refresh_instagram_user_token_falls_back_on_error(self):
        class _ErrorResponse(_FakeResponse):
            ok = False
        session = _FakeSession([
            _ErrorResponse({'error': {'message': 'bad token'}}),
            _FakeResponse({'access_token': 'fb-tok', 'expires_in': 5183944}),
        ])
        with patch.dict(os.environ, {'META_APP_ID': 'app-id', 'META_APP_SECRET': 'app-secret'}, clear=False):
            payload = meta_auth.refresh_instagram_user_token(access_token='long-lived', session=session)
        self.assertEqual(payload['access_token'], 'fb-tok')
        self.assertEqual(session.calls[1][1], meta_auth.META_TOKEN_URL)


class ResolveAccessTokenExpiryTests(unittest.TestCase):
    """Never-expiring Meta tokens (expires_at == 0, no expires_in) must resolve
    to a far-future sentinel so SAU never marks a healthy account "expired".
    """

    def _assert_far_future(self, iso: str) -> None:
        import datetime as _dt
        bound = (_dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) + _dt.timedelta(days=365 * 9)).isoformat()
        self.assertGreater(iso, bound)

    def test_never_expiring_token_gets_far_future_sentinel(self):
        with patch.object(meta_auth, 'debug_token_info', return_value={
            'expires_at': 0, 'is_valid': True, 'type': 'USER',
        }):
            res = meta_auth.resolve_access_token_expiry(access_token='never-tok')
        self.assertEqual(res['mode'], 'never')
        self.assertEqual(res['expires_at_epoch'], 0)
        self._assert_far_future(res['expires_at_iso'])

    def test_expiring_token_gets_exact_iso(self):
        import datetime as _dt
        epoch = int(_dt.datetime(2030, 6, 1, tzinfo=_dt.timezone.utc).timestamp())
        with patch.object(meta_auth, 'debug_token_info', return_value={'expires_at': epoch}):
            res = meta_auth.resolve_access_token_expiry(access_token='expiring-tok')
        self.assertEqual(res['mode'], 'expiring')
        self.assertEqual(res['expires_at_epoch'], epoch)
        self.assertEqual(res['expires_at_iso'], '2030-06-01T00:00:00')

    def test_debug_unavailable_falls_back_to_expires_in(self):
        import datetime as _dt
        with patch.object(meta_auth, 'debug_token_info', return_value={}):
            res = meta_auth.resolve_access_token_expiry(access_token='tok', expires_in=3600)
        self.assertEqual(res['mode'], 'expiring')
        got = _dt.datetime.fromisoformat(res['expires_at_iso'])
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        # ISO must be ~now + 3600s (tolerance for test runtime skew)
        self.assertLess(abs((got - now).total_seconds() - 3600), 300)

    def test_invalid_token_verdict_is_not_masked(self):
        from myUtils.meta_auth import MetaOAuthError
        with patch.object(meta_auth, 'debug_token_info', side_effect=MetaOAuthError('Invalid OAuth access token')):
            with self.assertRaises(MetaOAuthError):
                meta_auth.resolve_access_token_expiry(access_token='dead-tok')

    def test_no_debug_and_no_expires_in_stays_unknown(self):
        with patch.object(meta_auth, 'debug_token_info', return_value={}):
            res = meta_auth.resolve_access_token_expiry(access_token='tok')
        self.assertEqual(res['mode'], 'unknown')
        self.assertEqual(res['expires_at_iso'], '')
        self.assertIsNone(res['expires_at_epoch'])


if __name__ == '__main__':
    unittest.main()


def test_invalid_debug_data_is_rejected_with_safe_provider_codes(monkeypatch):
    import pytest
    monkeypatch.setenv('META_APP_ID', 'synthetic-app')
    monkeypatch.setenv('META_APP_SECRET', 'synthetic-secret')
    error = {'is_valid': False, 'expires_at': 0, 'error': {
        'code': 190, 'error_subcode': 460, 'message': 'secret-token-in-provider-message'}}
    session = _FakeSession([_FakeResponse({'data': error})])
    with pytest.raises(meta_auth.MetaOAuthError) as exc:
        meta_auth.debug_token_info(access_token='synthetic', session=session)
    assert exc.value.terminal
    assert (exc.value.error_code, exc.value.error_subcode) == (190, 460)
    assert 'secret-token' not in str(exc.value)
    with pytest.raises(meta_auth.MetaOAuthError):
        meta_auth.resolve_access_token_expiry(access_token='synthetic', expires_in=5184000, debug_data=error)


def test_valid_zero_outranks_duration_and_tracks_data_access():
    import datetime as dt
    deadline = int(dt.datetime(2035, 1, 1, tzinfo=dt.timezone.utc).timestamp())
    resolved = meta_auth.resolve_access_token_expiry(access_token='synthetic', expires_in=3600,
        debug_data={'is_valid': True, 'expires_at': 0, 'data_access_expires_at': deadline})
    assert resolved['mode'] == 'never'
    assert resolved['expires_at_epoch'] == 0
    assert resolved['data_access_expires_at_epoch'] == deadline
    assert resolved['data_access_expires_at_iso'] == '2035-01-01T00:00:00'


def test_zero_without_affirmative_validity_is_unknown():
    for data in ({}, {'expires_at': 0}, {'is_valid': True}, {'is_valid': None, 'expires_at': 0}):
        resolved = meta_auth.resolve_access_token_expiry(access_token='synthetic', debug_data=data)
        assert resolved['mode'] == 'unknown'
        assert resolved['expires_at_iso'] == ''


def test_duration_epoch_is_calculated_before_removing_utc_timezone(monkeypatch):
    import datetime as dt
    real_datetime = dt.datetime
    class Clock(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2030, 1, 2, 3, 4, 5, tzinfo=tz)
        def timestamp(self):
            # A naive datetime.timestamp() would silently use local time.
            assert self.tzinfo is not None
            return super().timestamp()
    monkeypatch.setattr(meta_auth.datetime, 'datetime', Clock)
    resolved = meta_auth.resolve_access_token_expiry(access_token='synthetic', expires_in=3600, debug_data={})
    assert resolved['expires_at_iso'] == '2030-01-02T04:04:05'
    assert resolved['expires_at_epoch'] == int(real_datetime(2030, 1, 2, 4, 4, 5, tzinfo=dt.timezone.utc).timestamp())


def test_managed_pages_follow_validated_cursor_without_forwarding_url_token():
    session = _FakeSession([
        _FakeResponse({'data': [{'id': 'other'}], 'paging': {
            'next': meta_auth.META_ME_ACCOUNTS_URL + '?after=next-page&access_token=untrusted',
            'cursors': {'after': 'next-page'}}}),
        _FakeResponse({'data': [{'id': 'wanted', 'access_token': 'page-token'}]}),
    ])
    result = meta_auth.fetch_managed_pages(access_token='synthetic', session=session)
    assert [p['id'] for p in result['data']] == ['other', 'wanted']
    assert session.calls[1][1] == meta_auth.META_ME_ACCOUNTS_URL
    assert session.calls[1][2]['params']['access_token'] == 'synthetic'
    assert session.calls[1][2]['params']['after'] == 'next-page'


def test_managed_pages_reject_unsafe_cycles_ambiguity_and_truncation():
    import pytest
    for next_url in ('https://attacker.invalid/me/accounts?after=x',
                     'http://graph.facebook.com/me/accounts?after=x',
                     'https://graph.facebook.com/me/feed?after=x'):
        session = _FakeSession([_FakeResponse({'data': [], 'paging': {'next': next_url}})])
        with pytest.raises(meta_auth.MetaOAuthError):
            meta_auth.fetch_managed_pages(access_token='synthetic', session=session)
        assert len(session.calls) == 1
    paged = {'data': [], 'paging': {'next': meta_auth.META_ME_ACCOUNTS_URL + '?after=same'}}
    for responses, cap in (([paged, paged], 25), ([paged], 1),
                            ([{'data': [{'id': 'same'}, {'id': 'same'}]}], 25)):
        with pytest.raises(meta_auth.MetaOAuthError):
            meta_auth.fetch_managed_pages(access_token='synthetic', max_pages=cap,
                session=_FakeSession([_FakeResponse(p) for p in responses]))
