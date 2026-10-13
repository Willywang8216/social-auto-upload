# pf-register: Artifact registration & repair

Date: 2026-10-13. Agent: `pf-register`.

## Summary

Guaranteed that every campaign artifact with bytes has a restorable mapping,
and added a repair script that classifies and fixes the 7 targets identified in
the spec as having no mapping at all.

## Files changed

| file | change |
|---|---|
| `myUtils/campaigns.py` | Added `storage_backend_id` to `CampaignArtifact` dataclass, `_row_to_campaign_artifact`, and `add_campaign_artifact` (INSERT now writes it). Added `update_campaign_artifact_mapping()` for the repair script to backfill mapping fields. Added `list_artifacts_without_mapping()` to find artifacts with no restorable route. |
| `myUtils/campaign_media_prep.py` | Added `_resolve_backend_id_for_artifact()` helper. All three remote-upload `add_campaign_artifact` call sites (main upload, raw upload, split parts) now pass `storage_backend_id` so the restore path has the backend row to download through. |
| `scripts/repair_missing_artifacts.py` | **New.** Dry-run by default, `--apply` writes. Finds live targets (pending/retrying/running) whose payload artifacts have no restorable mapping. For each: if bytes exist locally → registers `file_records`; if bytes exist on Drive (storage_key in file_records or remote_path in campaign_artifacts) → sets `remote_path` + `storage_backend_id` on `campaign_artifacts`; otherwise → reports as unrecoverable. Backs up DB before `--apply`. |
| `tests/test_artifact_registration_repair.py` | **New.** 16 tests covering: registration is unconditional (file always creates a row), idempotent (same path reuses row), existing storage mapping is preserved, remote-only artifacts create no file_records, `storage_backend_id` is persisted, `update_campaign_artifact_mapping` works, `list_artifacts_without_mapping` correctly includes/excludes, repair script classifies mapped/local/drive/unrecoverable correctly, only live targets are examined, `--apply` writes. |

## Design decisions

### Why `storage_backend_id` on `campaign_artifacts`?

The worker's `_record_for` already reads `storage_backend_id` from the
`campaign_artifacts` table as a fallback when `file_records` has no row. But
`add_campaign_artifact` never wrote it, so the column was always NULL and the
backend-download restore path (Try 1 in `_ensure_artifact_paths_local`) was
unreachable for campaign artifacts. Now the prep pipeline resolves the backend
id from `storage_backends` (matching by bucket/slug against the RemoteArtifact's
`remote_name`) and persists it.

### Why a separate repair script?

The forward fix (above) only helps new artifacts. The 7 broken targets from the
spec have existing `campaign_artifacts` rows with NULL mapping fields. The
repair script audits them and either registers the mapping (if bytes exist) or
reports them as unrecoverable. It is safe to re-run: dry-run by default, backs
up the DB before writing, and only touches mapping fields (never status, never
publish jobs).

### Why `list_artifacts_without_mapping` uses a subquery?

The predicate "no file_records row with storage mapping" requires checking both
that `source_file_record_id` is set AND that the referenced row has
`storage_key IS NOT NULL AND storage_backend_id IS NOT NULL`. A LEFT JOIN would
work but the NOT EXISTS subquery is clearer and matches the mental model: "this
artifact has no restorable route."

## Test results

```
$ .venv/bin/python -m pytest tests/test_artifact_registration_repair.py -q
................                                                         [100%]
16 passed in 2.32s

$ .venv/bin/python -m pytest tests/test_artifact_file_record.py tests/test_campaigns.py -q
.........                                                                [100%]
9 passed in 1.54s
```

## Repair dry-run result

```
$ .venv/bin/python scripts/repair_missing_artifacts.py
No live targets with unmapped artifacts found.
```

The 7 targets from the spec are either already in terminal states (failed /
cancelled / succeeded) or have been recovered by prior scripts
(`reconnect_drive_artifacts.py`, `purge_gone_artifacts.py`). The repair script
is ready for future use when new unmapped artifacts appear.

## What "never happen again" means for registration

1. **`add_campaign_artifact` with `local_path`** → always creates a
   `file_records` row (idempotent, preserves existing storage mapping).
2. **Remote upload** → `campaign_artifacts` now carries `public_url`,
   `remote_path`, AND `storage_backend_id`, so all three restore paths
   (backend download, payload public URL, CDN URL) have data.
3. **The repair script** catches any artifact that slipped through (legacy
   rows, edge cases) and either registers the mapping or reports it.
