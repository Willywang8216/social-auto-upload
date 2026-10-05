# Threads publish failure — job 4517 / target 4517 / account 62 (NWthreads)

Author: threads-fix agent
Date: 2026-10-05
Files changed:
- `myUtils/prepared_publishers.py`
- `tests/test_prepared_publishers.py`

## Symptom

Every attempt failed with an opaque Meta container error:

```
PreparedPublishError: Threads container 17947184544345874 failed to process: ERROR
```

(3 attempts, then permanent failure.) Instagram published the *same* artifact,
so Meta's media pipeline accepts the file in general.

## Investigation

### 1. Meta was returning the real error but we never asked for it

`_wait_for_container_status` polled the container with
`params={"fields": field}` where `field` was `status` (Threads) or
`status_code` (Instagram). **Meta only returns a field when it is explicitly
requested.** So `error_message` was never in the response and the helper fell
back to the bare status enum, producing the useless `... failed to process: ERROR`
(and even an empty tail for Instagram: `... failed to process:`).

A live read-only replay against the real containers confirmed the field exists
and Meta's value:

```
GET graph.threads.net/v1.0/17947184649345874?fields=status,error_message
-> {"id":"17947184649345874","status":"ERROR","error_message":"UNKNOWN"}
```

So even after requesting it, Meta reports `UNKNOWN` for this failure — the
diagnostic had to come from the media itself.

### 2. The actual root cause: the media is far over Threads' video limit

The task described the artifact as "re-encoded 1080x1920 h264 24fps, 198 s,
~47 MB". The bytes that were actually published are not that:

- Local artifact: `videoFile/SFW Taipei Stonewall.mp4` (also `/tmp/...`)
- R2 object: `https://pub-9915b1494003455c9ab872fd7094e64e.r2.dev/campaigns/2492/videos/SFW%20Taipei%20Stonewall.mp4`
- `Content-Length: 602285541` (~602 MB), `Last-Modified: 2026-10-05 05:43:04`

`ffprobe` on the published bytes:

```
codec_name=hevc  width=1080 height=1906 pix_fmt=yuv420p r_frame_rate=24/1
duration=813.256009 s   size=602285541 B   bit_rate=5924683
```

- Threads video posts are capped at **5 minutes (300 s)**. 813 s is ~2.7x the limit.
- Instagram Reels allow up to **15 minutes (900 s)**, so the same 813 s HEVC file
  is accepted there — exactly the divergence observed.

The "re-encoded 198 s / 47 MB" file was never the one referenced by the job
payload / uploaded to R2; the campaign artifact points at the original long
cut. That is a data/prep bug outside `prepared_publishers.py` (re-encode was not
wired to the published artifact), and the fix here makes the publisher refuse
it locally with an actionable message instead of burning 3 opaque retries.

Frame-rate/size were *not* the cause: 24 fps is within 24–60 fps, and 602 MB is
under the 1 GB cap.

## Fix

`myUtils/prepared_publishers.py`

1. **Surface Meta's real error detail.** `_container_status_field()` now also
   returns the fields to request (`status,error_message` for Threads;
   `status_code,status` for Instagram) and `_wait_for_container_status` sends
   them. The error path prefers `error_message`, then Instagram's human-readable
   `status`, ignores the bare status enum, and when Meta gives nothing
   (`""`/`UNKNOWN`) says so explicitly and points at the media duration/size
   limits.

2. **Fail fast on Threads video limits.** New
   `_validate_threads_video_artifact()` (called before container creation for a
   Threads video) enforces, when a local path is visible:
   - allowed container `.mp4/.mov/.m4v`
   - `THREADS_MAX_VIDEO_BYTES` = 1 GB
   - `THREADS_MAX_VIDEO_SECONDS` = 300 s via `media_pipeline.probe_video_duration`

   Probing is best-effort: remote-only artifacts or an ffprobe failure are
   skipped so a publish Meta might accept is never blocked.

   For job 4517 this now raises locally:
   `Threads video duration 813s exceeds the 300s limit; re-encode a shorter cut
   before publishing (Instagram Reels allow up to 15 minutes, ...)`.

## Regression tests

Added to `tests/test_prepared_publishers.py`:
- `test_threads_container_error_message_is_surfaced` — asserts the status poll
  requests `error_message` and the real detail reaches the error.
- `test_threads_container_unknown_error_is_actionable` — `error_message:
  UNKNOWN` becomes an actionable "no diagnostic detail ... duration/size limits".
- `test_threads_video_over_duration_limit_fails_before_container` — 813 s fails
  with a clear message and **zero** API calls.
- `test_threads_video_over_size_limit_fails_before_container` — >1 GB fails
  before any container is created.

## Verification

```
.venv/bin/python -m pytest tests/ -q -k 'threads or publish or prepared'
259 passed, 944 deselected, 12 subtests passed
```

## Operator action

Re-run job 4517 only after re-encoding the Stonewall cut to <= 5 minutes
(<= 300 s) and uploading *that* file to the R2 key referenced by the artifact.
Until then, Threads will keep rejecting the current 813 s / 602 MB object. No
code change can make Threads accept a 13.5-minute video.
