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
| 4 | 3 | Google OIDC and sessions | ✅ complete (flag-gated; live path needs a Google client) |
| 5 | 4 | CSRF and authorization (AuthContext, roles) | ✅ complete |
| 6 | 5 | Workspace schema and legacy backfill | ✅ expand + backfill tool (constrain + prod run pending) |
| 7 | 6 | Route-by-route tenant isolation | 🚧 profiles+accounts+jobs+media scoped (20-test matrix); campaigns/analytics remain |
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

## Phase 3 — Google OIDC and sessions 🚧

**Identity/workspace foundation — done (this session):**
- **Migration `0015_identity_and_workspaces`** (portable `op.create_table`, runs
  on SQLite + PostgreSQL): `users`, `auth_identities` (unique on
  `provider, provider_subject`), `workspaces`, `workspace_members`, `sessions`.
  It is the first migration past the raw block, so it is applied by
  `bootstrap()` via the D-8 fix — verified end-to-end.
- **ORM models** (`sau_app/db/identity_models.py`) + **`IdentityRepository`**
  with `upsert_google_login()`: on first login, transactionally creates
  user + Google identity + personal workspace + owner membership; on repeat
  login, refreshes email/claims/profile and returns the same user + workspace.
  **Keyed by Google `sub`, never email** (changing email keeps the same user).
- **Tests** (`tests/test_identity_provisioning.py`, 5): first-login provisioning,
  repeat-login reuse, sub-not-email keying, per-user workspace isolation with
  unique slugs, missing-`sub` rejection. Run on SQLite + PostgreSQL (CI).

**OIDC transport — done (flag-gated, `SAU_GOOGLE_LOGIN_ENABLED`, default off):**
- `sau_app/auth/oidc.py` — `GoogleOIDCClient` (Authorization Code + PKCE +
  nonce + state; `openid email profile` only; ID-token verified via Google JWKS
  with iss/aud/exp/nonce checks) behind an injectable seam
  (`app.config['SAU_OIDC_CLIENT']`) so the flow is testable without Google.
- `sau_app/auth/transactions.py` — server-side login-transaction store
  (migration 0016 `oauth_login_transactions`): only a **hash** of `state`,
  single-use, 10-min TTL — defeats replay/CSRF; survives multi-process
  (unlike the legacy in-memory patreon state).
- `sau_app/auth/sessions.py` — server-side sessions (table from 0015), opaque
  high-entropy cookie, `Secure`+`HttpOnly`+`SameSite=Lax`+`__Host-`, idle +
  absolute expiry, per-session CSRF secret.
- `sau_app/auth/permissions.py` — role→permission map for the session response.
- `sau_app/auth/routes.py` — `/auth/google/start`, `/auth/google/callback`,
  `/api/v1/session`, `/api/v1/logout` (CSRF-protected). Registered by
  `init_extensions` only when the flag is on; the four paths bypass the legacy
  bearer gate (added to `PUBLIC_PATHS`) and enforce their own session auth. The
  redirect URI is **server-derived** from `SAU_PUBLIC_BASE_URL`, never the
  client. Config fails closed if the flag is on but client id/secret/base URL
  are missing.
- Tests: `tests/test_google_login_flow.py` (7) drive the whole flow against a
  fake Google client — first-login provisioning, secure session cookie, session
  introspection with permissions + CSRF, CSRF-protected logout + revocation,
  state replay/`error` rejection, and the flag-off no-op. Production wiring
  (flag set in env before import → 4 routes + real client registered) verified.

> **Live path needs the user:** create a dedicated Google Cloud OAuth 2.0 "Web"
> client (separate from YouTube), set `GOOGLE_LOGIN_CLIENT_ID/SECRET` +
> `SAU_PUBLIC_BASE_URL`, register `<base>/auth/google/callback`, and flip
> `SAU_GOOGLE_LOGIN_ENABLED=true`. Documented in `.env.example`.

> **Needs the user for the *live* path:** a real Google OAuth client
> (`GOOGLE_LOGIN_CLIENT_ID`/`SECRET`) and a registered redirect URI. The code and
> its mocked tests can be built and CI-verified without them; only enabling a
> real Google login in production requires the credentials.

### (original Phase 3 goal, for reference)

**Goal:** Authlib Google OIDC (`openid email profile` only), separate from
YouTube; `users`/`auth_identities`/`workspaces`/`workspace_members`/`sessions`;
first-login transactional workspace creation keyed by `sub`; `/auth/google/*`,
`/api/v1/session`, `/api/v1/logout`; HttpOnly session cookie; session-id
rotation. **Rollback:** `SAU_AUTH_MODE=legacy`.

## Phase 4 — CSRF and authorization ✅

**Goal:** auth middleware + `AuthContext`, permission decorators, role checks,
CSRF + Origin validation, 401/403/404 discipline — **inert under the default
legacy mode**.

**Delivered:**
- **Mode flags** (`AppConfig`): `SAU_AUTH_MODE` (legacy→hybrid→oidc, default
  legacy) and `SAU_TENANCY_MODE` (single→shadow→enforced, default single), with
  validation (invalid → warn + fall back; hybrid/oidc without Google login →
  warn). `sessions_honored_by_gate` is true only in hybrid/oidc.
- `sau_app/tenancy/context.py` — immutable `AuthContext`
  (user/workspace/role/permissions/auth_method) on `flask.g`, `current_auth()`.
- `sau_app/tenancy/middleware.py` — `before_request` that resolves the session
  **only when Google login is on** (skips all DB work otherwise), sets
  `g.sau_session_authenticated` **only in hybrid/oidc**, and enforces
  CSRF + Origin **only for session-cookie unsafe requests** (never bearer/
  webhooks). Fails open to anonymous on any resolution error.
- `sau_backend.py::_enforce_auth` — additively accepts a valid session
  (`g.sau_session_authenticated`); only ever set when Google login is on, so
  legacy-mode behavior is byte-identical.
- `sau_app/tenancy/decorators.py` — `require_auth` (401) and
  `require_permission` (401 anonymous / 403 authenticated-but-forbidden), not
  clearing the session on a 403.
- Roles→permissions from Phase 3's `permissions.py`.

**Exit evidence:** **572 passed** (14 new `tests/test_tenancy_middleware.py`):
config-mode defaults/validation, decorator 401/403, session context resolution,
CSRF 403-without/200-with, bad-Origin 403, viewer-403/editor-200 permission
checks, legacy-mode inertness, and the **real `sau_backend` gate** — anonymous
401, legacy token still works in hybrid, valid session satisfies the gate.
Security suite (71) green; route matrix + CSVs green.

**Rollback:** `SAU_AUTH_MODE=legacy` (the default) disables all of it.

## Phase 5 — Workspace schema and legacy backfill ✅ (expand + backfill tool)

**Goal:** expand→backfill→constrain per the rollout doc; legacy workspace;
orphan/count reports.

**Delivered:**
- **Mapper hardening** (`myUtils/profiles.py`, `myUtils/media_groups.py`): the
  four unfiltered `Model(**payload)` mappers now filter to declared dataclass
  fields (the precedent already used by `sheet_export_service` et al.), so a new
  `workspace_id` column doesn't break the legacy data layer. (`campaigns.py`/
  `jobs.py` build by explicit column name and were already safe.)
- **Migration 0017** (`workspace_id_expand`): adds a nullable `workspace_id`
  (String 36) to the 28 tenant-owned tables, indexed on the hot ones. Portable
  (SQLite + PostgreSQL), guarded (skips existing columns), applied via the D-8
  path — verified end-to-end (bootstrap reaches head 0017; profile/account/
  media-group round-trips still pass).
- **Backfill engine** (`sau_app/tenancy/backfill.py` + `tables.py`): ensures the
  legacy user + legacy workspace + owner membership (idempotent, from
  `SAU_LEGACY_OWNER_EMAIL`/`SAU_LEGACY_WORKSPACE_NAME`), assigns every existing
  tenant row to it (`UPDATE ... WHERE workspace_id IS NULL`, only touching NULLs
  so a re-run/partial state is safe), and emits pre/post counts + an orphan
  report. CLI: `python -m sau_app.tenancy.backfill [--dry-run]`, exits non-zero
  if any orphan remains. `TENANT_TABLES` is a single source of truth shared with
  the migration (drift-guarded by a test).

**Exit evidence:** **577 passed** (5 new `tests/test_backfill.py`: table-list
drift guard, full assignment + legacy-workspace creation, idempotency, dry-run
writes nothing + reports orphans, only-touches-NULL). CLI smoke-tested
(dry-run + real run exit 0). 112 data-layer tests green after the mapper change.

**Not done here:** the **constrain** step (NOT NULL, workspace-scoped composite
uniques, FKs, optional PostgreSQL RLS) — a follow-up revision once the backfill
has run — and the **actual production backfill**, which is an operator step
against the real database (backup first, then `python -m sau_app.tenancy.backfill`).
**Rollback:** `alembic downgrade` 0017 (drops the columns); the backfill only
writes NULL rows so it is re-runnable.

## Phase 6 — Route-by-route tenant isolation 🚧 (profile domain proven)

**Goal:** replace unscoped `list_*`/`get_*` with workspace-scoped access across
the routes, driven by `AuthContext`, 404 on foreign-workspace. Enforcement is
gated by `SAU_TENANCY_MODE` (default `single` = unscoped, unchanged).

**Delivered (the pattern, proven on the tenant-root domain):**
- `_workspace_scope()` (`sau_backend.py`): returns the session's workspace in
  `enforced` mode, else `None` (single/shadow, or legacy-token admin path).
- `myUtils/profiles.py`: `create/get/list/update/delete_profile` take an optional
  `workspace_id` (default `None` = today's unscoped SQL); when set, reads/writes
  are scoped and a foreign-workspace profile raises `LookupError` → 404.
- The five `/profiles*` routes + the account-create parent check pass
  `_workspace_scope()`.
- **Two-user isolation matrix** (`tests/test_tenant_isolation.py`, 8) in
  `enforced` mode against the real app: anonymous 401; each user lists only their
  own; A gets 404 reading/patching/deleting B's profile by known id (B intact);
  A cannot create an account under B's profile; A fully manages its own; a
  created profile is scoped to the caller's workspace; and the legacy token stays
  unscoped (admin path).

**Exit evidence:** **585 backend tests pass** (8 new). Default (`single`) mode
unchanged — 577 prior tests still green, `test_profiles.py` green.

**Accounts domain — done (2026-07-12):** `get/list/update/delete_account` +
`update_account_status` take the optional `workspace_id`; `add_account` stamps
the new account with its **parent profile's workspace** so ownership propagates
automatically. Wired routes: `/accounts/<id>` PATCH, `check-connection`,
`refresh-token`, `/api/accounts/<id>/check`, **`/api/auth/cookies/<id>/export`
(the P0 cookie-export — now workspace-scoped, also fixing its missing-account
500)**, `/accounts/<id>/import-cookies`, and the `/api/accounts` list. Two-user
matrix extended to 14 tests (591 total passing): A gets 404 patching/exporting/
importing/checking/refreshing B's account; `/api/accounts` lists only the
caller's workspace; accounts inherit the parent workspace.

**Remaining (same helper + pattern, incremental):** media/media-groups,
campaigns/templates, jobs + job logs, analytics, OAuth status, exports, and the
batch account routes (report-unauthorized-ids semantics) — each threads
`workspace_id` through its registry calls and adds its slice of the two-user
matrix. Then flip `SAU_TENANCY_MODE` `single`→`shadow`→`enforced` once the
production backfill (Phase 5) has run. **Rollback:** `SAU_TENANCY_MODE=single`.

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
- 2026-07-11 — Phase 3 identity/workspace foundation. Migration 0015 (identity/
  workspace/session tables, portable; applied via the D-8 path), identity models
  + `IdentityRepository.upsert_google_login()` (first-login workspace
  provisioning keyed by Google `sub`). 551 backend tests pass (5 new). Commit
  `305a4b0`. CI on head `305a4b0`
  ([run 29159600015](https://github.com/Willywang8216/social-auto-upload/actions/runs/29159600015)):
  postgres-tests ✅ (identity provisioning on real PostgreSQL), frontend-build ✅,
  dependency-guard ✅, backend-tests ✅.
- 2026-07-11 — Phase 3 OIDC transport complete (flag-gated). Authlib Google
  client + server-side login transactions (migration 0016) + server-side
  sessions + CSRF + the four `/auth/google/*` and `/api/v1/*` routes, all off by
  default. 558 backend tests pass (7 new, whole flow against a mocked Google
  client). Legacy bearer auth untouched. Live path needs the user's Google
  OAuth client (documented in `.env.example`). Commit `950cd96`. CI on head
  `950cd96`
  ([run 29173550355](https://github.com/Willywang8216/social-auto-upload/actions/runs/29173550355)):
  postgres-tests ✅, frontend-build ✅, dependency-guard ✅, backend-tests ✅.
- 2026-07-12 — Phase 4 complete. Compatibility mode flags (SAU_AUTH_MODE /
  SAU_TENANCY_MODE), `AuthContext`, tenancy middleware (session resolution +
  CSRF/Origin, inert in legacy mode), session-aware legacy gate, and
  require_auth/require_permission decorators. 572 backend tests pass (14 new).
  Default behavior unchanged. Commit `3830753`. CI on head `3830753`
  ([run 29185712375](https://github.com/Willywang8216/social-auto-upload/actions/runs/29185712375)):
  backend-tests ✅, postgres-tests ✅, frontend-build ✅, dependency-guard ✅.
- 2026-07-12 — Phase 5 expand + backfill tool. Hardened the legacy row-mappers,
  migration 0017 (nullable workspace_id on 28 tenant tables, applied via D-8),
  and the idempotent legacy-workspace backfill engine + CLI with orphan
  reporting. 577 backend tests pass (5 new). Constrain step + the real
  production backfill remain (the latter is an operator step on the live DB).
  Commit `4e4032b`. CI on head `4e4032b`
  ([run 29204422479](https://github.com/Willywang8216/social-auto-upload/actions/runs/29204422479)):
  postgres-tests ✅, frontend-build ✅, dependency-guard ✅, backend-tests ✅.
- 2026-07-12 — Phase 6 profile-domain isolation. `_workspace_scope()` helper +
  workspace_id scoping through the profile registry + the five /profiles routes,
  gated by SAU_TENANCY_MODE (default single = unscoped). Two-user isolation
  matrix (8 tests) proves A cannot list/read/modify/delete B's profile in
  enforced mode. 585 backend tests pass (8 new). Other domains follow the same
  pattern incrementally. Commit `1bdc8b3`. CI on head `1bdc8b3`
  ([run 29205390492](https://github.com/Willywang8216/social-auto-upload/actions/runs/29205390492)):
  postgres-tests ✅, frontend-build ✅, dependency-guard ✅, backend-tests ✅.
- 2026-07-12 — Phase 6b accounts-domain isolation. Account registry scoped
  (accounts inherit the parent profile's workspace on create); wired PATCH,
  check/refresh, cookie export/import, and the /api/accounts list. Two-user
  matrix now 14 tests; 591 backend tests pass (6 new). The user's Google login
  client ID + both registered redirect hosts recorded in
  docs/credential-setup.md with the go-live env block (secret stays in the
  deployment env). Commit `01b77aa`. CI on head `01b77aa`
  ([run 29217020659](https://github.com/Willywang8216/social-auto-upload/actions/runs/29217020659)):
  backend-tests ✅, postgres-tests ✅, frontend-build ✅, dependency-guard ✅.
- 2026-07-13 — PR #27 (Phases 0-6b) **merged to main** by the operator; Google
  login verified live at /auth/google/start (hybrid mode). Follow-up work
  restarted from the new main on the same branch.
- 2026-07-13 — Phase 6c jobs + media isolation. workspace_id scoping through
  the jobs registry (enqueue stamps it; get/list scoped) and media_groups +
  media_assets (create stamps; get/list/delete scoped); wired /jobs*,
  /media-groups*, /api/media/assets*, /api/media-groups* routes. Two-user
  matrix now 20 tests; 597 backend tests pass. Follow-up **PR #28**
  (https://github.com/Willywang8216/social-auto-upload/pull/28), commit `b2fa84e`; CI all green (backend/postgres/frontend/dependency-guard).

## Next incomplete task

Phase 6 (continued) — extend the proven `_workspace_scope()` pattern to the
remaining domains: accounts (`/accounts/*`, `/api/accounts`), media +
media-groups, campaigns + templates, jobs + job logs, analytics, OAuth status,
and exports — each threading `workspace_id` through its registry calls and
adding its slice of the two-user matrix. Then Phase 7 (credential encryption)
and the frontend (Phase 10).

**Standing user input still needed** (does not block the code build): a Google
OAuth client to exercise a *real* login (Phase 3 live path), and access to run
the Phase 5 backfill + flip `SAU_TENANCY_MODE=enforced` against the *production*
database.
