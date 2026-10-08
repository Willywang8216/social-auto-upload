# Duplicate-queue recovery — before/after inventory (2026-10-08)

Operator task: clear the duplicate damage left in the publish queue by the 499
retry loop, republish what was legitimately unpublished, and report the final
queue. All mutations went through the bearer-token API (`/jobs/targets/<id>/
cancel` and `/jobs/targets/<id>/resubmit`); **no rows were deleted and no SQL
was used to mutate the queue**.

## Backups (taken before any mutation)

| Purpose | File |
| --- | --- |
| Before recovery (first snapshot, 19:36:57 CST) | `db/database.db.before-dup-recovery-20261008193657.bak` |
| Immediately before the bulk cancel (19:41:59 CST) | `db/database.db.before-dup-cancel-20261008194159.bak` |

Both verified with `PRAGMA integrity_check` = `ok`.

## Duplicate identity used

Matches the fixed guard in `myUtils/publish_orchestrator._already_queued_for_media`
(read-only; source not edited): two **live** targets (`pending`/`retrying`/`running`)
on the same **account + platform** are duplicates when they share either

1. the same `media_group_id`, or
2. exactly the same set of underlying `media_group_items.file_record_id`s.

Grouping was validated by calling the live guard function directly, e.g.
`guard(127, 2104) -> True` (same file set as live group 1854) and
`guard(999, 1854) -> False`. Keep rule: earliest `schedule_at` wins (an empty
schedule counts as "due now" = earliest); never cancel `succeeded`.

## BEFORE inventory (from the pre-change backup)

Live targets: **1654** (`pending` 1646, `running` 2, plus the rest terminal).

Target status:

| status | before | after |
| --- | ---: | ---: |
| cancelled | 2839 | 3165 |
| pending | 1646 | 1326 |
| succeeded | 999 | 999 |
| failed | 143 | 148 |
| running | 2 | 3 |

Campaign status:

| status | before | after |
| --- | ---: | ---: |
| publishing | 2278 | 2281 |
| needs_review | 272 | 280 |
| preparing | 8 | 5 |

Duplicate inventory (read-only, computed before touching anything):

* **302 duplicate groups**, **354 duplicate live targets** to cancel.
  * by platform: telegram 308, twitter 44, nw_sw_blog 1, reddit 1.
  * 30 groups shared one `media_group_id`; 272 groups matched only via the
    identical underlying file-record set (retry rebuilt the group around the
    same file).
* `file_record 1142` sat in 6 media groups (`2552..2557`), each set `{1142}` —
  the canonical retry case; the guard now blocks it.
* Stuck `preparing` campaigns older than ~10 min: **8**
  (2578–2581 and 2586–2589, created 11:27–11:36 UTC). **All 8 carried a
  persisted `prepRequest`** in `metadata_json`, so the worker can claim them
  (do not delete).
* `needs_review` with a prep error: **272** (all 272 had `last_error`).
* Targets cancelled in the last 24h: **2483** at snapshot time (the large
  majority are the documented 2026-10-07 deliberate cleanup of unrecoverable
  pre-October media — see `logs/agent-notes.md` — not incident churn).

> Note: the brief's figures (72 campaigns / 59 groups, 103 duplicate jobs,
> 27 cancelled) are lower than the live measurement. The live DB at 19:36
> showed 302 duplicate groups / 354 extras; the retry damage is older and
> broader than the single 499 burst (it also includes the 2026-09-21
> file-set retries that pushed account 127's horizon into 2027).

## What was cancelled

* Bulk pass 1 (19:42): **353 cancelled**, 1 already cancelled.
* A concurrent one-off burst (≈19:43–19:50, now stopped) **re-activated 266**
  of those rows to `pending` with fresh schedules. The burst is documented
  below.
* Bulk pass 2 (19:52): **267 cancelled** (the 266 re-activated + 1 new).
* Net result: **354 distinct duplicate extras cancelled** —
  telegram 308, twitter 44, nw_sw_blog 1, reddit 1.
* 23 of the union are live because the rewrite flipped which member was
  earliest, so they became the canonical keep instead of the cancel. Exactly
  one live target now remains per (account, file-set, platform).
* No `succeeded` target and no `running` target was touched.

Per-request audit logs (JSONL, one line per API call):

* `logs/dup-cancel-test1-0.jsonl` (probe)
* `logs/dup-cancel-all-0.jsonl` (pass 1)
* `logs/dup-cancel-all2-0.jsonl` (pass 2)

### Concurrent re-activation anomaly (important)

Between pass 1 and pass 2 something outside this session re-queued 266
cancelled targets to `pending` and pushed their schedules forward via the
`_next_free_slot` allocator (observed `12:00 -> 22:00`, `22:00 -> next 12:00`).
It was a one-off burst, not a loop: the count went 227 → 240 → 266 → 267 and
then held flat. The source was not identifiable from this box (not the app's
own scheduler threads, not `scripts/reschedule`-style cron, not the
`engvoc-scheduler` container, and tests only open temp DBs). Pass 2 cleared the
result and the state has been stable since. If this recurs, re-run the
inventory below; the operation is idempotent.

## What was resubmitted (recovery)

Recovery rule applied: `cancelled`/`failed`, account enabled, at least one
underlying `file_record` has a `storage_key` or an on-disk file, not already
covered by a live target or by a `succeeded` target for the same
(account, platform, file-set), and collapsed to one row per group.

* **28 targets resubmitted** — the 2026-10-08 05:49–06:05 cancellations
  (campaigns 2504/2506/2507/2508/2511; groups 2503/2505/2506/2507/2510).
  Each underlying file (922/930/931/932/935) appears in exactly one media
  group and had no live or succeeded sibling, so these were genuinely
  unpublished.
  * by platform: twitter 7, bluesky 6, telegram 6, reddit 3, nw_sw_blog 3,
    facebook 1, instagram 1, threads 1.
  * audit: `logs/dup-resubmit-incident.jsonl`.
  * all 28 are now `pending` with their original future slots
    (2026-10-13 → 2027-02-01).
* **Failed targets: 0 resubmitted.** Every failed row in the window is either
  media genuinely gone (`MediaRestoreError`), a `[content-guard]` refusal, a
  deterministic platform refusal, or already succeeded for that account+media.
* The 595 other recoverable-looking `cancelled` rows from 2026-10-07 were
  **deliberately left cancelled**: they are the documented cleanup of
  unrecoverable pre-October media (`logs/agent-notes.md`, backups
  `before-stale-cancel-*` / `before-oldjob-cancel-*`). Re-queueing them would
  undo that cleanup and recreate the failure noise it removed.

## Stuck `preparing` campaigns (left in place)

5 campaigns remain `preparing`: **2578, 2579, 2580, 2581, 2590**. All five
carry a `prepRequest`, so they are worker-claimable and were left for the
worker (per the brief). 2578–2581 have now been waiting ~29 min; the worker has
claimed each at least once and the store's stale-lease sweep
(`requeue_stale_preparing`) will release or `needs_review` them. Campaigns
without a `prepRequest`: **0**.

`needs_review`: 280, all with a `last_error` (prep failures such as
"no persisted prep request" and "No publishable posts queued"); none were
deleted.

## FINAL queue state

* Duplicate groups: **0** (`cancel_candidates = 0`), stable across four
  consecutive checks.
* Live targets: **1329** (`pending` 1326 + `running` 3).

Pending by platform and next due (UTC):

| platform | pending | next due |
| --- | ---: | --- |
| telegram | 525 | 2026-10-08 12:00 |
| twitter | 255 | 2026-10-08 13:00 |
| bluesky | 234 | 2026-10-08 13:00 |
| reddit | 118 | 2026-10-08 13:00 |
| nw_sw_blog | 117 | 2026-10-08 13:00 |
| threads | 19 | 2026-10-08 12:00 |
| instagram | 18 | 2026-10-08 12:00 |
| facebook | 18 | 2026-10-08 12:00 |
| youtube | 11 | 2026-10-08 14:00 |
| tiktok | 11 | 2026-10-08 13:00 |

`running` right now: bluesky ×3.

## Reproduction

```bash
# read-only duplicate inventory
python3 /tmp/dup_inventory.py

# cancel extras (plan handed in via DUP_PLAN), then resubmit recoverables
DUP_PLAN=/tmp/dup_plan3.json python3 /tmp/dup_cancel.py all2 0 10000
python3 /tmp/dup_resubmit.py
```

## Caveats

* No commit/push, no container restart, no source edits.
* Concurrent test sessions write to the shared `logs/worker.log` (test fixture
  noise such as `ffmpeg exploded` / `campaign 1`); they use temp DBs and do
  not touch the live queue.
* `db/database.db` is live and moved during the run (worker publishing, async
  prep); numbers above are point-in-time and the queue was re-verified at the
  end.
