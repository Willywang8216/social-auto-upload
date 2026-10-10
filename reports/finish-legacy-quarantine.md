# Finish the legacy Drive quarantine — re-verified, restored, completed

Date: 2026-10-10 · Executed against the live Drive (`GDrive-willywang8216`) and
`db/database.db`, on branch `main` at `32a1991`.

Tool: `scripts/purge_legacy_drive.py` (dry-run by default). This report completes
the work begun in `reports/purge-legacy-drive.md` and adds the missing
`--restore` mode that makes the quarantine trustworthy.

## 0. TL;DR

- The classifier was **re-run against the current DB**. The live target count
  had grown (1334 → 1340), so the split changed: **927 needed / 191 unused**
  (`16,484,031,675 B`) at the first dry-run.
- The 20 objects already in `sau/trash/2026-10-10` were re-checked against the
  current DB: **0 were needed, 0 restores required**.
- A new **`--restore`** mode (exact inverse of `--apply`, verify-before-remove,
  never overwrites) was added and proved on the live Drive.
- The quarantine was **completed**: the legacy roots now hold **921 objects, all
  needed, 0 unused**. The trash tree holds **216 objects / 18,509,388,450 B**,
  every one of which re-checks as unused against the current DB.
- Tests: **1613 passed, 1 skipped, 161 subtests** (baseline 1599 + 14 new).

## 1. Re-verified classifier split (current DB)

Command:

```bash
.venv/bin/python scripts/purge_legacy_drive.py --json > /tmp/dryrun-current.json
```

Reference sets read from the DB at that moment:

| set | value |
| --- | ---: |
| `file_records` | 666 |
| live payload keys | 469 |
| live campaign keys | 508 |
| live campaigns | 435 |
| live targets (pending 1334 + running 6) | 1340 |

Classification per legacy root (before this session moved anything):

| root | objects | needed | unused | unused bytes |
| --- | ---: | ---: | ---: | ---: |
| `sau/videoFile` | 631 | 444 | 187 | 15,284,367,466 |
| `sau/uploads` | 4 | 1 | 3 | 1,026,159,060 |
| `sau/generated` | 483 | 482 | 1 | 173,505,149 |
| **TOTAL** | **1118** | **927** | **191** | **16,484,031,675** |

Why this differs from the 929 / 208 in the task brief: the DB is live. Between
the earlier analysis and this run, targets finished, so fewer objects were
protected. The classifier is deliberately keyed on *live* targets
(`pending`/`retrying`/`running`), which is exactly why it must be re-run.

### The DB kept moving during the run

The most visible shift was `sau/generated`: it had 1 unused object at the first
dry-run and 7 by the time the batch driver listed it. Six campaign artifacts
became unused because their campaigns' last live targets finished:

```
campaigns/campaign-2237/20260822085411068_pub_pub.mp4   323,708,685 B
campaigns/campaign-2278/20260822085411068_pub_pub.mp4   323,708,685 B
campaigns/campaign-2324/SFW lv_0_20260907103730_part1_pub_pub.mp4   173,505,149 B
campaigns/campaign-2325/SFW lv_0_20260907103730_part2_pub_pub.mp4   167,298,754 B
campaigns/campaign-2326/SFW lv_0_20260907103730_part3_pub_pub.mp4   167,292,682 B
campaigns/campaign-2339/20260820122135356_pub_pub.mp4   238,110,124 B
campaigns/campaign-2340/20260822085411068_pub_pub.mp4   323,708,685 B
```

None is mapped by a `file_records` row, names a live target payload, or belongs
to a live campaign. They are eligible by the same definition as every other
unused object, and were quarantined (reversibly). They are called out here so
the operator can review them; `--restore` brings any of them back in one
command.

## 2. Trash re-check for the interrupted run (step 2)

The interrupted run had moved **20 objects / 481,529,615 B** into
`sau/trash/2026-10-10/videoFile`. Before doing anything else, all 20 were
re-classified against the *current* DB with the same strong + weak rules, run
against their original `(root, key)`.

Result: **0 needed, 20 still unused, 0 restores required.** No object in the
trash tree is named by a `pending`/`retrying`/`running` target, nor by a
`file_records` row, nor by a live campaign. The safety net found nothing to
bring back at that moment.

The same check was repeated at the end over the **full 216-object trash tree**:
**0 needed, 216 unused, 0 restores.** See §5.

## 3. `--restore` mode (new, step 3)

`--restore` is the exact inverse of `--apply` and never deletes:

1. list `sau/trash/<date>` and split each path into `(root, key)`,
2. `rclone copyto` the trash object back to `sau/<root>/<key>`,
3. `rclone lsjson` the legacy destination and require the size to match the
   trash object,
4. only then `rclone deletefile` the trash copy.

It refuses to overwrite a legacy path that already holds a *different* object
(`destination_conflict`); an equal-size object is reported `already_present`.
A non-existent trash tree is a clean no-op.

```bash
# plan (no writes)
.venv/bin/python scripts/purge_legacy_drive.py --restore \
    --quarantine-prefix sau/trash/2026-10-10
# restore everything in the tree
.venv/bin/python scripts/purge_legacy_drive.py --restore \
    --quarantine-prefix sau/trash/2026-10-10 --apply
# restore one object
.venv/bin/python scripts/purge_legacy_drive.py --restore \
    --quarantine-prefix sau/trash/2026-10-10 \
    --restore-key videoFile/_batch1/payloads/NW-NSFW-2026-09-21-1T_pub.json --apply
```

**Real integration proof** (a 425 B payload JSON, restored then re-quarantined):

```
restore results:
  restored: 1
  restored bytes: 425
```

`rclone lsjson` on the legacy path then returned the object at 425 B, and
`rclone lsjson` on the trash path returned rc=3 (gone).

## 4. Bounded, resumable apply (step 4)

The apply is resumable: every completed move removes the object from its legacy
root, so a re-run simply finds fewer. Each run re-reads the DB and reclassifies,
so an object that became needed is left alone.

A single `--apply` pass was too slow to finish in one shot: the shared Google
Drive API project is rate-limited (`Quota exceeded … RATE_LIMIT_EXCEEDED` on
`drive.googleapis.com`), so rclone's pacer backs off ~1–2 min per object. The
script therefore supports an opt-in **`--batch`** mode that does, per root, one
`copy --files-from`, one recursive `lsjson` verification, and one
`delete --files-from` for the verified sources — instead of three rclone
processes per object. It preserves the invariant that **no source is removed
until its destination has been verified by `lsjson` at the same size**, and it
still runs under `--limit` so each invocation is bounded.

Completed batches (all `quarantined`; no failures):

| run | roots | objects | bytes moved | unused before |
| --- | --- | ---: | ---: | ---: |
| per-object `--limit 25` #1 | videoFile | 25 | 12,037 | 176 |
| per-object `--limit 25` #2 | videoFile | 25 | 11,782 | 151 |
| `--batch --limit 3` (integration) | videoFile | 3 | 1,362 | 125 |
| `--batch --limit 40` #1 | videoFile | 40 | 19,136 | 122 |
| `--batch --limit 40` #2 | videoFile/uploads | 40 | 582,420,577 | 82 |
| `--batch --limit 40` #3 | videoFile/generated | 40 | 16,710,897,977 | 42 |
| `--batch --limit 40` #4 | generated | 2 | 561,818,809 | 2 |
| `--batch --limit 40` #5 | — | 0 | 0 | 0 |

Two runs were interrupted early on purpose (the first 40-minute per-object
attempt and the per-object batch #3 when the batch driver replaced it); their
partial moves are included in the final Drive state and are self-healing
because every operation is copy → verify → delete.

## 5. Final state (steps 1–4 verification)

**Legacy roots (re-listed after the last batch):**

| root | objects | needed | unused | unused bytes |
| --- | ---: | ---: | ---: | ---: |
| `sau/videoFile` | 444 | 444 | 0 | 0 |
| `sau/uploads` | 1 | 1 | 0 | 0 |
| `sau/generated` | 476 | 476 | 0 | 0 |
| **TOTAL** | **921** | **921** | **0** | **0** |

The 921 needed + the 216 quarantined = 1137, exactly the object count at the
start of the earlier report.

**Trash tree `sau/trash/2026-10-10`:**

| root | objects | bytes |
| --- | ---: | ---: |
| `sau/videoFile` | 206 | — |
| `sau/uploads` | 3 | — |
| `sau/generated` | 7 | — |
| **TOTAL** | **216** | **18,509,388,450** |

**Final safety net** — every one of the 216 trash objects was re-checked against
the current DB (657 `file_records`, 460 live-payload keys, 498 live-campaign
keys, 428 live campaigns, 1314 live targets):

```
TRASH RECHECK: needed= 0 unused= 216
  (no needed)
```

So **no restores were required at any point**. This session moved
**196 objects / 18,027,858,835 B** (the final 216 / 18,509,388,450 B minus the
20 / 481,529,615 B that predated the session).

## 6. Tests (steps 3, 5)

`tests/test_purge_legacy_drive.py` now has 24 tests. The new ones:

- `LegacyPurgeRestoreTests` (9): the restore spec is the exact inverse of the
  quarantine spec; `parse_trash_entries`; dry-run planning; copy→verify→delete
  ordering; refusing to overwrite an equal-size or different-size legacy object;
  keeping the trash copy when the copy or the verification fails; and
  `allow_missing` for a non-existent trash tree.
- `LegacyPurgeBatchTests` (5): batch quarantines everything it verified;
  never deletes a source that failed verification; deletes nothing when the copy
  fails; marks delete failures; dry-run makes no rclone calls.

```
.venv/bin/python -m pytest tests/ --ignore=tests/test_security_http.py -q
1613 passed, 1 skipped, 161 subtests passed
```

The pre-change baseline on this checkout is **1599 passed, 1 skipped** (the
1592 in the task brief was stale by 7); the +14 are exactly the new restore and
batch tests.

## 7. Commands run (audit)

| command | result |
| --- | --- |
| `purge_legacy_drive.py --json` | 927 needed / 191 unused / 16,484,031,675 B |
| trash re-check of the initial 20 objects | 0 needed, 0 restores |
| `purge_legacy_drive.py --restore --quarantine-prefix sau/trash/2026-10-10` | planned all 20 |
| `--restore --restore-key …NW-NSFW-2026-09-21-1T_pub.json --apply` | restored 1 (425 B), trash copy gone |
| `--apply --limit 25` ×2 (per-object) | 50 quarantined |
| `--apply --batch --limit 3` | 3 quarantined |
| `--apply --batch --limit 40` | 122 quarantined over 4 productive batches |
| `purge_legacy_drive.py --json` (final) | 921 needed / **0 unused** |
| `rclone lsjson -R sau/trash/2026-10-10` | 216 objects / 18,509,388,450 B |
| trash re-check of all 216 | 0 needed, 0 restores |
| `pytest tests/ --ignore=tests/test_security_http.py -q` | 1613 passed, 1 skipped |

To reverse the whole quarantine:

```bash
.venv/bin/python scripts/purge_legacy_drive.py --restore \
    --quarantine-prefix sau/trash/2026-10-10 --apply
```
