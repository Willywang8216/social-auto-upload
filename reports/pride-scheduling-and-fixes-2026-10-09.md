# Taipei Pride + time-sensitive news: scheduling verified, and four bugs fixed

Date: 2026-10-09
Task: fix the GitHub Actions outage, schedule the newly uploaded Drive videos
(the Pride invitation one week before Taipei Pride, the NSFW news in three days),
build a reusable "move the schedule by N days" feature, and separate the Drive
archive from the new-asset area.

---

## 1. The scheduling you asked for is DONE and verified

Your brief already said the date — campaign 2599's own notes read *"SFW
invitation to this year's Taipei Pride (**2026-10-31**)"*. I independently
corroborated it: Wikipedia records Taiwan Pride 2025 as **2025-10-25**, i.e. the
last Saturday of October, which makes 2026's **Saturday 2026-10-31**.

| campaign | what | status | pending | first post | expected | ✓ |
| --- | --- | --- | ---: | --- | --- | --- |
| 2598 | pride invitation, SFW | publishing | 15 | 2026-10-24 | 2026-10-24 | ✓ |
| 2599 | pride invitation, SFW | publishing | 13 | 2026-10-24 | 2026-10-24 | ✓ |
| 2600 | pride invitation, NSFW | publishing | 10 | 2026-10-24 | 2026-10-24 | ✓ |
| 2601 | pride invitation, NSFW | publishing | 10 | 2026-10-24 | 2026-10-24 | ✓ |
| 2602 | time-sensitive NSFW news | publishing | 14 | 2026-10-12 | 2026-10-12 | ✓ |
| 2603 | time-sensitive NSFW news | preparing¹ | 28 | 2026-10-12 | 2026-10-12 | ✓ |

¹ 2603 finished re-prepping during this session (0 → 28 posts); the status is
stale-by-one-tick and its 28 posts are queued.

**Pride: 2026-10-24 is exactly one week before 2026-10-31.** First posts land
Saturday 20:00 Taipei. **News: 2026-10-12 is exactly three days from today.**

Nothing needed rescheduling — your submits were already correct. What was
*missing* was the truth about their state, which the bugs below were hiding.

## 2. Four bugs found and fixed

### BUG 1 (HIGH) — the test suite writes to the PRODUCTION database

`tests/conftest.py` pinned the alert credentials, generated-media root and job
log dir — **but not the database**. Most modules bind the live path at import:

```
myUtils/jobs.py:45, campaigns.py:15, account_events.py:14,
analytics_store.py:21, content_generator.py:21
    DB_PATH = Path(BASE_DIR) / "db" / "database.db"
```

so any test or helper calling one of those **without** an explicit `db_path`
reads and writes production — the campaigns, the queue, and the campaign status
a live publish depends on.

This is not theoretical: campaign **2603 was left with 0 posts and had to be
reclaimed** after a full-suite run, which is how it was found.

Fixed by pinning `SAU_DB_PATH` (the documented override) and repointing any
module-level `DB_PATH` already bound. **Verified: a suite run now leaves
`db/database.db` byte-identical (md5 unchanged).**

### BUG 2 (MEDIUM) — a re-prep that queues nothing un-schedules a live campaign

Campaign 2599 sat in `needs_review` with *"No publishable posts queued"* while
all 13 of its posts were queued and correctly scheduled for 2026-10-24. A
re-prep had queued 0 new jobs and overwrote the status.

To an operator that reads as *"this did not schedule"* and invites a re-submit of
a campaign that is already correct. Both prep-completion paths now check
`campaigns.campaign_has_queued_posts` first, so a prep that produced nothing
leaves an already-scheduled campaign in `publishing`. Repaired 2599.

### BUG 3 — the offload archive and new assets shared one tree

You asked for these to be separated. Measured first:

```
sau/videoFile   649 objects  22.9 GiB
sau/generated   485 objects  27.3 GiB
sau/uploads       4 objects
```

each holding published **and** never-published material together; 699 of 704
`file_records` rows resolve to a real object, 80 carry a legacy placeholder size.

New layout (additive — legacy rows are kept, so nothing breaks):

```
sau/inbox/{videoFile,uploads,generated}/<key>      NEW material
sau/published/{videoFile,uploads,generated}/<key>  ALREADY published
sau/archive/<campaign>/...                         unchanged
sau/assets/...                                     unchanged
```

The tier lives in the **endpoint**, never the key, because restore composes
`storage_backends.endpoint + "/" + file_records.storage_key`. Keeping the key
byte-identical means a migration only repoints `storage_backend_id`, so for any
record the path is either the old location (bytes still there) or the new one
(bytes moved) — **never a pair naming the wrong object.** That is what makes a
half-applied migration safe.

A record counts as `published` only when a **succeeded** target names it, so a
staged far-future post with an offloaded copy is not misread as published.

`scripts/migrate_drive_layout.py` is dry-run by default, uses `rclone moveto`
(server-side rename, no re-upload, no delete) and **refuses `--apply` while any
record is unresolved**. Verified: it refuses with *"80 unresolved"* and nothing
moved (`sau/inbox/` does not exist on Drive).

### BUG 4 (fixed earlier this session) — GitHub Actions disabled

GitHub's own Actions page states it verbatim:

> Workflows aren't being run on this fork because of its GitHub Actions usage. A
> repository maintainer can re-enable them.

It is repository-specific — other repos on the same account kept running — and
**cannot be cleared through the API** (`PUT /actions/permissions` returns 204
without clearing it). Only you can, in the UI.

**Action for you:** repo → **Actions** tab → re-enable workflows. Until then,
`scripts/deploy-local.sh` builds, verifies and deploys locally (tested).

## 3. New feature: shift the schedule by N days ("smartly")

`myUtils/schedule_shift.py`, exposed as `sau schedule shift` and
`POST /jobs/schedule/shift`.

Scope by profile / platform / account / campaign / status / date-range / ids /
all-pending. Dry-run by default; `--apply` backs up the DB and re-plans under the
existing slot lock.

"Smartly" is the point: a naive `+3 days` can put two posts for one account
inside the anti-spam window or stack more than 3 posts on a day. The shift
re-spaces its result through the **existing** allocator
(`_next_free_slot` under `slot_reservation_lock`), so the invariants still hold
and nothing is reimplemented. It is wall-clock preserving: `schedule_at` is naive
UTC and the operator is Asia/Shanghai, so a test pins the DST case where
`+86400*N` would move the local hour and this does not.

Live dry-run: **1282 targets planned, 22 moved further than 3 days** (12 min-gap,
10 daily-cap), each named with its reason. Nothing written.

## 4. Test suite

**1518 passed, 1 skipped** (was 1474 at the start of this session).

## 5. Operator decisions

1. **Re-enable GitHub Actions** in the repo's Actions tab — only you can.
2. **Approve the Drive migration**: `scripts/migrate_drive_layout.py` (dry-run
   first). It is blocked until the 80 size-mismatched records are resolved; the
   report explains why it refuses rather than guessing.
3. `muyuan.do` audio transcription returns **403 Forbidden**. It is best-effort
   (publishing still works), but the LLM loses transcript context — worth fixing
   the credential if you want richer copy.
