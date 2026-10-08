# Async-prep safety notes

Scope: the staged-media data-loss risk in the inbox one-click publish route,
the regression coverage for it, and the API contract for asynchronous prep.
Author: session working in `/home/will/social-auto-upload`.

---

## 1. The data-loss risk and the fix

### What was wrong

`POST /api/inbox/items/<id>/publish` (`inbox_item_publish` in
`sau_backend.py`) stages the item's Google Drive source into
`videoFile/_inbox_cache/` and then deleted that staged file when the submit
returned **no jobs**:

```python
submission_started = bool(result.jobs)          # <-- keyed on jobs
if not result.jobs:
    (Path(BASE_DIR) / "videoFile" / staged_path).unlink(missing_ok=True)
```

That is safe only while `submit_publish` is synchronous (either it queues jobs
or it raises). Once prep becomes asynchronous, `submit_publish` returns
`jobs=[]` while the campaign is still `preparing`; the staged source is then
deleted *before the worker ever runs prep*, which silently destroys the only
local copy of the media. The cleanup condition must be keyed on **"did the
submission start (was a campaign created)?"**, never on "were jobs returned?".

### The fix (sau_backend.py, `inbox_item_publish`)

```python
        # Once the staged source is handed to the orchestrator, a failure can
        # leave a partially-created campaign (e.g. profile 1 queued, profile 3
        # missing): those rows reference the staged path, so the exception
        # handlers below must not delete it. Only a failure *before* this point
        # means the media was never referenced and is safe to discard.
        orchestrator_invoked = True
        result = publish_orchestrator.submit_publish(...)
        # A submission has started as soon as the orchestrator materialised a
        # campaign, even if it has not queued any jobs yet. Under asynchronous
        # prep ``result.jobs`` is legitimately empty while the campaign is
        # still ``preparing``; deleting the staged source then would destroy
        # the only local copy before prep ever reads it. Only a submission that
        # produced no campaign at all genuinely failed to start.
        submission_started = bool(result.campaign_ids)
        if not submission_started:
            (Path(BASE_DIR) / "videoFile" / staged_path).unlink(missing_ok=True)
    except LookupError as exc:
        if "staged_path" in locals() and not locals().get("orchestrator_invoked", False):
            (Path(BASE_DIR) / "videoFile" / staged_path).unlink(missing_ok=True)
        return jsonify({"code": 404, "msg": str(exc), "data": None}), 404
    except Exception as exc:  # noqa: BLE001
        if "staged_path" in locals() and not locals().get("orchestrator_invoked", False):
            (Path(BASE_DIR) / "videoFile" / staged_path).unlink(missing_ok=True)
        logging.getLogger(__name__).exception("inbox publish failed")
        return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
```

Key points:

* **Success path** deletes only when `result.campaign_ids` is empty. A campaign
  created with `jobs=[]` (the async shape) keeps the staged file.
* **`except LookupError` / `except Exception`** no longer delete once
  `submit_publish` has been invoked. `submit_publish` can raise `LookupError`
  (`profiles.get_profile`) on a *later* profile after an *earlier* profile
  already created a campaign; deleting the staged source then would orphan the
  media that campaign references. Leaking one staged file is recoverable;
  deleting a referenced source is not. Only a failure before the call
  (`remote_path_for_item`, `stage_remote_media`, `_inbox_publish_payload`)
  discards the staged file.

### The other caller with the same shape

`grep -rn "not result.jobs" sau_backend.py` now returns nothing. The
`/publish-center/submit` route (`publish_center_submit`) uses `result.jobs`
only to decide whether to start the drain thread and to build the review card;
it does **not** stage or delete anything, so it carries no deletion risk. The
only staging/deletion site is the inbox route above. (`stage_remote_media` is
also used by `GET /api/inbox` for previews, but that path never deletes based
on jobs.)

---

## 2. Regression tests

Added to `tests/test_inbox_both.py`, class `InboxPublishStagingCleanupTest`
(all `unittest.skipUnless(flask_available, ...)`):

| Test | Submit shape | Assertion |
| --- | --- | --- |
| `test_async_submit_with_campaign_but_no_jobs_keeps_staged_file` | `campaign_ids=[42], jobs=[]` | staged file **still exists**; response `campaignIds=[42]`, `jobs=[]` |
| `test_failed_submit_without_campaign_discards_staged_file` | `campaign_ids=[], jobs=[], skipped=[]` | staged file **deleted** |
| `test_lookup_error_after_orchestrator_invoked_keeps_staged_file` | `submit_publish` raises `LookupError` | staged file **still exists** |

Each test patches `sau_backend.BASE_DIR` to a temporary directory, writes a
real staged file under `videoFile/_inbox_cache/`, patches
`myUtils.inbox_drive.remote_path_for_item` / `stage_remote_media` and
`sau_backend.publish_orchestrator.submit_publish`, then drives the real Flask
route via `app.test_client()`.

Run: `.venv/bin/python -m pytest tests/test_inbox_both.py -q` -> `10 passed`.
Full suite: `.venv/bin/python -m pytest tests/ -q` -> `1324 passed, 1 skipped`,
`0 failed`.

> Note: the baseline quoted in the task was 1309 passed; the working tree
> already contained concurrent async-prep work in `myUtils/campaigns.py` /
> `myUtils/worker.py` when this change was tested, which accounts for the
> higher count. No tests were removed.

---

## 3. Pinned API contract — `POST /publish-center/submit` under async prep

When prep is asynchronous the endpoint returns **before** prep runs:

```json
{
  "code": 200,
  "msg": "queued",
  "data": {
    "campaignIds": [42],
    "status": "preparing",
    "jobs": [],
    "skipped": []
  }
}
```

Contract rules (consumers MUST follow):

* `campaignIds` is the authoritative "the submission started" signal. Non-empty
  `campaignIds` with empty `jobs` means **prep is in progress**, not failure.
* `status` is `"preparing"` until the worker has finished prep and enqueued
  jobs; callers should poll the publish-entities / campaign view until the
  campaign leaves `preparing`. (The current route does not emit `status` yet;
  adding it is tracked here so consumers can be updated in lockstep.)
* `jobs` may legitimately be `[]` while `status == "preparing"`. Treating an
  empty `jobs` as "nothing happened" is a contract violation.
* `skipped` lists profiles/accounts dropped during submit; it is valid with
  non-empty `campaignIds`.

---

## 4. Every `result.jobs` consumer and its async verdict

`grep -rn "result\.jobs" --include=*.py .` (excluding `.claude/worktrees`) and
the frontend `data.jobs` / `submitResult.jobs` readers:

### Backend

| # | Location | Use | Async verdict (`jobs=[]`, campaign `preparing`) |
| --- | --- | --- | --- |
| 1 | `sau_backend.py` `publish_center_submit` ~L7048 `if result.jobs: _start_worker_drain_thread()` | starts the in-process drain | **NEEDS CHANGE.** With `jobs=[]` the drain thread is not started, so `worker._prep_tick()` (which claims `preparing` campaigns) is not kicked from the submit path. Correct only if a standalone worker is always running. Should trigger on non-empty `result.campaign_ids` (or unconditionally when a campaign exists). |
| 2 | `sau_backend.py` `publish_center_submit` ~L7055 `_notify_tg_review(jobs=result.jobs or [], ...)` | Telegram review card | **STALE.** `_tg_review_cards` iterates jobs; `[]` yields zero cards. Under async the review card is never sent at submit time. It must be emitted when prep completes and jobs are created (or keyed off `campaignIds` and built from campaign posts). |
| 3 | `sau_backend.py` `publish_center_submit` ~L7062 response `"jobs": result.jobs` | API payload | **OK structurally, misleading.** Returns `[]`; with `status:"preparing"` this is the pinned contract. Frontend must not read it as failure (see #8). |
| 4 | `sau_backend.py` `inbox_item_publish` ~L10610 `if result.jobs: _start_worker_drain_thread()` | starts the in-process drain | **NEEDS CHANGE**, same as #1. |
| 5 | `sau_backend.py` `inbox_item_publish` ~L10615 `_notify_tg_review(jobs=result.jobs or [], ...)` | Telegram review card | **STALE**, same as #2. |
| 6 | `sau_backend.py` `inbox_item_publish` ~L10621 response `"jobs": result.jobs` | API payload | **OK structurally.** This route has no `skipped` key; `campaignIds` is present. |
| 7 | `mcp_server/tools/publish.py` L85 `"jobs": result.jobs` | MCP `publish_submit` result | **OK structurally, misleading.** An MCP client receives `jobs:[]` with no `status`; it should read `campaignIds` as the start signal. Consider adding `status` to the MCP payload for parity. |

### Telegram

| # | Location | Use | Async verdict |
| --- | --- | --- | --- |
| 8 | `sau_backend.py` `_tg_review_cards` L6876 `for job in jobs or []` | builds one card per queued campaign post | **BROKEN under async** (same root cause as #2/#5): zero jobs -> zero cards. The card is the operator's pre-publish approve/edit gate; with async prep it would never appear. It must be scheduled after prep finalises and jobs exist. |

### Frontend (`sau_frontend/src/views/PublishCenter.vue`)

| # | Line | Use | Async verdict |
| --- | --- | --- | --- |
| 9 | L1527-1531 `type: data.jobs?.length ? 'success' : 'warning'` and `message: ... '送出成功，但未產生任何工作'` | submit result banner | **WRONG under async.** Empty jobs is rendered as a warning "submitted but produced no jobs", which is exactly the "nothing happened" misread the contract forbids. Must key on `data.status === 'preparing'` / non-empty `campaignIds` and show a "preparing" state instead. |
| 10 | L1535 `if (data.jobs?.some(j => j.platform === 'tiktok')) startStatusPolling()` | starts TikTok status polling | **INCOMPLETE.** With no jobs yet there is nothing to poll and polling never starts for the campaign; it should poll the campaign until jobs materialise. |
| 11 | L425 `v-if="hasTiktokSelected && submitResult.jobs?.length"` | TikTok latency hint | **OK** (hidden until jobs exist; benign). |
| 12 | L432-433 `v-if="submitResult.jobs?.length"` / `v-for="job in submitResult.jobs"` | job links list | **OK but silent.** Renders nothing while `preparing`; the user gets no feedback unless #9 is fixed. |
| 13 | L741 `if (!submitResult.value?.jobs?.length) return` inside `pollTiktokStatuses` | status poller guard | **NEEDS PAIRING** with #10: once jobs are populated by a campaign poll, this works; as written `submitResult.jobs` is a one-shot snapshot that async submit leaves empty, so the poller is a no-op. |

### Out of scope but noted

* No `not result.jobs` cleanup condition remains anywhere in `sau_backend.py`
  (verified by grep) — the only such deletion site was the inbox route fixed
  above.
* The concurrent async-prep work in `myUtils/campaigns.py` (prep request
  persistence, leases) and `myUtils/worker.py` (`_prep_tick`,
  `claim_next_preparing_campaign`, `has_preparing_campaigns`) is the machinery
  that will make the above verdicts live; those files are held for this
  session and were not edited.
