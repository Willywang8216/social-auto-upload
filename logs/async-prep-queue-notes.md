# Async prep queue — design & implementation notes

Status: implemented (worker-side queue + tests), held-file extraction validated in
an isolated clean tree, held-file enablement diff captured but **not applied**
(`myUtils/publish_orchestrator.py` is held).

## Problem

`POST /publish-center/submit` ran the whole submission synchronously inside the
request:

1. `publish_orchestrator.submit_publish` creates `file_records` + a media group;
2. per profile it resolves accounts and creates a campaign in `preparing`;
3. it calls `prepare_artifacts(...)` — the slow step (~900 s of ffmpeg);
4. only then can it generate per-account drafts (draft generation consumes the
   `media_context` returned by step 3) and enqueue `publish_job_targets`;
5. it flips the campaign to `publishing`.

A >100 s request dies at Cloudflare's 524. `preparing` was a state nothing
resumed. This change adds the smallest correct queue so an off-request worker can
claim a `preparing` campaign, run steps 3+4, and record the outcome.

## 1. Schema decision — `metadata_json`, no migration

**No migration is needed.** `campaigns.metadata_json` is `TEXT NOT NULL DEFAULT
'{}'` and already holds a JSON object (`myUtils/campaigns.py::_row_to_campaign`).
The whole submit payload is JSON-serialisable and fits:

| field | where it lives | notes |
|---|---|---|
| `brief`, `options`, `schedule` | `metadata.prepRequest` | `schedule` is the raw request dict, so `_resolve_base_time` can rebuild it |
| `accountDrafts`, `tiktokPostSettings` | `metadata.prepRequest` | keyed by account id as strings, exactly as the request supplies them |
| `mediaFilePaths` | `metadata.prepRequest` | needed by nothing downstream today (prep reads the media group) but persisted for audit/replay |
| `selectedAccountIds` | `campaigns.selected_account_ids_json` | already a column |
| `profileId`, `mediaGroupId` | `campaigns.profile_id` / `media_group_id` | already columns |
| lease + attempts | `metadata._prepLease` / `metadata._prepAttempts` | queue-only bookkeeping |

New keys are defined in `myUtils/campaigns.py`:

```python
PREP_REQUEST_KEY = "prepRequest"
PREP_LEASE_KEY = "_prepLease"        # {"owner": str, "claimedAt": iso}
PREP_ATTEMPTS_KEY = "_prepAttempts"  # int
```

`campaigns.metadata` is preserved by `update_campaign` (it reads current then
writes the full dict), so the request and the lease coexist with the existing
`title` / `notes` / `requestedPlatforms` / `source` keys.

Why not a `prep_lease` column: `metadata_json` can express the lease, and adding
a column would require a migration + `db/createTable.py` + Alembic revision for
no gain. (The task explicitly asked to prefer metadata when it works.)

## 2. Claim mechanism — compare-and-swap on `metadata_json`

`myUtils/campaigns.claim_next_preparing_campaign(owner=...)`:

* opens `BEGIN IMMEDIATE` (same pattern as `jobs.claim_next_targets`);
* scans `status = 'preparing'` ordered by id;
* skips any campaign whose `_prepLease.claimedAt` is inside the lease window
  (a live worker holds it);
* for the first claimable candidate it does a single guarded UPDATE:

  ```sql
  UPDATE campaigns
     SET metadata_json = ?          -- new metadata with our lease
   WHERE id = ? AND status = 'preparing' AND metadata_json = ?   -- raw observed value
  ```

* the `metadata_json = <raw>` predicate is the CAS: two workers that observed the
  same row cannot both win — the loser's UPDATE matches 0 rows. The DB serialises
  the writes, so no read/write race remains.

The claim bumps `_prepAttempts` and records
`_prepLease = {"owner": <pid+uuid>, "claimedAt": <utc iso>}`.

`myUtils/worker.py` owns the worker identity (`_prep_owner`) and only creates the
prep task after a successful claim, so "not double-claiming" is enforced at the
store layer, not in the loop.

## 3. Recovery mechanism — stale-lease sweep

`myUtils/campaigns.requeue_stale_preparing(older_than_minutes=..., max_attempts=...)`
mirrors `jobs.requeue_stale_running`:

* a `preparing` campaign whose lease `claimedAt` is older than the cutoff is
  stale (default window `DEFAULT_PREP_LEASE_MINUTES = 120`, generous enough to
  never fire on a slow-but-alive ffmpeg run);
* below `max_attempts` the lease is cleared with a CAS so the next claim picks it
  up again (attempts are retained in `_prepAttempts`);
* at/over `max_attempts` the campaign is moved to `needs_review` with an
  explanatory `last_error`, so a crash loop cannot hang forever.

`PublishWorker._prep_tick` runs this sweep on the same
`_STALE_SWEEP_TICK_INTERVAL` cadence (≈60 ticks) as the target sweep.
`claim_next_preparing_campaign` also treats an already-stale lease as claimable,
so a restart recovers immediately even before a sweep tick.

## 4. Worker integration

* `PublishWorker(..., prep_runner=...)` — a pluggable callable
  `(Campaign, Path) -> {"jobs", "skipped"}` (sync or async). `None` disables the
  prep queue (existing tests that construct the worker directly are unaffected).
* `_prep_tick` claims up to `WorkerConfig.prep_batch_size` (default 1) campaigns,
  bounded by `prep_max_concurrent` (default 1 — ffmpeg is heavy).
* `_run_campaign_prep` runs the runner via `asyncio.to_thread` (never blocks the
  event loop), then calls `campaigns.finish_campaign_prep(...)`:
  * `jobs` non-empty → `publishing` (+ `prepared_at`, `published_at`);
  * no jobs → `needs_review` (`last_error = "No publishable posts queued"`);
  * exception → `needs_review` (`last_error = "prep failed: ..."`).
  * `finish_campaign_prep` is lease-guarded: if another worker reclaimed the
    campaign, it returns `False` and the stale result is discarded.
* `drain()`/`_has_pending()` now also consider `preparing` campaigns, so a
  `--once` or Flask in-process drain will not exit before preparing them.
* `run_worker_drain(...)` and the `python -m myUtils.worker` CLI install the
  production runner by default (`campaign_prep.make_default_prep_runner`), which
  lazily imports the `sau_backend` callbacks only when a campaign is actually
  preparing. `run_worker_drain(..., prep_runner=False)` disables it.

## 5. Draft generation ordering

Draft generation cannot run before artifacts exist, which is exactly why the
per-account draft+enqueue half moved with prep. `myUtils/campaign_prep.py`
provides:

* `run_campaign_prep(campaign, ...)` — the worker entry point: rebuild the
  request from `metadata.prepRequest`, call `prepare_artifacts`, then
  `finalize_campaign`.
* `finalize_campaign(campaign, ...)` — the extracted per-account loop (draft
  selection/override normalisation, first-comment handling, multi-media split,
  per-part posts, `_enqueue_post`). It reuses the orchestrator's leaf helpers
  (`_request_data_for_options`, `_files_with_roles`, `_already_queued_for_media`,
  `_build_payload`, `_next_free_slot`, `_enqueue_post`, `_load_booked_slots`) so
  only the orchestration loop is mirrored.

The extraction was validated in an isolated clean checkout: with
`submit_publish` delegating to `campaign_prep.finalize_campaign` the whole
`tests/test_publish_center.py` suite passes except the one source-inspection test
(details below).

## 6. Held-file diff — the exact change wanted

Two diffs, in order. Apply **Diff A** first (behaviour-preserving extraction);
**Diff B** is the actual request-path fix and will require the accompanying test
and wiring updates listed after it.

### Diff A — extract the loop into `campaign_prep.finalize_campaign`

Adds one import and replaces the 190-line inline loop with a call. This removes
the drift risk between the two copies and is validated as behaviour-preserving.

`tests/test_publish_center.py::DuplicateQueueGuardWiringTests::test_account_loop_consults_the_duplicate_guard`
does `inspect.getsource(submit_publish)` and asserts
`_already_queued_for_media(` appears there. After Diff A it lives in
`campaign_prep.finalize_campaign`, so that assertion must be retargeted to
`campaign_prep.finalize_campaign` (or dropped — the behaviour itself is covered
by the submit tests).

```diff
--- a/myUtils/publish_orchestrator.py
+++ b/myUtils/publish_orchestrator.py
@@ -29,6 +29,7 @@
 from pathlib import Path
 from typing import Callable
 
+from myUtils import campaign_prep
 from myUtils import campaigns as campaign_store
 from myUtils import content_rating
 from myUtils import content_rules
@@ -415,200 +416,33 @@
             skipped.append({"profileId": profile.id, "reason": f"artifact prep failed: {exc}"})
             continue
 
-        # group accounts by platform so each platform gets its own
-        # campaign_posts/job spec
-        grouped_accounts: dict[str, list[profile_registry.Account]] = {}
-        for account in accounts:
-            grouped_accounts.setdefault(account.platform, []).append(account)
-
-        link_in_first_comment = bool((options or {}).get("linkInFirstComment"))
-        # Publish publicly by default. The frontend toggle can still request a
-        # draft (inbox) upload, but the historical default of False meant every
-        # TikTok post silently landed in the drafts and was invisible on the
-        # profile. An unreviewed app rejects DIRECT_POST and the universal
-        # Sociamonials fallback then catches it.
-        tiktok_direct_post = bool((options or {}).get("tiktokDirectPost", True))
-
-        artifacts = [
-            artifact.to_dict()
-            for artifact in campaign_store.list_campaign_artifacts(campaign.id, db_path=db_path)
-        ]
-
-        for platform, platform_accounts in grouped_accounts.items():
-            supports_first_comment = platform_capabilities.platform_supports_first_comment(platform)
-            supports_multi_media = platform_capabilities.platform_supports_multi_media(platform)
-
-            for account in platform_accounts:
-                # Cross-job duplicate guard. The target table is unique on
-                # (job_id, account_ref, file_ref), so the same media re-submitted
-                # under a new job used to queue again - that is how one account
-                # accumulated 662 targets for 356 files. Skip (and report) rather
-                # than silently piling on more scheduled sends.
-                if _already_queued_for_media(
-                    account.id, media_group.id, db_path=db_path
-                ):
-                    skipped.append({
-                        "profileId": profile_id,
-                        "accountId": account.id,
-                        "accountName": account.nickname or account.account_name,
-                        "platform": account.platform,
-                        "reason": "this media is already queued for this account",
-                    })
-                    continue
-                draft_override = account_drafts.get(str(account.id)) or account_drafts.get(account.id)
-                if isinstance(draft_override, dict) and draft_override.get("message"):
-                    # The override arrives from the client, and a model-backed
-                    # preview can leave the draft as a stringified JSON object or
-                    # a "Title: ... / Description: ..." blob. Normalise here, at
-                    # the one place the override is consumed, so every caller —
-                    # the web UI, the inbox one-click route and MCP — gets the
-                    # same clean copy the preview showed the operator.
-                    draft = content_rules.normalize_draft_fields(dict(draft_override))
-                else:
-                    try:
-                        # Draft content is account-specific (language, voice,
-                        # and connected identity may differ even on one platform).
-                        request_for_account = dict(request_data)
-                        effective_language = str(
-                            (account.config or {}).get("audience_language")
-                            or (account.config or {}).get("audienceLanguage")
-                            or (profile.settings or {}).get("default_language")
-                            or (profile.settings or {}).get("defaultLanguage")
-                            or ""
-                        ).strip()
-                        if platform == "twitter" and len(effective_language.replace(",", " ").replace("+", " ").split()) > 1:
-                            raise ValueError("X requires one language per account; use distinct account settings instead of a bilingual language list")
-                        if effective_language:
-                            request_for_account["_accountLanguage"] = effective_language
-                        draft = dict(generate_account_draft(
-                            account, profile, media_group, request_for_account, media_context
-                        ))
-                    except Exception as exc:  # noqa: BLE001
-                        account_config = account.config or {}
-                        account_settings = profile.settings or {}
-                        if (account_config.get("audience_language")
-                                or account_config.get("audienceLanguage")
-                                or account_settings.get("default_language")
-                                or account_settings.get("defaultLanguage")):
-                            skipped.append({
-                                "profileId": profile_id,
-                                "accountId": account.id,
-                                "reason": f"language-specific draft generation failed: {exc}",
-                            })
-                            continue
-                        if content_rules.is_usable_copy(brief):
-                            draft = {
-                                "message": (brief or "").strip(),
-                                "hashtags": [],
-                                "firstComment": "",
-                                "error": str(exc),
-                            }
-                        else:
-                            # No real copy and no usable brief: skip rather than
-                            # publish the media-group name / generic batch brief.
-                            skipped.append({
-                                "profileId": profile_id,
-                                "accountId": account.id,
-                                "reason": f"draft generation failed and no usable brief: {exc}",
-                            })
-                            continue
-
-                if not link_in_first_comment or not supports_first_comment:
-                    # If the platform does not support a first-comment
-                    # convention, fold any link back into the body so the
-                    # user does not lose it.
-                    if draft.get("firstComment") and not supports_first_comment:
-                        message = (draft.get("message") or "").rstrip()
-                        comment = (draft.get("firstComment") or "").strip()
-                        if comment and comment not in message:
-                            draft["message"] = (message + "\n\n" + comment).strip()
-                        draft["firstComment"] = ""
-                    elif not link_in_first_comment:
-                        draft["firstComment"] = ""
-
-                if supports_multi_media or len(publishable_files) <= 1:
-                    # One source video longer than a platform cap becomes
-                    # several part-posts; anything within the cap is a single
-                    # group, identical to before.
-                    if artifact_part_groups_for_platform is not None:
-                        part_groups = artifact_part_groups_for_platform(artifacts, platform)
-                    else:
-                        part_groups = [artifact_payloads_for_platform(artifacts, platform)]
-                    for part_index, part_artifacts in enumerate(part_groups, start=1):
-                        post = campaign_store.add_campaign_post(
-                            campaign.id,
-                            platform,
-                            account_ids=[account.id],
-                            draft=draft,
-                            status=campaign_store.CAMPAIGN_POST_READY,
-                            db_path=db_path,
-                        )
-                        account_tt_settings = tiktok_post_settings.get(str(account.id)) or tiktok_post_settings.get(account.id)
-                        payload = _build_payload(
-                            campaign, post, draft, part_artifacts, platform,
-                            tiktok_direct_post=tiktok_direct_post,
-                            tiktok_post_settings=account_tt_settings if isinstance(account_tt_settings, dict) else None,
-                        )
-                        if len(part_groups) > 1:
-                            payload["partIndex"] = part_index
-                            payload["partCount"] = len(part_groups)
-                        targets = [
-                            (
-                                f"account:{account.id}",
-                                f"campaign_post:{post.id}",
-                                _next_free_slot(account.id, base_time, stagger_offset, booked_slots),
-                            )
-                        ]
-                        stagger_offset += 1
-                        queued_jobs.append(
-                            _enqueue_post(
-                                platform, payload, targets, campaign, post, job_to_payload, db_path
-                            )
-                        )
-                else:
-                    for media_file in publishable_files:
-                        single_id = int(media_file["file_record_id"])
-                        post = campaign_store.add_campaign_post(
-                            campaign.id,
-                            platform,
-                            account_ids=[account.id],
-                            draft=draft,
-                            file_record_ids=[single_id],
-                            status=campaign_store.CAMPAIGN_POST_READY,
-                            db_path=db_path,
-                        )
-                        account_tt_settings = tiktok_post_settings.get(str(account.id)) or tiktok_post_settings.get(account.id)
-                        payload = _build_payload(
-                            campaign, post, draft,
-                            artifact_payloads_for_platform(artifacts, platform),
-                            platform,
-                            tiktok_direct_post=tiktok_direct_post,
-                            tiktok_post_settings=account_tt_settings if isinstance(account_tt_settings, dict) else None,
-                        )
-                        payload["fileRecordIds"] = [single_id]
-                        targets = [
-                            (
-                                f"account:{account.id}",
-                                f"campaign_post:{post.id}",
-                                _next_free_slot(account.id, base_time, stagger_offset, booked_slots),
-                            )
-                        ]
-                        stagger_offset += 1
-                        queued_jobs.append(
-                            _enqueue_post(
-                                platform, payload, targets, campaign, post, job_to_payload, db_path
-                            )
-                        )
-
+        result = campaign_prep.finalize_campaign(
+            campaign,
+            profile=profile,
+            accounts=accounts,
+            media_context=media_context,
+            media_files=media_files,
+            request_data=request_data,
+            options=options or {},
+            account_drafts=account_drafts,
+            tiktok_post_settings=tiktok_post_settings,
+            db_path=db_path,
+            artifact_payloads_for_platform=artifact_payloads_for_platform,
+            generate_account_draft=generate_account_draft,
+            artifact_part_groups_for_platform=artifact_part_groups_for_platform,
+            job_to_payload=job_to_payload,
+            base_time=base_time,
+        )
+        queued_jobs.extend(result["jobs"])
+        skipped.extend(result["skipped"])
         campaign_store.update_campaign(
             campaign.id,
-            status=campaign_store.CAMPAIGN_PUBLISHING if queued_jobs else campaign_store.CAMPAIGN_NEEDS_REVIEW,
+            status=campaign_store.CAMPAIGN_PUBLISHING if result["jobs"] else campaign_store.CAMPAIGN_NEEDS_REVIEW,
             prepared_at=now_fn().isoformat(timespec="seconds"),
-            published_at=now_fn().isoformat(timespec="seconds") if queued_jobs else None,
-            last_error=None if queued_jobs else "No publishable posts queued",
+            published_at=now_fn().isoformat(timespec="seconds") if result["jobs"] else None,
+            last_error=None if result["jobs"] else "No publishable posts queued",
             db_path=db_path,
         )
-
     return SubmitResult(campaign_ids=campaign_ids, jobs=queued_jobs, skipped=skipped)
 
 
```

### Diff B — defer prep/finalize from the request path

Persist the full submit payload under `metadata.prepRequest` at campaign
creation and remove the whole prep+finalize block from `submit_publish`. The
campaign stays `preparing`; the worker's `run_campaign_prep` performs the work.

```diff
--- a/myUtils/publish_orchestrator.py
+++ b/myUtils/publish_orchestrator.py
@@ -29,6 +29,7 @@
 from pathlib import Path
 from typing import Callable
 
+from myUtils import campaign_prep
 from myUtils import campaigns as campaign_store
 from myUtils import content_rating
 from myUtils import content_rules
@@ -387,228 +388,22 @@
                 "notes": request_data.get("notes", ""),
                 "requestedPlatforms": sorted({account.platform for account in accounts}),
                 "source": "publish-center",
+                # Persist the full submit payload so the off-request worker can
+                # run artifact prep + per-account draft/enqueue later. See
+                # myUtils/campaign_prep.py and logs/async-prep-queue-notes.md.
+                campaign_store.PREP_REQUEST_KEY: {
+                    "brief": brief,
+                    "options": options or {},
+                    "schedule": schedule,
+                    "accountDrafts": account_drafts,
+                    "tiktokPostSettings": tiktok_post_settings,
+                    "mediaFilePaths": list(media_file_paths),
+                },
             },
             db_path=db_path,
         )
         campaign_ids.append(campaign.id)
 
-        media_files = _load_media_group_files_via_callback(
-            media_group.id, db_path=db_path
-        )
-        publishable_files = _files_with_roles(media_files)
-        try:
-            media_context = prepare_artifacts(
-                campaign.id,
-                profile,
-                media_files,
-                request_data,
-                selected_platforms={account.platform for account in accounts},
-                db_path=db_path,
-            )
-        except Exception as exc:  # noqa: BLE001
-            campaign_store.update_campaign(
-                campaign.id,
-                status=campaign_store.CAMPAIGN_NEEDS_REVIEW,
-                last_error=f"artifact prep failed: {exc}",
-                db_path=db_path,
-            )
-            skipped.append({"profileId": profile.id, "reason": f"artifact prep failed: {exc}"})
-            continue
-
-        # group accounts by platform so each platform gets its own
-        # campaign_posts/job spec
-        grouped_accounts: dict[str, list[profile_registry.Account]] = {}
-        for account in accounts:
-            grouped_accounts.setdefault(account.platform, []).append(account)
-
-        link_in_first_comment = bool((options or {}).get("linkInFirstComment"))
-        # Publish publicly by default. The frontend toggle can still request a
-        # draft (inbox) upload, but the historical default of False meant every
-        # TikTok post silently landed in the drafts and was invisible on the
-        # profile. An unreviewed app rejects DIRECT_POST and the universal
-        # Sociamonials fallback then catches it.
-        tiktok_direct_post = bool((options or {}).get("tiktokDirectPost", True))
-
-        artifacts = [
-            artifact.to_dict()
-            for artifact in campaign_store.list_campaign_artifacts(campaign.id, db_path=db_path)
-        ]
-
-        for platform, platform_accounts in grouped_accounts.items():
-            supports_first_comment = platform_capabilities.platform_supports_first_comment(platform)
-            supports_multi_media = platform_capabilities.platform_supports_multi_media(platform)
-
-            for account in platform_accounts:
-                # Cross-job duplicate guard. The target table is unique on
-                # (job_id, account_ref, file_ref), so the same media re-submitted
-                # under a new job used to queue again - that is how one account
-                # accumulated 662 targets for 356 files. Skip (and report) rather
-                # than silently piling on more scheduled sends.
-                if _already_queued_for_media(
-                    account.id, media_group.id, db_path=db_path
-                ):
-                    skipped.append({
-                        "profileId": profile_id,
-                        "accountId": account.id,
-                        "accountName": account.nickname or account.account_name,
-                        "platform": account.platform,
-                        "reason": "this media is already queued for this account",
-                    })
-                    continue
-                draft_override = account_drafts.get(str(account.id)) or account_drafts.get(account.id)
-                if isinstance(draft_override, dict) and draft_override.get("message"):
-                    # The override arrives from the client, and a model-backed
-                    # preview can leave the draft as a stringified JSON object or
-                    # a "Title: ... / Description: ..." blob. Normalise here, at
-                    # the one place the override is consumed, so every caller —
-                    # the web UI, the inbox one-click route and MCP — gets the
-                    # same clean copy the preview showed the operator.
-                    draft = content_rules.normalize_draft_fields(dict(draft_override))
-                else:
-                    try:
-                        # Draft content is account-specific (language, voice,
-                        # and connected identity may differ even on one platform).
-                        request_for_account = dict(request_data)
-                        effective_language = str(
-                            (account.config or {}).get("audience_language")
-                            or (account.config or {}).get("audienceLanguage")
-                            or (profile.settings or {}).get("default_language")
-                            or (profile.settings or {}).get("defaultLanguage")
-                            or ""
-                        ).strip()
-                        if platform == "twitter" and len(effective_language.replace(",", " ").replace("+", " ").split()) > 1:
-                            raise ValueError("X requires one language per account; use distinct account settings instead of a bilingual language list")
-                        if effective_language:
-                            request_for_account["_accountLanguage"] = effective_language
-                        draft = dict(generate_account_draft(
-                            account, profile, media_group, request_for_account, media_context
-                        ))
-                    except Exception as exc:  # noqa: BLE001
-                        account_config = account.config or {}
-                        account_settings = profile.settings or {}
-                        if (account_config.get("audience_language")
-                                or account_config.get("audienceLanguage")
-                                or account_settings.get("default_language")
-                                or account_settings.get("defaultLanguage")):
-                            skipped.append({
-                                "profileId": profile_id,
-                                "accountId": account.id,
-                                "reason": f"language-specific draft generation failed: {exc}",
-                            })
-                            continue
-                        if content_rules.is_usable_copy(brief):
-                            draft = {
-                                "message": (brief or "").strip(),
-                                "hashtags": [],
-                                "firstComment": "",
-                                "error": str(exc),
-                            }
-                        else:
-                            # No real copy and no usable brief: skip rather than
-                            # publish the media-group name / generic batch brief.
-                            skipped.append({
-                                "profileId": profile_id,
-                                "accountId": account.id,
-                                "reason": f"draft generation failed and no usable brief: {exc}",
-                            })
-                            continue
-
-                if not link_in_first_comment or not supports_first_comment:
-                    # If the platform does not support a first-comment
-                    # convention, fold any link back into the body so the
-                    # user does not lose it.
-                    if draft.get("firstComment") and not supports_first_comment:
-                        message = (draft.get("message") or "").rstrip()
-                        comment = (draft.get("firstComment") or "").strip()
-                        if comment and comment not in message:
-                            draft["message"] = (message + "\n\n" + comment).strip()
-                        draft["firstComment"] = ""
-                    elif not link_in_first_comment:
-                        draft["firstComment"] = ""
-
-                if supports_multi_media or len(publishable_files) <= 1:
-                    # One source video longer than a platform cap becomes
-                    # several part-posts; anything within the cap is a single
-                    # group, identical to before.
-                    if artifact_part_groups_for_platform is not None:
-                        part_groups = artifact_part_groups_for_platform(artifacts, platform)
-                    else:
-                        part_groups = [artifact_payloads_for_platform(artifacts, platform)]
-                    for part_index, part_artifacts in enumerate(part_groups, start=1):
-                        post = campaign_store.add_campaign_post(
-                            campaign.id,
-                            platform,
-                            account_ids=[account.id],
-                            draft=draft,
-                            status=campaign_store.CAMPAIGN_POST_READY,
-                            db_path=db_path,
-                        )
-                        account_tt_settings = tiktok_post_settings.get(str(account.id)) or tiktok_post_settings.get(account.id)
-                        payload = _build_payload(
-                            campaign, post, draft, part_artifacts, platform,
-                            tiktok_direct_post=tiktok_direct_post,
-                            tiktok_post_settings=account_tt_settings if isinstance(account_tt_settings, dict) else None,
-                        )
-                        if len(part_groups) > 1:
-                            payload["partIndex"] = part_index
-                            payload["partCount"] = len(part_groups)
-                        targets = [
-                            (
-                                f"account:{account.id}",
-                                f"campaign_post:{post.id}",
-                                _next_free_slot(account.id, base_time, stagger_offset, booked_slots),
-                            )
-                        ]
-                        stagger_offset += 1
-                        queued_jobs.append(
-                            _enqueue_post(
-                                platform, payload, targets, campaign, post, job_to_payload, db_path
-                            )
-                        )
-                else:
-                    for media_file in publishable_files:
-                        single_id = int(media_file["file_record_id"])
-                        post = campaign_store.add_campaign_post(
-                            campaign.id,
-                            platform,
-                            account_ids=[account.id],
-                            draft=draft,
-                            file_record_ids=[single_id],
-                            status=campaign_store.CAMPAIGN_POST_READY,
-                            db_path=db_path,
-                        )
-                        account_tt_settings = tiktok_post_settings.get(str(account.id)) or tiktok_post_settings.get(account.id)
-                        payload = _build_payload(
-                            campaign, post, draft,
-                            artifact_payloads_for_platform(artifacts, platform),
-                            platform,
-                            tiktok_direct_post=tiktok_direct_post,
-                            tiktok_post_settings=account_tt_settings if isinstance(account_tt_settings, dict) else None,
-                        )
-                        payload["fileRecordIds"] = [single_id]
-                        targets = [
-                            (
-                                f"account:{account.id}",
-                                f"campaign_post:{post.id}",
-                                _next_free_slot(account.id, base_time, stagger_offset, booked_slots),
-                            )
-                        ]
-                        stagger_offset += 1
-                        queued_jobs.append(
-                            _enqueue_post(
-                                platform, payload, targets, campaign, post, job_to_payload, db_path
-                            )
-                        )
-
-        campaign_store.update_campaign(
-            campaign.id,
-            status=campaign_store.CAMPAIGN_PUBLISHING if queued_jobs else campaign_store.CAMPAIGN_NEEDS_REVIEW,
-            prepared_at=now_fn().isoformat(timespec="seconds"),
-            published_at=now_fn().isoformat(timespec="seconds") if queued_jobs else None,
-            last_error=None if queued_jobs else "No publishable posts queued",
-            db_path=db_path,
-        )
-
     return SubmitResult(campaign_ids=campaign_ids, jobs=queued_jobs, skipped=skipped)
 
 
```

Accompanying non-held-file changes required by Diff B:

1. **Start the worker when a campaign is queued.** The route currently only
   calls `_start_worker_drain_thread()` when `result.jobs` is non-empty
   (`sau_backend.py` `publish_center_submit`, and the MCP/inbox equivalents).
   With prep deferred the response has no jobs, so change the trigger to
   `if result.campaign_ids: _start_worker_drain_thread()`.
2. **Update synchronous expectations.** `tests/test_publish_center.py` asserts a
   200 + jobs from `/publish-center/submit`. Those tests must be split: submit
   asserts one `preparing` campaign + persisted `prepRequest`, while the
   prep/enqueue assertions move to `tests/test_campaign_prep_queue.py` (already
   added).
3. **Response/frontend.** `/publish-center/submit` should tell the client the
   campaign is preparing (the publish-center list already renders `preparing`).
   If the SPA relies on `data.jobs` immediately, it must poll/refetch.

## 7. Tests

New file `tests/test_campaign_prep_queue.py` (16 tests) covers exactly the four
required behaviours plus the extracted runner:

* claim records the lease + attempt;
* a live lease is not double-claimed;
* a stale lease is immediately reclaimable;
* `requeue_stale_preparing` releases stale leases and fails over-budget ones;
* `finish_campaign_prep` rejects a foreign owner and clears the lease for the
  owner;
* worker success → `publishing`, worker failure → `needs_review` with the error,
  empty result → `needs_review`;
* prep-disabled worker leaves a `preparing` campaign untouched;
* `drain()` does not exit while a preparing campaign exists;
* `run_campaign_prep` calls `prepare_artifacts` before `generate_account_draft`
  and produces the post + job;
* a legacy campaign with no persisted `prepRequest` raises.

Results:

* `tests/test_campaign_prep_queue.py`: **16 passed**.
* Full suite in an isolated clean `git archive HEAD` tree with only this
  change set applied (no other session's edits, held file left untouched):
  **1337 passed, 1 skipped, 92 subtests passed** (≈4m02s). The task baseline
  was 1309 passed / 1 skipped; the gap is pre-existing test additions at the
  current HEAD plus the 16 new tests here, not a regression.
* Diff A validated in the same isolated tree: `tests/test_publish_center.py`
  **57 passed**, with only the source-inspection test failing as explained.
* The working tree currently also contains another session's uncommitted edits
  (`sau_backend.py`, `tests/test_inbox_both.py`) that make an in-place
  `pytest tests/ -q` nondeterministic. That is why validation was run in the
  isolated archive; my two modified files (`campaigns.py`, `worker.py`) are
  unaffected by those edits.

## 8. Residual risks / follow-ups

* **Double-finalize window.** If a prep outlives the lease window (a >2 h
  ffmpeg run) a second worker can reclaim and both can enqueue. The lease-guarded
  `finish_campaign_prep` stops the stale result from clobbering status, but the
  enqueue itself is not transactional with the status flip. Either raise the
  window above the worst-case prep or add an idempotency key derived from
  `(campaign_id, platform, account_id, part_index)`. Not needed at default
  traffic; flagged.
* **Standalone worker imports `sau_backend` lazily.** `make_default_prep_runner`
  imports the Flask app's callbacks at first prep. That is correct for the
  in-process drain (already loaded) but spins up `sau_backend`'s module-level
  threads in a standalone worker. A dedicated callback module would be cleaner.
* **Multi-profile submit semantics.** `finalize_campaign` starts `stagger_offset`
  at 0 per campaign and reloads booked slots from the DB. Because earlier
  campaigns in the same submit have already committed their targets, collision
  avoidance still holds; the offset sequence can differ slightly from the old
  shared counter. The anti-spam gap allocator covers this.

## 9. Shared working-tree note

This repository's working tree is shared with another agent session that is
working the same feature area in parallel. While this session was implementing
the queue, that session added a separate `submit_publish_async(...)` wrapper
plus Telegram-review wiring to `myUtils/campaign_prep.py`, and its own
`myUtils/campaign_media_prep.py` / notes files
(`logs/async-prep-{extract,safety,wire}-notes.md`). That code is not part of
this session's diff, but it coexists in the module.

Because both sessions were writing concurrently, `pytest tests/ -q` in the
live tree is not a stable signal. The authoritative validation for this work
was run against an isolated `git archive HEAD` tree containing only this
session's files (the held `publish_orchestrator.py` left untouched):
**1337 passed, 1 skipped, 92 subtests**. The current combined snapshot (this
session's queue + the other session's `submit_publish_async`) also passes the
full suite in the same isolated tree.
