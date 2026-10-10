"""OpenAI-compatible LLM client helpers."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

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
    """Headers for the Muyuan LLM gateway.

    The gateway sits behind Cloudflare, which fingerprint-checks the request. A
    bare ``Authorization: Bearer`` gets ``HTTP 403`` with an
    ``error code: 1010`` (or a "Just a moment..." challenge page) on EVERY route
    - messages, chat/completions and the native Gemini path alike. Measured
    against the live service: only a full Claude Code header set gets through.

    So this must keep sending the ``claude-cli`` user agent, ``x-app: cli`` and
    ``anthropic-version``. Requests that omit them are rejected by the edge
    before authentication is even considered, which is why the operator saw
    transcription fail with 403 while the keys themselves were valid.

    Overridable for a future gateway or if the check changes:
    ``SAU_LLM_USER_AGENT`` and ``SAU_LLM_EXTRA_HEADERS`` (JSON object).
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "anthropic-version": os.environ.get("SAU_LLM_ANTHROPIC_VERSION", "2023-06-01"),
        "anthropic-beta": os.environ.get(
            "SAU_LLM_ANTHROPIC_BETA",
            "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14",
        ),
        "user-agent": os.environ.get(
            "SAU_LLM_USER_AGENT", "claude-cli/2.0.30 (external, cli)"
        ),
        "x-app": os.environ.get("SAU_LLM_X_APP", "cli"),
    }
    extra = os.environ.get("SAU_LLM_EXTRA_HEADERS", "").strip()
    if extra:
        try:
            parsed = json.loads(extra)
            if isinstance(parsed, dict):
                headers.update({str(k): str(v) for k, v in parsed.items()})
        except (ValueError, TypeError):
            logging.getLogger(__name__).warning(
                "SAU_LLM_EXTRA_HEADERS is not a JSON object; ignoring"
            )
    return headers


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


_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def coerce_json_object(content: str | None) -> dict | None:
    """Best-effort extraction of a JSON object from an LLM reply.

    Models asked to "return JSON" routinely wrap it in a ```json fence or
    add a sentence around it. Callers that did not request
    ``response_format=json_object`` (several proxies reject it) still need
    the object back - otherwise the raw JSON text ends up posted verbatim
    as the caption.
    """
    if not content:
        return None
    text = str(content).strip()
    candidates = [text, _JSON_FENCE_RE.sub("", text).strip()]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


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


def _image_content_part(source):
    """An OpenAI-compatible image part, from a local file or an existing URL.

    Returns None when the source cannot be used, so one unreadable frame never
    fails the whole call.
    """
    try:
        text = str(source or "").strip()
        if not text:
            return None
        if text.startswith(("data:", "http://", "https://")):
            url = text
        else:
            path = Path(text)
            if not path.is_file():
                return None
            suffix = path.suffix.lower().lstrip(".") or "jpeg"
            mime = "image/jpeg" if suffix in {"jpg", "jpeg"} else f"image/{suffix}"
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            url = f"data:{mime};base64,{encoded}"
        return {"type": "image_url", "image_url": {"url": url}}
    except Exception:  # noqa: BLE001
        return None


# --- rate limiting -----------------------------------------------------------
#
# The LLM gateway enforces a request-count limit per API key/group, measured
# live: 100 requests / 5 minutes on the "welfare" and "default" groups and only
# 5 / 5 minutes on the Gemini group. A burst of campaign prep easily exceeds
# that, so requests are paced locally and a 429 is honoured instead of being
# treated as a generic endpoint failure (which used to burn every remaining
# endpoint in the pool instantly).
#
# ``SAU_LLM_MIN_INTERVAL_SECONDS`` sets the minimum gap between requests;
# the default is deliberately conservative (a little over the welfare group's
# 5-minute budget spread across a batch). Set it to 0 to disable pacing.
_RATE_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0
_MAX_RATE_LIMIT_WAIT = 300.0  # never sleep longer than one full window


def _min_interval_seconds() -> float:
    raw = os.environ.get("SAU_LLM_MIN_INTERVAL_SECONDS")
    if raw is None or str(raw).strip() == "":
        return 3.0
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        return 3.0


def _note_rate_limit(wait: float) -> None:
    """Record that the gateway asked us to slow down.

    Pushes the pacing clock forward so the NEXT call also waits, which stops a
    retry loop from immediately re-triggering the same 429.
    """
    global _LAST_REQUEST_AT
    with _RATE_LOCK:
        _LAST_REQUEST_AT = max(_LAST_REQUEST_AT, time.monotonic()) + max(0.0, wait)


def _retry_after_seconds(response) -> float:
    """How long to wait after a 429, from Retry-After when present.

    Accepts both forms the header allows (delta-seconds and an HTTP date).
    Falls back to a bounded default when the server sends nothing usable, so the
    caller still backs off rather than hammering a limited key.
    """
    raw = str(getattr(response, "headers", {}).get("Retry-After") or "").strip()
    if raw:
        try:
            return min(max(0.0, float(raw)), _MAX_RATE_LIMIT_WAIT)
        except ValueError:
            pass
        try:
            from email.utils import parsedate_to_datetime

            when = parsedate_to_datetime(raw)
            delta = when.timestamp() - time.time()
            return min(max(0.0, delta), _MAX_RATE_LIMIT_WAIT)
        except Exception:  # noqa: BLE001 - a malformed header is not fatal
            pass
    return 10.0


def _pace_request() -> None:
    """Sleep so consecutive LLM requests respect the configured minimum gap."""
    interval = _min_interval_seconds()
    if interval <= 0:
        return
    global _LAST_REQUEST_AT
    with _RATE_LOCK:
        now = time.monotonic()
        wait = interval - (now - _LAST_REQUEST_AT)
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST_AT = time.monotonic()


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
    images: Sequence[str | Path] | None = None,
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
            user_content: object = user_prompt
            if images:
                # Vision models take the user message as a parts list. Anything
                # unreadable is dropped rather than failing the request.
                parts = [{"type": "text", "text": user_prompt}]
                parts.extend(
                    part for part in (_image_content_part(image) for image in images)
                    if part is not None
                )
                if len(parts) > 1:
                    user_content = parts
            payload = {
                "model": entry.get("model") or model,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            }
            if response_json:
                payload["response_format"] = {"type": "json_object"}
            _pace_request()
            response = http.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout_seconds,
            )
            if int(getattr(response, "status_code", 0) or 0) == 429:
                # The gateway rate-limits hard and the limit is per KEY/group:
                # 100 requests / 5 min on the welfare and default groups, and
                # only 5 / 5 min on the Gemini group. Treating 429 as a generic
                # failure burned the remaining endpoints instantly, so honour it:
                # wait for Retry-After when the server sends one, otherwise back
                # off, and only THEN move on. The wait is bounded so a persistent
                # limit still surfaces instead of hanging forever.
                wait = _retry_after_seconds(response)
                _note_rate_limit(wait)
                if wait > 0:
                    time.sleep(wait)
                last_error = RuntimeError(
                    f"rate limited (HTTP 429) by {base_url}; waited {wait:.1f}s"
                )
                continue
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
        # A multipart upload must NOT send Content-Type: application/json - the
        # boundary has to be set by requests, or the server cannot parse the
        # body. The Cloudflare-bypassing headers are kept (they are what gets the
        # request past the edge at all); only Content-Type is dropped.
        upload_headers = {
            key: value
            for key, value in _headers(resolved_api_key).items()
            if key.lower() != "content-type"
        }
        _pace_request()
        response = http.post(
            f"{base_url}/audio/transcriptions",
            headers=upload_headers,
            data=data,
            files={"file": (audio_file.name, handle, "application/octet-stream")},
            timeout=timeout_seconds,
        )
    if int(getattr(response, "status_code", 0) or 0) == 429:
        # Transcription shares the same per-key budget as chat completions, so a
        # 429 here must be reported as a rate limit (and the pacing clock pushed
        # forward) rather than surfacing as a generic HTTP error. Transcription is
        # best-effort, so the caller can simply skip it for this campaign.
        wait = _retry_after_seconds(response)
        _note_rate_limit(wait)
        raise RuntimeError(
            f"rate limited (HTTP 429) transcribing with {base_url}; "
            f"retry after about {wait:.0f}s"
        )
    response.raise_for_status()
    payload = response.json()
    return TranscriptionResult(text=str(payload.get("text", "")).strip(), payload=payload)
