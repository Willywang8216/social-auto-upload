"""Tests for the LLM API wrapper."""

from __future__ import annotations

import json
import inspect
import time
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from myUtils import llm_client


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeSession:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _FakeResponse(self.payload)


def _llm_env(**overrides):
    """A single-endpoint LLM env with the *pool* explicitly switched off.

    The client prefers SAU_LLM_POOL over the legacy SAU_LLM_* variables (see
    _load_pool), so on a box that configures a pool the legacy vars are ignored,
    the request goes to the pooled endpoint, and these URL assertions fail for
    a reason that has nothing to do with the code under test.
    """
    return patch.dict(os.environ, {"SAU_LLM_POOL": "", **overrides}, clear=False)


class LlmClientTests(unittest.TestCase):
    def test_generate_chat_completion_parses_json_content(self) -> None:
        session = _FakeSession(
            {
                "choices": [
                    {"message": {"content": '{"message":"hello","hashtags":["#a","#b","#c"]}'}}
                ]
            }
        )
        with _llm_env(
            SAU_LLM_API_BASE_URL="https://llm.example.com",
            SAU_LLM_API_KEY="test-key",
        ):
            result = llm_client.generate_chat_completion(
                "system",
                "user",
                session=session,
                response_json=True,
            )
        self.assertEqual(result.parsed_json, {"message": "hello", "hashtags": ["#a", "#b", "#c"]})
        self.assertEqual(session.calls[0]["url"], "https://llm.example.com/v1/chat/completions")

    def test_transcribe_audio_hits_openai_compatible_endpoint(self) -> None:
        session = _FakeSession({"text": "transcript"})
        with tempfile.TemporaryDirectory() as tmp_dir, _llm_env(
            SAU_LLM_API_BASE_URL="https://llm.example.com/v1",
            SAU_LLM_API_KEY="test-key",
        ):
            audio_file = Path(tmp_dir) / "audio.wav"
            audio_file.write_bytes(b"wav")
            result = llm_client.transcribe_audio(audio_file, session=session)
        self.assertEqual(result.text, "transcript")
        self.assertEqual(
            session.calls[0]["url"],
            "https://llm.example.com/v1/audio/transcriptions",
        )
        self.assertIn("files", session.calls[0])


class _SeqResponse:
    def __init__(self, payload: dict | None = None, error: Exception | None = None) -> None:
        self._payload = payload or {}
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self) -> dict:
        return self._payload


class _SeqSession:
    """Returns queued responses in order so we can simulate endpoint failures."""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self._responses.pop(0)


class LlmRotationTests(unittest.TestCase):
    def test_rotates_to_next_endpoint_on_failure(self) -> None:
        session = _SeqSession(
            [
                _SeqResponse(error=RuntimeError("500 upstream error")),
                _SeqResponse(payload={"choices": [{"message": {"content": "OK"}}]}),
            ]
        )
        pool = json.dumps(
            [
                {"base_url": "https://a.example.com", "api_key": "k1", "model": "m1"},
                {"base_url": "https://b.example.com", "api_key": "k2", "model": "m2"},
            ]
        )
        with patch.dict(os.environ, {"SAU_LLM_POOL": pool}, clear=False):
            result = llm_client.generate_chat_completion("system", "user", session=session)
        self.assertEqual(result.content, "OK")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0]["url"], "https://a.example.com/v1/chat/completions")
        self.assertEqual(session.calls[1]["url"], "https://b.example.com/v1/chat/completions")
        # Each entry uses its own model.
        self.assertEqual(session.calls[0]["json"]["model"], "m1")
        self.assertEqual(session.calls[1]["json"]["model"], "m2")

    def test_per_entry_headers_are_merged(self) -> None:
        session = _SeqSession([_SeqResponse(payload={"choices": [{"message": {"content": "hi"}}]})])
        pool = json.dumps(
            [{"base_url": "https://a.example.com", "api_key": "k1", "headers": {"X-Trace": "yes"}}]
        )
        with patch.dict(os.environ, {"SAU_LLM_POOL": pool}, clear=False):
            llm_client.generate_chat_completion("system", "user", session=session)
        self.assertEqual(session.calls[0]["headers"]["X-Trace"], "yes")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer k1")

    def test_raises_last_error_when_all_endpoints_fail(self) -> None:
        session = _SeqSession(
            [
                _SeqResponse(error=RuntimeError("boom-1")),
                _SeqResponse(error=RuntimeError("boom-2")),
            ]
        )
        pool = json.dumps(
            [
                {"base_url": "https://a.example.com", "api_key": "k1"},
                {"base_url": "https://b.example.com", "api_key": "k2"},
            ]
        )
        with patch.dict(os.environ, {"SAU_LLM_POOL": pool}, clear=False):
            with self.assertRaises(RuntimeError):
                llm_client.generate_chat_completion("system", "user", session=session)
        self.assertEqual(len(session.calls), 2)


if __name__ == "__main__":
    unittest.main()


class CoerceJsonObjectTests(unittest.TestCase):
    def test_plain_json(self) -> None:
        self.assertEqual(
            llm_client.coerce_json_object('{"message": "hi", "hashtags": []}'),
            {"message": "hi", "hashtags": []},
        )

    def test_fenced_json(self) -> None:
        text = "```json\n{\"message\": \"hi\"}\n```"
        self.assertEqual(llm_client.coerce_json_object(text), {"message": "hi"})

    def test_json_with_surrounding_prose(self) -> None:
        text = "Sure! Here is the draft:\n{\"message\": \"hi\", \"cta\": \"go\"}\nLet me know."
        self.assertEqual(llm_client.coerce_json_object(text), {"message": "hi", "cta": "go"})

    def test_non_json_returns_none(self) -> None:
        self.assertIsNone(llm_client.coerce_json_object("just a caption {not json"))
        self.assertIsNone(llm_client.coerce_json_object(""))
        self.assertIsNone(llm_client.coerce_json_object("[1, 2, 3]"))


class MuyuanCloudflareHeaderTests(unittest.TestCase):
    """The Muyuan gateway is behind Cloudflare and fingerprint-checks POSTs.

    Measured against the live service: a bare `Authorization: Bearer` gets a 403
    with `error code: 1010` (or a "Just a moment..." challenge) on every POST
    route - messages, chat/completions, embeddings, audio/transcriptions. Adding
    any ONE of the four headers below still fails; only all four together get
    through, and then a real completion is returned. So the client must keep
    sending the full Claude Code set, or every LLM and transcription call fails
    at the edge before authentication is even considered.
    """

    REQUIRED = ("anthropic-version", "anthropic-beta", "user-agent", "x-app")

    def test_all_fingerprint_headers_are_sent(self):
        headers = {k.lower(): v for k, v in llm_client._headers("k").items()}
        for name in self.REQUIRED:
            with self.subTest(header=name):
                self.assertIn(name, headers)
                self.assertTrue(str(headers[name]).strip())

    def test_user_agent_identifies_as_claude_cli(self):
        headers = {k.lower(): v for k, v in llm_client._headers("k").items()}
        self.assertIn("claude-cli", headers["user-agent"])
        self.assertEqual(headers["x-app"], "cli")

    def test_authorization_is_still_sent(self):
        headers = llm_client._headers("secret-key")
        self.assertEqual(headers["Authorization"], "Bearer secret-key")

    def test_transcription_uses_multipart_not_json_content_type(self):
        # A multipart body must let requests set the boundary, so the JSON
        # Content-Type has to be dropped for that call - but the Cloudflare
        # headers must survive, or the edge blocks it.
        source = inspect.getsource(llm_client.transcribe_audio)
        self.assertIn('key.lower() != "content-type"', source)
        self.assertIn("files={", source)

    def test_extra_headers_env_can_override(self):
        # A future gateway (or a changed check) must be tunable without a code
        # change.
        with patch.dict(os.environ, {"SAU_LLM_EXTRA_HEADERS": '{"x-custom": "1"}'}):
            headers = llm_client._headers("k")
        self.assertEqual(headers.get("x-custom"), "1")

    def test_bad_extra_headers_is_ignored_not_fatal(self):
        with patch.dict(os.environ, {"SAU_LLM_EXTRA_HEADERS": "not json"}):
            headers = llm_client._headers("k")  # must not raise
        self.assertEqual(headers["Authorization"], "Bearer k")


class MuyuanRateLimitTests(unittest.TestCase):
    """The gateway rate-limits per key/group and must be respected.

    Measured live: 100 requests / 5 minutes on the welfare and default groups and
    only 5 / 5 minutes on the Gemini group. Treating 429 as a generic failure
    burned every remaining endpoint in the pool instantly, so the client now
    honours Retry-After (bounded), pushes the pacing clock forward so the next
    call also waits, and paces consecutive requests.
    """

    class _Resp:
        def __init__(self, status_code=429, headers=None):
            self.status_code = status_code
            self.headers = headers or {}

    def test_retry_after_as_seconds(self):
        self.assertAlmostEqual(
            llm_client._retry_after_seconds(self._Resp(headers={"Retry-After": "12"})),
            12.0,
        )

    def test_retry_after_is_bounded(self):
        huge = llm_client._retry_after_seconds(
            self._Resp(headers={"Retry-After": "99999"})
        )
        self.assertLessEqual(huge, llm_client._MAX_RATE_LIMIT_WAIT)

    def test_missing_retry_after_still_backs_off(self):
        self.assertGreater(llm_client._retry_after_seconds(self._Resp()), 0)

    def test_malformed_retry_after_still_backs_off(self):
        self.assertGreater(
            llm_client._retry_after_seconds(self._Resp(headers={"Retry-After": "soon"})),
            0,
        )

    def test_note_rate_limit_pushes_the_pacing_clock(self):
        with patch.object(llm_client, "_LAST_REQUEST_AT", 0.0):
            llm_client._note_rate_limit(5.0)
            self.assertGreaterEqual(llm_client._LAST_REQUEST_AT, 5.0)

    def test_pacing_can_be_disabled(self):
        with patch.dict(os.environ, {"SAU_LLM_MIN_INTERVAL_SECONDS": "0"}):
            self.assertEqual(llm_client._min_interval_seconds(), 0.0)
            start = time.monotonic()
            llm_client._pace_request()  # must not sleep
            self.assertLess(time.monotonic() - start, 0.5)

    def test_pacing_waits_the_configured_interval(self):
        with patch.dict(os.environ, {"SAU_LLM_MIN_INTERVAL_SECONDS": "0.3"}):
            llm_client._pace_request()  # establishes the clock
            start = time.monotonic()
            llm_client._pace_request()
            self.assertGreaterEqual(time.monotonic() - start, 0.25)

    def test_a_429_is_backed_off_and_retried(self):
        """A 429 must be waited on, not treated as a fatal endpoint failure."""
        import json as _json

        class _FakeSession:
            def __init__(self):
                self.calls = 0

            class _R:
                def __init__(self, code, headers=None, payload=None):
                    self.status_code = code
                    self.headers = headers or {}
                    self._payload = payload or {}

                def json(self):
                    return self._payload

                def raise_for_status(self):
                    if self.status_code >= 400:
                        raise RuntimeError(f"HTTP {self.status_code}")

            def post(self, url, headers=None, json=None, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    return self._R(429, {"Retry-After": "0"})
                return self._R(200, {}, {"choices": [{"message": {"content": "ok"}}]})

        session = _FakeSession()
        pool = [{"base_url": "https://a.example", "api_key": "k", "model": "m"},
                {"base_url": "https://b.example", "api_key": "k", "model": "m"}]
        with patch.dict(os.environ, {"SAU_LLM_POOL": _json.dumps(pool)}):
            result = llm_client.generate_chat_completion(
                system_prompt="s", user_prompt="u", session=session,
            )
        self.assertEqual(getattr(result, "content", result), "ok")
        self.assertGreaterEqual(session.calls, 2, "the 429 must be retried")
