# Fix: media-restore host/container path mismatch

Date: 2026-10-09
Scope: `myUtils/worker.py`, `tests/test_worker_media_restore.py`
Constraint: resolution only — no DB rows were mutated, no container restart, no commit.

## 1. Inventory (read-only)

Host repo root is `/home/will/social-auto-upload`; the app's `BASE_DIR` inside the
container is `/app` (Dockerfile `WORKDIR /app`, compose mounts `./videoFile` at
`/app/videoFile`, etc.).

`campaign_artifacts.local_path`:

| shape | count |
| --- | --- |
| container-prefixed (`/app/...`) | 2476 |
| host-prefixed (`/home/will/...`) | 30 |
| relative/other | 0 |

Host-prefixed artifacts break down as:

| stored `local_path` | count |
| --- | --- |
| `/home/will/social-auto-upload/videoFile/_inbox_cache/demo.mp4` | 25 |
| `/home/will/social-auto-upload/videos/demo.mp4` | 5 |

No other absolute roots appear in `campaign_artifacts`.

`file_records.file_path`:

| shape | count |
| --- | --- |
| container-prefixed (`/app/...`) | 97 |
| host-prefixed (`/home/will/...`) | 1 (`/home/will/social-auto-upload/videoFile/_inbox_cache/demo.mp4`) |
| relative/other | 1011 (the normal convention, e.g. `videoFile/...`, `_batch1/...`) |

`publish_jobs.payload_json` (the payload is `payload_json`, not `payload`):

| shape | count |
| --- | --- |
| jobs whose JSON contains `/home/will/` | 55 |
| `artifacts[].local_path = .../videoFile/_inbox_cache/demo.mp4` | 35 |
| `artifacts[].local_path = .../videos/demo.mp4` | 20 |

`publish_job_targets.file_ref`:

| shape | count |
| --- | --- |
| host-prefixed | 0 |
| container-prefixed | 0 |
| relative | 5645 |

So the bug is confined to artifact `local_path` values (and the one
`file_records` row), not to `file_ref`. Live failure count at inventory time:

| `publish_job_targets.last_error` | count |
| --- | --- |
| `%Could not restore artifact%/home/will/%` | 12 |
| `%Artifact restore failed for /home/will/%` | 3 |

Job status for the 55 affected payloads: 12 pending, 22 failed, 21 cancelled.

## 2. Why it happened

`_ensure_artifact_paths_local` (and `_record_for` / `_resolve_file_path` /
`_prepared_artifact_local_paths`) worked from `artifact["local_path"]` verbatim.
Rows written by host-side tooling carry the host absolute prefix; the worker
tries to `mkdir`/write `/home/will/...` inside the container, where `/home/will`
does not exist. Running as uid 1000, `Path.mkdir(parents=True)` on an ancestor
that cannot be created surfaces as `EACCES` (`Permission denied: '/home/will'`),
so a path bug read as a permissions bug.

This was the third variant of the same class (previous prefix fixes lived in the
prep path), so the fix is one canonical normaliser rather than a fourth special
case.

## 3. The normaliser

Added to `myUtils/worker.py`:

- `_host_base_dirs()` — reads `SAU_HOST_BASE_DIRS` (comma-separated, default
  `/home/will/social-auto-upload`) and returns the host repo roots to map from.
- `_resolve_media_path(value)` — the single canonical normaliser. For an
  absolute path under a known host root it returns `BASE_DIR / relative`; for a
  container-prefixed path, a relative path (`videoFile/_batch1/x.mp4`,
  `uploads/...`), or any unrelated absolute path (`/tmp/...`) it returns the
  value unchanged. `BASE_DIR` is read at call time, so tests that patch
  `worker.BASE_DIR` (and a future container whose `BASE_DIR` is not `/app`) work.
  `/app` is never hardcoded; the container path is derived from `BASE_DIR`.

### Where it is applied

Every place a stored path becomes a real filesystem path in the worker:

- `_ensure_artifact_paths_local` — artifact `local_path` → `p`, before `exists()`,
  `mkdir`, downloads, and record lookups. `_record_for` now also receives the
  original `stored` string so exact `campaign_artifacts.local_path` lookups match
  either the host or the normalised shape.
- `_prepared_artifact_local_paths` — paths handed to the prepared publishers
  (Twitter/Reddit/Patreon cookie paths, etc.).
- `_fallback_media_paths` — local paths handed to the Sociamonials fallback.
- `_resolve_file_path` — `target.file_ref` / thread refs.
- `_try_download_from_storage` — derives repo-relative lookup refs and the
  download destination from relative, container-absolute, or host-absolute refs.
- `_resolve_account_path` — structured `cookie_path` and legacy `account_ref`.
- `_restore_optional_thumbnail` — payload `thumbnail`.

Also, `_record_for`'s generated-artifact absolute file_record lookup previously
hardcoded `/app/generated/`; it now derives the absolute form from `BASE_DIR`.

## 4. Error-message change

`_ensure_artifact_paths_local` now catches `PermissionError` separately from the
generic failure. If EACCES happens while preparing a missing path it raises:

```
Could not restore artifact '<stored>': after normalisation it resolves to
<resolved>, which could not be found and could not be created ([Errno 13]
Permission denied: '...'). The stored path may use a host prefix that does not
map onto BASE_DIR (<BASE_DIR>).
```

instead of the old `Artifact restore failed for /home/will/...: [Errno 13]
Permission denied: '/home/will'`. A genuinely missing artifact with no record
still fails fast with the existing explicit message
`Artifact <path> is missing locally and has no file record`.

## 5. Tests

Added to `tests/test_worker_media_restore.py`:

- `test_resolve_media_path_rewrites_host_prefix_to_base_dir`
- `test_resolve_media_path_leaves_container_prefix_untouched`
- `test_resolve_media_path_leaves_relative_path_untouched`
- `test_resolve_media_path_leaves_unrelated_absolute_path_untouched`
- `test_host_prefixed_artifact_restores_under_base_dir` (end-to-end restore)
- `test_missing_artifact_after_normalisation_raises_clear_message`
- `test_permission_error_on_missing_path_is_reported_as_path_problem`

Results:

- `pytest tests/test_worker_media_restore.py -q` → 20 passed.
- Worker/jobs focused run → 158 passed, 9 subtests passed.
- `pytest tests/ -q` → 1395 passed, 1 skipped, 105 subtests passed.
  (The count exceeds the 1353 baseline because other agents added tests
  concurrently. Two intermediate full-suite runs showed only transient failures
  in files another agent was editing mid-run — `test_prepared_publishers.py`,
  `test_sociamonials_fallback.py`, `test_media_prep.py` — which pass in
  isolation and on the final run.)

## 6. Data migration — proposed, NOT run

The code fix makes the worker tolerant of both shapes, so the DB does not need to
change for publishing to work. If the stored data should be normalised anyway
(e.g. so health queries and future tools stop seeing host paths), these are the
exact statements. Back up `db/database.db` first and run inside the container /
against the same `SAU_DB_PATH`.

Preview:

```sql
SELECT COUNT(*) AS artifacts_host FROM campaign_artifacts
  WHERE local_path LIKE '/home/will/social-auto-upload/%';
SELECT file_path FROM file_records
  WHERE file_path LIKE '/home/will/social-auto-upload/%';
SELECT COUNT(*) AS jobs_host FROM publish_jobs
  WHERE payload_json LIKE '%/home/will/social-auto-upload/%';
```

Migrate (container `BASE_DIR` is `/app`; substitute the real `BASE_DIR` for a
non-container deploy). `campaign_artifacts` and job payloads use the
container-absolute convention; `file_records.file_path` uses the repo-relative
convention:

```sql
-- campaign_artifacts: /home/will/social-auto-upload/... -> /app/...
UPDATE campaign_artifacts
SET local_path = '/app/' || substr(local_path, length('/home/will/social-auto-upload/') + 1)
WHERE local_path LIKE '/home/will/social-auto-upload/%';

-- file_records: strip the host prefix entirely (repo-relative convention)
UPDATE file_records
SET file_path = substr(file_path, length('/home/will/social-auto-upload/') + 1)
WHERE file_path LIKE '/home/will/social-auto-upload/%';

-- publish_jobs payload_json: replace the prefix in every embedded artifact path
UPDATE publish_jobs
SET payload_json = replace(payload_json, '/home/will/social-auto-upload/', '/app/')
WHERE payload_json LIKE '%/home/will/social-auto-upload/%';
```

`file_records` and `campaign_artifacts` may be regenerated by prep/offload, so
prefer migrating only `campaign_artifacts` and `publish_jobs` if in doubt.
