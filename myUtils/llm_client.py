"""OpenAI-compatible LLM client helpers."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - environment-specific
    requests = None


DEFAULT_CHAT_MODEL = "gpt-4.1-mini"
DEFAULT_TRANSCRIPTION_MODEL = "whisper-1"
DEFAULT_BASE_URL_ENV = "SAU_LLM_API_BASE_URL"
DEFAULT_API_KEY_ENV = "SAU_LLM_API_KEY"


@dataclass(frozen=True, slots=True)
class ChatCompletionResult:
    content: str
    payload: dict
    parsed_json: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    text: str
    payload: dict

    def to_dict(self) -> dict:
        return asdict(self)


def _normalise_api_base_url(api_base_url: str | None) -> str:
    base = (api_base_url or os.environ.get(DEFAULT_BASE_URL_ENV, "")).strip().rstrip("/")
    if not base:
        raise ValueError("No LLM API base URL configured")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


def _resolve_api_key(api_key: str | None) -> str:
    resolved = (api_key or os.environ.get(DEFAULT_API_KEY_ENV, "")).strip()
    if not resolved:
        raise ValueError("No LLM API key configured")
    return resolved


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
    }


def _extract_message_content(message_content) -> str:
    if isinstance(message_content, str):
        return message_content
    if isinstance(message_content, list):
        text_parts = []
        for item in message_content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(str(item.get("text", "")))
        return "".join(text_parts)
    return str(message_content or "")


def generate_chat_completion(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str = DEFAULT_CHAT_MODEL,
    temperature: float = 0.3,
    response_json: bool = True,
    api_base_url: str | None = None,
    api_key: str | None = None,
    session=None,
    timeout_seconds: int = 120,
) -> ChatCompletionResult:
    base_url = _normalise_api_base_url(api_base_url)
    resolved_api_key = _resolve_api_key(api_key)
    payload = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if response_json:
        payload["response_format"] = {"type": "json_object"}

    if session is None:
        if requests is None:
            raise RuntimeError("requests is required for chat completions")
        http = requests.Session()
    else:
        http = session
    response = http.post(
        f"{base_url}/chat/completions",
        headers={
            **_headers(resolved_api_key),
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    response_payload = response.json()
    content = _extract_message_content(
        response_payload.get("choices", [{}])[0].get("message", {}).get("content", "")
    ).strip()
    parsed_json = None
    if response_json and content:
        parsed_json = json.loads(content)
    return ChatCompletionResult(
        content=content,
        payload=response_payload,
        parsed_json=parsed_json,
    )


def transcribe_audio(
    audio_path: str | Path,
    *,
    model: str = DEFAULT_TRANSCRIPTION_MODEL,
    prompt: str | None = None,
    language: str | None = None,
    api_base_url: str | None = None,
    api_key: str | None = None,
    session=None,
    timeout_seconds: int = 600,
) -> TranscriptionResult:
    base_url = _normalise_api_base_url(api_base_url)
    resolved_api_key = _resolve_api_key(api_key)
    audio_file = Path(audio_path).expanduser().resolve()
    if session is None:
        if requests is None:
            raise RuntimeError("requests is required for audio transcription")
        http = requests.Session()
    else:
        http = session
    data = {
        "model": model,
    }
    if prompt:
        data["prompt"] = prompt
    if language:
        data["language"] = language

    with audio_file.open("rb") as handle:
        response = http.post(
            f"{base_url}/audio/transcriptions",
            headers=_headers(resolved_api_key),
            data=data,
            files={"file": (audio_file.name, handle)},
            timeout=timeout_seconds,
        )
    response.raise_for_status()
    payload = response.json()
    return TranscriptionResult(text=str(payload.get("text", "")).strip(), payload=payload)
