# Media Limits Enforcement Fixes

**Date:** 2026-10-13  
**Agent:** rf-media-limits  
**Scope:** `myUtils/campaign_media_prep.py`, `myUtils/media_prep.py`, `myUtils/media_pipeline.py`

## Summary

Fixed two critical bugs where oversized videos reached publishers unsplit, causing publish failures:

1. **Threads duration cap (300s):** 600s and 442s videos reached the Threads publisher unsplit, triggering "Threads video duration exceeds the 300s limit" errors (targets #5626, #3785).
2. **Facebook size cap (4096 MB):** Oversized files triggered "413 Request Entity Too Large" errors (target #5592).

## Root Cause Analysis

### Bug 1: Threads Duration Cap Bypass

**Location:** `myUtils/campaign_media_prep.py` lines 656-680 (split logic)

**Problem:** The split logic had two failure modes that silently fell back to the original oversized file:

1. **Exception handling:** When `split_to_seconds()` raised an exception (e.g., ffmpeg failure), the exception was caught and logged as a warning, then `continue` skipped to the next plan. The original unsplit file was used anyway.

2. **Single-part result:** When `split_to_seconds()` returned only 1 part (the original file), the code checked `if len(parts) <= 1: continue` and skipped the split. The original file was used.

3. **Duration probe failure:** When the duration probe failed (returned 0), the `over_time` check became `False`, and the split was skipped entirely. The comment acknowledged this was a problem but the fix was incomplete.

**Impact:** A 600s video targeting Threads (300s cap) would:
- Create a split plan: `(300.0, 1000.0) -> {"threads"}`
- Call `split_to_seconds(publish_path, workspace, 300.0, max_bytes=1000*1_000_000)`
- If the split failed or returned 1 part, silently use the original 600s file
- The Threads publisher would reject it with "duration exceeds limit"

### Bug 2: Facebook Size Cap Bypass

**Location:** Same as Bug 1 (split logic)

**Problem:** The same exception handling issue applied to size-based splits. When a file exceeded Facebook's 4096 MB cap:
- A split plan was created based on size
- If the split failed, the original oversized file was used
- The Facebook publisher would reject it with "413 Request Entity Too Large"

## Fixes Implemented

### Fix 1: Raise on Split Failure

**File:** `myUtils/campaign_media_prep.py`  
**Lines:** 656-680

**Change:** When `split_to_seconds()` raises an exception, raise a `ValueError` instead of silently continuing. The error message includes:
- Campaign ID
- Target platforms
- Duration and size caps
- Source video duration and size
- Original exception

**Rationale:** A split failure means the video cannot be made to fit the platform cap. Silently using the oversized file guarantees a publish failure with an opaque error message. Raising immediately provides a clear, actionable error.

### Fix 2: Raise on Single-Part Result

**File:** `myUtils/campaign_media_prep.py`  
**Lines:** 656-680

**Change:** When `split_to_seconds()` returns only 1 part (the original file), raise a `ValueError` instead of silently continuing.

**Rationale:** If the split logic was triggered (video is over the cap) but produced only 1 part, the split failed to divide the video. Using the original file guarantees a publish failure.

### Fix 3: Raise on Duration Probe Failure

**File:** `myUtils/campaign_media_prep.py`  
**Lines:** 630-650

**Change:** When the duration probe fails and any target platform has a duration cap, raise a `ValueError` instead of setting `duration=0` and continuing.

**Rationale:** If we cannot measure the duration, we cannot verify the video fits the platform cap. Silently continuing with `duration=0` makes the `over_time` check `False`, skipping the split. The oversized file reaches the publisher and is rejected.

**Exception:** If no target platform has a duration cap (e.g., YouTube-only campaign), the probe failure is logged as a warning and the code continues with size-based checks only.

## Test Coverage

**File:** `tests/test_media_limits_fixes.py`

### Threads Duration Cap Tests

1. `test_split_count_for_600s_video_with_300s_cap`: Verifies `_split_count()` returns 2 for a 600s video with 300s cap.
2. `test_split_count_for_442s_video_with_300s_cap`: Verifies `_split_count()` returns 2 for a 442s video with 300s cap.
3. `test_split_to_seconds_produces_parts_under_cap`: Verifies `split_to_seconds()` produces 2 parts, each ≤300s, with mocked ffmpeg.
4. `test_duration_probe_failure_does_not_skip_split`: Verifies that when the duration probe fails but size is over the cap, the split still happens (or raises).

### Facebook Size Cap Tests

1. `test_split_count_for_oversized_file`: Verifies `_split_count()` returns 2 for a 5000 MB file with 4096 MB cap.
2. `test_split_to_seconds_splits_on_size_overflow`: Verifies `split_to_seconds()` splits based on size even when duration fits.
3. `test_platform_limits_facebook_size_cap`: Verifies Facebook's `media_max_mb` is 4096.
4. `test_platform_limits_threads_duration_cap`: Verifies Threads' `video_max_seconds` is 300.

### Integration Tests

1. `test_threads_600s_video_is_split`: End-to-end test verifying a 600s video targeting Threads is split into 2 parts, each ≤300s.
2. `test_facebook_5gb_video_is_split`: End-to-end test verifying a 5 GB video targeting Facebook is split into 2 parts, each ≤4096 MB.

## Test Results

All 69 existing tests pass:
- `tests/test_campaign_media_prep.py`: 9 tests
- `tests/test_media_prep.py`: 48 tests
- `tests/test_media_pipeline.py`: 12 tests

All 10 new tests pass:
- `tests/test_media_limits_fixes.py`: 10 tests

## Files Changed

1. **`myUtils/campaign_media_prep.py`**
   - Lines 630-650: Duration probe failure handling
   - Lines 656-680: Split failure handling

2. **`tests/test_media_limits_fixes.py`** (new)
   - 10 tests covering Threads duration cap and Facebook size cap enforcement

## Backward Compatibility

The fixes are backward compatible:
- Campaigns with videos that fit all platform caps are unaffected.
- Campaigns with oversized videos now fail immediately with a clear error message instead of silently publishing and failing later.
- The error messages include all relevant context (campaign ID, platforms, caps, source video specs) for debugging.

## Future Improvements

1. **Retry logic:** When the split fails due to a transient error (e.g., disk full), retry with exponential backoff before giving up.
2. **Alternative split strategies:** When equal-part split fails, try variable-length splits (e.g., split at scene changes).
3. **Pre-flight validation:** Before queuing a campaign, validate that all source videos can be split to fit the target platform caps. Fail early with a clear message instead of failing during publish.
4. **Progressive encoding:** For very long videos, use a lower CRF (higher quality) for the first split and progressively lower CRF for subsequent splits to balance quality and file size.

## References

- Platform limits: `myUtils/platform_limits.py` (single source of truth)
- Platform limits documentation: `docs/platform-posting-limits.md`
- Platform limits research: `logs/platform-limits-research.md`
- Related bugs: Targets #5626, #3785 (Threads duration), #5592 (Facebook size)
