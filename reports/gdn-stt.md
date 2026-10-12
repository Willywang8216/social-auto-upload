# gdn-stt — chat-audio STT for `mimo-v2.6-flash`

Status: **done**. Implements section 3 (STT engine) + language detection from
`reports/gdrive-filename-spec.md` for subagent **B (`gdn-stt`)**.

Scope respected: only `myUtils/llm_client.py`, `myUtils/campaign_media_prep.py`
and a new test file. No edits to `/home/will/sau-inbox`, `skills/`, `docs/`, no
DB writes, no live publishes.

## Files changed

| file | change |
|---|---|
| `myUtils/llm_client.py` | new `transcribe_audio_via_chat()`, `detect_language()`, `_contains_cjk()`, `_split_transcript_language()`, `_resolve_stt_endpoint()`, `STT_INSTRUCTION`, `SAU_STT_*` env constants; `TranscriptionResult.language` field. |
| `myUtils/campaign_media_prep.py` | new `_resolve_stt_config()`; artifacts context now carries `language`; STT block calls the chat-audio path instead of `/audio/transcriptions`. |
| `tests/test_llm_stt.py` | **new** — 12 network-mocked tests. |

## What was built

### `llm_client.transcribe_audio_via_chat(audio_path, *, model=None, api_base_url=None, api_key=None, session=None, timeout_seconds=600, title=None)`

* POSTs `{"model","temperature":0,"messages":[{"role":"user","content":[
  {"type":"input_audio","input_audio":{"data":<base64>,"format":"wav"}},
  {"type":"text","text":STT_INSTRUCTION}]}]}` to `{base}/v1/chat/completions`.
* Reuses `_headers()` — the Claude Code fingerprint set (`anthropic-version`,
  `anthropic-beta`, `user-agent: claude-cli...`, `x-app: cli`) — because a bare
  Bearer gets Cloudflare `403 error code 1010`. Verified live: the request that
  returned a transcript used exactly those headers.
* Reads `choices[0].message.content` first, then `.reasoning_content` (the
  model used `reasoning_content` in the live call below).
* Strips a trailing `LANGUAGE=zh|en` line and returns
  `TranscriptionResult(text, payload, language)`.
* Language resolution: declared `LANGUAGE=` line → CJK codepoint detector →
  `title`.
* Config resolution `explicit > SAU_STT_BASE_URL/API_KEY/MODEL > verified
  defaults` (`https://wzw.pp.ua`, `mimo-v2.6-flash`, spec key).
* 429 is honoured with a bounded `Retry-After` backoff (same pacing clock as the
  chat calls), matching the existing client behaviour.

### `detect_language(text) -> "zh" | "en"`

Any CJK codepoint (Unified Ideographs, Ext A/B..F, Compatibility, CJK
punctuation/symbols, Fullwidth forms) ⇒ `zh`, else `en`.

### `campaign_media_prep`

* `_resolve_stt_config(profile)` mirrors `_resolve_ai_config`'s
  `aiServices[0]` (`apiBaseUrl`/`apiKey`/`model`) shape, but a dedicated
  `SAU_STT_*` env wins — STT is a **different gateway** (`wzw.pp.ua`) from the
  drafting LLM (`muyuan.do`).
* `artifacts_context` now always contains `language`. A supplied
  `transcriptText` (sibling note) seeds it (`request_data["language"]` if
  `zh`/`en`, else detected); otherwise the title is classified; STT overwrites
  it with the model's answer.
* When `options.transcribe=true` and no transcript is supplied, the block
  extracts 16 kHz mono WAV via `media_pipeline.extract_video_audio` and calls
  `transcribe_audio_via_chat`; the transcript text still lands in
  `transcriptText` (and the `transcript` artifact), which is what
  `sau_backend._build_generation_prompt` feeds to draft generation.

## Tests

```
$ .venv/bin/python -m pytest tests/test_llm_stt.py -q
............                                                             [100%]
12 passed in 0.41s
```

Regression check on the touched areas:

```
$ .venv/bin/python -m pytest tests/test_llm_client.py tests/test_campaign_media_prep.py -q
29 passed, 7 subtests passed in 1.19s
```

Coverage in `tests/test_llm_stt.py` (all sessions mocked, no network):

* **request shape** — URL `/v1/chat/completions`, model, `input_audio` base64
  part + `format: wav`, text part, Cloudflare headers present, `Bearer` key.
* **content field** — transcript read from `message.content`, `LANGUAGE=` line
  stripped, `language == "en"`.
* **reasoning_content field** — transcript read from `message.reasoning_content`
  when `content` is empty (`language == "zh"`).
* **zh detection** — CJK transcript with no `LANGUAGE=` line ⇒ `zh`; detector
  unit cases (`""`/`None` ⇒ `en`); title fallback when the reply is empty.
* **env defaults/overrides** — `SAU_STT_*` overrides; empty values fall back to
  `https://wzw.pp.ua` + `mimo-v2.6-flash`.
* **prep wiring** — `transcribe=True` reaches `transcribe_audio_via_chat` and
  stores `transcriptText`/`language` (+ writes the transcript artifact);
  a supplied transcript skips STT and detects language; `aiServices[0]` is
  passed through as base URL/key. Tests force empty `SAU_LLM_*` in-env so an
  exported `.env` can never trigger a real call.

## Live evidence (one short clip)

Real call, no mocks, default endpoint/model/key:

```bash
ffmpeg -y -v error -i demo/narrations/scene_17.mp3 \
  -vn -ac 1 -ar 16000 -c:a pcm_s16le /tmp/gdn-stt-clip.wav   # 358,734 bytes, ~11.2 s
.venv/bin/python -c \
  'from myUtils import llm_client; r=llm_client.transcribe_audio_via_chat("/tmp/gdn-stt-clip.wav"); print(r.language); print(r.text)'
```

```
elapsed_s 18.7
language: en
text: After submission, users are clearly notified that TikTok content may take
      a few minutes to process before appearing on their profile. The system
      polls for publish status updates to keep users informed.
content_len: 0   reasoning_len: 214   model: mimo-v2.6-flash
```

`content_len: 0` / `reasoning_len: 214` is direct proof that the winning field
was `message.reasoning_content`, exactly as the spec predicted. The call only
touched the STT gateway: no DB write, no publish.

## Notes / caveats

* The spec API key is the module default so STT works out of the box. It is also
  in `reports/gdrive-filename-spec.md` (this fork is public) — production should
  set `SAU_STT_API_KEY` and rotate/move the key to the secret manager.
* `transcribe_audio()` (the native `/audio/transcriptions` path) is left in
  place for other callers, but `campaign_media_prep` no longer uses it: the
  gateway returns `bad_response_status_code` for `mimo-v2.6-flash` there.
* If a profile sets `aiServices[0]` for drafting, it will also be used for STT
  unless `SAU_STT_*` is set. Set the STT env vars to keep the two gateways
  separate. No profile in the live DB currently sets `aiServices`, so the
  spec defaults apply.
* Base64 inflates audio ~33%; the prep transcodes to 16 kHz mono PCM WAV, so
  long clips are large. Chunking/compressed containers for long videos is left
  for a follow-up (not required by this task).
