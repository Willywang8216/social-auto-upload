# Platform Posting Limits — Code Enforcement Audit

**Repo:** `/home/will/social-auto-upload`
**Mode:** read-only audit (no code changed, nothing posted)
**Scope:** `myUtils/content_rules.py`, `myUtils/media_prep.py`, `myUtils/prepared_publishers.py`,
`myUtils/platform_capabilities.py`, `myUtils/sociamonials_fallback.py`, `myUtils/worker.py`,
`uploader/`, `sau_backend.py`, `tests/` (plus the two secondary char-limit tables in
`myUtils/content_generator.py` and `myUtils/sheet_export_service.py` that disagree with the above).

> Line numbers are as of this audit. If a file changes above a constant the number will drift.
> Constants are authoritative; use the quoted name to re-find them.

---

## 0. TL;DR

There are **three independent enforcement layers** and they do not share a source of truth:

1. **Draft-generation layer** (`sau_backend._generate_platform_draft` → `content_rules.prepare_platform_draft`).
   Trims `message` to `content_rules.PLATFORM_RULES[platform].max_chars`. **Only runs when the Publish
   Center generates copy with/without an LLM.** Any payload that arrives with a `draft` already set
   (prepared campaigns, Google-Sheet imports, sociamonials fallback, direct API/CLI) skips it.
2. **Import / media-prep layer** (`sau_backend._prepare_campaign_media_artifacts` → `_shrink_for_publish`
   → `media_prep.shrink`). Compresses video when it exceeds the strictest `media_prep.PLATFORM_MAX_MB`
   cap, 1080×1920 or 30 fps. Best-effort: if ffmpeg is missing or the encode fails it falls back to
   the source unless `_assert_within_platform_caps` can still prove the source is over cap. **Videos only;
   images never go through this layer.** It also never runs outside the Publish Center import path.
3. **Publish layer** (`myUtils/prepared_publishers.py` per-platform functions, `uploader/*` browser
   uploaders, `myUtils/sociamonials_fallback.py`). This is where *some* platforms enforce hard limits
   and others enforce nothing.

**Consequences:** the same limit can be defined in several files at different values (TikTok 2200 vs
150; Threads 1 GiB vs 1024 MB; TikTok 4 GiB vs "1 GB" message), and for several platforms a limit is
defined in only one layer and therefore silently bypassed by the other two. `linkedin` and `pinterest`
have **no publisher and no `content_rules` rule at all** — only a char constant used for generation.

---

## 1. Master matrix

Legend: **gen** = draft-generation trim (`content_rules`), **prep** = media import/shrink
(`media_prep`), **pub** = publish-time hard check, **fb** = sociamonials fallback.
"gen-only" means the only place the value is applied is the generation path; the publish call sends the
payload verbatim.

| Platform | Message limit | Media size (MB) | Video duration (s) | Max images | Max videos |
|---|---|---|---|---|---|
| **twitter / X** | 280 (gen; fb 280) | 512 prep; image 5 MB **browser only**; API none | 140 browser (auto-split) + fb; **API none** | 4 (pub) | 1 (pub) |
| **bluesky** | 300 (pub, fb, gen) | 300 prep; video 295 (pub); image 0.9 (pub) | 600 (pub, fb) | 4 (pub) | 1 (pub) |
| **facebook** | 63206 (gen-only; fb none) | 4096 prep-only | **none** | 10 (pub) | 1 (pub) |
| **instagram** | 2200 (gen-only; fb none) | 250 prep-only | fb 900 only; **pub none** | 10 (pub) | 1 (pub) |
| **threads** | 500 (pub, gen) | 1024 prep / **1 GiB pub (mismatch)** | 300 (pub, fb) | 10 (pub) | 1 (pub) |
| **tiktok** | 2200 (pub, gen); `content_generator`/sheet say **150** | 4096 prep / 4 GiB pub (error says "1 GB") | min 3 / max creator_info or 3600 (pub, fb) | 35 (pub) | 1 (pub) |
| **youtube** | title 100, desc 5000, tags 500 (pub); gen 5000 | 262144 prep-only (effectively unbounded) | **none** | thumbnail only, unchecked | 1 — silently uses `videos[0]`, **not enforced** |
| **reddit** | title 300 (pub); **body none** | **none** (prep default 300) | **none** | 1 (pub) | 1 (pub) |
| **telegram** | caption 1024 (pub); text-only 4096 **not enforced**; fb none | 2000 prep-only | **none** | 10 (pub, albums) | 1 (pub) |
| **linkedin** | **none in repo** (gen constant 3000 only) | **none** | **none** | **none** | **none** |
| **pinterest** | **none in repo** (gen constant 500 only) | **none** | **none** | **none** | **none** |
| **nw_sw_blog** | **none** (long-form, no cap) | n/a (Git commit) | n/a | hero image 1, unchecked | 0 |

Backend-wide hard cap that overrides everything above on the upload path:
`sau_backend.py:449 app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024` (500 MB Flask request cap).

---

## 2. Where the constants live

### 2.1 `myUtils/content_rules.py` (generation layer)
* `SHEET_MESSAGE_MAX_CHARS` — `content_rules.py:155-160` → facebook 63206, instagram 2200, twitter 280,
  **tiktok 150**.
* `PLATFORM_RULES` — `content_rules.py:200-215`:
  twitter 280, threads 500, instagram 2200, facebook 63206, **tiktok 2200**, bluesky 300.
  youtube/reddit/telegram have `max_chars=None`; patreon/teaching_blog/nw_sw_blog are `long_form`.
  **No linkedin or pinterest entry** (so `get_platform_rule('linkedin')` raises).
* Applied in `prepare_platform_draft` at `content_rules.py:319`
  (`trim_to_max_length(message, rule.max_chars)`) and in `build_sheet_row` at `content_rules.py:369`
  (`trim_to_max_length(message, SHEET_MESSAGE_MAX_CHARS.get(platform))`).

### 2.2 `myUtils/media_prep.py` (import/media-prep layer, video only)
* `PLATFORM_MAX_MB` — `media_prep.py:60-69` (decimal MB): bluesky 300, instagram 250, twitter 512,
  threads 1024, tiktok 4096, telegram 2000, youtube 262144, facebook 4096.
  **No entry** for reddit / linkedin / pinterest / nw_sw_blog / discord / patreon / medium / substack.
* `DEFAULT_MAX_MB = 300` — `media_prep.py:73` (fallback for any platform not in the table).
* `SIZE_HEADROOM = 0.98` — `media_prep.py:78`; effective trigger is `cap * 0.98`.
* `resolve_size_limit_mb` — `media_prep.py:94-107` (strictest cap among targets).
* `should_shrink` / `shrink` — `media_prep.py:231-254` / `267`. Dimensions capped at 1080×1920,
  fps at 30 (`TARGET_W/H`, `MAX_FPS` — `media_prep.py:28-30`).
* **Only caller in production:** `sau_backend._shrink_for_publish` (`sau_backend.py:3360`, called from
  `_prepare_campaign_media_artifacts` at `sau_backend.py:3585`). Never called by `worker.py` or any
  `prepared_publishers` function → a video that reaches publish without passing the Publish Center
  import path is never size-checked by this module.

### 2.3 `myUtils/prepared_publishers.py` (publish layer)
* Telegram: `_telegram_caption_chunks` 1024 → `prepared_publishers.py:417-420`; ≤10 media and
  images-only albums → `:744-747`; MTProto caption reuse `:982-983`.
* Facebook: ≤1 video `:1316`, ≤10 images `:1318` and slice `[:10]` `:1352`; message sent verbatim `:1337/:1373/:1381/:1394`.
* Instagram: ≤1 video `:1568`, ≤10 images `:1570`, slice `[:10]` `:1586`; caption verbatim `:1582/:1602/:1612`.
* Threads: `THREADS_MAX_TEXT_CHARS = 500` `:59`, truncate `:1776-1781`;
  `THREADS_MAX_VIDEO_SECONDS = 300.0` `:64`; `THREADS_MAX_VIDEO_BYTES = 1 GiB` `:65`, check `:1689`;
  duration check `:1699-1704`; ≤1 video `:1786`, ≤10 images `:1788`, slice `[:10]` `:1804`.
* TikTok: `TIKTOK_MAX_PULL_FROM_URL_BYTES = 4 GiB` `:72` (check `:1927`; message says "1 GB");
  `TIKTOK_MIN_VIDEO_SECONDS = 3.0` `:73`; `TIKTOK_MAX_VIDEO_SECONDS = 3600` `:74`;
  `TIKTOK_MAX_CAPTION_CHARS = 2200` `:75`; caption checks `:1908/:1950`, slices `:2087/:2246-2247`;
  ≤1 video `:2049`, ≤35 images `:2049/:2052`, slice `[:35]` `:2233`.
* X / Twitter API: ≤4 images / ≤1 video, no mix `:2694-2700`; **no text cap** — `{"text": message}`
  at `:2755`; media upload `_x_media_upload` `:2460-2523` has **no size or duration check**.
* Reddit API: ≤1 video `:3132`, ≤1 image `:3134`, `title[:300]` `:3169`; **body `text` not truncated**.
* YouTube API: `title[:100]` `:3340`, `description[:5000]` `:3362`, `tags[:500]` `:3367`; only
  `videos[0]` used, no rejection of >1.
* NW/SW blog: text/git only, no length caps (`:3754+`).
* Bluesky: `BLUESKY_MAX_IMAGE_BYTES = 900_000` `:3937`; `BLUESKY_MAX_VIDEO_BYTES = 295_000_000`
  `:3941`; `BLUESKY_MAX_VIDEO_SECONDS = 600.0` `:3942`; message required+truncated to 300 at
  `:4117/:4126-4127`; ≤4 images / ≤1 video / no mix `:4142`, slice `[:4]` `:4212`;
  duration check `:4178-4184`.
* `_message_title` helper truncates any derived title to 100: `:291-293`.

### 2.4 `uploader/` (browser uploaders)
* `uploader/twitter_uploader/main.py`: `TWITTER_MAX_VIDEO_SECONDS = 140.0` `:25`,
  `TWITTER_SPLIT_SEGMENT_SECONDS = 139.0` `:26`, `TWITTER_MAX_IMAGES_PER_TWEET = 4` `:27`,
  `TWITTER_MAX_IMAGE_BYTES = 5 MiB` `:28` (shrink `:105-139`), auto-split `plan_video_segments`
  `:184+` / `plan_thread_segments` `:250+`. **No tweet text length check**
  (`_build_post_text` `:637`).
* `uploader/youtube_uploader/main.py`: browser path `self.title[:100]` `:238`, tags
  `",".join(self.tags)[:500]` `:296`; no description/size/duration cap.
* `uploader/reddit_uploader/main.py`: no title/body/media limits.
* `uploader/base_post.py`: `MAX_TITLE_LENGTH = 100` `:19`, `MAX_TAGS = 5` `:22` (long-form only:
  medium/substack), `MIN_SCHEDULE_LEAD_TIME = 15 min`.
* `uploader/tk_uploader/main.py`, `ks_uploader`, `douyin_uploader`, `tencent_uploader`,
  `bilibili_uploader`, `xiaohongshu_uploader`: **no posting-limit constants** (browser legacy paths).

### 2.5 `myUtils/sociamonials_fallback.py` (fallback layer)
* `_MEDIA_REQUIRED_NETWORKS = {"in", "tiktok", "yt", "pi"}` — `:73`.
* `NETWORK_MAX_VIDEO_SECONDS` — `:79-85` (`tw 140, blsk 600, thrd 300, in 900, tiktok 3600`),
  enforced by `_assert_video_duration` `:388-405` **only when a local file can be probed**.
* `NETWORK_MAX_MESSAGE_CHARS` — `:86-89` (`tw 280, blsk 300`), applied in
  `compose_message_with_links` `:743-745`. **Every other network's message is sent untruncated.**
* X link-in-body is stripped and moved to first comment (`LINK_IN_MAIN_POST_FORBIDDEN_NETWORKS`,
  `:696`; applied in `compose_message_with_links` at `:741-748`). No cap on image count / video count / media size.

### 2.6 `myUtils/platform_capabilities.py` (fan-out flag, not a limit)
* `SINGLE_MEDIA_PLATFORMS = {tencent, tiktok, youtube, telegram, discord}` — `:26-34`.
* `SUPPORTS_FIRST_COMMENT = {facebook, instagram}` — `:36-40`.
* Used only in `publish_orchestrator.py:387-388` and the Publish Center UI (`sau_backend.py:7133-7134`).
  It does **not** encode any length/size/duration/count and omits platforms whose publishers enforce
  single-video rules (bluesky, twitter, facebook, instagram, threads).

### 2.7 `myUtils/worker.py`
* **No posting-limit enforcement at all.** It imports `content_rules` only for
  `is_usable_copy` / `message_matches_language` (`worker.py:106,118`). It dispatches straight to
  `prepared_publishers.publish_*_sync` (`:1758,1774,1825,1838,1851,1968,1983,1998,2013,2099,2112`)
  and to the sociamonials fallback (`:923+`). No size/duration/count pre-flight.

### 2.8 `sau_backend.py` (Publish Center)
* `MAX_CONTENT_LENGTH = 500 MB` — `:449` (global Flask upload cap).
* `_shrink_for_publish` — `:3360`; `_assert_within_platform_caps` — `:3405`; invoked `:3585`.
* `_generate_platform_draft` → `content_rules.prepare_platform_draft` — `:4057`; fallback trim
  `brief[:max_chars]` with `max_chars = rule.max_chars or 1000` — `:7229-7233`.
* Preview exposes `maxChars` from `content_rules` — `:7132`; `platform_capabilities` flags —
  `:7133-7134`.

---

## 3. Per-platform detail

### twitter / X
* **Message:** `content_rules` 280 (gen) + fallback `tw=280`. Publish-time X API (`:2755`) and browser
  composer (`_build_post_text`) send the message **unchecked**. Premium/25 000-char accounts are not
  modelled.
* **Media size:** `media_prep` 512 MB is only the *shrink trigger* on the import path. Browser image
  >5 MiB is re-encoded (`TWITTER_MAX_IMAGE_BYTES`). API `_x_media_upload` has no size cap.
* **Duration:** browser auto-splits >140 s into 139 s segments; fallback rejects `tw>140 s`. API
  publisher does not check and does not split → an API-connected X account can post a >140 s video and
  get an API rejection.
* **Images/videos:** 4 / 1, publisher-enforced. Browser batches images 4-per-tweet and makes a reply chain.
* **Unit mismatch:** browser image cap is MiB (`5 * 1024 * 1024`); `media_prep` uses decimal MB.

### bluesky
* Best-covered platform: message 300 (gen + publish truncation + fallback), video 600 s (publish +
  fallback), video 295 MB (publish), image 0.9 MB (publish), 4 images / 1 video / no mix (publish).
* Minor mismatch: `media_prep` says 300 MB, publisher hard-rejects at 295 MB (`_bluesky_shrink_video`
  re-encodes under 295 MB), and the lexicon comment mentions 300,000,000 bytes. Effective cap 295 MB.
* Fallback has no image cap: `collect_media` sends every image URL; only `NETWORK_MAX_MESSAGE_CHARS` /
  `NETWORK_MAX_VIDEO_SECONDS` are checked for `blsk`.

### facebook
* **Message 63206 is gen-only.** Publisher `publish_facebook_sync` passes `message` straight to
  `description`/`message` with no trim; fallback has no `fb` entry. A prepared/sheet payload with an
  over-length caption is sent as-is.
* **Media size 4096 MB is prep-only.** No publish-time check.
* **No duration constant anywhere** (prep or publish).
* 1 video / 10 images enforced at publish.

### instagram
* **Message 2200 is gen-only** (publisher no trim, fallback no `in` entry).
* **Media size 250 MB is prep-only.**
* **Duration:** publisher does not check; fallback `in=900`. Instagram Reels allow up to 15 min, so the
  900 s check is fallback-only.
* 1 video / 10 images enforced at publish.

### threads
* Message 500 truncation and 300 s / 1 GiB video validation are publish-enforced. Good.
* **Size mismatch:** `THREADS_MAX_VIDEO_BYTES = 1024*1024*1024` is **1 GiB = 1073.7 decimal MB**, while
  `media_prep.PLATFORM_MAX_MB["threads"] = 1024` decimal MB and its trigger is 1024×0.98 = 1003.5 MB.
  A 1050 MB file passes `media_prep` (if it shrank, output will be smaller) but is also accepted by the
  publisher's hard check. The two layers disagree on ~5%.
* Fallback has no `thrd` char entry (message untruncated).

### tiktok
* Strong publish-time enforcement: caption 2200, min 3 s, max creator-info or 3600 s, 1 video,
  35 images.
* **Inconsistent values:**
  * `content_rules.PLATFORM_RULES['tiktok'].max_chars = 2200`, but
    `SHEET_MESSAGE_MAX_CHARS['tiktok'] = 150` and `content_generator.PLATFORM_CHAR_LIMITS['tiktok'] = 150`.
    Google-Sheet import over-trims to 150; the publisher would accept 2200.
  * `TIKTOK_MAX_PULL_FROM_URL_BYTES = 4 GiB` but the raised error reads "up to 1 GB"
    (`prepared_publishers.py:1928-1929`). `media_prep.PLATFORM_MAX_MB['tiktok'] = 4096` decimal MB
    (4 GiB = 4294.97 MB) — a third number for the same cap.
  * `TIKTOK_FILE_UPLOAD_MAX_CHUNK_SIZE = 64 MiB` and chunk plan are upload mechanics, not a file cap.

### youtube
* Publish-time field caps: title 100, description 5000, tags 500 (browser and API both). No
  size/duration check.
* `media_prep` 262144 MB (256 GB) is a shrink trigger only; effectively no enforcement.
* `content_generator` youtube 5000 applies to the whole generated message while the publisher maps the
  message to *description* and derives a separate 100-char title — so a 5000-char generated message is
  fine as a description but the derived title is silently cut to 100.
* **>1 video not rejected:** `publish_youtube_sync` uses `media["videos"][0]` and ignores the rest.
* Fallback `yt` has no char/duration entry (only "media required").

### reddit
* `title[:300]` enforced at publish (API). Body/self-text has **no cap** although Reddit's body limit is
  40 000; `content_generator` uses 10000 for generation.
* 1 video / 1 image enforced at publish. Browser path has no explicit caps.
* `media_prep` has no reddit entry → default 300 MB trigger; publisher has no size/duration check.
* Fallback has no `reddit` network in the built-in map and no char/duration entry.

### telegram
* Media caption truncated to 1024 with the overflow posted as a follow-up message
  (`_telegram_caption_chunks`), for both Bot API and MTProto.
* **Text-only 4096 limit not enforced.** `content_generator` uses 4096; `content_rules` has no telegram
  cap; the fallback has no `telegram` entry. A long text-only post is sent to the API unchecked.
* ≤10 media, images-only albums enforced at publish. No size/duration check (`media_prep` 2000 MB
  trigger only).

### linkedin
* **No enforcement anywhere.** Not in `content_rules.PLATFORM_RULES`, not in `media_prep.PLATFORM_MAX_MB`,
  no `publish_linkedin_sync`. Only `content_generator.PLATFORM_CHAR_LIMITS['linkedin'] = 3000`,
  `sheet_export_service` 3000, and a `ln` sociamonials mapping. The Publish Center preview shows
  `maxChars: null`.

### pinterest
* **No enforcement anywhere.** Same situation as LinkedIn; only the generation constant 500 and a `pi`
  sociamonials mapping (which does require media). No publisher.

### nw_sw_blog
* Long-form MDX; no title/body/tag caps in `publish_nw_sw_blog_sync` (Git commit path). `content_rules`
  is `long_form` with no `max_chars`. `BasePostUploader.MAX_TITLE_LENGTH=100` is not applied here.
  A hero image URL is written to frontmatter but never validated for size/format.

---

## 4. Gaps — limits with NO enforcement

Ranked by risk (a real platform rejection or silently-wrong post that this code will still submit).

1. **X/API text length** — `publish_twitter_sync` sends `{"text": message}` (`:2755`) with no cap.
   Generation trims to 280, but prepared/API payloads do not. **Gap.**
2. **X/API video duration** — the 140 s rule exists only in the browser uploader's splitter and the
   fallback. The API path neither checks nor splits. **Gap.**
3. **X/API media size** — `_x_media_upload` (`:2460-2523`) has no image/video byte cap. **Gap.**
4. **Facebook message length** — 63206 defined only in generation. Publish + fallback send verbatim. **Gap.**
5. **Instagram message length** — same, 2200 gen-only. **Gap.**
6. **Facebook / Instagram / Reddit / YouTube / Telegram / LinkedIn / Pinterest media size at publish** —
   only `media_prep` (video, import path) knows a size cap; the publish functions do not. **Gap.**
7. **Instagram video duration in the primary path** — only the fallback checks 900 s. **Gap.**
8. **Facebook video duration** — no constant/check anywhere. **Gap.**
9. **YouTube video duration** — no check (YouTube's limit is ~12 h/256 GB). **Gap.**
10. **Reddit body length** — only the 300-char title is cut; body can exceed Reddit's 40 000. **Gap.**
11. **Telegram text-only 4096** — only the 1024 *caption* path is enforced. **Gap.**
12. **YouTube >1 video** — silently ignored, not rejected. **Gap.**
13. **NW/SW blog** — no title/body/tag/size validation. **Gap (low risk — operator-authored MDX).**
14. **LinkedIn / Pinterest** — no publisher, no `content_rules` rule, no media cap; the entire posting
    path is absent. **Gap.**
15. **Fallback max images / max videos** — `sociamonials_fallback.collect_media` appends every image and
    keeps one video; `NETWORK_MAX_MESSAGE_CHARS` only covers `tw`/`blsk`. All other networks (fb, in,
    thrd, yt, reddit, telegram, linkedin, pinterest) get no count or length guard. **Gap.**
16. **`media_prep` platforms not in the table** (reddit, linkedin, pinterest, nw_sw_blog, discord,
    patreon, medium, substack) silently fall back to `DEFAULT_MAX_MB = 300` — a 300 MB cap that is
    *not* the platform's real cap (Discord webhooks are ~8-25 MB; Pinterest ~32 MB images). **Gap.**
17. **`platform_capabilities.SINGLE_MEDIA_PLATFORMS`** omits facebook/instagram/threads/bluesky/twitter,
    so the Publish Center fan-out logic does not know those platforms also allow only one video. **Gap.**

---

## 5. Hard-coded inconsistencies

| Limit | Value A | Value B | Where |
|---|---|---|---|
| TikTok caption | **2200** | **150** | `content_rules.PLATFORM_RULES` `:200` + `prepared_publishers.TIKTOK_MAX_CAPTION_CHARS` `:75` vs `SHEET_MESSAGE_MAX_CHARS` `:155` and `content_generator.PLATFORM_CHAR_LIMITS` |
| TikTok max video size | **4 GiB** (=4295 decimal MB) | **4096 MB** and message **"1 GB"** | `prepared_publishers.TIKTOK_MAX_PULL_FROM_URL_BYTES` `:72` vs `media_prep.PLATFORM_MAX_MB['tiktok']` `:65` vs error text `:1928` |
| Threads video size | **1 GiB** (1073.7 MB) | **1024 MB** | `prepared_publishers.THREADS_MAX_VIDEO_BYTES` `:65` vs `media_prep.PLATFORM_MAX_MB['threads']` `:64` |
| Bluesky video size | **295 MB** | **300 MB** | `BLUESKY_MAX_VIDEO_BYTES` `:3941` vs `media_prep.PLATFORM_MAX_MB['bluesky']` `:61` (comment also cites 300,000,000 bytes) |
| Telegram message | **1024** (media caption) | **4096** (text) | `_telegram_caption_chunks` `:417` vs `content_generator.PLATFORM_CHAR_LIMITS['telegram']` |
| X image size unit | **5 MiB** | **512 MB decimal** (video) | `uploader/twitter_uploader TWITTER_MAX_IMAGE_BYTES` `:28` vs `media_prep` decimal-MB table |
| Reddit body | **10000** (gen) | **unlimited** (pub) | `content_generator.PLATFORM_CHAR_LIMITS['reddit']` vs `publish_reddit_sync` |
| YouTube message | **5000** (whole message, gen) | **100 title / 5000 description / 500 tags** (pub) | `content_generator`/`content_rules` vs `publish_youtube_sync` `:3340-3367` |
| URL/API duration tables | browser/fallback only | API publisher absent | see gaps 1-3 |
| Image vs video media tables | decimal MB (`10^6`) | MiB (`2^20`) in uploader chunk/image code | `media_prep.size_mb_decimal` doc vs `TWITTER_MAX_IMAGE_BYTES`, `TIKTOK_FILE_UPLOAD_*`, `THREADS_MAX_VIDEO_BYTES` |

No hard-coded posting limit was found in `myUtils/worker.py` — it deliberately does not validate.

---

## 6. Test coverage of limits

Tests exist for the generation and publish layers, but **none cover the gaps above**.

| Test file | What it pins |
|---|---|
| `tests/test_content_rules.py` | `prepare_platform_draft` trimming and label normalization (e.g. tiktok) |
| `tests/test_content_generator.py:27` | `PLATFORM_CHAR_LIMITS['instagram'] == 2200` |
| `tests/test_media_prep.py:113-148` | `PLATFORM_MAX_MB['bluesky']==300`, `resolve_size_limit_mb`, `should_shrink` |
| `tests/test_prepared_publishers.py:643-644` | Threads text truncation to `THREADS_MAX_TEXT_CHARS` |
| `tests/test_prepared_publishers.py:737` | Threads rejects `THREADS_MAX_VIDEO_BYTES + 1` |
| `tests/test_prepared_publishers.py:1187,1220-1237` | TikTok size + caption >2200 |
| `tests/test_prepared_publishers.py:2122-2128` | Bluesky image shrink under `BLUESKY_MAX_IMAGE_BYTES` |
| `tests/test_twitter_uploader.py:117,160` | X video split at 140 s, image batches ≤4 |
| `tests/test_twitter_block_heavy_media.py` | X browser heavy-media blocking (not limits) |
| `tests/test_sociamonials_fallback.py:239-279` | fallback `tw` 280 chars, `blsk` 300 chars, `tw` 140 s duration |
| `tests/test_sau_backend.py:408-502` | `_shrink_for_publish` / `_assert_within_platform_caps` behavior |
| `tests/test_youtube_auth.py`, `tests/test_reddit_uploader.py` | auth/browser flow, no limit assertions |
| `tests/test_sheet_export_service.py` | column mapping only (no char-limit assertion) |

No test asserts facebook/instagram/reddit/telegram/youtube message caps, X API text/duration/size, or
the LinkedIn/Pinterest absence.

---

## 7. Suggested follow-up (no code changed here)

1. Make `content_rules.PLATFORM_RULES` the single message-length source and have every publisher call
   `trim_to_max_length(message, get_platform_rule(platform).max_chars)` (or raise) so prepared/sheet/API
   payloads cannot bypass it.
2. Add a shared media-limit table (message, MB, duration, max images, max videos, per platform) consumed
   by both `media_prep` and `prepared_publishers`, replacing the three overlapping tables.
3. Add the X API duration/size checks (or route long API videos through the splitter).
4. Add LinkedIn and Pinterest to `content_rules` and `media_prep` (or explicitly document them as
   unsupported and reject them in the orchestrator).
5. Reconcile the TikTok 150 vs 2200 and the GiB-vs-decimal-MB values, and fix the "1 GB" error text.
6. Enforce Telegram text-only 4096 and Reddit body length.
7. Have `sociamonials_fallback` validate image/video counts and message length for every mapped network,
   not just `tw`/`blsk`.
