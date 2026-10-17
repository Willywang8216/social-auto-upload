# X (Twitter) publishing failed again — root cause & fix

Date: 2026-10-14 · Targets: 3391, 3399, 3448 (all `twitter` / `account:123`)

## What the operator saw

X publishes that had been working started failing again. Resubmitting them kept
failing and produced **the same post id every time**:

```
post_id=10689041 … delivery failed: Video URL not accessible after retries
```

## Why it happened again (two independent bugs)

1. **Stable idempotency key returned the same dead post forever.**
   The Sociamonials fallback sent `idempotency_key = "sau-target-<id>"`. That key
   is stable across attempts, so every retry returned the *original* post. Once
   Sociamonials had permanently failed to deliver that post, it returned the
   failure for ever — the target could never recover, no matter how many times
   it was re-queued. (The 4-digit post id was identical on every attempt.)

2. **A reused library asset was delivered from a dead URL.**
   Asset reuse (`SAU_SOCIAMONIALS_REUSE_ASSETS`, default on) matched a library
   asset by filename/size. That asset had originally been imported from the
   artifact's public R2 URL. When the object later moved, Sociamonials still
   fetched the old URL at delivery time and failed with
   "Video URL not accessible". Uploading the local bytes (already restored on
   disk, ~167 MB) avoids the URL entirely.

Bug #1 is why a retry could never succeed; bug #2 is why the first submit failed.

## The fix (three commits, all deployed)

| commit | change |
| --- | --- |
| `e4641dc` | Fallback must materialise local media; a missing local file is a **retryable** `MediaRestoreError` instead of submitting an unverified public URL. |
| `48cee88` | Idempotency key is now `sau-target-<id>-a<attempt>-<claim-time>` — stable within one attempt, **new** across attempts, so a retry creates a fresh post. |
| `4a571ae` | Library-asset reuse is **opt-in** (default off). Local bytes are uploaded, guaranteeing byte-backed delivery. Set `SAU_SOCIAMONIALS_REUSE_ASSETS=1` to re-enable. |

Regression tests added for both: a per-attempt key test and a default-off reuse
test. Full suite: **1766 passed, 1 skipped**.

## Result

- 3391 → **succeeded** (post_id 10754335, fresh assets recorded)
- 3399 → **succeeded**
- 3448 → still fails, but correctly: its media is genuinely gone (a
  `localhost:5409/getFile` URL and no `file_records`), so it retries as a
  `MediaRestoreError` rather than submitting a dead URL.

## Remaining failures (21), none an X/Sociamonials bug

| platform | n | cause | action |
| --- | --- | --- | --- |
| reddit | 14 | app credentials revoked at Reddit (401 / "Please log in") | **operator**: recreate Reddit app + re-auth accounts 105 (NW), 106 (SW) |
| reddit | 2 | media gone (`/app/uploads/...` objects removed) | unrecoverable |
| threads | 2 | video 442 s / 600 s > 300 s limit | re-cut or drop |
| threads | 1 | HTTP 400 unknown error (stale payload) | re-prep |
| tiktok | 1 | `invalid_params` (stale payload) | re-prep |
| twitter | 1 | 3448, media gone | unrecoverable |
