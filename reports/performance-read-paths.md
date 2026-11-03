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

## Broader query-plan audit (2026-11-03, live database)

`EXPLAIN QUERY PLAN` over the interactive read paths, run against a read-only
copy of the production database (111.6 MB; 3,690 `file_records`, 11,369
`publish_jobs`, 11,312 `publish_job_targets`, 2,979 `campaigns`).

Clean, no change needed:

* `files.page` and `jobs.page` — `ORDER BY id DESC LIMIT n` walks the rowid
  b-tree backwards and stops after the page, so the reported `SCAN` is O(page),
  not O(table).
* `jobs.by_account_ref`, `jobs.by_platform`, `targets.status_by_job`,
  `campaigns.page_ws`, `backends.default`, `files.by_id`, `jobs.payload_by_id`,
  `targets.status_counts`, `jobs.status_counts` — indexed or covering-index
  lookups.
* `jobs.by_schedule` — scans `publish_jobs` but probes the indexed
  `publish_job_targets(job_id)` per row and stops at the 500-row bound.
* `files.by_storage_key_like` — a leading-wildcard `LIKE` cannot use an index by
  construction; it stays a bounded scan.

Fixed. `file_records.file_path` and `file_records.filename` carried no index, so
every media resolution full-scanned the table. Measured over 60 references
sampled randomly from the production table:

| Lookup | Before | After | Gain |
|---|---:|---:|---:|
| `SELECT id FROM file_records WHERE file_path = ?` (60 lookups) | 21.44 ms | 0.46 ms | **46×** |
| entity `file_path = ? OR filename = ? LIMIT 1` (60 refs) | 11.35 ms | 0.67 ms | **17×** |

Migration `0025_file_record_path_indexes` adds both indexes additively. The OR
query now reports `MULTI-INDEX OR`; the equality query uses a covering index.
An earlier sample drawn from the *first* 60 rows made this look like a 1.2×
gain, because those rows match early and the scan stops; sampling across the
table is what makes the number honest.

## Not measured

* p95/p99 latency and memory under real concurrency (single-process web tier;
  needs a load generator, which was out of scope for this pass).
* Write-path cost of the two new indexes. At 3,690 rows it is not measurable,
  and `file_records` is append-mostly, so it stays negligible until the table
  grows by orders of magnitude.
