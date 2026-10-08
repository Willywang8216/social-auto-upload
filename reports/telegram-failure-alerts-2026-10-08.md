# Telegram failure alerts: what they were, and the two fixes

Investigated 2026-10-08 after the operator reported a run of failure notices.

## What the notices actually were

Two things, and neither was a new publish failure:

1. **The alerts themselves.** Every `publish-failure alert` line in
   `logs/worker.log` today is a **test fixture writing into the production log**:
   `job=1 target=1` / `RuntimeError('alert transport down')` comes from
   `tests/test_worker_publish_alerts.py`. The last *real* alert attempt was
   2026-10-07 21:15 (`instagram target #43`), and that one failed to deliver
   because of a transient connection reset. Verified live that the channel works:
   a probe alert returned `delivered: True`.
2. **The real symptom behind them: a retry loop.** The 09:00 digest and the
   alerts coincided with a batch runner on the operator's Windows box
   (`WindowsPowerShell`, seen in the nginx access log) calling
   `POST /publish-center/submit` **every ~2 minutes**, every call returning
   **499** (client gave up). The server kept working after each 499, so the
   retries produced **72 campaigns from 59 media groups** and cancelled 27
   targets mid-flight.

## Why the 499s happened (the root cause)

`publish_orchestrator.submit_publish` ran the ~900 s media prep **inside the HTTP
request**. Measured on the live box: a submit that should have failed instantly
still hung for the **full 120 s** client timeout. So any real submit overruns the
client's patience -> 499 -> the runner retries -> duplicate work.

This is the same root cause the earlier audit identified; the async prep work was
already built but had been left **OFF** (`SAU_ASYNC_PREP` unset).

## Fix 1 — enable async prep (done)

`SAU_ASYNC_PREP=1` in `.env`, container recreated. Measured effect on the identical
request:

| | sync (before) | async (after) |
|---|---|---|
| submit response | **120 s+ (timed out)** | **0.055 s** |
| response body | (never arrived) | `{"code":200,"data":{"campaignIds":[2577],"jobs":[],"status":"preparing"}}` |

Verified end-to-end: campaign 2577 went `preparing -> publishing` and enqueued
real jobs (5657-5662) with prep running off-request in the worker. A legacy
campaign with no persisted prep payload (2576) correctly landed in
`needs_review` rather than hanging, and 0 campaigns remain stuck in `preparing`.

**This removes the 499 -> retry loop entirely**: the runner now gets an instant
200 and has nothing to retry.

## Fix 2 — the duplicate guard has a blind spot (to fix)

The cross-job duplicate guard added earlier keys on **`media_group_id`**, but each
retry creates a **NEW media group**. So a resubmit of the same media under a new
submit never matched and queued again:

```
file_record_id 1142 -> 6 distinct media groups
file_record_id 1143 -> 6 distinct media groups
103 duplicate pending jobs for the same media+platform
```

Identity must be the **underlying file** (`media_group_items.file_record_id`), not
the media group. Fix: have `_already_queued_for_media()` also match on the file
record ids of the media group being submitted.

Note this also explains the earlier account-127 backlog: the same mechanism
produced 662 targets for 356 files.
