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


if __name__ == "__main__":
    unittest.main()
