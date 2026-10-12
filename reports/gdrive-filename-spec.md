# Feature spec — Drive filename convention (title / rating / date), transcript + STT, language routing

Status: approved-for-implementation by the operator (2026-10-12).
Coordinator: `verify-publish-e2e`. Read this before editing.

## 1. New filename convention (primary)

```
<Title with spaces>_<SFW|NSFW>_<YYYYMMDD>.<ext>
```

* **Title** = everything before the final two `_`-separated tokens. May contain
  spaces. Used as the human title / caption seed.
* **Flag** = `SFW` or `NSFW` (case-insensitive).
* **Date** = `YYYYMMDD` (optional). The intended publish **date**, local
  `Asia/Taipei`. If absent, schedule normally from now.
* Examples:
  * `My great title space separated_NSFW_20261015.mp4`
  * `Pride invitation_SFW_20261024.mp4`
  * `News update_NSFW.mp4` (no date)
* **Back-compat:** the existing `<topic?>__<sfw|nsfw>` form must keep working.

## 2. Optional transcript

A same-name sibling note in the same Drive folder is the transcript / copy source:
`My great title space separated_NSFW_20261015.txt` (also accept `.md` and
`<stem>.content.txt`). If absent, the **backend** runs STT on the video audio
(there is no requirement for an agent to transcribe).

## 3. STT engine (verified live 2026-10-12)

* Base `https://wzw.pp.ua`, model **`mimo-v2.6-flash`**. The API key is **not**
  in this repo (it is public): it lives in the gitignored `.env` as
  `SAU_STT_API_KEY` (and the container reads `.env` via `env_file`).
* The gateway **does not** support `/v1/audio/transcriptions` for this model
  (`bad_response_status_code`); `whisper-*` does not exist. Use
  **`/v1/chat/completions`** with an OpenAI `input_audio` content part:
  ```json
  {"model":"mimo-v2.6-flash","messages":[{"role":"user","content":[
    {"type":"input_audio","input_audio":{"data":"<base64-wav>","format":"wav"}},
    {"type":"text","text":"Transcribe the audio exactly as spoken, in its original language. Then on a new line write LANGUAGE=zh or LANGUAGE=en."}]}]}
  ```
* **Cloudflare:** a bare Bearer gets `403 error code: 1010`. Reuse
  `myUtils/llm_client._headers()` (claude-cli UA, `x-app: cli`,
  `anthropic-version`/`anthropic-beta`). This is why the old transcription
  path 403'd.
* **Transcript location:** the model often returns the transcript in
  `choices[0].message.reasoning_content`, sometimes in `.content`. Read
  `content or reasoning_content`.
* Audio prep: `ffmpeg -vn -ac 1 -ar 16000 -c:a pcm_s16le out.wav`. Keep chunks
  small (base64 inflates ~33%); for long clips transcode to a compressed format
  or chunk. `mimo-v2.5` is rejected as "high risk" — use `mimo-v2.6-flash`.

## 4. Language (zh / en)

Detect from the transcript (CJK codepoints ⇒ `zh`) else `en`; fall back to the
title. Store it on the ready entry. The existing per-account
`audience_language` routing already generates copy in each account's language;
the detected language seeds that (and the transcript).

## 5. Scheduling

`publishDate` becomes `schedule.startAt = "<YYYY-MM-DDT<HH>:00:00>"` (naive
local, Asia/Taipei), default hour 21:00 unless the operator sets one. The
existing `_next_free_slot` allocator handles collisions (≥30 min gap, ≤3/day).

## 6. Ready-entry contract (state.json)

Add to each `ready` item:
```
"title": "<Title with spaces>",     # alias of topic; keep topic for compat
"sfwFlag": "sfw"|"nsfw",
"publishDate": "YYYY-MM-DD"|null,
"language": "zh"|"en"|null,
"transcript": "<text>"|null,        # sibling note if present, else null (backend STT)
"contentNote": "<text>"|null        # existing behaviour
```

## 7. File ownership (no overlaps)

| subagent | owns |
|---|---|
| A (`gdn-watcher`) | `/home/will/sau-inbox/watch.py` + `/home/will/sau-inbox/tests/` — parser (title/flag/date), ready fields, language-from-note |
| B (`gdn-stt`) | `myUtils/llm_client.py`, `myUtils/campaign_media_prep.py`, new tests — chat-audio STT + language detection + wire into prep |
| C (`gdn-docs`) | `skills/sau-bulk-videos/SKILL.md` + `docs/` — convention, publishDate→schedule, language routing |
| D (`gdn-verify`) | end-to-end verification after A–C; no source edits |

Rules: no subagent touches another's files. No live publishes. No DB campaign
writes. Tests must not require network (mock the STT session); D may make one
live STT call on a short clip to prove the endpoint.
