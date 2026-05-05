"""Tests for YouTube OAuth helpers."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from myUtils import youtube_auth


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

    def post(self, url, **kwargs):
        self.calls.append(('POST', url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(('GET', url, kwargs))
        return self.responses.pop(0)


class YouTubeAuthTests(unittest.TestCase):
    def test_build_authorize_url_contains_expected_parameters(self):
        url = youtube_auth.build_authorize_url(
            client_id='client-id',
            redirect_uri='https://up.iamwillywang.com/oauth/youtube/callback',
            state='state123',
            scopes=('scope-a', 'scope-b'),
        )
        self.assertIn('client_id=client-id', url)
        self.assertIn('response_type=code', url)
        self.assertIn('access_type=offline', url)
        self.assertIn('state=state123', url)

    def test_exchange_code_for_token_uses_expected_endpoint(self):
        session = _FakeSession([_FakeResponse({'access_token': 'token', 'refresh_token': 'refresh'})])
        with patch.dict(os.environ, {'YT_CLIENT_ID': 'cid', 'YT_CLIENT_SECRET': 'secret'}, clear=False):
            payload = youtube_auth.exchange_code_for_token(
                code='auth-code',
                redirect_uri='https://up.iamwillywang.com/oauth/youtube/callback',
                session=session,
            )
        self.assertEqual(payload['access_token'], 'token')
        self.assertEqual(session.calls[0][1], youtube_auth.GOOGLE_TOKEN_URL)
        self.assertEqual(session.calls[0][2]['data']['grant_type'], 'authorization_code')

    def test_fetch_my_channels_uses_expected_endpoint(self):
        session = _FakeSession([_FakeResponse({'items': [{'id': 'UC123'}]})])
        payload = youtube_auth.fetch_my_channels(access_token='token', session=session)
        self.assertEqual(payload['items'][0]['id'], 'UC123')
        self.assertEqual(session.calls[0][1], youtube_auth.YOUTUBE_CHANNELS_URL)
        self.assertEqual(session.calls[0][2]['headers']['Authorization'], 'Bearer token')


if __name__ == '__main__':
    unittest.main()
