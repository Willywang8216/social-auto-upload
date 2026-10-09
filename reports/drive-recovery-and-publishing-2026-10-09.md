# Drive recovery, purge decision, Threads cap, and real publishing

Date: 2026-10-09 · all changes deployed and verified live

## Your question: which YouTube account is unverified?

**Account 108 "Willy Dev tutor" is the one that could not be verified** — but not
because the channel is unverified. Its **OAuth token expired on 2026-07-16**
(three months ago), so the API returns `401 Unauthorized` and its channel status
is simply unknown.

**Account 110 "Itswill_YT" (itsnakedwill) is verified**: the API reports
`longUploadsStatus=allowed`, `privacyStatus=public`, `isLinked=true`.

Since you confirmed all YouTube videos are verified, I made **no change** to the
YouTube cap — it stays at 12 h. The only outstanding item is re-consenting
account 108; until then it cannot publish to YouTube at all.

## The purge: checking Drive first changed the answer completely

You asked to purge unrecoverable targets, but to use the media if it is on Google
Drive. Doing it in that order meant there was nothing to purge:

| | count |
| --- | --- |
| artifacts with no local file, no HTTPS URL, no storage mapping | 648 |
| … **actually present on Google Drive** | **543** |
| … genuinely absent from Drive | 105 |
| referenced by a live target | 136 |
| … of those, on Drive | **136** |
| … of those, genuinely gone | **0** |

**Purging would have destroyed 136 recoverable live targets.** Instead they were
reconnected:

* new `scripts/reconnect_drive_artifacts.py` looks each artifact up on Drive by
  its path relative to `sau/videoFile`, `sau/generated` and `sau/uploads` and
  writes the same `file_records` mapping the offload cron writes, so the worker's
  existing restore path handles it. Dry-run by default; it only ever adds or
  updates that mapping.
* **543 artifacts reconnected.** Verified by restoring one for real: target 4632
  → 207,430,550 bytes from Drive.

The 105 genuinely-absent artifacts are referenced **only** by
`cancelled`/`failed`/`succeeded` targets — nothing live. `scripts/purge_unrecoverable_targets.py`
exists for them and cancels a target only when every artifact it needs is
unrecoverable, it is pending, older than `--min-age-days`, and never running.
Its current verdict: **0 targets to purge.**

Two bugs were caught while building it, both because the live data disagreed:
it compared `schedule_at` (a future publish time) against the age cutoff, which
skipped everything; and it ignored the payload's own `public_url`, so it proposed
cancelling five targets that restore fine from R2 — the very media an earlier fix
had recovered.

## Threads: 1,000 MB

`MEDIA_MAX_MB["threads"]` was 1024 in a decimal-MB table, allowing
1,024,000,000 bytes on a platform that documents 1 GB. Now **1000**, with
`THREADS_MAX_VIDEO_BYTES = 1,000,000,000`. Two tests hardcoded 1024; one now
reads the table so cap and plan cannot drift.

## Really publishing: Sociamonials

The storage quota was **99.3% full (3.97 of 4.0 GB)** — the state that produced
`HTTP 422 storage_quota_exceeded`. The dedupe removed 18 duplicate assets and
**reclaimed 1.95 GB**, taking it to **50.6% (2.02 GB used, 1.98 GB free)**.

The fallback also **refused** any video longer than the target network's cap,
where the direct path has always split. It now splits and uploads the first part.

Live results on the real X targets:

| target | before | after |
| --- | --- | --- |
| 1994 | fallback failed | **succeeded** (post 10716819) |
| 2241 | fallback failed | **succeeded** |
| 2830 | `tw video duration 222s exceeds the 140s limit` | **succeeded** (post 10716826) — split into two 111s parts |
| 2531 | fallback failed | failed: its media is one of the 105 **genuinely gone** |
| 2054 | fallback failed | still processing a 1 GB upload |

`succeeded` targets overall: **1029 → 1039**.

## Deployment note

GitHub stopped creating Actions runs after commit `edba1f3`, so the image
workflow did not fire for the last four commits and `docker-compose.yml`'s
`pull_policy: always` kept restoring the older image. The image was therefore
built locally from the current commit (verified to contain the fixes) and
deployed; the workflow file itself is valid and unchanged on `main`.

Also repaired: `logs/jobs/*.log` were root-owned from before the uid fix, so the
container (uid 1000) could not append and job logging was failing with
`PermissionError`. Ownership normalised to 1000:1000; nothing deleted.

## Test suite

**1474 passed, 1 skipped** (was 1468 at the start of this turn).
