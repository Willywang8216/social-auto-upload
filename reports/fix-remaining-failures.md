# Fixing the remaining failed publish targets

Date: 2026-10-10
Scope: the 48 `publish_job_targets` rows in `status='failed'` that the operator
measured, all from jobs created `<= 2026-10-08`.

At apply time the live queue had **51** failed targets, because the worker was
running and produced three more while this pass was in flight:

| new target | platform | error |
| --- | --- | --- |
| 3475 | telegram | `PreparedPublishError: Telegram media file is missing or empty: /app/generated/campaigns/campaign-233...` |
| 3391 | twitter | `Sociamonials accepted the post but delivery failed: Video URL not accessible after retries` |
| 3399 | twitter | `Sociamonials accepted the post but delivery failed: Video URL not accessible after retries` |

3475 is the same class as the missing-media rows below and was cancelled with
them; 3391/3399 are a Sociamonials delivery failure and are left alone. The
counts below are stated against the original 48 and called out where 3475 joins.

---

## 1. Per-class verdict table

| # | Class (target ids) | Verdict | Reasoning |
| --- | --- | --- | --- |
| 1 | MediaRestoreError — demo.mp4 / nonexistent.mp4 (5657, 5665, 5666, 5667, 5673, 5674, 5683, 5684, 5685, 5687, 5695, 5696, 5697, 5698, 5699) | **permanent → cancel** | `demo.mp4` / `nonexistent.mp4` were test fixtures that never existed on disk or in remote storage; the restore URL 404s. |
| 2 | `FileNotFoundError: /app/uploads/*.png` (75, 76) | **permanent → cancel** | Local PNGs are gone (`uploads/` is empty) and the only remote copy (`up.iamwillywang.com/getFile?...`) returns **404** (verified live). |
| 3 | Reddit "No subreddit on this account can accept this content" (3874, 3896, 3923 explicit content; 5435 monetised brand) | **permanent → cancel** | The Reddit registry refusal is content-based (`myUtils/subreddits.py`); the same payload is refused on every retry. |
| 4 | `[content-guard] placeholder/generic copy` (3473) | **permanent → cancel** | Copy is `publish-center-…` + a filename-like line + generic filler tags. `content_rules.is_usable_copy()` is correct here. |
| 5 | `[content-guard] copy does not match account language 'en'` (3476) | **permanent → cancel** | The copy is Traditional Chinese and the account's `audience_language` is `en`. The guard is correct. |
| 6 | `YouTube publish requires a local video artifact` (3850) | **permanent → cancel** | Campaign 2398 is image-only; YouTube can never accept it. `retryable=False` is correct. |
| 7 | `HTTPError 404 http://localhost:5409/getFile…` (3449, 3450) | **fixable code bug — fixed** | An unreachable localhost URL was stored as an artifact `public_url`. See §2.1. Targets are now stale; left in the queue. |
| 8 | `413 Request Entity Too Large` Facebook (5592) | **fixed code gap (backfill) — leave** | The size guard is present and the file is 108.5 MB (cap 4096 MB), so the guard is not the cause. The payload was missing the already-uploaded R2 URL, forcing a multipart `source` upload; `_backfill_artifact_public_urls` now recovers it. See §2.2. |
| 9 | Threads duration 442 s (3785) / 600 s (5626) | **fixed code/feature gap — leave** | 5626: the duration-probe failure silently disabled splitting (fixed `cf39dd1`). 3785: the splitter did not exist when the campaign was prepared (`b72bd2b`). See §2.3. |
| 10 | `TypeError: get_browser_options() takes 0 positional arguments` (73) | **fixed — leave (stale)** | Fixed `8fc146e` (2026-06-30); the call site passes no argument now. See §2.4. |
| 11 | `X direct publish disabled and the Sociamonials fallback failed` (3448, 3796, 5360, 5361, 5438, 5440, 5448, 5489, 5494) | **retryable — leave** | Fallback refused with `storage_quota_exceeded` (HTTP 422) on every target. Frees when the Sociamonials workspace quota is cleared / asset reuse works; not permanently impossible. |
| 12 | `HTTPError 403 open.tiktokapis.com/v2/post/publish/video/init/` (12, 13) | **retryable — leave** | May 2026 "This is a test" fixtures. 403 is app/permission mode, which can change (the repo added `FILE_UPLOAD` fallback for unverified domains, `eace052`). |
| 13 | `TikTok API error (invalid_params): The request post info is empty or incorrect` (3847) | **retryable — leave** | Single old photo post. Already classified `retryable=False` by the publisher's error handler; not demonstrably a current code defect without a live call, so not cancelled. |
| 14 | Reddit `RATELIMIT` r/gaybrosgonemild (3865, 4527) | **retryable — leave** | Rate limits expire; the classifier explicitly never treats them as permanent. |
| 15 | Reddit `HTTP 401 Unauthorized` (72) | **retryable — leave** | Token expiry; Reddit OAuth was rewritten after this job (`7b93d1a`). Re-auth can succeed. |
| 16 | Reddit `Please log in to do that.` (77) | **retryable — leave** | Cookie/token expiry; re-login can succeed. |
| 17 | Reddit `requires a non-empty subreddits array` (71) | **retryable — leave** | Account 105 now has subreddits configured; the publisher prefers the account's list. `TEEST` fixture, so no certainty either way. |
| 18 | Threads `HTTP 400 … subcode 2207032` (2772) | **retryable — leave** | Stale Meta container error; the container-status wait and URL normalisation were added after this job (`413a24b`, `559b126`). |
| 19 | Sociamonials delivery failure `Video URL not accessible after retries` (3391, 3399 — new) | **retryable — leave** | Media URL fetch issue; not classified permanent. |

**Cancelled: 25** (15 MediaRestoreError + 2 FileNotFoundError + 4 Reddit
platform-refused + 2 content-guard + 1 YouTube + the new 3475).
**Left alone: 26** (24 from the original set + 3391/3399).

---

## 2. Code bugs investigated

### 2.1 Unreachable localhost URL was emitted for non-URL-fetch platforms — FIXED

`myUtils/campaign_media_prep.py` only suppressed an unreachable origin when a
URL-fetch platform (`_REMOTE_URL_PLATFORMS = TikTok/Facebook/Instagram/Threads`)
was targeted:

```
if needs_public_url and not _is_public:
    ...suppress...
else:
    public_url = candidate      # localhost emitted for Bluesky/Reddit
```

Targets #3449/#3450 are Bluesky jobs whose payload artifact carried
`public_url = http://localhost:5409/getFile?filename=20260726165244333_pub_pub.mp4`.
Bluesky uploads bytes, but when the artifact's local copy (`/app/generated/...`)
is absent it falls back to fetching `public_url` (`_bluesky_fetch_to_temp`,
`myUtils/prepared_publishers.py:4561`) and got the 404.

Fix: a single host-aware helper is now used for **every** platform, and for the
raw TikTok artifact too.

- `myUtils/campaign_media_prep.py:47-63` — `_is_public_base_url()`
  (`https://`, host not in `{localhost,127.0.0.1,0.0.0.0,::1}`).
- `myUtils/campaign_media_prep.py:606-615` — suppress and warn when not public.
- `myUtils/campaign_media_prep.py:637-640` — same guard on `raw_public_url`.

Evidence (before/after, same inputs):

```
before: bluesky http://localhost:5409 -> imageUrls ['http://localhost:5409/getFile?filename=pic.jpg']
after : bluesky http://localhost:5409 -> imageUrls []   stored public_url None
        bluesky https://cdn.example.com -> imageUrls ['https://cdn.example.com/getFile?filename=pic.jpg']
```

Tests: `tests/test_campaign_media_prep.py` —
`test_unreachable_origin_is_suppressed_for_any_platform`,
`test_public_origin_helper_rejects_loopback_and_http`.

### 2.2 Facebook 413 — the missing size guard was NOT the cause

- Platform/cap: Facebook, `platform_limits.media_max_mb("facebook")` = **4096 MB**
  (`myUtils/platform_limits.py:56`), enforced at publish by
  `_enforce_video_limits` (`myUtils/prepared_publishers.py:101-155`, called for
  Facebook at `:1566`).
- The failed artifact is `108,498,063` bytes (108.5 MB, measured live from R2),
  so the guard correctly did not fire. **There is no missing size guard.**
- What actually happened: the payload's artifact id 2459 (`watermarked_video`)
  had `public_url: null`, while a sibling campaign artifact (id 2460,
  `remote_upload`, same `source_file_record_id`) already held the R2 URL. With
  no `public_url`, the publisher used the multipart `source` upload path
  (`prepared_publishers.py:1589-1594`) and Meta returned 413.
- This gap is already fixed by `_backfill_artifact_public_urls`
  (`myUtils/worker.py:1513`, called at `:1033`, commit `a565a71`). Reproduced:

```
before backfill: None
after  backfill: https://pub-9915b1494003455c9ab872fd7094e64e.r2.dev/campaigns/2555/videos/afdac...watermarked_video.mp4
```

With the URL, Facebook takes the `file_url` branch and Meta fetches server-side,
which avoids the multipart `source` path that returned the 413. No further code
change needed.

### 2.3 Threads duration 442 s / 600 s — why the split did not run

- The splitter exists: `campaign_media_prep.py` plans one split per over-cap
  `(seconds, MB)` and calls `media_prep.split_to_seconds`
  (`myUtils/campaign_media_prep.py:687-740`).
- **Target 5626 (600 s, 2026-10-08):** a duration-probe failure was swallowed
  (`except Exception: duration = 0.0`), which silently disabled duration
  splitting for *every* platform. Fixed in `cf39dd1` (B5): it now logs and keeps
  the size trigger. `_validate_threads_video_artifact`
  (`prepared_publishers.py:1917-1966`) raises `retryable=False` so an over-cap
  file fails once instead of burning the budget.
- **Target 3785 (442 s, 2026-09-21):** `media_prep.split_to_seconds` was not
  introduced until `b72bd2b` (2026-10-08), so on 2026-09-21 there was no
  duration split to run. Campaign 2386's only video is a manual trim; the R2
  object is exactly 300 s (downloaded and probed: `duration=300.000000`,
  16,728,520 bytes). The 442 s reading came from the stale local
  `/tmp/trim-work/src_..._300s.mp4`.
- No current code bug; both rows stay in the queue for a fresh submit/re-prep.

### 2.4 `get_browser_options()` TypeError — already fixed

`utils/browser_hook.py:3` defines `def get_browser_options():` (no params) and
every call site passes none, including the Reddit one that failed
(`uploader/reddit_uploader/main.py:208`). The failing call
`await get_browser_options(p)` was removed in `8fc146e` on 2026-06-30, one day
after target 73 failed (2026-06-29). Target 73 is stale; the classifier's
`test_an_unknown_error_is_left_alone` deliberately still leaves a bare
`TypeError` in that form un-cancelled.

### 2.5 YouTube "requires a local video artifact" — correct behaviour

`myUtils/prepared_publishers.py:3757/3766` raise
`retryable=False` when there is no video. Target 3850's payload contains a
single image artifact (campaign 2398) — YouTube cannot publish it. The
orchestrator now skips YouTube for video-less selections
(`myUtils/publish_orchestrator.py:445-451`). Correct; classified permanent.

### 2.6 content-guard cases — guard is right

`myUtils/worker.py:95-123` calls `content_rules.is_usable_copy()` and
`content_rules.message_matches_language()`. Verified directly:

```
m1 usable          = False   # publish-center-... + 20260820122135356 — adult...
m2 matches 'en'    = False
m2 matches 'zh'    = True
```

3473 is genuinely placeholder/generic; 3476 is genuinely the wrong language for
an `en` account. The guard was **not** weakened.

---

## 3. What was cancelled, and why

`scripts/purge_permanent_failures.py` now classifies three additional permanent
shapes (all content/destination-based, none retryable):

- `media-unrecoverable` += `FileNotFoundError` (media path gone) —
  `scripts/purge_permanent_failures.py:63`.
- `platform-refused` += `No subreddit on this account can accept this content` —
  `:81`.
- new `destination-incompatible` class for `YouTube publish requires a local
  video artifact` — `:89`.

Applied:

```
failed targets: 51
  media-unrecoverable        18  -> cancel
  platform-refused            4  -> cancel
  content-permanent           2  -> cancel
  destination-incompatible    1  -> cancel
  NOT CLASSIFIED             26  -> LEFT ALONE (review)
database backed up to db/database.db.bak-pre-permfail-20261010-111711
cancelled 25 target(s)
failed targets remaining: 26
```

Cancelled ids: 75, 76, 3473, 3475, 3476, 3850, 3874, 3896, 3923, 5435, 5657,
5665, 5666, 5667, 5673, 5674, 5683, 5684, 5685, 5687, 5695, 5696, 5697, 5698,
5699.

Rows are set to `cancelled` (never deleted); the pre-purge database is backed up.

## 4. What was left, and why

Left in `failed` (26): 12, 13, 71, 72, 73, 77, 2772, 3391, 3399, 3448, 3449,
3450, 3785, 3796, 3847, 3865, 4527, 5360, 5361, 5438, 5440, 5448, 5489, 5494,
5592, 5626.

- **Credential/rate-limit/transient** (12, 13, 71, 72, 77, 3865, 4527, 2772):
  a re-auth, a later attempt, or a code fix that already shipped can succeed.
- **Sociamonials quota** (3448, 3796, 5360, 5361, 5438, 5440, 5448, 5489, 5494,
  3391, 3399): blocked on the third-party workspace storage quota/delivery, not
  on us; can recover.
- **Already-fixed code gaps** (73, 3449, 3450, 5592, 5626, 3785): the code
  behind them is fixed and the rows are stale; a fresh submit can succeed.
- **Unproven single target** (3847): already non-retryable by design; not
  cancelled because there is no live reproduction.

None of these are certain to fail forever, so none were cancelled.

## 5. Test evidence

```
$ .venv/bin/python -m pytest tests/ --ignore=tests/test_security_http.py -q
1608 passed, 1 skipped, 161 subtests passed in 310.18s
```

(Baseline was 1592 passed, 1 skipped. The delta includes my +3 test methods and
concurrent work in `tests/test_purge_legacy_drive.py` by a parallel session. An
earlier run showed one flake in
`tests/test_offload_script.py::...test_concurrent_run_skips_when_lock_is_held`,
a timing-dependent `flock` test that passes in isolation and is unrelated to
these changes; the later clean runs above confirm it.)

Targeted:

```
$ .venv/bin/python -m pytest tests/test_campaign_media_prep.py tests/test_purge_permanent_failures.py -q
14 passed, 19 subtests passed
```

## 6. Notes

- The worker was live during this pass and moved the queue (48 → 51 failed). The
  three extra rows are handled above.
- `git status` also shows uncommitted changes in `scripts/purge_legacy_drive.py`
  and `tests/test_purge_legacy_drive.py` from a parallel session; they are not
  part of this work and were not touched.
