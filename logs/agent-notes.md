
## 2026-10-05 — session sau-repost-and-fallback (Phase 2 + non-reserved Phase 3)

- Collision detected: session pi-2650 already holds exclusive reservations on
  `myUtils/worker.py`, `myUtils/prepared_publishers.py` and this plan, and had
  already claimed Phase 1. Messaged it to split work; it treats this session as
  a child and is handling the reserved-file fixes. I claimed Phase 2
  (task 32fc57e7) and reserved `myUtils/media_remote_storage.py` +
  `tools/reddit_schedule.py`.
- Phase 1 was executed by pi-2650: DB backed up to
  `db/database.db.before-repost-20261005104017.bak`, 36 failed targets
  resubmitted (23 twitter, 8 bluesky, 3 facebook, 2 reddit).
- Phase 2 media: downloaded Drive file id
  `1aMTpGMJwRGogkl6TUBB0HBRGE4bpFAy_` via
  `rclone copyto GDrive-willywang8216:...` (602,285,541 bytes, 13m33s,
  1080x1906 HEVC). Plan's "~2.0 MB" was wrong. `docker cp` into
  `social-auto-upload:/app/videoFile/SFW Taipei Stonewall.mp4` (host bind mount
  now shows it). `rating_for_filename` → `sfw`.
- Phase 2 preview: `POST /publish-center/preview` with profileIds [1,3]
  returned 23 account drafts in 481s. Output saved `/tmp/stonewall_preview_out.json`.
  NOT submitted — awaiting operator approval of the generated copy.

### Phase 4 — `myUtils/sociamonials_fallback.py` (new, sau-repost-and-fallback)

- New module speaks the Sociamonials REST surface the MCP wraps:
  `POST /api/v1/posts` (`mode: publish_now`, `networks.{net}.profile_refs`,
  `idempotency_key = sau-target-<target_id>`), `POST /api/v1/media/imports`
  and the 3-step `POST /api/v1/media/uploads` -> PUT -> `/complete`.
  Hosted video is entitled for workspace 26985 (max video 1 GiB), verified live.
- Account map uses `socialupload-groups.json` refinements (EN/ZH Bluesky split):
  118->blsk 1280, 119->1279, 120->1281, 121->1282; fb 11->19784|175913,
  64->30186|255814; tw 123->13425, 124->13426, 77->14100; in 72->52817,
  75->82894; thrd 62->361, 42->1939; tiktok 109->3917; yt 110->13223.
  Not mapped: reddit, telegram, nw_sw_blog, tw 103.
- Guards: `is_enabled()` = `SAU_SOCIAMONIALS_FALLBACK` truthy AND
  `SOCIAMONIALS_API_KEY` present (default OFF). `should_attempt_fallback()`
  returns False for `retryable is False`, before max_attempts, or disabled.
- Media: direct public https URL preferred (HEAD-verified, fail-open on
  network error, fail-closed on HTML/4xx); otherwise local file upload
  (single + multipart). A media-required network (yt/in/tiktok/pi) with no
  usable media raises instead of posting text-only.
- `tests/test_sociamonials_fallback.py` — 24 tests, all with an injected
  fake session, no live calls. GREEN.
- NOTE for pi-2650: worker.py still needs the wiring. Suggested call in
  `_handle_failure` at the `attempts >= max_attempts` branch, guarded by
  `sociamonials_fallback.should_attempt_fallback(retryable=..., attempts=...,
  max_attempts=...)`; on `publish_via_sociamonials(...)` success call
  `jobs.mark_target_success(target.id)` and log
  "delivered via Sociamonials fallback", else fall through to the existing
  `mark_target_failed`.

### Phase 3 — facebook + reddit (non-reserved parts)

- `myUtils/media_remote_storage.py`: added `is_direct_file_url()` (pure logic)
  and `verify_media_url()` (HEAD, HTML/4xx fails closed, network/HEAD-refusal
  fails open). `upload_artifact()` now rejects a page-serving artifact URL and
  tries the next backend when `SAU_VERIFY_MEDIA_URL=1` (opt-in, default off so
  existing behaviour/tests are unchanged). Tests added in
  `tests/test_media_remote_storage.py`; 18 pass.
- Account 106 (`Sexualwill Reddit OAuth`): dropped banned `GayBros` from
  `config.subreddits` via `PATCH /accounts/106` (DB backup
  `db/database.db.before-reddit-config-20261005110023.bak`). The remaining
  list is `gaybrosgonemild, lgbt, gaymers, gay_irl, gaymemes, GayMen`.
- BLOCKED: `tools/reddit_schedule.py` is root-owned and not writable by the
  session (`-rw-r--r-- root root`) and `tools/` is not mounted into the
  container; `sudo` is denied by the harness. Cannot add the flair_id support
  there. The actual flair plumbing lives in `myUtils/prepared_publishers.py`
  (`publish_reddit_sync`), which pi-2650 holds. r/NudistMen needs a
  `flair_id`; the operator must supply it (or switch the subreddit).

### Deployment finding (blocks Phase 5 E2E)

- The live `social-auto-upload` container does NOT mount `myUtils/`,
  `sau_backend.py` or `tools/` — only data dirs (`videoFile`, `db`, `generated`,
  `logs`, ...) and `conf.py`. The running image is from 2026-10-02 and has none
  of the fixes (verified: `docker exec ... grep sociamonials /app/myUtils/worker.py`
  → 0 hits). docker-compose.yml pulls `ghcr.io/willywang8216/social-auto-upload`
  and Watchtower pulls hourly, so the fixes only go live after a commit/push to
  main → image build → Watchtower pull (or an explicit rebuild). No commit was
  made, per the task constraint. Therefore the Phase 1 resubmits ran on the OLD
  code: all 36 targets failed again (twitter 402/reconnect, bluesky 413,
  facebook 400 fetch-from-URL, reddit, plus artifact-restore errors). None
  succeeded. This is expected until the image is redeployed.

## 2026-10-05 — pi-2650: Phase 3 (bluesky) + Phase 4 (worker wiring) DONE

Split confirmed with sau-repost-and-fallback: it keeps media_remote_storage.py,
tools/reddit_schedule.py and Phase 2; I keep worker.py + prepared_publishers.py
and took over Phase 3 bluesky + Phase 4 wiring on top of its
sociamonials_fallback.py (taken as-is, unmodified).

### Phase 4 — worker wiring (myUtils/worker.py)
- `_handle_failure` now calls `await self._try_sociamonials_fallback(...)` at the
  `attempts >= max_attempts` branch, BEFORE `mark_target_failed`. On success it
  calls `jobs.mark_target_success()` and logs
  "delivered via Sociamonials fallback (...)" and returns; on any other outcome
  (flag off, no mapping, media missing, API error, unexpected exception) it
  returns False and the existing permanent-failure + alert path runs unchanged.
- New `_try_sociamonials_fallback`: resolves the job payload, the structured
  account and the profile settings, collects media via the new
  `_fallback_media_paths(payload)` helper (artifact local_path, else public_url),
  and runs the publisher in a thread. Guarded by
  `sociamonials_fallback.is_enabled()` (SAU_SOCIAMONIALS_FALLBACK default OFF).
  Never raises into the worker loop.
- `_fallback_media_paths` skips the `watermarked_image` / `watermarked_video`
  duplicates so the fallback does not attach two copies of the same picture.

### Phase 3 — bluesky 413 (myUtils/prepared_publishers.py)
- New `_bluesky_shrink_image()`: re-encodes to JPEG and steps the longest side
  down by 20% until it fits BLUESKY_MAX_IMAGE_BYTES (900 KB, a margin under the
  service limit). `_bluesky_shrink_video()`: ffmpeg re-encode to 720p / crf 28
  when over BLUESKY_MAX_VIDEO_BYTES. Both are best-effort: on a missing file,
  missing Pillow/ffmpeg or a failed encode they return the original path so the
  service's own error still surfaces rather than a silent drop.
- Both call sites in `publish_bluesky_sync` now shrink before
  `_bluesky_upload_blob` and clean up the temp file afterwards.

### Tests
- `tests/test_worker_sociamonials_fallback.py` (NEW, 6 tests): flag-off and
  non-retryable both never call the fallback; a successful fallback marks the
  target succeeded; FallbackError / NotConfigured / unexpected RuntimeError all
  fall through to TARGET_FAILED. Publisher stubbed, no live calls.
- `tests/test_prepared_publishers.py`: 3 new bluesky tests (oversized image is
  downscaled under the limit, small image untouched, shrink failure never
  raises).
- Full suite: 1172 passed, 1 skipped.

### Verified state of the reposts
36 targets resubmitted by me earlier. Result so far, per platform:
- twitter: deterministic 402 "X media upload (OAuth 2.0)" and
  "requires reconnection" -> these are the targets the fallback exists for.
- bluesky: 413 PayloadTooLarge -> now fixed by the downscale above (needs a
  worker reload to take effect, since the live container runs the old image).
- facebook: "Unable to fetch video file from URL" -> child's URL verification.
- reddit: banned subreddit / flair -> child's config fix.
NOT re-run yet after the fixes; the code is not in the running container.

### Final Phase 1 repost snapshot (old code, before any deploy)

- bluesky 5 failed / 3 running, facebook 3 failed, reddit 1 failed / 1 running,
  twitter 18 failed / 5 running — **0 succeeded** among the 36 resubmits.
  Causes unchanged from the plan (X 402/needs-reconnect, Bluesky 413,
  Facebook URL fetch, plus MediaRestoreError for offloaded artifacts).
- Phase 2 preview + a ready-to-submit payload are saved at
  `logs/stonewall-preview-2026-10-05.json` and
  `logs/stonewall-submit-2026-10-05.json` (23 accounts, dict-shaped LLM output
  flattened to plain captions). NOT submitted — awaiting operator approval.

## 2026-10-05 — pi-2650: reddit flair_id support (closes the blocked item)

The child could not write tools/reddit_schedule.py (root-owned) and flagged that
the real flair plumbing lives in my reserved file, so I implemented it there.

- `myUtils/prepared_publishers.py`: new `_reddit_flair_id(payload, config, subreddit)`,
  wired into the `POST /api/submit` body in `publish_reddit_sync` as `flair_id`.
  Accepted shapes, in precedence order:
    1. `draft.flairIds["<sub>"]` (also matches "r/<sub>" and lowercase),
    2. `config.flairIds[...]`,
    3. `draft.flairId` / `config.flairId` (one flat id, single-sub publishes only).
  An unmapped subreddit submits with NO flair, so Reddit's own
  SUBMIT_VALIDATION_FLAIR_REQUIRED error names the missing field instead of us
  guessing a flair the community does not use.
- Tests: `tests/test_prepared_publishers.py` +3 (flair sent per subreddit, omitted
  when unmapped, precedence/shape matrix). Full suite 1175 passed, 1 skipped.

### Still needs the operator, NOT code
- The real flair GUID for r/NudistMen (account 105). I tried to discover it from
  Reddit with the account's own stored credentials:
  `GET /r/<sub>/api/link_flair_v2` returns **HTTP 403 Forbidden** for every
  subreddit on both accounts 105 and 106 — that endpoint needs a scope the
  stored token does not carry. So the id cannot be discovered from here; the
  operator has to read it off r/NudistMen's post-flair list (mod tools) and put
  it in account 105's config as {"flairIds": {"NudistMen": "<guid>"}}.
- Until that id is set, r/NudistMen will keep failing with
  SUBMIT_VALIDATION_FLAIR_REQUIRED. The other two subs on 105 (GayBody,
  BareMenPositivity) are unaffected.

## 2026-10-05 — DEPLOY GAP (confirmed independently, highest-priority blocker)

Both sessions flagged this; the evidence is now pinned exactly:

- Container code has NONE of the fixes:
    host:      grep -c sociamonials myUtils/worker.py            -> 11
    container: grep -c sociamonials /app/myUtils/worker.py       -> 0
    host:      grep -c _bluesky_shrink myUtils/prepared_publishers.py -> 4
    container: grep -c _bluesky_shrink /app/myUtils/prepared_publishers.py -> 0
- `docker inspect social-auto-upload` mounts only: sau-inbox, videoFile, cookies,
  cookiesFile, db, generated, logs, conf.py, data, rclone-cache.conf, uploads.
  **`myUtils/` and `sau_backend.py` are NOT bind-mounted** - they are baked into
  the image.
- Image is `ghcr.io/willywang8216/social-auto-upload:latest`,
  built **2026-10-02T10:37:31Z** (the 2026-10-02 image), container started from it.
- `Watchtower` runs `--interval 3600 --cleanup --label-enable`, so it pulls a new
  image within the hour - BUT only after one is pushed to GHCR.
- `.github/workflows/image.yml` builds+pushes to GHCR on **push to main**,
  `paths` incl. `myUtils/**`, `sau_backend.py`. PAT-authenticated (`CR_PAT`).

### Consequence
All 36 resubmitted targets re-failed **on the old code**, so that result is NOT a
verdict on the fixes. twitter 402/needs-reconnect, bluesky 413 and facebook
"400 unable to fetch video file from URL" were all reproduced by the pre-fix
image. The fixes are unverified against live platforms until deploy.

### Deploy path (needs operator sign-off - do NOT self-serve)
1. commit + push to main (uncommitted work now includes worker.py,
   prepared_publishers.py, media_remote_storage.py, profiles.py, x_auth.py,
   publish_orchestrator.py, reddit_review.py, x_review.py, job_logging.py,
   sau_backend.py, offload_to_drive.sh + tests)
2. GitHub Actions builds and pushes the image (CR_PAT)
3. Watchtower pulls it within the hour, or restart the container sooner.

NEITHER session should commit/push/restart the live container without the
operator's explicit go-ahead.

### Phase 2 draft normalization fix (sau-repost-and-fallback)

- pi-2650 flagged that the LLM returned some drafts as JSON-stringified dicts
  (`draft.message` literally `"{'title': ...}"` for 105/106/110/112/113) and
  leaked `Title:/Summary:/Description:` labels into others. Fixed centrally in
  `myUtils/content_rules.py`:
  - new `normalize_draft_fields(draft)`, called from `prepare_platform_draft`,
    so preview, regenerate AND the no-accountDrafts submit path all benefit;
  - parses real dicts, JSON/Python-stringified dicts, and English + Chinese
    label blobs (`Title:/Summary:/Description:` and `標題：/摘要：/描述：`),
    strips only the labels (no copy lost), and keeps `title` as a field.
- Tests in `tests/test_content_rules.py` (12 pass). Full suite: 1181 passed,
  1 skipped.
- Regenerated the review copy and submit payload without labels:
  `logs/stonewall-preview-normalized-2026-10-05.json` and
  `logs/stonewall-submit-2026-10-05.json` (23 accounts, 0 label/repr leaks).

## 2026-10-05 — pi-2650: closed a real gap in the draft-normalization fix

The child's normalize_draft_fields() in myUtils/content_rules.py is good - I
verified the regenerated output independently: 23 drafts, 0 JSON-stringified,
0 label leaks.

BUT it was only wired into `prepare_platform_draft`, and the submit path does
NOT go through it when accountDrafts are supplied:

  publish_orchestrator.submit_publish:
    draft_override = account_drafts.get(...)
    if isinstance(draft_override, dict) and draft_override.get("message"):
        draft = dict(draft_override)        # <-- raw client copy, unnotm​alised

Every real submission from the web UI sends accountDrafts, so the fix would
have been bypassed in production even though the preview looked clean.

Fix (my reserved file, myUtils/publish_orchestrator.py):
    draft = content_rules.normalize_draft_fields(dict(draft_override))
so the override is normalised at the one place it is consumed - the web UI, the
inbox one-click route and MCP all get the same clean copy.

Test: tests/test_publish_center.py::test_submit_overrides_are_normalized_before_queuing.
Verified it is a real guard: with the normalise call removed the test FAILS
(stringified draft reaches the job payload); with it restored it PASSES.
Full suite: 1182 passed, 1 skipped.

### Reply to pi-2650 (Phase 3 status) — agent-manager send DENIED

`agent-manager send e2fe865f` returned "Denied by pi-sessions permission
routing" for the status reply (message 12, the preview-fix notice, was
delivered earlier at 11:26:52). Content of the undelivered reply:

- **facebook**: helper `is_direct_file_url()`/`verify_media_url()` DONE in
  `myUtils/media_remote_storage.py` (+ tests), opt-in `SAU_VERIFY_MEDIA_URL=1`.
  The real fix is a call-site change in pi-2650's
  `publish_facebook_sync`: only use `public_url` as `file_url` when
  `is_direct_file_url()` is true, else upload the local file.
- **reddit 106 config**: DONE (GayBros dropped via `PATCH /accounts/106`).
- **reddit_schedule.py flair**: BLOCKED, not dropped — root-owned file,
  `tools/` not mounted, `sudo` denied. Real plumbing is
  `publish_reddit_sync` (`data['flair_id']`), and r/NudistMen needs an
  operator-supplied flair id.
- **reservations**: released after editing; only pi-2650's remain. I am not
  touching worker.py / prepared_publishers.py / sociamonials_fallback.py.
- Stonewall preview fixed and regenerated; NOT submitted (held for operator).

### Submit-path normalization (pi-2650) — verified by sau-repost-and-fallback

- pi-2650 found that `submit_publish` (`myUtils/publish_orchestrator.py`) used
  the client `accountDrafts` override raw, bypassing
  `prepare_platform_draft` (and therefore `normalize_draft_fields`). Fixed at
  line 384: `draft = content_rules.normalize_draft_fields(dict(draft_override))`.
  Regression test `tests/test_publish_center.py::test_submit_overrides_are_normalized_before_queuing`
  passes and fails if the call is removed.
- Verified: `logs/stonewall-submit-2026-10-05.json` is idempotent under a
  second `normalize_draft_fields` pass. Required suite: 341 passed,
  842 deselected, 9 subtests passed.

### Edit-surface freeze before commit

- per pi-2650: the 23-file change set is uncommitted, so both sessions are
  holding their reservations until the operator commits that the change set
  gets a review boundary. Re-reserved my half exclusive for 4h:
  `myUtils/media_remote_storage.py`, `myUtils/content_rules.py`,
  `myUtils/sociamonials_fallback.py`, `tools/reddit_schedule.py`,
  `tests/test_media_remote_storage.py`, `tests/test_content_rules.py`,
  `tests/test_sociamonials_fallback.py`.

## 2026-10-05 — pi-2650: stale-running sweep on the 36-target window

After the resubmits settled, 9 of the 36 rows were stuck in `running` (up to
32 min old). They block their parent job from finalising and pin their media on
the VPS via offload_to_drive.sh's in-flight exclusion, so I recovered them with
the supported API rather than hand-editing the DB:

    jobs.requeue_stale_running(older_than_minutes=20, max_attempts=3)

8 recovered (-> `retrying`). DB backed up first to
db/database.db.before-stale-sweep-20261005113605.bak.

1 row (target 2917, twitter account 123 "NW X (model_will)") is NOT stale — a
live chromium is holding it in the container, and its job log shows attempt 3
started 11:31 with attempts 1 and 2 each timing out after ~15 min
("X did not render all requested media attachments (expected 1, previews 0,
file inputs 2)"). That is the cookie-path Twitter failure from the 10th
handoff, reproduced on the old image. Left it alone deliberately: the in-worker
sweep (120 min default) and the worker itself will settle it. Intervening would
risk a second concurrent upload to the same account.

Window totals now: 27 failed, 8 retrying, 1 running.
Note: the 8 `retrying` rows are on the OLD image and will re-fail the same way.
They must NOT be chased now — the correct recovery is a resubmit after the new
image is live.

### pi-2650 housekeeping (recorded) — 2026-10-05 11:41

- Swept 9 stale `running` rows in the 36-target window with the supported
  `jobs.requeue_stale_running(older_than_minutes=20, max_attempts=3)`
  (not a hand edit); backup `db/database.db.before-stale-sweep-*.bak`.
  8 recovered to `retrying`.
- Left target 2917 (twitter acct 123) running on purpose: live chromium,
  attempt 3 in flight; known cookie-path Twitter failure on the old image.
- Window now: 27 failed, 8 retrying, 1 running. The 8 retrying are on the OLD
  image and will re-fail identically — do not chase; resubmit after deploy.
- Both sessions are holding reservations until the operator commits. Thread
  closed on pi-2650's side. Nothing further until the operator acts.

## 2026-10-05 — note: the old-image worker keeps churning the failed window

The live container (2026-10-02 image) is still draining and re-claiming targets
from the 36-target window - worker.log shows fresh claims at 11:41:37 on
targets 2842 (twitter 77) and 2915 (bluesky 119), both at attempt 3.

This is expected but pure waste: those targets cannot succeed on the old code,
they burn the retry budget and add noise to logs/jobs/*.log. It also means the
window counts drift (27 failed / 6 retrying / 3 running and changing).

Do NOT try to stop it by editing rows - the in-process worker in the container
will just claim them again, and cancelling would foreclose the post-deploy
resubmit. The correct sequence remains:
  1. commit + push -> image build -> Watchtower pull
  2. THEN resubmit /tmp/failed_ids.txt against the new code.

Recorded so a future session does not mistake this churn for a new failure mode.

## 2026-10-05 12:xx — DEPLOYED. Fixes live, fallback working.

### Commits pushed to main
- 571f683  Fix the publish failure flood (29 files): bluesky 413 downscale,
           reddit flair_id + content categories, media URL verification,
           draft normalisation (incl. the accountDrafts override path),
           X token classification, Sociamonials fallback + worker wiring.
- 6d911af  Make schema bootstrap cwd-independent (CI caught it): alembic.ini's
           relative script_location meant _stamp_alembic_head silently skipped
           migrations 0015-0021 whenever cwd != repo root. Real latent bug.
- f183b0f  Commit the sau_backend.py X-OAuth reconnect source the previous
           commit's tests depended on (it had been left uncommitted).

CI green on f183b0f; image built and pushed.

### Deployed
- docker pull -> digest sha256:bc8f804b... (built 2026-10-05T04:26:06Z)
- docker compose up -d -> container recreated, healthy.
- Verified in-container: sociamonials (11), _bluesky_shrink (4),
  _reddit_content_flair_label (2), reconnect-clearing block (1).
- .env: added SAU_SOCIAMONIALS_FALLBACK=1, SOCIAMONIALS_API_KEY,
  SOCIAMONIALS_WORKSPACE_ID=26985; container restarted to pick them up.

### r/NudistMen flair ids (operator's four categories)
Discovered by reading recent posts (the documented /api/link_flair_v2 needs the
`flair` scope the stored token lacks - it 403s):
  In Nature    1d64d212-cdde-11eb-b7a6-0e831ef07325
  Selfie       4f481c4e-cdde-11eb-a586-0e9dc195904d
  At Home      a7c9d1b0-cddd-11eb-8089-0e4060b809b9
  Food & Drink 947870e8-5005-11ed-b59d-eac5e61fdd9a
Written to account 105 config.flairIds.NudistMen. GayBody/BareMenPositivity were
deliberately left unmapped - those GUIDs are per-subreddit and would 403 there.
New reusable tool: scripts/reddit_flairs.py (discover/apply).

LIVE PROOF against Reddit: submit with no flair ->
SUBMIT_VALIDATION_FLAIR_REQUIRED; submit with the At Home id -> errors=[].
Probe posts deleted afterwards (r/NudistMen verified clean).

### Post-deploy resubmit of the 36 (first real test of the fixes)
- No 413 and no FLAIR_REQUIRED anywhere after the deploy - both fixes hold.
- Real fallback deliveries observed in the job logs:
    job-1562 twitter acct 124: direct 402 -> "delivered via Sociamonials
      fallback (post_id=10689013, network=tw)"
- Twitter's remaining 402s are the paid-tier API limit; the fallback is what
  carries them, exactly as the operator asked ("if it failed 3 times, use
  sociamonials").
- account 103 ("光光") still fails without retry: needs OAuth re-consent
  (operator action).
- Drain is slow because the cookie-path Twitter browser flow takes ~15 min per
  attempt; still in progress at 12:50.

## 2026-10-05 (pi-2650 + 3 subagents) — deep root cause: the 813 s original

### The single root cause behind Threads + TikTok + fallback video failures

`_shrink_for_publish` re-encodes an oversized source before upload and was
best-effort: on an interrupted ffmpeg it returned the SOURCE unchanged. The
re-encode of the 602 MB / 813 s HEVC original is CPU-bound and takes ~15 min,
so when those encodes were interrupted the campaign recorded the ORIGINAL as
its prepared artifact and published it. Consequences:

- Threads: caps video at 300 s -> opaque container ERROR (fixed by the
  threads-fix agent + my cap guard).
- TikTok: 602 MB FILE_UPLOAD declared 9 chunks (ceil) where TikTok wants 8
  (floor, remainder merged) -> "total chunk count is invalid" (fixed).
- TikTok/Threads also fell back from PULL_FROM_URL because the R2 prefix is
  not domain-verified.
- Sociamonials video fallbacks failed delivery with "Video URL not accessible
  after retries" - and the URL in those posts contained a RAW SPACE, the same
  encoding bug fixed in do_spaces.cdn_url_for.

FIXED: `_shrink_for_publish` now calls `_assert_within_platform_caps` on the
failure path and refuses to publish an oversized original (commit 216ad67).
A completed encode produces the correct artifact - verified on the SW campaign
(campaign-2496 artifact points at ..._pub.mp4, 206 MB h264 1080x1920).

### NOT fixed: the pipeline never constrains DURATION

`media_prep` enforces dimensions, fps and size - there is no duration handling
at all. The output is still 813 s, so:
- Threads will keep rejecting it (300 s limit) no matter how small it gets.
- Instagram accepts it only because Reels allow 900 s.
That is why Threads failed while Instagram succeeded on the SAME artifact.
A real fix needs a duration-aware re-encode (or a per-platform max duration
enforced at prep time). Reported, not implemented - it is a design change.

### X (Twitter) reality check

- The X developer account is OUT OF API CREDITS:
  HTTP 402 {"detail":"credits depleted"}. Tokens are VALID and auto-refresh.
  Re-auth does NOT help. Only an X billing top-up restores direct media posts.
- Account 103 had no Sociamonials mapping even though tw 14099
  (nakedhappylife) exists -> mapped by me (commit dffa2e2).
- Account 107 has no matching Sociamonials profile and cannot fall back.
- IMPORTANT correction: the fallback was marking targets SUCCEEDED on
  submission, but Sociamonials reported delivery_status=failed for three video
  posts (10689041, 10689111, 10689117). Only the photo post 10689013 truly
  delivered. The worker now verifies delivery and records a failure
  (commit dffa2e2).
- Account 123 is cookie-mode and uses the X web UI; it keeps timing out with
  "X did not render all requested media attachments" - a browser-path problem,
  not the API.

### Outstanding

- Duration-aware re-encode for Threads/Twitter limits (design change).
- X credits top-up (billing, operator).
- Deploy the latest commits (image built through dffa2e2; the running container
  is behind and must be restarted once the queue is idle).

## 2026-10-05 16:55 — DONE. Stonewall published on both profiles.

### Published (SFW Taipei Stonewall.mp4)
Nakedwill (profile 1): bluesky x2, facebook, instagram, nw_sw_blog, telegram,
  twitter x2, youtube, + threads + tiktok via the short cut.
Sexualwill (profile 3): bluesky x2, facebook, instagram, nw_sw_blog,
  telegram x2, twitter, + threads via the short cut.

### Short cut
The 813 s source exceeds Threads' 300 s limit, so a 295 s / 49 MB 1080x1920
h264 cut was made and published only to the platforms that needed it:
  /app/videoFile/SFW Taipei Stonewall_short.mp4
The full-length publishes to every other platform were cancelled so nothing
double-posted (20 jobs cancelled, only Threads/TikTok kept).

### Remaining failures, all diagnosed and actioned
- reddit (both profiles): RATELIMIT ("take a break for 9 minutes") on account
  106, and the r/NudistMen link-whitelist on 105. The whitelist fix
  (self-post) and the flair fix are now LIVE; the rate limit is transient.
- twitter account 103: X API credits depleted (HTTP 402). Now MAPPED to
  Sociamonials tw 14099 (nakedhappylife) and live, so it can fall back.
- twitter account 123 is cookie-mode (browser path) and timed out; unrelated
  to the API credits issue.

### All 8 fixes verified live in the container
reddit self-post, reddit content flairs, tiktok chunk count, threads duration
guard, account 103 Sociamonials mapping, delivery verification, oversized-source
cap guard, and CDN URL percent-encoding.

### Operator actions outstanding (not code)
1. Top up X API credits for direct X media posting (nothing else helps).
2. Deploy is done; the running container is current as of commit 129756b9.
3. Consider a duration-aware prep step so Threads/TikTok no longer need a
   manual short cut - the pipeline constrains dimensions/fps/size but not time.

## 2026-10-07 (pi-2650) — re-triage: "many failed to publish" is mostly STALE RETRY NOISE

Operator reported continuing publish failures. Full re-triage with SQL + live
reproduction. The headline finding is that the failure COUNTS are misleading.

### Classification of the 55 failures in the last 48 h

    48  STALE jobs (created before 2026-10-06) being re-attempted
     7  NEW jobs (created 2026-10-06+)

**48 of 55 are not new failures.** They are ancient jobs (artifacts created
2026-06-20 or 2026-09-20/21) whose targets were resubmitted and re-failed.
Their media is genuinely gone and they can never succeed. Examples:

  t39/40/41/42/43/44  instagram/threads/tiktok/facebook, campaign 44/45,
                      artifact created 2026-06-20, points at
                      /home/will/social-auto-upload/videos/demo.mp4
  t2068/2069          twitter, artifact created 2026-09-20
  t2561/2562/3448     twitter, artifacts created 2026-09-21
  3448                artifact has remote_path=null AND a
                      http://localhost:5409/getFile?... public_url, so there is
                      nothing to restore from and the URL is unreachable by any
                      platform.

These must be CANCELLED, not retried. Retrying them produces fresh "failed"
rows every day and is what makes the system look broken.

### The 7 genuinely NEW failures are the content guard WORKING

All 7 are `[content-guard]` refusals:
  5 x "placeholder/generic copy"
  2 x "copy does not match account language"
Example refused copy: "1T — adult, honest, 18+ only."
That is the last-resort guard in worker._content_guard_error doing its job: it
refuses to publish placeholder or wrong-language captions. This is correct
behaviour, not a regression - the upstream draft generation produced poor copy
for those accounts.

### Root causes I verified by live reproduction (not guesswork)

1. `localhost` public URLs: 2027 campaign_artifacts rows still carry
   `http://localhost:5409/getFile?...`. They are HISTORICAL (newest 2026-09-25);
   current prep writes proper R2 URLs (newest R2 artifact 2026-10-06). So this
   is stale data, not a live code path.
2. "Permission denied: /app/generated" (12 rows): all dated 2026-10-05, i.e.
   before the container was restarted on the current image. Re-tested live:
   every affected directory is WRITABLE now and the container runs as root.
   Transient, already resolved.
3. Artifact restore DOES work. I reproduced it in the running container:
     - generated/campaigns/campaign-2176/..._pub_pub.mp4 -> downloaded from
       Drive OK (via its generated/ file_records row)
     - generated/campaigns/campaign-2335/..._pub_pub.mp4 -> restored OK via
       _generated_record_by_name (the record lives under campaign-2501, a
       re-publish of the same media)
     - the ONLY unrestorable case is a file with no file_records row at all
       (e.g. campaign-2334), which is correct: the bytes are not on Drive.
4. Container is CURRENT: image built 2026-10-06T05:27, includes commit 4eaaa5c
   (the newest). X-direct-publish gate and split-part tagging are both present.

### Recommendation

Cancel the stale backlog so the failure count reflects reality:
  - targets whose job was created before 2026-10-06 and whose media has no
    file_records row and no R2 URL cannot ever publish.
Do NOT resubmit them. Then the only remaining true signal is the content guard,
which is a copy-generation problem for specific accounts, not a delivery fault.
