# Drive archive: structured layout, offload drain and restore verification

Date: 2026-10-10 (UTC). Repo: `/home/will/social-auto-upload`.
Scope: make the Google Drive archive genuinely organised and manageable, and
verify the offload/restore pipeline end to end. The pipeline fixes from
`bc05676` were **not** redone.

Artifacts added:

| File | Purpose |
| --- | --- |
| `scripts/archive_drive.py` | Migrate the flat `sau/archive` tree into `sau/archive/<YYYY>/<MM>/<bundle>/<key>`. Dry-run by default; `rclone moveto` only on `--apply`; byte-verified; never deletes. |
| `tests/test_archive_drive.py` | 19 tests for the archive planner/apply/CLI. |
| `myUtils/drive_layout.py:153-180` | `archive_prefix()` + `structured_archive_path()` so the layout rule lives in the single source of truth, not only in the script. |
| `reports/drive-archive-manage.md` | This report. |

---

## 1. Measured state

### 1.1 Google Drive (`GDrive-willywang8216:sau`)

Read with `rclone lsjson -R --files-only` (Drive quota is flaky — see §5 — so
`inbox/generated` bytes are from the DB; its Drive file count of 38 was
confirmed separately).

| Tier / path | Files | Bytes |
| --- | ---: | ---: |
| `sau/inbox/videoFile` | 39 | 4,493,757,408 |
| `sau/inbox/generated` | 38 | 1,016,996,397 (DB sum) |
| `sau/inbox/uploads` | 0 | – (dir does not exist) |
| `sau/published/videoFile` | 4 | 146,598,088 |
| `sau/published/generated` | 11 | 466,369,839 |
| `sau/published/uploads` | 0 | – (dir does not exist) |
| `sau/archive` | 35 | 1,128,305,860 |
| `sau/assets` | 19 | 29,168,613 |
| **legacy `sau/videoFile`** | **650** | **25,165,383,177** |
| **legacy `sau/uploads`** | **4** | **1,027,927,446** |
| **legacy `sau/generated`** | **485** | **29,352,113,883** |

The tiered trees are healthy. The three **legacy, un-tiered** roots still hold
1,139 objects / ~55.5 GB (`sau/videoFile`, `sau/uploads`, `sau/generated`).
`scripts/migrate_drive_layout.py` is the existing tool for promoting those; it
is DB-driven, and the DB only names 212 + 1 + 472 = 685 of the 1,139 objects,
so a large orphan population would need an rclone-side listing too. This is
outside the archive task; see §7 (operator decisions).

### 1.2 Local disk and why each file is still there

`df` before any pipeline run: `97G total, 80G used, 17G free (83%)`.
Local media:

| Root | Files | Bytes |
| --- | ---: | ---: |
| `videoFile` | 384 | 9,077,473,626 |
| `generated` | 3 | 208,073,097 |
| `uploads` | 0 | 0 |
| **total** | **387** | **9,285,546,723** |

I replicated the offload script's keep-local logic exactly (`offload_to_drive.sh`
`pending_excludes`, `build_route_plan`) and attributed every file to one reason.
**In-flight = 0, due-soon = 0, truly-unrecorded = 0.**

| Reason | Files | Bytes | Share |
| --- | ---: | ---: | ---: |
| `videoFile/_library/**` — reddit tool enumerates it off disk | 299 | 147,441,015 | 1.6% |
| Recorded only as `/app/videoFile/...` → see §1.3 (mis-read as "unrecorded") | 85 | 8,930,032,611 | 96.2% |
| Already registered to a tiered backend → route plan skips it (§1.4) | 3 | 208,073,097 | 2.2% |
| **total** | **387** | **9,285,546,723** | 100% |

### 1.3 Finding: the `/app/` path shape defeats the "unrecorded" guard

`file_records` stores the 85 local `videoFile/_inbox_cache/*` files with a
container-absolute path:

```
/app/videoFile/_inbox_cache/23a34f79415f499f8a6450478b2d3b2a
```

`pending_excludes` builds its `recorded` set without stripping that prefix
(`offload_to_drive.sh:684-698`):

```python
if fp.startswith("uploads/"):   recorded.add(("uploads", ...))
elif fp.startswith("generated/"): recorded.add(("generated", ...))
elif fp.startswith("videoFile/"): recorded.add(("videoFile", ...))
else: recorded.add(("videoFile", fp))           # "/app/videoFile/..." lands here
```

so `/app/videoFile/_inbox_cache/x` is stored as
`("videoFile", "/app/videoFile/_inbox_cache/x")`, which never matches the walk's
relative key `("videoFile", "_inbox_cache/x")`. Every one of the 85 files is
therefore emitted as an exclusion and protected forever — the same failure mode
as the already-fixed "no `file_records` row" bug, reached through a different
path shape. The restore side already handles this shape
(`offload_to_drive.sh:245` tries `relative`, `videoFile/relative` and
`/app/videoFile/relative`), so the only blocker is the exclude builder.

### 1.4 Finding: already-tiered local copies are never purged

The 3 local `generated` files are registered with `storage_backend_id = 694`
(`sau/published/generated`) and a `storage_key`. `build_route_plan` skips any
row whose endpoint is already tiered (`offload_to_drive.sh:394`):

```python
# Only legacy, untiered rows are candidates; already-tiered rows and
# non-media endpoints are left alone.
if root is None or tier is not None:
    continue
```

so the local bytes are never routed, copied or purged. They are 198.4 MiB of
published campaign artifacts sitting on the VPS with a live Drive copy.

---

## 2. Archive design

### 2.1 Current shape (flat, unmanageable)

```
sau/archive/my-compressed-2026-10/index.jsonl
sau/archive/my-compressed-2026-10/copy-pack.json
sau/archive/my-compressed-2026-10/originals/<profile>/<file>.mp4
```

35 objects / 1,128,305,860 bytes, one bundle (`my-compressed-2026-10`,
compressed 2026-10-07/08). A retention sweep or "what did we archive in
October?" query has to know every bundle name; no date prefix exists to range
over.

### 2.2 Target shape (date-structured)

```
sau/archive/<YYYY>/<MM>/<bundle>/<original key...>
```

For the live archive this yields:

```
sau/archive/2026/10/my-compressed-2026-10/index.jsonl
sau/archive/2026/10/my-compressed-2026-10/copy-pack.json
sau/archive/2026/10/my-compressed-2026-10/originals/<profile>/<file>.mp4
```

### 2.3 Rationale

* **Date-addressable.** `<YYYY>/<MM>/` is a literal prefix, so a month's
  archive is one `rclone` call and a retention policy is a prefix rule. This is
  the property the flat tree lacked.
* **Bundles stay whole.** The bundle name is preserved as the segment after the
  date, so `index.jsonl`, `copy-pack.json` and all `originals/<profile>/*` move
  together and remain browsable by profile. The planner refuses to apply if one
  bundle would be split across two months (`plan_moves`, bundle-conflict guard).
* **Only a prefix is added.** Every byte after the date prefix is the original
  key, so the migration never has to rewrite an object's identity and a
  size/restore check is trivial. This mirrors the endpoint/key invariant the
  tier migration relies on.
* **Date provenance is deterministic.** Bundle-name date first
  (`my-compressed-2026-10` → `2026/10`), then `--date YYYY-MM`, then the
  object's Drive `ModTime`; an unresolvable object is reported, not guessed.
* **Idempotent/resumable.** Anything already under `<YYYY>/<MM>/` is skipped, so
  re-running after an interrupt is safe.
* **Never destructive.** `rclone moveto` is a server-side rename: no re-upload,
  no delete. The script issues no `delete`/`deletefile`/`purge` at all. The plan
  size is re-read from Drive immediately before each move and the destination
  size is re-read after; on mismatch it moves the object *back* and aborts.
* **No DB coupling.** Archive objects are not `file_records` rows (the archive
  endpoint is not a media root and restore never composes it), so the migration
  changes no columns and cannot corrupt a restore mapping.

### 2.4 Code

* `myUtils/drive_layout.py:153` — `archive_prefix()`.
* `myUtils/drive_layout.py:158` — `structured_archive_path(relative_path, year,
  month, *, prefix=None)`; raises on an empty key or month out of range.
* `scripts/archive_drive.py` — `parse_year_month`, `year_month_from_modtime`,
  `is_structured`, `plan_moves`, `_list_archive`, `apply_plan`, CLI.

---

## 3. Archive dry-run (real Drive, no changes)

Command:

```bash
.venv/bin/python scripts/archive_drive.py
```

Output (abridged to the three representative line groups; the full run is 75
lines, 35 moves):

```
Mode: dry-run  remote: GDrive-willywang8216
Planned: 35 moves, 1128305860 bytes; already_structured=0; unresolved=0

  GDrive-willywang8216:sau/archive/my-compressed-2026-10/index.jsonl
    -> GDrive-willywang8216:sau/archive/2026/10/my-compressed-2026-10/index.jsonl  (7430 bytes)
  GDrive-willywang8216:sau/archive/my-compressed-2026-10/copy-pack.json
    -> GDrive-willywang8216:sau/archive/2026/10/my-compressed-2026-10/copy-pack.json  (6157 bytes)
  GDrive-willywang8216:sau/archive/my-compressed-2026-10/originals/nakedwill-nsfw/NSFW NW new years resolution.mp4
    -> GDrive-willywang8216:sau/archive/2026/10/my-compressed-2026-10/originals/nakedwill-nsfw/NSFW NW new years resolution.mp4  (8169767 bytes)
  ... (32 more, all sau/archive/<...> -> sau/archive/2026/10/<...>)

Nothing is ever deleted; --apply uses rclone moveto (server-side rename).
```

`--apply` was **not** run (see §7).

---

## 4. Offload drain verification

Command: `bash offload_to_drive.sh` (as `will`; exit 0).

| | Total | Used | Available | Use% |
| --- | ---: | ---: | ---: | ---: |
| Before | 103,859,404,800 | 85,888,385,024 | 17,954,242,560 | 83% |
| After | 103,859,404,800 | 85,888,421,888 | 17,954,205,696 | 83% |

Used disk moved by +36,864 bytes (log writes). **Zero files were offloaded.**
Local counts were unchanged: `videoFile=384`, `uploads=0`, `generated=3`.

New `logs/offload.log` lines:

```
[2026-10-10T01:32:42Z] offload start
[2026-10-10T01:32:43Z] keeping 384 file(s) local (in-flight / due soon / unrecorded / _library)
[2026-10-10T01:32:43Z] route plan:
[2026-10-10T01:32:43Z] offload done rc=0 local videoFile=384 uploads=0
```

`logs/offload-plan.log`:

```
# route_plan files=0
#   videoFile.published=0
#   uploads.published=0
#   generated.published=0
#   videoFile.inbox=0
#   uploads.inbox=0
#   generated.inbox=0
```

The run itself is clean (rc=0, no rclone filter error, no unlink). It drains
nothing because every remaining local file is caught by one of the two guards
in §1.3 and §1.4:

* 85 `_inbox_cache` files (8.3 GiB) are excluded as "unrecorded" only because
  the exclude builder does not strip `/app/`.
* 3 `generated` files (198 MiB) are already registered to a tiered backend and
  `build_route_plan` never routes already-tiered rows.
* 299 `_library` files (141 MiB) are intentionally kept.

So the offload is **not** broken, but it is **not draining what remains** until
those two guards are addressed. That is a pipeline change and was left to the
operator per the "do not redo the pipeline work" constraint.

---

## 5. Restore verification (5 files, real Drive)

For each row I took `storage_backends.endpoint` and `file_records.storage_key`
straight from the DB and fetched with
`myUtils.media_remote_storage.download_from_backend(dict(row), key, dest)` —
the composed `endpoint + "/" + storage_key` that must never change. Re-run
against current HEAD after the parallel session's `myUtils/worker.py` restore
change (`1247360`).

| id | endpoint | composed object | DB bytes | fetched | ok |
| ---: | --- | --- | ---: | ---: | :--: |
| 1198 | `sau/inbox/videoFile` | `.../SFW NW+SW invitation to gay pride .mp4` | 105,547,761 | 105,547,761 | ✅ |
| 1200 | `sau/inbox/generated` | `.../campaigns/campaign-2598/SFW NW+SW invitation to gay pride _pub.mp4` | 9,886,238 | 9,886,238 | ✅ |
| 995 | `sau/published/videoFile` | `.../4e4413a8-10e5-42fb-a4e6-b3fd3c7699ea_NSFW_SWNW_news.mp4` | 46,375,033 | 46,375,033 | ✅ |
| 980 | `sau/published/generated` | `.../campaigns/campaign-2519/watermarked_video/07089dab-...-_NSFW_NWSW-watermarked_video.mp4` | 3,306,086 | 3,306,086 | ✅ |
| 1229 | `sau/inbox/generated` | `.../campaigns/campaign-2603/b8f269e1-..._NSFW_NWSW_news_pub.mp4` | 76,292,416 | 76,292,416 | ✅ |

All 5 byte sizes matched exactly (`FAILURES: 0`). Four distinct tier roots are
covered. Note: during the inventory phase `rclone` intermittently returned
`403 RATE_LIMIT_EXCEEDED` on `lsjson` for `inbox/generated`, which is why an
early count read 0; the object listing and all restores succeeded on retry.

---

## 6. Tests

New file `tests/test_archive_drive.py`: **19 tests** —
`parse_year_month`, `year_month_from_modtime`, `is_structured`,
`drive_layout.structured_archive_path`, `plan_moves` (mapping, skip-already,
undated, `--date` default, bundle-split refusal), `apply_plan` (dry-run is a
no-op, verify-before/after, refuse on pre-move size mismatch, move-back on
post-move mismatch) and a CLI dry-run against a fake `rclone`.

Full suite:

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/test_security_http.py -q
```

```
1559 passed, 1 skipped, 139 subtests passed in 113.02s (0:01:53)
```

Collection count is 1560 with the new file and 1541 without it, i.e. exactly
+19 new tests, all green. The stated baseline was 1526; the repo already
carried 15 tests from parallel work by the time this ran, so the delta is +19
new tests against the current tree, with 1 pre-existing skip.

---

## 7. Operator decisions

1. **Run the archive migration for real?** Recommended, but the call is yours.
   Dry-run is 35 moves / 1,128,305,860 bytes, all `sau/archive/<bundle>/…` →
   `sau/archive/2026/10/<bundle>/…`. It is a server-side rename (no re-upload),
   byte-verified on both sides, and deletes nothing. Run:
   ```bash
   .venv/bin/python scripts/archive_drive.py --apply
   ```
   Re-run the dry-run first (it is idempotent and skips anything already
   structured). A DB backup is not required for this migration (archive objects
   have no `file_records` rows) but is cheap insurance.

2. **Drain the stuck offload (8.3 GiB).** Two pipeline changes, deliberately not
   made here:
   * Normalise `/app/<root>/` in the `recorded` set in
     `offload_to_drive.sh:684-698` (strip `/app/videoFile/`,
     `/app/uploads/`, `/app/generated/`). This alone frees the 85
     `_inbox_cache` files / 8,930,032,611 bytes.
   * Purge already-tiered local copies (or route them through a dedicated
     verified pass), so the 3 `generated` files / 208,073,097 bytes registered
     to `sau/published/generated` stop being skipped at
     `offload_to_drive.sh:394`.

3. **Legacy un-tiered Drive trees.** ~55.5 GB across `sau/videoFile` (650),
   `sau/uploads` (4) and `sau/generated` (485). `scripts/migrate_drive_layout.py`
   already exists for the DB-named subset; run its dry-run before deciding,
   and be aware most legacy objects (1,139 vs 685 named rows) are orphans.

---

## 8. References

* `offload_to_drive.sh:684-698` — `recorded` build (no `/app/` normalisation).
* `offload_to_drive.sh:245` — restore candidates already include `/app/...`.
* `offload_to_drive.sh:382-395` — route plan skips already-tiered rows.
* `myUtils/drive_layout.py:149-180` — archive tier recognition, date-structured
  archive path.
* `scripts/archive_drive.py` — migration planner and applier.
* `scripts/migrate_drive_layout.py` — legacy → tier migration (untouched).
* `myUtils/media_remote_storage.py` / `myUtils/rclone_storage.py` — restore path
  (`endpoint + "/" + storage_key`), unchanged.
* `tests/test_archive_drive.py` — 19 new tests.
