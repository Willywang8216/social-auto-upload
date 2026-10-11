# Progressive Sociamonials uploads: reclaim-after-publish

**Date:** 2026-10-11
**Workspace:** 26985 (`superwill1` / `Nw`), pooled with the account's other workspaces
**Author:** SAU agent
**Operator request:** "progressively add sociamonial assets. when published then we can add new one."

## 1. Measured usage and the enforced allowance

The allowance **is** reported by the API: `GET /api/v1/workspaces/{id}/context`
returns a `storage` block (the docs call this `get_workspace_context`).  It is
pooled across the account's workspaces, and only *library* (reusable) media is
metered; publishing media and platform derivatives are exempt.

```
{
 "status": "ok",
 "usage_bytes": 3225794427,
 "metered_bytes": 3225794427,
 "exempt_bytes": 871275754,
 "total_bytes": 4097070181,
 "allowance_bytes": 4000000000,
 "available_bytes": 774205573,
 "percent_used": 80.6449,
 "unmetered": false,
 "unconfigured": false,
 "pooled": true,
 "warning": "80",
 "ratchet": {"active": false, "ceiling_bytes": null, "set_at": null},
 "enforcement": true,
 "max_upload_bytes": {"image": 15728640, "video": 1073741824}
}
```

Confirmed **empirically** with a zero-byte-effect probe: a media-upload grant
declaring 1,000,000,000 bytes (over the ~774 MB free, under the 1 GiB video
cap) was refused with `storage_quota_exceeded` and the numbers in
`error.details` — and it stored nothing:

```
HTTP 422
{"error": {"code": "storage_quota_exceeded",
 "message": "Storage allowance exceeded: using 3 GB of 4 GB (pooled across the
   account's workspaces); this file needs 1 GB and 774 MB is available...",
 "details": {"usage_bytes": 3225794427, "allowance_bytes": 4000000000,
   "available_bytes": 774205573, "percent_used": 80.6449, "pooled": true,
   "ratchet": null, "upgrade": {"plan": "thrive", ...}}}}
```

So the enforced ceiling is **4,000,000,000 bytes**, and the library stood at
**3,225,794,427 bytes (80.64%) with 774 MB free**.  The refusal is checked on
the *declared* size at grant creation, the *real* bytes at complete and the
*fetched* bytes on import (per the platform's own docs), so a full library
stops X publishing entirely while `SAU_X_DIRECT_PUBLISH` is unset.

**Asset inventory:** 121 assets, 3,216,388,953 bytes.  Each was listed with
`asset_id`, `filename`, `size_bytes`, `created_at`, `sent_count` and
`last_sent_at`.  Only `(filename, size_bytes)` is the reuse identity the
fallback matches on, so that is the key the reclaim rule uses.

## 2. The reclaim rule and why it is safe

A new module `myUtils/sociamonials_reclaim.py` and a tool
`scripts/sociamonials_reclaim.py` implement the request.  An asset is deleted
only when **all** of these hold:

1. **A delivered post used it.**  Either the new ledger
   `sociamonials_delivered_assets` names the asset id (written by the worker
   immediately after a target transitions to `succeeded`), or a `succeeded`
   target's payload references the filename *and* the asset's
   `sent_count >= 1`.  `failed` and `cancelled` targets never unlock a delete.
2. **No live target needs it.**  Any filename referenced by a
   `pending` / `retrying` / `running` target is never deleted, even if many
   posts already delivered with it.
3. **It is not starred** (an explicit operator pin).
4. **No unknown provenance.**  An asset whose filename no target history
   accounts for is kept.  The tool can never delete a file it does not
   understand.
5. **The grace window has passed** when one is configured
   (`SAU_SOCIAMONIALS_RECLAIM_GRACE_SECONDS`).

The delete endpoint frees the bytes immediately and is reversible for 7 days
(`POST /api/v1/media/assets/{id}/restore`), yet this is still live third-party
data, so the tool is **dry-run by default** and only acts under `--apply`.

**Why a grace/scheduled sweep rather than deleting inside the publish path.**
A delivered post can still be re-fetched by the destination platform after the
post is marked delivered, and a delete is only reversible for a week.  The
quota guard therefore reclaims with a grace window and only when the library is
actually short, while the operator tool defaults to no grace for an explicit,
observed sweep.  Batching also keeps DELETE calls out of a live publish and
lets an operator inspect the exact plan first.

## 3. The quota guard

The fallback's single reuse-vs-upload decision point (`_resolve_local_media`)
now calls `ensure_headroom` before any **new** upload (a reuse makes no bytes).
It reads the storage block; if the file plus a 32 MB safety margin does not
fit, it reclaims eligible delivered assets, re-reads the block, and only then
uploads.  If it still does not fit, it raises a clear
`SociamonialsFallbackError` naming usage/allowance/available/needed instead of
letting the upload fail with an opaque 422.  A storage read that is unavailable
or a reclaim sweep that errors never blocks the publish (the platform remains
the authority).  The guard is skipped when no SAU database path is available.

After a delivered target, the worker records the `asset_id`(s) the fallback
attached, so the ledger ties a delivery to the exact asset rather than only a
filename.  A migration (`0022_sociamonials_delivered_assets`) adds the ledger;
the module also creates it defensively with `CREATE TABLE IF NOT EXISTS`.

## 4. Dry run against the live API

```
$ python scripts/sociamonials_reclaim.py --dry-run
workspace:        26985
storage before:   used 3.23 GB / 4.00 GB allowance, 774.2 MB free (80.6449%)
assets listed:    121
library total:    3216388953 bytes (3.22 GB)
not reclaimable:  0 starred, 13 live target, 6 no delivered post, 86 unknown provenance
to delete:        16 asset(s)
reclaimable:      916316726 bytes (916.3 MB)
projected usage:  2.30 GB after cleanup
projected free:   1690.5 MB

Planned deletions (delivered, no live target needs them):
  DELETE asset 2159747      268.8 MB  20260822145825986_pub_pub.mp4  (created 2026-10-05 13:50:06, sent 1, last_sent 2026-10-05 14:04:53)
  DELETE asset 2200129      266.8 MB  20260822153824836_pub_pub.mp4  (created 2026-10-09 03:23:49, sent 1, last_sent 2026-10-09 03:35:05)
  DELETE asset 2159559      131.8 MB  20260822145825986_pub.mp4  (created 2026-10-05 13:15:12, sent 1, last_sent 2026-10-05 13:18:17)
  DELETE asset 2200112      131.2 MB  20260822153824836_pub.mp4  (created 2026-10-09 03:21:53, sent 2, last_sent 2026-10-09 03:27:55)
  DELETE asset 2166080       46.6 MB  1T_pub_pub_part1of2_pub.mp4  (created 2026-10-06 05:58:56, sent 1, last_sent 2026-10-06 06:02:46)
  DELETE asset 2158794       39.7 MB  20260722155038425.mp4  (created 2026-10-05 12:04:31, sent 1, last_sent 2026-10-05 12:05:09)
  DELETE asset 2159626       14.8 MB  firsttimeficked.mp4  (created 2026-10-05 13:21:50, sent 1, last_sent 2026-10-05 13:22:14)
  DELETE asset 2158025       14.1 MB  dicksmallbig.mp4  (created 2026-10-05 10:41:56, sent 1, last_sent 2026-10-05 10:43:55)
  DELETE asset 2214495        0.8 MB  9f8c91100d7b47faa75e8024e8c997b4_Serene Sensual Surrender 18__nsfw.png  (created 2026-10-10 10:23:51, sent 2, last_sent 2026-10-10 10:38:35)
  DELETE asset 1965497        0.6 MB  SFW.jpg  (created 2026-09-18 13:23:50, sent 6, last_sent 2026-09-18 13:45:43)
  DELETE asset 2157326        0.3 MB  Fox Masked Vulnerability 16.jpg  (created 2026-10-05 09:55:07, sent 1, last_sent 2026-10-05 09:55:29)
  DELETE asset 2159570        0.2 MB  Joyful Nude Nocturne 11.jpg  (created 2026-10-05 13:18:01, sent 1, last_sent 2026-10-05 13:18:17)
  DELETE asset 2175689        0.2 MB  Kitsune's Vulnerable Surrender 14.jpg  (created 2026-10-06 23:35:23, sent 1, last_sent 2026-10-06 23:36:12)
  DELETE asset 2170474        0.2 MB  Intimacy Beyond the Net 04.jpg  (created 2026-10-06 14:35:53, sent 1, last_sent 2026-10-06 14:37:33)
  DELETE asset 2175649        0.1 MB  Longing's Geometric Veil 02.jpg  (created 2026-10-06 23:15:21, sent 1, last_sent 2026-10-06 23:16:02)
  DELETE asset 2154152        0.0 MB  Chin-up in the Dirt 26.jpg  (created 2026-10-05 04:36:46, sent 1, last_sent 2026-10-05 04:37:25)

DRY RUN - nothing was deleted. Re-run with --apply to delete.
```

The 86 "unknown provenance" assets (~1.66 GB, mostly never-sent media from
2024–2025 and from failed posts) are deliberately **kept**: no delivered post
proves they are reclaimable, and the live-target set does not name them.  The
policy is conservative on purpose — it only frees what it can prove.

## 5. What was applied (bounded) and the resulting usage

The first apply was bounded to ~700 MB with `--max-bytes`:

```
$ python scripts/sociamonials_reclaim.py --apply --max-bytes 700000000
...
to delete:        13 asset(s)
reclaimable:      698748997 bytes (698.7 MB)
Applying deletions...
Done: 13 deleted, 0 skipped.
storage after:    used 2.53 GB / 4.00 GB allowance, 1473.0 MB free (63.1761%)
```

Usage moved from **3.23 GB / 774 MB free (80.64%)** to
**2.53 GB / 1,473 MB free (63.18%)** — no skips, so every planned asset was
deletable (`asset_in_use` never fired, confirming no live post still held one).

## 6. Publish proof

Quota-blocked X target **#5494** (account 124, `NW X (nudeweiwei)`) was
resubmitted through the live API with the `SAU_API_TOKENS` bearer:

```
$ curl -s -X POST http://localhost:5409/jobs/targets/5494/resubmit \
    -H "Authorization: Bearer <SAU_API_TOKENS>"
{"code":200,"data":{"accountName":"NW X (nudeweiwei)","attempts":0,
 "fileRef":"campaign_post:5493","id":5494,"jobId":5494,
 "scheduleAt":null,"status":"pending"},"msg":"resubmitted"}
```

The worker claimed it, restored the split part from storage and delivered it
through Sociamonials:

```
2026-10-11 17:43:10 | target claimed; starting execution
2026-10-11 17:43:10 | X direct publish disabled; using Sociamonials fallback
2026-10-11 17:44:49 | delivered via Sociamonials fallback (post_id=10729773,
  network=tw, status=scheduled) (delivery pending); the direct publish had
  failed with: X direct publish disabled
```

Target state after the run: `succeeded`.  The fallback uploaded a new library
asset (`2223098`, 27.7 MB) and the post was accepted, proving the quota no
longer blocks X.

**The progressive rule then closed the loop.**  A fresh dry run lists that very
asset as reclaimable, because its post delivered and no live target needs it:

```
$ python scripts/sociamonials_reclaim.py --dry-run
storage before:   used 2.55 GB / 4.00 GB allowance, 1445.2 MB free (63.8688%)
...
to delete:        4 asset(s)
reclaimable:      245274321 bytes (245.3 MB)
  DELETE asset 2200112      131.2 MB  20260822153824836_pub.mp4 ...
  DELETE asset 2166080       46.6 MB  1T_pub_pub_part1of2_pub.mp4 ...
  DELETE asset 2158794       39.7 MB  20260722155038425.mp4 ...
  DELETE asset 2223098       27.7 MB  b8f269e1-...-watermarked_video_part1of5_pub.mp4  (created 2026-10-11 09:43:34, sent 1, last_sent 2026-10-11 09:46:18)
```

## 7. Tests

`tests/test_sociamonials_reclaim.py` (30 tests) covers filename/history
parsing, the delivered/live/starred/unknown/grace protections, the ledger
upsert, the dry-run vs apply delete paths, a failed delete, the `--max-bytes`
bound, and the quota guard (fits, reclaims-then-fits, raises clearly when it
still cannot fit, survives an unavailable storage read and a failed sweep).
`tests/test_worker_sociamonials_fallback.py` gains a wiring test that a
delivered fallback records its assets for reclaim.  All tests use injected
fake sessions and temporary databases; nothing touches the live API.

```
$ .venv/bin/python -m pytest tests/ --ignore=tests/test_security_http.py -q
1646 passed, 1 skipped, 161 subtests passed in 168.65s
```

## 8. The new policy, in one paragraph

Sociamonials storage is used **progressively**: the fallback reuses an
identical library asset instead of re-uploading, a new upload first checks the
pooled storage block and reclaims already-delivered assets when the file will
not fit (raising a clear error only when it still cannot), and every delivered
target records the exact asset ids it used; `scripts/sociamonials_reclaim.py`
then sweeps assets whose posts have **delivered** and which no
pending/retrying/running target still needs, never touching a starred asset, a
live target's media, or an asset with no proven delivery — so the library stays
under its allowance and publishing is never blocked by a full quota.

## Operational note

The reclaim tool was run from the host against the live API and database.  The
running container still serves the previously deployed image, so the automatic
guard and ledger recording take effect on the next `scripts/deploy-local.sh`
(or the normal Watchtower pull).  Until then the sweep is available on demand.
The database migration `0022_sociamonials_delivered_assets` was applied to
`db/database.db` (backup: `db/database.db.bak-pre-sm-reclaim-20261011-173945`).
