# Current-State Architecture (Phase 0 Audit)

This document records how `social-auto-upload` works **today**, before any
multi-user conversion. It is the baseline the target design
(`docs/architecture/target-multi-tenant.md`) and rollout plan
(`docs/migrations/multi-tenant-rollout.md`) build on. Every claim here is
anchored to a file:line or to a machine-generated inventory under `reports/`.

- Baseline commit: `227c050fe686ae44ba4530e35c81785ea45ef13c`
- Baseline evidence: [`reports/baseline-verification.md`](../../reports/baseline-verification.md)
- Route inventory: [`reports/route-authorization-matrix.csv`](../../reports/route-authorization-matrix.csv)
- Table inventory: [`reports/table-ownership-inventory.csv`](../../reports/table-ownership-inventory.csv)
- Credential inventory: [`reports/credential-storage-inventory.csv`](../../reports/credential-storage-inventory.csv)
- Public-route inventory: [`reports/public-route-inventory.csv`](../../reports/public-route-inventory.csv)
- Filesystem inventory: [`reports/filesystem-path-inventory.csv`](../../reports/filesystem-path-inventory.csv)
- OAuth inventory: [`reports/oauth-flow-inventory.csv`](../../reports/oauth-flow-inventory.csv)

## 1. System overview

```
                    ┌──────────────────────────────────────────────┐
  Browser (SPA) ───▶│  Flask app  (sau_backend.py, 7778 lines)     │
  Vue3 + Pinia      │  137 @app.route handlers, no blueprints       │
  localStorage      │  app.run() dev server, threaded, port 5409    │
  'sau-auth-token'  │                                               │
                    │  before_request _enforce_auth (shared token)  │
                    │  in-process daemon threads:                   │
                    │   - worker drain  (publishing)                │
                    │   - account maintenance (token refresh)       │
                    │   - local-cleanup (6h)                        │
                    └───┬───────────────┬───────────────┬──────────┘
                        │               │               │
              ┌─────────▼───┐   ┌───────▼──────┐  ┌─────▼─────────┐
              │ SQLite       │   │ Local files  │  │ Playwright    │
              │ db/database  │   │ videoFile/   │  │ (patchright)  │
              │ .db (1 file) │   │ uploads/     │  │ browserless   │
              │ 29 tables    │   │ cookies/     │  │ or local      │
              └──────────────┘   │ cookiesFile/ │  └───────┬───────┘
                        ▲        └──────┬───────┘          │
                        │               │           social platforms
            ┌───────────┴──────┐  ┌─────▼────────┐  (OAuth APIs + web)
            │ sau_cli.py       │  │ DO Spaces /  │
            │ (direct DB+files;│  │ rclone (S3)  │
            │  bypasses HTTP)  │  └──────────────┘
            └──────────────────┘
```

Single-process modular monolith with **no tenant concept**. The unit of
grouping is a *profile* (a brand), which owns *accounts* (per-platform
connections). Both are globally visible to any authenticated caller.

## 2. Runtime topology and deployment

- **Process model:** `Dockerfile` runs `CMD ["python", "sau_backend.py"]`,
  whose `__main__` block calls Flask's built-in development server
  `app.run(host='0.0.0.0', port=5409, threaded=True)` (`sau_backend.py:7777`).
  No WSGI server (gunicorn/uwsgi/waitress) is present anywhere in
  `requirements.txt`, `pyproject.toml`, or `uv.lock`.
- **Worker:** the publishing worker runs as **in-process daemon threads**
  inside the Flask app — a drain thread (`_start_worker_drain_thread`,
  `sau_backend.py:6322-6332`), an account-maintenance loop
  (`sau_backend.py:2556-2582`), and an unconditional `local-cleanup` thread
  (`sau_backend.py:7491`). A standalone entrypoint exists
  (`python -m myUtils.worker`) but `docker-compose.yml` does not run it.
- **Compose:** one service, mounting `./db`, `./cookies`, `./cookiesFile`,
  `./videoFile`, `./uploads`, `./data`, and `conf.py` into the container;
  external `1panel-network`; `browserless` referenced but external. Requires
  a `.env` file to exist (defect D-14).
- **Port discrepancy:** the app listens on **5409**, but the Dockerfile
  `EXPOSE`s and README/docs reference **7777** in places — a doc/config
  mismatch (D-10).

## 3. Authentication today

- Model: a **shared bearer-token pool** in `SAU_API_TOKENS`, parsed by
  `myUtils/security.py::load_policy()`. When unset the app runs in
  **open mode** — every route is public (a single startup warning is logged).
- Gate: `@app.before_request _enforce_auth` (`sau_backend.py:353-378`).
  Order: open-mode bypass → `OPTIONS` bypass → public-path bypass →
  `extract_bearer_token` → constant-time `token_is_valid` → `401`.
- Identity: **none.** Passing the gate proves only that the caller knows a
  shared secret. `/whoami` (`sau_backend.py:381`) returns
  `{openMode, authenticated}` and nothing user-specific.
- Public bypass list: `PUBLIC_PATHS` + `PUBLIC_PREFIXES`
  (`myUtils/security.py:40-85`) — see the public-route inventory. `/getFile`
  and `/analytics/thumbnail/` are fully public data endpoints. `/login`
  accepts the token via `?auth=` query string because `EventSource` can't set
  headers.
- Frontend: the token lives in `localStorage['sau-auth-token']`
  (`sau_frontend/src/utils/auth.js`), attached as a `Bearer` header by an
  axios interceptor. The login page is a form where the user types the shared
  token (`LoginView.vue`).

## 4. Data model

- **Storage:** one SQLite file, `db/database.db`, reached through a hardcoded
  `Path(BASE_DIR)/db/database.db` in ~10 places; `SAU_DB_PATH` is honored
  **only** by `db/createTable.py`'s `__main__`, not at runtime.
- **Access:** raw `sqlite3` throughout — **87 `sqlite3.connect(` sites across
  31 files** (26 in `sau_backend.py`). SQLAlchemy is present only as an
  Alembic dependency; there are **no ORM models** in application code.
- **Schema:** 29 domain tables + `alembic_version`, defined both in
  `db/createTable.py` (raw `CREATE TABLE`) and in 14 Alembic revisions
  (head `0014_socialupload_full_schema`). See the table-ownership inventory.
  No user/tenant table exists; `profiles` is the de-facto root and has no
  owner column; `file_records`, `media_groups`, `media_group_items`,
  `media_assets`, `publish_templates`, `storage_backends`, and
  `publish_job_targets` have **no ownership column at all**.
- **Dual schema paths (defect D-8):** `createTable.py::bootstrap()` runs the
  raw-SQL `CREATE TABLE` chain and then `_stamp_alembic_head()`
  (`:708-760`) writes the current Alembic head into `alembic_version`
  *without running the migrations*, so a later `alembic upgrade` is a no-op.
  The real migration path (`alembic upgrade head`) runs only via
  `python db/createTable.py`. These two paths must converge before
  Postgres (Phase 2).

## 5. Storage and files

See the filesystem inventory. Local `videoFile/` and `uploads/` plus S3
(DigitalOcean Spaces via `myUtils/do_spaces.py`, boto3) and rclone
(`myUtils/rclone_storage.py`). Object keys are `uploads/<uuid>_<name>` — a
**flat, shared namespace** with no tenant prefix. `/upload/register` and the
multipart routes accept a **client-supplied key** validated only against a
`uploads/<uuid>_` regex. Cookie files are optionally AES-GCM encrypted
(`myUtils/cookie_storage.py`) when `SAU_COOKIE_ENCRYPTION_KEY` is set;
otherwise plaintext.

## 6. OAuth flows

See the OAuth inventory. Seven hand-rolled flows (Authlib is **not** a
dependency): meta, youtube, reddit, twitter, threads, tiktok, patreon. Six
persist state in per-platform `*_oauth_requests` tables; **patreon keeps
state in an in-process dict** (`_PATREON_OAUTH_REQUESTS`, `sau_backend.py:6501`),
which is lost on restart and breaks under multi-process deployment. Every
start route accepts a **client-supplied `redirectUri`** that is also used in
the token exchange; every callback page broadcasts its result via
`postMessage(..., '*')`. Access/refresh tokens land **plaintext** in
`accounts.config_json`. Callback hosts are configured in three disagreeing
places (`.env.example` `socialupload.*` and `up.*`, plus hardcoded
`socialupload.iamwillywang.com` fallbacks at `sau_backend.py:167-213`).

## 7. Jobs and concurrency

- `publish_jobs.idempotency_key` is **globally UNIQUE** and client-settable
  via `POST /jobs` (`sau_backend.py:5998`) — two workspaces submitting the
  same key would collide.
- `publish_job_targets` reference accounts/files by loose TEXT
  (`account_ref` = `account:<id>` or a cookie filename; `file_ref` = a path
  or remote key).
- The worker's maintenance tick scans **all** enabled accounts globally
  (`myUtils/worker.py:315-334`); `claim_next_targets` claims globally with a
  `BEGIN IMMEDIATE` transaction and an in-flight `excluded_accounts` set
  (`myUtils/jobs.py:385-449`).
- Per-account concurrency locks are **in-process asyncio only**
  (`utils/concurrency.py:46-91`) — no cross-process/tenant fairness.

## 8. Known-defects register

Each item is anchored and cross-referenced to the risk column in the route /
credential inventories. IDs are stable and referenced by the threat model.

| ID | Severity | Defect | Anchor |
|----|----------|--------|--------|
| D-1 | P0 | No user identity: passing the gate proves only knowledge of a shared token; open mode disables auth entirely | `security.py:88-109`; `sau_backend.py:353-393` |
| D-2 | P0 | `/api/auth/cookies/<id>/export` returns decrypted cookies by bare account id, no ownership check; also calls nonexistent `cookie_storage.read_cookie_file` and silently falls back to a raw file read | `sau_backend.py:7666-7681` |
| D-3 | P0 | `/downloadCookie` returns decrypted cookie bytes; guarded only by a path-traversal allowlist, not by tenant | `sau_backend.py:1742-1799` |
| D-4 | P0 | `_account_payload()` returns the full `config_json` including plaintext OAuth access/refresh tokens; leaked by `/profiles/<id>/accounts`, account create/PATCH/refresh, `/admin/oauth/status` | `sau_backend.py:1902`, `:5186`, `:4464` |
| D-5 | P0 | OAuth start routes accept a client-supplied `redirectUri` and use it in the token exchange (open-redirect / phishing surface) | `sau_backend.py:3460`, `:3827`, `:4007`, `:4198` |
| D-6 | P0 | OAuth callback pages `postMessage(..., '*')` with tokens/account data embedded — any opener origin can read them | `sau_backend.py:3529`, `:3889`, `:3971`, `:4265`, `:4352` |
| D-7 | P0 | OAuth access/refresh tokens stored plaintext in `accounts.config_json`; `storage_backends` keys plaintext too | inventory; `0002:148-151`, `0012:26-41` |
| D-8 | P1 | `bootstrap()` stamps the Alembic head without running migrations; dual raw-SQL vs Alembic schema paths | `db/createTable.py:708-760` |
| D-9 | P1 | Publishing (Playwright) runs inline in HTTP request handlers (`/postVideo`, `/jobs/run`) and as in-process daemon threads | `sau_backend.py:1365`, `:6049`, `:6322-6332` |
| D-10 | P2 | Port mismatch: app listens on 5409 but Docker/README reference 7777 | `sau_backend.py:7777` |
| D-11 | P1 | `tests/test_security_http.py` writes to the real shared `BASE_DIR` DB and `cookiesFile/`, so CI excludes it (`ci.yml:33`); test passes but is isolation-unsafe | `tests/test_security_http.py:44-49`; `.github/workflows/ci.yml:33` |
| D-12 | P1 | `/oauth/patreon/callback` and `/data-deletion` are not in `PUBLIC_PATHS`, so the browser redirect / Meta page gets 401 in token-protected mode | `security.py:40-72` |
| D-13 | P1 | Patreon OAuth state kept in an in-process dict; lost on restart, incompatible with multi-process gunicorn | `sau_backend.py:6501` |
| D-14 | P2 | `docker compose` fails on a fresh clone because `env_file: .env` is required with no fallback | `docker-compose.yml`; baseline F-2 |
| D-15 | P1 | Frontend axios interceptor treats 401 and 403 identically and clears the token for both | `sau_frontend/src/utils/request.js:57-73` |
| D-16 | P1 | OAuth popup `postMessage` handler does not validate `event.origin` | `sau_frontend/src/views/AccountManagement.vue:465-477` |
| D-17 | P1 | CLI writes the DB and cookie files directly, bypassing the HTTP auth gate entirely | `sau_cli.py:15` |
| D-18 | P2 | `/webhooks/tiktok` persists events even when `Tiktok-Signature` verification fails | `sau_backend.py:4915-4954` |
| D-19 | P1 | State-changing operations exposed over `GET` (`/deleteAccount`, `/deleteFile`) — CSRF-prone | `sau_backend.py:1222`, `:1108` |
| D-20 | P2 | `SAU_DB_PATH` honored only by `createTable.py __main__`, not at runtime — no way to relocate the runtime DB | `db/createTable.py:845` |

## 9. Baseline verification

Full command/exit-code/output evidence is in
[`reports/baseline-verification.md`](../../reports/baseline-verification.md).
Summary: 517 backend tests pass; the CI-excluded security test passes but is
isolation-unsafe (D-11); the frontend build passes; `npm test` has one
pre-existing failing test *file* (a Playwright spec collected by vitest, F-1);
`docker compose config` needs a `.env` (D-14). None are fixed in Phase 0.
