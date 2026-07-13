# Threat Model (Phase 0)

A lightweight STRIDE-per-surface analysis of `social-auto-upload` as it exists
today, driving the mitigation phases in the rollout plan. Finding IDs reuse the
defect register in [`../architecture/current-state.md`](../architecture/current-state.md)
(D-n) and the `risk` columns in the `reports/*.csv` inventories.

## 1. Scope and method

In scope: the Flask HTTP API, OAuth account-connection flows, webhooks, file
serving, uploads, the worker, the CLI, frontend token storage, and the
container deployment. Method: enumerate assets and actors, walk trust
boundaries, apply STRIDE per surface, rank findings, map each to a remediation
phase. This is a design-review artifact, not a pentest report.

## 2. Assets and actors

**Assets**
- OAuth access/refresh tokens (7 platforms) — currently plaintext in
  `accounts.config_json`.
- Platform browser cookies / Playwright storage state — files, optionally
  encrypted.
- Object-storage keys (`storage_backends`, env) — plaintext.
- The cookie encryption master key, LLM key, Google service-account key.
- Uploaded media.
- The legacy-workspace claim secret (target design).
- User identity and session material (target design).

**Actors**
- Anonymous internet user (can reach public routes).
- Authenticated tenant (holds a valid session/token; in the target, scoped to
  one workspace).
- Cross-tenant attacker (a valid user of workspace B probing workspace A).
- Malicious/compromised OAuth opener or redirect target.
- Provider webhook sender (may be spoofed).
- Operator / support (privileged).

## 3. Trust boundaries

```
 anonymous ─▶ [public routes] ─┐
                               ├─▶ Flask API ─▶ SQLite/Postgres
 tenant ────▶ [auth gate] ─────┘        │  ├─▶ filesystem (videoFile/cookies)
                                        │  └─▶ object store (S3)
 provider ──▶ [OAuth callback]──────────┤
 provider ──▶ [webhook]─────────────────┘
 operator ──▶ CLI ─▶ (direct DB + files, bypasses the API boundary)
 worker  ───▶ (in-process today) ─▶ platforms via OAuth/browser
```

The two boundaries that leak today are the **auth gate** (proves a shared
secret, not an identity) and the **CLI**, which sidesteps the gate entirely.

## 4. Per-surface analysis

### 4.1 HTTP API auth gate
- **Spoofing/EoP:** a single shared token grants everything; open mode grants
  everything to everyone (D-1). No per-user identity or authorization.
- **Information disclosure:** unscoped `list_*`/`get_*` return every tenant's
  data; `_account_payload` leaks plaintext OAuth tokens to any caller (D-4).
- **Tampering:** state-changing operations exposed over `GET`
  (`/deleteAccount`, `/deleteFile`) are CSRF-prone (D-19); no CSRF anywhere.
- Mitigation: Google OIDC + sessions (Ph 3), AuthContext + CSRF (Ph 4),
  workspace-scoped repositories + 404-on-foreign (Ph 5–6).

### 4.2 OAuth account-connection + postMessage
- **Tampering/redirect:** client-supplied `redirectUri` used in the token
  exchange (D-5) — open-redirect / consent-phishing surface.
- **Information disclosure:** callbacks `postMessage(..., '*')` with tokens in
  the page (D-6) — any opener origin can read them.
- **Repudiation/DoS:** patreon state in an in-process dict (D-13) — lost on
  restart, unusable multi-process.
- Mitigation: server-derived redirect URIs, unified tenant-bound
  `oauth_transactions` with hashed one-time state + expiry, pinned postMessage
  origin (Ph 5); frontend origin check (Ph 10).

### 4.3 Webhooks
- **Spoofing:** `/webhooks/tiktok` persists events even when the
  `Tiktok-Signature` fails (D-18); Meta deauth/data-deletion rely on
  `signed_request` verification.
- Mitigation: drop unverified events; resolve to a tenant-owned account before
  any mutation (Ph 5/9).

### 4.4 `/getFile` and static/thumbnail serving
- **Information disclosure:** `/getFile` and `/analytics/thumbnail/` are public
  and unscoped — any uploaded media is fetchable by filename; the thumbnail
  proxy refreshes a token from the *first* TikTok account on a cache miss.
- Mitigation: authorized or short-lived presigned downloads; workspace-prefixed
  private keys; never a shared account for refresh (Ph 8).

### 4.5 Upload register
- **Tampering:** `/upload/register` and multipart routes accept a
  client-supplied object key (regex-validated only) — no tenant prefix, so a
  crafted key can target the shared namespace.
- Mitigation: server-generated workspace-prefixed keys; reservation +
  `HEAD`-validated completion (Ph 8).

### 4.6 Worker
- **EoP/Information disclosure:** global account scan and global target claim
  (no tenant filter); in-process execution shares the API's blast radius;
  in-process asyncio locks don't serialize across processes.
- Mitigation: separate worker processes, re-verify `(id, workspace_id)`,
  distributed account locks, workspace-scoped idempotency (Ph 9).

### 4.7 CLI
- **EoP:** writes DB and cookie files directly, bypassing the auth gate (D-17).
- Mitigation: hosted access via workspace-scoped PATs; local direct mode stays
  single-tenant only (Ph 6+).

### 4.8 Frontend token storage
- **Information disclosure (XSS):** shared bearer token in `localStorage`
  (readable by any injected script); `appendAuthQuery()` can place it in URLs;
  401/403 conflated (D-15).
- Mitigation: HttpOnly `__Host-` session cookie, CSRF header, remove
  localStorage token and query-string auth (Ph 10).

### 4.9 Deployment
- **EoP/tampering:** Flask dev server in production (D-9/D-10), no gunicorn,
  worker in-process, `DEBUG_MODE=True` in the shipped example conf, secrets in
  a tenant-visible `storage_backends` table (D-7).
- Mitigation: gunicorn + separate worker containers, non-root, private
  storage, secret-manager keys, fail-closed startup (Ph 9/11).

## 5. Ranked findings

| Rank | ID | STRIDE | Surface | Remediation phase |
|---|---|---|---|---|
| 1 | D-1 | Spoofing/EoP | auth gate — shared token, no identity | 3–4 |
| 2 | D-4 | Info disclosure | `_account_payload` leaks OAuth tokens | 6–7 |
| 3 | D-2/D-3 | Info disclosure | decrypted cookie export endpoints | 7 |
| 4 | D-7 | Info disclosure | plaintext tokens + storage keys at rest | 7 |
| 5 | D-5 | Tampering | client-supplied OAuth redirect URI | 5 |
| 6 | D-6/D-16 | Info disclosure | `postMessage('*')` + no origin check | 5, 10 |
| 7 | — | Info disclosure | unscoped resources / cross-tenant reads by known id | 5–6 |
| 8 | `/getFile` | Info disclosure | public unscoped media serving | 8 |
| 9 | D-9 | EoP | publishing inline in request/threads; dev server | 9, 11 |
| 10 | D-13 | Repudiation/DoS | in-memory patreon OAuth state | 5 |
| 11 | D-18 | Spoofing | webhook persists unverified events | 5/9 |
| 12 | D-15/D-19 | Tampering | 401/403 conflation; state-changing GETs; no CSRF | 4, 10 |
| 13 | D-17 | EoP | CLI bypasses the auth boundary | 6+ |

## 6. Residual risk / out of scope for now

- Rate limiting and per-workspace quotas (login, OAuth start, uploads,
  publishing) — designed now, enforced in Ph 9/11.
- Supply-chain scanning (gitleaks, pip-audit, npm audit, container scan) —
  added to CI in Ph 11.
- Full audit-log coverage (credential changes, connections, publishing, member
  changes, exports) — Ph 9/11.
- DoS resistance of the browser workers under load — Ph 9.
