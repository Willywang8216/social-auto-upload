# `_inbox_cache` (12 GB): should an agent clean it? — NO, not as "clear the cache"

Date: 2026-10-09 · measured against the live host and DB

## Short answer

**Do not tell an agent to "clean out the cache".** The instruction is unsafe as
written, because `_inbox_cache` is **not a cache** in the evictable sense:

* **105 of its 107 files exist ONLY on local disk.** They are not on Google
  Drive (verified: `sau/videoFile/_inbox_cache` holds **2** files), and none has
  a restore route (`storage_key` + `storage_backend_id`, or a CDN url: **0 of
  105**). Deleting them is **permanent loss**.
* **82 of the 105 files are referenced by a publish job**, and 37 are needed by
  targets that are still **pending** — the Pride invitations and the
  time-sensitive news you just scheduled.
* The disk is at **92% (89 G of 97 G, 8.4 G free)**, so the pressure is real —
  but this is the wrong 12 GB to reclaim blindly.

## What is actually in there

| | files | bytes |
| --- | ---: | ---: |
| total | 105 | 12.69 GB |
| needed by live (pending/retrying/running) targets | 37 | 0.02 GB |
| duplicate copies of a twin (same content hash) | 20 | 3.01 GB |
| never referenced by **any** job, any status | 65 | 12.67 GB |

The bulk is a handful of oversized raw uploads, kept once per re-upload:

```
573.5 MB  *_SFW face exercises funny video.mp4      x4   (the 4K/60 source, 574 MB each)
326.6 MB  *_SFW 20260822082414627__nsfw.mp4
325.4 MB  *_20260822154556272__nsfw.mp4
325.0 MB  *_20260822155033165__nsfw.mp4
324.3 MB  *_20260907083916866__nsfw.mp4
```

Those four `face exercises` copies alone are **2.3 GB of the same 574 MB file**,
re-uploaded under different UUIDs. That is the real waste, not the directory.

## What a safe cleanup looks like

Only these two categories, and only with a real backup step:

1. **Duplicate copies (20 files, 3.01 GB).** Keep one of each content hash —
   preferring a copy a live target needs — and remove the rest. Safe *if* the
   kept twin stays. A script must prove that twin exists and is readable first.
2. **Never-referenced files (65 files, 12.67 GB).** Not unknown to the DB — 82
   of 105 have a `file_records` row — but no job has ever pointed at them. The
   catch: **they are not on Drive**, so removing them also removes the only copy
   of that source material. That is a content decision, not a cache decision.

**Recommended sequence:**

1. Upload the never-referenced originals to Drive **first** (the offload cron can
   do it; `offload_to_drive.sh` verifies every byte with `rclone check` before it
   unlinks anything).
2. Register the mapping (`file_records.storage_key` + `storage_backend_id`) so
   they stay restorable — `scripts/reconnect_drive_artifacts.py` does this.
3. *Then* the local copies are genuinely redundant and can go.
4. Keep the 37 live-needed files untouched regardless.

That reclaims up to **~12 GB safely**, at the cost of one Drive upload — with no
possibility of data loss.

## What NOT to do

* Do **not** run `rm -rf videoFile/_inbox_cache`. 37 live targets depend on those
  files and 8.4 G of headroom will not save a lost Pride invitation.
* Do **not** treat `_inbox_cache` as regenerable. It is not: the source videos
  came from the operator's phone/Drive and the pipeline only ever *adds*.
* Do **not** combine this with a `docker system prune -a`. Your crontab comment
  already warns that the `-a` class previously broke overleaf-mongo and put
  sharelatex into a 1953-restart crash loop; and `docker system df` reports
  **0 reclaimable volumes** — the 3.96 G of images is exactly that `-a` class.

## If you want it automated

A safe agent instruction would be:

> Upload every `videoFile/_inbox_cache` file that has no `file_records`
> restore route to Google Drive, verify each with `rclone check --size-only`,
> register the mapping, and only then delete local copies of files that are (a)
> present on Drive and (b) not referenced by any pending/retrying/running target.
> Report what you skipped and why. Dry-run first.

The distinction that matters: **move it to Drive, then delete the local copy** —
never "clear the cache".
