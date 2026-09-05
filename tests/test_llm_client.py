"""Tests for the LLM API wrapper."""

from __future__ import annotations

import json
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


class LlmClientTests(unittest.TestCase):
    def test_generate_chat_completion_parses_json_content(self) -> None:
        session = _FakeSession(
            {
                "choices": [
                    {"message": {"content": '{"message":"hello","hashtags":["#a","#b","#c"]}'}}
                ]
            }
        )
        with patch.dict(
            os.environ,
            {
                "SAU_LLM_API_BASE_URL": "https://llm.example.com",
                "SAU_LLM_API_KEY": "test-key",
            },
            clear=False,
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
        with tempfile.TemporaryDirectory() as tmp_dir, patch.dict(
            os.environ,
            {
                "SAU_LLM_API_BASE_URL": "https://llm.example.com/v1",
                "SAU_LLM_API_KEY": "test-key",
            },
            clear=False,
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
