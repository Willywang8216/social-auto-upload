# Platform Posting-Limits — Log & DB Audit

**Generated:** 2026-10-05 (limits-log-audit agent)
**Mode:** read-only. No code was edited; no posts were sent. `db/database.db`
was opened with SQLite `mode=ro`.
**Sources:** `logs/worker.log` (+`.1`), `logs/jobs/*.log` (1,092 files),
`logs/offload.log` (+`.1`), `logs/twitter.log`, `logs/xiaohongshu.log`,
`logs/digest.log.1`, and the `publish_job_targets` / `publish_jobs` tables.

**Method:** grepped logs for `413`, `PayloadTooLarge`, `request entity too
large`, `too large`, `file size`, `duration`, `exceeds`, `too long`, `chunk`,
`character`, `not satisfiable`, `280`, `rate`/`RATELIMIT`, and media-URL
`400`/`1363057`; joined `publish_job_targets.last_error` to
`publish_jobs.platform` and grouped by platform + error text.

**Caveats**
- `logs/worker.log` interleaves **pytest-captured** lines (e.g.
  `tests.test_job_logging:...`, synthetic `post_id=1/2/9/77/4242` fallbacks).
  Counts of *distinct production targets* below are taken from `logs/jobs/*.log`
  (which carry `job_id/target_id/platform/account_ref`) and the DB, not from
  raw `worker.log` line counts.
- Log timestamps are local; DB `finished_at` is UTC (8 h behind). Ignore the
  apparent 8 h offset when cross-checking.
- Several fixes are already in the working tree/deployed image (`_bluesky_shrink_image`,
  `THREADS_MAX_TEXT_CHARS` truncation, `_tiktok_chunk_plan`, `_assert_within_platform_caps`),
  so many failures below are **historical**; they are audited here because they
  remain in the logs and in `last_error`. See `logs/agent-notes.md`.

---

## Summary

| Platform | Limit / root cause | Distinct targets | Log events | Current DB state | Retry handling |
|---|---|---|---|---|---|
| **Bluesky** | 413 `PayloadTooLarge` — media blob over PDS ceiling (oversized image/video) | 8 | 44 job-log lines, 16 permanent failures | 7 succeeded after fix, 1 failed (MediaRestore) | Retried 3× (pointless pre-fix) |
| **Threads** | Caption > 500 chars (`HTTP 500 … at most 500 characters long`, code=100) | 5 | 15 job-log lines, 5 permanent | 2 failed rows remain (others overwritten by later duration error) | Retried 3× — **misclassified as HTTP 500** |
| **Threads** | Video duration > 300 s (499 s, 813 s) | 3 | 12 job-log lines (3 attempts + fallback each) | 3 failed | Retried 3× + fallback — deterministic, pointless |
| **TikTok** | Chunking / file size (`total chunk count is invalid`, `chunk size is invalid`, HTTP 416) | 6 | DB + job-4518 | 5 failed (May/Jun), 1 stuck `pending` | Mixed: old 3× retries; new code `retryable=False` |
| **Facebook** | Media-URL fetch 400 `code=389 error_subcode=1363057` | 40 | 131 job-log lines | 6 failed, 33 pending, 1 succeeded, 1 running | Retried 3×; **backlog will replay** |
| **Reddit** | `RATELIMIT` + `SUBREDDIT_NOTALLOWED_BANNED` + link whitelist | 5 | DB + job logs | 3 banned, 1 rate, 1 whitelist failed | Retried 3× (backoff shorter than Reddit's window) |
| **X (Twitter)** | Media-upload **credits depleted** `HTTP 402` (quota, not size) | many | ~100 `worker.log` events | not persisted as failed (fallback carries it) | Retried 3×, then Sociamonials fallback |
| **Instagram** | Opaque `400 …/media` + container "failed to process:" (empty message) | 11 | DB + job logs | 11 failed | Retried 3×; error body lost |

---

## Bluesky — HTTP 413 PayloadTooLarge

**Root cause.** The ATProto/PDS blob upload rejects media larger than the
service ceiling. The generic body `{'error': 'PayloadTooLarge', 'message':
'request entity too large'}` carries no filename, so it is only attributable by
the account/platform. Deep-dive in `agent-notes.md` ties it to an oversized
source artifact (the 602 MB / 813 s original) plus, for images, the ~900 KB
`app.bsky.embed.images` blob ceiling. `_bluesky_shrink_image` /
`_bluesky_shrink_video` now downscale before upload.

**Counts.**
- Distinct targets: **8** — jobs `2523, 2525, 2914, 2915, 3114, 3115, 3479, 3480`.
- Accounts: `118/120` (NW/SW Bluesky EN), `119/121` (NW/SW Bluesky ZH).
- Events: **44** job-log lines = **16 permanent failures** (8 targets × 2 rounds:
  Oct 4 ~21:00 and Oct 5 ~10:50–11:45) + 15 attempt-2 + 13 attempt-1 warnings.
- Resolution: 7 targets **succeeded** after the deploy (Oct 5 12:50–13:06 local);
  `3480` remains failed for an unrelated `MediaRestoreError` (permission denied).

**Examples.**
```
2026-10-04 21:02:16 | WARNING | [job_id=2914 target_id=2914 platform=bluesky account_ref=account:118 account_name=NW Bluesky EN attempt=1] target failed on attempt 1; retrying in 5.0s: PreparedPublishError: HTTP 413: {'error': 'PayloadTooLarge', 'message': 'request entity too large'}
2026-10-05 10:58:35 | ERROR | [job_id=3114 target_id=3114 platform=bluesky account_ref=account:118 account_name=NW Bluesky EN attempt=3] target failed permanently after 3 attempts: PreparedPublishError: HTTP 413: {'error': 'PayloadTooLarge', 'message': 'request entity too large'}
2026-10-05 11:45:03 | ERROR | [job_id=3480 target_id=3480 platform=bluesky account_ref=account:121 account_name=SW Bluesky ZH attempt=3] target failed permanently after 3 attempts: PreparedPublishError: HTTP 413: {'error': 'PayloadTooLarge', 'message': 'request entity too large'}
```
Also seen on the same path: `408 Client Error: Request Timeout for url: https://bsky.social/xrpc/com.atproto.repo.uploadBlob` (a large-blob symptom, not a limit per se).

---

## Threads — caption > 500 characters

**Root cause.** Threads caps post text at 500 chars. The API returns
`HTTP 500: Param text must be at most 500 characters long. code=100
type=THApiException` — a **client validation error reported as HTTP 500**, which
the worker treats as a transient server error. The code now truncates to
`THREADS_MAX_TEXT_CHARS` before the call.

**Counts.** Distinct targets: **5** — jobs `1919, 1930, 3302, 3695, 3859`
(accounts 42 `SW_threads`, 62 `NWthreads`). 15 job-log lines (5×3 attempts),
5 permanent failures. DB now shows 2 of these rows as failed with the char error
(`3695`, `3859`); `1919`/`1930` were later overwritten by the duration error.

**Examples.**
```
2026-09-18 02:07:04 | WARNING | [job_id=1919 ... platform=threads account_ref=account:62 attempt=1] target failed on attempt 1; retrying in 5.0s: PreparedPublishError: HTTP 500: Param text must be at most 500 characters long. code=100 type=THApiException
2026-09-22 21:25:17 | ERROR | [job_id=3302 ... account_ref=account:62 attempt=3] target failed permanently after 3 attempts: PreparedPublishError: HTTP 500: Param text must be at most 500 characters long. code=100 type=THApiException
2026-10-03 10:54:01 | ERROR | [job_id=3859 ... account_ref=account:42 attempt=3] target failed permanently after 3 attempts: PreparedPublishError: HTTP 500: Param text must be at most 500 characters long. code=100 type=THApiException
```

---

## Threads — video duration > 300 s

**Root cause.** Threads caps video at 300 s / 1 GB. The pipeline constrains
dimensions/fps/size but **not duration** (`agent-notes.md`), so the 813 s original
and a 499 s clip were submitted. Meta accepts the container then flips it to
`ERROR`; `_validate_threads_video_artifact` now probes locally and fails fast.

**Counts.** Distinct targets: **3** — jobs `1919, 1930` (499 s) and `4530` (813 s),
account 42/62. 12 job-log lines: 3 attempts + Sociamonials fallback per target.
All 3 permanently failed. The fallback also failed (`thrd video duration …` and
`HTTP 422 validation_failed`).

**Examples.**
```
2026-10-05 15:47:19 | WARNING | [job_id=4530 ... account:42 SW_threads attempt=2] target failed on attempt 2; retrying in 10.0s: PreparedPublishError: Threads video duration 813s exceeds the 300s limit; re-encode a shorter cut ...
2026-10-05 19:12:12 | ERROR | [job_id=1919 ... account:62 NWthreads attempt=3] target failed permanently after 3 attempts: PreparedPublishError: Threads video duration 499s exceeds the 300s limit; re-encode a shorter cut ...
2026-10-05 19:26:17 | WARNING | [job_id=1930 ... account:42 SW_threads attempt=3] sociamonials fallback failed; the target will be marked permanently failed. original error: ... duration 499s exceeds the 300s limit | fallback error: thrd video duration 499s exceeds the 300s limit
```

---

## TikTok — chunking / file size

**Root cause.** A 602 MB source plus an off-by-one chunk plan. TikTok computes
`total_chunk_count = floor(size/chunk_size)` with the remainder merged into the
last chunk; the old code used `ceil`, producing one chunk too many →
`invalid_params: The total chunk count is invalid`. A byte-range mismatch also
produced `HTTP 416 Range Not Satisfiable` on the resumable upload URL.
`_tiktok_chunk_plan` now uses floor and `TIKTOK_MAX_CAPTION_CHARS` /
`invalid_params` are marked `retryable=False`.

**Counts (DB failed targets).**
- `21` (account 90): `HTTPError: 416 Client Error: Range Not Satisfiable for url: https://open-upload-sg.tiktokapis.com/upload?...`
- `79` (account 100): `TikTok API error (invalid_params): The chunk size is invalid`
- `80, 81, 86` (account 100): `TikTok API error (invalid_params): The total chunk count is invalid`
- Recent `job-4518` (account 109 `Nakedwill_TK`): same `total chunk count is invalid`, logged **without retry**.

**Examples.**
```
2026-05-22 21:21:06 | ERROR | ... target failed permanently after 3 attempts: HTTPError: 416 Client Error: Range Not Satisfiable for url: https://open-upload-sg.tiktokapis.com/upload?upload_id=...
2026-07-03 13:58:16 | ERROR | ... target failed permanently after 3 attempts: PreparedPublishError: TikTok API error (invalid_params): The total chunk count is invalid
2026-10-05 14:23:09 | ERROR | [job_id=4518 ... account:109 Nakedwill_TK attempt=1] target failed without retry: PreparedPublishError: TikTok API error (invalid_params): The total chunk count is invalid
```

---

## Facebook — media-URL fetch 400 (`code=389 error_subcode=1363057`)

**Root cause.** Meta could not download the video from the URL we handed it
(`Unable to fetch video file from URL`). This is a media-URL accessibility
failure, not a numeric size cap — the URL must be public, reachable by Meta, and
correctly encoded. `agent-notes.md` attributes it to the R2/CDN URL (including a
raw-space encoding bug, since fixed by `do_spaces.cdn_url_for`) and media-URL
verification. Retrying cannot fix a permanently unreachable URL.

**Counts.** Distinct targets with the error in job logs: **40** —
`2741,2747,2756,2760,2779,2781,2795,2800,2807,2810,2816,2828,2831,3291,3299,3307,3315,3323,3331,3339,3347,3355,3363,3371,3379,3387,3395,3691,3700,3709,3718,3727,3736,3745,3754,3763,3772,3781,3790,3799,3808`.
131 job-log lines (retries). DB status of those 40 today: **6 failed, 33 pending,
1 succeeded, 1 running**. Accounts: `64` (SW-FB) and `11` (Nakedwill).
The 33 pending are a standing backlog that will replay the same 400.

**Examples.**
```
2026-10-02 21:42:53 | WARNING | [job_id=2816 ... platform=facebook account_ref=account:64 attempt=1] target failed on attempt 1; retrying in 5.0s: PreparedPublishError: HTTP 400: Unable to fetch video file from URL. code=389 error_subcode=1363057 type=OAuthException
2026-10-02 22:04:10 | ERROR | [job_id=3790 ... account:64 attempt=3] target failed permanently after 3 attempts: PreparedPublishError: HTTP 400: Unable to fetch video file from URL. code=389 error_subcode=1363057 type=OAuthException
2026-10-05 19:10:36 | ERROR | ... target failed without retry: PreparedPublishError: HTTP 400: Unable to fetch video file from URL. code=389 error_subcode=1363057 type=OAuthException
```
Related (same Meta fetch family, distinct causes): `HTTPError: 400 Client Error:
Bad Request for url: https://graph.facebook.com/v25.0/.../videos` (6) and
`PreparedPublishError: HTTP 400: … permission(s) must be granted …` (2).

---

## Reddit — rate limit + per-subreddit/link restrictions

**Root cause.** Community-level posting limits, surfaced by Reddit as per-submit
errors. The retry backoff (5 s / 10 s) is far shorter than Reddit's own window
(the message itself asked for 9–15 minutes), so every rate-limited target burns
all 3 attempts immediately. Banned/whitelist errors are permanent.

**Counts (limit-related failed targets in DB): 5**
- `RATELIMIT` ×1 — job `3865`, account 106, `r/gaybrosgonemild`:
  "Take a break for 9 minutes before trying again." (Sociamonials fallback also
  failed: no mapping for account 106.)
- `SUBREDDIT_NOTALLOWED_BANNED` ×3 — `r/GayBros`, account 106 (jobs `3834`, `3856`, `3888`).
- `SUBMIT_VALIDATION_LINK_WHITELIST` ×1 — `r/NudistMen`, link domain allowlist.

**Examples.**
```
2026-10-05 18:54:02 | WARNING | [job_id=3865 ... platform=reddit account:106 attempt=1] target failed on attempt 1; retrying in 5.0s: ... Reddit submit failed for r/gaybrosgonemild: [['RATELIMIT', "Looks like you've been doing that a lot. Take a break for 15 minutes before trying again.", 'ratelimit']]
2026-10-03 13:15:24 | ERROR | [job_id=3834 ... account:106 attempt=3] target failed permanently after 3 attempts: ... r/GayBros: [['SUBREDDIT_NOTALLOWED_BANNED', "You've been banned from contributing to this community", 'sr']]
... SUBMIT_VALIDATION_LINK_WHITELIST: 'The link must be from one of the approved domains: blogspot.com, freeimage.host, ... youtube.com.', 'link'
```

---

## X (Twitter) — media-upload credit exhaustion (`HTTP 402`) — adjacent quota, not size

**Root cause.** The X developer account is **out of API credits**; direct media
upload fails `HTTP 402` (`{"detail":"credits depleted"}`). Tokens are valid and
auto-refresh, so re-auth does not help — this is a paid-tier **quota** limit, not
a media-size limit. It shows up in `worker.log` as ~100 retry/failure events and
is the main driver of the Sociamonials fallback. It is **not** persisted in
`last_error` (the fallback marks the target succeeded), so it is invisible in the
failed-target table.

**Examples.**
```
2026-10-04 21:19:45 | WARNING | myUtils.worker:_handle_failure:785 - target failed on attempt 1; retrying in 5.0s: PreparedPublishError: X media upload (OAuth 2.0) request failed (HTTP 402) | details={"stage":"media upload (OAuth 2.0)","status":402,"code":""}
2026-10-04 21:20:12 | ERROR   | myUtils.worker:_handle_failure:768 - target failed permanently after 3 attempts: PreparedPublishError: X media upload (OAuth 2.0) request failed (HTTP 402) | details={"stage":"media upload (OAuth 2.0)","status":402,"code":""}
2026-10-05 11:21:25 | INFO | ... delivered via Sociamonials fallback (post_id=4242, network=blsk, status=delivered) ...
```
(No X 280-character or YouTube title-length failures were found in the logs.)

---

## Instagram — opaque 400 / container processing (secondary)

**Root cause.** `HTTPError: 400 Client Error: Bad Request for
https://graph.facebook.com/v25.0/<ig>/media` records only the status line — the
Graph API's actual `error_user_msg` (often a media-size/format/aspect-ratio
complaint) is **not captured**, so the true cause is unrecoverable from logs.
`Instagram container … failed to process:` is logged with an **empty** message.
2 permission errors and 1 container timeout are unrelated.

**Counts.** 11 failed targets (account 43, Jun 20; accounts 72/75, Sep):
6 × `.../media` 400, 2 × permission 400, 2 × empty container process failure,
1 × `not ready after 90s`.

**Examples.**
```
2026-06-20 16:44:50 | ERROR | ... failed permanently after 3 attempts: HTTPError: 400 Client Error: Bad Request for url: https://graph.facebook.com/v25.0/17841408190543669/media
2026-09-27 21:48:01 | WARNING | [job_id=2801 ... platform=instagram account:75 attempt=1] target failed on attempt 1; retrying in 5.0s: PreparedPublishError: Instagram container 17964907740188321 failed to process:
2026-09-27 14:09:15 | ERROR | ... account:75 attempt=3] target failed permanently after 3 attempts: PreparedPublishError: Instagram container 17964910383188321 failed to process:
```

---

## Cross-cutting findings

### Retried pointlessly (deterministic limits treated as transient)
- **Threads duration > 300 s** — input-level, deterministic, yet retried 3× with
  5 s/10 s backoff and then handed to a fallback that fails identically.
  `_validate_threads_video_artifact` raises `PreparedPublishError` without
  `retryable=False`, so the worker's default (`retryable=True`) applies.
- **Threads > 500 chars** — same: 5 targets × 3 attempts for a caption that never
  changes between attempts.
- **Facebook media-URL fetch 400** — 40 targets × up to 3 attempts; a dead URL is
  not transient. 33 targets are still `pending` and will repeat it.
- **Bluesky 413** — 8 targets × 3 attempts × 2 rounds before the downscale fix.
- **TikTok chunk errors (May/Jun)** — `attempts=3` in DB; now correctly
  `retryable=False` (`job-4518` logged "failed without retry").
- **Reddit `RATELIMIT`** — backoff (5/10 s) is orders of magnitude shorter than
  the limit window (9–15 min), so the retry is guaranteed to fail.

### Misclassified
- **Threads 500-char limit reported as `HTTP 500` (code=100).** It is a client
  validation error disguised as a server error, so it lands in the "retryable
  server error" bucket. It masked the real root cause for weeks (Sep 18 → Oct 3).
- **Facebook / Instagram raw `HTTPError: 400`** drops the response body, so the
  API's `error_user_msg`/`error_user_title` never reaches the log.
- **Instagram container `failed to process:`** is logged with an empty message;
  Meta gives no diagnostic.
- **X `HTTP 402`** is a quota/billing condition but is classified alongside
  generic prepared-publish failures; only the `details` JSON reveals the stage.

### Silent / masked
- **Threads caption truncation** now silently mutates post text to the 500-char
  cap (a `WARNING` only). Effective, but the operator's caption can be cut
  mid-word/emoji with no surfaced flag.
- **Sociamonials fallback can mask a platform limit as success.** Historic
  `agent-notes.md` records the worker marking targets succeeded on *submission*
  even when delivery later failed (`delivery_status=failed` for video posts);
  a follow-up commit added delivery verification. Any X 402 / Bluesky 413
  handled by the fallback therefore never appears in `publish_job_targets` as a
  failure — the platform limit becomes invisible to failure reporting.
- **Bluesky shrink is best-effort.** If Pillow/ffmpeg is missing or an encode
  fails, the original oversized path is used and the service's 413 surfaces only
  after all retries.

### Standing risk / anomaly
- **Facebook backlog:** 33 media-URL targets sit in `pending` with a URL Meta
  cannot fetch; they will replay the same 400 unless cancelled or re-plumbed.
- **`target_id=4518` state anomaly:** the job log records
  `target failed without retry: … The total chunk count is invalid` at
  2026-10-05 14:23:09, but the DB row is still `status=pending`, `attempts=0`,
  `last_error` empty (`started_at=2026-10-05T06:23:09`, no `finished_at`). The
  non-retry decision did not stick, so this deterministic chunk-limit failure
  will be (or has been) silently re-queued — see the `worker requeued … stale
  running target(s)` ticks at 15:24/15:43/16:43.

### Explicitly out of scope (not posting limits)
`MediaRestoreError`, `Permission denied: '/app/videoFile'`, missing/empty media
files, `NameError`/`get_browser_options()` code bugs, X browser-path
`TimeoutError … did not render all requested media attachments`, and
cookie-expiry lines in `logs/xiaohongshu.log` are infrastructure/auth issues,
not platform posting limits.

---

## Appendix — keyword distribution (raw grep, all log files)

| Keyword | Lines (approx.) | Where / meaning |
|---|---|---|
| `too large` | 88 | 100% Bluesky `PayloadTooLarge` |
| `500 characters` | 30 | Threads caption cap (job logs + worker.log(+1) duplicates) |
| `300s limit` | 28 | Threads duration cap |
| `chunk` / `chunk size` / `total chunk` | 4 DB rows + job-4518 | TikTok upload chunking |
| `Range Not Satisfiable` / `416` | 1 DB row | TikTok resumable PUT |
| `1363057` / `Unable to fetch video file from URL` | 131 job-log lines / 6 DB rows | Facebook media-URL fetch |
| `RATELIMIT` | 1 DB row | Reddit |
| `413` | 81 (`worker.log`) + 131 (`.1`) raw, incl. false positives | Mostly Bluesky; also incidental `.413` timestamps |
| `280` | 75 | **False positives** — millisecond timestamps (`.413`, `.280`), not X length |

No `HTTP 413`/`PayloadTooLarge` or size/duration-limit evidence was found for
Telegram, YouTube, nw_sw_blog, Douyin, Bilibili, Kuaishou, WeChat Channel,
Baijiahao, Xiaohongshu, or the offload pipeline.
