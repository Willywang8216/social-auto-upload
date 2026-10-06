# Error triage + recovery (2026-10-06, session x-credits)

## The errors you saw, resolved

| Report | What it actually was | Fix |
| --- | --- | --- |
| `RuntimeError: bluesky said no` | **Test fixture**, not production. `tests/test_worker_sociamonials_fallback.py` raises it and the worker's per-job logger wrote to the real `logs/jobs/job-1.log`. | `myUtils/job_logging.py` now honours `SAU_JOB_LOG_DIR`; `tests/conftest.py` points it at a temp dir; the 1.3 MB phantom `logs/jobs/job-1.log` is deleted. |
| `fb-blusky` | Test fixture (`account_name=fb-blusky` in the same test). | Same fix. |
| `Sociamonials … delivery failed: Video URL not accessible after retries` | Test fixture string in `test_worker_sociamonials_fallback.py` (post_id 10689041, account:1). No real target ever produced it. | Same fix. |
| `kaboom`, `account:999999` | Test fixtures. | Same fix. |
| `[content-guard] placeholder/generic copy` | **Real.** Two captions were the media-group name / a generic LLM line. | Regenerated real copy: target 2521 (`I used to treat my desires like a problem to solve…`) and 3387 (`Coffee on the back step this morning…`). |
| `RuntimeError` on Reddit (`name 'log' is not defined`) | Stale July failure; `log` is defined in the current uploader. | Verified fixed in source. |
| Douyin | **No Douyin jobs or accounts exist anywhere** (0 rows). The Publish Center is account-driven, so Douyin is never targeted. | Nothing to disable; TikTok is the short-video path. |

## Sociamonials is now the universal backup

Every failed retry (or permanent failure) on a mapped platform goes to
Sociamonials for the **same account / same platform / same metadata**:

- Wired the **separate teaching connector** (workspace 34293, its own agent
  key) for accounts 43/58/61/100/107/108/125 — previously every teaching post
  failed outright once the direct path broke.
- Mappings now carry `workspace_id`/`api_key_env`; `resolve_credentials` picks
  the right workspace per account. 23 accounts are mapped.
- Verified live: X account 77 redelivered as Sociamonials post 10695572.

Unmapped (no Sociamonials connection exists): Reddit, Telegram, blogs. Their
posts cannot fall back; they stay failed.

## Long videos are split, not truncated

`media_prep.split_to_seconds()` divides a too-long video into equal parts that
each fit the cap (a 13-minute cut becomes three ≤300 s Threads posts); the
pipeline fans them out as separate posts (`partIndex`/`partCount`). No footage
is dropped.

## Recovery + schedule

- Rewrote **84 mismatched captions** into the account language (en ↔ zh-Hant).
- Cancelled **7 canary/test placeholders** (`TEST`, `Canary re-test`,
  `publish-center-…`).
- Rescheduled **76 fallback-eligible failures** (≥24 h out, ≤3/account/day,
  ≥30 min apart), then ran `optimize_schedule.py --apply` (1350 moves into the
  researched engagement windows).
- Verified: 0 gaps under 30 min, 0 days over 3 posts/account.

## Remaining failures (honest)

**102 failed**: 76 have genuinely missing media (`MediaRestoreError`, the
generated artifact is gone), and ~12 are platform/auth issues on accounts that
do have a fallback mapping, so a retry will hand them to Sociamonials. The rest
are deterministic refusals or deleted-account references.

Counts: pending 3758 · succeeded 945 · cancelled 357 · failed 102.
