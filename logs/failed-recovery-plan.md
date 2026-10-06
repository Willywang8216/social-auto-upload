# Failed publish-target recovery plan — dry run

**Generated:** 2026-10-06 00:01:47 UTC  
**Database:** `db/database.db` (read-only; nothing was written)  
**Status:** DRY RUN — no DB mutation, no publish, no reschedule.  
**Base tool:** `scripts/recover_failed_targets.py` (classification + duplicate logic); scheduling windows from `logs/posting-times-research.md` and `scripts/optimize_schedule.py`.

## Headline

- **245** failed publish targets examined (every `status='failed'` row).
- **3** are genuinely recoverable and are scheduled below.
- **242** are skipped (duplicates or permanent/not-fixable-by-reschedule).
- **Date range of the plan:** `2026-10-06T13:25:00` → `2026-10-06T14:30:00` (UTC).

> Only **3 of 245** failures are worth rescheduling. The backlog is dominated by duplicates of posts that already succeeded, permanently missing media, dead OAuth tokens, and deterministic refusals (duration/Direct Post/content-guard/link/subreddit ban). See "Why the recoverable set is so small" below.

## Counts per platform (recoverable vs skipped)

| Platform | Failed | Recoverable | Skipped |
|---|---:|---:|---:|
| reddit | 17 | 3 | 14 |
| twitter | 86 | 0 | 86 |
| tiktok | 33 | 0 | 33 |
| bluesky | 32 | 0 | 32 |
| telegram | 27 | 0 | 27 |
| threads | 20 | 0 | 20 |
| facebook | 14 | 0 | 14 |
| instagram | 11 | 0 | 11 |
| youtube | 3 | 0 | 3 |
| nw_sw_blog | 2 | 0 | 2 |
| **Total** | **245** | **3** | **242** |

## Skip-reason breakdown (all platforms)

| Reason | Count |
|---|---:|
| duplicate of a succeeded (account, media) | 77 |
| missing media | 80 |
| oversized video duration | 9 |
| banned subreddit | 3 |
| [content-guard] | 3 |
| link whitelist | 1 |
| TikTok Direct Post required | 1 |
| account/auth/permission (needs operator) | 43 |
| stale/test media (videos/demo.mp4) | 24 |
| unresolved account (not in accounts) | 1 |
| **Total skipped** | **242** |

## Skip-reason breakdown per platform

| Platform | duplicate of a succeeded (account, media) | missing media | oversized video duration | banned subreddit | [content-guard] | link whitelist | TikTok Direct Post required | NSFW gate | account/auth/permission (needs operator) | stale/test media (videos/demo.mp4) | unresolved account (not in accounts) | Recoverable | Failed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| reddit | 0 | 6 | 0 | 3 | 0 | 1 | 0 | 0 | 4 | 0 | 0 | 3 | 17 |
| twitter | 16 | 47 | 2 | 0 | 3 | 0 | 0 | 0 | 18 | 0 | 0 | 0 | 86 |
| tiktok | 8 | 3 | 0 | 0 | 0 | 0 | 1 | 0 | 20 | 0 | 1 | 0 | 33 |
| bluesky | 26 | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 32 |
| telegram | 12 | 15 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 27 |
| threads | 5 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 | 0 | 20 |
| facebook | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 | 0 | 14 |
| instagram | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 | 0 | 11 |
| youtube | 0 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 3 |
| nw_sw_blog | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2 |

## Recovery plan (scheduled)

Each target is placed in its platform's best UTC window, at least 30 min from any other post to the same account, with at most 3 posts/account/day, and never colliding with the account's already-queued pending/retrying slots.

| Target | Job | Platform | Account | Media key | Original error | Scheduled (UTC) |
|---|---:|---|---|---|---|---|
| 74 | 74 | reddit | `account:105` | `4b014874-a57d-421c-8db4-5db458199f05_vlcsnap-2026-06-30-00h03m21s191` | TimeoutError: Locator.wait_for: Timeout 15000ms exceeded. Call log:   - waiting for locato | `2026-10-06T13:25:00` |
| 3865 | 3865 | reddit | `account:106` | `nude reverie by the lake 01__nsfw` | PreparedPublishError: Reddit submit failed for r/gaybrosgonemild: [['RATELIMIT', "Looks li | `2026-10-06T13:30:00` |
| 4527 | 4527 | reddit | `account:106` | `sfw taipei stonewall` | PreparedPublishError: Reddit submit failed for r/gaybrosgonemild: [['RATELIMIT', "Looks li | `2026-10-06T14:30:00` |

**Date range:** `2026-10-06T13:25:00` → `2026-10-06T14:30:00` (UTC).

Per-platform scheduled: reddit=3.

### Slot rationale

- target 74 · `account:105` (Nakedwill Reddit) · reddit window 13/14/22 UTC + 25 min account stagger → `2026-10-06T13:25:00`
- target 3865 · `account:106` (Sexualwill Reddit OAuth) · reddit window 13/14/22 UTC + 30 min account stagger → `2026-10-06T13:30:00`
- target 4527 · `account:106` (Sexualwill Reddit OAuth) · reddit window 13/14/22 UTC + 30 min account stagger → `2026-10-06T14:30:00`

## Why the recoverable set is so small

The 245 failures fall into these buckets:

1. **77 duplicates** — the same `(account, media)` already has a `succeeded` target, so re-posting would double-publish.
2. **80 missing media** — the artifact is gone, was never generated, or cannot be restored (includes `Artifact restore failed` / permission-denied and `has no safe public HTTPS recovery URL`).
3. **43 account/auth/permission** — dead/expired OAuth tokens, `requires reconnection`, TikTok app still in development mode, API access blocked, TikTok domain verification, missing OAuth scopes, config/code errors. Rescheduling cannot fix these; the account must be reconnected/reconfigured first.
4. **24 stale test media** — the shared test asset `videos/demo.mp4`; the 2026-10-05 health audit already classified these May–July failures as "not worth reposting".
5. **9 oversized duration** — the media is longer than the platform cap (e.g. the 198 s X/fallback video vs X's 140 s limit; Threads 442/499/813 s vs 300 s). A retry sends the same file and fails again.
6. **3 banned subreddit**, **3 [content-guard]**, **1 link whitelist**, **1 TikTok Direct Post required**, **0 NSFW gate** — deterministic refusals.
7. **1 unresolved account** — `account:90` no longer exists in `accounts`, so there is nothing to publish with.

## Base-script comparison (`scripts/recover_failed_targets.py`)

Running the base tool over the same 245 failed targets returns **62 recoverable** (`PERMANENT_ERROR_PATTERNS` = the 7 task categories; 106 permanent + 77 duplicates skipped). This plan is deliberately stricter, because "only genuine recoverable" excludes failures a reschedule cannot repair. The 62 break down as:

| Subset of the base tool's 62 | Count | Disposition in this plan |
|---|---:|---|
| genuinely transient (rate-limit / timeout / processing) | 3 | scheduled |
| only revealed by the job log to be oversized duration | 2 | skipped (duration) |
| unresolved account | 1 | skipped |
| missing media the base regex misses (`artifact not found`, `404 NOT FOUND`, `FileNotFound`) | 6 | skipped (missing media) |
| stale test media (`videos/demo.mp4`) | 24 | skipped |
| account/auth/permission base does not match | 26 | skipped (needs operator) |
| **Total** | **62** | **3 scheduled / 59 skipped** |

> The base tool also classes some `MediaRestoreError: Artifact restore failed` and `Generated artifact is missing` rows as permanent already (via `missing`); those are in the 80 missing-media count.

## Scheduling rules used

- **Best windows** (`scripts/optimize_schedule.py` / `logs/posting-times-research.md`):
  - `twitter`: 13:00, 14:00, 23:00, 00:00 UTC
  - `bluesky`: 13:00, 22:00, 23:00, 00:00 UTC
  - `facebook`: 12:00, 13:00, 23:00, 00:00 UTC
  - `instagram`: 12:00, 13:00, 23:00, 00:00 UTC
  - `threads`: 12:00, 13:00, 23:00, 00:00 UTC
  - `tiktok`: 13:00, 14:00, 23:00, 00:00 UTC
  - `youtube`: 14:00, 15:00, 16:00 UTC
  - `reddit`: 13:00, 14:00, 22:00 UTC
  - `telegram`: 12:00, 13:00, 22:00 UTC
  - `linkedin`: 13:00, 14:00, 15:00 UTC
  - `pinterest`: 12:00, 13:00, 14:00 UTC
  - `nw_sw_blog`: 13:00 UTC
  - `teaching_blog`: 13:00 UTC
- **Spacing:** ≥ 30 min between any two posts to the same account (`SAU_PUBLISH_MIN_GAP_MINUTES`).
- **Per-day cap:** 3 posts/account/day (`SAU_PUBLISH_MAX_PER_DAY`).
- **No collisions:** the allocator consults the account's existing pending/retrying `schedule_at` slots, so a recovery never lands on a queued post.
- **Account stagger:** `(account_id % 10) × 5 min` so a fan-out does not fire every account at `:00`.

## Apply (when approved)

This plan is dry-run only. Nothing below has been executed. The equivalent mutation would be:

```bash
# Back up first, then reschedule the 3 targets via the guarded jobs API:
#   jobs.reschedule_target(<target_id>, <slot>)  -> status pending, attempts 0
#   target 74: 2026-10-06T13:25:00  (reddit account:105)
#   target 3865: 2026-10-06T13:30:00  (reddit account:106)
#   target 4527: 2026-10-06T14:30:00  (reddit account:106)
# Or re-run the tool with a lookback that includes these rows once the operator accepts:
#   python scripts/recover_failed_targets.py --lookback-days 10000   # dry-run
```

> Even if applied, the two reddit targets on `account:106` would go out an hour apart and the one on `account:105` first, all inside the reddit 13–14 UTC window. The remaining 242 failures need the operator actions listed above (reconnect accounts, re-encode/shorten media, restore artifacts, or accept them as permanently failed).

