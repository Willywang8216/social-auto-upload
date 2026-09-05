"""OpenAI-compatible LLM client helpers."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - environment-specific
    requests = None


logger = logging.getLogger(__name__)

DEFAULT_CHAT_MODEL = os.environ.get("SAU_LLM_MODEL", "gpt-4.1-mini")
DEFAULT_TRANSCRIPTION_MODEL = "whisper-1"
DEFAULT_BASE_URL_ENV = "SAU_LLM_API_BASE_URL"
DEFAULT_API_KEY_ENV = "SAU_LLM_API_KEY"
# JSON array of endpoints for rotation, e.g.
#   [{"base_url": "...", "api_key": "...", "model": "...", "headers": {...}}]
# When unset/empty the client falls back to the single DEFAULT_* env vars, so
# existing single-endpoint deployments behave exactly as before.
POOL_ENV = "SAU_LLM_POOL"


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


def _load_pool() -> list[dict]:
    """Return the endpoint rotation pool.

    Prefers the ``SAU_LLM_POOL`` env var (a JSON array of
    ``{"base_url","api_key","model"?,"headers"?}`` entries). Falls back to a
    single-entry pool built from the legacy ``SAU_LLM_*`` env vars so existing
    deployments keep working unchanged.
    """
    raw = os.environ.get(POOL_ENV, "").strip()
    entries: list[dict] = []
    if raw:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("%s is not valid JSON; ignoring the pool", POOL_ENV)
            data = None
        if isinstance(data, dict):
            data = [data]
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("base_url") and item.get("api_key"):
                    entries.append(
                        {
                            "base_url": str(item["base_url"]).strip(),
                            "api_key": str(item["api_key"]).strip(),
                            "model": (str(item["model"]).strip() if item.get("model") else None),
                            "headers": item.get("headers") if isinstance(item.get("headers"), dict) else None,
                        }
                    )
    if entries:
        return entries

    base = os.environ.get(DEFAULT_BASE_URL_ENV, "").strip()
    key = os.environ.get(DEFAULT_API_KEY_ENV, "").strip()
    if base and key:
        return [
            {
                "base_url": base,
                "api_key": key,
                "model": os.environ.get("SAU_LLM_MODEL", "").strip() or None,
                "headers": None,
            }
        ]
    return []


def _resolve_endpoints(api_base_url: str | None, api_key: str | None, model: str) -> list[dict]:
    # An explicit endpoint (e.g. injected by a caller or test) is used as-is and
    # never rotated — this preserves the original single-shot contract.
    if api_base_url or api_key:
        return [{"base_url": api_base_url, "api_key": api_key, "model": model, "headers": None}]
    pool = _load_pool()
    if pool:
        return pool
    # No configuration anywhere: keep one entry so _normalise raises the
    # canonical "No LLM API base URL configured" error.
    return [{"base_url": None, "api_key": None, "model": model, "headers": None}]


def generate_chat_completion(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str = DEFAULT_CHAT_MODEL,
    temperature: float = 0.3,
    response_json: bool = False,
    api_base_url: str | None = None,
    api_key: str | None = None,
    session=None,
    timeout_seconds: int = 120,
) -> ChatCompletionResult:
    endpoints = _resolve_endpoints(api_base_url, api_key, model)

    if session is None:
        if requests is None:
            raise RuntimeError("requests is required for chat completions")
        http = requests.Session()
    else:
        http = session

    last_error: Exception | None = None
    total = len(endpoints)
    for index, entry in enumerate(endpoints):
        try:
            base_url = _normalise_api_base_url(entry.get("base_url"))
            resolved_api_key = _resolve_api_key(entry.get("api_key"))
            headers = {**_headers(resolved_api_key), "Content-Type": "application/json"}
            extra_headers = entry.get("headers")
            if isinstance(extra_headers, dict):
                headers.update(extra_headers)
            payload = {
                "model": entry.get("model") or model,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            if response_json:
                payload["response_format"] = {"type": "json_object"}
            response = http.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            response_payload = response.json()
            content = _extract_message_content(
                response_payload.get("choices", [{}])[0].get("message", {}).get("content", "")
            ).strip()
            parsed_json = json.loads(content) if (response_json and content) else None
            return ChatCompletionResult(
                content=content,
                payload=response_payload,
                parsed_json=parsed_json,
            )
        except Exception as exc:  # noqa: BLE001 - rotate to the next endpoint on any failure
            last_error = exc
            # Log the failure without leaking the API key (base_url only).
            logger.warning(
                "LLM endpoint %d/%d (%s) failed: %s: %s",
                index + 1,
                total,
                entry.get("base_url") or "<unset>",
                type(exc).__name__,
                str(exc)[:200],
            )
            continue

    assert last_error is not None  # loop always runs at least once
    raise last_error


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
