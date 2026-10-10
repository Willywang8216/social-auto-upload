# Legacy untiered Drive trees: what is unused vs what is in use

Date: 2026-10-10 · measured against the live Drive and `db/database.db`

## Access and method

- Drive listing per legacy root:
  `rclone --config ~/.config/rclone/rclone.conf lsf GDrive-willywang8216:sau/<root> --recursive --files-only --format ps`
- "Referenced" = a `file_records` row whose `storage_backend_id` points at that
  legacy endpoint (`sau/videoFile`, `sau/uploads` or `sau/generated`).
- "Live" = `publish_job_targets.status IN ('pending','retrying','running')`.
- An object counts as **in use** if any of:
  1. a `file_records` row maps it (storage key + backend), or
  2. a **live** target payload names it (`publish_jobs.payload_json`
     `artifacts[].local_path` / `.public_url`), or
  3. it is a `campaign_artifacts` row whose campaign has at least one live target.

## The three legacy roots

| root | objects on Drive |
| --- | ---: |
| `sau/videoFile` | 650 |
| `sau/uploads` | 4 |
| `sau/generated` | 483 |

Only 670 of the 1,137 are mapped by a `file_records` row. Crucially, **mapping is
not the same as usage**, and the gap is not free to delete:

| root | orphan (no mapping) | needed by a LIVE payload | needed by a LIVE campaign | **NOT needed anywhere** |
| --- | ---: | ---: | ---: | ---: |
| `sau/videoFile` | 453 | 244 (134 MB) | 248 (268 MB) | **205 (15,593 MB)** |
| `sau/uploads` | 3 | 0 | 0 | **3 (1,026 MB)** |
| `sau/generated` | 11 | 11 (466 MB) | 11 (466 MB) | **0** |
| **total** | **467** | | | **208 (≈16.6 GB)** |

The columns overlap (a `generated` object is both a live payload reference and a
live campaign reference), so they are not additive.

### The trap, stated plainly

**310 of the 453 `videoFile` "orphans" are cited elsewhere in the database.**
Deleting on "no `file_records` mapping" alone would have destroyed the source
media of scheduled posts. A spot check of one apparent "not needed" object found
a `campaign_artifacts` row (id 2497, campaign 2570) — but that campaign is
`needs_review` with **zero targets**, so it is genuinely not live. The classifier
above is therefore the one to trust, and it must be exact-match on the Drive key
rather than on the basename.

## Recommended action

- **208 objects / ≈16.6 GB unused** → quarantine (a reversible `rclone moveto`
  to `sau/trash/<date>/`), not an irreversible delete. It is a shared Drive; a
  reversible move costs nothing and can be undone.
- **The remaining ≈929 objects stay.** They are the real assets of scheduled
  posts. They are already on Drive, so nothing needs uploading — the requirement
  "if something is using them they should be offloaded to Drive" is already
  satisfied for these; what matters is that they are not removed.
- Any legacy object that is needed but **not** present in a tiered tree should be
  reported so it can be promoted by `scripts/migrate_drive_layout.py` (DB-driven;
  note it can only name the 685 rows the DB knows).

## Why the legacy trees exist at all

`bc05676` fixed the offload so it now writes to the tick tiered layout
(`sau/inbox/<root>` / `sau/published/<root>`). The untiered roots are the
**pre-fix** layout: everything the cron offloaded before it worked, plus objects
whose `file_records` rows predate the tier split. They are not corrupted — just
organised under the old scheme, which is why the choice is "quarantine the
unused" rather than "migrate everything".

## Verified separately

Restore from the **tiered** trees is byte-exact (8/8 sampled across
`sau/inbox/videoFile` and `sau/published/*`, including 573 MB files), so the
in-use assets are safe where they are.
