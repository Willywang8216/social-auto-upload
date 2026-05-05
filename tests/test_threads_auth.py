"""Tests for Threads OAuth helpers."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from myUtils import threads_auth


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

    def post(self, url, **kwargs):
        self.calls.append(('POST', url, kwargs))
        return self.responses.pop(0)


class ThreadsAuthTests(unittest.TestCase):
    def test_build_authorize_url_contains_expected_parameters(self):
        url = threads_auth.build_authorize_url(
            client_id='threads-app-id',
            redirect_uri='https://up.iamwillywang.com/oauth/threads/callback',
            state='state123',
            scopes=('threads_basic', 'threads_content_publish'),
        )
        self.assertIn('client_id=threads-app-id', url)
        self.assertIn('response_type=code', url)
        self.assertIn('state=state123', url)
        self.assertIn('threads_basic%2Cthreads_content_publish', url)

    def test_exchange_code_for_token_uses_expected_endpoint(self):
        session = _FakeSession([_FakeResponse({'access_token': 'token', 'user_id': 'th-1'})])
        with patch.dict(os.environ, {'THREADS_APP_ID': 'app-id', 'THREADS_APP_SECRET': 'app-secret'}, clear=False):
            payload = threads_auth.exchange_code_for_token(code='auth-code', redirect_uri='https://up.iamwillywang.com/oauth/threads/callback', session=session)
        self.assertEqual(payload['access_token'], 'token')
        self.assertEqual(session.calls[0][1], threads_auth.THREADS_TOKEN_URL)

    def test_fetch_me_uses_expected_endpoint(self):
        session = _FakeSession([_FakeResponse({'id': 'th-1', 'username': 'threads-demo'})])
        payload = threads_auth.fetch_me(access_token='token', session=session)
        self.assertEqual(payload['username'], 'threads-demo')
        self.assertEqual(session.calls[0][1], threads_auth.THREADS_ME_URL)


if __name__ == '__main__':
    unittest.main()
