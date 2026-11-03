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

## Still open

1. **Python 3.12 as the production runtime.** The suite passes on 3.12, but the
   production image and its base still build on 3.10.19. Moving the runtime
   needs a base-image rebuild and its own verification.
2. **`pillow` 11→12 and `fastmcp` 3→4** — major bumps, explicitly accepted with
   rationale in `reports/security-advisories-2026-11-03.md`.
3. **Frontend advisory graph** (9 high, transitive) — recorded, not upgraded.
4. **Container image CVE scanning** — no scanner configured on this host; a gap,
   not a clean result.
5. **Broader query-plan audit** — only the two paths named in the plan were
   measured; see `reports/performance-read-paths.md`.
