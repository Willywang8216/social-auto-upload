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
My great title space separated_NSFW_20261015.docx          # stem.docx (Word)
```

* If the note exists, its text becomes the ready entry's `transcript` and seeds
  the copy.
* `.docx` is accepted since **2026-10-20** — the operator writes hand
  transcripts and episode notes in Word (`both/*.docx` since 2026-10-13,
  `Nakedwill-news-20261016__sfw.docx` on 2026-10-17). Before that they were
  silently ignored, every docx-backed clip fell back to auto-titling from the
  filename, and the operator's links/resources never reached the copy. The
  watcher extracts the text with stdlib `zipfile` + `ElementTree` (the cron
  interpreter has no `python-docx`); a broken/legacy `.doc` yields an empty
  note instead of failing the scan.
* If it is **absent**, the **backend runs STT on the video audio**. Per spec §2:
  "there is no requirement for an agent to transcribe." A submitting agent should
  submit with transcription enabled and let the server do it.

> Long clips: the backend STT sends the whole WAV through
> `/v1/chat/completions` in one request, and a 10-minute clip (~5 MB base64)
> regularly takes 5+ minutes or times out. For anything over ~2 minutes, chunk
> it yourself (`ffmpeg -ss/-t` 120 s windows, one request per window) — that is
> what `stt_local.py` did for the 2026-10-20 batch.

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

### ⚠️ A pinned date is an anchor, not a promise — check the horizon

The allocator never breaks the ≥30 min / ≤3-per-day rules, so it **walks forward**
when an account is full. When the operator's queue is deep that walk is long:
on **2026-10-20** the pending queue was ~5 000 targets and the per-account
horizon was

| account | pending | last slot |
| --- | ---: | --- |
| `NW TG 中文` (`account:127`) | 613 | 2027-09-10 |
| `NW X (model_will)` (`account:123`) | 553 | 2027-04-15 |
| `NW X (nudeweiwei)` (`account:124`) | 532 | 2027-04-11 |
| `sexualwill` (`account:77`) | 220 | 2027-03-15 |

So a fresh submission asking for `2026-10-22T21:00` really lands on Telegram/blog
that evening but on X in **April 2027**. Two consequences:

* **"Are new uploads scheduled right away?"** — no. They are *queued* instantly
  (prep → `preparing` → `publishing`, targets get a slot), but the slot for a
  saturated account is whatever the cap allows, which can be months out.
* For anything **dated** (a Pride invitation, a news episode) do not trust the
  returned `schedule_at`. Verify it, and if it is past the event, **swap** the
  target with an evergreen target on the same account inside the window — a
  permutation keeps every invariant (the account's set of booked minutes does
  not change) while moving the dated copy earlier. That is what
  `_work/fix_pride_schedule.py` did on 2026-10-20 (14 swaps across accounts 77,
  103, 124; 0 Pride invitations left after 10-30).

See [`reports/inbox-new-uploads-2026-10-20.md`](../reports/inbox-new-uploads-2026-10-20.md)
for the full run.

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

**Routing policy (backend-enforced).** Content language selects the accounts:

> **Strict by default.** English content goes to English-audience accounts only.
> `includeZhForEnglish` is an explicit opt-in and was *not* wanted on 2026-10-27:
> 83 queued targets (English videos on 光光 / NW X (nudeweiwei) / NW Bluesky ZH /
> NW TG 中文 / SW Bluesky ZH / SW TG 中文) were cancelled, and 8 had already
> published. Two things caused it — that opt-in, and STT *refusal* text being
> stored as a transcript, which made the router see English on Chinese media and
> vice versa (`myUtils/llm_client.is_transcription_failure` now drops it).
> `scripts/audit_language_routing.py` lists/cancels violations, and
> `myUtils.pipeline_monitor` alerts on any that appear, so the next misroute is
> reported instead of published.

* **Chinese** content goes **only** to Chinese (`zh`/`zh-Hant`) accounts.
* **English** content goes to English (`en`) accounts by default. To also send
  it to the Chinese accounts, pass `options.includeZhForEnglish: true` in the
  submit payload ("English can go to the zh one if I said so").
* **Bilingual** accounts (`audience_language: en,zh-Hant`) accept either.
* An account with **no** `audience_language` is never filtered.

The backend drops the non-matching accounts at draft time and lists them in
`skipped` with a `content language is …` reason. So an English clip on `SFW NW`
goes to the EN set; the ZH set joins only when you opt in. Copy is written (and
humanized) in each kept account's own language — Traditional Chinese (not
Simplified) for `zh-Hant`.

---

## 9. Quick checklist for the submitting agent

1. Read the folder → persona (`nw`/`sw`/`both`/`teaching`/`msl`).
2. Parse the name → `title`, `sfwFlag`, optional `publishDate`, and (legacy)
   `topic`.
3. Look for the same-name `.txt` / `.md` / `.content.txt` → `transcript`; if
   absent, submit and let the backend STT it.
4. Detect `language` (`zh`/`en`) from the transcript or title.
5. Route per `audience_language` (§8): Chinese content → ZH accounts only;
   English → EN accounts only, unless the operator opts in with
   `options.includeZhForEnglish: true`. Write + humanize the copy in each kept
   account's language.
6. Map `publishDate` → `schedule.startAt = "<date>T21:00:00"` naive
   `Asia/Taipei`, and let the allocator resolve ≥30 min / ≤3-per-day collisions.
7. Preview → operator approval → submit; report per-platform delivery.
8. **Archive + metadata** (see §10): rename the published source in the Drive
   archive and write a same-name summary beside it.

---

## 10. Archive + metadata — rename the video, write the summary beside it

> **Automated since 2026-10-21.** `python -m myUtils.inbox_archive` runs from
> cron every 15 minutes and does this step for every inbox object whose campaign
> exists (scheduled or published) — see
> [`docs/pipeline-monitoring.md`](pipeline-monitoring.md) §2. The manual recipe
> below is what it runs (and what you use to unstick something).

After a clip is published (or an already-published inbox file is cleared out),
the Drive archive must stay self-describing: the video keeps a human name and a
**same-name summary/description file** sits beside it. This is the "rename +
metadata" step — do it for every piece of media that runs through the inbox.

**Rename to the filename convention** (same as §1):

```
<Title with spaces>_<SFW|NSFW>_<YYYYMMDD>.<ext>
```

- **Title** = the human title distilled from the transcript (or the original topic).
- **Flag** = `SFW` / `NSFW` (case-insensitive).
- **Date** = publish date `YYYYMMDD` in Asia/Taipei; omit when there is none.
- Keep the original extension and bytes — this is a **server-side rename**
  (`rclone moveto`), never a re-encode.

**Metadata file — same basename, `.txt` (or `.md`):**

```
My Title_NSFW_20261015.mp4
My Title_NSFW_20261015.txt     <- polished summary + description/copy
```

Put into that file: (1) the **polished transcript summary** (subject / tone /
key moments), and (2) the **per-account description/copy** (or at least the
shared brief), so the archive is the source of truth for "what was said and what
was posted".

On the deploy host, rclone lives in the container (the host shell has no
config). Rename with `rclone moveto` (server-side, no re-upload, no delete),
then upload the note beside it:

```bash
docker exec social-auto-upload sh -c \
  "RCLONE_CONFIG=/app/rclone-cache.conf rclone moveto \
   'GDrive-willywang8216:sau/published/videoFile/<old name>' \
   'GDrive-willywang8216:sau/published/videoFile/My Title_NSFW_20261015.mp4'"

docker exec -i social-auto-upload sh -c \
  "RCLONE_CONFIG=/app/rclone-cache.conf rclone copyto - \
   'GDrive-willywang8216:sau/published/videoFile/My Title_NSFW_20261015.txt'" \
  < /path/to/summary.txt
```

Verify with `rclone lsf` and only call it done when **both** objects list back:
the video under its new name and the `.txt` beside it. Already-published files
still in the inbox are archived this way instead of being re-published.

---

## 11. Blog articles are full articles (never empty)

The blog platforms (`nw_sw_blog` and `teaching_blog`) publish **long-form
articles**, not short captions. The backend prompt requires a complete
700–1500-word Markdown article (one `# H1`, several `H2/H3` sections, a
conclusion), and a guard rejects any blog message under 600 chars so an
"article" that is only a title can never be queued. Thin transcripts/notes are
expanded with on-topic, first-person reasoning — they are never echoed as the
body. Copy is then humanized per language (`humanizer` for English,
`humanizer-zh` for Traditional Chinese).

Re-publish helpers for legacy empty posts live in
`scripts/fix_empty_blog_articles.py` (full article re-generation) and
`scripts/fix_bilingual_articles.py` (en + zh-Hant re-publish).
