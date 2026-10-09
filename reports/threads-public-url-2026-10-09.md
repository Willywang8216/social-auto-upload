# Threads/Instagram/TikTok "requires a public_url": fixed and verified

Date: 2026-10-09
Commits: `a565a71` (fix + secret-leak guard), `43b2654` (test placeholder)

## What was reported

Your message ended with *"threads need a public url. fix it"*. The failure log
agreed - **7 targets** across three platforms:

```
PreparedPublishError: Threads video publish requires a public_url      (3x)
PreparedPublishError: Instagram video publish requires a public_url    (3x)
PreparedPublishError: TikTok video publish requires a public_url       (1x)
```

## Root cause: the payload was built from a stale snapshot

Threads, Instagram, Facebook and TikTok fetch media by URL - their APIs have no
upload endpoint - so the publish payload must carry a `public_url`.

Campaign 2555 had one. The payload did not:

```
artifact  kind                public_url
2459      watermarked_video   null        <- what the payload carried
2460      remote_upload       https://…   <- what it needed
```

The watermarked row is written **first**; the remote upload lands ~3 seconds
later (09:44:12 vs 09:44:15). The payload was built from the window between
them, so it captured the URL-less copy.

**Resubmitting could not help**: resubmit flips the target's status but reuses
the stored payload, so the missing URL persisted.

## Fix

The worker now backfills a missing `public_url` at publish time, from a sibling
artifact of the same `source_file_record_id` - preferring one whose
`artifact_kind` matches, so a watermarked payload gets the watermarked upload's
bytes. Best-effort and never raises, so it cannot block a publish that might
otherwise work.

Verified against the real failure before deploying: backfilling job #5626's
payload resolved artifact 2459 to the campaign's actual R2 URL.

## Verified live

| target | platform | before | after |
| --- | --- | --- | --- |
| 5558 | instagram | requires public_url | **succeeded** |
| 5563 | threads | requires public_url | **succeeded** |
| 5601 | instagram | requires public_url | **succeeded** |
| 5649 | instagram | requires public_url | **succeeded** |
| 5654 | threads | requires public_url | **succeeded** |
| 5626 | threads | requires public_url | **different error** (see below) |
| 11 | tiktok | requires public_url | **different error** (see below) |

**5 of 7 fixed.** The other two now fail on genuine, unrelated problems the fix
exposed:

* **5626 (threads)** - the video is 600.1 s and Threads caps at 300 s. The
  publisher correctly refuses rather than sending an oversized file.
* **11 (tiktok)** - `LookupError: Account not found: id=87`. That account has
  been deleted.

## A separate, systemic gap: Threads has never been split

Investigating 5626 found something bigger:

```
splits tagged split_for=twitter : 38
splits tagged split_for=threads : 0
```

No Threads-sized split has **ever** been created. The splitter's plan-building
logic is correct - re-running it on campaign 2555's real inputs produces the
right plan:

```
(140s, 512MB) -> twitter
(300s, 1024MB) -> threads      <- never persisted
(600s, 300MB) -> bluesky
```

and `split_to_seconds` on the same source yields 3 parts of ~200 s each. So the
capability works; the earlier prep runs simply did not produce it, and every
already-prepped campaign carries a payload without it.

**Consequence:** any Nakedwill/Sexualwill video over 300 s cannot publish to
Threads even though a valid split is one call away. Fixing this for existing
campaigns needs a re-prep (or a targeted split backfill); it is not fixed by the
URL work above.

## Also fixed: `git add -A` could commit secrets

The first attempt at this commit was rejected by GitHub's pre-receive hook. The
cause is worth recording:

`git add -A` staged `.env.bak-*` files - **every API key and the Telegram bot
token** - plus `rclone-cache.conf`, which held a **live Google Drive OAuth
refresh token**. `.env` was ignored but its timestamped backups were not.

Nothing reached the remote (the hook blocked it, and `git ls-tree origin/main`
confirms neither file is present). `.gitignore` now covers `.env.bak-*`,
`rclone*.conf`, `logs/` and `*.log*`, and the test that simulated a real token
used your actual chat id as a fixture - that is now a placeholder.

## Operator actions

1. **Threads duration** - decide whether to re-prep the affected campaigns so
   Threads-sized splits exist, or accept that >300 s videos stay Threads-less.
2. **TikTok account 87** - deleted; target 11 cannot succeed until it is
   reconnected or removed from its campaign.
3. Unchanged from before: X API credits (402), account 108 OAuth, the
   Sociamonials dedupe (1,949 MB, dry-run only).

---

# Follow-up (same day, commit 96d880e)

## The Threads split gap is now fixed

Two more defects surfaced while verifying 5626.

### a. The last part of an equal split was silently discarded

ffmpeg gives the final part of an equal split a slightly different duration:

```
part 1  200.083s
part 2  200.083s
part 3  200.048s   <- 35ms shorter
```

`_largest_fitting_parts` grouped parts by an **exact** duration match against the
maximum, so part 3 was excluded. Only 2 of 3 parts published — **a third of the
video never reached the platform, with no error**. Parts are now grouped by
`part_count`, which identifies the same plan without float equality.

### b. Threads had never been given a split at all

```
splits tagged split_for=twitter : 38
splits tagged split_for=threads : 0
```

New `scripts/backfill_platform_splits.py` adds the missing split for an existing
campaign. Dry-run by default; only **adds** artifacts, never mutates one. It also
handles the offload cron having removed the local copy (231 of 304 campaign
directories are empty by design) by fetching the hosted copy first.

Applied to campaign 2555 — the first Threads split ever created:

```
id    kind            part  split_for   url
2527  remote_upload   1     ["threads"]  yes
2528  remote_upload   2     ["threads"]  yes
2529  remote_upload   3     ["threads"]  yes
```

### Verified

A fresh submit of campaign 2555 now builds **3 Threads posts** (200.1s, 200.1s,
200.0s, all with URLs). Previously: 0 posts, and the publish failed on duration.

## Note on resubmit

Resubmitting an already-failed target reuses its **stored payload**, so it
cannot pick up a newly created split. Campaigns affected by the split gap need a
fresh submit from the UI rather than a resubmit of the old target.
