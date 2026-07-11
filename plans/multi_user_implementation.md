# Multi-User Implementation Plan (living document)

Tracks the conversion of `social-auto-upload` from a shared-token single-tenant
app into a multi-tenant SaaS. Updated at the end of every work session with
exact commands, test results, modified files, remaining risks, and the next
incomplete task. **No phase is "complete" because code compiles — completion
requires reproducible evidence.**

- Design: [`docs/architecture/target-multi-tenant.md`](../docs/architecture/target-multi-tenant.md)
- Current state + defects: [`docs/architecture/current-state.md`](../docs/architecture/current-state.md)
- Threat model: [`docs/security/threat-model.md`](../docs/security/threat-model.md)
- Rollout/rollback: [`docs/migrations/multi-tenant-rollout.md`](../docs/migrations/multi-tenant-rollout.md)

## Status legend

`✅ complete` · `🚧 in progress` · `⛔ blocked` · `⬜ not started`

Each phase records: goal · entry criteria · exit criteria + **evidence** ·
feature flags flipped · rollback. A phase is not done until its evidence links
to command output, test results, migration reports, or screenshots.

## Roadmap (maps to the required PR sequence)

| PR | Phase | Title | Status |
|----|-------|-------|--------|
| 1 | 0 | Audit documents and baseline tests | ✅ complete |
| 2 | 1 | Application factory and production server (Gunicorn) | ⬜ |
| 3 | 2 | PostgreSQL and repository foundation | ⬜ |
| 4 | 3 | Google OIDC and sessions | ⬜ |
| 5 | 4 | CSRF and authorization (AuthContext, roles) | ⬜ |
| 6 | 5 | Workspace schema and legacy backfill | ⬜ |
| 7 | 6 | Route-by-route tenant isolation | ⬜ |
| 8 | 7 | Credential encryption and response redaction | ⬜ |
| 9 | 8 | Tenant-aware object storage | ⬜ |
| 10 | 9 | Redis queues and separate workers | ⬜ |
| 11 | 10 | Frontend auth conversion | ⬜ |
| 12 | 11 | CI, observability, and deployment hardening | ⬜ |

> Phases 3 and 4 are grouped in the original spec's PR sequence (OIDC+sessions,
> then CSRF+authorization). They are kept as separate rows here for tracking.

## Phase 0 — Audit, baseline, isolation design ✅

**Goal:** produce committed audit inventories, recorded baseline runs, and the
isolation/compatibility design before any feature work. Zero behavioral code
change.

**Entry criteria:** fresh clone on `claude/multi-user-google-auth-kaj0of`,
baseline commit `227c050`.

**Exit criteria + evidence:**
- Baseline recorded — [`reports/baseline-verification.md`](../reports/baseline-verification.md):
  517 backend tests pass; `test_security_http.py` passes but is isolation-unsafe
  (D-11); frontend build passes; `npm test` has one pre-existing failing
  Playwright-spec file (F-1); `docker compose config` needs `.env` (D-14).
- Route matrix — all 137 routes annotated, verified by
  `uv run python scripts/audit/dump_route_matrix.py --check` (exit 0).
- Inventories — 6 CSVs under `reports/` valid via
  `uv run python scripts/audit/check_csvs.py` (exit 0).
- Docs — current-state (defects D-1..D-20), target design (keystone), threat
  model, rollout plan committed under `docs/`.
- No behavior drift — CI-parity pytest re-run matches baseline (see below);
  `git diff` vs base shows only added files under
  `scripts/audit/ reports/ docs/ plans/`.

**Evidence commands:**
```
uv run pytest tests/ --ignore=tests/test_security_http.py -q   # 517 passed
uv run python scripts/audit/dump_route_matrix.py --check        # route matrix OK: 137 routes
uv run python scripts/audit/check_csvs.py                        # all report CSVs valid
```

**Commits:** `9b661d3` (audit scripts), `1ab974a` (inventories + baseline),
`fe63434` (docs), plus this plan.

**Flags flipped:** none. **Rollback:** documentation/tooling only; deleting the
new files fully reverts.

## Phase 1 — Application factory and production server ⬜

**Goal:** `create_app()`, validated config, extensions module, domain
blueprints (no behavior change), standard error schema, request/correlation
ids, structured logging, readiness/liveness endpoints, Gunicorn entrypoint.
`sau_backend.py` becomes a thin compatibility wrapper; no long-running publish
runs inline in the API after the worker phase.
**Exit evidence (planned):** existing tests green under the factory; gunicorn
boots; health endpoints return 200. **Rollback:** keep `app.run` path behind a
flag until gunicorn is proven.

## Phase 2 — PostgreSQL and repository foundation ⬜

**Goal:** SQLAlchemy 2.x models + workspace-scoped repositories; Postgres in
dev/test compose; Alembic as the only schema authority (converge D-8); migrate
one domain at a time from raw SQLite. **Exit evidence (planned):** empty-DB
upgrade, legacy-fixture upgrade, and current-shape upgrade all pass; repository
integration tests green on Postgres. **Rollback:** `SAU_DATABASE_MODE=sqlite`.

## Phase 3 — Google OIDC and sessions ⬜

**Goal:** Authlib Google OIDC (`openid email profile` only), separate from
YouTube; `users`/`auth_identities`/`workspaces`/`workspace_members`/`sessions`;
first-login transactional workspace creation keyed by `sub`; `/auth/google/*`,
`/api/v1/session`, `/api/v1/logout`; HttpOnly session cookie; session-id
rotation. **Rollback:** `SAU_AUTH_MODE=legacy`.

## Phase 4 — CSRF and authorization ⬜

**Goal:** auth middleware + `AuthContext`, permission decorators, role checks,
CSRF + Origin validation, secure cookie config, session expiry/revocation;
401/403/404 discipline (do not clear session on a normal 403). **Rollback:**
`SAU_TENANCY_MODE=shadow`.

## Phase 5 — Workspace schema and legacy backfill ⬜

**Goal:** expand→backfill→constrain per the rollout doc; legacy workspace +
claim flow; orphan/count reports; workspace-scoped uniqueness; optional RLS.
**Exit evidence (planned):** zero orphans; pre/post counts equal; rollback
tested. **Rollback:** downgrade revisions; re-run idempotent backfill.

## Phase 6 — Route-by-route tenant isolation ⬜

**Goal:** replace unscoped `list_*`/`get_*` with workspace-scoped repositories
across all 137 routes (matrix `planned_workspace_scope`). **Exit evidence
(planned):** the two-user isolation matrix (A can/cannot reach B by known id;
unauth denied; role denials) passes for profiles, accounts, media, campaigns,
templates, jobs, logs, analytics, storage, OAuth status, exports.

## Phase 7 — Credential encryption and response redaction ⬜

**Goal:** move secret account fields into an encrypted `account_credentials`
store (versioned envelope encryption, mandatory in prod, fail closed); remove
`_account_payload` secret leakage (D-4); remove/gate cookie export (D-2/D-3);
redact logs. **Rollback:** feature flag gating the new store while dual-writing.

## Phase 8 — Tenant-aware object storage ⬜

**Goal:** private bucket, server-generated workspace-prefixed keys, upload
reservation + presigned PUT + `HEAD`-validated completion, quotas, magic-byte +
`ffprobe` validation, authorized/presigned downloads; audit/remove public
`/getFile` and thumbnail paths.

## Phase 9 — Redis queues and separate workers ⬜

**Goal:** separate API/worker deployments; Redis-backed queues; tenant ids in
every task; workers re-verify ownership; distributed account locks
(`lock:workspace:{workspace_id}:account:{account_id}`); workspace-scoped
idempotency; dead-letter + crash recovery; tenant-aware maintenance scans.

## Phase 10 — Frontend auth conversion ⬜

**Goal:** "Continue with Google", auth Pinia store, session bootstrap,
`withCredentials`, CSRF header, role-aware UI, logout + expiry handling,
workspace selector, redacted account DTOs; remove localStorage token,
`appendAuthQuery`, wildcard popup handling; fix 401/403 split (D-15) and popup
origin check (D-16).

## Phase 11 — CI, observability, deployment hardening ⬜

**Goal:** CI runs format, lint, type-check, unit + Postgres + Redis + tenant
isolation + migration tests, frontend lint/type-check/unit/build, Playwright
e2e, and security scans (gitleaks, pip-audit, npm audit, container scan).
**Re-enable `tests/test_security_http.py`** (fix its shared-`BASE_DIR`
isolation, D-11) and fix the vitest/Playwright collection issue (F-1).
Production: gunicorn, separate worker containers, non-root, private storage,
backups, health checks, HSTS/CSP/secure cookies, trusted hosts/proxy, rate
limits, quotas, structured logs, audit logs.

## Decision log

- 2026-07-10 — Phase 0 delivered as docs/inventories/baseline only; no behavior
  change; three-commit structure on `claude/multi-user-google-auth-kaj0of`.
- Identity keyed by Google `sub`, not email (see target design §15).
- Compatibility-first: current install = tenant zero via a claim flow; three
  orthogonal mode flags (auth/tenancy/database).

## Remaining risks / open items

- CI only triggers on push/PR to `main`; the branch alone won't run CI — a
  draft PR is used to obtain CI evidence.
- `test_security_http.py` and the vitest/Playwright collision (F-1) are known,
  root-caused, and deferred to Phase 11 (not fixed in Phase 0).
- Postgres cutover timing vs enforcement, and shared/team-workspace timing, are
  open (target design §16).

## Changelog

- 2026-07-10 — Phase 0 complete. Commits `9b661d3`, `1ab974a`, `fe63434` +
  this plan.

## Next incomplete task

Phase 1 — introduce `create_app()` and a Gunicorn entrypoint without changing
API behavior, keeping `sau_backend.py` importable as a thin wrapper.
