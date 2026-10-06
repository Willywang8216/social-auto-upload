# Sociamonials Fallback Coverage Audit

Date: 2026-10-05
Scope: `myUtils/sociamonials_fallback.py`, `myUtils/worker.py::_try_sociamonials_fallback`
and its call sites, `myUtils/platform_limits.py`, and every row of `accounts` in
`db/database.db`.
Method: read-only. The accounts table was read with `sqlite3`; the actual
`resolve_mapping()` was executed against each row via the vendored interpreter
(`.venv/bin/python`) rather than re-implementing the precedence logic.

---

## 1. Activation state

- `SAU_SOCIAMONIALS_FALLBACK=1` and `SOCIAMONIALS_API_KEY` are set in `.env`, so
  `is_enabled()` returns `True`. The fallback is live.
- Workspace id `26985` (`SOCIAMONIALS_WORKSPACE_ID`).
- `should_attempt_fallback()` is defined and documented as the guard that must be
  used to avoid re-routing permanent failures, **but nothing in the codebase
  calls it** (only its own unit tests do). The real guard is structural, in the
  worker caller. See §5.

## 2. Mapping precedence (as implemented)

`resolve_mapping(account_id, platform=..., account=..., settings=...)`:

1. Per-account override: `account.config["sociamonials"]` (accepts
   `network`, `profile_ref`/`profile_refs`, `name`).
2. Profile settings override: `profile.settings["sociamonials"]`, either a single
   entry or `{"accounts": {<id>: {...}}}`.
3. Built-in `SAU_ACCOUNT_TO_SOCIAMONIALS`.

When a platform is known, the resolved `network` must equal
`_platform_network(platform)` or the publish raises (hard mismatch guard).

### Override state in the database

- No profile row has a `sociamonials` key (checked all of `profiles.settings_json`).
- No account has a real `config["sociamonials"]` key.
- One false positive worth flagging: account **124**'s `config_json` contains the
  substring `"sociamonials"` only inside `_lastMaintenanceError`
  (`"… re-auth via Connect (Sociamonials fallback active)"`). Any naive
  `config_json LIKE '%sociamonials%'` scan will misreport it as an override.
  Account 124 is in fact served by the built-in map.

**Consequence: 100% of live mapping comes from the built-in dict.** The
override hooks are currently unused (no operator has re-pointed an account).

## 3. Per-account mapping result (all 34 rows)

Mapped (16):

| id | profile | platform | account_name | network | profile_refs |
|----|---------|----------|--------------|---------|--------------|
| 11 | 1 NW | facebook | Nakedwill | fb | 19784\|175913 |
| 42 | 3 SW | threads | SW_threads | thrd | 1939 |
| 62 | 1 NW | threads | NWthreads | thrd | 361 |
| 64 | 3 SW | facebook | SW-FB | fb | 30186\|255814 |
| 72 | 1 NW | instagram | NW_IG | in | 52817 |
| 75 | 3 SW | instagram | SW_IG | in | 82894 |
| 77 | 3 SW | twitter | sexualwill | tw | 14100 |
| 103 | 3 SW | twitter | 光光 | tw | 14099 |
| 109 | 1 NW | tiktok | Nakedwill_TK | tiktok | 3917 |
| 110 | 1 NW | youtube | Itswill_YT | yt | 13223 |
| 118 | 1 NW | bluesky | NW Bluesky EN | blsk | 1280 |
| 119 | 1 NW | bluesky | NW Bluesky ZH | blsk | 1279 |
| 120 | 3 SW | bluesky | SW Bluesky EN | blsk | 1281 |
| 121 | 3 SW | bluesky | SW Bluesky ZH | blsk | 1282 |
| 123 | 1 NW | twitter | NW X (model_will) | tw | 13425 |
| 124 | 1 NW | twitter | NW X (nudeweiwei) | tw | 13426 |

Unmapped (18) — every one of these can never fall back:

| id | profile | platform | account_name | why unmapped |
|----|---------|----------|--------------|--------------|
| 43 | 4 Teaching | instagram | Teaching_IG | Teaching/MLS persona — no Sociamonials profile in the map |
| 58 | 4 Teaching | facebook | teaching-fb | Teaching/MLS persona |
| 61 | 4 Teaching | threads | teaching-threads | Teaching/MLS persona |
| 100 | 4 Teaching | tiktok | Teaching_TK | Teaching/MLS persona |
| 107 | 4 Teaching | twitter | Willy Dev tutor | Teaching/MLS persona **on the platform where fallback matters most** |
| 108 | 10 MLS | youtube | Willy Dev tutor | MLS persona |
| 125 | 4 Teaching | bluesky | Teaching_BSKY | Teaching persona (status 0 / not logged in) |
| 105 | 1 NW | reddit | Nakedwill Reddit | Sociamonials network `reddit` defined in limits, but no profile mapped |
| 106 | 3 SW | reddit | Sexualwill Reddit OAuth | same |
| 116 | 1 NW | telegram | NW TG 本人 | Sociamonials network `telegram`, no profile mapped |
| 117 | 3 SW | telegram | SW TG 本人 | same |
| 122 | 3 SW | telegram | SW TG 中文 | same |
| 127 | 1 NW | telegram | NW TG 中文 | same |
| 112 | 1 NW | nw_sw_blog | NW Blog | blog platform — not a Sociamonials network |
| 113 | 3 SW | nw_sw_blog | SW Blog | blog platform |
| 114 | 4 Teaching | teaching_blog | Teaching Blog | blog platform |
| 115 | 10 MLS | teaching_blog | MSL Blog | blog platform |

Summary of platform coverage:

- **Mapped platforms:** `facebook`, `instagram`, `twitter`, `bluesky`, `threads`,
  `tiktok`, `youtube` (7).
- **Platforms with zero mapped accounts:** `reddit`, `telegram`, `linkedin`,
  `pinterest` (and the four blog platforms, which Sociamonials cannot publish to).
- **Entire Teaching (profile 4) and Money Systems Lab (profile 10) personas have
  no mapping at all** — their direct publishes fail straight to permanent failure
  with no fallback, including Teaching's Twitter account 107.

## 4. Limit enforcement per mapped network code

`NETWORK_MAX_*` are derived from `myUtils.platform_limits.py`, so fallback and
direct publishers share the numbers.

| code | message max | max images | video duration | media size |
|------|-------------|------------|----------------|------------|
| fb | 63206 — enforced (truncate) | 10 — enforced (drop extras) | 14460s — enforced, **local file only** | **NOT enforced** |
| in | 2200 — enforced | 10 — enforced | 900s — local only | **NOT enforced** |
| tw | 280 — enforced (links stripped first) | 4 — enforced | 140s — local only | **NOT enforced** |
| blsk | 300 — enforced | 10 — enforced | 600s — local only | **NOT enforced** |
| thrd | 500 — enforced | 20 — enforced | 300s — local only | **NOT enforced** |
| tiktok | 2200 — enforced | 35 — enforced | 3600s — local only | **NOT enforced** |
| yt | 5000 — enforced | **NOT enforced** (`MAX_IMAGES` has no `youtube`) | 43200s — local only | **NOT enforced** |
| ln | 3000 — defined, unreachable (no mapped account) | 20 — defined, unreachable | 900s — defined, unreachable | **NOT enforced** |
| pi | 800 — defined, unreachable | 1 — defined, unreachable | 300s — defined, unreachable | **NOT enforced** |
| reddit | 40000 — defined, unreachable | 20 — defined, unreachable | 900s — defined, unreachable | **NOT enforced** |
| telegram | 4096 — defined, unreachable; **caption cap 1024 ignored** | 10 — defined, unreachable | **no cap** (`VIDEO_MAX_SECONDS["telegram"] is None`) | **NOT enforced** |

Where each check lives:

- Message: `compose_message_with_links()` (sociamonials_fallback.py:719) truncates
  to `NETWORK_MAX_MESSAGE_CHARS`; X/Twitter URLs are removed first (line ~747) and
  carried in `first_comment`.
- Max images: `publish_via_sociamonials()` (line ~980) slices `image_refs` to
  `NETWORK_MAX_IMAGES[network]` with a warning.
- Video duration: `_assert_video_duration()` (line 393), called at line 975 only
  when `video_ref` came from a **local** artifact.
- Media size: **no check anywhere.** `_upload_local_media()` computes
  `size_bytes` (line 482) only to put it in the upload grant; it is never compared
  to `platform_limits.MEDIA_MAX_MB`. `_verify_remote_media()` (line 358) inspects
  content-type/status but never `Content-Length`. `platform_limits.media_max_mb`
  is imported by the direct publishers but **not** by `sociamonials_fallback.py`.

## 5. Failure-path coverage in the worker

`_handle_failure()` has three fallback call sites:

1. **Permanent, non-retryable failure** (`exc.retryable is False`) — calls
   `_try_sociamonials_fallback(..., only_platform="twitter")`. Only Twitter is
   allowed to fall back on a permanent failure. Every other mapped platform
   (fb, in, blsk, thrd, tiktok, yt) is marked failed without a fallback attempt.
2. **Retry budget exhausted** (`attempts >= max_attempts`) — calls the fallback
   with no `only_platform`, so any mapped platform is attempted. This is the only
   path that reaches non-Twitter platforms.
3. **X direct publish disabled** shortcut — always attempts the fallback.

So today: a Twitter target falls back on either a permanent failure or budget
exhaustion; a Facebook/Instagram/Bluesky/Threads/TikTok/YouTube target falls back
only after exhausting all three retries. A permanent (non-retryable) failure on
those platforms never reaches Sociamonials.

## 6. Unenforced limits (consolidated)

1. **Media file size — all networks.** No `MEDIA_MAX_MB` check in the fallback.
   Over-limit local files are uploaded as-is; over-limit remote URLs are handed to
   Sociamonials. Rejection is deferred to Sociamonials/an opaque 422, or the post
   silently fails delivery. (Affects fb 4096 MB, in 300 MB, tw 512 MB, blsk
   300 MB, thrd 1024 MB, tiktok 4096 MB, yt 262144 MB, plus unreachable
   ln/pi/reddit/telegram.)
2. **Video duration for URL-only media.** `_assert_video_duration` only probes a
   local file. A video supplied as a `public_url` (no `local_path`) skips the
   check entirely. A probe failure (ffprobe missing/error) also passes through.
3. **YouTube max images.** `NETWORK_MAX_IMAGES` has no `youtube` entry, so
   `max_images` is `None` and no image-count cap is applied for `yt`.
4. **Telegram media-caption length.** `MESSAGE_MAX_CHARS["telegram"]` is 4096, but
   `platform_limits.TELEGRAM_CAPTION_MAX_CHARS = 1024` is never consulted; a media
   post would be truncated only to 4096. (Telegram is currently unreachable, but
   the limit is still wrong if mapped later.)
5. **YouTube title / Reddit title limits.** `YOUTUBE_TITLE_MAX_CHARS = 100` and
   `REDDIT_TITLE_MAX_CHARS = 300` are not applied; `_network_options()` copies the
   draft title verbatim into `networks.yt.title`. (Reddit title is not sent at
   all, so a reddit post would lack its required title.)
6. **Max video count.** `MAX_VIDEOS` is not enforced. The code path only ever sets
   one `body["video_url"]`, and additional video artifacts silently overwrite the
   previous one with no warning.
7. **Bluesky byte cap.** `BLUESKY_BYTE_MAX = 3000` is not checked. The 300-char
   truncation makes overflow unlikely, but it is not a guarantee for all
   grapheme/byte combinations.

## 7. Other findings

- The module docstring/comment claims the built-in map was "corrected with the
  full `socialupload-groups.json` profile refs". No `socialupload-groups.json`
  exists anywhere in the repo (searched to depth 4), so those refs cannot be
  re-verified from source; they came from a live API discovery.
- `should_attempt_fallback()` is exported as the permanent-failure guard yet is
  never invoked by the worker; the docstring on the module and the function claim
  a guard that the call graph does not actually use.
- Image-count enforcement happens **after** every image has already been uploaded
  (`_upload_local_media`), so dropped images waste upload bandwidth but the post
  itself is correctly capped.

## 8. Recommendations (for the operator's "catch ALL platforms" goal)

1. Add a `MEDIA_MAX_MB` check in the fallback: compare `path.stat().st_size` in
   `_upload_local_media` (and `Content-Length` in `_verify_remote_media`) against
   `platform_limits.media_max_mb(NETWORK_TO_PLATFORM[network])`, and either shrink
   or raise `SociamonialsFallbackError` before the post call.
2. Enforce video duration for public-URL media too (HEAD `Content-Length` cannot
   give duration; either require a local probe or record the limitation
   explicitly).
3. Remove the `only_platform="twitter"` restriction in `_handle_failure` so a
   permanent failure on any mapped platform can fall back (or add the missing
   mapping for accounts 43/58/61/100/107/108/125).
4. Map the Teaching and Money Systems Lab accounts to their Sociamonials profiles,
   or explicitly document that those personas have no Sociamonials connection.
5. Add `reddit` / `telegram` / `linkedin` / `pinterest` to
   `SAU_ACCOUNT_TO_SOCIAMONIALS` only if Sociamonials actually holds connected
   profiles for them; otherwise mark those platforms as "no fallback possible".
6. Add the missing `youtube` entry to `MAX_IMAGES` (or document why `yt` needs no
   image cap) and enforce `TELEGRAM_CAPTION_MAX_CHARS` / `YOUTUBE_TITLE_MAX_CHARS`
   / `REDDIT_TITLE_MAX_CHARS`.
7. Either wire `should_attempt_fallback()` into `_try_sociamonials_fallback` or
   delete it to stop the doc/code drift.
8. Fix account 124's noise by moving the "Sociamonials fallback active" string out
   of `_lastMaintenanceError` (or add a real `config["sociamonials"]` override) so
   a naive override scan does not misreport it.
