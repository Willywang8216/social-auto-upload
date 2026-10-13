# pf-offload: fail-closed offload invariant

Date: 2026-10-13. Agent: `pf-offload`.

## Problem

The offload (`offload_to_drive.sh`) could purge a local file referenced by a
live publish target without a verified restore mapping. After purge, the
publisher fails with `PreparedPublishError: media file is missing` because the
worker has no way to pull the bytes back from Drive.

Root cause: `pending_excludes()` excluded in-flight and due-soon targets' files,
but far-future pending targets' files were NOT excluded. If such a file had a
`file_records` row (so it passed the "unrecorded" exclusion) but lacked
`storage_key`+`storage_backend_id` (no verified restore mapping), the offload
would copy it to Drive, verify the remote copy, and purge the local file —
leaving the future target with no restore route.

## Fix

### `offload_to_drive.sh` — `pending_excludes()`

Added a third pass after the existing in-flight and due-soon exclusions:

- Queries ALL live targets (`pending`/`running`/`retrying`)
- For each file referenced by a live target, checks if a verified restore
  mapping exists:
  - `file_records` row with `storage_key IS NOT NULL AND storage_backend_id IS NOT NULL`, OR
  - `campaign_artifacts` row with `public_url IS NOT NULL OR remote_path IS NOT NULL`
- If no mapping exists, emits the file as an exclusion (kept local)

Files WITH a verified mapping are safe to offload: the worker can restore them
at claim time.

### Defense in depth

The existing `purge_verified_sources()` already requires `register_verified_source()`
to succeed before unlinking, which writes `storage_key`+`storage_backend_id` to
`file_records`. So even if a file somehow reaches the purge step, the mapping is
persisted before deletion. The new exclusion in `pending_excludes()` is the
primary guard: it prevents unmapped live-target files from entering the purge
pipeline at all.

## Files changed

| file | change |
|---|---|
| `offload_to_drive.sh` | Added `has_restore_mapping()`, `emit_file_ref_if_unmapped()`, `emit_media_path_if_unmapped()` helpers and a new exclusion pass in `pending_excludes()` |
| `tests/test_offload_fail_closed.py` | NEW: 5 regression tests covering the exclude/keep decision |
| `docs/media-storage-pipeline.md` | Documented the fail-closed invariant |

## Test results

```
tests/test_offload_fail_closed.py::FailClosedTests::test_campaign_artifacts_public_url_counts_as_mapping PASSED
tests/test_offload_fail_closed.py::FailClosedTests::test_far_future_pending_with_mapping_is_purgeable PASSED
tests/test_offload_fail_closed.py::FailClosedTests::test_far_future_pending_without_mapping_is_kept PASSED
tests/test_offload_fail_closed.py::FailClosedTests::test_live_target_with_mapping_is_purgeable PASSED
tests/test_offload_fail_closed.py::FailClosedTests::test_live_target_without_mapping_is_kept PASSED

5 passed
```

All 17 existing `test_offload_script.py` tests still pass.

`bash -n offload_to_drive.sh` → Syntax OK.

## Test scenarios

| scenario | target status | mapping | expected | result |
|---|---|---|---|---|
| Live target WITH mapping | pending (far-future) | file_records(storage_key, backend_id) | purgeable (not excluded) | ✓ |
| Live target WITHOUT mapping | retrying | none | kept local (excluded) | ✓ |
| Far-future pending WITH mapping | pending (2099) | file_records(storage_key, backend_id) | purgeable (not excluded) | ✓ |
| Far-future pending WITHOUT mapping | pending (2099) | none | kept local (excluded) | ✓ |
| campaign_artifacts public_url | pending (far-future) | campaign_artifacts(public_url) | purgeable (not excluded) | ✓ |

## Scope compliance

- Did NOT edit `myUtils/worker.py` or `myUtils/campaigns.py`
- Only touched `offload_to_drive.sh`, `docs/media-storage-pipeline.md`, and new `tests/test_offload_fail_closed.py`
- No live offload runs; all tests use fixture DBs and temp dirs
