# Re-prepping the placeholder-media campaigns

Date: 2026-10-11 · Repo: `/home/will/social-auto-upload` · Session: re-prep the
campaigns whose prepared media is a 1–4 byte Drive placeholder, and verify
publication works.

All numbers below were taken from the live system (SQLite `db/database.db`, the
live Drive listing via `rclone lsf`, and the production worker log). Commands and
real output are pasted inline.

---

## 1. Scope: what is actually broken

The starting measurement in the brief ("45 campaigns / 281 targets") came from a
heuristic that only checked whether an artifact had an HTTPS `public_url` and a
local file. That heuristic is wrong in both directions:

* it flags campaigns whose artifact has **no** `public_url` but whose Drive
  object is perfectly real (e.g. 2505 / 2506 / 2507 / 2509), and
* it misses campaigns whose artifact **does** have an `https` `public_url` but
  whose Drive object is a placeholder and whose URL is dead (2436–2488, 2501).

The only correct test is the worker's own resolution against the **live Drive**:
no local file, and the resolved Drive object is missing or smaller than the
media threshold, and every HTTPS fallback (`artifact.public_url`,
`file_records.storage_cdn_url`) is dead. Measured with that test:

```
live targets        : 1285 across 427 campaigns
unrecoverable       :  375 targets across 50 campaigns
recoverable (raw source on Drive): 46 campaigns / 356 targets
no raw source (dead fixtures)     :  4 campaigns /  19 targets
```

That 356 + 19 = 375 matches the brief's headline ("375 live targets cannot
publish … 356 of those are recoverable"). The 45/281 figure was a detector
artifact; the real re-prep scope is **46 recoverable campaigns**.

The four fixtures (2577, 2578, 2579, 2587) reference `demo.mp4` /
`nonexistent.mp4` records with no Drive object. They cannot be re-prepped and are
reported, not touched.

None of the six operator campaigns (2598–2603) is in the broken set: their
prepared objects live under `inbox/generated/campaigns/campaign-2598..2603/` at
6–76 MB and resolve fine.

---

## 2. Why re-prep did not just work

### 2a. The scheduler never woke (commit `7518616`) — CONFIRMED LIVE

`drain()` is what runs the prep tick (`myUtils/worker.py:824`), and the scheduler
loop only started `drain()` when `job_runtime.has_claimable_targets()` was true —
i.e. when a *scheduled publish was due*. A campaign sitting in `preparing` has no
due target yet, so it was never picked up. Campaign 2503 sat in `preparing` for
46 minutes with a completely silent worker before this fix.

Deploy verification (`docker inspect` revision label is not reliable, so this was
checked against the built image and the running file):

```text
$ cat logs/ci-watch.state
75186160eef1037ddef754113ba0ccfd40ec406b

$ docker exec social-auto-upload sh -c \
    "grep -c has_claimable_preparing_campaigns /app/sau_backend.py"
1
$ docker exec social-auto-upload sh -c \
    "grep -c has_claimable_preparing_campaigns /app/sau_backend.py"   # before deploy: 0
```

`scripts/ci-watch.sh` built and deployed `commit-7518616` and the helper is now
present in the running `sau_backend.py`. (The container has since moved on to a
later `origin/main` — `dec762e` — which has `7518616` as an ancestor and still
contains the check.)

### 2b. Legacy campaigns have no stored `prepRequest`

`run_campaign_prep` (`myUtils/campaign_prep.py:295`) raises unless
`campaigns.metadata_json.prepRequest` exists:

```
ValueError: campaign 2503 has no persisted prep request ('prepRequest');
submit it through the publish-center async path
```

Pre-async-prep campaigns never stored it. `scripts/reprep_campaigns.py`
reconstructs it from data the campaign and its media group already hold:

* `brief` = the campaign's `notes` (falling back to the first queued draft),
* `mediaFilePaths` = `media_group_items` joined to `file_records`,
* `schedule.startAt` = the earliest existing target `schedule_at`, converted from
  UTC back through `Asia/Shanghai` (the same zone `_resolve_base_time` reads a
  naive `startAt` in),
* `options` / `accountDrafts` / `tiktokPostSettings` = the async defaults.

### 2c. Prep skipped Drive restore for absolute missing sources (commit `35e4315`)

Even with a `prepRequest`, the first re-prep of 2436 queued but did not restore
the raw source: `campaign_media_prep.prepare_campaign_media_artifacts` only tried
`_download_file_from_storage` inside the *relative*-path branch. Every
`/app/videoFile/_inbox_cache/...` source is absolute, so the missing local file
was used as-is, ffmpeg failed, and prep still recorded a "prepared" artifact
pointing at the absent raw file. Fixed in `myUtils/campaign_media_prep.py` to
always attempt the storage download when the resolved source is missing. The
running container has the fix (`grep 'if not raw_path.exists' …` returns it).

---

## 3. Campaign 2503 — end-to-end proof (scheduler → real media)

Forced re-prep (`scripts/reprep_campaigns.py --campaign 2503 --limit 1 --apply`),
which cancelled 0 targets (already cancelled by the earlier manual attempt) and
wrote the reconstructed `prepRequest`:

| step | evidence |
| --- | --- |
| before | `status=needs_review`, `last_error=prep failed: campaign 2503 has no persisted prep request` |
| applied | `campaign 2503 -> preparing` |
| worker claim | `2026-10-11 17:55:12 … campaign 2503 claimed for prep (owner=…)` |
| finished | `2026-10-11 17:56:49 … campaign 2503 prepared: 8 job(s) queued` |
| new posts | `campaign_posts` 5805–5812 (bluesky×2, nw_sw_blog, reddit, telegram×2, twitter×2) |
| new targets | `publish_job_targets` 5806–5813, all `pending`, 2026-10-12T12:00–13:35 |

The claim/prepare happened with **no unrelated due target** — this is the
`has_claimable_preparing_campaigns()` wake working.

Real-media proof for 2503 (the `_pub.mp4` regenerated by the production prep, and
the watermarked artifact the live targets actually reference):

```text
$ stat -c '%s %n' generated/campaigns/campaign-2503/dba6cdeb-…_NSFW_NW_Nakedwill_News_pub.mp4
3401601 generated/campaigns/campaign-2503/…_pub.mp4
$ file generated/campaigns/campaign-2503/…_pub.mp4
ISO Media, MP4 Base Media v1 [ISO 14496-12:2003]
$ head -c 16 …_pub.mp4 | xxd | head -1
00000000: 0000 001c 6674 7970 6973 6f6d 0000 0200  ....ftypisom....
$ docker exec social-auto-upload ffprobe -v error \
    -show_entries format=duration,size -of default=noprint_wrappers=1 /app/…_pub.mp4
duration=31.041667
size=3401601
```

The live target payload (artifact 2331, `watermarked_video`) resolves to the
Drive object `generated/campaigns/campaign-2503/watermarked_video/…-watermarked_video.mp4`
= **4,800,334 bytes** (real, vs the 1-byte stubs elsewhere).

---

## 4. Batch results

`scripts/reprep_campaigns.py` is dry-run by default. Each `--apply` snapshots the
DB first (`db/database.db.bak-pre-reprep-<UTC>`, SQLite online backup), cancels
**only** the broken `pending`/`retrying` targets (never `running`/`succeeded`),
marks the campaign `preparing`, and asserts 2598–2603 unchanged.

A complication: a parallel agent session was running the same pytest command,
and its tests claim **production** `preparing` campaigns (`owner` PID was
`.venv/bin/python -m pytest …`), writing artifacts into `/tmp/sau-test-generated-*`;
`ci-watch` also restarted the container mid-prep several times. Where a queued
prep was lost or stolen, the production pipeline was re-run directly inside the
container (same `make_default_prep_runner` the worker calls). Batch 1 ran fully
through the production worker.

### Batch 1 — 2436, 2439, 2446, 2447, 2448

| campaign | status | broken targets cancelled | verified media (live target) |
| --- | --- | --- | --- |
| 2436 | publishing | 8 | Drive `inbox/generated/…/…_pub.mp4` **2,375,604 B**, local `ftypisom`, ffprobe 16.25 s |
| 2439 | publishing | 2 | Drive **8,215,255 B** |
| 2446 | publishing | 8 | Drive `…_pub.mp4` **63,917,729 B** + parts 25,384,654 / 33,694,806 |
| 2447 | publishing | 8 | Drive **67,878,714 B** + parts 34,279,830 / 28,177,524 |
| 2448 | publishing | 8 | local **69,116,032 B** `part1of2_pub.mp4` (+ 33,082,863 part 2), production worker 9 jobs |

2436 was the fix-validation run: first attempt (before `35e4315`) produced only
the raw-source path with ffmpeg errors; after the fix it produced a real
`_pub.mp4` at the path the live targets reference. The fresh Drive listing shows
`inbox/generated/campaigns/campaign-2436/…_pub.mp4` = 2,375,604 B.

### Batch 2 — 2449–2454

| campaign | status | verified media |
| --- | --- | --- |
| 2449 / 2450 | preparing | in flight (mid-prep container restart; stale-lease sweep re-queued and re-claimed them) |
| 2451 | publishing | Drive 44,669,992 B (`ftypisom`) |
| 2452 | publishing | Drive 56,766,277 B (`ftypisom`) |
| 2453 | publishing | local 48,582,957 B (`part1of2_pub.mp4`), production worker 9 jobs |
| 2454 | preparing | in flight |

### Batch 3 — 2465, 2466, 2467, 2480, 2485 (smallest sources first)

| campaign | status | verified media |
| --- | --- | --- |
| 2465 | publishing | Drive 33,351,388 B (13 jobs queued) |
| 2466 | publishing | Drive 35,617,339 B (13 jobs queued) |
| 2467 | publishing | local 58,266,070 B (7 jobs queued) |
| 2480 | preparing | in flight |
| 2485 | publishing | local 8,278,397 B (7 jobs queued) |

**Verified at the end of batch 3:** 13 of the 17 touched campaigns are `publishing`
and every one of their live targets resolves to a Drive or local object between
**2.4 MB and 69.1 MB** with a valid ISO-BMFF header; 4 (2449, 2450, 2454, 2480)
are still mid-prep. The scope had dropped from 375 targets / 50 campaigns to:

```text
unrecoverable           : 253 targets across 34 campaigns
recoverable (raw source on Drive): 30 campaigns
no raw source (fixtures)          :  4 campaigns
```

---

## 5. The six operator campaigns (2598–2603) are untouched

Checked before the first apply and again in every `--apply` (the tool aborts if
they changed), and independently at the end:

```text
$ sqlite3 db/database.db "SELECT id,status,prepared_at FROM campaigns WHERE id BETWEEN 2598 AND 2603 ORDER BY id;"
2598|publishing|2026-10-09T09:19:49
2599|publishing|2026-10-09T09:58:40
2600|publishing|2026-10-09T09:26:26
2601|publishing|2026-10-09T09:32:18
2602|publishing|2026-10-09T10:09:55
2603|publishing|2026-10-09T12:18:00

$ diff operator-targets-before.txt operator-targets-after.txt   # per-status counts
OPERATOR TARGETS: IDENTICAL
```

The tool hard-excludes `{2598..2603}` from detection and refuses `--campaign`
against them.

---

## 6. Tests

```text
$ .venv/bin/python -m pytest tests/ --ignore=tests/test_security_http.py -q
1646 passed, 1 skipped, 161 subtests passed in 160.80s (0:02:40)
```

The baseline in the brief was 1613 passed, 1 skipped; the increase is test
additions from the parallel sessions (commit `7518616` added 2,
`tests/test_campaign_media_prep.py` still passes 6). No failures.

---

## 7. What is left

1. **13 of the 17 touched campaigns are done and verified** (audited real media
   above). **4 are still mid-prep** (2449, 2450, 2454, 2480) and were re-queued
   after a mid-prep container restart; the worker's stale-lease sweep recovered
   them automatically, so they need no operator action.
2. **30 recoverable campaigns remain** in the re-prep queue. The tool is
   resumable — re-running `scripts/reprep_campaigns.py --apply` finds only what
   is left and processes them in `--limit` batches.
3. **4 fixture campaigns** (2577, 2578, 2579, 2587 / 19 targets) reference
   `demo.mp4` / `nonexistent.mp4`; they have no raw source and must be cancelled
   by the operator, not re-prepped.
4. **Environment interference:** the parallel session's pytest claims production
   `preparing` campaigns and writes artifacts under `/tmp/sau-test-generated-*`.
   Those junk `campaign_artifacts` rows (2436/2446/2503) are inert — the live
   payloads select the real artifacts — but they should not exist. The tests
   should use an isolated DB (`SAU_DB_PATH`) so they cannot claim production
   campaigns; this also caused a transient `database is locked` during polling.
5. **`ci-watch` restarts mid-prep** kill an in-flight `preparing` campaign; the
   worker's stale-lease sweep re-queues it after 45 min, so no data is lost, but
   it slows a batch. Not new — the auto-deploy is otherwise correct.

---

## 8. Files

* `scripts/reprep_campaigns.py` — the tool (dry-run default, live-Drive
  verification, bounded `--limit`, DB backup, operator guard, resumable).
* `myUtils/campaign_media_prep.py` — the absolute-path source-restore fix
  (commit `35e4315`).
* Per-batch evidence: `logs/reprep/batch1.json`, `batch2.json`, `batch3.json`,
  `logs/reprep/2503-apply.json`, `/tmp/container_prep.log`.
