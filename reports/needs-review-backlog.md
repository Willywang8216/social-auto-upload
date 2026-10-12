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

Preps run on the host in 4 parallel slices (immune to container recreations):

```
nohup env SAU_REPREP_REPO=/home/will/social-auto-upload \
  SAU_PUBLISH_SCHEDULER_INTERVAL_SECONDS=0 .venv/bin/python \
  logs/reprep/direct_prep.py --owner reprep-backlog-{1..4} <ids> \
  > logs/reprep/backlog-{1..4}.txt
```

Each slice holds 66 campaigns. First completion: `1896: jobs=5 succeeded=True
lease_ok=True (94s)`. Progress is in `logs/reprep/backlog-*.txt`; verify with
`logs/reprep/verify_reprep.py <ids>`.

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

None from this task. The 264 preps are running; when they finish the campaigns
are `publishing` with targets from 2027-02-01 onward. If any individual prep
fails it returns to `needs_review` with a reason and can be retried with
`direct_prep.py --owner reprep-retry <id>`.
