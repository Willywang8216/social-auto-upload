# 504 on large Publish Center submits — timeout fix + async design verdict

Date: 2026-10-08
Scope allowed: `docker-compose.yml`, `sau_backend.py`. Files NOT edited:
`myUtils/media_prep.py`, `myUtils/publish_orchestrator.py` (held).

## 1. What changed (the one-line, zero-risk part)

`docker-compose.yml` — the `social-auto-upload` service now overrides the
image's baked-in gunicorn CMD with a `command:` whose `--timeout` is `1800`
(everything else identical):

```
command:
  - gunicorn
  - wsgi:app
  - --workers
  - "1"
  - --threads
  - "8"
  - --timeout
  - "1800"
  - --bind
  - 0.0.0.0:5409
```

Why compose and not the Dockerfile: the service uses
`image: ghcr.io/willywang8216/social-auto-upload:latest` with
`pull_policy: always`, so the runtime CMD comes from the pulled image. The
Dockerfile (`Dockerfile:70`) still bakes `--timeout 120`; a normal deploy never
rebuilds it, so a compose `command:` override is the correct knob for the
running/next-deployed container. (`Dockerfile:70` should be bumped to 1800 too
in a follow-up so the image default matches, but it is outside this task's
allowed edit set.)

Verified:
- YAML parses: `command == ['gunicorn','wsgi:app','--workers','1','--threads','8','--timeout','1800','--bind','0.0.0.0:5409']`.
- The running container still reports
  `["gunicorn","wsgi:app","--workers","1","--threads","8","--timeout","120","--bind","0.0.0.0:5409"]`
  and was **not restarted** (as instructed), so the new value applies on the
  next `docker compose up -d` / recreate.

## 2. Important: the 504 is NOT gunicorn — raising `--timeout` alone does not fix it

Two independent facts make the gunicorn timeout a red herring for this failure:

**(a) `--threads 8` selects the gthread worker, and gthread does not enforce
`--timeout` on in-flight requests.** Gunicorn's arbiter only murders a worker
whose heartbeat (`WorkerTmp.last_update`) goes stale (`gunicorn/arbiter.py`
`murder_workers`). The gthread main loop calls `self.notify()` at the top of
every iteration (`gunicorn/workers/gthread.py:380`) and dispatches requests to a
thread pool, so it keeps beating every ~1 s even while a request thread is inside
a 900 s ffmpeg encode. The request is therefore never killed at 120 s by
gunicorn. (`worker_class` auto-resolves `sync`+`threads>1` → `gthread`,
`gunicorn/config.py:107`.)

**(b) The container sits behind OpenResty, and OpenResty emits the 504.** Live
evidence in `/opt/1panel/www/sites/socialupload.iamwillywang.com/log/error.log`:

```
2026/10/08 16:18:57 [error] ... upstream timed out (110: Connection timed out)
  while reading response header from upstream, client: 172.71.155.67,
  server: socialupload.iamwillywang.com, request: "POST /publish-center/submit
  HTTP/2.0", upstream: "http://172.18.0.36:5409/publish-center/submit"
```

and the matching access-log lines return `504` for
`POST /publish-center/submit` (and occasionally `GET /healthz`, i.e. the 8
threads can be fully consumed). The 504 cadence (~65–75 s between retries) is
consistent with nginx's default `proxy_read_timeout 60s`, not gunicorn's 120 s.
The three app vhosts (`socialupload.iamwillywang.com.conf`,
`up.iamwillywang.com.conf`, `socialupload.willywangdata.com.conf`) set
`proxy_pass`/`client_max_body_size` but **no** `proxy_read_timeout`, so nginx's
60 s default applies.

**(c) Cloudflare is in front of the origin.** The immediate clients in the log
are Cloudflare edge IPs (`172.71.x`, `104.22.x`) with the operator's real IP in
the final field. Cloudflare's proxy read timeout to origin is ~100 s (Error 524),
so even if gunicorn and nginx timeouts were raised past the worst transcode,
a >100 s synchronous submit would just flip from `504` to Cloudflare `524`. The
frontend XHR budget (`PublishCenter.vue` sets `xhr.timeout = 300000`/`600000`)
is irrelevant once an edge in the chain gives up.

**Conclusion for section 2:** the `--timeout 1800` change is harmless and
correct-in-spirit, but it will not, by itself, stop the operator's 504. The
robust fix has to remove the long-held HTTP request — i.e. the async design.

Minimum vhost-side stopgap if the sync design is kept (NOT applied; host config,
outside repo): add `proxy_read_timeout 1800s; proxy_send_timeout 1800s;` to the
three app server blocks, and be aware Cloudflare still caps at ~100 s unless the
domain is on Enterprise/Argo or the API host is proxied around Cloudflare.

## 3. Async option — design verdict: NOT small, hand back

### 3.1 Where the slow work lives today

`publish_center_submit` (`sau_backend.py:7686`) parses the request and calls
`publish_orchestrator.submit_publish(...)`; the orchestrator (held) does
everything synchronously:

1. `ensure_file_record_for_path` per media path + `create_media_group` /
   `add_media_group_item` (lines ~291–316).
2. Per profile: `get_profile`, `_resolve_accounts`,
   `content_rating.restrict_accounts`, platform-compatibility filtering
   (lines ~332–365).
3. `campaign_store.create_campaign(status=CAMPAIGN_PREPARING)` (line ~380).
4. `prepare_artifacts(...)` at line ~400 = `sau_backend._prepare_campaign_media_artifacts`
   (`sau_backend.py:3571`), the ~900 s CPU-bound step: intro/outro concat,
   `_shrink_for_publish` (ffmpeg under `media_prep.encode_slot()`), watermark,
   screenshots, remote upload, split-to-caps, and all `campaign_artifacts`
   DB writes.
5. Per account: `generate_account_draft(..., media_context)` → `add_campaign_post`
   → `_enqueue_post` creates `publish_jobs` + `publish_job_targets`
   (lines ~455–595) — **draft generation needs `media_context`, which only
   exists after step 4**.
6. `update_campaign(status=PUBLISHING/NEEDS_REVIEW, prepared_at=...)`.

Back in `publish_center_submit`, `result.jobs` drives
`_start_worker_drain_thread()`, `_notify_tg_review(jobs=...)`, and the JSON body.

### 3.2 Functions that would move

- **`myUtils/publish_orchestrator.submit_publish` must be split** (in a held
  file) into a fast `submit_publish` (steps 1–3, return campaign ids + a
  persisted payload) and a slow `finalize_campaign_prep` (steps 4–6, run
  off-request).
- **`sau_backend._prepare_campaign_media_artifacts`** would be invoked by the
  worker instead of the request. It currently reads Flask `request.host_url`
  (via `from flask import request` inside the function) to mint local
  `/getFile` URLs; with no request context that raises `RuntimeError` and the
  URL is silently dropped (the raw-URL branch then hits `NameError` and is
  swallowed). It must be refactored to take an explicit base URL from config
  (`ops_alerts.public_app_origin()`).
- **`myUtils/worker.py`** currently only claims `publish_job_targets`
  (`_tick` → `jobs.claim_next_targets`). It would need a new prep scan/lease (or
  a new queue table/row type) to find `preparing` campaigns and run
  prep + the per-account draft/enqueue loop.
- **The whole per-account draft + `_enqueue_post` block** (orchestrator lines
  ~455–595) moves to the worker, since drafts cannot be produced before
  artifacts exist. That drags `_resolve_accounts`, compatibility filtering,
  duplicate-guard (`_already_queued_for_media`), and `_next_free_slot`
  staggering along with it.
- **A new persisted submit payload** is required (options, schedule,
  account_drafts, tiktok_post_settings, brief, selected_account_ids). Today
  `campaigns.metadata` only stores title/notes/requestedPlatforms, and the
  request-scoped `request_data`/`options` are lost when the request ends. This
  needs a schema change under `migrations/versions/` — outside the allowed set.

### 3.3 What the caller would receive instead of the prepared artifacts

Today: `{code:200, data:{campaignIds, jobs:[payloads with generated draft copy +
artifact URLs], skipped}}`.

Async: `{code:200, data:{campaignIds, status:"preparing", jobs:[], skipped:[]}}`
(or a `prepJobId`). Consequences:

- **No drafts and no artifacts at submit time** — `_generate_account_draft`
  cannot run without `media_context`, so the per-account copy the UI/Telegram
  review shows does not exist yet.
- **Telegram review card (`_notify_tg_review`) cannot fire at submit**; it would
  have to be emitted by the worker after prep, changing notification timing and
  requiring the worker to own `sau_backend`'s notify helper (or a shared
  module).
- **The frontend needs a new readiness contract** (poll/SSE on campaign status
  `preparing → publishing`), plus a way to surface prep failures that today come
  back as a synchronous HTTP 400. The Publish Center currently assumes `jobs`
  are present in the submit response.

### 3.4 What would break

1. `/publish-center/submit` contract and every consumer of `result.jobs`.
2. `/inbox/.../publish` (`sau_backend.py` ~11257) stages remote media to a
   local path and **deletes it if `result.jobs` is empty**
   (`if not result.jobs: ...unlink`). Under async, `jobs` is empty while the
   campaign is still `preparing`, so the staged source would be deleted before
   prep runs — a data-loss bug. Cleanup would have to key off prep completion.
3. Existing tests encode the synchronous contract:
   `tests/test_campaigns_http.py::test_campaign_prepare_builds_drafts_per_account_language`
   patches `_prepare_campaign_media_artifacts`/`_generate_account_draft` and
   asserts they ran during the request; `test_campaign_prepare_missing_expected_media_context_is_not_an_error`
   asserts a 200 from the sync path. These would need rewriting.
4. Worker→app import boundary: the worker would have to call
   `_prepare_campaign_media_artifacts` / `_generate_account_draft`, which live in
   `sau_backend.py` (Flask app). Prep logic should first be extracted into a
   Flask-free `myUtils/campaign_prep.py`.
5. Crash recovery: `preparing` is already a terminal-looking state that nothing
   resumes. Async makes stuck-`preparing` the normal failure mode unless a
   lease/retry sweep is added (the existing `jobs.requeue_stale_running` only
   covers `publish_job_targets`).
6. Scheduling/dup-guard semantics shift: slot staggering and the
   already-queued guard currently run at submit; moving them later changes
   `schedule_at` ordering.

### 3.5 Verdict

**Not genuinely small — do not implement blind; handing the design back.** It is
a multi-file refactor spanning a held module
(`myUtils/publish_orchestrator.py`), the worker, the API contract, the DB schema
(new migration), and two notification/frontend flows. Suggested order when
picked up: (1) extract prep + draft/enqueue into a Flask-free
`myUtils/campaign_prep.py`; (2) persist the submit payload and add a prep lease
state; (3) teach the worker to scan/claim `preparing` campaigns; (4) switch both
routes to return `preparing` and move the Telegram card to the worker;
(5) add frontend polling and prep-failure surfacing.

## 4. Test result

`.venv/bin/python -m pytest tests/ -q` → **1318 passed, 1 skipped, 92 subtests
passed** in 205.67s (exit 0, no failures). The task's stated baseline was 1306
passed / 1 skipped; my change (`docker-compose.yml` only) cannot affect pytest,
so the +12 is environmental (repo/test state observed at run time), not caused
by this work. The suite is green either way, which is the required invariant.
`docker-compose.yml` is not exercised by the tests.
