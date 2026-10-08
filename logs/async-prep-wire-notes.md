# Async-prep wiring notes

Scope: make the Publish Center HTTP submit actually *use* the async prep queue
built in `myUtils/campaigns.py` / `myUtils/worker.py` / `myUtils/campaign_prep.py`
/ `myUtils/campaign_media_prep.py`. Before this change `POST /publish-center/submit`
still ran the full `publish_orchestrator.submit_publish` synchronously, so a
~900 s ffmpeg prep still died at Cloudflare's ~100 s origin cap. This session
wires the switch, fixes the `result.jobs` consumers the safety notes flagged,
and adds the frontend/MCP parity.

Author: session working in `/home/will/social-auto-upload`.
Constraint honoured: **no edit to `myUtils/publish_orchestrator.py`** (held
file). The equivalent orchestrator-side diff is recorded in §5.

---

## 1. The switch — `SAU_ASYNC_PREP` (default OFF)

`sau_backend._async_prep_enabled()` reads `SAU_ASYNC_PREP` and is truthy only
for `1`/`true`/`yes`/`on` (case-insensitive). Unset or anything else is OFF.

* **OFF (default):** both routes take the original `publish_orchestrator.submit_publish`
  call, unchanged argument-for-argument. The response has **no** `status` key,
  so the JSON is byte-for-byte what it was. This is the critical requirement
  and it is pinned by `test_async_prep_off_keeps_the_synchronous_submit`.
* **ON:** both routes call the new `campaign_prep.submit_publish_async(...)`,
  which materialises the campaign(s) in `preparing` and returns immediately.

The switch is read once per request (`async_prep = _async_prep_enabled()`), so
both the submit call and the response shape branch on the same value.

---

## 2. What changed on each path

### `myUtils/campaign_prep.py` — new `submit_publish_async(...)`

The async twin of `publish_orchestrator.submit_publish`, but only the
*pre-campaign* half:

1. register file records (`ensure_file_record_for_path` callback);
2. create the one shared media group (same `primary_video_file_id` logic);
3. compute the filename-derived content rating;
4. per profile: resolve enabled accounts, drop NSFW-incompatible accounts
   (`content_rating.restrict_accounts`), drop YouTube-without-video and
   TikTok-photo-without-Direct-Post, then create **one campaign per profile**
   with `status=CAMPAIGN_PREPARING`, the surviving `selected_account_ids`, and
   metadata carrying `PREP_REQUEST_KEY = {brief, options, schedule,
   accountDrafts, tiktokPostSettings, mediaFilePaths}`;
5. return `publish_orchestrator.SubmitResult(campaign_ids=..., jobs=[],
   skipped=...)`.

It does **not** run `prepare_artifacts`, draft generation, or job enqueue. The
worker's `_prep_tick()` claims the `preparing` campaign and `run_campaign_prep()`
reads `PREP_REQUEST_KEY` back, runs the heavy work, then `finalize_campaign()`
generates drafts and enqueues jobs. `_base_time()` reads the persisted
`schedule`, so a deferred submit keeps its base time.

The pre-campaign filtering is intentionally mirrored from the orchestrator. The
exact orchestrator diff that makes this shared instead of mirrored is in §5.

### `sau_backend.py` — `publish_center_submit`

```python
async_prep = _async_prep_enabled()
...
if async_prep:
    result = campaign_prep.submit_publish_async(
        profile_ids=..., selected_account_ids=..., media_file_paths=...,
        brief=..., options=..., schedule=..., account_drafts=...,
        tiktok_post_settings=..., db_path=db_path,
        ensure_file_record_for_path=_ensure_file_record_for_path,
    )
else:
    result = publish_orchestrator.submit_publish(   # unchanged
        ...,
    )
```

Response (ON only adds `status`):

```json
{"code":200,"msg":"queued",
 "data":{"campaignIds":[...],"status":"preparing","jobs":[],"skipped":[...]}}
```

### `sau_backend.py` — `inbox_item_publish`

Same branch. The already-merged staged-file cleanup (keyed on
`result.campaign_ids`, not `result.jobs`) is left exactly as-is, so the staged
Drive source survives an async submit. `source="inbox"` is recorded in metadata.

### Drain trigger (both routes) — the regression fix

Was:

```python
if result.jobs:
    _start_worker_drain_thread()
```

Now:

```python
if result.jobs or result.campaign_ids:
    _start_worker_drain_thread()
```

Without this, async submit returns `jobs=[]` and the in-process worker is never
kicked, so `_prep_tick()` never claims the campaign and prep never starts.

### Telegram review cards — decision (see §3)

Was called unconditionally with `result.jobs or []`; now only when
`result.jobs` is non-empty at submit. Under async the cards are emitted by the
worker after prep finalises.

### `mcp_server/tools/publish.py` — parity

`publish_submit` now also branches on `_async_prep_enabled()` and calls
`campaign_prep.submit_publish_async` when ON. Its result gains a `status` field:

* async ON → `"preparing"` (with `jobs: []`);
* sync → `"queued"` when jobs were enqueued, `"needs_review"` when none were.

So an MCP client no longer has to guess from `campaignIds` alone.

### `sau_frontend/src/views/PublishCenter.vue` — see §4.

---

## 3. Telegram decision

**Choice: skip the card build at submit under async; have the worker emit the
cards after prep finalises.**

Why:

* `_notify_tg_review` builds one card per **job** (`_tg_review_cards` iterates
  `jobs`). Under async there are no job rows at submit time, so calling it there
  can only ever produce zero cards — the operator would never get the
  approve/edit gate.
* The card content is read *back from the DB* (job row → payload → draft,
  artifacts, targets). That data does not exist until `finalize_campaign` has
  run. Building from the campaign instead would duplicate the "what copy/media
  will actually be sent" derivation and could show something the jobs never
  contain.
* The worker already has the real `{"jobs": [...]}` result immediately after
  `run_campaign_prep`. `campaign_prep.make_default_prep_runner()._runner` now
  calls `sau_backend._notify_tg_review(jobs=result["jobs"], db_path=db_path)`
  right after prep (fail-soft: any exception is logged, never fails the prep).
* The synchronous path is unchanged: it still notifies at submit. Because the
  call is now guarded by `if result.jobs:`, a synchronous submit that produces
  no jobs behaves exactly as before (empty cards were already a no-op). There
  is no double-notify: async never notifies at submit, sync never reaches the
  prep runner (its campaigns are already `publishing`/`needs_review`).

The lazy import means the standalone worker pays for `sau_backend` only when it
actually prepares a campaign — the same trade-off `make_default_prep_runner`
already made.

---

## 4. Frontend change (`sau_frontend/src/views/PublishCenter.vue`)

The submit handler used `data.jobs?.length ? 'success' : 'warning'`, so an async
submit (`jobs: []`, `campaignIds: [...]`, `status: 'preparing'`) rendered the
warning **"送出成功，但未產生任何工作"** — the exact "nothing happened" misread the
pinned contract forbids.

Now:

```js
const isPreparing = data.status === 'preparing'
  || (!data.jobs?.length && (data.campaignIds?.length || 0) > 0)
submitResult.value = {
  type: data.jobs?.length ? 'success' : (isPreparing ? 'info' : 'warning'),
  message: data.jobs?.length
    ? `已排入 ${data.jobs.length} 個發佈工作`
    : (isPreparing
        ? '已排入準備佇列，媒體準備完成後會自動建立發佈工作'
        : '送出成功，但未產生任何工作'),
  jobs: data.jobs,
  campaignIds: data.campaignIds,
  status: data.status,
  skipped: data.skipped,
}
```

`campaignIds` / `status` are stored on `submitResult` for the follow-up poller.

**Not done (documented follow-up):** the notes file also flagged that
`if (data.jobs?.some(j => j.platform === 'tiktok')) startStatusPolling()` never
fires under async (no jobs yet) and that `pollTiktokStatuses` returns early on
an empty `submitResult.jobs`. Properly fixing it needs a campaign-status API the
frontend can poll until `status` leaves `preparing` and jobs appear; that is a
separate endpoint/API change and is deliberately left out of this wiring change
so the display fix ships without a new API dependency. The campaign read view
(`/publish-center/entities`) already reports `preparing` for those campaigns, so
the operator sees them there in the meantime.

---

## 5. Proposed `publish_orchestrator.py` diff (NOT applied — held file)

Today `submit_publish_async` mirrors the orchestrator's pre-campaign setup. The
drift-free version is one `async_prep` flag on `submit_publish`; the backend and
MCP tool would pass `async_prep=True` and could drop `submit_publish_async`
entirely. Exact diff:

```diff
--- a/myUtils/publish_orchestrator.py
+++ b/myUtils/publish_orchestrator.py
@@
     artifact_part_groups_for_platform: Callable | None = None,
     job_to_payload: Callable,
     now_fn: Callable = datetime.now,
+    async_prep: bool = False,
 ) -> SubmitResult:
@@
         campaign = campaign_store.create_campaign(
             profile.id,
             media_group.id,
             status=campaign_store.CAMPAIGN_PREPARING,
             selected_account_ids=[account.id for account in accounts],
             metadata={
                 "title": request_data.get("title", ""),
                 "notes": request_data.get("notes", ""),
                 "requestedPlatforms": sorted({account.platform for account in accounts}),
                 "source": "publish-center",
+                # Async prep: persist the raw submit payload so the worker's
+                # run_campaign_prep can rebuild request_data/options off-request.
+                **(
+                    {
+                        campaign_store.PREP_REQUEST_KEY: {
+                            "brief": brief,
+                            "options": options or {},
+                            "schedule": schedule,
+                            "accountDrafts": account_drafts,
+                            "tiktokPostSettings": tiktok_post_settings,
+                            "mediaFilePaths": [str(p) for p in media_file_paths],
+                        }
+                    }
+                    if async_prep
+                    else {}
+                ),
             },
             db_path=db_path,
         )
         campaign_ids.append(campaign.id)
 
+        # Async prep: the request ends here; myUtils.worker runs the artifact
+        # pipeline + finalize_campaign from the persisted prep request.
+        if async_prep:
+            continue
+
         media_files = _load_media_group_files_via_callback(
             media_group.id, db_path=db_path
         )
```

The `continue` sits immediately after `campaign_ids.append(campaign.id)` and
before `media_files = _load_media_group_files_via_callback(...)`. Everything
below it (artifact prep, the platform/account draft loop, and the final
`update_campaign(...)`) is skipped; `campaign_prep.run_campaign_prep` +
`finalize_campaign` reproduce that block exactly, with the campaign status set
by the worker's lease-guarded `finish_campaign_prep`. Callers then pass
`async_prep=_async_prep_enabled()` and delete `submit_publish_async`.

(The separate drift the `campaign_prep` docstring notes — `finalize_campaign`
mirroring the orchestrator's post-prep loop — is unaffected by this diff.)

---

## 6. Tests

Added (all green):

* `tests/test_publish_center.py`
  * `PublishCenterSubmitTests.test_async_prep_off_keeps_the_synchronous_submit`
    — switch OFF runs `submit_publish`, returns jobs, **no `status` key**, and
    never calls `submit_publish_async`.
  * `PublishCenterSubmitTests.test_async_prep_on_returns_preparing_without_running_prep`
    — switch ON returns `status:"preparing"`, `jobs:[]`, a created campaign row
    in `CAMPAIGN_PREPARING` with a populated `PREP_REQUEST_KEY`; asserts
    `_prepare_campaign_media_artifacts` and `_generate_account_draft` are
    **not** called in the request and `_notify_tg_review` is not called.
  * `PublishCenterSubmitTests.test_async_prep_on_starts_drain_with_empty_jobs`
    — regression for the `if result.jobs` drain guard.
  * `AsyncPrepSwitchTests.test_switch_parses_truthy_values_only` /
    `test_switch_defaults_off_when_unset`.
* `tests/test_inbox_both.py`
  * `test_publish_endpoint_starts_drain_when_campaign_has_no_jobs` — the inbox
    drain regression.
  * `test_publish_endpoint_async_prep_returns_preparing` — inbox switch ON uses
    `submit_publish_async`, returns `status:"preparing"`, and never calls the
    synchronous `submit_publish`.
* `tests/test_mcp_server.py`
  * `test_publish_submit_status_is_preparing_under_async`
  * `test_publish_submit_status_is_queued_on_sync_path`

Run: `.venv/bin/python -m pytest tests/ -q`

```
1353 passed, 1 skipped, 105 subtests passed in 269.24s
```

Baseline quoted in the task: `1330 passed, 1 skipped`. The delta is this
session's 9 tests plus the already-present untracked async-prep queue/extraction
tests (`test_campaign_prep_queue.py`, `test_campaign_media_prep.py`). No tests
removed, none skipped beyond the pre-existing one.

---

## 7. Files changed

* `myUtils/campaign_prep.py` — added `submit_publish_async`; the default prep
  runner now emits the Telegram review cards after finalising.
* `sau_backend.py` — `_async_prep_enabled()`; async branch in
  `publish_center_submit` and `inbox_item_publish`; drain guard changed to
  `result.jobs or result.campaign_ids`; TG notify guarded by `if result.jobs`;
  `status` added to the async response.
* `mcp_server/tools/publish.py` — async-aware `publish_submit` + `status`.
* `sau_frontend/src/views/PublishCenter.vue` — "preparing" banner state.
* `tests/test_publish_center.py`, `tests/test_inbox_both.py`,
  `tests/test_mcp_server.py` — new tests.
* `logs/async-prep-wire-notes.md` — this file.

Not touched: `myUtils/publish_orchestrator.py` (held; diff in §5),
`myUtils/campaigns.py`, `myUtils/worker.py`, `myUtils/campaign_media_prep.py`.

Not committed, not pushed, container not restarted.
