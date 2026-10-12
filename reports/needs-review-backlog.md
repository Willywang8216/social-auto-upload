# needs_review backlog — re-prep far-future + purge unrecoverable

Date: 2026-10-12 · Repo: `/home/will/social-auto-upload`
Task: `050702d0` — "re-prep 173 logger + 51 recoverable, decide 32 legacy".

## What was done

### 1. Stopped the publish-now flood

The re-prep tool reconstructs a missing schedule as `None`, which the submit path
reads as **publish-now**. 270 of the recoverable campaigns had no target
history, so a plain re-prep would have fired ~270 old posts immediately.
`scripts/reprep_campaigns.py` now takes `--default-start` (env
`SAU_REPREP_DEFAULT_START`) and:
* applies it when a campaign has no target history, **and**
* no longer preserves a **past** schedule (only future ones).

Run against the whole backlog with `2027-02-01T21:00:00` (Asia/Taipei) — about
3.7 months out. The existing `_next_free_slot` allocator then keeps
≥30 min between posts to one account and ≤3/account/day, so the 264 campaigns
drip out over months rather than arriving as a block.

Proof (campaign 1896, first to finish): the new targets carry
`2027-02-01T13:15:00Z` … `13:30:00Z` (= 21:15–21:30 Taipei), not now.

### 2. Re-prep applied

```
$ .venv/bin/python scripts/reprep_campaigns.py --drive-listing logs/reprep-drive-listing.txt \
    <264 --campaign ids> --default-start "2027-02-01T21:00:00" --apply --limit 300
  campaign 2604 -> preparing: cancelled 0 broken target(s)
  operator campaigns 2598-2603: UNCHANGED
```

`campaigns`: **264 preparing**, `needs_review` → 0 (after the purge below).

Preps run through **atomic-claim workers** so nothing is prepped twice. The
first attempt used four `direct_prep.py` slices, but that helper overwrites a
lease unconditionally and raced the production worker (13 duplicate targets
appeared on campaigns 1896/1898-1900, cancelled by
`scripts/dedup_pending_targets.py --apply`). It was replaced with three host
standalone workers plus the container worker — `claim_next_preparing_campaign`
is a compare-and-swap, so two workers can never take the same campaign:

```
nohup .venv/bin/python -m myUtils.worker --max-concurrent 2 \
  > logs/reprep/host-worker-{1..3}.log 2>&1 &
```

`prep_max_concurrent=1` per worker ⇒ 4 concurrent preps (3 host + 1 container).
First completion: `1896: jobs=5 succeeded=True lease_ok=True (94s)`; a sample of
prepared `_pub_pub.mp4` files is 18-102 MB with a valid `ftyp` header. Progress
is in `logs/worker.log` and `logs/reprep/host-worker-*.log`.

### 3. Unrecoverable entries purged

The 21 campaigns with no recoverable raw source were **deleted** (campaign rows
+ their cancelled targets; `campaign_posts`/`campaign_artifacts` cascade). The
media itself is untouched on Drive.

```
backup: db/database.db.bak-pre-purge-inrec-20261012-105631
deleted campaigns: 21
```

IDs: `60 61 62 63 2395 2408 2575 2579 2580 2581 2583 2584 2585 2589 2591 2592
2593 2594 2595 2596 2597`.

### 4. Wrong-status campaigns

The 10 campaigns that already had queued posts but read `needs_review`
(`2129 2132 2133 2134 2171 2215 2216 2217 2218 2411`) were flipped to
`publishing` with `last_error=NULL`. Backup:
`db/database.db.bak-pre-statusflip-<ts>`.

## Operator campaigns

2598-2603 are unchanged (asserted by the tool on every apply); 2602/2603 keep
the 14 duplicate-X cancellations from the earlier task.

## Remaining

The 264 preps are running through the 4 workers; 11 completed in the first
~30 minutes, then the switches. ETA is ~10-12h of CPU-bound prep on 4 cores
(load ~25-30). When they finish the campaigns are `publishing` with targets from
2027-02-01 onward. If an individual prep fails it returns to `needs_review` with
a reason and can be retried by the workers; the stray `reprep-backlog-*` leases
from the abandoned slices were cleared so the workers pick those up.
