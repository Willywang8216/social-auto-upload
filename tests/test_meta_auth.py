"""Tests for Meta OAuth helpers."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from myUtils import meta_auth


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

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


if __name__ == '__main__':
    unittest.main()
