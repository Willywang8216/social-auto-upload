# Audit — media offload / download half of the pipeline

Date: 2026-10-09 (UTC)
Repo: `/home/will/social-auto-upload`
Auditor: read-only audit (no code changed). DB queries + live container experiments only.

Scope audited:

* `myUtils/media_remote_storage.py`
* the offload cron `offload_to_drive.sh` (installed at `crontab` line `17,47 * * * * /home/will/social-auto-upload/offload_to_drive.sh`)
* `myUtils/worker.py` restore paths (`download_from_backend` callers)
* the `generated/` cache and its restore path

Environment during audit:

* `social-auto-upload` container is Up, `/app` is the repo mount.
* Container env: `SAU_STORAGE_BACKENDS=share,do_spaces`, `SAU_SHARE_ENABLED=0`,
  `DO_SPACES_BUCKET=nw-assetsoffload`, `DO_SPACES_ENDPOINT=https://…r2.cloudflarestorage.com`,
  `DO_SPACES_CDN_URL=https://pub-9915b1494003455c9ab872fd7094e64e.r2.dev`,
  `RCLONE_CONFIG=/app/rclone-cache.conf`, `SAU_DEFAULT_RCLONE_REMOTE=Onedrive-Yahooforsub-Tao`.
* `SAU_VERIFY_MEDIA_URL` is **not set** (host `.env`, `docker-compose.yml`, container) → strict
  URL verification is off in production.
* `storage_backends` table has exactly four rows: `default` (do_spaces, stale `sau-media` /
  `sgp1.digitaloceanspaces.com`) plus three rclone rows `gdrive-videofile`/`gdrive-uploads`/`gdrive-generated`
  (bucket `GDrive-willywang8216`, endpoints `sau/videoFile|uploads|generated`).

---

## 1. Verified good

These were exercised, not assumed.

### 1.1 The rclone read-back path works end to end (real bytes moved)

I downloaded real objects through the exact spec the worker builds
(`remote_name=bucket`, `remote_root=endpoint`, `remote_path=storage_key`) inside the container,
writing only to `/tmp`:

| backend (storage_backends row) | spec | result |
|---|---|---|
| 417 gdrive-uploads `sau/uploads` | `GDrive-willywang8216:sau/uploads/b8fac499-…png` | rc=0, 1 768 386 bytes |
| 416 gdrive-videofile `sau/videoFile` | `GDrive-willywang8216:sau/videoFile/cbb5ec12-…mp4` | rc=0, 21 232 328 bytes |
| 448 gdrive-generated `sau/generated` | `GDrive-willywang8216:sau/generated/campaigns/campaign-2470/2d45dbf5…__nsfw_pub.mp4` | rc=0, 16 895 305 bytes = `file_records.filesize` |

The container remote `GDrive-willywang8216` is reachable (`rclone listremotes`), and
`rclone-cache.conf` contains only that remote. So the Drive half of the loop is functional.

### 1.2 Upload key == download key

* Offload: `offload_to_drive.sh:205` `register_verified_source` writes
  `storage_key = <path relative to the transfer root>` and `storage_backend_id` = the row whose
  `endpoint = sau/<root>`. Restore: `worker.py:1957-1963` calls `download_from_backend(backend, storage_key, …)`;
  `media_remote_storage.py:169-175` turns that into `rclone copyto <bucket>:<endpoint>/<storage_key>`.
* DO Spaces/R2: `media_remote_storage.py:191-198` `_spaces_key` builds
  `campaigns/<campaign_id>/<artifact_subdir>/<name>`; the returned URL is that same key under the CDN.
  Real DB rows show the key is a (percent-encoded) suffix of `public_url`:
  `campaign_artifacts` id=2312 `remote_path=campaigns/2490/videos/SFW Taipei Stonewall.mp4`
  `public_url=…r2.dev/campaigns/2490/videos/SFW%20Taipei%20Stonewall.mp4`. Of 215 rows with both
  columns, 204 match literally and the other 11 match after percent-encoding — no key drift.

### 1.3 Offload refuses to delete unless the remote copy is verified

`offload_to_drive.sh:263-311` `purge_verified_sources`: `rclone check --one-way` per file, then
`register_verified_source` commits the restore mapping (`BEGIN IMMEDIATE`), then the privileged
unlink. If registration or the check fails the local file is kept and the run is marked failed
(`failed=1`). Tested by `tests/test_offload_script.py` (size-mismatch keeps the file, one bad file
does not block siblings). The lock (`offload_to_drive.sh` `acquire_offload_lock`) prevents overlapping runs.

### 1.4 `verify_media_url` semantics match the docstring

`media_remote_storage.py:99-141` issues a **HEAD** (not a GET/body fetch), `allow_redirects=True`.
`status >= 400` (so **403 and 404**) → `False` (failure); `405/501` and any network exception → `True`
(fail open); `text/html` → `False`. Covered by `tests/test_media_remote_storage.py:230-245`. It is only
consulted when `SAU_VERIFY_MEDIA_URL` is truthy (`media_remote_storage.py:254-260`), which is **not set** in prod.

### 1.5 Restore loop works for the large majority of pending/in-flight targets

Read-only simulation of the real `worker._ensure_artifact_paths_local` (downloads stubbed so no media
was written) against a frozen DB snapshot of all `pending`/`running`/`retrying` targets:

* 386 payloads referenced at least one missing artifact
* **362 restored OK**, **24 failed** (section 2.1)
* restore branch histogram: backend (rclone) for the overwhelming majority, `public_url` for a few generated artifacts.

### 1.6 The 34 historical "no-source" artifacts are not referenced by the live queue

There are 34 `campaign_artifacts` rows (historical campaigns 15–43, 84, 1822) whose local file is gone
and which have no restorable DB source. A `publish_job_targets`/`publish_jobs` join found **0**
pending/running/retrying targets referencing any of them. They are dead history, not active risk.

---

## 2. Bugs

### BUG 1 (high) — non-generated artifacts ignore their own `public_url`; live R2 media is treated as unrestorable

**Where**
* `myUtils/worker.py:1930-1890` `_record_for` — for a path under `/generated/` it consults
  `campaign_artifacts`, but for any other path (`/videoFile/`, `/uploads/`) it does **not**; it only
  returns a `file_records` row.
* `myUtils/worker.py:1967-1979` — the "Try 2" branch that uses `artifact["public_url"]` is gated on
  `is_generated_artifact`.
* `myUtils/worker.py:1980-1988` "Try 3" and `:1990-2002` only use `row["storage_cdn_url"]` / `media_assets`.
* Net effect: for a **non-generated** artifact, `artifact["public_url"]` is never tried.

**Evidence**
Payload for target 3849 (`pending`, `twitter`, campaign 2398) has a single artifact:

```
local_path = /app/videoFile/_inbox_cache/104a7e2b5d3d49798d52cd4ba7fdecf9_SFW.jpg
public_url = https://pub-9915b1494003455c9ab872fd7094e64e.r2.dev/campaigns/2398/images/104a…SFW.jpg
source_file_record_id = 567
```

`file_records` row 567: `file_path=/app/videoFile/_inbox_cache/…jpg, storage_key=NULL,
storage_backend_id=NULL, storage_cdn_url=NULL`. The local file is gone.

Running the real `_ensure_artifact_paths_local` on that payload yields:

```
MediaRestoreError: Could not restore artifact /app/videoFile/_inbox_cache/104a…SFW.jpg
(file_record=/app/videoFile/_inbox_cache/104a…SFW.jpg): no usable storage source
```

and the R2 URL itself is alive and correct:

```
curl -r 0-1023 <r2 url>  →  HTTP 206, content-type image/jpeg, 1024 bytes
curl -r 0-1023 https://pub-…r2.dev/campaigns/2404/videos/ebaf85056…SFW.mp4 → HTTP 206, video/mp4
```

24 pending payloads fail this way; the 4 distinct artifacts are:

| artifact local_path | campaigns / targets | type |
|---|---|---|
| `/app/videoFile/_inbox_cache/104a7e2b5d3d49798d52cd4ba7fdecf9_SFW.jpg` | 2398, 2399 → targets 3849, 3859, 3860, 3861 | **real production media, live R2 URL** |
| `/app/videoFile/_inbox_cache/ebaf85056a89435fa20fc6bc1664354e_SFW.mp4` | 2404 → target 3916 | **real production media, live R2 URL** |
| `/app/nonexistent.mp4` | 2577 → targets 5658–5664 | test fixture |
| `/home/will/social-auto-upload/videoFile/_inbox_cache/demo.mp4` | 2578, 2579, 2587 → targets 5668–5680, 5686 | test fixture |

Both real artifacts are inbox-sourced media staged by `myUtils/inbox_drive.py:74`
(`videoFile/_inbox_cache/<uuid>_<name>`), uploaded to R2 by `campaign_media_prep`, and recorded only in
`campaign_artifacts.public_url`. Because the path is under `/videoFile/` it takes the non-generated branch.

**Root cause (two coupled defects)**

1. `campaign_media_prep.py:504-516` stores `remote_path` + `public_url` in `campaign_artifacts` but
   calls `campaign_store.add_campaign_artifact(...)` without a `storage_backend_id`.
   `myUtils/campaigns.py:658-695` has no `storage_backend_id` parameter at all — so **every**
   `campaign_artifacts.storage_backend_id` is NULL (verified: 215 rows have `remote_path`, **0** have a backend).
2. Even if it were set, the actual R2 backend is configured only by env (`do_spaces.client_from_env()` at
   `media_remote_storage.py:202`), and the `storage_backends` `default` row still points at DO Spaces
   (`sau-media` / `sgp1.digitaloceanspaces.com`), not the R2 bucket. So `remote_path` alone is not a
   usable restore key for these rows.

**Proposed fix**

In `_ensure_artifact_paths_local`, make Try 2 unconditional: when not downloaded, use
`artifact["public_url"]` (after `_normalise_artifact_url` + `_public_https_url`) as a fallback for
*any* missing artifact, not just generated ones. This is a 3-line widening of the existing guard
(`worker.py:1969`) and reuses the already-safe `_download_public_artifact`. It does not change the
precedence (backend restore still wins). Optionally also teach `_record_for`'s non-generated branch to
look up `campaign_artifacts` by `local_path` and return `public_url AS storage_cdn_url`.

**Tests to add**
* `test_worker_media_restore.py`: a non-generated (`<root>/videoFile/...`) artifact with only
  `public_url` set restores from that URL when `download_from_backend` fails / has no mapping.
* A regression asserting `_download_public_artifact` is called with the artifact URL for a
  non-generated missing path.
* A test that `add_campaign_artifact` persists `storage_backend_id` (once that column is populated).

---

### BUG 2 (medium) — `campaign_artifacts.remote_path` is dead data; its restore query can never match

**Where** `myUtils/worker.py:1866-1867`:

```sql
SELECT remote_path AS storage_key, storage_backend_id, public_url AS storage_cdn_url, local_path AS file_path
FROM campaign_artifacts WHERE local_path=? AND remote_path IS NOT NULL AND storage_backend_id IS NOT NULL
```

**Evidence** `SELECT COUNT(*) FROM campaign_artifacts WHERE remote_path IS NOT NULL AND
storage_backend_id IS NOT NULL` = **0** (215 rows have `remote_path`; all have NULL backend).
`add_campaign_artifact` never writes the column. So this branch is unreachable, and the only reason
generated artifacts restore is the separate `file_records`/`public_url` paths.

**Proposed fix** Either populate `storage_backend_id` at prep time (requires an R2 backend row that
matches the env) or delete the dead branch and rely on `public_url`. Do one, not both.

**Tests** A test that a generated artifact with `campaign_artifacts.remote_path` + a real backend row
restores via that key (would currently fail), or remove the branch and assert `public_url` is used.

---

### BUG 3 (medium) — no `share` branch in `download_from_backend`; share uploads are unrestorable

**Where** `myUtils/media_remote_storage.py:154-181`: dispatches `provider == "rclone"` to rclone, and
**everything else** to `do_spaces.client_from_row(backend)`. There is no `share` branch.
`myUtils/share_storage.py:102-171` uploads and returns `remote_name="share"` with a server-generated
`remote_path` (tgstate id), and there is no `share_storage.download_artifact`.

**Evidence** `storage_backends` contains no `share` row; `SAU_SHARE_ENABLED=0` today, so latent.
`tests/test_media_remote_storage.py` has no provider-`share` download test.

**Proposed fix** Add a `share` download path (or explicitly reject `share` rows with a clear error)
and persist a `share` backend row if share is ever enabled.

**Tests** `download_from_backend` with `provider="share"` either downloads via the share host or raises
an explicit, non-S3 error.

---

### BUG 4 (low) — 5 `file_records` rows have `storage_key` but no backend, so Try 1 silently skips them

**Evidence**

```
id=82  file_path=uploads/382225c6-…_vlcsnap….png  storage_key=<same>  storage_backend_id=NULL  local_cleaned_at=2026-07-02
id=84  …  id=88  (all under uploads/, cleaned 2026-07-04)
```

`worker.py:1946` requires both `storage_key` **and** `storage_backend_id`. These 5 rows can never be
restored; `_record_for` returns them but Try 1 is skipped. Not referenced by any pending target today.

**Proposed fix** Backfill the backend id, or treat a key-only row as unrestorable and fall through to
the public-URL/CDN attempts.

---

## 3. Upload idempotency (file already present remotely)

`upload_artifact` (`media_remote_storage.py:214-268`) never checks whether the object already exists;
it calls the backend unconditionally. Per backend:

| backend | key | behaviour on repeat |
|---|---|---|
| `do_spaces` | deterministic `campaigns/<id>/<subdir>/<name>` (`media_remote_storage.py:191`) | `SpacesClient.upload_file` PUTs the same key → **overwrites**, no duplicate object (`do_spaces.py:95-103`) |
| `rclone` | deterministic `<root>/campaigns/<id>/<subdir>/<name>` (`rclone_storage.py:60-73`) | `rclone copyto` to the same path → **overwrites**; note `ensure_public_link` runs `rclone link` again and may return/rotate the share link |
| `share` | server-assigned id (`share_storage.py:157-171`) | POSTs again, gets a **new id → duplicate object** every call |

So the offload targets (rclone) and the production upload target (do_spaces/R2) are storage-idempotent
(no duplicate bytes), but re-running prep still re-uploads the bytes (bandwidth) and, for `share`, grows
the remote. There is no `exists()`/`head_object` short-circuit anywhere.

---

## 4. Race: offload deletes local while publish reads it

**Where**
* `offload_to_drive.sh:414-416` excludes in-flight (`running`/`retrying`) and pending targets due within
  `DUE_SOON_MINUTES = 30`, plus unrecorded files and `videoFile/_library/**` (`:525`).
* `offload_to_drive.sh:267` lists candidates with `rclone lsf … --min-age 10m --exclude-from "$EXCLUDES"`,
  then `:305-310` register-then-`rm`.
* `worker.py:1920` `if p.exists(): continue` (and `worker.py:1719-1721` in `_resolve_file_path`) — the
  worker trusts the file exists at check time and the uploader opens it later.

**The race** The exclude list is a snapshot taken at `pending_excludes` time. A target that becomes due
between the snapshot and the unlink is not protected. `--min-age 10m` only bounds how new a file must be.
The vulnerable window is: worker checks `p.exists()` → returns true → (offload deletes) → uploader opens
the path → `FileNotFoundError`. This is narrow (cron is every 30 min and excludes the 30-min due window)
but real, and it is exactly the kind of failure that is misread as a normal missing media error.

**Proposed mitigation** In `_ensure_artifact_paths_local`, after the exists-check, keep the file open
(or re-check at uploader open time and restore on `FileNotFoundError` from the publisher). Offload could
also re-evaluate in-flight targets immediately before each `rm`. A test could hold a file, delete it
between the existence check and the download attempt, and assert the worker restores rather than raises.

---

## 5. Permanent-loss quantification (`db/database.db`)

All counts are against the live DB. `local_path` is `campaign_artifacts.local_path` with `/app/` mapped
to the repo (and host-prefixed rows mapped via `_resolve_media_path` semantics).

| metric | count |
|---|---|
| `campaign_artifacts` rows total | 2509 |
| local file missing | 961 (958 in a second pass after the 09:17 offload) |
| missing **AND** (`public_url IS NULL OR remote_path IS NULL`) — the literal question | **753** |
| missing **AND** `public_url IS NULL` **AND** `remote_path IS NULL` | 88 |
| … of those 88, restorable via `source_file_record_id → file_records.storage_key` | 82 |
| … of those 88, **truly unrestorable** (no source of any kind) | **6** (5 `screenshot`, 1 `local`) |
| historical `campaign_artifacts` with no source at all (incl. http-only URLs) | 34 — **0 referenced by pending/running/retrying** |

The literal `OR` figure (753) is misleading because `remote_path` is NULL on 2294 rows by design (see
BUG 2). The meaningful number is **6 truly unrecoverable rows, none referenced by the live queue**, plus
the 2 distinct `_inbox_cache` artifacts in BUG 1 (5 pending targets) that are *recoverable from R2 but the
code refuses to try*, and 3 demo/`nonexistent.mp4` test-fixture artifacts (19 more targets).

Note: 28 of the 34 no-source historical rows carry only an `http://socialupload.iamwillywang.com/getFile…`
URL. `_download_public_artifact`/`_public_https_url` (`worker.py:2762-2779`) require `https://`, so those
URLs are never usable by the restore path even though the self-hosted share may still serve them.

---

## 6. UNKNOWNS needing operator input

1. **Who deleted the `_inbox_cache` media?** `file_records` ids 567/572 have
   `local_cleaned_at=NULL` and `storage_key=NULL`, yet the files are gone and the offload log has zero
   `_inbox_cache` mentions. Either an older/other cleanup removed them, or the offload moved them without
   registering. The fix in BUG 1 makes this moot for these rows, but the underlying deletion is unexplained.
2. **Is the `storage_backends.default` do_spaces row ever used?** It points at
   `sau-media`/`sgp1.digitaloceanspaces.com` while the live uploads go to R2 (`nw-assetsoffload`,
   `…r2.dev`). Should it be updated or removed? Every R2 object is currently reachable only by its
   `campaign_artifacts.public_url`.
3. **The offload at 09:17 UTC is still running and is being rate-limited by Google Drive**
   (`logs/offload.log` shows repeated `rateLimitExceeded`). This audit did not wait for it to finish;
   operators should confirm it eventually exits 0 and that no files were purged after a failed
   `rclone check`. (The verified-delete ordering suggests not, but it should be confirmed.)
4. **`generated/` cache count.** The task said 231/304 empty; at audit time it was 163 empty dirs / 3
   remaining files, then 63 files after the 09:17 copy phase. The directory is actively changing.
5. **Are `campaign_post:<id>`-only targets always campaign payloads?** Every non-campaign direct target
   with a missing file currently returns `MediaRestoreError` from `_try_download_from_storage`
   (`worker.py:1730-1745`, storage_key-only, no URL fallback). There were no such pending targets during
   the audit, so this path is untested live.

---

## 7. Existing test coverage and gaps

Command run (as requested), via the project venv (system python has no pytest):

```
.venv/bin/python -m pytest tests/ -q -k "offload or remote_storage or media_remote"
```

Result: **38 passed, 1399 deselected** in 3.10s.

Relevant test files:
* `tests/test_media_remote_storage.py` — share upload, dispatcher order/fallback, `is_direct_file_url`,
  `verify_media_url` (403/404 fail, 405/network fail open), rclone vs S3 `download_from_backend`, URL encoding.
* `tests/test_worker_media_restore.py` — generated artifact restores from `public_url`;
  generated artifact refuses to fall back to source bytes; legacy generated mapping; host-prefix
  normalisation; CDN empty-response rejection; partial-download cleanup.
* `tests/test_offload_script.py` — register-before-delete, size mismatch keeps the file, one bad file
  does not block siblings, lock behaviour, notify/recovery, generated-backend preflight.

**Gaps (the tests that would have caught BUG 1):**
1. No test for a **non-generated** artifact restoring from its own `public_url`. All public-URL tests use
   a path under `generated/`.
2. No test for `download_from_backend` with `provider="share"`.
3. No test that `add_campaign_artifact` persists `storage_backend_id` (it does not).
4. No test for a `file_records` row with `storage_key` but `storage_backend_id IS NULL` (BUG 4).
5. No test for the offload↔publish TOCTOU (file removed between `exists()` and open).
6. No test that a key-only/`remote_path` campaign_artifact row is usable (BUG 2).

---

## Appendix — commands executed (key ones)

```bash
# Schema
sqlite3 db/database.db ".schema campaign_artifacts; .schema file_records; .schema storage_backends; .schema media_assets"
sqlite3 db/database.db "SELECT id,provider,bucket,endpoint,enabled FROM storage_backends;"

# Literal permanent-loss question (Python; maps /app → repo)
#   total 2509, missing 961, (missing AND (public_url NULL OR remote_path NULL)) = 753,
#   both NULL = 88, of which 82 restorable via source_file_record, 6 truly unrestorable.

# Real restore through the worker's own rclone spec (writes only to /tmp)
docker exec social-auto-upload rclone --config /app/rclone-cache.conf copyto \
  "GDrive-willywang8216:sau/generated/campaigns/campaign-2470/2d45dbf5…__nsfw_pub.mp4" /tmp/x

# Live R2 URL check
curl -r 0-1023 https://pub-9915b1494003455c9ab872fd7094e64e.r2.dev/campaigns/2398/images/104a…SFW.jpg
#   → HTTP 206, image/jpeg

# Worker restore simulation against a frozen DB snapshot (downloads stubbed)
#   386 payloads with missing artifacts → 362 OK, 24 FAIL
#   failures: _inbox_cache jpg (x4 targets), _inbox_cache mp4 (x1), nonexistent.mp4 (x7), _inbox_cache/demo.mp4 (x12)

# Existing coverage
.venv/bin/python -m pytest tests/ -q -k "offload or remote_storage or media_remote"
#   → 38 passed, 1399 deselected
```
