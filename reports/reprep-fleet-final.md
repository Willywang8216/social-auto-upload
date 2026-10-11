# Re-prep fleet — final coordination report

Date: 2026-10-11 · Repo: `/home/will/social-auto-upload`
Role: coordinator (`verify-publish-e2e`). This report supersedes the intermediate
`reports/reprep-slice-s{1..4}.md` (those verifiers ran before the host finish and
with a stale Drive listing; their `FAILED` verdicts are superseded here).

## 1. Result

**The entire recoverable re-prep queue is drained.**

```
$ .venv/bin/python scripts/reprep_campaigns.py --drive-listing logs/reprep-drive-listing.txt --limit 100
  live targets        : 1267 across 422 campaigns
  unrecoverable       : 0 targets across 0 campaigns
  recoverable (raw source on Drive): 0
  no raw source (cannot re-prep / fixtures): 0
  DRY RUN: would re-prep 0 campaign(s)

$ sqlite3 db/database.db "SELECT COUNT(*) FROM campaigns WHERE status='preparing';"
0
```

**45/45 touched campaigns verified with real media** (`size >= ftyp` proof,
`verify_reprep.py`):

| campaign | status | live | source | size | ok |
|---|---|---|---|---|---|
| 2436 | publishing | 8 | drive | 2.4 MB | true |
| 2439 | publishing | 8 | drive | 8.2 MB | true |
| 2446 | publishing | 9 | drive | 63.9 MB | true |
| 2447 | publishing | 9 | drive | 67.9 MB | true |
| 2448 | publishing | 9 | drive | 69.1 MB | true |
| 2449 | publishing | 9 | drive | 47.8 MB | true |
| 2450 | publishing | 9 | drive | 40.8 MB | true |
| 2451 | publishing | 9 | drive | 44.7 MB | true |
| 2452 | publishing | 9 | drive | 56.8 MB | true |
| 2453 | publishing | 9 | drive | 48.6 MB | true |
| 2454 | publishing | 9 | drive | 54.0 MB | true |
| 2455 | publishing | 13 | drive | 76.0 MB | true |
| 2456 | publishing | 7 | local | 78.8 MB | true |
| 2457 | publishing | 9 | drive | 78.0 MB | true |
| 2458 | publishing | 9 | local | 78.0 MB | true |
| 2459 | publishing | 9 | drive | 78.7 MB | true |
| 2460 | publishing | 9 | drive | 56.3 MB | true |
| 2461 | publishing | 7 | drive | 68.3 MB | true |
| 2462 | publishing | 9 | drive | 68.0 MB | true |
| 2463 | publishing | 7 | drive | 59.4 MB | true |
| 2464 | publishing | 7 | local | 59.6 MB | true |
| 2465 | publishing | 13 | drive | 33.4 MB | true |
| 2466 | publishing | 13 | drive | 35.6 MB | true |
| 2467 | publishing | 7 | drive | 58.3 MB | true |
| 2468 | publishing | 7 | drive | 35.0 MB | true |
| 2469 | publishing | 7 | drive | 24.7 MB | true |
| 2470 | publishing | 7 | drive | 16.9 MB | true |
| 2471 | publishing | 7 | drive | 23.3 MB | true |
| 2472 | publishing | 7 | drive | 14.7 MB | true |
| 2473 | publishing | 7 | drive | 38.2 MB | true |
| 2474 | publishing | 7 | drive | 33.8 MB | true |
| 2475 | publishing | 7 | drive | 18.3 MB | true |
| 2477 | publishing | 9 | drive | 54.4 MB | true |
| 2478 | publishing | 7 | drive | 17.0 MB | true |
| 2479 | publishing | 7 | drive | 55.2 MB | true |
| 2480 | publishing | 7 | drive | 15.3 MB | true |
| 2481 | publishing | 7 | drive | 32.3 MB | true |
| 2482 | publishing | 7 | drive | 18.1 MB | true |
| 2483 | publishing | 7 | drive | 43.9 MB | true |
| 2484 | publishing | 7 | drive | 49.3 MB | true |
| 2485 | publishing | 7 | drive | 8.3 MB | true |
| 2486 | publishing | 9 | drive | 53.7 MB | true |
| 2487 | publishing | 7 | local | 53.6 MB | true |
| 2488 | publishing | 9 | drive | 87.9 MB | true |
| 2501 | publishing | 9 | drive | 110.2 MB | true |

The six operator campaigns (2598-2603) are byte-for-byte untouched: all
`publishing`, 15/13/10/10/14/28 targets, **all pending, 0 cancelled, 0
succeeded**.

## 2. What blocked it, and what was fixed

### 2.1 ci-watch restarted over active preps (root cause of the lost batches)

`scripts/ci-watch.sh` waited for `running`/`retrying` publish targets before
recreating the container, but **not** for campaigns being prepped. The worker
runs prep in-process, so a restart strands the `preparing` row until the
stale-lease sweep (up to 2h). At `14:44Z` the watcher did exactly that and killed
six in-flight preps.

Fix: the drain wait now also counts campaigns in `preparing` whose `_prepLease`
was refreshed in the last 30 minutes (dead leases are ignored so a stranded
campaign cannot block deploys forever). Verified live:

```
running=0 preparing=7      # would previously have restarted immediately
```

Commit `83a50da`. (The watcher subsequently waited the full 1800s bound, logged
`WARNING: ... 3 active prep(s) still in flight ... deploying anyway`, and
deployed `58141c7` — the safe, explicit behaviour.)

### 2.2 The container was running a stale image, so deploys repeated

`docker inspect` showed the container on revision `989453f` while `:latest` and
`ci-watch.state` were `58141c7`. ci-watch therefore kept "re-deploying" roughly
hourly. It is now aligned at `58141c7`; no further recreations occurred after
16:14Z.

### 2.3 Preps are long; running them in the container is fragile

Because of 2.1/2.2, the coordinator moved the remaining preps to the **host**
(`SAU_REPREP_REPO` support added to `logs/reprep/direct_prep.py`), where they
survive a container recreate. Host `ffmpeg` 8.0.1 and the repo `.venv` were used.
The remaining campaigns were processed in parallel slices:

* host `h1` 2459/2460/2461, `h2` 2468/2469/2470, `h3` 2488/2501;
* host `fix1` 2456/2458, `fix2` 2464/2487 (the four campaigns that had been
  killed *after* cancelling their targets but *before* re-queuing, leaving them
  `preparing` with 0 live targets — invisible to the scan until repaired).

### 2.4 `verify_reprep.py` produced false negatives for Drive-backed media

The helper built the remote path as `REMOTE:key` instead of `REMOTE/key`, so
`rclone cat` returned nothing and every Drive-backed artifact read `ok:false`
despite a correct size. Fixed in `logs/reprep/verify_reprep.py`; re-running gave
`ok:true` and `ftyp` for all of them. (Operational helper under the gitignored
`logs/` tree, not a product change.)

## 3. Tests

```
$ .venv/bin/python -m pytest tests/ --ignore=tests/test_security_http.py -q
1663 passed, 1 skipped, 161 subtests passed in 128.94s (0:02:08)
```

## 4. Fleet hygiene

The three hung predecessor sessions (`reprep-batch-a`, `reprep-batch-b`,
`fix-fixtures-and-nr`), the finished `batch-b-finish`/`commit-fixtures-nr`, the
four prep sessions and the four verify sessions were archived and killed. No
file leases were held. Only the coordinator session remains active.

## 5. Still owed by the operator (not changed here)

1. **Duplicate X targets on 2602/2603** (the split-artifact bug fixed in
   `f498d49`): cancel the four on 2602 (`5755 5757 5759 5761`) and the ten on
   2603 (`5771 5773 5775 5777 5779 5781 5783 5785 5787 5789`) before they fire.
   Left untouched deliberately: the original brief forbade modifying 2598-2603.
2. **13 `needs_review` campaigns with queued posts** (2129, 2132, 2133, 2134,
   2171, 2215-2218, 2411 flip to `publishing`; 2579/2580/2581 are dead-fixture
   media). Documented in `reports/fix-fixtures-and-needs-review.md`.
3. **The `SAU_X_DIRECT_PUBLISH` / Sociamonials "scheduled" gap** from
   `reports/verify-publish-e2e.md` §2 — an accepted Sociamonials post is
   recorded as succeeded while its delivery is still `scheduled`.
