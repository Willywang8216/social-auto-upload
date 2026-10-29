#!/usr/bin/env python3
"""A local, always-available STT + language detector for the SAU pipeline.

Why this exists
---------------
The hosted gateway (``wzw.pp.ua`` / ``mimo-v2.6-flash``) went away on
2026-10-27 and every new upload since then got a content-free brief — and, far
worse, no language at all, because the pipeline classified content language from
the *transcript*. With no transcript it fell back to the filename or the
LLM-written title, so English videos were routed to the Chinese accounts. The
operator reported it three times; the queue had ~1 900 misrouted targets.

What it does
------------
Runs faster-whisper (CTranslate2, int8, CPU) on the host and speaks the two
shapes the app already knows:

  * ``POST /v1/chat/completions`` with an OpenAI ``input_audio`` part — the shape
    ``llm_client.transcribe_audio_via_chat`` sends. The reply body carries the
    transcript plus a ``LANGUAGE=xx`` line, exactly like the hosted model.
  * ``POST /v1/audio/transcriptions`` (multipart) — the native OpenAI shape.

Language detection is the part that matters, so it is done properly rather than
from a single 30 s window: several windows are sampled across the *whole* video
(the DeerOdds clip is 93 minutes long and its first 30 seconds are silence),
windows without speech are dropped, and the confident ones vote. A result that
no window agrees on comes back as ``language: null`` — "unknown" must never be
silently treated as "English".

Results are cached by content hash, so a re-prep or a second campaign over the
same media costs nothing.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOGGER = logging.getLogger("local-stt")

MODEL_SIZE = os.environ.get("SAU_STT_LOCAL_MODEL", "tiny")
DEVICE = os.environ.get("SAU_STT_LOCAL_DEVICE", "cpu")
COMPUTE_TYPE = os.environ.get("SAU_STT_LOCAL_COMPUTE", "int8")
CPU_THREADS = int(os.environ.get("SAU_STT_LOCAL_THREADS", "3"))
PORT = int(os.environ.get("SAU_STT_LOCAL_PORT", "9099"))
TOKEN = os.environ.get("SAU_STT_LOCAL_TOKEN", "").strip()
CACHE_DIR = Path(os.environ.get("SAU_STT_LOCAL_CACHE", "/home/will/sau-work/stt-cache"))

#: How many windows to sample for language detection, how long each is, and the
#: confidence a window needs before its vote counts.
LANG_WINDOWS = int(os.environ.get("SAU_STT_LOCAL_WINDOWS", "5"))
LANG_WINDOW_SECONDS = float(os.environ.get("SAU_STT_LOCAL_WINDOW_SECONDS", "25"))
LANG_MIN_PROBABILITY = float(os.environ.get("SAU_STT_LOCAL_MIN_PROB", "0.60"))
#: Cap the transcript so a 93-minute upload cannot occupy the box for an hour.
TRANSCRIBE_SECONDS = float(os.environ.get("SAU_STT_LOCAL_TRANSCRIBE_SECONDS", "240"))

_MODEL = None
_MODEL_LOCK = threading.Lock()
_INFER_LOCK = threading.Lock()


def _model():
    global _MODEL
    with _MODEL_LOCK:
        if _MODEL is None:
            from faster_whisper import WhisperModel

            started = time.time()
            _MODEL = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE,
                                  cpu_threads=CPU_THREADS)
            LOGGER.info("loaded whisper-%s in %.1fs", MODEL_SIZE, time.time() - started)
        return _MODEL


def _read_wav(path: Path):
    """Mono 16 kHz int16 WAV -> float32 in [-1, 1]."""
    import numpy as np

    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise ValueError("expected 16-bit PCM")
        frames = handle.readframes(handle.getnframes())
        rate = handle.getframerate()
        channels = handle.getnchannels()
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != 16000:
        raise ValueError(f"expected 16 kHz, got {rate}")
    return audio


def _windows(total: int, count: int, length: int) -> list[tuple[int, int]]:
    """``count`` windows of ``length`` samples spread across ``total``."""
    if total <= length:
        return [(0, total)]
    spans = []
    for index in range(count):
        # 5 %, 25 %, 45 %, 65 %, 85 % of the way through the audio
        fraction = (index + 0.5) / count
        start = int(total * fraction) - length // 2
        start = max(0, min(start, total - length))
        spans.append((start, start + length))
    return spans


def _detect_language(audio) -> tuple[str | None, float, list[dict]]:
    """Vote across sampled windows. Returns (language|None, confidence, votes)."""
    model = _model()
    rate = 16000
    length = int(LANG_WINDOW_SECONDS * rate)
    votes: list[dict] = []
    for start, end in _windows(len(audio), LANG_WINDOWS, length):
        chunk = audio[start:end]
        if chunk.size < rate:  # under a second of audio is not evidence
            continue
        try:
            language, probability, _all = model.detect_language(chunk)
        except Exception as exc:  # noqa: BLE001 - one bad window is not fatal
            LOGGER.warning("detect_language failed on window %s: %s", start, exc)
            continue
        # Confirm there is actually speech here: a music-only or silent window
        # gave a confident-but-meaningless answer for the DeerOdds clip.
        speech = 0.0
        try:
            _segments, info = model.transcribe(
                chunk, beam_size=1, vad_filter=True, language=language,
                condition_on_previous_text=False,
            )
            speech = float(getattr(info, "duration_after_vad", 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            speech = float(chunk.size) / rate
        votes.append({
            "at": round(start / rate, 1),
            "language": language,
            "probability": round(float(probability), 3),
            "speech_seconds": round(speech, 1),
        })
    usable = [v for v in votes
              if v["probability"] >= LANG_MIN_PROBABILITY and v["speech_seconds"] >= 2.0]
    if not usable:
        return None, 0.0, votes
    tally: dict[str, list[float]] = {}
    for vote in usable:
        tally.setdefault(vote["language"], []).append(vote["probability"])
    winner = max(tally, key=lambda lang: (len(tally[lang]), sum(tally[lang])))
    # A tie between two languages is not a decision.
    counts = sorted((len(v) for v in tally.values()), reverse=True)
    if len(counts) > 1 and counts[0] == counts[1]:
        return None, 0.0, votes
    confidence = sum(tally[winner]) / len(tally[winner])
    return winner, confidence, votes


def _transcribe(audio, language: str | None) -> str:
    model = _model()
    rate = 16000
    limit = int(TRANSCRIBE_SECONDS * rate)
    clip = audio[:limit] if audio.size > limit else audio
    with _INFER_LOCK:
        segments, _info = model.transcribe(
            clip, beam_size=1, vad_filter=True, language=language,
            condition_on_previous_text=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()


def _cache_key(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


def analyse(wav_path: Path, *, language_only: bool = False) -> dict:
    data = wav_path.read_bytes()
    key = _cache_key(data)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / f"{key}{'-lang' if language_only else ''}.json"
    if cached.is_file():
        try:
            payload = json.loads(cached.read_text(encoding="utf-8"))
            payload["cached"] = True
            return payload
        except (OSError, ValueError):
            pass
    started = time.time()
    audio = _read_wav(wav_path)
    duration = len(audio) / 16000
    language, confidence, votes = _detect_language(audio)
    # ``purpose=language`` skips the transcript. Language detection is ~5 s
    # (five short windows); transcribing the same probe is ~35 s, and the queue
    # sweep only needs to know which language the video is in.
    text = "" if language_only else _transcribe(audio, language)
    payload = {
        "language": language,
        "language_confidence": round(confidence, 3),
        "language_votes": votes,
        "duration_seconds": round(duration, 1),
        "text": text,
        "model": MODEL_SIZE,
        "cached": False,
        "elapsed_seconds": round(time.time() - started, 1),
    }
    try:
        tmp = cached.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(cached)
    except OSError as exc:  # noqa: BLE001 - caching is best-effort
        LOGGER.warning("could not cache %s: %s", key, exc)
    return payload


_LANGUAGE_LINE = re.compile(r"^\s*LANGUAGE\s*=\s*([a-zA-Z-]+)\s*$", re.MULTILINE)


def _parse_multipart(body: bytes, content_type: str) -> dict[str, bytes]:
    match = re.search(r"boundary=([^;]+)", content_type or "")
    if not match:
        raise ValueError("no multipart boundary")
    boundary = match.group(1).strip().strip('"').encode()
    parts: dict[str, bytes] = {}
    for chunk in body.split(b"--" + boundary):
        if not chunk.strip() or chunk.strip() == b"--":
            continue
        head, _, payload = chunk.partition(b"\r\n\r\n")
        if not _:
            continue
        name = re.search(rb'name="([^"]+)"', head)
        if not name:
            continue
        parts[name.group(1).decode()] = payload.rstrip(b"\r\n")
    return parts


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 - quieter access log
        LOGGER.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        if not TOKEN:
            return True
        header = self.headers.get("Authorization") or ""
        return header.strip() in {f"Bearer {TOKEN}", TOKEN} or self.headers.get("x-api-key") == TOKEN

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") in ("/healthz", "/v1/models"):
            if self.path.rstrip("/") == "/v1/models":
                self._send(200, {"data": [{"id": f"local-whisper-{MODEL_SIZE}", "object": "model"}]})
            else:
                self._send(200, {"status": "ok", "model": MODEL_SIZE})
            return
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):  # noqa: N802
        if not self._authorised():
            self._send(401, {"error": {"message": "unauthorized"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        path = self.path.split("?")[0].rstrip("/")
        language_only = "purpose=language" in (self.path.split("?", 1)[1] if "?" in self.path else "")
        try:
            if path.endswith("/chat/completions"):
                payload = json.loads(raw.decode("utf-8"))
                content = ((payload.get("messages") or [{}])[0].get("content") or [])
                encoded = ""
                for part in content if isinstance(content, list) else []:
                    if isinstance(part, dict) and part.get("type") == "input_audio":
                        encoded = str((part.get("input_audio") or {}).get("data") or "")
                        break
                if not encoded:
                    self._send(400, {"error": {"message": "no input_audio part"}})
                    return
                import base64

                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                    handle.write(base64.b64decode(encoded))
                    temp = Path(handle.name)
                try:
                    result = analyse(temp, language_only=language_only)
                finally:
                    temp.unlink(missing_ok=True)
                # Match the hosted model's contract: transcript, then LANGUAGE=.
                declared = result["language"] or "unknown"
                body = f"{result['text']}\nLANGUAGE={declared}"
                self._send(200, {
                    "id": "chatcmpl-local",
                    "object": "chat.completion",
                    "model": f"local-whisper-{MODEL_SIZE}",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": body}}],
                    "usage": {},
                })
                return
            if path.endswith("/audio/transcriptions"):
                parts = _parse_multipart(raw, self.headers.get("Content-Type") or "")
                audio = parts.get("file")
                if not audio:
                    self._send(400, {"error": {"message": "no file part"}})
                    return
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                    handle.write(audio)
                    temp = Path(handle.name)
                try:
                    result = analyse(temp, language_only=language_only)
                finally:
                    temp.unlink(missing_ok=True)
                self._send(200, result)
                return
            self._send(404, {"error": {"message": f"unknown path {path}"}})
        except Exception as exc:  # noqa: BLE001 - never take the service down
            LOGGER.exception("request failed")
            self._send(500, {"error": {"message": f"{type(exc).__name__}: {exc}"}})


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _model()  # warm the model so the first request is not slow
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    LOGGER.info("local STT listening on 0.0.0.0:%d (whisper-%s, %d threads)",
                PORT, MODEL_SIZE, CPU_THREADS)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
