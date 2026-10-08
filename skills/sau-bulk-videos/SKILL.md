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

## How to use this skill — the 30-second cue

**You:** point at a folder of videos whose names carry a routing prefix.

> "Publish everything in `~/videos/august/`. They're prefixed `SFW NW`, `NSFW SW`, etc."

**The agent:**

1. reads each filename's prefix → decides profile + destinations,
2. transcribes and summarises each clip,
3. writes per-account titles/descriptions/captions, then humanizes them
   (`humanizer` for English, `humanizer-zh` for 繁中),
4. **shows you the copy** — you approve or edit,
5. submits, and auto-splits / compresses anything over a platform limit,
6. schedules across days at sensible local times (never a one-day dump),
7. reports **published vs failed per platform** with the real error text.

**Naming is the whole interface:**

```
SFW  NW    <anything>.mp4   → ALL Nakedwill platforms      (profile 1)
NSFW NW    <anything>.mp4   → ONLY Nakedwill nudity-allowed platforms
SFW  SW    <anything>.mp4   → ALL Sexualwill platforms      (profile 3)
NSFW SW    <anything>.mp4   → ONLY Sexualwill nudity-allowed platforms
SFW  NW+SW <anything>.mp4   → ALL platforms of BOTH profiles
NSFW NW+SW <anything>.mp4   → ONLY nudity-allowed, on BOTH profiles
Teaching   <anything>.mp4   → Teaching profile             (profile 4)
```

`NW+SW` (either order, `NW+SW` / `SW+NW` / `NW SW`) means **both personas in one
submission** — `profileIds: [1, 3]`. `submit_publish` already iterates over every
profile id, so this is one call, not two.

`SFW` = publish **everywhere** (including Instagram/Facebook/Threads/TikTok/YouTube).
`NSFW` = publish **only** where nudity is allowed. No prefix → the agent asks
rather than guessing.

### Language is chosen automatically, per account

The content's language decides **which account of that profile** receives it.
Each account carries an `audience_language`; the app generates and routes by it,
and a guard refuses mismatched copy. You do not pick the language by hand — but
you must know the split, because a clip is not published once per profile, it is
published once per **language-appropriate account**:

**Nakedwill (profile 1) — English accounts**
facebook · instagram · nw_sw_blog · reddit · tiktok · threads · youtube ·
twitter `NW X (model_will)` · bluesky `NW Bluesky EN` · telegram `NW TG 本人`

**Nakedwill (profile 1) — Traditional Chinese accounts**
bluesky `NW Bluesky ZH` · telegram `NW TG 中文` · twitter `NW X (nudeweiwei)`

**Sexualwill (profile 3) — English accounts**
facebook · instagram · reddit · threads · twitter `sexualwill` ·
bluesky `SW Bluesky EN` · telegram `SW TG 本人`

**Sexualwill (profile 3) — Traditional Chinese accounts**
bluesky `SW Bluesky ZH` · telegram `SW TG 中文` · twitter `光光`

**Bilingual (accepts either script)**
nw_sw_blog `SW Blog` (`en,zh-Hant`)

So an English clip on `SFW NW`: write English copy for the EN accounts and
Traditional Chinese copy for the three ZH accounts. The English set and the
Chinese set both go out; they are not alternatives.

**Write the copy in the account's language, then humanize with the matching
skill** — `humanizer` for English, `humanizer-zh` for 繁中. Simplified Chinese is
rejected on a `zh-Hant` account, so generate Traditional characters.

**What you get asked about:** the generated copy (step 4) and anything ambiguous
(a missing prefix, an unclear persona). Everything else runs unattended.

---

## Contents

- **1.** the prefix rule (read first)
- **2.** getting the files to the app (Drive route) + transcribing
- **3.** writing + humanizing the copy
- **3b.** platform limits and what is auto-adjusted
- **4.** preview → approve → submit
- **5.** scheduling so it spreads naturally
- **6.** verifying real delivery
- **7.** a worked end-to-end example

---

One pass per folder: **identify → transcribe → summarise → write copy → schedule**.
The rule that matters most is the **filename prefix decides the destinations**.

## 0. Talk to the app

The app is a **deployed service**, not a local process. A client machine only
needs network access to the domain plus the token from the synced `.env` — no
Docker, no WSL, no Python env, no copy of the SAU code.

```bash
# Locate the synced .env (override with SAU_ENV=/path/to/.env if needed)
SAU_ENV="${SAU_ENV:-}"
if [ -z "$SAU_ENV" ]; then
  for c in ./.env "$HOME/social-auto-upload/.env" \
           "${OneDrive:-$HOME/OneDrive}/Scripts-ssh-ssl-keys/socialupload/.env"; do
    [ -f "$c" ] && SAU_ENV="$c" && break
  done
fi
# Public base URL comes from the .env; fall back to the production domain.
export SAU_API="${SAU_API:-$(grep -hE '^SAU_PUBLIC_BASE_URL=' "$SAU_ENV" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\"')}"
export SAU_API="${SAU_API:-https://socialupload.iamwillywang.com}"
export SAU_TOKEN="${SAU_TOKEN:-$(grep -hE '^SAU_API_TOKENS=' "$SAU_ENV" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\"')}"
# every call needs:  -H "Authorization: Bearer $SAU_TOKEN"
curl -s "$SAU_API/healthz"        # confirm the deployed app is alive before expensive work
```

If `SAU_TOKEN` is empty, stop and ask the user for the `.env` path — do **not**
fall back to a local server. There is no local backend to use.

## 1. The prefix rule (read this first)

A filename begins with a routing prefix. It has two independent parts:

| Prefix | Profile | Meaning |
|---|---|---|
| `SFW NW` | Nakedwill (profile 1) | **Every** enabled NW platform |
| `NSFW NW` | Nakedwill (profile 1) | **Only** NW platforms that permit nudity |
| `SFW SW` | Sexualwill (profile 3) | **Every** enabled SW platform |
| `NSFW SW` | Sexualwill (profile 3) | **Only** SW platforms that permit nudity |
| `SFW NW+SW` | **both** | Every enabled platform on **both** profiles |
| `NSFW NW+SW` | **both** | Only nudity-permitting platforms, on **both** profiles |
| `Teaching` | Teaching (profile 4) | Every enabled Teaching platform |

The two parts are independent: rating (SFW/NSFW) and persona (NW/SW/both).
`NW+SW` accepts either order and either separator (`NW+SW`, `SW+NW`, `NW SW`).

**SFW is the opposite of what you might assume.** SFW means *publish everywhere*,
including the platforms that only accept non-explicit media. NSFW means
*publish only where nudity is allowed* — it is a **restriction**, not a broadening.

Worked examples:

- `SFW NW beach morning.mp4` → all NW accounts, **in both languages** (the EN set
  and the three ZH accounts).
- `NSFW NW shower scene.mp4` → NW accounts **minus** Instagram, Facebook,
  Threads, TikTok, YouTube.
- `SFW SW studio.mp4` → all SW accounts, both languages.
- `NSFW SW explicit tease.mp4` → SW adult-safe accounts only.
- `SFW NW+SW trip.mp4` → every platform on **both** profiles in one submission
  (`profileIds: [1, 3]`).

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

## 2. Get the files to the app

### Default: upload over HTTP (works from any client)

Upload each clip with the app's own endpoint. It returns the stored name under
`videoFile/`:

```bash
# -> {"code":200,"data":"<uuid>_<sanitised name>.mp4","msg":"File uploaded successfully"}
curl -s -X POST "$SAU_API/upload" \
  -H "Authorization: Bearer $SAU_TOKEN" \
  -F "file=@/path/to/SFW NW clip.mp4"
```

Use `"videoFile/<that data value>"` as the `mediaFilePaths` entry in
`/publish-center/submit`. `secure_filename` turns spaces/punctuation into `_`, and
the SFW/NSFW rating parser accepts that (`SFW_NW_clip.mp4` is still SFW). Keep the
default → uploaded name mapping so the routing decision (section 1) still matches
the file you submitted.

For large files use the presigned routes instead of the proxy: `POST /upload/direct`
(returns a presigned PUT URL) then `POST /upload/register`, or the
`/upload/multipart/init` + `/presign` + `/complete` trio. `/upload` is fine up to a
few hundred MB.

### Only when you are ON the deploy host

Staged media must be readable by the **container**, which is not the same as being
readable on the host. Two things are true on this deployment and both save time:

1. `videoFile/` is a bind mount, so anything the container writes there the host
   sees (and vice versa).
2. The container mounts an rclone config (`/app/rclone-cache.conf`) that has the
   **`GDrive-willywang8216`** remote, and it can **read and write** `sau/`.
   Verified live: `rclone lsf GDrive-willywang8216:sau/` lists `assets/`,
   `generated/`, `uploads/`, `videoFile/`, and a test write/delete round-tripped.

A host shell on the same box may have **no** rclone config (`~/.config/rclone`
missing) — that is expected and does **not** mean Drive is unreachable. Always
test from inside the container:

```bash
docker exec social-auto-upload sh -c \
  "RCLONE_CONFIG=/app/rclone-cache.conf rclone lsf 'GDrive-willywang8216:sau/'"
```

The harmless `Failed to save config ... device or resource busy` notice appears on
every container rclone call (a read-only bind mount); it does not affect the
transfer.

### Choosing a route

| route | when |
|---|---|
| **HTTP upload** (`POST /upload` on the domain) | the default from any client machine: only network + the token are needed. |
| **Local → Drive → container** (`rclone copy` to `GDrive-willywang8216:sau/videoFile/<batch>/`, then `rclone copy` into `videoFile/` in the container) | on the **deploy host**, when you want the Drive archive (the source survives and a re-run needs no re-upload). |
| Local → container directly (`docker cp` / `rclone copyto`) | deploy host only, when Drive is unavailable and the batch is small. |

Prefer Drive: it is already the offload target
(`offload_to_drive.sh` → `GDrive-willywang8216:sau`), so bulk media placed in
`sau/videoFile/<batch>/` matches where the app already restores from, and the
archive is not duplicated.

```bash
# 1) local -> Drive archive
rclone copy "<local folder>" "GDrive-willywang8216:sau/videoFile/<batch>/" -P

# 2) Drive -> container's videoFile (one hop, inside the container's config)
docker exec social-auto-upload sh -c \
  "RCLONE_CONFIG=/app/rclone-cache.conf rclone copy \
   'GDrive-willywang8216:sau/videoFile/<batch>/' /app/videoFile/<batch>/ -P"
```

Do not upload 10 GB into the DB or re-encode locally: the app does its own probe,
transcribe and prep.

---

## 2b. Transcribe → summarise

**Ask for transcription in the submit call, not the preview** — this trips people
up:

- `preview` builds its media context with an **empty `transcriptText`**. It drafts
  from the filename and `brief` only, so a preview never reflects what is *said*.
- The real transcription runs in `_prepare_campaign_media_artifacts` on the
  **submit** path, using `whisper-1` via the configured LLM
  (`myUtils/llm_client.transcribe_audio`), and it is enabled by
  `options.transcribe: true` — or **automatically** whenever an LLM is configured
  and no transcript exists yet.

So a preview is for *structure and routing*, not final copy. **The transcription
itself is server-side** — it runs on `/publish-center/submit`, so on a client
machine you do not run any SAU code. On the **deploy host** (needs the checkout +
`.venv`) you can transcribe before submitting:

```bash
# deploy host only
python -c "from myUtils import media_pipeline as m; \
  m.extract_video_audio('<video>', '/tmp/a.wav')"
python -c "from myUtils import llm_client as c; \
  print(c.transcribe_audio('/tmp/a.wav'))"
```

From a client, either submit with `options.transcribe: true` and treat the
generated drafts as the transcript-derived copy, or transcribe with a local
whisper/LLM tool. Then summarise the transcript into subject, tone, key moments,
and any names/places worth surfacing. If a clip has no speech, say so and build
the copy from the visual content rather than inventing a transcript.


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

## 3b. Platform limits — and what the app fixes for you

`myUtils/platform_limits.py` is the **single source of truth** (researched
2026-10-05; sources in `logs/platform-limits-research.md`). Do not recite limits
from memory — read that module. Sizes are **decimal MB** because that is the unit
platforms publish their caps in.

| platform | chars | max MB | max seconds | imgs | vids |
|---|---:|---:|---:|---:|---:|
| twitter | 280 | 512 | 140 | 4 | 1 |
| bluesky | 300 | 300 | 600 | 10 | 1 |
| facebook | 63206 | 4096 | 14460 | 10 | 1 |
| instagram | 2200 | 300 | 900 | 10 | 1 |
| threads | 500 | 1024 | 300 | 20 | 1 |
| tiktok | 2200 | 4096 | 3600 | 35 | 1 |
| youtube | 5000 | 262144 | 43200 | — | 1 |
| reddit | 40000 | 1000 | 900 | 20 | 1 |
| telegram | 4096 | 2000 | none | 10 | 10 |
| linkedin | 3000 | 5000 | 900 | 20 | 1 |
| pinterest | 800 | 2048 | 300 | 1 | 1 |

Field-scoped limits that are easy to miss:
`telegram` media caption **1024** (not 4096) · `youtube` **title 100** and
**tags 500** · `reddit` **title 300** · `bluesky` **3000 UTF-8 bytes** (300 graphemes).

### What adjusts automatically — do not hand-roll these

| problem | the app already does | where |
|---|---|---|
| caption too long | truncates to the platform's cap | `content_rules.trim_to_max_length` |
| video too long **or** too big | re-encodes into equal **part** videos, one plan per distinct cap pair, tagged so a big-cap platform (YouTube) never gets a small-cap platform's parts | `media_prep.split_to_seconds` via `sau_backend._prepare_campaign_media_artifacts` |
| file over the size cap | re-encodes to the publishing profile (1080×1920, CRF ~22, ≤30 fps, AAC 128k); refuses to publish the un-shrunk original if the re-encode fails | `media_prep.shrink` / `sau_backend._shrink_for_publish` |
| image over Bluesky's blob cap | downscales to JPEG, stepping the longest side down, under ~900 KB | `_bluesky_shrink_image` |
| oversized emoji/fps/sar | normalises fps to ≤30 and sets `sar=1` | `media_prep.build_filters` |

Verified behaviour for a **813 s / 602 MB** clip:

```
threads -> 3 parts      (300 s cap)
twitter -> 6 parts      (140 s cap, the strictest)
youtube -> 1 part       (untouched: 43200 s / 256 GB cap)
```

So **you normally do not need to cut or compress anything by hand.** Feed the
clip to the app with the right `selected_platforms` and let it plan. Choose by
hand only when you want an editorial cut (e.g. a chosen highlight), not because a
number was exceeded.

The one thing that is *not* automatic is **choosing the destination**: passing the
wrong platforms either wastes a split (Twitter's 140 s cap makes 6 parts) or
sends an adult clip somewhere it will be rejected. That is what the prefix rule in
section 1 is for.

Do the arithmetic before submitting so you can tell the user what will happen.
On the **deploy host** (needs the checkout + `.venv`):

```bash
# deploy host only
.venv/bin/python -c "
from myUtils import platform_limits as pl, media_prep as mp
sec, mb = pl.video_max_seconds('threads'), pl.media_max_mb('threads')
print(mp._split_count(<duration_s>, <size_bytes>, sec, mb*1024*1024))"
```

From a client, use the table above (plus `ffprobe` for duration/size) or just let
the server plan the split on submit — it re-derives every cap from
`myUtils/platform_limits.py` and refuses to overrun.

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
# deploy host only: tail the per-job log
# ls -t logs/jobs/job-*.log | head
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

## 7. Worked example

Folder:

```
~/videos/august/
  SFW  NW  storytime morning coffee.mp4       (48 s,   12 MB)
  NSFW NW  shower after gym.mp4               (813 s, 602 MB)
  SFW  SW  studio shoot part 2.mp4            (640 s, 180 MB)
  Teaching  how to factor quadratics.mp4      (300 s,  40 MB)
```

What the agent decides, and why:

**`SFW NW storytime morning coffee.mp4`** → profile 1, rating sfw → all NW
platforms. 48 s / 12 MB is inside every cap, so nothing is split or shrunk.

**`NSFW NW shower after gym.mp4`** → profile 1, rating nsfw → NW minus
Instagram/Facebook/Threads/TikTok/YouTube. 813 s / 602 MB then gets planned
**per platform**:

```
threads  -> 3 parts   (300 s cap)
twitter  -> 6 parts   (140 s cap)
bluesky  -> 2 parts   (600 s cap, and 300 MB)
reddit   -> 1 part    (900 s, but 602 MB is under its 1000 MB cap -> shrink only)
telegram -> 1 part    (no duration cap; 602 MB under 2000 MB)
youtube  -> excluded (nsfw)
```

The big-cap destinations never receive a small-cap destination's parts.

**`SFW SW studio shoot part 2.mp4`** → profile 3, rating sfw → all SW platforms.
640 s / 180 MB: YouTube and Facebook take it whole; Threads splits it into 3;
Twitter into 5; Instagram stays 1 part (900 s / 300 MB).

**`Teaching how to factor quadratics.mp4`** → profile 4 → the 7 Teaching
accounts. 300 s is exactly Threads' cap, so it is left whole.

Then scheduling: rather than 4 clips × N accounts in one evening, the agent
spreads them over several days, anchored to local 12:00-22:00, with the
automatic 5-minute per-target stagger so no account posts twice in one slot.

Finally it submits, waits, and reports per platform — distinguishing a real
delivery from a queued target, and quoting the actual error for anything that
failed.

## Reference

- Rating rules and the platform gate: `myUtils/content_rating.py`
- Publish entry point: `myUtils/publish_orchestrator.submit_publish`
- Media prep (audio extraction, watermark, shrink): `myUtils/media_pipeline.py`,
  `myUtils/media_prep.py`
- Job/retry semantics: `myUtils/worker.py`, `myUtils/jobs.py`
- Credentials live in the vault, never here:
  `rclone cat Onedrive-Yahooforsub-Tao:credentials/sau-publish-platforms-credentials.json`
