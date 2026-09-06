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

    def test_no_debug_and_no_expires_in_defaults_to_never(self):
        with patch.object(meta_auth, 'debug_token_info', return_value={}):
            res = meta_auth.resolve_access_token_expiry(access_token='tok')
        self.assertEqual(res['mode'], 'never')
        self._assert_far_future(res['expires_at_iso'])


if __name__ == '__main__':
    unittest.main()
