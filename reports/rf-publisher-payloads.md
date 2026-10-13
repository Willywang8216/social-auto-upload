# rf-publisher-payloads Report

**Agent:** rf-publisher-payloads  
**Date:** 2026-01-XX  
**Scope:** `myUtils/prepared_publishers.py`, `myUtils/sociamonials_fallback.py`, `tests/test_rf_publisher_payloads.py`

## Summary

Fixed 5 critical publish failures identified in the failed-target inventory. All fixes are offline (mock sessions), include comprehensive tests, and maintain backward compatibility.

## Issues Fixed

### 1. Reddit RATELIMIT Errors Marked Permanent (Targets 3865, 4527)

**Problem:** Reddit rate limit errors (RATELIMIT code) were being marked as permanent failures instead of retryable, causing the worker to give up instead of backing off.

**Root Cause:** The error handling code checked for permanent error codes but didn't explicitly handle RATELIMIT as a transient condition.

**Fix:** Added explicit RATELIMIT detection in `publish_reddit_sync()` that raises `PreparedPublishError(..., retryable=True)` with a clear message.

**Code Location:** `myUtils/prepared_publishers.py:3767-3775`

**Test:** `test_reddit_ratelimit_error_is_retryable()`

**Reproduction:**
```python
# Simulate Reddit returning RATELIMIT error
session = _RecordingSession([
    _FakeResponse({"access_token": "token"}),
    _FakeResponse({"json": {"errors": [["RATELIMIT", "You are doing this too much"]]}})
])
# Before fix: retryable=False (permanent failure)
# After fix: retryable=True (worker retries with backoff)
```

---

### 2. Reddit Empty Subreddits Array (Target 71)

**Problem:** When an account had no subreddits configured, the error message was generic and didn't name the account or explain how to fix it.

**Root Cause:** The validation raised a bare error without context about which account or how to resolve it.

**Fix:** Enhanced the error message to include the account name and explicit instructions to add subreddits to the config. Marked as `retryable=False` since configuration won't change between retries.

**Code Location:** `myUtils/prepared_publishers.py:3477-3483`

**Test:** `test_reddit_empty_subreddits_is_non_retryable_with_actionable_error()`

**Reproduction:**
```python
account = SimpleNamespace(
    account_name="brand-main",
    config={"subreddits": [], ...}
)
# Before: "Reddit publish requires a non-empty subreddits array"
# After: "Reddit account 'brand-main' publish requires a non-empty subreddits array. 
#         Fix: add subreddits to the account config..."
```

---

### 3. TikTok invalid_params: Empty Post Info (Target 3847)

**Problem:** TikTok rejected posts with "The request post info is empty or incorrect" when the payload message was empty.

**Root Cause:** The code built `post_info` with an empty `title` field when both `reviewed_title` and `message` were empty. TikTok's API requires a non-empty title.

**Fix:** Added early validation before building `post_info` that raises a clear, non-retryable error when the title would be empty. The error message explains the fix: provide a message or set `tiktokPostSettings.title`.

**Code Location:** `myUtils/prepared_publishers.py:2355-2362`

**Test:** `test_tiktok_empty_title_raises_clear_error()`

**Reproduction:**
```python
payload = {
    "message": "",  # Empty
    "artifacts": [{"public_url": "https://cdn.example/video.mp4", ...}]
}
# Before: TikTok API rejects with opaque "invalid_params" error
# After: Clear error raised before API call: "TikTok publish requires a 
#         non-empty title/description..."
```

---

### 4. Sociamonials Video URL Not Accessible (Targets 3391, 3399)

**Problem:** Sociamonials accepted posts but delivery failed with "Video URL not accessible after retries" because URLs with spaces or special characters weren't percent-encoded.

**Root Cause:** The `public_url` was passed through without normalization. URLs like `https://cdn.example/my video.mp4` (with literal spaces) were rejected by Sociamonials' fetch guard.

**Fix:** 
1. Added `_normalize_public_url()` function that percent-encodes unsafe characters
2. Applied normalization when setting `video_url` from a public URL
3. Added delivery failure detection that raises a clear error when Sociamonials reports URL accessibility issues

**Code Location:** 
- `myUtils/sociamonials_fallback.py:337-358` (normalization function)
- `myUtils/sociamonials_fallback.py:1393-1401` (applied to video_url)
- `myUtils/sociamonials_fallback.py:1507-1515` (delivery failure detection)

**Tests:** 
- `test_sociamonials_normalizes_video_url_with_spaces()`
- `test_sociamonials_preserves_already_encoded_urls()`
- `test_sociamonials_delivery_failure_on_inaccessible_url()`

**Reproduction:**
```python
url = "https://cdn.example/my video file.mp4"
# Before: URL passed as-is, Sociamonials fetch fails
# After: URL normalized to "https://cdn.example/my%20video%20file.mp4"
```

---

### 5. nw_sw_blog HTTP 409 Conflict (Target 5750)

**Problem:** GitHub API returned 409 Conflict when the file was modified between the GET (read SHA) and PUT (update), causing the publish to fail permanently.

**Root Cause:** The code didn't handle the race condition where another process (or a retry) modified the file between reading its SHA and attempting to update it.

**Fix:** Implemented idempotent conflict resolution:
1. On 409, re-read the file to get the fresh SHA
2. Compare the existing content with what we want to write
3. If content matches: mark as success (idempotent, no retry needed)
4. If content differs: retry the PUT with the fresh SHA

**Code Location:** `myUtils/prepared_publishers.py:4341-4371`

**Tests:** 
- `test_nw_sw_blog_409_conflict_retries_with_fresh_sha()`
- `test_nw_sw_blog_409_conflict_idempotent_when_content_matches()`

**Reproduction:**
```python
# Simulate 409 conflict
session = _RecordingSession([
    _FakeResponse({"sha": "old-sha"}),  # Initial GET
    _FakeResponse({}, status_code=409),  # PUT fails with conflict
    _FakeResponse({"sha": "new-sha", "content": "..."}),  # Re-GET
    _FakeResponse({"content": {"sha": "..."}}),  # Retry PUT succeeds
])
# Before: 409 propagates as permanent failure
# After: Automatically retries with fresh SHA, or succeeds idempotently
```

---

## Test Results

### New Tests (tests/test_rf_publisher_payloads.py)
```
10 passed in 0.41s
```

All 10 new tests pass, covering:
- Reddit RATELIMIT retryable behavior
- Reddit permanent errors remain non-retryable
- Reddit empty subreddits actionable error
- TikTok empty title validation
- TikTok valid title success path
- Sociamonials URL normalization (spaces)
- Sociamonials URL preservation (already encoded)
- Sociamonials delivery failure detection
- nw_sw_blog 409 retry with fresh SHA
- nw_sw_blog 409 idempotent success

### Existing Tests (Regression Check)
```
tests/test_prepared_publishers.py: 142 passed
tests/test_sociamonials_fallback.py: 61 passed
Total: 203 passed, 31 subtests passed in 17.74s
```

No regressions detected.

---

## Files Changed

1. **myUtils/prepared_publishers.py**
   - Reddit: Added RATELIMIT detection and retryable flag (lines 3767-3775)
   - Reddit: Enhanced empty subreddits error message (lines 3477-3483)
   - TikTok: Added empty title validation (lines 2355-2362)
   - nw_sw_blog: Added 409 conflict handling with retry/idempotency (lines 4341-4371)

2. **myUtils/sociamonials_fallback.py**
   - Added `_normalize_public_url()` function (lines 337-358)
   - Applied URL normalization to video_url (lines 1393-1401)
   - Added delivery failure detection for URL accessibility (lines 1507-1515)

3. **tests/test_rf_publisher_payloads.py** (NEW)
   - 10 comprehensive tests covering all 5 fixes
   - All tests use mock sessions (offline, no live posts)

---

## Verification Commands

```bash
# Run new tests
.venv/bin/python -m pytest tests/test_rf_publisher_payloads.py -v

# Run existing tests (regression check)
.venv/bin/python -m pytest tests/test_prepared_publishers.py tests/test_sociamonials_fallback.py -q

# Run all three together
.venv/bin/python -m pytest tests/test_prepared_publishers.py tests/test_sociamonials_fallback.py tests/test_rf_publisher_payloads.py -q
```

---

## Impact Analysis

### Worker Behavior Changes

1. **Reddit RATELIMIT**: Worker will now retry with exponential backoff instead of marking permanent failure. Expected to recover targets 3865, 4527.

2. **Reddit Empty Subreddits**: Worker will mark as permanent failure immediately with actionable error. Operator can fix config and requeue.

3. **TikTok Empty Title**: Worker will mark as permanent failure with clear error. Operator can provide a message and requeue. Prevents wasted API calls.

4. **Sociamonials URL Encoding**: Posts with spaces in URLs will now deliver successfully. Expected to recover targets 3391, 3399.

5. **nw_sw_blog 409**: Worker will automatically resolve conflicts. Expected to recover target 5750 and prevent future 409 failures.

### Backward Compatibility

All fixes maintain backward compatibility:
- Existing error codes and retry logic unchanged
- New error messages are more descriptive but don't break parsing
- URL normalization is transparent to callers
- 409 handling is internal to the publisher

### Performance Impact

Minimal:
- Reddit RATELIMIT: No performance impact (same code path, different flag)
- Reddit subreddits: No performance impact (validation already existed)
- TikTok title: No performance impact (validation added before API call)
- Sociamonials URL: Negligible (URL encoding is O(n) where n = URL length)
- nw_sw_blog 409: Only adds work on 409 (rare race condition)

---

## Recommendations

1. **Monitor Recovery**: After deployment, monitor targets 3847, 3391, 3399, 5750, 3865, 4527, 71 for recovery.

2. **Alert on Empty Titles**: Consider adding a pre-publish validation step that warns operators when a payload has an empty message, before it reaches the publisher.

3. **URL Validation**: Extend URL normalization to other platforms that accept public URLs (Facebook, Instagram, Threads, YouTube) to prevent similar issues.

4. **Rate Limit Telemetry**: Add metrics for Reddit RATELIMIT occurrences to identify accounts that need rate limit tuning.

---

## Conclusion

All 5 issues from the failed-target inventory have been fixed with comprehensive tests. The fixes are minimal, focused, and maintain backward compatibility. No changes were made to `worker.py`, `uploader/**`, or `campaign_media_prep.py` as specified in the scope.

**Status:** ✅ Complete, tested, ready for deployment
