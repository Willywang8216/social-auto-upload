# Multi-Tenant Migration & Rollout Plan

How the schema and data migrate from single-tenant SQLite to multi-tenant
PostgreSQL **without a big-bang rewrite and without data loss**, and how to roll
each step back. Reads alongside the target design
([`../architecture/target-multi-tenant.md`](../architecture/target-multi-tenant.md))
and the table inventory
([`../../reports/table-ownership-inventory.csv`](../../reports/table-ownership-inventory.csv)).

## 1. Principles

- **Expand → backfill → constrain.** Add nullable columns, backfill, *then*
  add NOT NULL / uniqueness / FKs. Never rename or drop existing columns during
  the compatibility window.
- **Compatibility-first.** All existing data becomes the *legacy workspace*
  (tenant zero); the working install keeps running throughout.
- **Every step reversible.** Each Alembic revision has a tested `downgrade`;
  each cutover has a documented data-rollback (not just "restore the backup").
- **No destructive change while `SAU_TENANCY_MODE=single`.**
- **Stop on ambiguity.** If any record's ownership cannot be determined, the
  migration halts and emits an orphan report — it does not guess.

## 2. Prerequisite: converge the two schema paths (defect D-8)

Today `db/createTable.py::bootstrap()` creates tables via raw SQL and then
*stamps* the Alembic head (`_stamp_alembic_head`, `:708-760`) without running
migrations, so `alembic upgrade` is a no-op on a bootstrapped DB. Before any
tenant migration:

- Make Alembic the single source of truth: `createTable.py` (or its
  replacement) runs `alembic upgrade head` against a real connection.
- Verify the raw-SQL schema and the Alembic-built schema are identical
  (extend `tests/test_alembic.py`).
- Do **not** stamp a legacy database as current without executing and verifying
  the migrations against it.

## 3. Expand (new revisions, additive)

- **`0015` — identity/tenancy tables:** `users`, `auth_identities`,
  `workspaces`, `workspace_members`, `sessions`, `api_keys` (DDL in the target
  design). Additive; no existing table touched.
- **`0016` — nullable `workspace_id` (+ `created_by_user_id` where attribution
  matters):** add `workspace_id UUID NULL` to every tenant-owned table per the
  inventory's `planned_scoping`. Index each new column. Still nullable, still
  no constraints — safe to deploy while running the old code.

## 4. Backfill (idempotent, verifiable)

1. Create the one legacy user + legacy workspace from
   `SAU_LEGACY_OWNER_EMAIL` / `SAU_LEGACY_WORKSPACE_NAME` (owner binding
   deferred to the claim flow).
2. Backfill **top-level** tables directly to the legacy workspace:
   `profiles`, `file_records`, `media_groups`, `media_assets`,
   `publish_templates`, `user_info`.
3. Backfill **child** tables through their parent per the inventory
   `backfill_source` (e.g. `accounts` → `profiles.workspace_id`,
   `campaign_posts` → `campaigns.workspace_id`, analytics →
   `accounts.workspace_id`, `publish_job_targets` → `publish_jobs.workspace_id`).
4. Resolve `*_oauth_requests` and `account_events` via their nullable
   `profile_id`/`account_id`, else assign to the legacy workspace.
5. Emit reports and **stop if any non-intentional orphan remains**:
   `reports/pre-migration-row-counts.json`,
   `reports/post-migration-row-counts.json`,
   `reports/workspace-backfill-report.json`,
   `reports/orphaned-records.json`,
   `reports/foreign-key-validation.json`.

Backfill must be idempotent (re-runnable) and must never overwrite a
non-legacy `workspace_id` once multi-user data exists.

## 5. Constrain (after backfill verifies clean)

1. Add composite indexes for the hot query paths (`workspace_id` first).
2. Replace global uniqueness with workspace uniqueness:
   - `profiles`: `UNIQUE(workspace_id, slug)`
   - `accounts`: `UNIQUE(workspace_id, profile_id, platform, account_name)`
   - `publish_jobs`: `UNIQUE(workspace_id, idempotency_key)`
   - `publish_templates`: `UNIQUE(workspace_id, name)`
3. Add foreign keys where practical (Postgres; SQLite is limited).
4. Set `workspace_id` NOT NULL where the inventory marks required ownership.
5. Add PostgreSQL Row-Level Security to sensitive tables (`profiles`,
   `accounts`, `account_credentials`, media, jobs) once app-level scoping is
   green; API uses a non-superuser role that sets
   `SET LOCAL app.workspace_id` per transaction.
6. Verify row counts and relationship counts match the pre-migration baseline.

## 6. SQLite → PostgreSQL (`SAU_DATABASE_MODE`)

- **`sqlite`** — today.
- **`dual-verify`** — build Postgres in parallel; migrate a *copy* of
  production; compare row counts, ids, relationships, JSON fields, timestamps,
  file references, credential blobs, job states, analytics. Optionally
  mirror-write and verify. **Never migrate the live DB directly.**
- **`postgres`** — controlled cutover: (1) publishing into maintenance mode /
  brief write freeze; (2) back up SQLite; (3) final sync; (4) integrity checks;
  (5) start API on Postgres; (6) read-only checks; (7) one media upload;
  (8) one canary publish; (9) confirm jobs + analytics; (10) re-enable
  publishing. Keep the SQLite backup + rollback config until Postgres is
  proven stable.

## 7. Per-phase rollback

Each phase defines a **trigger**, a **rollback command**, a **data-rollback**,
and a **validation**. A plan that only says "restore the backup" is
insufficient — during hybrid operation new writes complicate rollback, so each
cutover must use a brief write freeze **or** a tested reverse-sync.

| Phase | Trigger | Rollback | Data rollback | Validate |
|---|---|---|---|---|
| Expand (0015/0016) | migration error / perf regression | `alembic downgrade` one rev | additive columns only — dropping them loses no legacy data | row counts unchanged; app boots |
| Backfill | orphan report non-empty / count mismatch | re-run is idempotent; or `UPDATE ... SET workspace_id=NULL` | legacy workspace assignment is reversible while `single` | counts equal; zero orphans |
| Constrain | constraint violation on real data | `alembic downgrade` to pre-constrain rev | none (constraints only) | uniqueness holds; FKs valid |
| Auth (OIDC) | login breakage | flip `SAU_AUTH_MODE=legacy` | sessions are ephemeral (Redis) | legacy token still logs in |
| Tenancy enforce | cross-tenant test regression | flip `SAU_TENANCY_MODE=shadow` | none (read-path scoping) | shadow logs show no divergence |
| Postgres cutover | integrity/canary failure | flip `SAU_DATABASE_MODE=sqlite`, restart on SQLite backup | reverse-sync writes made on Postgres, or discard within the write freeze | canary publish + counts match |

Retain rollback scripts and the SQLite backup until each phase is stable.

## 8. Production backup runbook (operator — run on the deployed host)

> Not executable from the CI/clone environment (no production data present).
> `scripts/backup.sh` and `scripts/sau-daily-backup.sh` already archive the
> right paths; use them rather than duplicating steps.

Before **any** migration phase touches production:

1. **Freeze/prepare:** put publishing into maintenance mode (or stop the
   compose stack for a cold backup).
2. **Database:** `sqlite3 db/database.db ".backup 'backups/pre-phase-<n>.db'"`
   (consistent snapshot; do not `cp` a live DB).
3. **Files + config:** archive `conf.py`, `.env`, `secrets/`, `cookies/`,
   `cookiesFile/`, `videoFile/`, `uploads/`, `data/`,
   `sau_frontend/dist/`, `docker-compose.yml` — exactly the
   `BACKUP_ITEMS` set in `scripts/backup.sh`. Run `./scripts/backup.sh`
   (rotates 5 copies to the rclone remote) or `./scripts/sau-daily-backup.sh`.
4. **Record state:** current deployed commit SHA, `alembic current`, and a
   per-table `SELECT COUNT(*)` snapshot.
5. **Restore drill:** on a scratch host, restore the archive + `.db`, boot the
   API read-only, confirm counts — *prove the backup restores* before relying
   on it.
6. **Secrets:** back up `.env`/`conf.py`/keys to a secret store; **never**
   commit them.

The compose stack mounts `./db`, `./cookies`, `./cookiesFile`, `./videoFile`,
`./uploads`, `./data` directly into the container, so those host directories
are the authoritative state to back up and restore.

## 9. Cutover checklist (Phase 9 production rollout)

Restore a production snapshot into staging → run all migrations → compare every
table count → verify legacy rows belong to the legacy workspace → test two
separate Google users → cross-tenant adversarial tests → canary publish on one
API platform and one browser platform → worker-restart-during-job test →
DB/Redis restart test → OAuth callback replay test → revoked-session test →
credential-key-rotation test → deploy behind a flag → monitor before open
registration.
