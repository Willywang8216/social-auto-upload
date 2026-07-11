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
| 2 | 1 | Application factory and production server (Gunicorn) | ✅ complete |
| 3 | 2 | PostgreSQL and repository foundation | ✅ foundation complete |
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
`fe63434` (docs), `75f4faf` (this plan).

**Pull request:** [#27 (draft)](https://github.com/Willywang8216/social-auto-upload/pull/27)
— opens the required PR sequence; kept draft (Phase 0 is docs/tooling only).
CI runs on the PR (the branch alone does not trigger it).

**CI result:** all green on head `15d2123`
([run 29138591047](https://github.com/Willywang8216/social-auto-upload/actions/runs/29138591047))
— `backend-tests` ✅, `frontend-build` ✅, `dependency-guard` ✅.

**Flags flipped:** none. **Rollback:** documentation/tooling only; deleting the
new files fully reverts.

## Phase 1 — Application factory and production server ✅

**Goal:** `create_app()`, validated config, extensions module, standard error
schema, request/correlation ids, structured logging, readiness/liveness
endpoints, Gunicorn entrypoint — **without changing existing route behavior**.

**Delivered (new `sau_app/` package + `wsgi.py`):**
- `sau_app/config.py` — `AppConfig` + `load_config()` that **fails closed in
  production** (missing `SECRET_KEY`, open mode, or `DEBUG_MODE` on) and only
  warns in dev/test.
- `sau_app/observability.py` — request/correlation IDs (`X-Request-ID`, reused
  inbound or generated), a per-request structured log line, and a standard JSON
  error schema (`{code,msg,data,requestId}`) for unmatched 404 / 405 / uncaught
  500 (guarded to re-raise under TESTING so tests are unaffected).
- `sau_app/health.py` — `GET /healthz` (liveness) and `GET /readyz` (readiness,
  runs registered checks incl. a DB ping) blueprint.
- `sau_app/extensions.py` — idempotent `init_extensions(app)` +
  `register_readiness_check()`.
- `sau_app/__init__.py::create_app()` — wraps the monolith app for Gunicorn;
  `wsgi.py` exposes `app`. Dockerfile CMD now
  `gunicorn wsgi:app --workers 1 --threads 8` (single worker until Phase 9
  splits the in-process publishing threads). `python sau_backend.py` still works.
- `sau_backend.py` wiring is purely additive: `init_extensions(app)` after CORS
  (so request-id runs before the auth gate) + a `database` readiness probe.
  `/healthz` + `/readyz` added to `PUBLIC_PATHS` so they bypass the token gate.

**Exit evidence:**
- Full suite green: **535 passed, 22 subtests** (517 prior + 18 new
  `tests/test_app_factory.py`); no regressions. `test_security_http.py`: 12 passed.
- **Real Gunicorn boot** (protected mode): `/healthz`→200 (+`X-Request-ID`),
  `/readyz`→`{database:ok}` 200, `/whoami` 401 without token / 200 with token,
  inbound request-id echoed; zero tracebacks in the server log.
- Route matrix regenerated to **139** routes (`dump_route_matrix.py --check`
  exit 0); `check_csvs.py` exit 0.

**Flags flipped:** none (config validation only bites when `APP_ENV=production`).
**Rollback:** revert the Dockerfile CMD to `python sau_backend.py`; the factory
is otherwise additive.

## Phase 2 — PostgreSQL and repository foundation ✅ (foundation)

**Goal:** SQLAlchemy 2.x models + workspace-scoped repositories; Postgres in
dev/test compose; Alembic as the only schema authority (converge D-8).

**Delivered:**
- **D-8 fixed** (`db/createTable.py`): `bootstrap()` now stamps the revision the
  raw `CREATE TABLE` block implements (`RAW_SCHEMA_REVISION = 0014`) and then
  runs `alembic upgrade head`, so any migration newer than the raw block is
  **applied** rather than silently skipped by a blind head-stamp. Byte-identical
  today (head == 0014 → upgrade is a no-op); the future path is proven by a
  simulation (stamp 0013 → upgrade applies 0014) and 2 new `test_alembic.py`
  tests.
- **SQLAlchemy 2.x layer** (`sau_app/db/`): `base.py` (DeclarativeBase + naming
  convention), `models.py` (dialect-agnostic `Profile`/`Account` mapping the
  real columns), `engine.py` (`resolve_database_url` — `DATABASE_URL` wins, else
  the legacy SQLite file; `make_engine`/`get_sessionmaker`/`session_scope`;
  SQLite FK pragma).
- **Repository layer** (`sau_app/db/repositories/`): `WorkspaceScopedRepository`
  base (workspace scope is a threaded-through no-op until the `workspace_id`
  column exists, then it becomes the enforced filter for all subclasses at once)
  + `ProfileRepository`. Parallel to the raw `myUtils/profiles.py`; **no cutover
  yet** (domains migrate one at a time in later work).
- **PostgreSQL**: `postgres` extra (`psycopg[binary]`), a `postgres` service in
  `docker-compose.yml` (opt-in `--profile dev`, prod stack unchanged), and a new
  CI job `postgres-tests` with a `postgres:16` service running the repository +
  migration tests against real PostgreSQL.

**Exit evidence:**
- **546 passed, 1 skipped, 22 subtests** (11 new: `test_db_repositories.py`,
  extra `test_alembic.py`). The skip is the Postgres materialization test, which
  runs in CI where `TEST_DATABASE_URL` is set.
- Legacy interop proven both directions: a profile written via the SQLAlchemy
  repo is read identically by `myUtils.profiles` (raw sqlite), and vice versa.
- Models materialize via `metadata.create_all` on SQLite (local) and PostgreSQL
  (CI job).

**Not done here (incremental follow-ups):** cutting the live backend routes over
from raw `sqlite3` to the repositories (one domain at a time), modelling the
remaining ~27 tables, and making the 14 legacy SQLite-raw migrations
Postgres-compatible (the sqlite→postgres data cutover, per the rollout doc).
**Rollback:** `DATABASE_URL` unset → SQLite; the ORM layer is parallel/additive.

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
- 2026-07-11 — Phase 1 uses a **wrapping factory** rather than rewriting the
  7.7k-line monolith into blueprints in one step: `create_app()` returns the
  existing module-global `app` after idempotent `init_extensions()`. This keeps
  all 137 routes byte-identical while adding the production server, health,
  request-ids, and config validation. Blueprint decomposition of existing routes
  is deferred to later phases (moved incrementally, per the original plan).
- 2026-07-11 — Phase 1 committed to the **same** designated branch (PR #27) — the
  harness mandates development on `claude/multi-user-google-auth-kaj0of`, so the
  intended "separate PR per phase" is realized as clearly-labeled separate
  commits on the one branch rather than distinct PRs.

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
- 2026-07-11 — Phase 1 complete. `sau_app/` factory package, `wsgi.py`, Gunicorn
  entrypoint, health/readiness endpoints, request IDs, config validation. 535
  backend tests pass (18 new); real Gunicorn boot verified. Commits `31b28dd`,
  `3481c0b`. CI green on head `3481c0b`
  ([run 29154548808](https://github.com/Willywang8216/social-auto-upload/actions/runs/29154548808))
  — backend-tests ✅, frontend-build ✅, dependency-guard ✅.
- 2026-07-11 — Phase 2 foundation complete. D-8 fixed (schema-authority
  convergence); SQLAlchemy 2.x models/engine/repositories (`sau_app/db/`);
  PostgreSQL extra + compose service + `postgres-tests` CI job. 546 backend tests
  pass (11 new). Commits `f04cc98`, `40e2467`. CI green on head `40e2467`
  ([run 29159310813](https://github.com/Willywang8216/social-auto-upload/actions/runs/29159310813))
  — backend-tests ✅, **postgres-tests ✅ (real PostgreSQL 16)**, frontend-build
  ✅, dependency-guard ✅. Domain-by-domain cutover of live routes is incremental
  follow-up work.

## Next incomplete task

Phase 3 — Google OIDC application login (Authlib, `openid email profile` only,
kept separate from the YouTube connection flow) + `users`/`auth_identities`/
`workspaces`/`workspace_members`/`sessions` and first-login workspace creation.
**Blocker to flag to the user:** the live end-to-end login path needs real
`GOOGLE_LOGIN_CLIENT_ID`/`SECRET` + a registered redirect URI; the code and its
tests (mocked OIDC discovery/token) can be built and verified without them, but
a real Google login can't be exercised in CI without those secrets.
