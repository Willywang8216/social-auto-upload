# Read-path measurements — Phase 1/2 hardening (2026-11-03)

Scope: the two read paths the hardening plan called out as unbounded, measured
on a **synthetic** database at production scale. No production data, no
production load.

Method: `bench_getfiles.py` (session scratch) bootstraps a migrated database,
inserts 4,000 `file_records` rows, and times the previous query shape against
the new one. Seven repetitions; the median is reported.

| Path | Rows returned | Median |
|---|---:|---:|
| `/getFiles` — previous (`SELECT *` + Python conversion) | 4,000 | **12.92 ms** |
| `/getFiles` — bounded (`COUNT` + `LIMIT 500 OFFSET 0`) | 500 | **2.39 ms** |

**5.4× faster, 3,500 fewer rows serialised per request.** The live table held
3,671 rows when this work started and grows with every upload, so the previous
shape degraded continuously while the new one does not.

## What changed

`/getFiles` now:

* accepts `limit` / `offset` (default 500, capped at 2,000 — an absurd value is
  clamped rather than honoured),
* orders `id DESC` so the newest media is on page one,
* returns `total`, `limit`, `offset` and `hasMore` alongside the existing
  `data` array, so the response shape stays backward compatible,
* rejects a non-integer `limit`/`offset` with 400 instead of raising.

## `/publish-entities`

Already bounded before this work, and verified rather than changed:
`_load_publish_entity_jobs` filters and pages its candidate set in SQL
(`ORDER BY j.id DESC LIMIT ?`, `_ENTITY_JOB_SCAN_LIMIT = 500`), and campaign
candidates are bounded by `_ENTITY_CAMPAIGN_SCAN_LIMIT = 500`. The Python work
that follows (hydrating targets, merging two bounded sources, sorting, paging)
therefore operates on a bounded candidate set, not on whole tables.

`tests/test_media_lifecycle.py::test_publish_entity_candidates_are_sql_bounded`
inserts `_ENTITY_JOB_SCAN_LIMIT + 25` jobs and asserts the loader returns no
more than the bound — so a future change that removes the SQL `LIMIT` fails the
suite rather than silently reintroducing a full scan.

## Not measured

* p95/p99 latency and memory under real concurrency (single-process web tier;
  needs a load generator, which was out of scope for this pass).
* `EXPLAIN QUERY PLAN` review of every query. The two paths named in the plan
  were the targets; a broader query-plan audit remains open work.
