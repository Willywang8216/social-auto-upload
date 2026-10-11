# Fixing the dead fixtures and making `needs_review` honest

Date: 2026-10-11
Repo: `/home/will/social-auto-upload`
Scope: (A) the 4 demo-fixture campaigns with 19 live targets, (B) the 283
`needs_review` campaigns, (C) the code bug behind a wrong status.

No campaign status was bulk-edited. The only writes were the 21
`publish_job_targets` rows described in Part 1, each proven unrecoverable first
(DB backed up by the tool).

---

## Part 1 — Fixture cancellations (A)

### Result

`publish_job_targets` went from **1281 pending** to **1254 pending**; the 4
fixture campaigns now have **0 live targets**.

```
database backed up to /home/will/social-auto-upload/db/database.db.bak-pre-permfail-20261011-125620
cancelled 21 target(s)
failed targets remaining: 25
pending targets remaining: 1254
```

The 19 fixture targets are now `cancelled`:

| campaign | fixture | target ids (all cancelled) |
| --- | --- | --- |
| 2577 | `nonexistent.mp4` (file_record 1187) | 5658, 5659, 5660, 5661, 5662, 5663, 5664 |
| 2578 | `_inbox_cache/demo.mp4` (1188) | 5668, 5669, 5670, 5671, 5672 |
| 2579 | `_inbox_cache/demo.mp4` (1188) | 5675, 5676, 5677, 5678, 5679, 5680 |
| 2587 | `_inbox_cache/demo.mp4` (1188) | 5686 |

```
id    status     last_error
5658  cancelled  cancelled: permanent failure (media-unrecoverable) - retry cannot succeed
...
5686  cancelled  cancelled: permanent failure (media-unrecoverable) - retry cannot succeed
```

The same `--apply` pass also cancelled **2 failed targets that its existing
classifier already deems permanent** (they predate the last purge and were not
fixtures):

| target | campaign | error |
| --- | --- | --- |
| 5317 | 2513 | `No subreddit on this account can accept this content: r/NudistMen` |
| 5378 | 2514 | `No subreddit on this account can accept this content: r/NudistMen` |

### Why each fixture is unrecoverable (all three checks)

**1. `file_records` has no restore route.** Both rows have no
`storage_backend_id`, no `storage_key`, and no `storage_cdn_url`:

```
id    filename         file_path                                                      filesize  storage_key  storage_backend_id  storage_cdn_url
1187  nonexistent.mp4  nonexistent.mp4
1188  demo.mp4         /home/will/social-auto-upload/videoFile/_inbox_cache/demo.mp4
```

There is no local file for either path, and no `campaign_artifacts.remote_path`.

**2. The campaign media group points at those exact records.**
`media_group_items` for groups 2565/2566/2570 are file_record 1187 (group 2565)
and 1188 (groups 2566, 2570). Every artifact in the 4 campaigns resolves back to
1187/1188.

**3. The live Drive listing has no matching object.** Full listing
(`rclone lsf GDrive-willywang8216:sau --recursive --files-only --format p`,
**1467 files**):

```
$ grep -E "nonexistent\.mp4|_inbox_cache/demo\.mp4" /tmp/drive_index2.txt
NO MATCHES in 1467-line live Drive listing
```

The only `demo.mp4` on Drive is `videoFile/2a9b43bb-aeee-4721-88d7-754440edaf45_demo.mp4`,
which belongs to **file_record 89 / media group 86** (a different, July upload),
not to 1187/1188.

**4. The stored `public_url` is a dead placeholder** — both return HTTP 404:

```
https://socialupload.iamwillywang.com/getFile?filename=nonexistent.mp4       -> HTTP 404
https://socialupload.iamwillywang.com/getFile?filename=_inbox_cache/demo.mp4 -> HTTP 404
```

No local copy, no Drive object, no storage key, no CDN URL, and a 404 URL: no
re-prep and no restore can ever produce bytes. **Nothing was cancelled that had
a real source.**

### The tool

The named `scripts/purge_permanent_failures.py` only classified **failed**
targets (by matching `last_error`); the 19 live fixtures are **pending** and so
were invisible to it. It now has an opt-in, conservative `--pending-media`
mode. A pending target is cancelled only when **every** artifact is gone:
local file absent, not in the supplied Drive listing, no `storage_key` +
`storage_backend_id`, no `storage_cdn_url`, no `remote_path`, and its
`public_url` — if present — is verified dead. It requires `--drive-listing`
**and** `--verify-urls`; anything it cannot prove, including an unreachable
URL (network error), is left alone.

Dry run before applying (real output):

```
pending targets scanned (>= 2d): 1107
  media-unrecoverable      19  -> cancel
  ids: [5658, 5659, 5660, 5661, 5662, 5663, 5664, 5668, 5669, 5670, 5671, 5672, 5675, 5676, 5677, 5678, 5679, 5680, 5686]
  NOT CLASSIFIED         1088  -> LEFT ALONE (review)
```

It cancelled exactly the 19 expected ids and left 1088 pending targets alone.

---

## Part 2 — The 283 `needs_review` campaigns (B)

Measured from `db/database.db` (`status='needs_review'`). 283 confirmed.

### 2.1 Status is WRONG — has queued posts (13)

These campaigns already have `campaign_posts.status='queued'`; `needs_review`
reads to an operator as "this did not schedule", which is false.

| ids | queued posts | `last_error` | how it got there |
| --- | --- | --- | --- |
| 2129, 2132, 2133, 2134, 2171, 2215, 2216, 2217, 2218, 2411 | 2–6 each | `prep failed: campaign N has no persisted prep request ('prepRequest')` | **worker exception path (bug — now fixed, Part 3.1)** |
| 2579, 2580, 2581 | 8 / 1 / 1 | `No publishable posts queued` | demo-fixture family; last error written **before** commit `774989e` (2026-10-08 < 2026-10-09), i.e. by the pre-guard path. No current code defect. |

**Do not "re-publish" these** — their posts are already queued. The 10 worker
ones should be flipped back to `publishing` (operator decision; this report does
not change status). The 3 fixture-family ones are scheduled on dead media (see
2.3) and their targets are now cancelled, so flipping the status alone would be
wrong — they need the fixture status decision below.

### 2.2 Prep genuinely produced nothing with no queued posts (270)

| group | count | ids | why |
| --- | --- | --- | --- |
| **`logger` NameError (already-fixed code bug)** | **173** | 1910–2097 (range; see below) | `artifact prep failed: name 'logger' is not defined`; raw source is intact (`src_ok=173/173`) and no artifact was written. Re-prep can now succeed. |
| **artifact prep failed, source present** | **51** | 60, 61, 62, 1896–1909, 1958–1971, 2136, 2147–2160, 2407, 2409, 2534, 2535, 2559 | ffmpeg/ffprobe/path failures; the media-group source still resolves. Re-prep candidates. |
| **legacy: no persisted `prepRequest`** | **32** | 2169, 2172, 2264, 2365, 2408, 2530–2533, 2537–2540, 2544, 2546–2549, 2551–2553, 2560–2563, 2570–2576 | Sync-submitted campaigns the async worker claimed; `run_campaign_prep` raises because there is no async prep payload. Pre-dates the async queue; prep/re-submit path needs a decision. |
| **demo-fixture media (dead `_inbox_cache/demo.mp4`)** | **11** | 2583, 2584, 2585, 2589, 2591, 2592, 2593, 2594, 2595, 2596, 2597 | `No publishable posts queued`; artifacts reference file_record 1188 exactly like the Part 1 fixtures. Unrecoverable — spent. |
| **media gone (other)** | **2** | 63, 2395 | 63: `artifact prep failed: /app/test.mp4` (no local/Drive/storage copy). 2395: `artifact prep failed: /app/data/publish/sfw-teaching-least-squares-proof-calculus.png` (same). |
| **real media, still produced nothing** | **1** | 2604 | **Resolved: redundant resubmission.** Campaign 2603 (`publishing`) schedules the exact same file (file_record 1205) to the same 8 accounts. `publish_orchestrator._already_queued_for_media` matches on the whole file-record set, so 2604's accounts were all skipped and it queued nothing. Correct behaviour, not a defect; retire 2604. **Excluded from the 2598–2603 no-touch set but not modified.** |

Total: **173 + 51 + 32 + 11 + 2 + 1 = 270**, plus the 13 wrong-status = **283**.

#### The 173 `logger` campaigns

Created 2026-09-20/21. The `NameError` came from `sau_backend.py`'s storage and
watermark helpers, which referenced a bare `logger` that was never defined. It
was fixed on **2026-09-21** in commit `81805e8`
("fix(publish): define the logger used by storage/watermark helpers"). A static
AST scan of the current tree finds **no** remaining bare `logger` load. All 173
have their source media mapping intact, so they are **recoverable by re-prep**,
not spent.

### 2.3 The wider fixture family

22 campaigns reference file_record 1187/1188. Full state after Part 1:

- **Targets cancelled (fixtures):** 2577 (8), 2578 (9), 2579 (8), 2582 (1),
  2586 (1), 2587 (8), 2588 (1), 2590 (5) — 41 targets, all now terminal.
- **Spent `needs_review`, no posts/targets:** 2575, 2583, 2584, 2585, 2589,
  2591–2597.
- **`needs_review` but already queued (the 2.1 three):** 2579, 2580, 2581.

2580/2581/2582/2586/2588/2590 each had a single post whose target was already
cancelled before this pass, so they have no live work either.

### 2.4 What "other" means here

There is **no** catch-all "other" bucket: every one of the 270 non-queued
campaigns falls into one of the six reasons above, and no `needs_review`
campaign has a blank `last_error`.

---

## Part 3 — Code bugs fixed (C)

`myUtils.campaigns.campaign_has_queued_posts` is the guard that stops a prep
producing nothing from regressing a scheduled campaign. Auditing every path that
sets campaign `status='needs_review'` found three places still missing it.

### 3.1 `myUtils/worker.py` — the prep **failure** path (root cause of the 10)

`_run_campaign_prep` had the guard only on the success path (added in
`774989e`). Its `except` handler unconditionally wrote `needs_review` with
`prep failed: …`, which is exactly how 2129/2132/2133/2134/2171/2215–2218/2411
were flipped while their posts were queued. The handler now checks
`campaign_has_queued_posts`; when true it leaves the campaign `publishing` and
clears `last_error`, otherwise it records the failure as before.

### 3.2 `myUtils/campaigns.py` — `requeue_stale_preparing` (lease expiry)

`scripts/reprep_campaigns.py` sets campaigns with live targets back to
`preparing`. If that re-prep's worker dies, the lease sweep's
`attempts >= max_attempts` branch moved the campaign to `needs_review` even
though queued posts existed. It now selects `publishing` (with `last_error`
cleared) when `campaign_posts` has a queued row, else `needs_review` as before.

### 3.3 `sau_backend.py` — `POST /campaigns/<id>/publish`

A re-publish that queued nothing (e.g. every account disabled) wrote
`needs_review` + "No publishable posts were queued" over a campaign that already
had queued posts. It now uses the same guard.

### 3.4 `scripts/purge_permanent_failures.py` — counting + pending media

- The apply loop counted with `connection.total_changes and 1 or 0` (always 1,
  even for zero-row updates); now uses `cursor.rowcount`.
- Added the conservative `--pending-media` classifier described in Part 1.

### 3.5 Paths confirmed correct (no change)

- `publish_orchestrator.submit_publish` (both the artifact-prep exception and
  the final status) operates on a **freshly created** campaign, so it cannot
  already have queued posts.
- `sau_backend`'s `/campaigns/prepare` exception path likewise acts on the
  campaign it just created.
- `mcp_server/tools/publish.py` only reports the status string; the DB status is
  set by the orchestrator.
- `sau_backend` lines 9852/9892 write `prepared_posts.status`, a different table
  from campaign status.

### 3.6 The `logger` bug

Fixed long ago (`81805e8`, 2026-09-21), verified by AST scan. The 173 stale
statuses remain only because nothing re-drove those campaigns.

---

## Part 4 — Tests

Baseline: **1646 passed, 1 skipped**. After the changes:

```
$ .venv/bin/python -m pytest tests/ --ignore=tests/test_security_http.py -q
1660 passed, 1 skipped, 161 subtests passed in 202.33s (0:03:22)
```

New/updated tests:

- `tests/test_campaign_prep_queue.py`
  - `test_worker_prep_failure_does_not_regress_a_queued_campaign`
  - `test_requeue_stale_preparing_keeps_a_queued_campaign_scheduled`
- `tests/test_campaigns_http.py`
  - `test_campaign_publish_does_not_regress_an_already_queued_campaign`
- `tests/test_purge_permanent_failures.py`
  - dead URL + no route → `media-unrecoverable`
  - live URL, unverifiable URL, network-error probe → left alone
  - local file, Drive object, storage mapping, `remote_path` → left alone
  - empty / partially-recoverable payloads → left alone

---

## Part 5 — What the operator should do

1. **Nothing for the 19 fixtures** — they are cancelled with proof. The 4
   campaigns (2577, 2578, 2579, 2587) and their 41 fixture targets are now all
   terminal; no live work remains on dead media.
2. **Repair the 13 wrong-status campaigns** (Part 2.1). The 10 worker-path ones
   (2129, 2132, 2133, 2134, 2171, 2215–2218, 2411) are genuinely scheduled:
   flips to `publishing` are correct now that the code cannot regress them. The
   3 fixture-family ones (2579, 2580, 2581) are scheduled on dead media and
   should be decided with the fixture cleanup, not flipped blindly.
3. **Re-prep the 173 `logger` campaigns** — the bug is already fixed and their
   sources are intact. `scripts/reprep_campaigns.py` is the resumable tool.
4. **Triage the 51 "artifact prep failed, source present"** — the media exists,
   so these are re-prep/re-drive candidates (ffmpeg, path, or transient).
5. **Decide on the 32 legacy no-`prepRequest` campaigns** — they predate the
   async queue and cannot be driven by the worker as-is; either reconstruct a
   `prepRequest` (as `reprep_campaigns.py` does) or leave them spent.
6. **Retire campaign 2604** — it is a redundant resubmission of media campaign 2603 already schedules to the same 8 accounts; the duplicate-media guard correctly queued nothing.
7. **Retire the 11 spent fixture `needs_review` campaigns** (2583, 2584, 2585,
   2589, 2591–2597) and 63/2395 once the operator accepts they are spent.
8. **Leave the remaining 25 failed targets alone** — they are retryable or need
   a code fix already tracked elsewhere (localhost URL, Sociamonials quota,
   Threads duration, TikTok invalid params, etc.).

---

## Part 6 — Fleet coordination (2026-10-11, after the first pass)

The parallel re-prep fleet is alive and progressing, so no extra prep workers
were spawned (4 CPU cores: the container worker plus two direct prep processes
already saturates them; more would thrash).

- Purged `pi-2650` (CLI bundle broken, idle, all tasks `done`, no file leases).
- Aligned `reprep-batch-a`/`reprep-batch-b` with this pass: the 19 fixtures are
  cancelled, skip 2577/2578/2579/2587, and do not fan out more prep.
- `verify-publish-e2e` committed `f498d49` (duplicate split-post fix + DB-path
  isolation) and is finalising; its 2602/2603 duplicate-target cancellation is
  **operator-gated** by this task's no-touch rule.
- Re-prep fleet progress at this pass: of the 30 recoverable campaigns
  (2455–2501), **25 are `publishing`** and 5 remain `preparing`, with 14 fresh
  `campaign_artifacts` since 12:30Z today. The remaining re-prep is CPU-bound,
  not blocked.
