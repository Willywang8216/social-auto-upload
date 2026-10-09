# Feature: move scheduled targets forward by N days (smartly)

**Status:** implemented, tested, dry-run exercised against the live DB (no
writes). Session task: "Move the schedule by 3 days (or smartly adjust which is
supposed to be implemented as a feature)" — delivered as a reusable CLI + API
feature, not a one-off script.

## What it does

`shift` moves the `publish_job_targets.schedule_at` of a selected set of targets
forward by `N` calendar days (default **3**), then re-spaces the result so it
still satisfies the scheduler's anti-spam invariants:

* no two targets for the same account inside `MIN_GAP_MINUTES` (default 30,
  `myUtils/publish_orchestrator.py:54`);
* no more than `MAX_POSTS_PER_ACCOUNT_PER_DAY` per account per day (default 3,
  `myUtils/publish_orchestrator.py:55`).

The shift is **dry-run by default**. `--apply` / `"apply": true` writes, backs
up the DB first, and re-plans under the existing slot lock.

## Design

New module: `myUtils/schedule_shift.py`.

### 1. Wall-clock-preserving shift

`shift_wall_clock(dt_utc, days, operator_tz)` (`myUtils/schedule_shift.py:56`)
converts the stored naive-UTC value to the operator zone, adds the days to the
**local calendar date**, re-localises and converts back to naive UTC. This is
the important part: `schedule_at` is naive UTC (`myUtils/jobs.py:153`
`_now_iso`, `myUtils/publish_orchestrator.py:194` `_resolve_base_time`) and the
operator reasons in local time. For the operator's default `Asia/Shanghai`
(DST-free) this is numerically the same as `+86400*N`, and the tests **assert**
that equivalence; a second test pins `America/New_York` across the 2026-03-08
DST boundary, where `+86400*N` would change the local hour and the correct
implementation does not.

Date-range scope is likewise interpreted as operator-local dates and converted
to a naive-UTC `[start, end)` window (`local_date_range_to_utc`,
`myUtils/schedule_shift.py:77`).

### 2. Reuse the existing allocator (no parallel reimplementation)

After the wall-clock shift, each target is placed with the *existing*
`publish_orchestrator._next_free_slot` (`myUtils/publish_orchestrator.py:813`)
inside the same booking map produced by `_load_booked_slots`
(`myUtils/publish_orchestrator.py:783`). Before allocating, the selected
targets' **own old slots are removed** from the booking map
(`_remove_selected_bookings`, `myUtils/schedule_shift.py:304`) so a target's
previous time cannot block its new one. Targets are processed in shifted
chronological order so the earliest keeps its slot and later collisions walk
forward via the allocator's existing `+gap` / `roll-to-next-day` branches.

Each moved target records why it went further than the plain shift:

* `"min gap"` — another booking for the same account is within the gap;
* `"daily cap"` — that account already has `max_per_day` on that day.

The reason is computed by mirroring `_next_free_slot`'s two branches; a comment
notes it must be kept in step if the allocator changes (see UNKNOWNS).

### 3. Scope

`ShiftScope` (`myUtils/schedule_shift.py:121`) supports, ANDed:

| scope | CLI | API body |
| --- | --- | --- |
| profile | `--profile <id>` | `scope.profileId` |
| platform | `--platform <slug>` | `scope.platform` |
| account(s) | `--account <id>` (repeatable) | `scope.accountIds: [..]` |
| campaign | `--campaign <id>` | `scope.campaignId` |
| status | `--status pending,retrying` | `scope.statuses: [..]` |
| date range | `--from/--to YYYY-MM-DD` | `scope.from` / `scope.to` |
| terminal opt-in | `--include-terminal` | `scope.includeTerminal` |
| all pending | default | default |

Campaign scope joins `campaign_posts` via `('campaign_post:' || cp.id) = file_ref`
(`myUtils/schedule_shift.py:291`).

### 4. Safety

* `plan_shift` (`myUtils/schedule_shift.py:325`) is strictly read-only.
* `apply_shift` (`myUtils/schedule_shift.py:464`) takes
  `po.slot_reservation_lock` (`myUtils/publish_orchestrator.py:714`), re-plans
  under the lock, then writes.
* Every `UPDATE` is guarded `WHERE id = ? AND status = ?` using the status the
  plan saw, so a target the worker claimed (`pending` → `running`) between plan
  and write is skipped, not relocated mid-upload
  (`myUtils/schedule_shift.py:506`). Running is never selected
  (`NEVER_MOVE_STATUSES`), and `succeeded`/`cancelled` require
  `include_terminal=True` (`_effective_statuses`, `myUtils/schedule_shift.py:228`).
* `backup_database` (`myUtils/schedule_shift.py:450`) copies the SQLite file
  beside itself before the CLI/API `--apply` run. The connection journal mode is
  the default `delete`, so a plain copy cannot miss a WAL sidecar.

## CLI / API surface

### CLI

```
sau schedule shift [--days N] [--profile ID] [--platform SLUG]
                   [--account ID ...] [--campaign ID] [--status a,b]
                   [--from YYYY-MM-DD] [--to YYYY-MM-DD]
                   [--include-terminal] [--min-gap M] [--max-per-day C]
                   [--db-path PATH] [--apply] [--json] [--limit N]
```

Parser: `sau_cli.py:944`; dispatch: `sau_cli.py:1407`; implementation:
`_schedule_shift_command` (`sau_cli.py:1692`). Reachable both as
`python -m sau_cli …` and the installed `sau` console script (verified).

### HTTP

`POST /jobs/schedule/shift` (`sau_backend.py:6636`). Body:

```json
{
  "days": 3,
  "apply": false,
  "includeTerminal": false,
  "scope": {
    "profileId": 3, "platform": "twitter", "accountIds": [120, 121],
    "campaignId": 2529, "statuses": ["pending", "retrying"],
    "from": "2026-10-20", "to": "2026-10-22"
  }
}
```

Returns `{code, msg, data:{mode, summary, changes[]}}`. Dry-run unless
`"apply": true`; the apply path backs up and scopes by
`_workspace_scope()`. Bad `days` / `from` / `to` returns HTTP 400.

## Live dry-run output (no `--apply`)

Command (against the real `db/database.db`, 2026-10-09):

```
sau schedule shift --db-path db/database.db --days 3 --json
```

```json
{"mode":"dry-run","summary":{"days":3,"timezone":"Asia/Shanghai",
 "minGapMinutes":30,"maxPerDay":3,"total":1282,"movedFurther":22,
 "byReason":{"min gap":12,"daily cap":10},
 "byPlatform":{"facebook":14,"instagram":14,"threads":15,"telegram":517,
   "tiktok":9,"nw_sw_blog":115,"bluesky":226,"reddit":114,"youtube":9,
   "twitter":249},
 "pastDueOriginal":0,"skipped":0,"droppedProtectedStatuses":[]}}
```

**Summary:** 1282 targets selected (all pending/retrying), **22 needed extra
movement** beyond the plain `+3` days — 12 for the per-account min gap and 10
for the per-day cap. Examples from the printed table:

```
target 5270 (min gap):    shifted 2026-10-16T22:10:00 -> 2026-10-16T22:40:00
target 5268 (min gap):    shifted 2026-10-17T22:00:00 -> 2026-10-17T22:30:00
target 5585 (daily cap):  shifted 2026-10-17T22:00:00 -> 2026-10-18T22:00:00
target 5568 (daily cap):  shifted 2026-10-18T22:00:00 -> 2026-10-19T22:00:00
```

Post-run verification: no `*.bak-schedule-shift-*` file was created, no
`db/*.slots.lock` exists, and the sampled target rows (`5364`, `5270`, `5274`,
`5585`, `4158`) still hold their original `schedule_at` values. The dry-run
wrote nothing.

## Tests

`tests/test_schedule_shift.py` — 19 tests:

* `WallClockTests` — local wall clock preserved; DST-free zone equals naive
  `+N days`; DST zone diverges from naive and preserves 08:00; local↔UTC range
  conversion.
* `PlanShiftTests` — basic N-day shift; min-gap enforcement (reason `min gap`);
  per-day cap enforcement (reason `daily cap`); plan is read-only; running never
  selected; succeeded/cancelled need explicit opt-in; non-positive days rejected;
  platform/account scope; profile/date-range scope.
* `ApplyShiftTests` — apply writes and backs up; CLI dry-run writes nothing;
  CLI `--apply` writes.
* `ShiftApiTests` — HTTP dry-run by default; HTTP apply; HTTP rejects `days=0`.

Full suite command from the task:

```
.venv/bin/python -m pytest tests/ --ignore=tests/test_security_http.py -q
```

Result: **1493 passed, 1 skipped, 125 subtests passed in 123.62s**. The task's
floor was ≥1474 passed / 1 skipped, so 19 new tests were added without
regressing anything.

## Files changed

| File | Change |
| --- | --- |
| `myUtils/schedule_shift.py` | new module (planner, applier, wall-clock/range helpers, backup, table formatter) |
| `tests/test_schedule_shift.py` | new test file (19 tests) |
| `sau_cli.py` | `schedule shift` parser + dispatch + `_schedule_shift_command` |
| `sau_backend.py` | `POST /jobs/schedule/shift` |

## UNKNOWNS / decisions to revisit

1. **Past-due targets are not clamped.** If an original `schedule_at` is more
   than N days in the past, the shifted value is still in the past and the
   worker will claim it immediately, defeating the delay. The live DB currently
   has 0 past-due pending/retrying targets, so this was not exercised. Whether
   to clamp such targets to `now` (or to `now + N`) is a product decision; the
   planner reports them in `pastDueOriginal` rather than guessing.
2. **"Day" for the per-day cap is the stored UTC day**, because
   `_next_free_slot` compares `candidate.date()` on the naive-UTC value. The
   operator's local day starts at 08:00 UTC in `Asia/Shanghai`, so the cap
   boundary is not the local midnight the operator may expect. This is
   pre-existing scheduler behaviour, reused unchanged; changing it would be a
   separate decision.
3. **Reason strings mirror the allocator's branches.** If
   `_next_free_slot` grows a third adjustment branch, `plan_shift` must be
   updated or it will mislabel the reason (the *allocation* stays correct
   because it is the allocator's output).
4. **Dry-run does not take the slot lock.** A concurrent submit between a
   dry-run plan and a later `--apply` can change the result. The written result
   is still correct because `apply_shift` re-plans under
   `slot_reservation_lock`; only the pre-apply preview can be stale.
5. **`apply_shift` does not reset status or attempts.** A `failed` target that
   is explicitly included via `--status failed` gets a new future `schedule_at`
   but stays `failed` until resubmitted (`jobs.resubmit_target`). That matches
   the existing reschedule semantics; worth confirming it is what an operator
   expects.
6. **Timezone change between schedule and shift.** Because the shift
   reinterprets the stored UTC instant in the *current* operator zone, changing
   `SAU_OPERATOR_TIMEZONE` between queueing and shifting can move the wall-clock
   time. The module takes `operator_tz` as an explicit parameter, so an API/CLI
   caller can pin it; the default follows the environment.
7. **No frontend control.** The feature is CLI + HTTP only; the calendar UI
   does not yet expose a "shift" button. The API is ready for it.
8. **Non-`account:<id>` `file_ref`s** (legacy filename refs) are shifted but
   skip the per-account invariant, since there is no account to space against;
   the plan marks them with an empty reason.
