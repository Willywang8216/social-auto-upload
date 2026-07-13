# Target Multi-Tenant Architecture

The design `social-auto-upload` is migrating toward: one shared deployment
serving many Google-authenticated users, each with a private workspace, with
strict tenant isolation, encrypted credentials, private storage, and separate
workers. This document is the **keystone** the later phases implement against.

It is deliberately **compatibility-first**: the current working installation
becomes *tenant zero* (a "legacy workspace") and keeps operating throughout the
migration. Nothing here is a big-bang rewrite. Phasing lives in
[`../migrations/multi-tenant-rollout.md`](../migrations/multi-tenant-rollout.md)
and the living plan in [`../../plans/multi_user_implementation.md`](../../plans/multi_user_implementation.md).

## 1. Goals and non-goals

**Goals**
- One frontend, one Flask API, one PostgreSQL DB, one Redis, separate worker
  processes, one private object-storage bucket, one logical workspace per user.
- Google OpenID Connect for *application login*, kept distinct from the
  existing YouTube *account-connection* OAuth.
- Every tenant-owned row, file, object key, job, and OAuth transaction carries
  an explicit workspace owner; every query filters by the authenticated
  workspace; a user can never read, write, or infer another workspace's data.
- Credentials encrypted at rest, mandatory in production; no secrets in normal
  API responses.

**Non-goals (explicitly out of scope)**
- Rewriting to FastAPI/Django/Next.js or microservices.
- Rewriting the platform uploaders (`uploader/*`) or changing publishing
  behavior, retries, idempotency, or account validation semantics.
- One deployed application instance per user.

## 2. Ownership hierarchy

```
User (a person who logs in with Google)
└── Workspace (personal by default; shared/team later)
    ├── Profiles / Brands
    │   └── Social Accounts ──▶ Account Credentials (encrypted)
    ├── Media Assets / Media Groups
    ├── Campaigns / Templates / Prepared Posts
    ├── Publish Jobs ──▶ Job Targets
    ├── Analytics
    ├── Storage Metadata
    ├── OAuth Transactions
    └── Audit Logs
```

A **Profile is not a User.** In this repo a Profile is a brand/publishing
identity that owns social accounts; it stays a business object *below* the
workspace and must not become the authentication identity.

## 3. Identity and tenancy schema (DDL sketch)

New authentication/tenancy tables use UUID primary keys. This is a sketch for
design review — the authoritative DDL will be Alembic revisions (Phase 3/5).

```sql
users (
  id UUID PK, primary_email TEXT, display_name TEXT, avatar_url TEXT,
  status TEXT NOT NULL, created_at, updated_at, last_login_at
)

auth_identities (
  id UUID PK,
  user_id UUID NOT NULL REFERENCES users(id),
  provider TEXT NOT NULL,            -- 'google'
  provider_subject TEXT NOT NULL,    -- Google 'sub' claim; the durable key
  email_at_provider TEXT,
  email_verified BOOLEAN NOT NULL,
  claims_json JSONB NOT NULL,
  created_at, updated_at,
  UNIQUE(provider, provider_subject) -- NEVER key identity by email
)

workspaces (
  id UUID PK, name TEXT NOT NULL, slug TEXT NOT NULL,
  status TEXT NOT NULL,
  created_by_user_id UUID NOT NULL REFERENCES users(id),
  created_at, updated_at, UNIQUE(slug)
)

workspace_members (
  workspace_id UUID NOT NULL REFERENCES workspaces(id),
  user_id UUID NOT NULL REFERENCES users(id),
  role TEXT NOT NULL,                -- owner | admin | editor | viewer
  created_at,
  PRIMARY KEY(workspace_id, user_id)
)

sessions (                          -- server-side; Redis is source of truth,
  id UUID PK,                       -- this table is the durable mirror/audit
  user_id UUID NOT NULL,
  active_workspace_id UUID NOT NULL,
  created_at, last_seen_at, expires_at, revoked_at,
  csrf_secret TEXT NOT NULL,
  user_agent_hash TEXT, ip_prefix TEXT
)

api_keys (                          -- workspace-scoped PATs for hosted CLI
  id UUID PK, workspace_id UUID NOT NULL, created_by_user_id UUID NOT NULL,
  name TEXT NOT NULL, token_prefix TEXT NOT NULL, token_hash TEXT NOT NULL,
  scopes JSONB NOT NULL, created_at, last_used_at, expires_at, revoked_at
)
```

### Roles and permissions (initial)

| Capability | owner | admin | editor | viewer |
|---|---|---|---|---|
| Manage workspace | ✅ | limited | ❌ | ❌ |
| Manage members | ✅ | ✅ | ❌ | ❌ |
| Connect accounts | ✅ | ✅ | ✅ | ❌ |
| Upload media | ✅ | ✅ | ✅ | ❌ |
| Publish | ✅ | ✅ | ✅ | ❌ |
| View analytics | ✅ | ✅ | ✅ | ✅ |
| Export credentials | ❌ (off by default) | ❌ | ❌ | ❌ |

Even though every workspace starts with a single member, the boundary is built
now so team sharing later needs no second schema rewrite.

## 4. Authentication design

### 4.1 Two distinct OAuth purposes

- **Google application login** — determines *who* is using the app. Scopes:
  `openid email profile` only. No YouTube scopes, no offline access, no
  refresh token. Config: `GOOGLE_LOGIN_CLIENT_ID` / `GOOGLE_LOGIN_CLIENT_SECRET`
  / `GOOGLE_LOGIN_REDIRECT_URI`.
- **YouTube account connection** — the existing flow that lets an already
  logged-in user connect a channel (`youtube.upload`, `youtube.readonly`).
  Keeps its own config (`YT_CLIENT_ID` / `YT_CLIENT_SECRET` /
  `YOUTUBE_REDIRECT_URI`) and preferably a different Google OAuth client, so
  login and publishing permissions never couple.

### 4.2 Google OIDC (Authorization Code + PKCE), server-side

1. User clicks "Continue with Google".
2. Backend generates `state`, OIDC `nonce`, PKCE `code_verifier` + challenge.
3. Store the transaction server-side (Redis, 5–10 min TTL), bound to the
   anonymous browser session.
4. Redirect to Google with the **exact configured** redirect URI.
5. On callback validate `state`, PKCE, `nonce`, issuer, audience, signature,
   expiry.
6. Upsert identity by Google **`sub`** (never email).
7. On first login, in one transaction create user + identity + personal
   workspace + owner membership.
8. Rotate the session id; redirect to the dashboard.

Authlib provides the Flask OIDC client and can use a server cache instead of
exposing temporary OAuth material in Flask's client-side session.

### 4.3 Session and CSRF

- Server-side sessions in Redis; the browser holds only an opaque cookie.
- Cookie: name `__Host-sau_session`, `Secure`, `HttpOnly`, `SameSite=Lax`,
  `Path=/`, no `Domain`.
- Rotate session id after login and after any workspace/role change; revoke on
  logout; support global revocation; enforce idle + absolute expiry.
- **Never** store the session token in `localStorage`/`sessionStorage` or a URL.
- SSE uses **same-origin** requests so the browser sends the cookie — dropping
  today's `?auth=` query-string token (D-3/public-route inventory).
- Require `X-CSRF-Token` on `POST/PUT/PATCH/DELETE`; validate `Origin` for
  unsafe browser requests; reject missing/invalid CSRF before route logic.

### 4.4 Session API

```
GET  /auth/google/start
GET  /auth/google/callback
GET  /api/v1/session            -> {authenticated, user, workspace, permissions, csrfToken}
POST /api/v1/logout
POST /api/v1/session/refresh
POST /api/v1/session/select-workspace
```

`/api/v1/session` returns identity, active workspace, role-derived
permissions, and a short-lived CSRF token — and **never** provider tokens,
refresh tokens, cookie files, encryption keys, client secrets, or raw account
config. This replaces `/whoami`, which only proves token validity.

## 5. The AuthContext pattern (enforcement)

Every authenticated request resolves an immutable context in middleware:

```python
@dataclass(frozen=True)
class AuthContext:
    user_id: UUID
    workspace_id: UUID
    role: str
    permissions: frozenset[str]
    session_id: UUID
    auth_method: str   # 'google_session' | 'legacy_token' | 'api_key'
```

Rules (the core of tenant isolation):

1. Never trust a client-supplied `workspaceId`; read it from the server
   session.
2. Every list/read/update/delete query includes `workspace_id`.
3. Child resources verify **both** their own and their parent's workspace.
4. Return **404** (not 403) for another workspace's resource, so existence
   isn't confirmed.
5. Batch operations reject the whole request or report unauthorized ids
   without processing them.
6. Background tasks and webhooks re-load the resource *with* its workspace
   before acting.
7. Cross-workspace admin operations use a separate service role and emit an
   audit event.

Repository signatures change from unscoped to workspace-scoped, e.g.
`profile_registry.get_profile(id)` →
`profile_repo.get_for_workspace(profile_id=..., workspace_id=ctx.workspace_id)`.
As defense in depth, PostgreSQL Row-Level Security is applied to sensitive
tables after application-level scoping works: the API runs as a non-superuser
role and executes `SET LOCAL app.workspace_id = '<uuid>'` inside every
transaction; migrations/support use a separate privileged role.

## 6. Compatibility-first migration — tenant zero

The current installation is **not disposable test data**; it becomes the first
tenant. Three orthogonal mode flags let the code become tenant-aware *before*
login and persistence change, and let each concern advance independently.

### 6.1 Mode matrix

| Flag | Values (progression) | Meaning |
|---|---|---|
| `SAU_AUTH_MODE` | `legacy` → `hybrid` → `oidc` | who may authenticate |
| `SAU_TENANCY_MODE` | `single` → `shadow` → `enforced` | how strictly workspace scope is applied |
| `SAU_DATABASE_MODE` | `sqlite` → `dual-verify` → `postgres` | where data lives |

- **`legacy`** — today's shared bearer token only.
- **`hybrid`** — accept *either* the legacy bearer token *or* a Google session;
  both resolve to an `AuthContext`. The legacy token maps to the legacy
  workspace/owner. This lets the backend go tenant-aware without forcing a
  login change.
- **`oidc`** — Google session is the normal browser auth; the legacy token
  survives only as a restricted emergency/service mechanism behind an explicit
  flag.
- **`single`** — repositories default to the one legacy workspace; scope
  filters are computed but not required.
- **`shadow`** — workspace filters are computed and any divergence is **logged
  and tested** but not enforced (safety net before flipping enforcement).
- **`enforced`** — every request/query/upload/OAuth/job/worker action requires
  authenticated workspace ownership; the compatibility fallback is forbidden.
- **`sqlite` → `dual-verify` → `postgres`** — write SQLite while
  mirror-verifying Postgres, then cut over (see rollout doc).

Valid default progression: start `legacy/single/sqlite`; end
`oidc/enforced/postgres`. A transition's prerequisites are listed in the
rollout doc; enforcement (`enforced`) must not precede a green tenant-isolation
test matrix.

### 6.2 The legacy workspace and the claim flow

On the first tenant migration a single legacy user + legacy workspace is
created from deployment config, and **all existing rows are backfilled into
it**:

```
SAU_LEGACY_WORKSPACE_NAME="Will's Workspace"
SAU_LEGACY_OWNER_EMAIL="willywang8216@gmail.com"
SAU_LEGACY_CLAIM_SECRET=<one-time secret, hashed at rest>
SAU_LEGACY_CLAIM_REQUIRED=true
```

Email is used **only** to identify who may claim the legacy workspace during
migration. The safer claim flow:

1. Owner logs in with Google.
2. Backend checks the verified Google email equals the configured owner email.
3. Backend also requires the one-time claim secret.
4. Backend binds the legacy workspace to that Google `sub`.
5. The claim secret is permanently invalidated (single-use, hashed, expiring).
6. All future logins rely solely on the stored `sub`; any *other* Google user
   who logs in gets a fresh empty personal workspace.

This prevents a misconfigured email or deployment var from letting the wrong
person claim the existing data.

### 6.3 Compatibility fallback (temporary)

During `single`/`shadow`, repository methods accept an optional `workspace_id`
and fall back to the legacy workspace with a warning naming the caller:

```python
def list_profiles(*, workspace_id=None):
    workspace_id = workspace_id or require_legacy_workspace_in_compatibility_mode()
```

The fallback works only in `single`/`shadow`, logs the caller, is forbidden in
`enforced`, and is removed once enforcement is proven.

## 7. Enforcement points

- **Routes:** resolve `AuthContext` in `before_request`; lookup helpers take
  the context. 404-on-foreign-workspace. (Route inventory
  `planned_workspace_scope` column tags each of the 137 routes.)
- **Repositories:** mandatory workspace scope; no unscoped `list_*`/`get_*`.
- **Worker:** re-fetch job by `(job_id, workspace_id)` and account by
  `(account_id, workspace_id)`; reject mismatches; distributed account lock
  `lock:workspace:{workspace_id}:account:{account_id}`.
- **Filesystem/object store:** server-generated, workspace-prefixed keys
  (`filesystem-path-inventory.csv` `planned_layout`):
  `workspaces/{workspace_id}/media/{asset_id}/...`,
  `workspaces/{workspace_id}/cookies/{account_id}/{version}`.
- **CLI:** hosted access via workspace-scoped Personal Access Tokens
  (`api_keys`); the local direct-DB CLI keeps working for single-tenant use
  but must not bypass workspace authorization against the hosted service.

## 8. OAuth account-connection hardening

- Unify the six per-platform `*_oauth_requests` tables (and the in-memory
  patreon state) into one `oauth_transactions` table carrying `purpose`,
  `workspace_id`, `initiated_by_user_id`, hashed one-time `state`, `nonce`,
  encrypted PKCE verifier, server-derived `redirect_uri`, `expires_at`,
  `consumed_at`, `status`.
- Redirect URIs come **only** from trusted server config
  (`SAU_PUBLIC_BASE_URL`), never from the frontend.
- Callbacks verify workspace + account still exist and that the account
  belongs to the transaction's workspace.
- Callback pages post to the **exact configured frontend origin** (or use a
  server redirect carrying only a one-time success reference) — never
  `postMessage(..., '*')`, never tokens in the popup.
- Store tokens in the encrypted credential store, reusing the existing
  AES-GCM envelope (`myUtils/cookie_storage.py`) generalized with key
  versioning.

## 9. Credential storage

Separate public account metadata (channel title, username, avatar, scopes,
expiry, status) from secret material (access/refresh tokens, cookies,
Playwright storage state, webhook secrets, API keys). Secrets move out of
`accounts.config_json` into an `account_credentials` table with versioned
envelope encryption, `UNIQUE(account_id, credential_type)`, master key from a
secret manager, **mandatory in production (no plaintext fallback)**. Normal
account endpoints never return secret fields; logs and exceptions redact
tokens; the unrestricted cookie-export endpoints (D-2, D-3) are removed or put
behind owner reauthentication + explicit permission + audit + rate limit.

## 10. Media and object-storage isolation

Private bucket; server-generated workspace-prefixed keys; upload *reservation*
before a short-lived presigned PUT; completion validated via object-store
`HEAD` (key prefix, size, MIME, magic-bytes, checksum); per-workspace quotas
and file limits; `ffprobe` before video processing; temp cleanup; presigned
GET only after authorization; temporary public *derivatives* (not the source)
for platforms that require a public pull URL. `/getFile` and the analytics
thumbnail prefix (currently public, `public-route-inventory.csv`) become
authorized or presigned.

## 11. Jobs and workers

Keep idempotency, per-target progress, retries, backoff, and account
concurrency limits — but move execution to dedicated worker processes (Redis
queues: `publish_api`, `publish_browser`, `media_processing`,
`analytics_sync`, `credential_maintenance`, `webhook_processing`). Every task
payload carries `job_id`, `workspace_id`, `account_id`,
`media_asset_id`/`artifact_id`, attempt, correlation id. Workers re-verify
ownership, take the distributed account lock (TTL + heartbeat, released in
`finally`), classify retryable vs terminal errors, add a dead-letter state, and
never log secrets. Idempotency uniqueness becomes
`UNIQUE(workspace_id, idempotency_key)`.

## 12. Frontend

Remove the shared-token form, `localStorage['sau-auth-token']`, the bearer
interceptor for browser sessions, `appendAuthQuery()`, client-supplied OAuth
callback URIs, and any raw-secret display. Add a "Continue with Google" screen,
an auth Pinia store (`status | user | workspace | permissions | csrfToken`,
`loadSession/startGoogleLogin/logout/switchWorkspace/can`), `axios` with
`withCredentials: true` and an `X-CSRF-Token` header on unsafe methods, a route
guard that awaits `loadSession()` (not token presence), and **distinct** 401
(reload session → login) vs 403 (insufficient permission; keep session)
handling (fixing D-15). Validate OAuth popup `postMessage` against the exact
app origin (fixing D-16).

## 13. Configuration

One canonical origin (`SAU_PUBLIC_BASE_URL`, `SAU_FRONTEND_ORIGIN`) generates
all callbacks server-side, normalizing today's three disagreeing callback
hosts. New required production vars: `APP_ENV`, `SECRET_KEY`, `DATABASE_URL`,
`REDIS_URL`, session cookie/TTL settings, the `GOOGLE_LOGIN_*` trio,
`CREDENTIAL_MASTER_KEY` + `CREDENTIAL_KEY_VERSION`, `OBJECT_STORAGE_*`
(`OBJECT_STORAGE_PRIVATE=true`), `INTERNAL_SERVICE_TOKEN`. Production startup
**fails closed** when a mandatory secret is missing. Auth mechanisms stay
separate: browser = session cookie; CLI = workspace-scoped API keys; workers =
service credentials; webhooks = provider signature verification.
`SAU_API_TOKENS` is retired as the primary user login.

## 14. Feature-flag rollout order

```
1. Google login for the legacy owner only
2. Tenant shadow checks (SAU_TENANCY_MODE=shadow)
3. PostgreSQL
4. Private tenant-aware storage
5. Separate workers
6. Tenant enforcement (SAU_TENANCY_MODE=enforced)
7. Invite-only test users
8. Open registration
```

Supporting flags: `SAU_GOOGLE_LOGIN_ENABLED`, `SAU_LEGACY_AUTH_ENABLED`,
`SAU_TENANT_ENFORCEMENT_ENABLED`, `SAU_POSTGRES_ENABLED`,
`SAU_REDIS_WORKERS_ENABLED`, `SAU_PRIVATE_STORAGE_ENABLED`,
`SAU_MULTI_USER_REGISTRATION_ENABLED`. Open registration is never enabled
before the tenant-isolation test matrix passes.

## 15. Decision log

- **Identity key = Google `sub`, not email** — email is mutable/re-assignable;
  `sub` is the durable per-provider identifier.
- **Legacy install = tenant zero via a claim flow** — preserve the working
  system; bind to `sub` after an email + one-time-secret claim.
- **Three orthogonal mode flags** — decouple auth, tenancy, and database
  migrations so each can advance and roll back independently.
- **404 (not 403) for foreign-workspace resources** — avoid confirming
  existence across tenants.
- **Reuse the existing AES-GCM cookie envelope** for the credential store
  rather than introducing a new crypto scheme; add key versioning.
- **Keep Flask + Vue; no framework rewrite** — tenant isolation is a
  data/authorization problem, not a framework problem.

## 16. Open questions

- Service-token story after `oidc` (CI, internal automation) — API keys vs a
  dedicated `INTERNAL_SERVICE_TOKEN` scope.
- Exact Postgres cutover timing relative to enforcement.
- Whether shared/team workspaces ship in the initial multi-user release or
  strictly after personal workspaces are proven.
