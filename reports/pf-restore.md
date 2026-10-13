# pf-restore: Media Restore Preflight Fix

Date: 2026-10-13  
Agent: `pf-restore`  
Scope: `myUtils/worker.py`, `tests/test_worker_media_restore.py`

## Problem

47 failed `publish_job_targets` referenced a `watermarked_video` artifact that **HAS** a restore mapping (file_records row with storage_key + storage_backend_id) but the publisher still raised:
- `PreparedPublishError: Telegram media file is missing or empty: /app/generated/...watermarked_video.mp4`
- `PreparedPublishError: YouTube video artifact is missing or empty: ...`
- `sociamonials fallback failed ... fallback error: local media missing: /app/generated/...`

The restore was not actually happening before the publisher ran, OR the restore failed but the error was not distinguished as retryable vs permanent, so the target burned 3 instant attempts and was marked permanently failed.

## Root Cause

Three compounding defects:

1. **No retryable attribute on MediaRestoreError**: The error didn't distinguish between "no mapping exists" (permanent, retrying won't help) and "mapping exists but download failed transiently" (retryable, the bytes are on the backend). The worker's `_handle_failure` treated all `MediaRestoreError` as retryable (default `retryable=True`), so targets with no mapping burned 3 instant attempts before being marked permanently failed.

2. **Restore preflight was soft**: `_ensure_artifact_paths_local` raised `MediaRestoreError` but didn't enforce that the restore actually succeeded before the publisher ran. If the download failed silently or the error was caught somewhere, the publisher would still be called with a missing file.

3. **Stale mappings**: campaign-2514 had a file_records row (storage_key=campaigns/campaign-2514/...mp4, storage_backend_id=691) but the object was NOT on Drive (`rclone lsf` returned "directory not found"). The mapping existed but the bytes were gone.

## Changes

### 1. Added `retryable` attribute to `MediaRestoreError` (myUtils/worker.py:1932-1944)

```python
class MediaRestoreError(RuntimeError):
    """A required publish media file could not be restored locally.

    ``retryable`` distinguishes a transient download failure (the mapping
    exists and the bytes are on the backend, but the fetch failed —
    network timeout, 503, etc.) from a permanent one (no mapping at all,
    or the mapping is stale and the object is gone).  The worker's
    ``_handle_failure`` reads this attribute to decide whether the target
    should be retried or marked permanently failed.
    """

    def __init__(self, *args, retryable: bool = True) -> None:
        super().__init__(*args)
        self.retryable = retryable
```

### 2. Set `retryable` appropriately at each raise site

**Permanent (retryable=False)**:
- `_resolve_file_path`: "Required media is unavailable locally or in storage" — no file_records row
- `_ensure_artifact_paths_local`: "Could not find artifact source record" — no mapping found
- `_ensure_artifact_paths_local`: "Artifact ... is missing locally and has no file record" — no mapping
- `_ensure_artifact_paths_local`: "Artifact is missing and has no safe public HTTPS recovery URL" — no mapping/URL
- `_ensure_artifact_paths_local`: "Could not restore artifact ... no usable storage source" — no usable source
- `_ensure_artifact_paths_local`: "Could not restore artifact ... after normalisation" — path resolution failure

**Retryable (retryable=True)**:
- `_try_download_from_storage`: "Remote restore failed" — mapping exists, download failed (transient)
- `_ensure_artifact_paths_local`: "Artifact restore failed" — mapping exists, download failed (transient)
- `_ensure_artifact_paths_local`: generic catch-all — could be transient

### 3. Added comprehensive tests (tests/test_worker_media_restore.py)

**ArtifactRestoreFromBackendTests** (3 tests):
- `test_file_records_row_triggers_backend_download_and_size_verification`: Reproduces the campaign-2514 failure. An artifact with a file_records row (storage_key + storage_backend_id) must be downloaded via the backend, size-verified, and placed at the local path.
- `test_campaign_artifacts_fallback_with_public_url`: When no file_records row exists but campaign_artifacts has public_url, the restore must use the public URL. Covers the campaign-2568 case.
- `test_generated_artifact_restores_from_file_records`: A generated artifact (watermarked_video kind) with a file_records row must restore from the backend, not fall back to source bytes.

**MediaRestoreErrorRetryableTests** (3 tests):
- `test_no_mapping_is_permanent`: An artifact with no file_records row and no campaign_artifacts row must raise MediaRestoreError with retryable=False.
- `test_backend_download_failure_is_retryable`: An artifact with a file_records row but a failed backend download must raise MediaRestoreError with retryable=True (transient).
- `test_successful_restore_does_not_raise`: A successful restore must not raise and must place the file.

## Test Results

```
$ .venv/bin/python -m pytest tests/test_worker_media_restore.py tests/test_worker.py -q
41 passed in 6.00s
```

All 35 tests in `test_worker_media_restore.py` pass (29 existing + 6 new).  
All 6 tests in `test_worker.py` pass.

## Drive Path Composition

For rclone backends, the path is composed as:
```
remote_name:remote_root/storage_key
```

Where:
- `remote_name` = `storage_backends.bucket` (e.g., "GDrive-willywang8216")
- `remote_root` = `storage_backends.endpoint` (e.g., "sau/inbox/generated")
- `storage_key` = `file_records.storage_key` (e.g., "campaigns/campaign-2514/watermarked_video.mp4")

Full rclone path: `GDrive-willywang8216:sau/inbox/generated/campaigns/campaign-2514/watermarked_video.mp4`

The path composition is correct. The campaign-2514 failure is a **stale mapping**: the file_records row exists but the object is NOT on Drive (`rclone lsf` returned "directory not found"). This is a data issue, not a code issue. The offload script (`offload_to_drive.sh`) may have deleted the object without updating file_records, or the object was never uploaded.

## Residual Issues

1. **campaign-2514 stale mapping**: The file_records row exists but the object is not on Drive. This is a data issue that needs manual investigation or a repair script (pf-register owns the repair script).

2. **campaign-2568 missing mapping**: The campaign_artifacts row exists but has no file_records row and no public_url/remote_path. This is a registration issue (pf-register owns the fix).

3. **campaign-2550, campaign-2543 missing mappings**: No file_records and no usable campaign_artifacts mapping. These are registration issues (pf-register owns the fix).

## Files Changed

- `myUtils/worker.py`: Added `retryable` attribute to `MediaRestoreError`, set it appropriately at each raise site.
- `tests/test_worker_media_restore.py`: Added 6 new tests covering the restore preflight and retryable distinction.

## Verification

The fix ensures:
1. **Restore runs before publisher**: `_ensure_artifact_paths_local` is called in `default_executor` before the publisher runs. If the restore fails, `MediaRestoreError` is raised and the publisher is never called.
2. **Size verification**: `_download_atomically` checks that the downloaded file is non-empty and passes `_looks_like_media` before publishing it.
3. **Retryable distinction**: `_handle_failure` reads `exc.retryable` to decide whether to retry or mark permanently failed. No-mapping failures are permanent (retrying won't help), transient download failures are retryable (the bytes exist on the backend).
4. **Clear error messages**: Each raise site has a specific error message that explains what went wrong.
