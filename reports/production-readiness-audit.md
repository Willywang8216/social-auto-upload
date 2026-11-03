# SAU production-readiness audit — 2026-11-03

Companion to `reports/reliability-audit.md` (2026-11-01) and the workspace
handoff. This records what the 2026-11-03 hardening pass changed, what it
verified, and what it deliberately did **not** do.

Baseline when this pass started: VPS `7d4ef0d8322ee1c5f5881f10b76461d9b45cf496`,
local source `3b375d3bde4d3e403c38dd7b39801a5045c667ab`.

## Verdict

**Keep Flask + Vue 3 + SQLite.** SAU has genuinely strong foundations — durable
submissions, idempotency, fenced worker leases, provider receipts, distinct
uncertain-delivery states, Alembic migrations, isolated tests, revision-bound
deploys. The weakness was **inconsistent application**: the newer protections
were bypassed by older routes. This pass closed the highest-severity of those
bypasses rather than rewriting anything.

## What changed

| Area | Before | After |
|---|---|---|
| Route authorization | **0 of 158** routes enforced the role permission map; the UI hid controls a viewer could still call | `sau_app/security/authorization.py` enforces the existing map for session callers in one place; legacy bearer operators stay fully trusted |
| Deletion | state-changing **GET**; bytes unlinked before the row committed; `user_info` probed before `accounts` | DELETE-only (GET → 405); reference-guarded; row committed before bytes; explicit namespace; refuses on unknown delivery outcome |
| Media serving | any upload returned **inline** from the app origin (`.html`, `.svg`) | allow-listed media only; everything else is an opaque attachment |
| Outbound fetch | bare `urllib` request, no host validation | HTTPS-only, every resolved address public, every redirect re-validated, size + deadline bounded |
| Upload registration | client-supplied `public_url`/`size` trusted; insert failures swallowed | object HEAD-verified, URL derived from the backend, failures reported |
| Rate limiting / headers | none | per-process limiter on OAuth/upload/batch/credential routes; nosniff + CSP + Referrer-Policy |
| MCP | `db_path` arbitrary; non-loopback HTTP bind allowed | constrained to configured roots; non-loopback bind refused without an explicit flag |
| Media list read | `SELECT * FROM file_records` (unbounded) | SQL-paged; measured 12.92 ms/4 000 rows → 2.39 ms/500 rows |
| Dependencies | 15 advisory-bearing runtime packages | 13 upgraded; 2 explicitly accepted with follow-up recorded |

## Evidence

* 457 maintained Python files, 0 static errors.
* 545 tests pass across the affected suites after the dependency upgrade.
* `scripts/audit/dump_route_matrix.py --check` passes at 160 routes, pinned by
  a test so drift fails the suite.
* The 78 remaining local failures were **proven pre-existing** by stashing this
  work and re-running the identical files at the base commit — identical
  results, all environmental (missing `fcntl`, Windows file locks, absent
  `rclone`/`crontab`/`ffmpeg`).

## Deployment (2026-11-03)

**Deployed and verified.** Both roles run
`5feba347d1ca060fbd5ff3b648851e645e34c4a4`, image
`sha256:c85ca0f101293e057978eed018cca91c705f70f560714cd9d3d865502f649242`,
gate binding `26a6136f1948cd3456058e2883a2855e74807eb1b2aa801c52a0f7fd6d869506`.

| Gate | Result |
|---|---|
| Linux Python 3.10.19 | 2,859 passed, 2 skipped, 170 subtests |
| Linux Python 3.12.14 | 2,859 passed, 2 skipped, 170 subtests |
| Pinned Node 22.21.1 frontend | 130 tests, production build |
| Maintained Python static checks | 458 files, 0 errors |
| Offline browser smoke | passed on both interpreters |
| Exact-image non-root web/worker smoke | passed, network disabled |

**The Linux gate earned its keep.** It caught a defect the Windows suite
missed: the outbound-fetch rewrite had dropped the original's Content-Length
comparison, so a truncated download would have been accepted and written out as
a complete object. Fixed, with two new regression tests
(`test_fetch_rejects_a_truncated_body`, `test_fetch_accepts_an_exact_length_match`).

Live checks after activation: both roles healthy on the matched image/revision;
anonymous access rejected; bearer accepted; Google login disabled; watcher
revision matches; claims unpaused. `/getFiles` returns `total` with a bounded
page (`total: 3681`, page of 5, `hasMore: true`), confirming the pagination is
live. Against the pre-release backup: **0 missing targets, 0 changed schedules,
0 changed successful/cancelled history, 0 changed target destinations or media
references, 0 changed account identity/routing bindings.**

## Follow-up pass (2026-11-03, later)

Closed the four gaps this audit left open.

| Area | Before | After |
|---|---|---|
| Production runtime | Python 3.10.19 (end-of-life Oct 2026) | **3.12.14**; base rebuilt and pushed, `requires-python >=3.12,<3.14`, gate matrix 3.12 + 3.13 |
| `pillow` / `fastmcp` | 11.2.1 / 3.4.7, majors deferred | **13.0.0 / 4.1.2**; the exported locked runtime graph is now at **0 advisories** |
| Container image scanning | no scanner; recorded as a gap | `scripts/audit/image_cve_scan.sh` plus a `release-gates.sh image` mode, also run by `all` |
| Query plans | only the two paths named in the plan | `EXPLAIN QUERY PLAN` audit of the interactive reads; `file_records(file_path)` and `(filename)` indexed - **46×** and **17×** faster |

### The image scan found a real defect

The app image carried a second, unreachable Python environment: the base image
installs its own pinned dependency list into the system interpreter, and 1,295
stale distributions survived into the final image. The app venv could not import
any of it, but scanners reported it as live. The Dockerfile now deletes it and
asserts at build time that the app's `sys.path` cannot see it.

Scoped to what the runtime actually imports (`/opt/venv`), the previous image
had **0 CRITICAL and 0 HIGH**. The current image's whole-image totals fall from
12/301/481 to 3/227/414 (CRITICAL/HIGH/MEDIUM); everything left is Debian 13.7
OS packages plus the bundled `uv` build tool. Details and the gate policy are in
`reports/security-advisories-2026-11-03.md`.

### Deployment (2026-11-03, later)

**Deployed and verified.** Both roles run
`ea125a0ded166979e223bbf703d388c1323e8d38`, image
`sha256:97eec960f970efbb3987957caa7a7a74dcd05f3011085b06db9d18221570af46`.

| Gate | Result |
|---|---|
| Linux Python 3.12.14 | 2,859 passed, 2 skipped, 170 subtests |
| Linux Python 3.13.15 | 2,859 passed, 2 skipped, 170 subtests |
| Pinned Node 22.21.1 frontend | 130 tests, production build |
| Maintained Python static checks | 459 files, 0 errors |
| Image CVE scan | 0 CRITICAL/HIGH in language packages |
| Non-root, network-disabled image smoke | passed |

**The gate earned its keep again.** It caught three real consequences of the new
migration: two tests pin the Alembic head deliberately and needed updating, and
`test_release_static_invariants` rejects the literal strings `pip install` and
`requirements.txt` anywhere in the app Dockerfile - the explanatory comment
about the base image tripped it, so the comment was reworded rather than
relaxing the guard.

Live checks after activation: both roles healthy on the matched image and
revision; `readyz` reports database and worker ok; `release_probe` passes for
both roles; anonymous `/getFiles` rejected (401); Google login disabled
(`SAU_GOOGLE_LOGIN_ENABLED=false`, `SAU_AUTH_MODE=legacy`); claims unpaused; the
live schema is at `0025_file_record_path_indexes` and the live query plan uses
`COVERING INDEX idx_fr_file_path`. Against the pre-release snapshot: **0 added,
0 removed, 0 changed rows** across `publish_jobs`, `publish_job_targets`,
`campaigns`, `campaign_posts`, `file_records`, `accounts`, `media_groups` and
`publish_submissions`.

## Still open

1. **Frontend advisory graph** (9 high, transitive). `npm audit fix --force`
   broke 22 frontend tests, so it needs its own reviewed increment.
2. **Debian OS advisories in the base image**, including 3 CRITICAL
   (`libxml2`, `rclone` ×2). Debian publishes no fixed version yet; they are
   recorded by the scan rather than gated on.
3. **p95/p99 latency under concurrency** - still unmeasured; needs a load
   generator.
4. **The watcher cannot deploy.** `ci-watch.sh` fetches `origin/main`, and the
   live checkout is 15 commits ahead of it, so every tick logs "local HEAD has
   unpublished/divergent history; refusing to overwrite it". That is the
   intended protection, but it means releases currently run through a manual
   `deploy-local.sh` invocation until the history is pushed with authorization.
