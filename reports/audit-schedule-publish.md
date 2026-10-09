# Audit: scheduling, account routing, publishing, and the Sociamonials fallback

Date: 2026-10-09
Repo: `/home/will/social-auto-upload`
Mode: **read-only**. No source, schema, or DB row was modified. Throwaway
copies (`/tmp/audit_route.db`, `/tmp/audit_dup.db`, `/tmp/audit_race.db`) were
used for the counterexamples; `db/database.db` was only ever read.

Audited revision: commit `24bfcc5` (HEAD). **Concurrent change observed:** while
this audit ran, a parallel agent session left an *uncommitted* working-tree edit
to `myUtils/worker.py` adding `_account_profile_mismatch` (see Addendum in §5,
R1). The findings below describe the committed code I read; the addendum records
how the in-flight change affects R1. `myUtils/campaigns.py`, which was `M` when
the audit started, was committed/cleaned by a parallel session mid-audit, so
re-run `git status` before acting.

Ground truth confirmed from the live DB (`db/database.db`):

| profile id | name | accounts (enabled) |
|---|---|---|
| 1 | NW | 13 (13) |
| 3 | SW | 11 (11) |
| 4 | Teaching | 7 (7) |
| 10 | Money Systems Lab | 2 (2) |

## Commands actually run

```bash
.venv/bin/python -m pytest tests/ -q -k "publish or worker or fallback or orchestrator"
# -> 436 passed, 1001 deselected, 56 subtests passed in 41.54s

# ground truth / collision / duplicate queries against db/database.db (sqlite3 via python3)
# counterexample on copies: /tmp/audit_route.db, /tmp/audit_dup.db, /tmp/audit_race.db
```

---

## 1. ROUTING

### 1.1 Enqueue time IS profile-scoped (proven)

`submit_publish` resolves accounts per profile and only ever creates targets from
that list:

- `myUtils/publish_orchestrator.py:281` `_resolve_accounts(profile_id, selected_account_ids)`
  calls `profile_registry.list_accounts(profile_id=profile_id, enabled=True)`.
  `myUtils/profiles.py:689-691` adds `accounts.profile_id = ?`. Then the selected
  ids are **intersected** with those rows (`publish_orchestrator.py:288-292`), so
  a foreign account id in `selectedAccountIds` is silently dropped.
- The campaign is created with that `profile.id` (`publish_orchestrator.py:400-410`),
  and `_enqueue_post(...)` only receives accounts from `grouped_accounts`
  (`publish_orchestrator.py:425-427`, 594-645).
- The async twin mirrors this: `campaign_prep._resolve_campaign_accounts`
  (`campaign_prep.py:41`) → same `_resolve_accounts`.

Commands/evidence:

```
resolve_accounts(profile=1, selected=[64 -> SW]) = []   # enforced
profiles 1/3/4/10 own exactly 13/11/7/2 accounts
```

So **at enqueue time** profile X can only queue accounts currently owned by X.

### 1.2 Publish time does NOT re-check profile ownership (BUG R1)

The target row stores only `account_ref = "account:<id>"`. There is **no
`profile_id` column** on `publish_job_targets` (schema, `db/database.db`), and
nothing compares `account.profile_id` with `job.profile_id` before publishing:

- `myUtils/worker.py:1559` `_resolve_structured_account` → `profile_registry.get_account(account_id)`
  (`profiles.py:588`) — lookup by id only, no profile, no `enabled` filter.
- `myUtils/worker.py:2735` `default_executor` resolves the account and hands it
  to the publisher; the `_publish_prepared_*` wrappers
  (`worker.py:2161-2586`) only use the passed `account`.
- The fallback has the same shape: `sociamonials_fallback.resolve_mapping`
  keys off `account_id` only (`sociamonials_fallback.py:376`).

Counterexample (executed on `/tmp/audit_route.db`):

```
BEFORE: account 64 -> (profile 3, facebook, SW-FB)
resolve_accounts(profile=1, selected=[64]) = []        # enqueue-time guard works
UPDATE accounts SET profile_id=1 WHERE id=64;          # account moved AFTER queueing
worker _resolve_structured_account('account:64')
    -> account 64 facebook SW-FB profile_id now 1
existing SW job/target: (target 122, account:64, job profile 3)
    -> still publishes to account 64 with NO profile re-check
```

**Impact:** move/repurpose an account between profiles (NW↔SW↔Teaching) and any
already-queued target, retry, or resubmission for the old profile will publish to
the new owner. This is the exact "profile must publish ONLY to its own accounts"
invariant, and it only holds at the moment of queueing.

### 1.3 Disabled accounts are still published (BUG R2)

`profile_registry.get_account` does not filter `enabled`, and neither
`_resolve_structured_account` nor `default_executor` re-checks it. `_resolve_accounts`
filters `enabled=True` only when a job is created. Disabling an account after its
targets are queued does not stop them. (The async prep path *does* re-filter at
prep time via `_resolve_campaign_accounts`, but a target that is already enqueued
is never re-filtered.)

Live DB check: 0 currently-disabled accounts have live targets, so this is a
latent hole, not an active incident.

### 1.4 Cross-profile state currently clean

Query over all live targets found **0** rows where `job.profile_id !=
accounts.profile_id`, 0 disabled, 0 missing accounts. The invariant is intact in
the DB today; the code just does not defend it.

---

## 2. SCHEDULING

### 2.1 Shape: per-target, then per-account de-conflicted

- Each `(account, campaign_post)` gets its own `schedule_at`
  (`publish_orchestrator.py:596-645`, `campaign_prep.py:207-281`).
- `_next_free_slot(account_id, base_time, offset_index, booked)`
  (`publish_orchestrator.py:714`) computes `base + 5*offset_index`
  (`STAGGER_MINUTES=5`), then walks forward in `MIN_GAP_MINUTES=30` steps
  (`SAU_PUBLISH_MIN_GAP_MINUTES`, default 30) until it is ≥30 min from any
  booking **for that account**, and rolls to the same wall-clock slot the next
  day once `MAX_POSTS_PER_ACCOUNT_PER_DAY=3` is reached for a date.
- `booked` is seeded once per submit by `_load_booked_slots`
  (`publish_orchestrator.py:684`) from pending/retrying targets with a non-null
  `schedule_at`, and mutated in-loop so targets in one submit don't collide.

### 2.2 Real scheduled target — schedule is honoured

`t5628` (tiktok, account:109, NW):

```
DB:  schedule_at = 2026-10-08T23:00:00
DB:  started_at  = 2026-10-08T23:00:51   finished_at = 2026-10-08T23:03:50
log: 2026-10-09 07:00:51 | target claimed; starting execution
     account_ref=account:109 attempt=1
log: 2026-10-09 07:03:50 | SUCCESS ... target succeeded
```

`23:00 UTC = 07:00 CST` exactly. The worker did not publish early. The mechanism:

- `jobs._claimable_clause` (`jobs.py:165-176`) requires
  `schedule_at <= now` (tz-naive UTC, same shape as `jobs._now_iso`), so future
  targets are not claimable.
- `PublishWorker._has_pending` only counts *claimable* targets (`worker.py:323-337`),
  and the backend `_publish_scheduler_loop` (`sau_backend.py:6914`) wakes the
  worker when something becomes due (`SAU_PUBLISH_SCHEDULER_INTERVAL_SECONDS=60`
  is set in `.env`).
- When due, the worker publishes *now*: `_publish_date_for_target`
  (`worker.py:1999`) deliberately returns `0` (the "no platform-side scheduling"
  sentinel) for an already-due target, so uploaders don't reject a past
  `publish_date`.

A future target is genuinely not claimed: `job-5669.log` does not exist and
`t5669` is still `pending` at `2026-10-16T12:00:00`.

**Two targets CAN collide in the same minute.** Live DB, pending/retrying,
identical `(account_ref, schedule_at)`:

```
account:120 @ 2026-10-14T22:00:00 -> 3 targets
account:103 @ 2026-10-13T22:35:00 -> 2
account:106 @ 2026-10-14T23:15:00 -> 2
account:113 @ 2026-10-13T22:10:00 -> 2
account:117 @ 2026-10-14T22:25:00 -> 2
account:120 @ 2026-10-15T13:00:00 -> 2
account:121 @ 2026-10-14T22:05:00 -> 2
account:122 @ 2026-10-14T22:20:00 -> 2
 account:77 @ 2026-10-13T22:30:00 -> 2
```

There is **no DB uniqueness** on `(account_ref, schedule_at)` (schema). The
allocator's reservation is a read-then-insert with no lock across the two, so:

- **TOCTOU race (BUG S1).** Two concurrent submits both `_load_booked_slots`
  before either inserts and allocate the same slot. Reproduced on
  `/tmp/audit_race.db` with two threads and a barrier:

  ```
  two concurrent submits allocated: {'a': '2026-11-01T22:00:00',
                                     'b': '2026-11-01T22:00:00'}
  COLLISION
  ```

- Bypass paths that write `schedule_at` without `_next_free_slot`:
  `jobs.reschedule_target` (`jobs.py:1031`, used by the UI and the TG review
  poller), `jobs.resubmit_target` (`jobs.py:1116`, preserves `schedule_at`),
  `scripts/optimize_schedule.py`, `scripts/respace_pending_targets.py`,
  `scripts/recover_failed_targets.py`. These can re-introduce collisions.

The current allocator, handed the *current* booked set, does resolve correctly
(`_next_free_slot(120, 2026-10-14T22:00, 0, booked) -> 2026-10-16 22:30`), so
the existing collisions are consistent with a bypass/race rather than a plain
logic error.

### 2.3 Timezone: the Publish Center submit path is not UTC-correct (BUG S2)

Logs are CST/UTC+8; `publish_job_targets.schedule_at` is documented as tz-naive
**UTC** (`jobs.py:165-176`, `worker.py:1989-1997`). But the initial submit
converts only tz-aware input:

- `publish_orchestrator._resolve_base_time` (`publish_orchestrator.py:170-190`):
  ```python
  if candidate.tzinfo is not None:
      candidate = candidate.astimezone(timezone.utc).replace(tzinfo=None)
  return candidate
  ```
  A naive string is returned untouched and later treated as UTC.
- The frontend sends a **naive local** string:
  `PublishCenter.vue:382-384` `value-format="YYYY-MM-DDTHH:mm:00"` and
  `PublishCenter.vue:1442-1443` `return { publishNow: false, startAt: schedule.startAt }`
  — no `toISOString()`.
- Reproduced:
  ```
  _resolve_base_time({"startAt": "2026-10-18T07:00:00"})      -> 2026-10-18 07:00 (tzinfo=None)
  _resolve_base_time({"startAt": "2026-10-18T07:00:00+08:00"}) -> 2026-10-17 23:00
  ```

**Impact:** an operator in UTC+8 picking 07:00 gets `07:00` stored and fired at
`07:00 UTC = 15:00` local — 8 hours late. The CalendarView renders the stored
string as-is (`CalendarView.vue:269,321`), so the UI *shows* the intended local
time and hides the error. The reschedule path is correct
(`JobsView.vue:341` does `new Date(...).toISOString().slice(0,19)`), so the two
schedule entry points disagree. This is currently latent: no async-prep campaign
in the live DB persisted a `prepRequest` with a `schedule`, and the existing
whole-hour slots look like they came from `scripts/optimize_schedule.py`
(UTC-preferred hours). It is still a real code defect the moment a scheduled
Publish Center submit is made from a CST browser.

`test_publish_center.py:53` `test_start_at_returns_utc_naive_datetime` *asserts*
the naive pass-through (hour stays 10, tzinfo None), so the suite enshrines the
bug instead of catching it.

### 2.4 Nit

`campaign_prep.finalize_campaign` accepts `base_time` but ignores it, recomputing
`_base_time(campaign.metadata)` for every target (`campaign_prep.py:226,271`).
Deterministic, so harmless today; it just invites drift if `_base_time` ever
becomes time-dependent.

---

## 3. FALLBACK trigger table

`_try_sociamonials_fallback` is reached from three places:

- `worker.py:1008-1024` — **X direct path disabled** (config shortcut, no error):
  `if job.platform == "twitter" and not _x_direct_publish_enabled()`.
  With `.env` as-is (`SAU_X_DIRECT_PUBLISH` unset, `SAU_SOCIAMONIALS_FALLBACK=1`,
  key present), `_x_direct_publish_enabled()` returns `not is_enabled()` = **False**,
  so **every X target skips direct publishing entirely.**
- `worker.py:1109-1124` — `retryable is False` (permanent): fallback is tried
  **before** marking failed. This deliberately bypasses
  `sociamonials_fallback.should_attempt_fallback` (see BUG F1).
- `worker.py:1126-1145` — retry budget exhausted (`attempts >= max_attempts`).

The `_content_guard_error` refusal (`worker.py:1000-1007`) marks failed and
returns **before** `_handle_failure`, so placeholder/wrong-language copy never
reaches the fallback.

`resolve_mapping` (`sociamonials_fallback.py:330-381`) must return a mapping and
the network must match the platform (`sociamonials_fallback.py:1140`), otherwise
the call raises and the target falls through to permanent failure. Mappings are
by account id (`SAU_ACCOUNT_TO_SOCIAMONIALS`, `sociamonials_fallback.py:128`);
Teaching accounts carry the separate workspace/key.

| platform (accounts) | mapped? | retryable=True, attempts<max | retryable=True, exhausted | retryable=False (permanent) | content guard |
|---|---|---|---|---|---|
| twitter (123,124,77,103,107) | yes (tw) | no (retries) — **except** X-direct-disabled shortcut: yes immediately | yes | yes | no |
| facebook (11,64,58) | yes (fb) | no | yes | yes | no |
| instagram (72,75,43) | yes (in) | no | yes | yes | no |
| threads (62,42,61) | yes (thrd) | no | yes | yes | no |
| bluesky (118,119,120,121,125) | yes (blsk) | no | yes | yes | no |
| tiktok (109,100) | yes (tiktok) | no | yes | yes | no |
| youtube (110,108) | yes (yt) | no | yes | yes | no |
| reddit (105,106) | no | no | attempted → raises "no mapping" → fail | attempted → fail | no |
| telegram (116,117,122,127) | no | no | attempted → fail | attempted → fail | no |
| nw_sw_blog (112,113) | no | no | attempted → fail | attempted → fail | no |
| teaching_blog (114,115) | no | no | attempted → fail | attempted → fail | no |

Net: fallback **can deliver** for fb/in/tw/blsk/thrd/tiktok/yt; for
reddit/telegram/blog it is attempted (wasting a lookup) then fails. It **does
fire on permanent/permission errors** for every mapped platform.

### 3.1 Quota fix (past 4 GB bug) is present but bounded

- `_resolve_local_media` (`sociamonials_fallback.py:674`) is the single
  reuse-vs-upload decision; it calls `_lookup_reusable_asset`
  (`sociamonials_fallback.py:630`) → `select_reusable_asset`
  (`sociamonials_fallback.py:580`), which requires exact `(filename, size_bytes,
  kind, ready)`. On hit it returns `asset://<id>` without uploading. Reuse is on
  by default (`SAU_SOCIAMONIALS_REUSE_ASSETS` unset).
- Post idempotency is per target (`idempotency_key = f"sau-target-{target_id}"`,
  `sociamonials_fallback.py:1211`), so a retried target/reused asset does not
  create a second post.
- Residual: the library lookup is a single page (`limit=200` and **no
  pagination/offset**, `sociamonials_fallback.py:645-647`). Once the workspace
  library exceeds 200 assets, an existing copy beyond page 1 is missed and the
  file is re-uploaded. (`logs/fix-sociamonials-quota-notes.md` records the
  library at 122 assets when fixed, so this has not bitten yet.)

### 3.2 Delivery truth

- Explicit `delivery_state == "failed"` → `mark_target_failed` and returns
  `True` (`worker.py:1260-1284`). This is the fix for the old
  "200-but-delivery-failed marked succeeded" bug.
- Everything else (`delivered`, `pending`, `unknown`, `requires_approval`) →
  `mark_target_success` (`worker.py:1286`). `_wait_for_delivery`
  (`sociamonials_fallback.py:1316`) treats an unreadable/short poll as
  `unknown`/`pending`. `test_worker_sociamonials_fallback.py:570`
  `test_unconfirmed_delivery_is_still_a_success` codifies this. So a
  still-queued or approval-held post is recorded as **succeeded** even though it
  has not reached the platform. The known bug is reduced, not eliminated.

---

## 4. Duplicate guard

`_already_queued_for_media(account_id, media_group_id)` (`publish_orchestrator.py:58`)
matches a live target (pending/retrying/running) for the same account when
either (1) the campaign's `media_group_id` is identical, or (2) the **set of
file_record_ids** is identical in both directions (full set equality, order and
duplicates irrelevant). It is read-only/best-effort; any SQL error returns
`False` (`publish_orchestrator.py:145-152`).

Real example on `/tmp/audit_dup.db`:

```
target t5686 acct=121 campaign=2587 group=2570 files=[1188]
  _already_queued_for_media(existing group)                 = True
NEW group with SAME file set [1188]                          = True   # retry deduped
NEW group with SUBSET [] (synthetic queued group [A,B])      = False  # genuine new post allowed
NEW group with DIFFERENT file [17]                           = False
```

Adversarial check over the live DB: for all 1282 live targets, the number of
distinct `(account_ref, frozenset(file_record_ids))` keys equals 1282 — i.e.
**zero** duplicate file-sets are live. The guard's documented goals (the
file_record 1142 / 662-target / 2027-04 backlog incidents) look contained.

Coverage gap: the query only counts targets whose `file_ref` is
`campaign_post:<id>` (`publish_orchestrator.py:104-106`). Targets with a
different `file_ref` shape (legacy/non-campaign uploads) are not deduped.

---

## 5. BUGS

### R1 (HIGH at commit 24bfcc5; partially fixed in uncommitted worktree) — No profile re-validation at publish time
- Where: `myUtils/worker.py:1559` (`_resolve_structured_account`), `:2735`
  (`default_executor`), `:2161-2586` (`_publish_prepared_*`);
  `myUtils/sociamonials_fallback.py:376`.
- Evidence: counterexample in §1.2 (account 64 moved 3→1, SW job still targets it);
  `publish_job_targets` has no `profile_id` column.
- Impact: cross-profile publish when an account is moved/repurposed after queueing.
- Fix: persist `profile_id` on `publish_job_targets` (or join the job) and, at
  claim/executor time, resolve the account and abort (mark failed) if
  `account.profile_id != job.profile_id`. Same check before the fallback mapping.
- Test: enqueue a target for profile A/account X, move X to profile B, drain;
  assert no publisher called and target failed with a "profile mismatch" reason.
- **Addendum (uncommitted, observed during audit):** a parallel session added
  `_account_profile_mismatch(target, account, job)` to `worker.py` and an early
  refusal in `_run_target` that fails the target before the executor/fallback.
  This closes R1 for the direct/X-skip/fallback path (the check runs before the
  `try` that reaches `_try_sociamonials_fallback`). It carries **no test** yet and
  does not address R2 (the `enabled` re-check), nor does it validate the fallback
  mapping's workspace when called from any other entry point. Re-verify against
  the committed revision once this lands.

### R2 (MEDIUM) — Disabled accounts still publish
- Where: `myUtils/worker.py:1559`; `myUtils/profiles.py:588` (no `enabled` clause).
- Evidence: live DB currently has 0 disabled-account live targets; code path has
  no guard.
- Fix: skip/fail targets whose account `enabled=0` at claim time (mirror
  `_resolve_accounts(enabled=True)`).
- Test: enqueue, disable account, drain, assert not published.

### S1 (HIGH) — Non-atomic slot reservation → collisions
- Where: `publish_orchestrator._load_booked_slots:684` + `_next_free_slot:714`;
  no unique index on `(account_ref, schedule_at)`.
- Evidence: 9 identical-`(account,schedule_at)` pending groups in the live DB
  (e.g. account:120 @ 2026-10-14T22:00 ×3); two-thread barrier reproduction in §2.2.
- Fix: reserve in one `BEGIN IMMEDIATE` transaction (re-read booked, allocate,
  insert), or add a unique partial index and retry on conflict; route
  reschedule/resubmit/recovery scripts through the same reservation.
- Test: concurrent submit test asserting distinct `schedule_at` per account.

### S2 (HIGH, latent) — Publish Center naive-local schedule treated as UTC
- Where: `myUtils/publish_orchestrator.py:170-190`; frontend
  `sau_frontend/src/views/PublishCenter.vue:382-384,1442-1443`.
- Evidence: `_resolve_base_time` returns naive input unchanged; reproduced
  `07:00` CST pick stays `07:00` (would fire 15:00 CST). Reschedule path does it
  correctly (`JobsView.vue:341`).
- Fix: convert naive `startAt` from the operator timezone (Asia/Shanghai) to UTC,
  or require/force the client to send an offset (e.g. append `+08:00` / use
  `toISOString()`); reject naive input explicitly. Also fix
  `test_start_at_returns_utc_naive_datetime` to assert conversion.
- Test: naive `2026-06-17T10:00:00` + declared tz → expect `02:00` UTC (or an
  error if the policy is "offset required").

### F1 (HIGH) — Fallback fires on permanent/non-retryable failures; guard is dead code
- Where: `myUtils/worker.py:1109-1124`; `sociamonials_fallback.should_attempt_fallback:268`
  is **never called** anywhere in production (grep: only definition + tests).
- Evidence: `tests/test_worker_sociamonials_fallback.py:138`
  `test_non_retryable_failure_attempts_the_fallback` asserts the fallback *is*
  attempted for a permanent failure, directly contradicting
  `test_sociamonials_fallback.py:160` `test_should_attempt_fallback_never_for_non_retryable`.
- Impact: permanent/permission failures on mapped platforms are silently
  re-routed to a different Sociamonials connection — the exact thing the module
  docstring (`sociamonials_fallback.py:23-26`) says must never happen.
- Fix: decide the policy and make code match docs. If permanent fallback is
  wanted only for X, guard with `should_attempt_fallback(retryable=...)` or
  `only_platform="twitter"`. Delete the dead guard if it is truly obsolete.
- Test: permanent failure on a non-X mapped account → assert fallback not called.

### F2 (MEDIUM) — "Accepted but unconfirmed" still marked succeeded
- Where: `myUtils/worker.py:1260-1295`; `sociamonials_fallback.py:1316-1370`.
- Evidence: only `delivery_state == "failed"` fails; `pending`/`unknown`/
  `requires_approval` all call `mark_target_success`
  (`test_unconfirmed_delivery_is_still_a_success`).
- Impact: residual of the old 200-but-not-delivered bug; approval-held posts
  count as delivered.
- Fix: give `requires_approval` and timed-out `pending` a distinct
  `needs_review`/warning state instead of success, or extend the polling budget.
- Test: `requires_approval=True` + no `delivered` → assert not succeeded.

### F3 (MEDIUM) — Reuse lookup is single-page
- Where: `myUtils/sociamonials_fallback.py:630-660` (`limit=200`, no pagination).
- Evidence: quota notes record 122 assets; comment acknowledges 200/page.
- Impact: once the library exceeds one page, an existing copy is missed and
  re-uploaded, re-inflating the quota (the original incident class).
- Fix: paginate (`offset`) or look up by `sha256`/size and cache per run.
- Test: fake library page 1 without the match, page 2 with it → expect reuse.

### P1 (MEDIUM) — Lease loss does not roll back enqueued jobs
- Where: `myUtils/worker.py:879-935` (`_run_campaign_prep` enqueues first, then
  `finish_campaign_prep`); `campaign_prep.finalize_campaign`.
- Evidence: `finalize_campaign` calls `add_campaign_post` + `_enqueue_post`
  before `finish_campaign_prep` verifies the `_prepLease` owner; on lost lease it
  only logs "result discarded" but the jobs/posts are already committed.
- Impact: a prep exceeding the 45 min lease can be reclaimed by a second worker
  and double-enqueue. `_already_queued_for_media` mitigates only if the first
  worker's rows are visible before the second's check (race window remains, and
  `job.idempotency_key` is keyed on the freshly-created `post.id`, so it does not
  dedupe across two finalizes).
- Fix: verify lease ownership immediately before enqueue (pass an
  owner-checked callback), or make enqueue idempotent per
  `(campaign, account, media_group)` and roll back on lost lease.
- Test: reclaim a campaign mid-finalize and assert exactly one job per account.

---

## 6. Test gaps (from `pytest -k "publish or worker or fallback or orchestrator"`, 436 passed)

- No test asserts cross-profile account ownership at publish time (R1).
- No test asserts a disabled account is not published (R2).
- `test_start_at_returns_utc_naive_datetime` asserts the tz bug instead of
  catching it (S2).
- No concurrent-submit/reservation test (S1).
- `should_attempt_fallback` is tested in isolation but never exercised by the
  worker; `test_non_retryable_failure_attempts_the_fallback` asserts the
  opposite policy (F1).
- No test that a fallback with `requires_approval`/`pending` is not marked fully
  delivered (F2).
- No test for reuse-lookup pagination (F3).
- No test for lease loss between enqueue and `finish_campaign_prep` (P1).
- `_already_queued_for_media` coverage is campaign-post only; no test for
  non-campaign `file_ref` shapes.

---

## 7. UNKNOWNS / cannot confirm from here

1. **Whether the naive-local submit path is actually used** — no
   `prepRequest.schedule` exists in the live DB, and existing scheduled slots
   look optimizer-generated (UTC hours), so S2 is proven in code but not yet in
   production data.
2. **Origin of the 9 live schedule collisions** — the allocator is correct with a
   fresh booked set; I could not prove whether they came from an older run, a
   recovery/optimizer/UI reschedule, or the concurrent-submit race. The race is
   independently reproduced.
3. **Sociamonials server behaviour for `first_comment` on X, and whether
   `requires_approval` posts eventually publish** — no live API calls made
   (read-only audit); the code itself marks `require_approval` as a warning but
   succeeds.
4. **Workspace library size vs the 200-row lookup cap** — the quota note says
   122 assets at the time of the fix; current live count was not re-fetched
   (that would require a live API call).
5. **`myUtils/campaigns.py` is uncommitted** (`git status: M`) from a prior
   session; I did not read/modify it beyond `get_prep_request`/`finish_campaign_prep`
   for this audit. If the finalize ordering (P1) is changed there in parallel,
   re-check.
