# Audit — per-platform caps and video adjustment (compress / split / multi-post)

Date: 2026-10-09 · Read-only audit; no code changed.
Repo: `/home/will/social-auto-upload` @ `24bfcc5` (branch `main`).
Scope: `myUtils/platform_limits.py`, `myUtils/media_prep.py`,
`myUtils/media_pipeline.py`, `sau_backend.py` (`_artifact_payloads_for_platform`,
`_select_videos_for_platform`, `_artifact_part_groups_for_platform`),
`myUtils/campaign_media_prep.py` (split-plan builder), plus the publisher
enforcement in `myUtils/prepared_publishers.py` needed to answer "what happens
to an over-cap video".

## Commands actually run

```
python -m pytest tests/ -q -k "platform_limits or media_prep or split"
  -> 89 passed, 1348 deselected, 3 subtests passed in 17.51s

# DB evidence
sqlite3 db/database.db  (via .venv/bin/python, see below)
  - accounts: 11 platforms present (bluesky, facebook, instagram, nw_sw_blog,
    reddit, teaching_blog, telegram, threads, tiktok, twitter, youtube)
  - campaign_artifacts WHERE part_index IS NOT NULL:
      split_for=["twitter"] -> 38, split_for=["threads"] -> 3
  - campaign 2555: posts/artifacts/jobs/targets inspected
  - campaign 2517 (5-part twitter split) and 2520 (3-part) inspected

# Runtime probes (no code changed)
  _enforce_video_limits(twitter, 999s)            -> retryable=True
  _validate_threads_video_artifact(600s)          -> retryable=True
  _artifact_payloads_for_platform(..., "tiktok")  -> [raw id]  (split parts ignored)
  should_shrink({1080x608,30fps,50MB}, instagram) -> False  (no aspect check)
  media prep 305 MB vs instagram                  -> prep: fits; publisher: refuses
```

`python` is not on PATH in this shell; all Python was run as
`.venv/bin/python`.

## Per-platform table

`code` = `myUtils/platform_limits.py` (dur = `VIDEO_MAX_SECONDS`, size =
`MEDIA_MAX_MB`). `real` = the API figure this repo actually talks to, per
`docs/platform-posting-limits.md` / `logs/platform-limits-research.md`.
Over-cap behaviour is what the code does today for a video **over the code
value**.

| platform | cap in code (dur / size) | real published cap (API) | over-cap behaviour | gap |
|---|---|---|---|---|
| **twitter** | 140 s / 512 MB (`platform_limits.py:54,70`) | API default 1,200 s / 8 GB; free web 140 s / 512 MB | **(a) split** into 140 s parts, tagged `split_for=["twitter"]` (`campaign_media_prep.py:642-697`); if still over, `(c) refuse` via `_enforce_video_limits` (`prepared_publishers.py:2987`) | Code is the *app* cap, not the API cap the module docstring claims; conservative, so not dangerous. Refusal is retryable (BUG 1). |
| **threads** | 300 s / 1,024 MB (`:58,74`) | 300 s / 1 GB (research doc states 1,024 MB API) | **(a) split** now planned per-platform (`campaign_media_prep.py:642-647`); publisher pre-validates and `(c) refuses` (`prepared_publishers.py:1909-1949`) | **Existing campaigns are not fixed.** The backfill that added the 3 threads parts (`scripts/backfill_platform_splits.py`) only INSERTs artifacts; it creates no posts/jobs, so job 5626 still carried the full 600 s file. See BUG 4. Refusal retryable (BUG 1). 1,024 vs 1,000 MB minor. |
| **instagram** | 900 s / 300 MB (`:57,73`) | Reel/feed video 300 MB, 3–900 s | **(a) split**; then `(c) refuse` via `_enforce_video_limits` (`prepared_publishers.py:1814`) | No 9:16/4:5 aspect check (BUG 9). Refusal retryable (BUG 1). |
| **facebook** | 14,460 s / 4,096 MB (`:56,72`) | 4 GB documented (up to ~10 GB organic), 14,460 s | **(a) split**; then `(c) refuse` via `_enforce_video_limits` (`prepared_publishers.py:1558`) | Refusal retryable (BUG 1). |
| **bluesky** | 600 s / 300 MB (`:55,71`) | 600 s / 300 MB | **(a) split** (+ untagged-parts fallback, `sau_backend.py:2285-2301`); also `(b) compress` in `_bluesky_shrink_video` (`prepared_publishers.py:4417-4470`); duration guard `(c) refuses` (`:4602-4607`) | Duration guard retryable (BUG 1). |
| **reddit** | 900 s / 1,000 MB (`:61,77`) | 900 s / 1 GB — only for **native** video | **(a) split** creates N link posts; publisher **link-posts only** (`prepared_publishers.py:3400-3480`), so 900 s / 1 GB is not enforced by Reddit for the external URL | The cap only applies to native upload, which this code does not do → **unnecessary multi-posting** for >900 s sources. Not a rejection risk. |
| **telegram** | None / 2,000 MB (`:62,80`) | user/MTProto 2 GB; **Bot API upload 50 MB** | size-only split >2,000 MB (`campaign_media_prep.py:645`); no publisher size guard | **Bot API 50 MB is not encoded.** A 60 MB file is sent to Bot API and rejected; the Telegram 4xx override makes it retryable (`prepared_publishers.py:829-830`) → burns retries. BUG 10. |
| **tiktok** | 3,600 s / 4,096 MB (`:59,75`) | **Content Posting API 600 s** / 4 GB (web is 3,600 s) | **(d) never split and effectively silently sent** — the TikTok branch bypasses part selection entirely (`sau_backend.py:2167-2182`); publisher then `(c) refuses` on size/duration or the API rejects | **BUG 2** (split never selected) and **BUG 7** (3,600 vs 600 s). |
| **youtube** | 43,200 s / 262,144 MB (`:60,76`) | 12 h / 256 GB verified; **15 min unverified** | No size/duration guard; oversize is sent and the API rejects (4xx → non-retryable via `_raise_for_status`, `prepared_publishers.py:273-284`) | Unverified 15-min cap not encoded; no proactive clear error. Low risk (Google 400 is non-retryable). |
| **nw_sw_blog** | *(absent from `platform_limits`)* | Markdown (GitHub Contents API) — no video | N/A — publisher writes Markdown only (`prepared_publishers.py:4162`); never consumes video artifacts | None (no video). |
| **teaching_blog** | *(absent from `platform_limits`)* | Markdown (GitHub Contents API) — no video | N/A — publisher writes Markdown only (`prepared_publishers.py:3965`) | None (no video). |

### Which platforms NEVER get a split but need one?

- **tiktok** — never. `_artifact_payloads_for_platform` (`sau_backend.py:2146`)
  takes the TikTok-only branch at `:2167-2182`, which picks the raw artifact and
  ignores every `part_index` artifact. Verified at runtime:
  `tiktok selected ids: [1]`, `groups: [[1]]` even though two fitting tiktok-tagged
  parts were supplied. TikTok's API cap is 600 s, so 601–3,600 s videos are never
  split and are refused at publish (or rejected).
- **threads for already-prepped campaigns** — effectively never. The 3 parts in
  the DB are orphaned (BUG 4). New preps *do* plan threads (`campaign_media_prep.py:642`);
  the 0→3 count is real, but no post/job was created for those parts.
- **telegram over 50 MB via Bot API** — never split (only >2,000 MB triggers),
  and no guard → rejected.
- **youtube unverified >15 min** — never split (cap is 12 h), corrected only by
  the API's own 4xx.
- `nw_sw_blog` / `teaching_blog` have no caps because they publish no video.

### Is aspect ratio checked anywhere?

No publisher checks aspect. The only normalization is inside
`media_prep.build_filters` (`media_prep.py:251-280`), which runs **only when
`should_shrink` is true**. `should_shrink` (`media_prep.py:285-309`) checks
size, `width > 1080`, `height > 1920`, `fps > 30` — **not aspect**.
`resize_to_target_if_landscape` is an explicit no-op stub
(`media_prep.py:575-582`), and `grep` finds no `aspect` check in
`prepared_publishers.py` other than Bluesky passing width/height through
(`:4485-4506`, no validation).

Verified: `should_shrink({1080x608, 30fps, 50 MB}, instagram)` → `False`.

Real failure mode: a landscape clip that is ≤1080 px wide, ≤30 fps and under
the size cap is published as-is. IG feed accepts up to 1.91:1, so it is not a
hard API rejection there — but it is **not a 9:16 Reel**, and likewise is not a
vertical TikTok / YouTube Short. This is a reach/categorisation failure rather
than a crash. A 1280×720 source *does* shrink (width > 1080) and gets
letterboxed to 1080×1920; the gap is the ≤1080-wide landscape band.

## Multi-post verification (DB)

For campaign **2517** (5-part twitter split) the DB shows the model works:

```
account 123: posts 5488-5492 (one per part)
             jobs  5489-5493  partIndex=1..5 partCount=5
             targets 5489-5493 schedule_at = 10-08 13:00 / 10-08 23:00 /
                       10-09 13:00 / 10-09 23:00 / 10-10 13:00
account 124: posts 5493-5497, jobs 5494-5498, targets 5494-5498
```

So an N-part split does create N posts and schedules each on its own slot
(`STAGGER_MINUTES`, `publish_orchestrator.py:575-615`, `_next_free_slot`).
Campaign 2520 (3 parts, account 77): posts 5425-5427, jobs 5426-5428,
targets 5426-5428 scheduled 10-12 23:00 / 10-13 13:00 / 10-13 23:00.

Caveat: the sibling parts do not stay live. In 2517 part 1 failed
(target 5489) and targets 5490-5493 were then `cancelled` with `attempts=0`;
in 2520 part 1 is still `pending` (target 5426) yet targets 5427-5428 are
already `cancelled` (`attempts=0`). Nothing in `worker.py` / `jobs.py` /
`publish_orchestrator.py` cancels siblings automatically (grep finds no
sibling/part cascade), so the actor is unknown — but a multi-part series can be
partially cancelled and never fully publish.
- The `scripts/dedup_pending_targets.py` regex strips `_part\d+` but **not**
  `_part1of5_pub` (the actual split-name shape), so it does not currently group
  real split parts. Verified: `normalise_media("..._part1of5_pub.mp4")` →
  `..._part1of5`. If the split naming ever loses the `ofN` suffix, the dedupe
  script would cancel all but the first part.

For campaign **2555** no split happened at all (twitter *and* threads jobs
carried the full watermarked file); the threads parts were added later by the
backfill and are unreferenced.

## BUGS

### BUG 1 — over-cap refusals are `retryable=True`, so they burn the retry budget

Evidence:
- `_enforce_video_limits` (`prepared_publishers.py:101`) raises at `:118-121`
  (size) and `:130-133` (duration) with the default `retryable=True`
  (`PreparedPublishError.__init__`, `:178-183`). Called for facebook `:1558`,
  instagram `:1814`, twitter `:2987`.
- `_validate_threads_video_artifact` (`:1909`) raises size/duration at
  `:1932-1949` with the same default.
- `_validate_tiktok_video_artifact` (`:2151`) raises at `:2171-2175`, `:2185`.
- Bluesky duration guard raises at `:4602-4607`.

Runtime: `_enforce_video_limits(twitter, 999s).retryable == True`;
`_validate_threads_video_artifact(600s).retryable == True`.

Live DB proof: target **5626** (threads, campaign 2555) is `failed` with
`attempts=3` and `last_error = "PreparedPublishError: Threads video duration
600s exceeds the 300s limit"`. The guard produced the clear message but then the
worker retried it three times (`worker.py:15-16`, default `max_attempts=3` at
`worker.py:152`; the non-retry branch is `worker.py:1109`
`if getattr(exc, "retryable", True) is False`).

Fix: raise these proactive guards with `retryable=False` (one shared helper,
e.g. `_over_cap_error(...)`), so a correctly-detected over-cap file fails once.

Test: assert `.retryable is False` for `_enforce_video_limits`,
`_validate_threads_video_artifact`, `_validate_tiktok_video_artifact`, and the
Bluesky duration guard. `tests/test_platform_limits.py:EnforceVideoLimitTests`
only asserts it raises, never the flag.

### BUG 2 — TikTok never gets a split (its branch ignores all split parts)

Evidence: `sau_backend.py:2148` routes tiktok to a separate branch
(`:2167-2182`) that groups by `(source_file_record_id, role)` and appends
`raw or items[0]`; `part_index` artifacts are never considered. Runtime probe
with 1 raw + 2 fitting tiktok-tagged parts → `[1]`. The plan builder *does*
create tiktok-tagged parts (`campaign_media_prep.py:642-647`), so work is done
and thrown away.

Impact: a TikTok source >600 s (API cap) is never split; it is sent whole and
refused by `_validate_tiktok_video_artifact`, or rejected by the API.

Fix: in the TikTok branch, after picking the raw artifact, also consider
raw/split artifacts for that source (and, for the mixed-platform case, split the
**raw** source, not the watermarked one, so the un-watermarked requirement is
kept). Then `_select_videos_for_platform` can return one raw part per post.

Test: feed a tiktok payload with a full raw + a 2-part tiktok-tagged split and
assert two groups / two jobs are produced.

### BUG 3 — split plan uses MiB while the caps and the publisher use decimal MB

Evidence:
- `campaign_media_prep.py:645` `size_bytes > float(mb) * 1024 * 1024` and
  `:656` `max_bytes=(mb * 1024 * 1024)`, `:667` stores `max_media_mb` in MiB.
- Caps are documented decimal (`platform_limits.py:11-16`) and the publisher
  compares `size_bytes > size_limit_mb * 1_000_000`
  (`prepared_publishers.py:117`); `media_prep.size_mb_decimal` uses `1_000_000`.

Runtime proof: a 305 MB file against Instagram's 300 MB cap → the plan builder
computes `over_size=False` (305e6 < 314.57e6) so it does **not** split, while the
publisher computes `305e6 > 300e6` and refuses. The prep therefore believes the
file fits while the publisher rejects it.

Fix: use `1_000_000` for the split trigger, the `max_bytes` argument, and the
stored `max_media_mb` (rename to `max_media_mb_decimal` or simply reuse
`media_prep.size_mb_decimal`).

Test: a 305 MB / instagram case must split (or the publisher must agree); and a
round-trip test that the value stored in `max_media_mb` equals decimal MB.

### BUG 4 — the Threads split "fix" is orphaned for existing campaigns

Evidence: `scripts/backfill_platform_splits.py` only INSERTs into
`campaign_artifacts` (`:245-285`); it never creates `campaign_posts` or
`publish_jobs` and never rewrites a stored payload. For campaign 2555:

```
campaign_artifacts 2527-2529  part_index=1..3 split_for=["threads"] created_at 2026-10-09 00:14
campaign_posts 5624 (threads) created 2026-10-08 10:40
publish_jobs   5626 (threads) payload artifacts = [watermarked_video, part_index=None]
publish_job_targets 5626 status=failed attempts=3 "Threads video duration 600s exceeds the 300s limit"
```

So the parts exist but nothing references them; the campaign still published the
full 600 s file and burned three attempts. The same is true for twitter on 2555
(jobs 5630/5631 carried the full file). The plan builder in the current code
now derives the threads plan, so **future** preps are fixed; **already-prepped
campaigns are not**, and the report `reports/threads-public-url-2026-10-09.md`
("Threads gap fixed") overstates it.

Fix: either re-prep affected campaigns (the current builder creates the plan and
the posts), or extend the backfill to create the missing posts/jobs/payloads —
not just artifacts.

Test: an integration test that, given a campaign whose artifacts include N
tagged parts and an existing single post, a re-prep/reconcile produces N jobs
with `partIndex`, or that the backfill refuses to run without wiring the jobs.

### BUG 5 — a duration-probe failure silently disables duration splitting

Evidence: `campaign_media_prep.py:627-633`:

```python
try:
    duration = float((media_prep.probe(publish_path) or {}).get("duration") or 0)
except Exception:
    duration = 0.0
...
over_time = bool(sec and duration and duration > float(sec))
```

A probe failure (no ffprobe, unreadable/offloaded file, container path) makes
`duration=0`, so no duration plan is created for **any** platform. Only the
size trigger still works. Campaign 2555 produced no split for either twitter
(140 s) or threads (300 s) despite a 600 s source; that is consistent with this
path (or with the plan builder not existing at prep time), but the code
silently treats "could not measure" as "fits".

Fix: on a probe failure, do not silently treat as 0; either raise a prep error
or fall back to the publisher guard (and log at warning).

Test: mock `probe` to raise and assert the plan is not silently skipped for a
known-over-cap source.

### BUG 6 — `SAU_ENCODE_CONCURRENCY` is bypassed by some ffmpeg calls

Gated (good): `media_prep._run` (`:166-178`, ffmpeg only, ffprobe skipped),
`media_pipeline.run_subprocess` (`:88-98`), `watermark_service._run_ffmpeg`
(`:38-46`), and `_bluesky_shrink_video` (`prepared_publishers.py:4441-4465`).

Bypassed:
- `uploader/twitter_uploader/main.py:149-150` `run_subprocess` calls
  `subprocess.run(check=True)` with no slot; `split_video_segment:307-333`
  uses it for a full re-encode (reachable from `worker.py:2099` via
  `TwitterThreadVideo`).
- `myUtils/tg_review.py:239-243` `_poster_frame` runs ffmpeg directly.

So the answer to "is it respected by **every** ffmpeg call?" is **no**.
`tests/test_media_prep.py:EncodeThrottleCoverageTests` covers media_pipeline,
the Bluesky re-encode and watermark_service only; it does not cover these two.

Fix: route both through `media_prep.encode_slot()` (or
`media_pipeline.run_subprocess`). Test: assert both call sites acquire the slot.

### BUG 7 — TikTok duration cap is 3,600 s; the Content Posting API is 600 s

Evidence: `platform_limits.py:75` `"tiktok": 3600.0`,
`prepared_publishers.py:76` `TIKTOK_MAX_VIDEO_SECONDS = ... or 3600`; the
research doc (`docs/platform-posting-limits.md:46,90`) states the API limit is
**600 s** (3,600 s is the *web* upload path). The split plan therefore does not
split a 601–3,600 s TikTok video, and `_validate_tiktok_video_artifact` falls
back to `creator_info`'s `max_video_post_duration_sec` and refuses it
(`:2182-2185`). Fix: encode 600 s for the API (or the creator maximum) and split
accordingly, which depends on BUG 2 being fixed.

Test: `video_max_seconds("tiktok") == 600` and a 900 s source produces a tiktok
split plan.

### BUG 8 — Threads size cap 1,024 MB vs 1 GB

`platform_limits.py:58` `"threads": 1024` is a decimal-MB table but 1,024 is a
binary number; the real 1 GB is 1,000 MB decimal. `THREADS_MAX_VIDEO_BYTES`
(`prepared_publishers.py:67`) therefore allows 1,024,000,000 bytes. The research
doc itself hedges ("1,024 MB API"), so this is minor/uncertain; worth aligning
to 1,000 unless Meta's API documents 1 GiB.

### BUG 9 — no aspect-ratio enforcement

See above. `media_prep.py:285-309` ignores aspect;
`resize_to_target_if_landscape` (`:575-582`) is a no-op; no publisher checks it.
Fix: add `abs(w/h - 9/16)` to `should_shrink` (or invoke the resize stub), so
the ≤1080-wide landscape band is normalized. Test: `should_shrink(1080x608)`
must be `True`, and a 1080×608 input must produce a 1080×1920 `_pub.mp4`.

### BUG 10 — Telegram Bot API 50 MB upload cap not encoded and forced retryable

`platform_limits.py:62` `"telegram": 2000` (user/MTProto 2 GB); the Bot API
upload cap is 50 MB (research doc). No publisher size guard, and the Telegram
4xx override (`prepared_publishers.py:829-830`) deliberately re-raises ordinary
4xx as `retryable=True`, so a "file is too big" refusal will retry. Fix: derive
the cap from the resolved transport (`_telegram_is_mtproto`,
`prepared_publishers.py:959`) and raise non-retryably for Bot API oversize.

### BUG 11 (minor) — Reddit native-video cap applied to a link-only path

`platform_limits.py:77` `"reddit": 1000` / `:61` `900 s` are for native video,
but `publish_reddit_sync` posts a link/self-post to externally-hosted media
(`prepared_publishers.py:3400-3480`). The split plan creates N reddit posts for
a >900 s source that Reddit would happily accept as one link post. Fix: exempt
reddit from the split plan (or gate the cap on native upload).

### BUG 12 (minor) — `_select_videos_for_platform` can return a non-fitting part

`sau_backend.py:2309-2313`: when no full artifact and no fitting variant exist,
it returns `[items[0]]`, which can be an over-cap part. Runtime: a twitter
selection over a tiktok-tagged 300 s split returned the 300 s part (twitter cap
140 s). The publisher guard then refuses. Fix: return the largest available
artifact and let the guard fail, or raise.

## UNKNOWNS

- Why campaign 2555's prep produced **no** plan for twitter *or* threads while
  2517/2520 (same day, earlier) produced twitter parts. A duration-probe failure
  (BUG 5) fits, but the prep log for 2555 is no longer available to confirm. The
  practical conclusion is unchanged: existing campaigns need a re-prep.
- The sibling-part cancellations in 2517/2520 were not produced by any code path
  found in `worker.py` / `jobs.py` / `publish_orchestrator.py`; they look like an
  operator/dedupe action. `scripts/dedup_pending_targets.py` does not currently
  match the `_partNofM` naming, so it is not the cause today.
- Whether Meta's Threads API documents 1 GB or 1,024 MB (BUG 8) — the repo's own
  research says 1,024 MB, so this is left as a doc/decision point.
- Whether a >50 MB Telegram account uses MTProto in production; the cap fix
  needs the configured transport to be known at prep time.

## Test coverage gaps (from the requested run)

`tests/ -k "platform_limits or media_prep or split"` → 89 passed. Gaps:

1. `EnforceVideoLimitTests` asserts only that `_enforce_video_limits` raises —
   never the `retryable` flag, and only for `twitter` (BUG 1).
2. No test asserts the split **plan builder** (`campaign_media_prep.py:626-700`)
   produces a given `(sec, MB) -> platforms` plan; `tests/test_campaign_media_prep.py`
   only covers Flask-freeness/public-origin.
3. No test covers the TikTok artifact branch (`sau_backend.py:2167-2182`); BUG 2
   is invisible to the suite.
4. No test for the MiB-vs-decimal split trigger (BUG 3).
5. `EncodeThrottleCoverageTests` omits `tg_review._poster_frame` and
   `uploader/twitter_uploader.split_video_segment` (BUG 6).
6. `test_platform_limits.py::test_supported_platforms_are_fully_mapped` checks
   `MESSAGE_MAX_CHARS`, `MEDIA_MAX_MB`, `MAX_VIDEOS` but not
   `VIDEO_MAX_SECONDS`; it also iterates only `SUPPORTED_PLATFORMS`, so
   `nw_sw_blog` / `teaching_blog` are never checked.
7. No test asserts aspect normalization for the ≤1080-wide landscape band
   (BUG 9).
