---
name: sau-bulk-videos
description: |
  Turn a folder full of videos into scheduled, platform-ready posts. Use it when
  the user points at a directory of videos (or drops files in one) and wants them
  summarised and published — e.g. "publish these videos", "process this folder",
  "batch upload these", or when filenames carry an SFW/NSFW + persona prefix.
  Each video is transcribed, summarised, and rewritten into per-platform titles,
  descriptions, captions and hashtags, then scheduled across the right profiles
  with a sensible spread. Handles the "SFW NW" / "NSFW NW" / "SFW SW" /
  "NSFW SW" / "Teaching" naming convention. Do NOT use it to publish a single
  explicit file the user has already described in full, and never use it to ask
  for or store a credential.
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - AskUserQuestion
metadata:
  owner: social-auto-upload
  trigger: publish these videos, process this folder, batch upload, bulk videos, SFW NW, NSFW NW, SFW SW, NSFW SW, Teaching
---

# Batch-publish a folder of videos

One pass per folder: **identify → transcribe → summarise → write copy → schedule**.
The rule that matters most is the **filename prefix decides the destinations**.

## 1. The prefix rule (read this first)

A filename begins with a routing prefix. It has two independent parts:

| Prefix | Profile | Meaning |
|---|---|---|
| `SFW NW` | Nakedwill (profile 1) | **Every** enabled NW platform |
| `NSFW NW` | Nakedwill (profile 1) | **Only** NW platforms that permit nudity |
| `SFW SW` | Sexualwill (profile 3) | **Every** enabled SW platform |
| `NSFW SW` | Sexualwill (profile 3) | **Only** SW platforms that permit nudity |
| `Teaching` | Teaching (profile 4) | Every enabled Teaching platform |

**SFW is the opposite of what you might assume.** SFW means *publish everywhere*,
including the platforms that only accept non-explicit media. NSFW means
*publish only where nudity is allowed* — it is a **restriction**, not a broadening.

Worked examples:

- `SFW NW beach morning.mp4` → all 13 NW accounts (Instagram, Facebook, Threads,
  TikTok, YouTube **and** the adult-safe ones).
- `NSFW NW shower scene.mp4` → NW accounts **minus** Instagram, Facebook,
  Threads, TikTok, YouTube.
- `SFW SW studio.mp4` → all SW accounts.
- `NSFW SW explicit tease.mp4` → SW adult-safe accounts only.

If a filename carries **no** prefix, do not guess: ask the user which profile and
rating to use, then rename the file so the run is reproducible.

### The gate is real and enforced server-side

`myUtils/content_rating.py` is the single authority:

```python
NSFW_RESTRICTED_PLATFORMS = {"instagram", "facebook", "threads", "youtube", "tiktok"}
```

A file is SFW **only** when its name starts or ends with the token `sfw`
(any case, separated by space / `_` / `-` / `.`). **Everything else is NSFW.**
This default is deliberate: an unlabelled file must never reach a platform that
bans nudity.

The server applies this regardless of what you send, so a NSFW selection can
never leak onto Instagram even if the request asks for it. Do not try to work
around it, and do not rename a file to `sfw` to widen its reach — that is the
one thing this skill must never do.

## 2. Work the folder

Resolve the app once. Everything below talks to the running backend.

```bash
cd <social-auto-upload repo>
export SAU_API="http://localhost:5409"
export SAU_TOKEN="$(grep -E '^SAU_API_TOKENS=' .env | cut -d= -f2)"
# every call needs:  -H "Authorization: Bearer $SAU_TOKEN"
```

Confirm it is alive before doing anything expensive:

```bash
curl -s "$SAU_API/healthz"
```

Then, for each video:

1. **Parse the prefix** and derive `(profile_id, rating)`:

   | prefix | profile_id | rating |
   |---|---|---|
   | `SFW NW` | 1 | sfw |
   | `NSFW NW` | 1 | nsfw |
   | `SFW SW` | 3 | sfw |
   | `NSFW SW` | 3 | nsfw |
   | `Teaching` | 4 | (unset — usually sfw) |

2. **Get it into the app.** Staged media must live under `videoFile/`; the
   container writes there and the host sees it through a bind mount. Do not
   invent other paths.

3. **Probe duration** so you can route long clips correctly:

   ```bash
   curl -s -X POST "$SAU_API/media/video-info" \
     -H "Authorization: Bearer $SAU_TOKEN" -H 'Content-Type: application/json' \
     -d '{"file_path":"<relative path under videoFile/>"}'
   ```

   Duration matters because **Threads caps video at 300 s** and Twitter at 140 s
   for the API path. If a clip exceeds a destination's limit, either drop that
   destination for the clip or make a shorter cut — do not let it fail three
   times first.

4. **Extract the audio and transcribe it.** `myUtils/media_pipeline.extract_video_audio`
   writes 16 kHz mono WAV; feed that to your transcription tool to get the
   spoken content. If a clip has no speech (music only, silent), say so and
   build the copy from the visual content and filename instead of inventing a
   transcript.

5. **Summarise** the transcript into: subject, tone, key moments, and any
   names/places/claims worth surfacing. Keep this internal — it feeds the drafts.

## 3. Write the copy (humanize both languages)

Generate per-account drafts, then rewrite them through a humanizer:

| language | skill | status on this machine |
|---|---|---|
| Traditional Chinese | **`humanizer-zh`** | installed |
| English | **`humanizer`** | **not installed** (see below) |

Humanizing is required, not optional: the raw LLM draft reads like AI, and these
are personal-brand accounts where that is a liability. Apply the humanizer
**after** the first draft exists and **before** submission, so the reviewed copy
is the published copy.

### If the English `humanizer` skill is not installed

The Chinese skill is a translation of <https://github.com/blader/humanizer> (MIT)
plus Wikipedia's *Signs of AI writing*; the English original is the upstream.
Check before relying on it:

```bash
ls ~/.claude/skills/ | grep -x humanizer
```

If it is missing, do **one** of these, in order of preference:

1. Install it (ask the user first — it is an external dependency):
   clone <https://github.com/blader/humanizer> into `~/.claude/skills/humanizer`.
2. Otherwise apply the same rules `humanizer-zh` encodes, in English: strip the
   tells listed below, and say in your report that you did it by hand.

Do not silently skip humanizing English copy, and do not claim a humanizer ran
when it did not.

### The tells to remove (both languages)

- Inflated phrasing: "delve", "tapestry", "testament to", "it's not just X, it's Y".
- The rule-of-three list, and "Not only … but also".
- Em-dash overuse, and a summary paragraph that restates the post.
- Uniform paragraph lengths and every sentence opening the same way.
- Hashtag stuffing, and stock openers like "Let's dive in".
- Chinese: 书面腔 ("首先/其次/最后"), 排比, 空泛总结句, 过度四字词.

Keep the speaker's actual voice: contractions, uneven sentence length, one
concrete detail beats three abstractions.

Respect each platform's real limits — the app enforces them and will truncate or
reject otherwise:

| platform | max chars | notes |
|---|---|---|
| twitter | 280 | 3 hashtags, needs media |
| bluesky | 300 | 3 hashtags; images ≤ 900 KB are downscaled automatically |
| threads | 500 | |
| instagram | 2200 | |
| facebook | 63206 | |
| tiktok | 2200 | title separate |
| youtube | — | needs a **video**; title + description |

Language follows the **account**, not the file: `NW Bluesky EN` gets English,
`NW Bluesky ZH` gets Chinese. Never post source-language copy to an account
configured for another language — the content guard rejects it.

## 4. Preview, then submit

```bash
# 1) generate the drafts the user will review
curl -s -X POST "$SAU_API/publish-center/preview" \
  -H "Authorization: Bearer $SAU_TOKEN" -H 'Content-Type: application/json' \
  -d '{"profileIds":[1],"selectedAccountIds":[...],"brief":"<summary>",
       "options":{"watermark":true,"useLlm":true}}'

# 2) submit once the copy is approved
curl -s -X POST "$SAU_API/publish-center/submit" \
  -H "Authorization: Bearer $SAU_TOKEN" -H 'Content-Type: application/json' \
  -d '{"profileIds":[1],"mediaFilePaths":["<path under videoFile/>"],
       "brief":"<summary>","options":{...},
       "schedule":{"publishNow":false,"startAt":"YYYY-MM-DDTHH:MM:00"},
       "accountDrafts":{...}}'
```

**Show the generated copy to the user before submitting.** The operator asked for
reviewed metadata; submitting unreviewed copy is the failure mode this step exists
to prevent.

For a NSFW file, pass **only the adult-safe account ids** in
`selectedAccountIds`. The server would strip the rest anyway; being explicit keeps
the intent legible and avoids a confusing `skipped` list.

## 5. Schedule it well — do not bunch

The goal is a natural cadence, not a dump.

- **Spread across days, not hours.** For more than a couple of posts, distribute
  them over several days rather than squeezing them into one window.
- **Respect the audience's clock.** The operator's audience is Taipei-based;
  schedule in the local evening/afternoon rather than at 3am.
- **One post per account per slot.** The orchestrator already staggers targets
  **5 minutes apart** (`STAGGER_MINUTES`), so set `startAt` to the intended first
  slot and let it fan out from there.
- **Vary the slot times** between days so the feed does not look automated.
- **Never schedule in the past.** A target whose time has passed runs immediately;
  that is how a backlog turns into a flood.

A reasonable default for a mid-size batch: 2–3 posts per account per day, spread
across 4–10 days, anchored to local 12:00–22:00.

## 6. Verify, don't assume

Queued is not published. After submitting, watch the actual outcome:

```bash
curl -s "$SAU_API/jobs/<job_id>" -H "Authorization: Bearer $SAU_TOKEN"
ls -t logs/jobs/job-*.log | head
```

Per-platform gotchas worth knowing before you report success:

- **Threads** rejects video over 300 s; the publisher fails fast with the measured
  duration rather than retrying blindly.
- **X/Twitter** media upload needs API credits; when they are exhausted the
  Sociamonials fallback carries it, and that fallback **verifies delivery** — a
  queued-but-undelivered post is reported as failed, so trust the job status.
- **Reddit** subreddits may require a flair or restrict links to a whitelist;
  such subreddits are configured to receive a **self post** with the media link in
  the body.
- **YouTube/TikTok** need video (not images).

Report **published vs failed per platform** with the error text. Do not describe a
queued target as published.

## Reference

- Rating rules and the platform gate: `myUtils/content_rating.py`
- Publish entry point: `myUtils/publish_orchestrator.submit_publish`
- Media prep (audio extraction, watermark, shrink): `myUtils/media_pipeline.py`,
  `myUtils/media_prep.py`
- Job/retry semantics: `myUtils/worker.py`, `myUtils/jobs.py`
- Credentials live in the vault, never here:
  `rclone cat Onedrive-Yahooforsub-Tao:credentials/sau-publish-platforms-credentials.json`
