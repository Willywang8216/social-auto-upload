# Publish pipeline health audit — 2026-10-05

Author: session `x-credits` (bb6304cf). Read-only investigation plus two
code changes (not yet deployed). Evidence: `db/database.db`,
`logs/worker.log`, `logs/jobs/*.log`, `logs/offload.log`,
`logs/publish_digest.log`, live read-only Sociamonials GETs.

Verdict in one line: the pipeline is delivering on every platform, but the
**scheduled backlog is badly jammed and duplicated** (same account, same
media, same minute), and X now depends on Sociamonials.

---

## 1. Platform status (last 24 h of finished targets)

| platform | succeeded | failed | notes |
|---|---:|---:|---|
| bluesky | 31 | 2 | healthy |
| telegram | 21 | 0 | healthy |
| instagram | 6 | 0 | healthy |
| twitter | 8 | 23 | failures are the pre-deploy 402/401/reconnect backlog; current code falls back |
| threads | 6 | 2 | 813 s source vs 300 s limit (design gap, see §6) |
| facebook | 4 | 2 | "Unable to fetch video file from URL" — CDN URL encoding fix is deployed |
| nw_sw_blog | 3 | 1 | healthy |
| tiktok | 1 | 1 | chunk-count fix deployed |
| youtube | 1 | 0 | healthy |
| reddit | 0 | 5 | RATELIMIT + r/NudistMen link whitelist; fixes deployed, rate limit transient |

All 5 X accounts report valid tokens; direct X media upload still returns
`HTTP 402 credits depleted`, so X rides the Sociamonials fallback.

## 2. Scheduling is working, but the backlog is jammed (main finding)

There are **3,089 pending targets** spread over 243 distinct days. The Publish
Center allocated each submit from its own base time with a global 5-minute
stagger that reset to zero on every submit, so independent campaigns collided:

- **363 exact collisions** = **1,018 targets** share an `(account_ref,
  schedule_at)` minute.
- Worst case: `account:116` (NW TG 本人) has **4 posts queued at the exact same
  minute** (`13:10:00`) every day, and up to **7 posts/day** on one account.
- **417 account-days have >2 posts.**
- **508 `(account, media)` duplicate groups = 1,016 pending targets** — the same
  video/image queued to the same account 2–4 times through different campaigns
  (e.g. `20260822145825986_pub.mp4` is queued 4× to `account:118` at
  `2026-10-05T13:00:00`).

Per-account pending load (top): 118=429, 119=428, 116=428, 124=421, 123=421,
120=168, 121=139, 117=135, 103=131, 77=130.

### Fix made (not yet deployed): collision-free slot allocator

`myUtils/publish_orchestrator.py` now has `_load_booked_slots()` +
`_next_free_slot()`. A scheduled submit consults the account's existing
pending/retrying bookings (and the ones allocated earlier in the same submit)
and walks forward until it is at least `SAU_PUBLISH_MIN_GAP_MINUTES` (default
30) from any existing booking and within `SAU_PUBLISH_MAX_PER_DAY` (default 3)
per account per day. `publishNow` is unchanged. Tests:
`tests/test_publish_center.py::NextFreeSlotTests` (6 tests).

This stops **new** jams; it does not touch the 1,018 already-queued collisions.
Those need a one-time cleanup (see §7).

## 3. Failed-target recovery

452 targets are `failed`. Breakdown after removing (a) failures the publisher
classified as permanent and (b) targets whose media already reached the same
account successfully:

| platform | failed | dup/skip | permanent | recoverable |
|---|---:|---:|---:|---:|
| twitter | 245 | 2 | 64 | 179 |
| facebook | 48 | 1 | 2 | 45 |
| tiktok | 35 | 8 | 2 | 25 |
| bluesky | 32 | 1 | 19 | 12 |
| telegram | 29 | 0 | 27 | 2 |
| threads | 23 | 3 | 0 | 20 |
| reddit | 19 | 0 | 7 | 12 |
| instagram | 16 | 2 | 2 | 12 |
| youtube | 3 | 0 | 1 | 2 |
| nw_sw_blog | 2 | 0 | 2 | 0 |
| **total** | **452** | **17** | **126** | **309** |

New tool `scripts/recover_failed_targets.py` (dry-run by default) reschedules
only the recoverable set to fresh, spaced slots using the same allocator. A
2-day lookback currently plans **29 targets** (46 permanent and 3 duplicates
skipped). It has **not** been applied. Important: because the accounts already
hold hundreds of pending posts, the allocator pushes some recovered posts out
to November. **Deduplicate the backlog first** (§7), then recover.

## 4. X / Twitter via Sociamonials

- Fallback map (`SAU_ACCOUNT_TO_SOCIAMONIALS`): 77→14100, 123→13425,
  124→13426, **103→14099 (added in dffa2e2)**; 107 has no Sociamonials X
  profile. All mapped refs are connected and `agent_can_publish_to=true`.
- 123 is cookie-mode; the fallback ignores the SAU token, so it still falls
  back for X.
- Delivery verification is now live: the worker only marks a fallback target
  succeeded when Sociamonials reports `delivered`; a `failed`/`needs_attention`
  hand-off is recorded as a real failure (`job 2842` post `10689041` was
  re-classified failed, correctly).

### X link restriction (remember this)

Sociamonials' X/Twitter hand-off **rejects a URL in the main post**. Fix made
(not yet deployed) in `myUtils/sociamonials_fallback.py`:

- `LINK_IN_MAIN_POST_FORBIDDEN_NETWORKS = {"tw"}` documents the rule.
- `compose_message_with_links()` strips every `http(s)://…` URL from the tweet
  body (truncating to 280 *after* stripping) and returns them.
- `publish_via_sociamonials()` appends the moved links to the follow-up reply
  (`first_comment`), merging with any existing `firstComment`, and logs a
  warning. Whether Sociamonials honours `first_comment` for `tw` is
  **unverified** — watch for a 422 on the next X fallback; if it is rejected,
  the fallback for a link-bearing X post must drop the link entirely.
- Non-X networks keep links in the body.
- Tests: `tests/test_sociamonials_fallback.py` (5 new cases).

## 5. Best posting time (data-limited)

`video_analytics_videos` holds only 69 rows (YouTube 41, Instagram 14,
Threads 7, TikTok 7); Instagram/Threads snapshots carry ~0 views, so an
engagement-by-hour ranking is not statistically usable. What the data does
show:

- YouTube: strong views in the 04:00–07:00Z and 15:00–16:00Z windows, but that
  reflects the existing upload times, not an independent optimum.
- The whole backlog is currently pinned to **13:00Z = 21:00 Taipei = 09:00 ET
  / 06:00 PT**. That is prime evening for the Taiwan/Chinese audience but
  pre-dawn for US Pacific.

Recommendation until analytics are richer: for the global English audience add
a second daily window around **16:00–18:00Z** (09:00–11:00 PT) and keep 13:00Z
for the Chinese audience, then let `analytics_advisor` re-rank once a few weeks
of Instagram/TikTok/Threads snapshots exist. Do not mass-shift the existing
schedule on this evidence.

## 6. Pipeline / metadata / storage

- **Offload / Google archive**: `logs/offload.log` last run 2026-10-05T08:42Z,
  exit 0, 407 local files, `healthy: true`. Rclone backends
  `gdrive-videofile`, `gdrive-uploads`, `gdrive-generated` are enabled; DO
  Spaces `sau-media` is the default backend.
- **Digest**: `logs/publish_digest.log` shows `sent: true`; the earlier
  `lastSent: null` snapshot was taken before the day's send.
- **Metadata**: `file_records` holds 729 rows (321 with a storage key). The
  `data/file_metadata.db` file is empty and is not referenced by any code —
  the metadata of record is `file_records`.
- **Known pipeline gap (already reported, not fixed)**: `media_prep` constrains
  dimensions/fps/size but **not duration**, so an 813 s source still needs a
  manual short cut for Threads/TikTok (300 s / other caps). This is why
  Threads/TikTok keep failing on that campaign.
- R2-hosted **video** fallbacks can be accepted by Sociamonials and then fail
  delivery ("Video URL not accessible after retries"); the worker now records
  that as a failure instead of a silent success.

## 7. Prioritized actions

1. **Deploy** the two code changes (X link guard + anti-jam allocator) and
   `scripts/recover_failed_targets.py`. No commit/restart was performed here.
2. **Clean the scheduled backlog before recovering failures.** 1,016 pending
   targets are `(account, media)` duplicates and 1,018 share a minute. Delete
   the redundant duplicates (keep the earliest), then run
   `scripts/recover_failed_targets.py --lookback-days 2` (dry-run → `--apply`).
3. **Decide the daily cadence** — `SAU_PUBLISH_MAX_PER_DAY` (default 3) and
   `SAU_PUBLISH_MIN_GAP_MINUTES` (default 30) are the new anti-spam knobs.
4. **X credits**: top up to restore direct X media posting, or accept
   Sociamonials-only (account 107 has no X fallback).
5. **Watch the first link-bearing X fallback** for a `first_comment` 422 and
   confirm whether the thread reply is honoured.
6. **Duration-aware media prep** so Threads/TikTok stop needing a manual short
   cut.
