# Async-prep Step 1 — media-prep extraction notes

Scope: extract the ~900 s media preparation out of the Flask monolith
(`sau_backend._prepare_campaign_media_artifacts`) into a Flask-free module so a
worker process can call it without a request context. Refactor only; no
behaviour change on the request path.

## Important: filename collision (why the module is not `myUtils/campaign_prep.py`)

A **parallel agent session** is concurrently building the async prep *queue*
("step 2") and had already created `myUtils/campaign_prep.py` (untracked) with a
different API:

* `finalize_campaign(...)`, `run_campaign_prep(...)`, `make_default_prep_runner()`
* imported by `myUtils/worker.py` (`from myUtils import campaign_prep`)
* `make_default_prep_runner` lazily imports `_prepare_campaign_media_artifacts`
  from `sau_backend`

That file was being written while I worked (mtimes 18:02:45 and
`myUtils/worker.py` 18:03:27, with edits to `myUtils/campaigns.py` adding
`finish_campaign_prep`). Overwriting it would have broken `worker.py` and
violated "keep tests green" and the instruction not to disturb files held by
others.

I therefore put the extraction in a collision-free module,
**`myUtils/campaign_media_prep.py`**, and did **not** modify
`myUtils/campaign_prep.py`. To preserve the other session's work verbatim, a
byte-identical backup is at
`logs/campaign_prep.py.parallel-session-backup` (md5
`fd10e16363f330f797cd7ec57796d0f7`, same as the live file at the time of
writing). `sau_backend.py` imports the extraction from
`myUtils.campaign_media_prep`.

If the intended end state is a single `myUtils/campaign_prep.py`, the two
modules can be merged later; the extracted function is self-contained and
cycle-free.

## What moved

Moved into `myUtils/campaign_media_prep.py` (all Flask-free):

| Helper | Referenced by extracted code | Other `sau_backend` users |
| --- | --- | --- |
| `prepare_campaign_media_artifacts` (was `_prepare_campaign_media_artifacts`) | entry point | wrapper only |
| `_derive_watermark_spec` | yes | 2 (routes) |
| `_resolve_file_record_path` | yes | none |
| `_download_file_from_storage` | yes | none |
| `_is_image_file`, `_is_video_file` | yes | 2 |
| `_shrink_for_publish` | yes | tests only |
| `_assert_within_platform_caps` | via `_shrink_for_publish` | tests only |
| `_load_storage_backend_by_id` | via `_download_file_from_storage` | 3 |
| `_resolve_ai_config` | yes | 1 (`_generate_platform_draft`) |
| `_resolve_video_file_path_safely` | via `_download_file_from_storage` | 8 + tests + one lazy import elsewhere |

Grep evidence (before the change), per name, in `sau_backend.py`:

```
_download_file_from_storage   3283 def, 3611, 3655      -> only the extracted fn
_resolve_file_record_path     3551 def, 3601            -> only the extracted fn
_shrink_for_publish           3461 def, 3686            -> only the extracted fn
_assert_within_platform_caps  3506 def, 3502            -> only _shrink_for_publish
_is_image_file                3453 def, 3690, 3984, 7941
_is_video_file                3457 def, 3663, 3685, 3708, 3739, 3793, 3810, 3910, 3991, 7939
_derive_watermark_spec        3533 def, 3580, 6734, 6819
_resolve_ai_config            4186 def, 3999, 4206
_load_storage_backend_by_id   3241 def, 3214, 3321, 3348
_resolve_video_file_path_safely 342 def, 1320, 1377, 3294, 3383, 3411, 7805, 7849
```

## What stayed in `sau_backend.py`

* `_prepare_campaign_media_artifacts` — now a thin wrapper with the **same
  signature** that passes `request.host_url` only when a request context exists
  (`has_request_context()`), else `None`. All callers unchanged
  (`campaigns_prepare`, `publish_center_submit`, `inbox_item_publish`, and the
  `prepare_artifacts=` orchestrator callback).
* Everything else, including the helpers not referenced by the extracted code
  (`_delete_file_from_storage`, `_cleanup_local_files`,
  `_ensure_file_record_for_path`, `_load_media_group_files`, ...).
* The moved helper names are re-exported into `sau_backend` by importing them
  from `myUtils.campaign_media_prep`, so every existing call site and test that
  uses `sau_backend.<helper>` keeps working. No cycle: no `myUtils` module
  imports `sau_backend`.

One subtlety: `tests/test_security_hardening.py` patches `sau_backend.BASE_DIR`
and calls `_resolve_video_file_path_safely`. The implementation moved, so the
shared resolver now takes an optional `base_dir=` and `sau_backend` wraps it
with its own live `BASE_DIR`; the module is defined once (no duplicated path
validation). Verified by the previously failing security tests.

## The one Flask touchpoint that was removed

Before (inside the `else:` "no remote upload" branch):

```python
from flask import request as _flask_request
base_url = (
    _ops_alerts.public_app_origin()
    or _flask_request.host_url.rstrip("/")
).rstrip("/")
```

Outside a request this raised `RuntimeError`, the URL was dropped, and a later
fallback branch could `NameError` (`base_url` undefined, swallowed by
`except (RuntimeError, NameError)`).

After, in `campaign_media_prep.prepare_campaign_media_artifacts`:

```python
def prepare_campaign_media_artifacts(..., public_base_url: str | None = None):
    ...
    base_url = (
        ops_alerts.public_app_origin()
        or (public_base_url or "")
    ).rstrip("/")
    if not base_url:
        raise RuntimeError("no public origin configured")   # caught -> URL stays None
```

Ordering is identical to the request path: configured public origin first, then
the passed host. The raw-artifact branch now requires `base_url` before building
a URL, so an absent origin yields `None` rather than a bogus relative
`/getFile?...`.

## How no-request-context is proved

`tests/test_campaign_media_prep.py` (4 tests, all passing):

1. `test_module_is_flask_free` — `inspect.getsource(campaign_media_prep)` contains
   no `import flask` / `from flask`, and the module has no `flask` attribute.
2. `test_runs_with_no_request_context_and_explicit_base_url` — asserts
   `flask.has_request_context() is False`, then calls the function with
   `public_base_url="https://cdn.example.com"` and checks the emitted
   `/getFile` URL.
3. `test_no_origin_leaves_the_url_unset` — `public_base_url=None` and
   `ops_alerts.public_app_origin() == ""` → no exception, `public_url is None`,
   `imageUrls == []`.
4. `test_configured_public_origin_wins_over_passed_host` — confirms the
   request-path precedence is preserved.

## Test result

```
.venv/bin/python -m pytest tests/ -q
1342 passed, 1 skipped, 92 subtests passed in 293.40s
```

Baseline was 1309 passed, 1 skipped; the delta is the 4 new tests here plus the
parallel session's own additions, all green. The pre-existing
`test_security_hardening.py` and `test_sau_backend.py` suites (which exercise
the moved helpers) pass unchanged.

## Files changed

* Added `myUtils/campaign_media_prep.py` (Flask-free extraction).
* Added `tests/test_campaign_media_prep.py`.
* Modified `sau_backend.py`: thin wrapper + helper imports; removed the moved
  function bodies.
* Added `logs/async-prep-extract-notes.md` (this file) and
  `logs/campaign_prep.py.parallel-session-backup`.
* **Not modified:** `myUtils/campaign_prep.py`, `myUtils/media_prep.py`,
  `myUtils/publish_orchestrator.py`, `myUtils/worker.py` (parallel session /
  task constraints).
