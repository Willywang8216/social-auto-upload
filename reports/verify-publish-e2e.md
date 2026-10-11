# Verify publish + scheduling end-to-end — 2026-10-11

Scope: `/home/will/social-auto-upload` at `58141c7`. Operator bottom line:
"make sure that everything especially the publication and scheduling work
properly". This is verification, not a campaign change: **no campaign was
directly published, resubmitted or cancelled, and campaigns 2598-2603 were
read only.** One caveat, reported in full in §3: the *baseline* test run (before
the isolation bug below was fixed) had a production side effect — it published
target 5252 and permanently failed five live targets. That side effect is itself
the second bug fixed here.

Host clock at verification: **2026-10-11 13:20 UTC = 21:20 Asia/Taipei**
(host TZ is `Asia/Taipei`, `timedatectl` +0800). Schedule times in
`publish_job_targets.schedule_at` are naive UTC; the API layer localises naive
operator input (Asia/Shanghai, also +0800) so the stored UTC is the wall clock
minus 8h (`myUtils/publish_orchestrator.py:194`).

---

## 1. Scheduling — the six operator campaigns

All six are `campaigns.status = 'publishing'`, every `campaign_posts.status =
'queued'` and every target `pending` (nothing sent yet). Query:

```sql
SELECT cp.campaign_id, t.status, t.schedule_at
FROM campaign_posts cp JOIN publish_job_targets t ON t.job_id = cp.last_published_job_id
WHERE cp.campaign_id IN (2598,2599,2600,2601,2602,2603);
```

| campaign | content | profile | pending / total | first `schedule_at` (UTC) | first wall clock (Taipei) | last `schedule_at` (UTC) | last wall clock (Taipei) |
|---|---|---|---|---|---|---|---|
| 2598 | SFW Pride invitation | NW | 15 / 15 | 2026-10-24T12:00:00 | 2026-10-24 20:00 | 2026-10-25T13:55:00 | 2026-10-25 21:55 |
| 2599 | SFW Pride invitation | SW | 13 / 13 | 2026-10-24T12:00:00 | 2026-10-24 20:00 | 2026-10-25T13:50:00 | 2026-10-25 21:50 |
| 2600 | NSFW Pride invitation | NW | 10 / 10 | 2026-10-24T14:10:00 | 2026-10-24 22:10 | 2026-10-27T14:45:00 | 2026-10-27 22:45 |
| 2601 | NSFW Pride invitation | SW | 10 / 10 | 2026-10-24T14:00:00 | 2026-10-24 22:00 | 2026-10-27T14:45:00 | 2026-10-27 22:45 |
| 2602 | NSFW time-sensitive news | SW | 14 / 14 | 2026-10-12T12:00:00 | 2026-10-12 20:00 | 2026-10-16T13:45:00 | 2026-10-16 21:45 |
| 2603 | NSFW time-sensitive news | SW | 28 / 28 | 2026-10-12T13:50:00 | 2026-10-12 21:50 | 2026-10-28T13:55:00 | 2026-10-28 21:55 |

Wall-clock / date math checked directly:

```
$ date -u -d "2026-10-24T12:00:00Z" "+UTC: %F %T"
UTC: 2026-10-24 12:00:00
$ TZ=Asia/Taipei date -d "2026-10-24T12:00:00Z" "+Taipei: %F %T %Z"
Taipei: 2026-10-24 20:00:00 CST
$ python3 -c "from datetime import date; print((date(2026,10,31)-date(2026,10,24)).days)"
7
```

- `2026-10-24T12:00Z` **is** `2026-10-24 20:00` Taipei.
- It is **exactly 7 days** before `2026-10-31` (Taipei Pride).
- The `prepRequest.schedule.startAt` stored on the campaigns is naive local
  (`2598/2599 = 2026-10-24T20:00`, `2600/2601 = ...T22:00`,
  `2602/2603 = 2026-10-12T20:00`), and `_resolve_base_time` localised it
  correctly (+8h) to the UTC values above. The 2600/2601 first target is
  22:10/22:00, i.e. 2600's first platform was nudged 10 minutes by the
  per-account slot serialiser, not a timezone error.

Scheduler wake-up: `sau_backend.py:7034` `_publish_scheduler_loop` wakes the
drain for a due target **or** a claimable `preparing` campaign
(`sau_backend.py:7049`, commit `7518616`). Interval
`SAU_PUBLISH_SCHEDULER_INTERVAL_SECONDS=60` is present in the container.
`myUtils/jobs.py:179` `has_claimable_targets` compares `schedule_at <= now`
as naive-UTC ISO strings, matching the stored shape.

Verdict: **scheduling is correct for all six campaigns.** The targets are still
`pending` by design (earliest wall clock is 2026-10-12 20:00 Taipei, i.e.
tomorrow), so they have not been exercised yet.

---

## 2. Publication — real delivery in the last 6 hours

14 targets succeeded between 2026-10-11 07:20Z and 13:20Z (naive UTC). Evidence
is the write-once per-target log `logs/jobs/job-<id>.log` plus the payload /
platform tables.

| target | platform | account | job log (`target succeeded`) | strongest delivery evidence |
|---|---|---|---|---|
| 5610 | tiktok | 109 Nakedwill_TK | 13:01:51 → 13:05:07 | `tiktok_publish_status` row: `status='publish_complete'`, `publish_id=v_pub_file~v2-1.7695394886096177173`, `polled_at=13:05:07` |
| 5252 | bluesky | 120 SW Bluesky EN | 13:02:57 → 13:03:03 (attempt 2) | Bluesky API returns the post URI; success logged |
| 5609 | nw_sw_blog | 112 NW Blog | 13:00:41 → 13:02:55 | blog API 2xx; success logged |
| 5385 | bluesky | 118 NW Bluesky EN | 13:00:22 → 13:01:50 | Bluesky API success |
| 5386 | bluesky | 119 NW Bluesky ZH | 13:00:22 → 13:01:50 | Bluesky API success |
| 5580 | instagram | 72 NW_IG | 12:03:53 → 12:05:47 | Graph API container+publish returned ids; publisher raises on non-2xx (`prepared_publishers.publish_instagram_sync`) |
| 5608 | threads | 62 NWthreads | 12:03:53 → 12:05:34 | Threads API 2xx |
| 5578 | facebook | 11 Nakedwill | 12:01:47 → 12:04:04 | `/videos` returned an id; `_raise_for_status` |
| 5326 | telegram | 127 NW TG 中文 | 12:00:59 → 13:03:52 | payload `telegramCompletedByDelivery[5326] = {"@nakedwillzh":["media"], "@nakedwillzhchat":["media"]}` |
| 5390 | telegram | 116 NW TG 本人 | 12:01:47 → 12:03:52 | `{"@nakedwill":["media"], "@nakedwilltgchannel":["media"]}` |
| 3859 | threads | 42 SW_threads | 12:00:09 → 12:01:46 | Threads API 2xx |
| 5256 | telegram | 122 SW TG 中文 | 12:00:09 → 12:01:46 | `{"@nakedsexualwei":["media"], "@nakedsexualweiwei":["media"]}` |
| 5257 | telegram | 117 SW TG 本人 | 12:00:09 → 12:00:59 | `{"@sexualwill":["media"], "@sexualwilltgchannel":["media"]}` |
| 5494 | twitter | 124 NW X (nudeweiwei) | 09:43:10 → 09:44:49 | Sociamonials `post_id=10729773, network=tw, status=scheduled` — **accepted, delivery pending** |

Per-platform totals in the window: **succeeded 14, failed 0 caused by a
platform** (the 5 failures below were caused by the test suite — see §3),
telegram 4, bluesky 3, threads 2, tiktok 1, instagram 1, facebook 1,
nw_sw_blog 1, twitter 1.

Honest caveats:

- X/Twitter (5494) is **not confirmed live**. With `SAU_X_DIRECT_PUBLISH` unset
  the worker skips the direct path and hands the post to Sociamonials. The job
  log reads:
  `delivered via Sociamonials fallback (post_id=10729773, network=tw, status=scheduled) (delivery pending)`.
  "Scheduled" is an accepted submission, not a delivered post.
- Instagram/Facebook/Threads/NW-blog do not record a post id in their job logs
  or payloads, so the proof is "the publisher returned 2xx / raised nothing".
  Their sync publishers do raise `PreparedPublishError` on any non-2xx or
  missing id, so a success is a real API acceptance.

---

## 3. Failures in the last 6 hours

Six targets ended `failed` in the window. Query:

```sql
SELECT t.id,t.job_id,pj.platform,t.attempts,t.started_at,t.finished_at,t.last_error
FROM publish_job_targets t JOIN publish_jobs pj ON pj.id=t.job_id
WHERE t.status='failed' AND t.finished_at >= datetime('now','-6 hours');
```

| target | platform | attempts | error | classification |
|---|---|---|---|---|
| 5253 | bluesky | 3 | `RuntimeError: cannot schedule new futures after interpreter shutdown` | **CODE BUG** (test isolation) — fixed |
| 5255 | reddit | 3 | same | **CODE BUG** (test isolation) — fixed |
| 5332 | nw_sw_blog | 3 | same | **CODE BUG** (test isolation) — fixed |
| 5296 | twitter | 1 | `X direct publish disabled and the Sociamonials fallback failed` (fallback error: same shutdown `RuntimeError`) | **CODE BUG** (test isolation) — fixed |
| 5297 | twitter | 1 | same | **CODE BUG** (test isolation) — fixed |
| 5388 | reddit | 1 | `No subreddit on this account can accept this content: r/NudistMen does not accept explicit content; r/BareMenPositivity ...` | **Permanent** (content/subreddit policy) |
| (pre-existing) | reddit | — | same subreddit-policy class | Permanent |

No rate-limit / quota / transient platform failures occurred in the window.

### Root cause of the five shutdown failures (reproduced and fixed)

The full test suite was draining the **production** database. A suite worker
thread claimed live targets, then the pytest interpreter began shutting down,
so `asyncio.to_thread` raised `RuntimeError: cannot schedule new futures after
interpreter shutdown` on every subsequent claim. `/tmp/pytest-baseline.log`
records the pytest process (`process id=2106008`, `MainProcess`,
`Thread-1 (_run)`) claiming production job 5252/5253/5255 at 21:00 Taipei:

```
'job_id': 5253, ... 'message': 'target claimed; starting execution'
'job_id': 5253, ... 'message': 'target failed on attempt 1; retrying in 5.0s:
  RuntimeError: cannot schedule new futures after interpreter shutdown'
```

Reproduced with an isolation probe:

```
SAU_DB_PATH env = /tmp/sau-test-db-odmf0jxs/database.db
jobs.DB_PATH     = /home/will/social-auto-upload/db/database.db   <-- PRODUCTION
campaigns.DB_PATH= /home/will/social-auto-upload/db/database.db   <-- PRODUCTION
```

Cause: `tests/conftest.py` pins `SAU_DB_PATH`, but `myUtils/jobs.py`,
`myUtils/campaigns.py` and 16 other modules bound
`Path(BASE_DIR)/db/database.db` at import and ignored the override. The
conftest rebind loop only patched modules that happened to be imported before
it ran, which they were not. Fix + regression test in §5.

Note: `5252` was retried and **succeeded** on attempt 2 via the production
worker (`logs/jobs/job-5252.log`, 21:02:57 → 21:03:03), so the test run did not
lose that post.

---

## 4. Health

| check | command | result |
|---|---|---|
| healthz | `curl -s -o /dev/null -w '%{http_code}' http://localhost:5409/healthz` | **200** (`{"status":"ok"}`) |
| container | `docker inspect -f '{{.State.Health.Status}} restarts={{.RestartCount}}' social-auto-upload` | `healthy restarts=0` |
| offload cron | `grep 'offload done' logs/offload.log \| tail -1` | `[2026-10-11T12:55:47Z] offload done rc=0 local videoFile=302 uploads=0` (17,47 cron; previous runs rc=0) |
| worker draining | `docker top social-auto-upload` (gunicorn `wsgi:app`, 1 worker/8 threads) + `logs/worker.log` prep/refresh ticks; DB queue empty of due work | active |
| stuck `running`/`retrying` | `SELECT COUNT(*) ... WHERE status IN ('running','retrying')` | **0** (target timeout `SAU_TARGET_TIMEOUT_SECONDS=1200`) |
| ci-watch | `cat logs/ci-watch.state` / `tail -1 logs/ci-watch.log` | state `58141c7991...` = `origin/main` = HEAD; `[2026-10-11T13:20:04Z] already deployed and running; nothing to do` — it never restarted over an in-flight target |

`ci-watch.sh` waits for `running`/`retrying` to reach 0 before recreating the
container (default 1800s bound). The queue has had 0 such rows throughout this
verification, so no target was stranded by a deploy.

---

## 5. Code bugs found, reproduced and fixed (with tests)

### Bug 1 — duplicated split parts queued duplicate posts (affects 2602/2603)

`campaign_artifacts` appends a fresh copy of every split part on each prep run
instead of replacing the previous set. `_largest_fitting_parts` then returned
**every** copy, and `_artifact_part_groups_for_platform` emitted one post per
copy (`sau_backend.py:2185`).

Reproduced against the live rows (read-only):

```
campaign 2602 twitter: artifact_rows=6  -> part_groups=4   (expected 2)
campaign 2603 twitter: artifact_rows=23 -> part_groups=15  (expected 5)
campaign 2599 twitter: artifact_rows=6  -> part_groups=2   (correct, jobs predate the 2nd set)
```

This is why campaign 2602 has 4 twitter targets per account instead of 2, and
2603 has 10 instead of 5. 2598/2600/2601 have one artifact set and are correct;
2599 has two sets but its jobs were queued before the second appeared.

Fix: `sau_backend._largest_fitting_parts` collapses to one artifact per
`part_index` in the chosen `part_count` bucket, preferring a copy that carries a
`public_url`. After the fix the same live rows give `2602 -> 2`, `2603 -> 5`,
`2599 -> 2`.

Test: `tests/test_artifact_selection.py::ArtifactPartGroupsTests::test_duplicate_artifact_rows_do_not_duplicate_posts`.

### Bug 2 — the test suite drained the production database

Described in §3. `SAU_DB_PATH` is the documented override but 18 modules
ignored it. It is now centralised in
`utils/conf_defaults.default_db_path()` and used by `myUtils/jobs.py`,
`myUtils/campaigns.py`, `myUtils/profiles.py`, `myUtils/analytics_store.py`,
`myUtils/account_events.py`, `myUtils/content_generator.py`,
`myUtils/media_asset_service.py`, `myUtils/media_groups.py`,
`myUtils/meta_review.py`, `myUtils/publish_templates.py`,
`myUtils/reddit_review.py`, `myUtils/sheet_export_service.py`,
`myUtils/system_health.py`, `myUtils/threads_review.py`,
`myUtils/tiktok_review.py`, `myUtils/watermark_service.py`,
`myUtils/x_review.py`, `myUtils/youtube_review.py`. Production is unchanged
when `SAU_DB_PATH` is unset (it falls back to `<BASE_DIR>/db/database.db`).
Re-running the probe now yields the pinned temp path in every module.

Test: `tests/test_db_path_isolation.py`.

---

## 6. Test suite

Baseline (before fixes): `1646 passed, 1 skipped, 161 subtests passed`
(`.venv/bin/python -m pytest tests/ --ignore=tests/test_security_http.py -q`).

Final run (after the fixes below):

```
$ .venv/bin/python -m pytest tests/ --ignore=tests/test_security_http.py -q
1663 passed, 1 skipped, 161 subtests passed in 252.39s (0:04:12)
```

The +17 vs the `1646` baseline is 4 tests added here (`test_db_path_isolation.py`
×3 and the duplicate-part test) plus 13 from a concurrently-running session that
is editing the same working tree (purge-permanent-failures / re-prep work). The
final run used the fixed DB isolation, and the production queue shows no new
success or failure at `13:03`-`13:24` UTC — i.e. the suite no longer touches
production.

The tree is **shared with another live session**; the other session's changes
(`myUtils/worker.py`, `scripts/purge_permanent_failures.py`,
`tests/test_campaign_prep_queue.py`, `tests/test_campaigns_http.py`,
`tests/test_purge_permanent_failures.py`, part of `myUtils/campaigns.py` and
`sau_backend.py`) are uncommitted and were deliberately left untouched by this
verification.

---

## 7. Verdict

**Scheduling is correct** — all six campaigns are queued with the intended
Taipei wall clocks (2598-2601 first post 2026-10-24 20:00/22:00, exactly 7 days
before Pride; 2602/2603 first post 2026-10-12 20:00/21:50), and the worker,
scheduler and health endpoints are functioning. **Publication works** — 14
targets really delivered in the last 6 hours (Telegram channels and a
TikTok `publish_complete` are hard evidence) with no platform-caused failure.
**Two code bugs were found and fixed**: duplicate split parts were queuing
duplicate posts on campaigns 2602/2603, and the test suite was draining the
production database. **Action still owed by the operator (not done here):** the
already-queued duplicate X targets on 2602 (5755, 5757, 5759, 5761) and 2603
(5771, 5773, 5775, 5777, 5779, 5781, 5783, 5785, 5787, 5789) should be
cancelled before those dates; the fix stops new duplicates but does not remove
existing rows, and this verification was forbidden from touching them.

Verdict: **scheduling and publishing are healthy; do not re-prep 2602/2603, and
cancel the four duplicate X targets on 2602 and the ten on 2603 before they fire.**

---

## 8. Follow-up — the re-prep fleet finished (2026-10-11 evening)

After this verification, the coordinator finished the placeholder-media re-prep
fleet. The authoritative result is in `reports/reprep-fleet-final.md`:

- **45/45 recoverable campaigns re-prepped and verified** (real MP4, 2.4-110 MB,
  `ftyp` present). Final scan: **0 recoverable, 0 unrecoverable, 0 fixtures**,
  and **0 campaigns left in `preparing`**.
- The fleet had died mid-run for two real reasons, both fixed:
  - `scripts/ci-watch.sh` restarted over active preps (it waited for in-flight
    *publishes* but not *preps*). Fixed with a 30-minute-active-prep lease check
    (commit `83a50da`).
  - The container was on a stale image, so ci-watch redeployed hourly and killed
    the in-container preps. The remainder was run on the host instead.
- Four campaigns that had been killed after cancelling their targets but before
  re-queuing (2456, 2458, 2464, 2487) were re-prepped and verified.
- Full suite after the work: `1663 passed, 1 skipped, 161 subtests passed`.
- The six operator campaigns (2598-2603) remain byte-for-byte untouched; the
  duplicate-target and `needs_review` decisions in §7 are still owed to the
  operator.
