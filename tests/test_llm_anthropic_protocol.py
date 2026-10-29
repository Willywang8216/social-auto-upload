"""The pool must be able to speak the Anthropic Messages API.

AgentRouter (the operator's 12-account set, and every Claude-Code-style gateway)
rejects ``/v1/chat/completions`` outright::

    模型 gpt-6.1-sol 不支持 /v1/chat/completions 接口，请改用 /v1/messages

so the whole pool was unusable against it even though every key was valid. A
pool entry now carries ``protocol: anthropic`` and the client posts to
``/v1/messages`` with ``x-api-key`` instead. Two details are load-bearing and
pinned here because both cost a debugging round trip:

* the Claude-Code header set (``user-agent: claude-cli``, ``x-app: cli``) is
  required — a bare ``x-api-key`` gets ``401 unauthorized client detected``;
* ``temperature`` must be omitted — gpt-6.1-sol answers ``400 ... does not
  support 0.3 with this model. Only the default (1) value is supported.``
"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from myUtils import llm_client


class _Response:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _Session:
    def __init__(self, payload: dict | None = None):
        self.calls: list[dict] = []
        self.payload = payload or {"content": [{"type": "text", "text": "ok"}]}

    def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002
        self.calls.append({"url": url, "headers": dict(headers or {}), "json": json})
        return _Response(self.payload)


def _pool(protocol: str) -> str:
    return json.dumps([{
        "base_url": "https://agentrouter.org",
        "api_key": "sk-test",
        "model": "gpt-6.1-sol",
        "protocol": protocol,
    }])


class AnthropicProtocolTests(unittest.TestCase):
    def test_protocol_names_normalise(self):
        for value in ("anthropic", "Anthropic", "messages", "claude"):
            self.assertEqual(llm_client._normalise_protocol(value), "anthropic")
        for value in (None, "", "openai", "whatever"):
            self.assertEqual(llm_client._normalise_protocol(value), "openai")

    def test_pool_keeps_the_protocol(self):
        with patch.dict(os.environ, {"SAU_LLM_POOL": _pool("anthropic")}):
            entry = llm_client._load_pool()[0]
        self.assertEqual(entry["protocol"], "anthropic")
        with patch.dict(os.environ, {"SAU_LLM_POOL": _pool("openai")}):
            self.assertEqual(llm_client._load_pool()[0]["protocol"], "openai")

    def test_anthropic_entry_posts_to_messages_with_x_api_key(self):
        session = _Session()
        with patch.dict(os.environ, {"SAU_LLM_POOL": _pool("anthropic")}):
            result = llm_client.generate_chat_completion(
                "You are a test.", "Reply with the single word: ok",
                session=session, api_base_url=None, api_key=None,
            )
        self.assertEqual(result.content, "ok")
        call = session.calls[0]
        self.assertTrue(call["url"].endswith("/v1/messages"), call["url"])
        self.assertIn("x-api-key", call["headers"])
        self.assertNotIn("Authorization", call["headers"])
        # the Claude-Code fingerprint is what the gateway authenticates against
        self.assertIn("claude-cli", call["headers"]["user-agent"])
        self.assertEqual(call["headers"]["x-app"], "cli")

    def test_temperature_is_not_sent(self):
        session = _Session()
        with patch.dict(os.environ, {"SAU_LLM_POOL": _pool("anthropic")}):
            llm_client.generate_chat_completion(
                "s", "u", temperature=0.3, session=session,
            )
        self.assertNotIn("temperature", session.calls[0]["json"])

    def test_the_system_prompt_is_a_top_level_field(self):
        session = _Session()
        with patch.dict(os.environ, {"SAU_LLM_POOL": _pool("anthropic")}):
            llm_client.generate_chat_completion("SYSTEM HERE", "user", session=session)
        body = session.calls[0]["json"]
        self.assertEqual(body["system"], "SYSTEM HERE")
        self.assertEqual(body["messages"], [{"role": "user", "content": "user"}])
        self.assertEqual(body["model"], "gpt-6.1-sol")
        self.assertIn("max_tokens", body)

    def test_json_mode_asks_for_json_and_parses_it(self):
        session = _Session({"content": [{"type": "text", "text": '{"title": "x"}'}]})
        with patch.dict(os.environ, {"SAU_LLM_POOL": _pool("anthropic")}):
            result = llm_client.generate_chat_completion("s", "u", response_json=True, session=session)
        self.assertIn("JSON", session.calls[0]["json"]["system"])
        self.assertEqual(result.parsed_json, {"title": "x"})

    def test_an_openai_entry_still_posts_to_chat_completions(self):
        session = _Session({"choices": [{"message": {"content": "ok"}}]})
        with patch.dict(os.environ, {"SAU_LLM_POOL": _pool("openai")}):
            result = llm_client.generate_chat_completion("s", "u", session=session)
        self.assertEqual(result.content, "ok")
        self.assertTrue(session.calls[0]["url"].endswith("/v1/chat/completions"))
        self.assertIn("Authorization", session.calls[0]["headers"])

    def test_a_dead_anthropic_entry_rotates_to_a_working_one(self):
        class _Mixed(_Session):
            def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002
                self.calls.append({"url": url, "headers": dict(headers or {}), "json": json})
                if "127.0.0.1" in url:
                    raise ConnectionError("refused")
                return _Response({"content": [{"type": "text", "text": "ok"}]})

        pool = json.dumps([
            {"base_url": "http://127.0.0.1:9", "api_key": "dead", "model": "x", "protocol": "anthropic"},
            {"base_url": "https://agentrouter.org", "api_key": "sk-test", "model": "gpt-6.1-sol",
             "protocol": "anthropic"},
        ])
        session = _Mixed()
        with patch.dict(os.environ, {"SAU_LLM_POOL": pool}):
            result = llm_client.generate_chat_completion("s", "u", session=session)
        self.assertEqual(result.content, "ok")
        self.assertEqual(len(session.calls), 2)


if __name__ == "__main__":
    unittest.main()
