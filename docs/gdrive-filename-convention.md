# Drive inbox filename convention, transcript/STT, and language routing

Source of truth: [`reports/gdrive-filename-spec.md`](../reports/gdrive-filename-spec.md)
(feature spec, approved-for-implementation by the operator 2026-10-12).
This page is the operator- and agent-facing summary; when the spec and this page
disagree, the spec wins.

Two audiences read this file:

* **the operator** — what to name a file and where to drop it;
* **the submitting agent** — how a name becomes `schedule.startAt`, which
  accounts receive the clip, and where the transcript comes from.

---

## 1. The filename convention

### Primary (new) form

```
<Title with spaces>_<SFW|NSFW>_<YYYYMMDD>.<ext>
```

| field | rule |
|---|---|
| **Title** | everything before the final two `_`-separated tokens. **May contain spaces.** Becomes the human title / caption seed. |
| **Flag** | `SFW` or `NSFW` (case-insensitive). |
| **Date** | `YYYYMMDD`, **optional**. The intended publish date in **local `Asia/Taipei`**. If absent, schedule normally from now. |

Examples (spec §1):

```
My great title space separated_NSFW_20261015.mp4
Pride invitation_SFW_20261024.mp4
News update_NSFW.mp4            # no date
```

The **brand/persona is the folder**, not a filename token — see §2.

### Legacy form (must keep working)

```
<topic?>__<sfw|nsfw>.<ext>
```

This is the form documented in
[`/home/will/sau-inbox/README.md`](file:///home/will/sau-inbox/README.md) and
`00-先讀我-README.txt`; the watcher parser currently implements it
(`watch.py: parse_name`). The new form is **additive**: files that use the old
`__` separator must still parse. Per spec §1 "Back-compat: the existing
`<topic?>__<sfw|nsfw>` form must keep working."

Notes carried over from the legacy rules:

* `<topic>` is optional — an empty topic means "auto-title me".
* video/img is decided by the **extension**, not a token:
  `.mp4 .mov .webm` = video, `.jpg .png .webp` = image.
* The `sfw`/`nsfw` flag is mandatory — without it the file is `pending` and the
  operator is notified on Telegram.

---

## 2. Where files go — the `SAU-Inbox/<brand>/video/` layout

Drive root is **`SAU-Inbox`** (verified live:
`rclone lsf GDrive-willywang8216:SAU-Inbox/` lists `00-先讀我-README.txt`,
`both/`, `msl/`, `nw/`, `sw/`, `teaching/`). Each brand folder mirrors the same
tree:

```
SAU-Inbox/
├── nw/        Nakedwill (profile 1)
├── sw/        Sexualwill (profile 3)
├── both/      Nakedwill + Sexualwill in one submission (profileIds [1, 3])
├── teaching/  Teaching (profile 4)
└── msl/       Money Systems Lab (profile 10)
    └── video/   ← put clips here
        img/     ← still images / covers
        thumb/   ← (optional) operator-chosen safe thumbnail
```

The host mirror is `/home/will/sau-inbox/{nw,sw,teaching,msl,both}/{video,img,thumb}/`
(spec §1 + inbox README "資料夾"). The brand folder is what selects the persona;
the filename only carries **rating** and (optionally) **date**. The legacy prefix
rule in `skills/sau-bulk-videos/SKILL.md` (`SFW NW`, `NSFW NW+SW`, `Teaching`, …)
is the equivalent for files that arrive with the persona in the name instead of
the folder — both routes end at the same `profileIds`.

---

## 3. Optional transcript

A **same-name sibling** note in the *same* Drive folder is the transcript / copy
source (spec §2). Accepted names:

```
My great title space separated_NSFW_20261015.txt           # stem.txt
My great title space separated_NSFW_20261015.md            # stem.md
My great title space separated_NSFW_20261015.content.txt   # stem.content.txt
```

* If the note exists, its text becomes the ready entry's `transcript` and seeds
  the copy.
* If it is **absent**, the **backend runs STT on the video audio**. Per spec §2:
  "there is no requirement for an agent to transcribe." A submitting agent should
  submit with transcription enabled and let the server do it.

---

## 4. STT fallback (server-side)

When no transcript note is present the backend transcribes the audio itself.
Verified live **2026-10-12** (spec §3):

| item | value |
|---|---|
| base URL | `https://wzw.pp.ua` |
| model | **`mimo-v2.6-flash`** |
| endpoint | **`/v1/chat/completions`** with an OpenAI `input_audio` content part — **not** `/v1/audio/transcriptions` |
| auth/model key | configured in the app's LLM config / vault (see spec; **never** commit the key — this repo is public) |

Two gotchas the implementation must respect (spec §3):

* **The gateway rejects `/v1/audio/transcriptions`** for this model
  (`bad_response_status_code`), and `whisper-*` does not exist there. Use
  `/v1/chat/completions` with:
  ```json
  {"model":"mimo-v2.6-flash","messages":[{"role":"user","content":[
    {"type":"input_audio","input_audio":{"data":"<base64-wav>","format":"wav"}},
    {"type":"text","text":"Transcribe the audio exactly as spoken, in its original language. Then on a new line write LANGUAGE=zh or LANGUAGE=en."}]}]}
  ```
* **Cloudflare:** a bare `Authorization: Bearer` gets `403 error code: 1010`.
  Reuse `myUtils/llm_client._headers()` (claude-cli UA, `x-app: cli`,
  `anthropic-version`/`anthropic-beta`). This is why the old transcription path
  403'd.
* **Transcript location:** the model may return the text in
  `choices[0].message.reasoning_content`, sometimes in `.content` — read
  `content or reasoning_content`.
* **Audio prep:** `ffmpeg -vn -ac 1 -ar 16000 -c:a pcm_s16le out.wav`. base64
  inflates ~33%, so keep chunks small; transcode long clips or chunk them.
  `mimo-v2.5` is rejected as "high risk" — use `mimo-v2.6-flash`.

---

## 5. Language detection (`zh` / `en`)

Detected from the transcript (CJK codepoints ⇒ `zh`, otherwise `en`), falling
back to the title when there is no transcript (spec §4). The result is stored on
the ready entry as `language`. The existing per-account `audience_language`
routing (§6) then generates copy in each account's language, seeded by the
detected language and the transcript.

---

## 6. Ready-entry contract (`state.json`)

Spec §6 adds these fields to every `ready` item (kept alongside the legacy ones):

```json
{
  "title": "<Title with spaces>",
  "sfwFlag": "sfw" | "nsfw",
  "publishDate": "YYYY-MM-DD" | null,
  "language": "zh" | "en" | null,
  "transcript": "<text>" | null,
  "contentNote": "<text>" | null
}
```

* `title` is an **alias** of `topic`; `topic` is kept for compatibility.
* `publishDate` is `null` for the legacy form and for a new-form name with no
  date token.
* `transcript` is the sibling note if present, otherwise `null` (the backend then
  runs STT, §4).

---

## 7. `publishDate` → `schedule.startAt` (the submit mapping)

A `publishDate` becomes a naive local start time (spec §5):

```
publishDate = "2026-10-15"
  ⇒  schedule.startAt = "2026-10-15T21:00:00"
```

* **Format:** `"YYYY-MM-DDT21:00:00"` — **naive** (no timezone suffix), interpreted
  in local **`Asia/Taipei`**.
* **Default hour is 21:00** unless the operator sets a different one.
* A file with **no** date token schedules normally from now (the scheduler places
  it at the next preferred window).
* Example submit call:
  ```bash
  curl -s -X POST "$SAU_API/publish-center/submit" \
    -H "Authorization: Bearer $SAU_TOKEN" -H 'Content-Type: application/json' \
    -d '{"profileIds":[1],"mediaFilePaths":["videoFile/<name>"],
         "schedule":{"publishNow":false,"startAt":"2026-10-15T21:00:00"}}'
  ```

### Collision handling

The spec delegates collisions to the existing allocator:
"the existing `_next_free_slot` allocator handles collisions (≥30 min gap,
≤3/day)" (spec §5). Concretely, when two targets land on the same account at the
same slot the scheduler:

* keeps at least a **30-minute gap** between two posts from the same account;
* allows **at most 3 posts per account per day**;
* the orchestrator additionally **staggers each account's fan-out 5 minutes
  apart**.

These match the scheduling policy in
[`skills/sau-bulk-videos/SKILL.md`](../skills/sau-bulk-videos/SKILL.md) §5. A
pinned `publishDate` is an **anchor** — only the time-of-day may be nudged into a
preferred window; the date itself is not moved.

---

## 8. Per-profile / per-language account routing

Language follows the **account**, not the file: an account configured with
`audience_language: en` receives English copy, a `zh-Hant` account receives
Traditional Chinese. The content guard rejects mismatched copy.

The table below is the **live** `accounts` table (queried from
`db/database.db`, `config_json.audience_language`, 2026-10-12). It matches the
tables in [`skills/sau-bulk-videos/SKILL.md`](../skills/sau-bulk-videos/SKILL.md)
§"Language is chosen automatically, per account".

**Nakedwill — profile 1 — English accounts (`audience_language: en`)**
facebook `Nakedwill` · instagram `NW_IG` · nw_sw_blog `NW Blog` · reddit
`Nakedwill Reddit` · tiktok `Nakedwill_TK` · threads `NWthreads` · youtube
`Itswill_YT` · twitter `NW X (model_will)` · bluesky `NW Bluesky EN` · telegram
`NW TG 本人`

**Nakedwill — profile 1 — Traditional Chinese accounts (`audience_language: zh-Hant`)**
bluesky `NW Bluesky ZH` · telegram `NW TG 中文` · twitter `NW X (nudeweiwei)`

**Sexualwill — profile 3 — English accounts (`audience_language: en`)**
facebook `SW-FB` · instagram `SW_IG` · reddit `Sexualwill Reddit OAuth` · threads
`SW_threads` · twitter `sexualwill` · bluesky `SW Bluesky EN` · telegram
`SW TG 本人`

**Sexualwill — profile 3 — Traditional Chinese accounts (`audience_language: zh-Hant`)**
bluesky `SW Bluesky ZH` · telegram `SW TG 中文` · twitter `光光`

**Bilingual (accepts either script, `audience_language: en,zh-Hant`)**
nw_sw_blog `SW Blog` (profile 3). *(The profile-1 blog `NW Blog` is `en`-only.)*

**Teaching — profile 4**
twitter `Willy Dev tutor` (`en`); the rest are `zh-Hant`: bluesky `Teaching_BSKY`,
facebook `teaching-fb`, instagram `Teaching_IG`, threads `teaching-threads`,
tiktok `Teaching_TK`, teaching_blog `Teaching Blog`.

**Money Systems Lab — profile 10 (`en`)**
teaching_blog `MSL Blog` · youtube `Willy Dev tutor`.

An English clip on `SFW NW` therefore publishes to the **EN set and the ZH set** —
both, not one or the other. Write English copy for the EN accounts and Traditional
Chinese copy (not Simplified) for the ZH accounts, then humanize with `humanizer`
(English) or `humanizer-zh` (繁中).

---

## 9. Quick checklist for the submitting agent

1. Read the folder → persona (`nw`/`sw`/`both`/`teaching`/`msl`).
2. Parse the name → `title`, `sfwFlag`, optional `publishDate`, and (legacy)
   `topic`.
3. Look for the same-name `.txt` / `.md` / `.content.txt` → `transcript`; if
   absent, submit and let the backend STT it.
4. Detect `language` (`zh`/`en`) from the transcript or title.
5. Route per `audience_language` (§8); write + humanize both sets.
6. Map `publishDate` → `schedule.startAt = "<date>T21:00:00"` naive
   `Asia/Taipei`, and let the allocator resolve ≥30 min / ≤3-per-day collisions.
7. Preview → operator approval → submit; report per-platform delivery.
