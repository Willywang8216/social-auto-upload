# Sociamonials fallback: `storage_quota_exceeded` root cause + de-dupe

## Symptom

Every X/Twitter fallback publish for the affected targets failed with:

```
fallback error: media upload grant failed (HTTP 422): storage_quota_exceeded
```

(targets 5360, 5489, and others.)

Live workspace **26985** storage block (verified against the API):

| field | value |
| --- | --- |
| status | `ok` (misleading - no warning flag is set even at 99.57%) |
| usage_bytes | 3,982,902,697 (3.98 GB) |
| allowance_bytes | 4,000,000,000 (4.00 GB) |
| available_bytes | 17,097,303 (17 MB) |
| percent_used | 99.57% |
| warning | 90 |
| enforcement | true |

A video upload grant needs tens to hundreds of MB, so a 17 MB headroom is
refused at `create` with `storage_quota_exceeded` (the docs check the *declared*
size at create, the real bytes at complete, and the fetched bytes at import).

## API surface verified (before acting)

Read from `https://www.sociamonials.com/developers.php` and cross-checked
against the live workspace:

- **`GET /api/v1/media/assets`** - lists the workspace library, up to
  **200 rows/page** (`limit`/`offset`, `total`). Rows carry `asset_id`,
  `filename`, `media_type`, `size_bytes`, `processing_status`, `created_at`,
  `starred`, `sent_count`, `url`. Confirmed live: `total: 122`.
- **`DELETE /api/v1/media/assets/{id}`** - deletes one asset. Requires
  `assets.delete`. **Reversible for 7 days** (`POST .../{id}/restore`); bytes
  stop counting against the allowance immediately. Refused with `asset_in_use`
  while an unpublished post still references it, and `asset_not_deletable` for
  platform-managed media (watermarks, video thumbnails). This is the documented
  way to free space after `storage_quota_exceeded`.
- **`asset://<id>` reuse is real** - `image_urls` / `video_url` accept
  `asset://<asset_id>` for a video/image already in the workspace library. The
  server resolves it to the hosted URL before validation. Only a **ready** asset
  **in the same workspace** can be attached; otherwise the 422 names
  `asset_not_ready` / `asset_wrong_workspace`.

Both the delete endpoint and `asset://` reuse therefore exist and behave as the
task assumed.

## De-duplication dry run (`scripts/sociamonials_dedupe.py --dry-run`)

Nothing was deleted. Live output:

```
workspace:        26985
assets listed:    122
library total:    3973497223 bytes (3.97 GB)
duplicate groups: 10
to delete:        18 asset(s)
reclaimable:      1949506522 bytes (1949.5 MB)
projected usage:  2.02 GB after cleanup

Planned deletions (one copy of each filename is kept):
  DELETE asset 2161416      268.8 MB  20260822145825986_pub_pub.mp4  (keep 2159747, created 2026-10-05 13:50:06)
  DELETE asset 2161858      207.4 MB  1T_pub.mp4  (keep 2159066, created 2026-10-05 12:13:56)
  DELETE asset 2162227      207.4 MB  1T_pub.mp4  (keep 2159066, created 2026-10-05 12:13:56)
  DELETE asset 2169826      207.4 MB  1T_pub.mp4  (keep 2159066, created 2026-10-05 12:13:56)
  DELETE asset 2154359      171.5 MB  20260822085411068_pub.mp4  (keep 2154175, created 2026-10-05 04:39:29)
  DELETE asset 2154566      171.5 MB  20260822085411068_pub.mp4  (keep 2154175, created 2026-10-05 04:39:29)
  DELETE asset 2161462      131.8 MB  20260822145825986_pub.mp4  (keep 2159559, created 2026-10-05 13:15:12)
  DELETE asset 2157797       83.0 MB  20260820101216997_pub.mp4  (keep 2157438, created 2026-10-05 10:06:11)
  DELETE asset 2157884       83.0 MB  20260820101216997_pub.mp4  (keep 2157438, created 2026-10-05 10:06:11)
  DELETE asset 2157974       83.0 MB  20260820101216997_pub.mp4  (keep 2157438, created 2026-10-05 10:06:11)
  DELETE asset 2158156       83.0 MB  20260820101216997_pub.mp4  (keep 2157438, created 2026-10-05 10:06:11)
  DELETE asset 2158177       83.0 MB  20260820101216997_pub.mp4  (keep 2157438, created 2026-10-05 10:06:11)
  DELETE asset 2164318       81.5 MB  20260725224435111_pub.mp4  (keep 2161928, created 2026-10-05 17:55:03)
  DELETE asset 2161890       39.7 MB  20260722155038425.mp4  (keep 2158794, created 2026-10-05 12:04:31)
  DELETE asset 2157390       23.5 MB  dicktalk2.mp4  (keep 2154153, created 2026-10-05 04:37:01)
  DELETE asset 2183176       23.5 MB  dicktalk2.mp4  (keep 2154153, created 2026-10-05 04:37:01)
  DELETE asset 2159800        0.1 MB  Contemplative Nude Reclining 85.jpg  (keep 2159770, created 2026-10-05 13:51:01)
  DELETE asset 2183058        0.1 MB  Contemplative Solitude 91.jpg  (keep 2182833, created 2026-10-07 13:15:10)

DRY RUN - nothing was deleted. Re-run with --apply to delete.
```

**Headline numbers:** 122 assets listed, 10 duplicate groups, **18 assets
deletable**, **1,949,506,522 bytes (1949.5 MB) reclaimable**. Usage would drop
from 3.97 GB to ~2.02 GB, leaving ~1.98 GB of headroom under the 4 GB
allowance.

A handful of the duplicates carry `sent_count > 0`, but the docs are explicit
that a *published* post is not altered by a delete and records of the asset are
kept; only a still-unpublished reference blocks a delete (`asset_in_use`). The
script reports such skips instead of failing the batch.

`--apply` deletes the non-keeper of each `(filename, size_bytes)` group,
preferring to keep a **starred** copy and otherwise the **oldest** upload. It
can never delete the last copy of a filename.

## Root-cause fix (`myUtils/sociamonials_fallback.py`)

The duplicates were made by the fallback itself: every publish uploaded the
target's media, and the only idempotency key was `sau-target-<id>` on the
*post*, not on the *file*. A retried target (or the same video reused across
targets) uploaded another physical copy each time.

The docs list `idempotency_key` only on create/import, **not** on the upload
grant (`POST /api/v1/media/uploads`); the existing code deliberately sends no
key there because replaying a completed multipart grant yields a closed session
whose part PUTs fail with `NoSuchUpload`. So the fix is the reuse path, not a
file-level upload key:

- **New `select_reusable_asset(...)`** picks an existing ready asset whose
  `filename` **and** `size_bytes` match exactly and whose `media_type` agrees.
  Preference: starred, then oldest, then lowest asset id - stable across
  retries. This is the same `(filename, size_bytes)` identity the de-dupe
  script uses, so a genuinely different file that merely shares a name is never
  reused.
- **New `_lookup_reusable_asset(...)`** calls
  `GET /api/v1/media/assets?search=<filename>` and passes the rows through the
  selector. It **never raises**: a plan without library browsing answers
  `asset_access_not_enabled`, and a lookup is only an optimisation, so any
  failure falls through to the normal upload.
- **New `_resolve_local_media(...)`** is the single reuse-vs-upload decision
  point. On reuse it returns `asset://<id>` and logs
  `sociamonials media reused asset://<id> for <name>`; otherwise it uploads and
  logs `sociamonials media uploaded <name> as asset://<id>`. The publish loop
  now calls this instead of `_upload_local_media` directly.
- **Kill switch:** `SAU_SOCIAMONIALS_REUSE_ASSETS=0` restores
  upload-every-time behaviour if ever needed.

Net effect once the backlog is cleaned: a repeated file is attached by
reference, so the library stops refilling.

## Tests

New/updated, all against injected fake sessions - no live calls:

- `tests/test_sociamonials_fallback.py`
  - reuse of an identical image and video asset (no `/media/uploads` call, post
    body carries `asset://<id>`);
  - same filename but different size still uploads;
  - a 403 `asset_access_not_enabled` listing failure still publishes;
  - `SAU_SOCIAMONIALS_REUSE_ASSETS=0` disables reuse;
  - `select_reusable_asset` exact name/size/kind, starred/oldest preference,
    skips non-ready assets, honours `exclude_ids`.
- `tests/test_sociamonials_dedupe.py`
  - group by `(filename, size_bytes)`, ignore unusable rows;
  - keeper prefers starred then oldest;
  - plan keeps exactly one copy and never the last copy;
  - library listing paginates 200/page;
  - dry run plans but deletes nothing;
  - `--apply` deletes and reports `asset_in_use` skips.

Full suite: **1395 passed, 1 skipped** (`pytest tests/ -q`).

## Operator approval required

The deletion is **live third-party data and has NOT been performed**. Only
`--dry-run` was run. To reclaim the ~1.95 GB:

```bash
python scripts/sociamonials_dedupe.py --apply
```

Deletes are reversible for 7 days via `POST /api/v1/media/assets/{id}/restore`.
Nothing was committed, pushed, or restarted.
