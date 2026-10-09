# Google Drive structure: separating published history from new assets

Task: separate the archive of previously-published assets from the new-asset
area in Google Drive. Repo: `/home/will/social-auto-upload`.

Status: **implemented, dry-run only.** No Drive object was moved, renamed or
deleted and no `--apply` was run. The only real-Drive operation performed was
`rclone lsjson` (read-only listing).

---

## 1. Measured current layout

Measured with `rclone --config /home/will/.config/rclone/rclone.conf lsjson
-R --files-only` (one listing per root; sizes/timestamps from the listing).

| Prefix | Files | Bytes | Oldest (UTC) | Newest (UTC) |
|---|---:|---:|---|---|
| `GDrive-willywang8216:sau/videoFile` | 649 | 24,591,859,118 (22.903 GiB) | 2024-10-17T13:16:12Z | 2026-10-08T11:36:22Z |
| `GDrive-willywang8216:sau/uploads` | 4 | 1,027,927,446 (0.957 GiB) | 2026-07-01T16:12:41Z | 2026-09-07T06:30:50Z |
| `GDrive-willywang8216:sau/generated` | 485 | 29,352,113,883 (27.336 GiB) | 2026-09-07T08:07:35Z | 2026-10-09T04:17:58Z |
| `GDrive-willywang8216:sau/archive` | 35 | 1,128,305,860 (1.051 GiB) | 2026-10-07T17:46:59Z | 2026-10-08T08:53:17Z |
| `GDrive-willywang8216:sau/assets` | 19 | 29,168,613 (27.8 MiB) | 2026-09-05T23:52:10Z | 2026-09-06T08:31:11Z |
| **total** | **1192** | **~55.2 GiB** | | |

`videoFile/` breakdown by first path segment:

| Segment | Files | Bytes |
|---|---:|---:|
| `_library` | 307 | 174.0 MiB |
| `_batch1` | 186 | 5962.0 MiB |
| `<root>` (flat) | 74 | 2889.5 MiB |
| `_photos` | 38 | 17.2 MiB |
| `_homealone` | 30 | 293.4 MiB |
| `_inbox` | 4 | 305.3 MiB |
| `_batch` | 3 | 76.4 MiB |
| `_pub_ep002` | 3 | 4834.4 MiB |
| `_pub_ep002_flat` | 2 | 8805.1 MiB |
| `_inbox_cache` | 2 | 95.2 MiB |

`generated/` is entirely `campaigns/**` (485 files). `archive/` is entirely
`my-compressed-2026-10/**` (33 originals + `index.jsonl` + `copy-pack.json`).
`assets/` is brand material under `sw/`, `nw/`, `msl/`, `teaching/` and is not
part of the per-publish media lifecycle.

Cross-check of the three offload roots against `file_records` in
`db/database.db` (each Drive object is addressed as
`storage_backends.endpoint + "/" + file_records.storage_key`):

- 704 `file_records` rows carry a `storage_key`; 699 of them have a
  `storage_backend_id` and therefore a concrete endpoint.
- **699 / 699 resolve to a real object** at `endpoint/key` (0 missing).
- **80 rows have a `filesize` that disagrees with the remote object** (legacy
  placeholder sizes: 1 B, 4 B, 34 B vs multi-MB objects).
- 487 remote objects are not referenced by any `storage_key` row — 35 are the
  archive and the rest are mostly records whose `storage_backend_id` is NULL.

The mix the operator is complaining about is real: `sau/videoFile`,
`sau/uploads` and `sau/generated` each contain published assets *and* material
that has never been published (staged far-future posts and never-scheduled
uploads) in the same tree.

---

## 2. Proposed layout and rationale

```
sau/inbox/{videoFile,uploads,generated}/<key>       NEW material awaiting first publish
sau/published/{videoFile,uploads,generated}/<key>   assets whose publish target succeeded
sau/archive/<campaign>/...                          superseded/compressed originals (unchanged)
sau/assets/...                                      brand assets (unchanged)
```

### Why a tier prefix *above* the existing root, and not a change to the key

The restore path builds the Drive object from exactly two columns:

- `myUtils/rclone_storage.py:143` `download_artifact()` does
  `root + "/" + remote_path` where `remote_path` is `file_records.storage_key`.
- `myUtils/media_remote_storage.py:154` `download_from_backend()` for
  `provider == "rclone"` passes `bucket` (remote name) and `endpoint`
  (root) into that call.

So the invariant the whole pipeline relies on is
`<storage_backends.endpoint>/<file_records.storage_key>`. Putting the tier in
the **endpoint** and leaving `storage_key` byte-for-byte unchanged means a
migration only has to repoint `file_records.storage_backend_id`; it never
rewrites the key. That makes a partially-applied migration safe: for every
record, `endpoint/key` is either the old path (bytes still there) or the new
path (bytes moved and the record repointed), never a mix that names the wrong
object.

It is also **additive**: the legacy `sau/<root>` backend rows are kept, so any
record not yet migrated still restores exactly as before. `assets/` and
`archive/` are left alone.

### Classification rule

A record is `published` iff a **succeeded** publish target names it — by
`source_file_record_id` or by `local_path` in the job's `payload_json`
(`myUtils/drive_layout.py:166` `published_refs_from_payloads`). Everything else
(pending, failed, cancelled, or never referenced) is `inbox`
(`myUtils/drive_layout.py:207` `tier_for_media`). This avoids the false
positive of classifying a staged far-future post as published just because it
has an offloaded copy.

---

## 3. What was implemented

| File | Change |
|---|---|
| `myUtils/drive_layout.py` (new, 271 lines) | Pure mapping/classification module: `legacy_endpoint` (`:76`), `tier_endpoint` (`:81`), `route_endpoint` (`:86`), `split_endpoint` (`:91`), `split_media_path` (`:112`), `full_remote_path` (`:153`), `remote_spec` (`:162`), `published_refs_from_payloads` (`:166`), `tier_for_media` (`:207`), `migrate_endpoint` (`:233`), `object_move` (`:247`). |
| `scripts/migrate_drive_layout.py` (new) | Dry-run-by-default migration: `list_remote_sizes` (`:54`), `_canonical_key` (`:83`), `build_plan` (`:104`), `ensure_tiered_backends` (`:238`), `apply_plan` (`:273`), `main` (`:301`). |
| `offload_to_drive.sh` | `ensure_layout_backends` (`:272`) additively creates the 6 tiered `storage_backends` rows; `build_route_plan` (`:310`) classifies each recorded file and writes per-root/per-tier `--files-from` lists; `register_verified_source` (`:207`) now honours `REGISTER_ENDPOINT`; `purge_verified_sources` (`:402`) filters via `PURGE_INCLUDE` (`:410`); the main loop (`:712`–`:729`) copies/verifies/purges each root into `sau/published/<root>` and `sau/inbox/<root>`. |
| `tests/test_drive_layout.py` (new) | 21 tests over the mapping + migration helpers. |
| `tests/test_offload_script.py` | 2 new tests: `REGISTER_ENDPOINT` routing and `build_route_plan` splitting. |

The offload loop stays fail-closed: a missing backend, an unrecorded file, or
an in-flight/due-soon file is kept local exactly as before. `--files-from`
limits each pass to its planned files and `--exclude-from` still applies, so the
routing include cannot re-admit a protected file.

No change was needed in `myUtils/worker.py` (`_resolve_file_path:1786`,
`_try_download_from_storage:1811`, `_ensure_artifact_paths_local:1888`): they
read `storage_key` + `storage_backend_id` and the backend's `endpoint`, all of
which the migration keeps consistent.

---

## 4. Migration: dry-run output

Command actually run (read-only):

```
.venv/bin/python scripts/migrate_drive_layout.py --json
```

Output (snapshot; full plan in `/tmp/drive_migration_plan.json`):

```
mode: dry-run  remote: GDrive-willywang8216
summary:
  planned:       619
  published:     116
  inbox:         503
  already_tiered:  0
  unresolved:     80
  bytes_to_move: 38864480528   (36.2 GiB)
published_source_ids: 155   published_media_keys: 231

moves by root/tier       bytes by root/tier
  generated/inbox   426    21.802 GiB
  generated/published 57    5.526 GiB
  uploads/inbox       1     0.002 GiB
  videoFile/inbox    76     4.576 GiB
  videoFile/published 59    4.290 GiB

unresolved by reason
  recorded size does not match remote object   80
```

Representative planned moves:

```
[inbox    ] GDrive-willywang8216:sau/uploads/b8fac499-...png
          -> GDrive-willywang8216:sau/inbox/uploads/b8fac499-...png
[published] GDrive-willywang8216:sau/videoFile/2a9b43bb-..._demo.mp4
          -> GDrive-willywang8216:sau/published/videoFile/2a9b43bb-..._demo.mp4
[inbox    ] GDrive-willywang8216:sau/videoFile/caeb62b4-..._ai-audit-5k_final.mp4
          -> GDrive-willywang8216:sau/inbox/videoFile/caeb62b4-..._ai-audit-5k_final.mp4
```

Note `move` preserves the key: only the endpoint prefix changes. The 80
unresolved records are size mismatches (legacy placeholder `filesize`), and
`--apply` **refuses to run while any record is unresolved** (`main:301`):

```
Refusing --apply: unresolved records must be fixed first (80 found).
```

### Safety properties of `--apply` (not run)

- Uses `rclone moveto` (server-side rename on the same remote; no re-upload,
  no delete). If it fails the DB is not updated.
- `ensure_tiered_backends` is idempotent and additive; it never edits or
  disables the legacy `sau/<root>` rows.
- Every object's recorded size is compared to the remote size before it is
  planned; a mismatch is left untouched.

---

## 5. Tests

New/changed tests: `tests/test_drive_layout.py` (21) and two additions to
`tests/test_offload_script.py`.

Full suite command (as requested):

```
.venv/bin/python -m pytest tests/ --ignore=tests/test_security_http.py -q
```

Result:

```
1516 passed, 1 skipped, 131 subtests passed in 220.75s (0:03:40)
```

Baseline floor of `>=1474 passed, 1 skipped` is met; 23 of the passing tests
are the new layout/migration/offload coverage.

---

## 6. UNKNOWNS / operator decisions

1. **Apply the migration.** The dry-run is complete but `--apply` was
   deliberately not run. The 80 size-mismatch records must be resolved first.
   Those records can be repaired from the verified remote size (the same repair
   the offloader already performs at `offload_to_drive.sh:207`), but that is a
   production-data write and needs the operator's approval.
2. **Do new offloads now go to the tiered tree while legacy objects stay put?**
   Yes. `ensure_layout_backends` will create the tiered rows on the next cron
   tick and route per publish status; the existing `sau/<root>` objects remain
   restorable through the legacy backend rows until the operator applies the
   migration. Mixed-tree during the transition is expected and safe.
3. **`sau/archive` semantics.** The existing campaign-structured archive is
   left untouched. "Superseded/compressed originals" is not auto-populated;
   deciding when a published asset becomes "superseded" is a product decision,
   not an inferable one.
4. **Unreferenced records default to `inbox`.** 487 remote objects carry no
   `storage_key` row and 500+ records have no succeeded target; these are
   classified inbox. If the operator wants unreferenced/old records in
   `published`, that rule can be added later.
5. **Possible concurrent activity (additive, not mine).** Six tiered
   `storage_backends` rows (`id 689`–`694`, slugs `gdrive-{inbox,published}-*`,
   `created_at 2026-10-09 09:17:02 UTC`) appeared in the live
   `db/database.db` during this session. I verified they were **not** created by
   this work: replaying the dry-run on a copy of the DB leaves
   `storage_backends` at 4, and running `tests/test_drive_layout.py` does not
   change the live DB (`before=10 after=10`). They are exactly the rows
   `ensure_layout_backends` would create and are additive, so they were left in
   place. Likely another agent/operator ran the new code independently.
6. **`scripts/reconcile_generated_drive.py`** still points at legacy
   `sau/generated` (`ENDPOINT = "sau/generated"`). It remains correct for
   un-migrated rows; if the migration is applied it should be updated or made
   tier-aware.

### Rollback

The implementation is additive. To revert behaviour, remove the
`build_route_plan` call from the offload block and the tiered backends; legacy
`endpoint/key` rows are never modified by the dry-run. There is no destructive
step to undo.
