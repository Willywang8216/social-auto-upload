# Phase 0 Baseline Verification

Recorded evidence of the repository's state **before any multi-user work**. Every command
below was run in a fresh clone on the branch `claude/multi-user-google-auth-kaj0of`
(even with `origin/main`). Failures are recorded with root causes — nothing was fixed
as part of this phase.

- Date: 2026-07-10 (UTC)
- Starting commit: `227c050fe686ae44ba4530e35c81785ea45ef13c`
- Environment: Linux 6.18.5 container (cloud execution environment), fresh clone,
  no production data present (`db/database.db`, `cookies/`, `cookiesFile/`, `uploads/`,
  `videoFile/`, `.env`, `conf.py` all absent at start — all gitignored).

> **Production backup note:** the mandated backups of `db/database.db`, cookie
> directories, media directories, `.env`, and `conf.py` apply to the *deployed*
> installation, which is not reachable from this environment. The operator backup
> runbook is documented in `docs/migrations/multi-tenant-rollout.md` and must be
> executed on the production host before any migration phase (Phase 2+) touches it.

## Summary

| # | Command | Exit | Duration | Result |
|---|---------|------|----------|--------|
| 1 | `git status --short --branch && git rev-parse HEAD` | 0 | <1s | clean tree at `227c050f` |
| 2 | toolchain versions | 0 | <1s | Python 3.11.15, uv 0.8.17, node v22.22.2, npm 10.9.7, Docker 29.3.1 |
| 3 | `uv sync --extra web --group dev` | 0 | 3.6s | OK (installs patchright 1.58.2, Flask 3.1.1, SQLAlchemy 2.0.49, alembic, pytest…) |
| 4 | `cp -n conf.example.py conf.py` | 0 | <1s | OK (gitignored) |
| 5 | `uv run python -m compileall -q sau_backend.py sau_cli.py myUtils utils uploader db` | 0 | 0.3s | OK, no syntax errors |
| 6 | `uv run pytest tests/ --ignore=tests/test_security_http.py -q` (CI parity) | 0 | 90.2s | **517 passed, 22 subtests passed, 0 failed** |
| 7 | `uv run pytest tests/test_security_http.py -q` (excluded from CI) | 0 | 6.5s | **12 passed** — but see side-effect finding below |
| 8 | `npm ci` (sau_frontend) | 0 | 11.2s | OK |
| 9 | `npm test` (sau_frontend, = `vitest run`) | **1** | 1.4s | **12/12 unit tests pass; 1 test *file* fails** — see F-1 |
| 10 | `npm run build` (sau_frontend) | 0 | 12.3s | OK (`✓ built in 11.66s`) |
| 11 | `docker compose config --quiet` | **1** → 0 | <1s | **fails without `.env`** — see F-2; passes with an empty `.env` |
| 12 | `git status --short` after all runs | 0 | <1s | clean (all runtime artifacts gitignored) |

Browser note: patchright chromium is preinstalled in this environment
(`PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers`); `patchright install chromium` was **not**
needed — the full suite passed without it.

## Details and findings

### 1–2. Git state and toolchain

```
## claude/multi-user-google-auth-kaj0of
227c050fe686ae44ba4530e35c81785ea45ef13c
Python 3.11.15 | uv 0.8.17 | node v22.22.2 | npm 10.9.7 | Docker 29.3.1
```

Python 3.11.15 satisfies `requires-python = ">=3.10,<3.13"`.

### 6. CI-parity backend suite — PASS

```
517 passed, 22 subtests passed in 90.18s (0:01:30)
```

Zero failures, zero skips reported. This is the no-drift reference set: the identical
command is re-run after all Phase 0 commits and must produce the identical result.

### 7. `tests/test_security_http.py` — PASSES but is isolation-unsafe (evidence for its CI exclusion)

```
12 passed in 6.53s
```

The test itself is healthy. The reason CI excludes it (`.github/workflows/ci.yml:33`)
is **shared-state coupling, not failure**: running it against a fresh tree created

- `db/database.db` (528 KB — it bootstraps the *real* `BASE_DIR` database via
  `conf.BASE_DIR`, not an isolated temp dir), and
- `cookiesFile/` (empty directory)

inside the repository working tree. On a developer machine with real data these paths
are the production SQLite file and cookie store — the test seeds and deletes
`user_info` rows in whatever database is present. `git status` stays clean only
because both paths are gitignored. Re-enabling this test in CI (planned Phase 11)
requires pointing it at an isolated temp `BASE_DIR` first.
Defect register: `docs/architecture/current-state.md` D-11.

### 9. Frontend unit tests — F-1: vitest collects a Playwright spec

```
Error: Playwright Test did not expect test() to be called here.
 ❯ demo/demo.spec.ts:243:1
 Test Files  1 failed | 3 passed (4)
      Tests  12 passed (12)
```

**Root cause:** there is no vitest configuration limiting test discovery, so vitest's
default include pattern (`**/*.{test,spec}.?(c|m)[jt]s?(x)`) collects
`sau_frontend/demo/demo.spec.ts`, which is an `@playwright/test` spec (the demo-video
recorder, driven by `playwright.config.ts` with `testDir: 'demo'`). Playwright's
`test()` refuses to execute under a foreign runner, failing the *file* while all 12
real unit tests (3 files under `src/utils/__tests__/`) pass.

**Classification:** `code-bug` (test-tooling misconfiguration), pre-existing, fails
identically anywhere. Not fixed in Phase 0 (zero-behavior-change rule); the fix
(vitest `include`/`exclude` scoped to `src/`) lands with the CI work (Phase 11),
which also starts running `npm test` in CI for the first time.

### 11. `docker compose config` — F-2: hard dependency on `.env`

```
env file /home/user/social-auto-upload/.env not found
exit=1
```

**Root cause:** `docker-compose.yml` declares `env_file: .env` for the app service
without `required: false`, so *any* compose command fails on a fresh clone until the
operator creates `.env`. With an empty `.env` the config validates cleanly (exit 0) —
the YAML itself is well-formed. **Classification:** `env-limitation` of fresh clones /
compose-portability defect. Recorded; fix belongs to deployment hardening (Phase 11).
(External references — `1panel-network`, the `browserless` host — are only resolved
at `docker compose up`, which was not attempted here.)

### 12. Working-tree cleanliness

`git status --short` is empty after the full baseline. Runtime artifacts created by
the baseline run, all gitignored: `.venv/`, `conf.py`, `db/database.db`,
`cookiesFile/`, `sau_frontend/node_modules/`, `sau_frontend/dist/`, `__pycache__/`
(19 dirs), and an empty `.env` (created only to complete the compose validation).

## No-drift confirmation (filled at end of Phase 0)

Re-run of step 6 after the Phase 0 docs/inventories commits:

- Command: `uv run pytest tests/ --ignore=tests/test_security_http.py -q`
- Result: **517 passed, 22 subtests passed in 78.94s** — identical pass/fail
  set to the baseline run (step 6). Phase 0 introduced no behavior drift.
- Corroborating checks (exit 0): `dump_route_matrix.py --check`
  ("route matrix OK: 137 routes"), `check_csvs.py` ("all report CSVs valid").
- `git diff main --name-status` shows only added (`A`) files under
  `scripts/audit/`, `reports/`, `docs/`, `plans/` — zero existing files
  modified or deleted.
