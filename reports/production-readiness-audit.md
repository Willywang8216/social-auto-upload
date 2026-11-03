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

## Deliberately NOT done in this pass

1. **Python 3.12 runtime.** The Dockerfile and `Dockerfile.base` still pin
   3.10.19. The suite already runs on 3.12 in CI, but the production image and
   its base have not been rebuilt/moved, so the runtime change is unverified.
2. **`create_app()` independence.** It still returns the legacy global app.
   De-globalising ~158 route decorators and the handlers that read
   `app.config` directly is a real refactor, not a rename; it was not started
   rather than half-done.
3. **Full Linux release matrix + image smoke** for these commits. Not run.
4. **Deployment.** Production is untouched and still on `7d4ef0d…`.
5. **Frontend advisory graph** (9 high, transitive) — recorded, not upgraded.
6. **Container image scanning** — no scanner configured on this host; a gap,
   not a clean result.

## Open follow-ups (ordered)

1. Run the Linux 3.10/3.12 matrix, frontend build and exact-image smoke on
   these commits.
2. Move the production runtime to Python 3.12 and re-verify.
3. Refactor `create_app()` for real instance independence with an
   import-direction test.
4. Upgrade `pillow` and `fastmcp` major versions with their suites.
5. Add an image CVE scan to the release gate.
6. Deploy through the existing locked, drained, backed-up promotion path.
