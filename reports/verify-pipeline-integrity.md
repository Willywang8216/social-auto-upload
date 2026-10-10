# Pipeline integrity verification after the 2026-10-10 cleanup

Date: 2026-10-10 · Repo HEAD at verification: `d2cb8d6` · DB snapshot:
`/tmp/snapshot_verify.db` (sha256 `3d027e47b8082d0f3fbce8ac383ad4a7406058f89677941b2181df7c5b64e5f5`,
taken 19:48 +0800) · Drive listing: `rclone lsf GDrive-willywang8216:sau -R --files-only --format ps`
(1,385 objects, the same snapshot the operator used).

Companion: `reports/legacy-drive-analysis-2026-10-10.md`, `reports/purge-legacy-drive.md`.
Tooling written this session: `scripts/verify_pipeline_integrity.py`,
`scripts/sample_pipeline_restore.py`.

## Verdict

**Scheduling + publishing is intact for 959 of 1,334 live targets; the other 375 are a
pre-existing prepared-media corruption, not a cleanup regression — and I fixed the
worker bug that was failing to notice and would have handed 1–4 byte files to publishers.**

---

## 1. Independent count (my own script, not the operator's)

`scripts/verify_pipeline_integrity.py` recomputes the exact resolution the worker uses
(`myUtils/worker.py:1993 _ensure_artifact_paths_local`, `:2032 _record_for`), then asks
whether that source is provably present: a local file, a `file_records` row whose
`endpoint/storage_key` object exists in the live Drive listing, a `campaign_artifacts`
row, `storage_cdn_url`, or a reachable artifact `public_url`. It probes the public URLs
the way the worker does (percent-encoding first).

```text
$ .venv/bin/python scripts/verify_pipeline_integrity.py \
    --db /tmp/snapshot_verify.db --min-media-bytes 256 --probe-public-url
live targets (pending/retrying/running): 1334
artifact resolution categories: {'remote': 682, 'public_url': 36, 'none': 375, 'local': 241}
recovery reasons: {'drive:rclone:sau/generated': 233, 'drive:rclone:sau/published/videoFile': 12,
 'drive:rclone:sau/published/generated': 46, 'artifact_public_url_after_placeholder': 35,
 'artifact_public_url': 1, 'drive:rclone:sau/inbox/videoFile': 257,
 'public_url_dead_http_400': 10, 'public_url_dead_http_404': 365,
 'on_disk': 241, 'drive:rclone:sau/videoFile': 45, 'drive:rclone:sau/inbox/generated': 89}
```

| outcome | targets | how |
| --- | ---: | --- |
| on disk locally | 241 | `worker._resolve_media_path` path exists |
| restore from Drive (`endpoint/storage_key`) | 682 | byte-verified against the Drive listing |
| restore from artifact `public_url` | 36 | HTTP 200/206, real bytes (35 after the placeholder fallback) |
| **obtainable** | **959** | |
| prepared media gone (1–4 byte stub) | 356 | raw source still on Drive for all 356 |
| test fixtures (`demo.mp4`/`nonexistent.mp4`) | 19 | no Drive object, share URL 404 |
| **unresolved** | **375** | |

So the operator's `1334/1334 resolvable` is **not correct**: 375 are not. The audit
counted a Drive object as "resolvable" without checking that the object holds the media
it claims (see §3). Per-platform unresolved: telegram 94, twitter 96, bluesky 92,
reddit 47, nw_sw_blog 46 (all 356 stubs + 19 fixtures).

---

## 2. Restore proofs — 28 byte-exact fetches, all 10 platforms, both tiers

`scripts/sample_pipeline_restore.py` picked one target per `(platform, tier)` plus large
files and fetched each through the real
`myUtils.media_remote_storage.download_from_backend` (the same call the worker makes at
`myUtils/worker.py:2136`). Every size was compared to the Drive listing size.

```text
sampled 28 objects, 1,129,957,400 bytes total
all match True
legacy 16, tiered 12
endpoints: sau/generated, sau/inbox/generated, sau/inbox/videoFile,
           sau/published/generated, sau/published/videoFile, sau/videoFile
platforms: bluesky, facebook, instagram, nw_sw_blog, reddit, telegram,
           threads, tiktok, twitter, youtube
```

| platform | tier | target | bytes fetched | endpoint |
| --- | --- | ---: | ---: | --- |
| bluesky | legacy | 5322 | 3,547,263 | `sau/generated` |
| bluesky | tiered | 4144 | 27,484 | `sau/inbox/videoFile` |
| facebook | legacy | 5281 | 13,434,879 | `sau/generated` |
| facebook | tiered | 5702 | 9,886,238 | `sau/inbox/generated` |
| instagram | legacy | 5282 | 13,434,879 | `sau/generated` |
| instagram | tiered | 5703 | 9,886,238 | `sau/inbox/generated` |
| nw_sw_blog | legacy | 5324 | 3,547,263 | `sau/generated` |
| nw_sw_blog | tiered | 4146 | 27,484 | `sau/inbox/videoFile` |
| reddit | legacy | 5325 | 3,547,263 | `sau/generated` |
| reddit | tiered | 4147 | 27,484 | `sau/inbox/videoFile` |
| telegram | legacy | 5326 | 3,547,263 | `sau/generated` |
| telegram | tiered | 4148 | 27,484 | `sau/inbox/videoFile` |
| threads | legacy | 5287 | 13,434,879 | `sau/generated` |
| threads | tiered | 3859 | 571,987 | `sau/published/videoFile` |
| tiktok | tiered | 5709 | 105,547,761 | `sau/inbox/videoFile` |
| tiktok | legacy | 5549 | 19,176,858 | `sau/videoFile` |
| twitter | legacy | 5328 | 3,547,263 | `sau/generated` |
| twitter | tiered | 4149 | 27,484 | `sau/inbox/videoFile` |
| youtube | legacy | 5554 | 29,549,718 | `sau/generated` |
| youtube | tiered | 5714 | 9,886,238 | `sau/inbox/generated` |
| telegram | legacy | 4624 | 116,236,330 | `sau/videoFile` |
| telegram | legacy | 4732 | 113,546,534 | `sau/videoFile` |
| telegram | legacy | 4694 | 110,527,380 | `sau/videoFile` |
| telegram | legacy | 4596 | 110,363,939 | `sau/videoFile` |
| telegram | legacy | 4595 | 110,207,218 | `sau/videoFile` |
| telegram | legacy | 4747 | 109,396,463 | `sau/videoFile` |
| nw_sw_blog | tiered | 5609 | 108,498,063 | `sau/published/generated` |
| twitter | tiered | 5630 | 108,498,063 | `sau/published/generated` |

All 28 sha256 values are in `/tmp/sample_restore_results.json`; sizes match the Drive
listing exactly. This includes the `sau/published/videoFile` object the operator
spot-checked (target 3859, 571,987 B) and both legacy and tiered roots.

---

## 3. The critical finding: 1–4 byte placeholder Drive objects

The Drive listing contains 63 objects under `generated/**` used by live targets whose
**actual bytes are 1 or 4 bytes**. They are not a metadata mismatch: the object itself is
a one-byte `x`.

I proved the old worker would restore one and call it success. Running the **real**
`worker._ensure_artifact_paths_local` on target 3908 (pre-fix), with the destination in a
sandbox:

```text
target 3908 payload artifact:
  local_path = /app/generated/campaigns/campaign-2404/ebaf..._SFW_pub.mp4
  public_url = https://pub-9915b1494003455c9ab872fd7094e64e.r2.dev/campaigns/2404/videos/ebaf..._SFW_pub.mp4
  source_file_record_id = 572

$ python ... worker._ensure_artifact_paths_local(payload, db_path=...)
RESULT file exists: True size: 1
first bytes: b'x'          # <-- the 1-byte Drive object, not the R2 video
```

Why: `_record_for` (`myUtils/worker.py:2032`) treats **any** artifact whose path contains
`generated/` as a generated artifact and returns the `file_records` row found by that
path — target 3908's row 707 (`storage_backend_id=448`, `storage_key=campaigns/campaign-2404/ebaf..._SFW_pub.mp4`,
`filesize=1.0`). The backend restore then downloads the 1-byte object, and
`_download_atomically` accepted it because it only rejected a 0-byte file. The artifact's
working R2 `public_url` was never tried.

Scope across the live queue (frozen snapshot):

```text
live targets resolving to a < 256 B Drive object : 391 (pre-fix)
  after the fix: 35 recover from the artifact public_url
  remain unresolved                                  : 356, across 46 unique stub objects
  earliest unresolved stub schedule                  : 2026-10-17T13:00
  raw source (source_file_record_id) present on Drive: 356 / 356
```

Examples of the fallback difference:
* target 3908 — R2 `..._SFW_pub.mp4` serves 200/206; post-fix the worker restored
  **117,228,306 bytes**, header `b'\x00\x00\x00 ftypisom'`.
* target 4650 — URL had a raw space, normalised by the worker; post-fix restored
  **102,017,465 bytes**, header `b'\x00\x00\x00 ftypisom'`.
* target 4181 — no working fallback; post-fix raises
  `MediaRestoreError: ... download is not the media it claims to be: ... (1 bytes)` and
  the dead share URL logs `HTTPError 404`.

### This is not a today's-cleanup regression

The stub objects' Drive ModTime is **2026-10-09T01:21:31Z** and their
`file_records.upload_time` is 2026-10-03 … 2026-10-09 (46 records share the exact second
`2026-10-03 00:38:21`). Today's quarantine did not touch them: `sau/trash/2026-10-10/`
contains no stub basename (checked all 63), and the newest `generated/**` stub predates
today. Today's cleanup only moved 35 unused `videoFile` objects to trash (see §5).

---

## 4. Unrecoverable cases — exact reasons, quantified

### 4a. 356 targets: prepared media genuinely gone, raw source intact

For each, the prepared `*_pub.mp4` is a 1–4 byte Drive object and the artifact
`public_url` (`https://socialupload.iamwillywang.com/getFile?filename=...`) returns
HTTP 400 (10) or 404 (346):

```text
UNRESOLVED target 4181 (bluesky, pending):
  local_path  = /app/generated/campaigns/campaign-2446/d162..._part3__nsfw_pub.mp4
  public_url  = https://socialupload.iamwillywang.com/getFile?filename=campaigns/campaign-2446/d162..._part3__nsfw_pub.mp4
  source id   = 612
  resolve     = ('none', 'public_url_dead_http_404', <share url>)
```

This is **not** a registration that fails to resolve — it resolves to a real object that
is placeholder-sized. It is prepared-media loss. All 356 have their **raw source** on
Drive (66.9 GiB total; e.g. file_record 612 = `_inbox_cache/d162..._part3__nsfw.mp4`,
239,438,308 B in `sau/inbox/videoFile`). The worker deliberately refuses to substitute a
raw source for a derived artifact (`96c3a4b`), so recovering them requires **re-preparing**
the campaign media, not a restore-path change.

### 4b. 19 targets: dead test fixtures

These are the `nonexistent.mp4` / `demo.mp4` fixtures created during the 2026-10-08
duplicate-resubmit test (`source_file_record_id` 1187/1188). Their `file_records` row has
**no** `storage_backend_id`/`storage_key`/`storage_cdn_url`, no Drive object exists, and
the share URL is 404:

```text
fr#1187 nonexistent.mp4  file_path='nonexistent.mp4'  backend=NULL key=NULL
fr#1188 demo.mp4         file_path='/home/will/.../videoFile/_inbox_cache/demo.mp4' backend=NULL key=NULL
$ curl -r 0-1023 https://socialupload.iamwillywang.com/getFile?filename=nonexistent.mp4
404   (content-type: application/json)
```

Target ids: 5658–5664, 5668–5672, 5675–5680, 5686. These are genuinely unrecoverable and
should be cancelled; the 15 identical fixtures that already failed are already cancelled.
(`32a1991` shows the operator's permanent-failure purge already cancels the failed ones.)

---

## 5. The legacy trees — are they the only copy, and do they restore?

Yes. Of the 156 unique Drive objects the live queue restores from `endpoint/storage_key`,
**84 live only in the untiered legacy roots** (`sau/videoFile`, `sau/generated`; 5.49 GiB)
and 72 live in a tiered tree (1.01 GiB). The 28-object sample above includes **16 legacy
restores**, all byte-exact, so they are safe where they are.

The operator's `915 NEEDED objects` figure is a broader reference set (it also counts
`campaign_artifacts` and every live `file_records` mapping, including audio/images/previews),
not just the primary payload artifact; my 84 is the primary-media subset of exactly the
objects a pending target must fetch. The direction is the same and confirmed: those objects
are needed and they restore.

The purge was safe. `sau/trash/2026-10-10/` currently holds **35** `videoFile` objects
(another session quarantined more after the 5-object proof). Checking every quarantined
basename against every live drive path:

```text
quarantined objects: 35
live drive keys whose basename is in quarantine: 0
exact live-key matches in quarantine: 0
```

---

## 6. Offload cron health

```text
$ tail -4 logs/offload.log
[2026-10-10T11:47:03Z] keeping 304 file(s) local (in-flight / due soon / unrecorded / _library)
...
[2026-10-10T11:21:47Z] offload done rc=0 local videoFile=299 uploads=0

$ grep "offload done rc=" logs/offload.log | tail
... 2026-10-10 rc=0: 24 runs, rc=1: 0
$ grep "offload done rc=1" logs/offload.log | tail -1
[2026-10-09T17:43:38Z] offload done rc=1 local videoFile=424 uploads=0
```

* Every run today exited `rc=0`; the last non-zero run was **2026-10-09 17:43** (the
  `--files-from` + `--filter` conflict, since fixed).
* The keep-local exclusion build is intact at `offload_to_drive.sh:536-548` (in-flight,
  due-soon, unrecorded, `_library`) and is logged on every run
  (`keeping N file(s) local (in-flight / due soon / unrecorded / _library)`).
* No pending target was stranded by an offload: every non-placeholder target either is on
  disk or restores from Drive (§1, §2).

---

## 7. What I fixed

**Bug:** the restore path accepted any non-empty download as valid media, so a 1-byte
placeholder was treated as a successful video restore (`myUtils/worker.py:1863`
`_download_atomically`). This is what made the operator's "resolvable" audit look clean.

**Fix** (in `myUtils/worker.py`, lines 1824–1880): `_looks_like_media()` checks the
container signature for the media extensions the pipeline restores (jpg/jpeg/png/gif/bmp/
webp/webm/mkv plus ISO-BMFF `ftyp`/`moov`/`mdat`/`free`/`wide`/`skip` for mp4/mov/m4v/3gp).
A download whose name claims a media type but whose bytes do not match is rejected, which
lets the existing fallback chain run (artifact `public_url` → `storage_cdn_url` →
`media_assets`) and otherwise fails loudly instead of publishing garbage. Unknown
extensions fail open.

**Tests** (`tests/test_worker_media_restore.py`, +3):
* `test_media_signature_accepts_real_containers_and_rejects_placeholders`
* `test_one_byte_placeholder_restore_is_rejected`
* `test_one_byte_placeholder_falls_through_to_public_url`

Existing media-restore fixtures that wrote arbitrary bytes were updated to a valid
ISO-BMFF header (`_MP4_FIXTURE`).

> Attribution note: a concurrent session committed `myUtils/worker.py` (including this
> guard) inside commit `88343f6` while I was editing tests, so the code change is already
> in HEAD `d2cb8d6`; `tests/test_worker_media_restore.py` is still uncommitted in the
> working tree. The Docker worker was built before the fix and must be rebuilt/restarted
> to pick it up.

**Full suite** (task 6):

```text
$ .venv/bin/python -m pytest tests/ --ignore=tests/test_security_http.py -q
1608 passed, 1 skipped, 161 subtests passed in 116.68s (0:01:56)
```

(The 1608 vs the 1592 baseline includes test additions by concurrent sessions; this
session adds 3 to `test_worker_media_restore.py`, which alone is 29 passed.)

---

## 8. One-line verdict

**Scheduling + publishing is safe for the 959 live targets whose media is real; the
worker bug that silently consumed 1–4 byte placeholder Drive files is fixed and tested,
and the remaining 375 (356 reconstructions + 19 dead fixtures) must be regenerated or
cancelled rather than published.**

### Follow-ups for the operator

1. **Rebuild/restart the `social-auto-upload` container** so the media signature guard in
   `88343f6`/HEAD is live.
2. **356 prepared-media-gone targets** (earliest 2026-10-17): re-run campaign prep from
   their raw sources, or cancel. They will now fail with a clear `MediaRestoreError`
   instead of publishing a 1-byte file, so the permanent-failure purge can pick them up.
3. **19 test fixtures** (targets 5658–5664, 5668–5672, 5675–5680, 5686): cancel.
4. **63 placeholder `generated/**` objects / 46 unresolved**: consider a `file_records`
   cleanup that drops the placeholder `storage_key` mapping (or a constraint that a
   generated `filesize < 64` is never registered as restorable), so the data cannot
   regress even if the guard is bypassed.

### Commands run (audit)

| command | result |
| --- | --- |
| `rclone lsf GDrive-willywang8216:sau -R --files-only --format ps` | 1,385 objects |
| `scripts/verify_pipeline_integrity.py --db /tmp/snapshot_verify.db --min-media-bytes 256 --probe-public-url` | 1334 live → 959 obtainable / 375 unresolved |
| `scripts/sample_pipeline_restore.py --min 28` | 28 restores, 1,129,957,400 B, all byte-exact |
| real `_ensure_artifact_paths_local` on 3908 / 4650 / 4181 | 117,228,306 B / 102,017,465 B / MediaRestoreError |
| `rclone lsjson` on the stub objects | `Size: 1`, `b'x'` |
| `rclone lsf .../sau/trash/2026-10-10 -R` | 35 objects, 0 referenced by live targets |
| `tail logs/offload.log` | last run `rc=0` at 2026-10-10T11:21:47Z |
| full pytest | 1608 passed, 1 skipped |
