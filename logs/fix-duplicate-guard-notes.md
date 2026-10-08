# Fix: over-blocking duplicate guard in `_already_queued_for_media`

Scope: `myUtils/publish_orchestrator.py`, `tests/test_publish_center.py`.
No commits, no pushes, no container restart.

## Final semantics / SQL

The guard now blocks a queue only when **all** of these hold:

1. the target belongs to the same account (`t.account_ref = 'account:<id>'`);
2. the target is still **live** (`pending` / `retrying` / `running`; terminal
   `succeeded` / `failed` / `cancelled` never count);
3. **and** either
   - the queued campaign is the *same* media group
     (`c.media_group_id = :media_group_id`), or
   - the queued campaign's media group has exactly the **same set of
     `file_record_id`s** as the group being submitted (set equality in both
     directions, order- and duplicate-insensitive).

```sql
WITH candidate AS (
    SELECT DISTINCT file_record_id
    FROM media_group_items
    WHERE media_group_id = :media_group_id
)
SELECT 1
FROM publish_job_targets t
JOIN campaign_posts cp ON ('campaign_post:' || cp.id) = t.file_ref
JOIN campaigns c ON c.id = cp.campaign_id
WHERE t.account_ref = :account_ref
  AND t.status IN ('pending', 'retrying', 'running')
  AND (
        c.media_group_id = :media_group_id
        OR (
            EXISTS (SELECT 1 FROM candidate)
            AND NOT EXISTS (            -- every candidate file is in the queued group
                SELECT 1 FROM candidate cand
                WHERE NOT EXISTS (
                    SELECT 1 FROM media_group_items queued_item
                    WHERE queued_item.media_group_id = c.media_group_id
                      AND queued_item.file_record_id = cand.file_record_id
                )
            )
            AND NOT EXISTS (            -- every queued file is in the candidate group
                SELECT 1 FROM media_group_items queued_item
                WHERE queued_item.media_group_id = c.media_group_id
                  AND NOT EXISTS (
                      SELECT 1 FROM candidate cand
                      WHERE cand.file_record_id = queued_item.file_record_id
                  )
            )
        )
  )
LIMIT 1
```

Why the **set** and not "shares any one file": a retry of an identical
submission rebuilds the media group around the same files, so set equality
catches `{1142}` -> `{1142}`. But sharing a single asset is common and
legitimate (a multi-media post that reuses one clip, a single-media split of a
larger batch), so "shares any one file" wrongly blocked `{701}` -> `{701,702}`.
The two-way `NOT EXISTS` makes the comparison symmetric, and `DISTINCT` makes
it order/duplicate insensitive. The exact-group check is kept so an *empty*
group that is resubmitted under the same id is still caught.

## Why the tests were actually failing (root cause)

The 10 red tests were **not** caused by the guard. They are caused by the
ambient repo-root `.env` (git-ignored, local, untracked) containing:

```
SAU_ASYNC_PREP=1
```

`myUtils/__init__.py` calls `load_repo_env()` at import, and
`from myUtils import publish_orchestrator` in the test module triggers it. So
every publish-center/inbox submit in the test run took the **async prep** path
and returned `{"status": "preparing", "jobs": []}` instead of queueing jobs
synchronously. That is exactly the `jobs=[]` the task reported, and it is why
the submit tests looked like the guard had skipped everything.

Evidence:
- `.env` was modified at `2026-10-08 19:22:18` (it is `SAU_ASYNC_PREP=1` on
  line 125); the sibling `.env.bak-async-test-20261008192218` backup has no
  `ASYNC` line. This is external, shared local state (a concurrent async-prep
  testing session), not a source change.
- Reverting only `myUtils/publish_orchestrator.py` to `HEAD` did **not** fix
  `test_submit_creates_jobs_for_valid_request` — it still returned `jobs=[]`.
- With `SAU_ASYNC_PREP=0`, all 10 failures pass; the guard change is unrelated.
- Every failed test's response carried `"status": "preparing"`.

The failure was diagnosed by *reading the fixture*, as requested: the submit
fixture seeds no campaign rows at all and uses one file record per test, so the
guard has nothing to match; the second condition was never the cause.

## Test results

Guard-specific (3 pre-existing + 2 new regression tests):

```
.venv/bin/python -m pytest tests/test_publish_center.py -q -k "Duplicate"
=> 10 passed, 3 subtests passed
```

Full file (requires the ambient async flag off; see caveat):

```
SAU_ASYNC_PREP=0 .venv/bin/python -m pytest tests/test_publish_center.py -q
=> 54 passed, 16 subtests passed
```

Full suite:

```
SAU_ASYNC_PREP=0 .venv/bin/python -m pytest tests/ --ignore=tests/test_security_http.py -q
=> 1346 passed, 1 skipped, 105 subtests passed
```

`1346 = 1341 (baseline) + 3 (new tests already added) + 2 (new regression
tests added here)`.

### New regression tests

`tests/test_publish_center.py::DuplicateGuardSameFileDifferentGroupTests`:
- `test_same_file_in_a_new_media_group_is_still_a_duplicate` (retry case) — passes.
- `test_a_genuinely_different_file_is_not_blocked` — passes.
- `test_same_file_on_a_different_account_is_allowed` — passes.
- `test_sharing_one_file_with_a_different_set_is_not_blocked` — **new**;
  fails under the "shares any one file" guard, passes with set equality.
- `test_same_set_in_a_different_order_is_still_a_duplicate` — **new**; proves
  order independence.

### Caveat / environment

The literal command in the brief (`.venv/bin/python -m pytest tests/
--ignore=tests/test_security_http.py -q`) currently shows 10 failures **only
because `.env` sets `SAU_ASYNC_PREP=1`** — 6 in `test_publish_center.py` and 4
in `test_inbox_both.py`. I did not edit `.env` (outside the allowed file set,
and it is active shared state for a concurrent async-prep session) nor
`tests/test_inbox_both.py`. With the flag off (its default, and what the suite
assumes), the suite is fully green. The robust root-cause fix is to stop the
repo `.env` from leaking into tests (pin `SAU_ASYNC_PREP=0` in
`tests/conftest.py` or skip `load_repo_env()` under pytest), which is a
separate change from this guard fix.

No `generated/` `PermissionError` was hit on the final runs; the sync path mocks
media prep, and the async path was disabled.

## Live DB verification

Live DB: `db/database.db` (read-only queries only; nothing written).

- `file_record 1142` is in media groups `2552..2557`, each group's full set is
  just `{1142}`.
- `guard(account=127, group=2557)` -> **True** (matches the live target on
  group `2553`, whose set is also `{1142}`). All of `2552..2557` -> True.
- Existing unrelated group `15` (set `{17}`, no live target for 127) -> **False**.
- Brand-new absent group `987654321` -> **False**.
- `guard(account=99999, group=2557)` -> **False** (different account).

## Files changed

- `myUtils/publish_orchestrator.py` — set-equality duplicate guard + docstring.
- `tests/test_publish_center.py` — two new regression tests in
  `DuplicateGuardSameFileDifferentGroupTests`.
