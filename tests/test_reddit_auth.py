"""Tests for Reddit OAuth helpers."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from myUtils import reddit_auth


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


class RedditAuthTests(unittest.TestCase):
    def test_build_authorize_url_contains_expected_parameters(self):
        url = reddit_auth.build_authorize_url(
            client_id='client-id',
            redirect_uri='https://up.iamwillywang.com/oauth/reddit/callback',
            state='state123',
            scopes=('identity', 'submit'),
        )
        self.assertIn('client_id=client-id', url)
        self.assertIn('response_type=code', url)
        self.assertIn('state=state123', url)
        self.assertIn('duration=permanent', url)

    def test_exchange_code_for_token_uses_expected_endpoint(self):
        session = _FakeSession([_FakeResponse({'access_token': 'token', 'refresh_token': 'refresh'})])
        with patch.dict(os.environ, {'REDDIT_CLIENT_ID': 'cid', 'REDDIT_CLIENT_SECRET': 'secret'}, clear=False):
            payload = reddit_auth.exchange_code_for_token(
                code='auth-code',
                redirect_uri='https://up.iamwillywang.com/oauth/reddit/callback',
                session=session,
            )
        self.assertEqual(payload['access_token'], 'token')
        self.assertEqual(session.calls[0][1], reddit_auth.REDDIT_TOKEN_URL)
        self.assertEqual(session.calls[0][2]['data']['grant_type'], 'authorization_code')

    def test_fetch_user_info_uses_expected_endpoint(self):
        session = _FakeSession([_FakeResponse({'name': 'demo-user'})])
        payload = reddit_auth.fetch_user_info(access_token='token', session=session)
        self.assertEqual(payload['name'], 'demo-user')
        self.assertEqual(session.calls[0][1], reddit_auth.REDDIT_ME_URL)
        self.assertEqual(session.calls[0][2]['headers']['Authorization'], 'Bearer token')

    def test_build_authorize_url_with_default_scopes(self):
        url = reddit_auth.build_authorize_url(
            client_id='cid',
            redirect_uri='https://example.com/cb',
            state='st',
        )
        self.assertIn('scope=identity+submit+read', url)

    def test_build_state_token_is_url_safe(self):
        token = reddit_auth.build_state_token()
        self.assertIsInstance(token, str)
        self.assertGreater(len(token), 10)

    def test_exchange_code_raises_on_oauth_error(self):
        session = _FakeSession([_FakeResponse({'error': 'invalid_grant', 'error_description': 'Code expired'})])
        with patch.dict(os.environ, {'REDDIT_CLIENT_ID': 'cid', 'REDDIT_CLIENT_SECRET': 'secret'}, clear=False):
            with self.assertRaises(reddit_auth.RedditOAuthError) as ctx:
                reddit_auth.exchange_code_for_token(
                    code='bad-code',
                    redirect_uri='https://example.com/cb',
                    session=session,
                )
        self.assertIn('Code expired', str(ctx.exception))

    def test_fetch_user_info_raises_on_error(self):
        session = _FakeSession([_FakeResponse({'error': 401, 'message': 'Unauthorized'})])
        with self.assertRaises(reddit_auth.RedditOAuthError) as ctx:
            reddit_auth.fetch_user_info(access_token='bad-token', session=session)
        self.assertIn('Unauthorized', str(ctx.exception))

    def test_required_env_raises_when_missing(self):
        with patch.dict(os.environ, {'REDDIT_CLIENT_ID': ''}, clear=False):
            with self.assertRaises(reddit_auth.RedditOAuthError) as ctx:
                reddit_auth._required_env('REDDIT_CLIENT_ID')
        self.assertIn('REDDIT_CLIENT_ID', str(ctx.exception))

    def test_user_agent_defaults_when_none(self):
        agent = reddit_auth._user_agent(None)
        self.assertIn('social-auto-upload', agent)

    def test_user_agent_uses_provided_value(self):
        agent = reddit_auth._user_agent('custom-agent/1.0')
        self.assertEqual(agent, 'custom-agent/1.0')


if __name__ == '__main__':
    unittest.main()
