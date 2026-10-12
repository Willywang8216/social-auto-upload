"""Chat-audio STT (``mimo-v2.6-flash`` on wzw.pp.ua) — all network mocked.

The gateway publishes the model only through ``/v1/chat/completions`` with an
OpenAI ``input_audio`` content part (there is no ``/v1/audio/transcriptions``
route for it and no ``whisper-*`` model). Cloudflare also rejects a bare
``Authorization: Bearer`` with ``403 error code: 1010``, so the request must
reuse :func:`myUtils.llm_client._headers`.

These tests pin the request shape, both transcript fields (``content`` and
``reasoning_content``), the CJK ⇒ ``zh`` language detector, env/profile
overrides, and the ``campaign_media_prep`` wiring. Nothing here touches the
network, the database, or a publisher.
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

if "conf" not in sys.modules:
    conf_module = types.ModuleType("conf")
    conf_module.BASE_DIR = str(Path(__file__).resolve().parent.parent)
    conf_module.DEBUG_MODE = True
    conf_module.LOCAL_CHROME_HEADLESS = True
    conf_module.LOCAL_CHROME_PATH = ""
    sys.modules["conf"] = conf_module

from myUtils import campaign_media_prep
from myUtils import llm_client
from myUtils import profiles as profile_registry

WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt "


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class _FakeSession:
    """Records every POST so the request shape can be asserted."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _FakeResponse(self.payload)


def _env(**overrides):
    """Isolate from the LLM pool and disable pacing so tests do not sleep."""
    base = {"SAU_LLM_POOL": "", "SAU_LLM_MIN_INTERVAL_SECONDS": "0"}
    base.update(overrides)
    return mock.patch.dict(os.environ, base, clear=False)


def _write_wav(directory: str, name: str = "clip.wav") -> Path:
    path = Path(directory) / name
    path.write_bytes(WAV_BYTES)
    return path


class ChatAudioSttTests(unittest.TestCase):
    def test_reads_content_field_and_sends_input_audio_part(self) -> None:
        session = _FakeSession(
            {"choices": [{"message": {"content": "Hello there\nLANGUAGE=en"}}]}
        )
        with tempfile.TemporaryDirectory() as tmp, _env():
            audio = _write_wav(tmp)
            result = llm_client.transcribe_audio_via_chat(
                audio,
                model="mimo-v2.6-flash",
                api_base_url="https://wzw.pp.ua",
                api_key="sk-test",
                session=session,
            )

        self.assertEqual(result.text, "Hello there")
        self.assertEqual(result.language, "en")

        call = session.calls[0]
        # POST /v1/chat/completions (base normalised to /v1), never /audio/transcriptions.
        self.assertEqual(call["url"], "https://wzw.pp.ua/v1/chat/completions")
        self.assertEqual(call["json"]["model"], "mimo-v2.6-flash")

        parts = call["json"]["messages"][0]["content"]
        audio_part = next(part for part in parts if part["type"] == "input_audio")
        self.assertEqual(
            audio_part["input_audio"]["data"],
            base64.b64encode(WAV_BYTES).decode("ascii"),
        )
        self.assertEqual(audio_part["input_audio"]["format"], "wav")
        text_part = next(part for part in parts if part["type"] == "text")
        self.assertIn("Transcribe", text_part["text"])
        self.assertIn("LANGUAGE=zh", text_part["text"])

        # The Cloudflare-bypassing header set must be reused, not a bare Bearer.
        headers = {key.lower(): value for key, value in call["headers"].items()}
        for required in ("anthropic-version", "anthropic-beta", "user-agent", "x-app"):
            self.assertIn(required, headers)
        self.assertEqual(headers["authorization"], "Bearer sk-test")

    def test_reads_reasoning_content_field(self) -> None:
        # Observed live: mimo-v2.6-flash often returns the transcript in
        # reasoning_content with an empty content field.
        session = _FakeSession(
            {"choices": [{"message": {"content": "", "reasoning_content": "<think>你好，大家好\nLANGUAGE=zh"}}]}
        )
        with tempfile.TemporaryDirectory() as tmp, _env():
            audio = _write_wav(tmp)
            result = llm_client.transcribe_audio_via_chat(
                audio,
                api_base_url="https://wzw.pp.ua",
                api_key="sk-test",
                session=session,
            )
        # The gateway wraps reasoning_content in <think>…</think>; the tags must
        # not leak into the caption seed.
        self.assertEqual(result.text, "你好，大家好")
        self.assertEqual(result.language, "zh")

    def test_detects_zh_without_a_language_line(self) -> None:
        session = _FakeSession({"choices": [{"message": {"reasoning_content": "今天天气很好"}}]})
        with tempfile.TemporaryDirectory() as tmp, _env():
            audio = _write_wav(tmp)
            result = llm_client.transcribe_audio_via_chat(audio, session=session, api_key="sk-test")
        self.assertEqual(result.text, "今天天气很好")
        self.assertEqual(result.language, "zh")

    def test_language_falls_back_to_title_when_transcript_is_empty(self) -> None:
        session = _FakeSession({"choices": [{"message": {"content": ""}}]})
        with tempfile.TemporaryDirectory() as tmp, _env():
            audio = _write_wav(tmp)
            result = llm_client.transcribe_audio_via_chat(
                audio, session=session, title="我的影片", api_key="sk-test"
            )
        self.assertEqual(result.text, "")
        self.assertEqual(result.language, "zh")

    def test_sau_stt_env_vars_override_defaults(self) -> None:
        session = _FakeSession({"choices": [{"message": {"content": "hi"}}]})
        with tempfile.TemporaryDirectory() as tmp, _env(
            SAU_STT_BASE_URL="https://stt.example.com",
            SAU_STT_MODEL="mimo-custom",
            SAU_STT_API_KEY="sk-env",
        ):
            audio = _write_wav(tmp)
            result = llm_client.transcribe_audio_via_chat(audio, session=session)
        self.assertEqual(result.text, "hi")
        self.assertEqual(session.calls[0]["url"], "https://stt.example.com/v1/chat/completions")
        self.assertEqual(session.calls[0]["json"]["model"], "mimo-custom")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer sk-env")

    def test_defaults_are_the_verified_gateway_values(self) -> None:
        session = _FakeSession({"choices": [{"message": {"content": "hi"}}]})
        with tempfile.TemporaryDirectory() as tmp, _env(
            SAU_STT_BASE_URL="", SAU_STT_MODEL="", SAU_STT_API_KEY="sk-test-key"
        ):
            audio = _write_wav(tmp)
            # Empty base/model fall back to the non-secret verified defaults.
            llm_client.transcribe_audio_via_chat(audio, session=session)
        self.assertEqual(session.calls[0]["url"], "https://wzw.pp.ua/v1/chat/completions")
        self.assertEqual(session.calls[0]["json"]["model"], "mimo-v2.6-flash")

    def test_missing_api_key_raises(self) -> None:
        session = _FakeSession({"choices": [{"message": {"content": "hi"}}]})
        with tempfile.TemporaryDirectory() as tmp, _env(
            SAU_STT_BASE_URL="", SAU_STT_MODEL="", SAU_STT_API_KEY=""
        ):
            audio = _write_wav(tmp)
            with self.assertRaises(ValueError):
                llm_client.transcribe_audio_via_chat(audio, session=session)

    def test_detect_language_helper(self) -> None:
        self.assertEqual(llm_client.detect_language("hello world"), "en")
        self.assertEqual(llm_client.detect_language("你好世界"), "zh")
        self.assertEqual(llm_client.detect_language("Mixed 中文 text"), "zh")
        self.assertEqual(llm_client.detect_language(""), "en")
        self.assertEqual(llm_client.detect_language(None), "en")

    def test_missing_audio_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                llm_client.transcribe_audio_via_chat(
                    Path(tmp) / "nope.wav", session=_FakeSession({}), api_key="sk-test"
                )


def _profile(settings: dict | None = None) -> profile_registry.Profile:
    return profile_registry.Profile(
        id=1, name="Test", slug="test", settings=settings or {}
    )


class CampaignPrepSttWiringTests(unittest.TestCase):
    """``options.transcribe=true`` must reach the chat-audio STT path."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.video = Path(self.tmp.name) / "clip.mp4"
        self.video.write_bytes(b"not really an mp4")
        self.db_path = Path(self.tmp.name) / "db.db"
        self.monkey = mock.patch.object(
            campaign_media_prep.campaign_store,
            "add_campaign_artifact",
            return_value=None,
        )
        self.add_artifact = self.monkey.start()
        self.addCleanup(self.monkey.stop)

    def _artifact_path(self, campaign_id, source_path, *, artifact_kind, suffix=None, base_dir=None):
        return Path(self.tmp.name) / f"{artifact_kind}{suffix or ''}"

    def _run(self, request_data: dict, settings: dict | None = None) -> dict:
        media_files = [
            {"file_record_id": 7, "role": "video", "file_path": str(self.video)}
        ]
        with mock.patch.object(
            campaign_media_prep.media_pipeline, "extract_video_audio",
            side_effect=lambda src, out, **kwargs: Path(out),
        ), mock.patch.object(
            campaign_media_prep.media_pipeline, "prepare_campaign_artifact_path",
            side_effect=self._artifact_path,
        ), mock.patch.object(
            campaign_media_prep, "_shrink_for_publish",
            side_effect=lambda p, *a, **k: Path(p),
        ), mock.patch.object(
            campaign_media_prep.ops_alerts, "public_app_origin", return_value=""
        ), _env(
            # Never let an exported .env make the tests hit a real endpoint:
            # empty values are falsy, so ``has_ai`` is False and only the
            # explicit ``transcribe=True`` tests reach the (mocked) STT path.
            SAU_LLM_API_KEY="",
            SAU_LLM_API_BASE_URL="",
        ):
            return campaign_media_prep.prepare_campaign_media_artifacts(
                1,
                _profile(settings),
                media_files,
                request_data,
                selected_platforms=set(),
                db_path=self.db_path,
            )

    def test_transcribe_true_uses_chat_stt_and_stores_language(self) -> None:
        stt_result = llm_client.TranscriptionResult(
            text="你好世界", payload={"choices": []}, language="zh"
        )
        with mock.patch.object(
            llm_client, "transcribe_audio_via_chat", return_value=stt_result
        ) as stt:
            context = self._run({"title": "My clip", "transcribe": True})

        stt.assert_called_once()
        self.assertEqual(context["transcriptText"], "你好世界")
        self.assertEqual(context["language"], "zh")
        # The transcript artifact is written with the STT text.
        transcript_path = Path(self.tmp.name) / "transcript.txt"
        self.assertEqual(transcript_path.read_text(encoding="utf-8"), "你好世界")

    def test_supplied_transcript_skips_stt_and_detects_language(self) -> None:
        with mock.patch.object(llm_client, "transcribe_audio_via_chat") as stt:
            context = self._run(
                {"title": "My clip", "transcriptText": "你好，这是字幕", "transcribe": True}
            )
        stt.assert_not_called()
        self.assertEqual(context["transcriptText"], "你好，这是字幕")
        self.assertEqual(context["language"], "zh")

    def test_supplied_language_is_honoured(self) -> None:
        context = self._run(
            {"title": "My clip", "transcriptText": "no cjk here", "language": "zh"}
        )
        self.assertEqual(context["language"], "zh")

    def test_profile_ai_services_supplies_base_url_and_key(self) -> None:
        stt_result = llm_client.TranscriptionResult(text="hi", payload={}, language="en")
        settings = {
            "aiServices": [
                {"apiBaseUrl": "https://profile.example.com", "apiKey": "sk-profile"}
            ]
        }
        with mock.patch.object(
            llm_client, "transcribe_audio_via_chat", return_value=stt_result
        ) as stt, _env(SAU_STT_BASE_URL="", SAU_STT_API_KEY="", SAU_STT_MODEL=""):
            self._run({"title": "My clip", "transcribe": True}, settings)
        self.assertEqual(stt.call_args.kwargs["api_base_url"], "https://profile.example.com")
        self.assertEqual(stt.call_args.kwargs["api_key"], "sk-profile")


if __name__ == "__main__":
    unittest.main()
