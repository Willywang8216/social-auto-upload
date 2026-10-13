# rf-reddit-cookie: Reddit cookie-uploader bug fixes

**Date:** 2025-07-09  
**Agent:** rf-reddit-cookie  
**Scope:** `uploader/reddit_uploader/**`, `tests/test_reddit_cookie_fixes.py`

---

## Summary

Fixed two bugs in the legacy cookie-based Reddit uploader that caused target failures:

1. **Target 73** – `TypeError: get_browser_options() takes 0 positional arguments but 1 was given`
2. **Target 77** – `RuntimeError: Reddit API error: Please log in to do that.` (raw, unclassified, retryable)

---

## Bug 1: `get_browser_options()` arity mismatch

### Reproduction

The historical bug (fixed in commit `8fc146e`) called `get_browser_options(p)` passing the Playwright instance as an argument, but the function signature is `def get_browser_options():` (0 params). This produced:

```
TypeError: get_browser_options() takes 0 positional arguments but 1 was given
```

### Current state

The fix is already in place: `uploader/reddit_uploader/main.py:219` calls `get_browser_options()` with 0 args, matching the definition in `utils/browser_hook.py:3`.

### Regression tests added

- `test_hook_signature_takes_no_positional_args` – pins the 0-arg contract via `inspect.signature`
- `test_hook_callable_with_zero_args` – verifies it returns a valid dict
- `test_reddit_uploader_imports_hook_version` – ensures the reddit module uses the hook version, not a local copy
- `test_calling_with_one_arg_raises_typeerror` – documents the exact error the historical bug produced

### Other callers checked

| File | Call | Status |
|------|------|--------|
| `uploader/reddit_uploader/main.py:219` | `get_browser_options()` | ✅ 0 args |
| `uploader/twitter_uploader/main.py:760` | `get_browser_options()` | ✅ 0 args |
| `myUtils/login.py:51,175` | `get_browser_options()` | ✅ 0 args (own definition) |

No other arity mismatches found.

---

## Bug 2: "Please log in" → raw RuntimeError (retryable)

### Problem

When Reddit cookies expired, the uploader raised raw `RuntimeError` messages:
- API path: `RuntimeError: Failed to submit post to r/subreddit` (after Reddit returned `USER_REQUIRED`)
- Browser path: `RuntimeError: Not logged in - re-login required`

These were classified as generic failures by the worker, which would retry them (burning the retry budget) and potentially route to fallback – all futile since the cookie is dead.

### Fix

**`uploader/reddit_uploader/proxy_manager.py`:**
- Added `RedditCredentialError` exception class
- Added `_is_credential_error_code()` – detects `USER_REQUIRED` code and "please log in" message
- Added `_looks_like_credential_error()` – detects HTTP 401/403 and "please log in" in body
- Modified `submit_post()` and `submit_self_post()` to raise `RedditCredentialError` when auth errors are detected (instead of silently returning None)

**`uploader/reddit_uploader/main.py`:**
- Imported `PreparedPublishError` from `myUtils.prepared_publishers`
- Imported `RedditCredentialError` from `proxy_manager`
- `_post_reddit_cookie()`: catches `RedditCredentialError` and raises `PreparedPublishError(retryable=False)` immediately – does NOT fall back to browser (it would also fail)
- `_post_via_browser()`: raises `PreparedPublishError(retryable=False)` when redirected to `/login` (instead of raw `RuntimeError`)

### Result

- Credential failures are now `PreparedPublishError(retryable=False)` – the worker stops after one attempt
- The error message clearly says "re-login required"
- The `_is_credential_failure()` regex in `worker.py` already matches "Please log in", so the worker's credential-failure classifier catches these
- Non-credential errors (rate limits, transient network issues) still fall back to browser as before

### Tests added

- `RedditCredentialErrorTests` (6 tests) – unit tests for the helper functions
- `ProxyManagerSubmitCredentialTests` (4 tests) – verifies `submit_post`/`submit_self_post` raise `RedditCredentialError` on auth failure, return None on non-credential errors
- `PostRedditCookieCredentialPathTests` (2 tests) – verifies credential errors don't fall back to browser, non-credential errors still do
- `BrowserLoginRedirectTests` (1 test) – verifies browser login redirect raises `PreparedPublishError(retryable=False)`

---

## Files changed

| File | Change |
|------|--------|
| `uploader/reddit_uploader/proxy_manager.py` | Added `RedditCredentialError`, `_is_credential_error_code()`, `_looks_like_credential_error()`; modified `submit_post()` and `submit_self_post()` to raise on auth failure |
| `uploader/reddit_uploader/main.py` | Import `PreparedPublishError` and `RedditCredentialError`; catch credential errors in `_post_reddit_cookie()` and raise `PreparedPublishError(retryable=False)`; raise `PreparedPublishError` in `_post_via_browser()` on login redirect |
| `tests/test_reddit_cookie_fixes.py` | **NEW** – 17 regression tests covering both bugs |

---

## Test results

```
$ .venv/bin/python -m pytest tests/test_reddit_cookie_fixes.py tests/test_prepared_publishers.py -q
163 passed, 31 subtests passed in 13.43s

$ .venv/bin/python -m pytest tests/test_reddit_uploader.py tests/test_reddit_auth.py tests/test_credential_failure.py tests/test_purge_permanent_failures.py -q
76 passed, 16 subtests passed in 1.02s
```

All existing tests continue to pass. No regressions.
