# Local release watcher and safety gates

**`scripts/ci-watch.sh` is the sole production deployer**, not a redundant
Watchtower/Actions fallback. Keep its existing per-user schedule:

```cron
*/2 * * * * /bin/bash /home/will/social-auto-upload/scripts/ci-watch.sh >/dev/null 2>&1
```

GitHub Actions tests and publishes images only. Both app containers remain opted
out of Watchtower. Historical Actions availability is recorded in
[`reports/ci-actions-reenable.md`](../reports/ci-actions-reenable.md); it does not
change deployment ownership.

## One tested candidate, two process roles

The Compose `social-auto-upload` (web) and `worker` services share the **same
immutable image ID**, uid/gid, data/config mounts, network and environment. Only
the worker runs `python -m myUtils.worker --max-concurrent 3` and maintenance.
WSGI still calls `sau_app.create_app()`, but selects `SAU_PROCESS_ROLE=web` before
importing it. Both services use `init: true`, SIGTERM and a 65-minute Compose
stop grace period; the release script never escalates its bounded wait to KILL.

Both use `SAU_DB_PATH=/app/db/database.db` and
`SAU_PAUSE_CLAIMS_FILE=/app/db/publish.pause`. Web requires worker health.
STT, development PostgreSQL, the proxy override, external network and existing
mounts are preserved; release commands name only the web/worker pair and use
`--pull never --no-deps`.

Production uses the pinned **Python 3.12.14** `sau-base:slim` image. The app
build asserts that version and stamps the full SHA in `SAU_BUILD_REVISION` and
`org.opencontainers.image.revision`. `GIT_SHA` remains a compatible build arg.

## Shared gates and receipts

Linux prerequisites: Git, Bash, Python 3.12+, **uv 0.12.7**, **Node 22.21.1**,
Docker with Compose v2, `flock`, and the existing ffmpeg/browser system libraries.
uv can provision the 3.12/3.13 test interpreters. Restore also needs `fuser`,
`tar`, `sha256sum` and the existing rclone installation.

```bash
bash scripts/release-gates.sh all --receipts /tmp/sau-gate-receipts
bash scripts/release-gates.sh verify --receipts /tmp/sau-gate-receipts
```

The shared recipe runs:

- Linux **3.12 and 3.13**: `uv sync --frozen --extra web --group dev
  --no-install-project`, compile/undefined-name checks across maintained Python
  (including `uploader`, `mcp_server`, `sau_app`, scripts and tests), and **all**
  `tests/`, including `test_security_http.py`.
- Frontend: `npm ci`, `npm test`, `npm run build` inside `sau_frontend`. Demo
  recordings are excluded by its Vitest configuration, not by skipping UI tests.
- Image: frozen uv export with hashes into an isolated runtime venv, avoiding
  unpinned/base-image Python packages. `Dockerfile.dockerignore` retains the npm
  lockfile while excluding runtime secrets/data. No dependency lock is rewritten.

Tests and image builds use disposable clean worktrees, never the production
bind mount. Gate subprocesses have an allowlisted environment and empty HOME;
no `.env`, operator config, `.netrc`, cloud credentials or live SQLite file is
copied into them. Test tempfiles are isolated per invocation.

Receipts record the full commit, tree and lockfile hashes, required checks and
Linux matrix component. A local image receipt additionally binds the **image
ID** to those receipts. A cached image/short tag alone is not evidence. The
image workflow calls the same CI gates and verifies their exact-SHA artifacts
before publishing; CI also retains supplemental PostgreSQL dialect coverage.

## Watcher and deployment sequence

All deploy paths, backup and offline restore use `logs/ci-watch.lock`. The watcher
passes the open lock descriptor to the candidate's deploy script, which verifies
its inode and ownership rather than trusting an environment-only bypass.

1. Fetch `origin/main`, refuse dirty/untracked source or unpublished/divergent
   local history, and check out the **resolved exact commit** in a private
   worktree. A matching state marker is accepted only if both roles, image
   receipt, revisions and readiness agree.
2. `deploy-local.sh` verifies/runs the gates and builds without production mounts,
   then import-checks the image with networking disabled. No running services
   are changed yet.
3. Save image IDs, actual container config/environment/commands, rendered Compose
   and runtime config under `backups/releases/<time>-<full-sha>-<pid>/baseline/`.
   These files **contain secrets**: directories are 0700, files 0600; never paste
   them into logs/issues. Cookie/config encryption behavior is unchanged. Keep
   the previous image locally via `:previous` as well as the recorded image ID.
   Before pausing/stopping anything, the selected `/app/conf.py` bind source
   (including `SAU_CONF_FILE`) must resolve to a readable regular file. Valid
   symlinks are accepted; missing/unreadable paths are not guessed or created.
   The runtime checkout must also ignore exactly `db/publish.pause`; the helper
   checks this before creating the marker or stopping any service. New checkouts
   carry `/db/publish.pause` in `.gitignore`. For an older initial deployment, add
   that exact rule to `.git/info/exclude`, preserving its other entries. Never
   hide all untracked changes to bypass the cleanliness check.
4. Create a release-owned `db/publish.pause` marker. Wait for running/leased
   targets and leased prep work to finish, and for a fresh draining heartbeat.
   Pending retries and unclaimed prep requests are not executing work. Bad SQL,
   missing schema and stale/ambiguous prep leases fail closed, never become zero.
5. Send SIGTERM and require both processes to exit gracefully. Automatic restart
   is disabled while stopping. Recheck DB activity after shutdown. A timeout
   aborts with the pause retained; there is no "deploy anyway after two hours".
6. Take a **read-only-source online SQLite snapshot** (`Connection.backup`),
   verify `quick_check` and SHA256, and verify an isolated restore. No raw live
   `database.db`/WAL copying is used. The snapshot stays with the baseline.
   Before migration, seal the required Compose/config/image baseline and SQLite
   snapshot/checksum in a private `recovery-manifest.json` of SHA256 hashes.
7. Run `python -m myUtils.db_bootstrap` explicitly **inside the candidate image** while
   both writers are stopped and claims remain paused, then start the matched
   web/worker pair without pulling/restarting dependencies.
   The image-owned bootstrap is outside the `/app/db` data bind mount; never
   use a potentially stale host `db/createTable.py` for container migrations.
   Both rendered services must use `pull_policy: never` and the exact verified
   local image ID. This keeps migration compatible with Compose 2.29.7, whose
   `run` command does not support `--pull`; `up --pull never` remains explicit.
8. Verify schema, image IDs, OCI/environment/HTTP revisions, and this container's
   fresh **draining** worker heartbeat while still paused. Safely fast-forward
   the still-clean host checkout so the next cron tick uses the tested scripts.
9. Unless `--hold` was requested, remove only this release's own pause, require a
   **new non-draining** heartbeat, `/readyz` 200 and both Docker healthchecks, then
   atomically record the full SHA in **`logs/ci-watch.state`** and update `:latest`.
   Manual and automated releases use this same successful revision marker.

Normal Docker health is intentionally **unhealthy during a pause**: web probes
`/readyz`, not `/` or `/healthz`; worker probes call `myUtils.jobs.worker_health`
with the configured DB through a read-only adapter. The temporary
`--allow-paused` release probe is separate: web may report only
`worker_unavailable` (database/schema must be OK), and the worker must report a
fresh draining heartbeat from its own process, not a previous container. No
public health policy is weakened just to pass a deployment.

Bounds: `SAU_RELEASE_DRAIN_SECONDS` (default 600; existing
`SAU_CI_WATCH_MAX_WAIT_SECONDS` remains its fallback),
`SAU_RELEASE_STOP_SECONDS` (600), `SAU_RELEASE_READY_SECONDS` (180). They must be
positive. The old force/defer-expiry bypasses are gone.

## Parent-controlled initial rollout / manual use

These commands require a **clean committed candidate**; uncommitted integration
work must never be shipped. They are release instructions, not a record of a
production deployment performed by this change.

```bash
REV=$(git rev-parse HEAD)
# Build/test first; does not read production config or touch services.
bash scripts/deploy-local.sh --revision "$REV" --build-only

# Parent/operator first quiesces ingress and any independent DB-writing tools.
# For the first transition from the legacy embedded-worker web ONLY:
bash scripts/deploy-local.sh --revision "$REV" --no-build --legacy-stop-web --hold

# Inspect the paused release and perform non-publishing verification.
# This activation intentionally permits queued work to start immediately:
bash scripts/deploy-local.sh --revision "$REV" --resume
```

**Legacy limitation:** the previous web embeds publishers and cannot acknowledge
the pause marker or reliably join its publishing threads. `--legacy-stop-web`
requires an explicitly controlled idle/outage window, checks legacy activity
conservatively, stops web gracefully, then rechecks. Any remaining/ambiguous
activity aborts before migration; an operator must reconcile it. There is no
claim of a zero-downtime, race-free drain of an unmodified legacy web. Automatic
rollout refuses that transition without the flag. Legacy web is **not restarted
automatically on rollback**, because it would ignore the pause.

The parent owns rollout auth decisions: if Google login lacks an operator
allowlist, the parent disables it at rollout. These scripts do not guess allowed
identities or rewrite auth/content/credential settings.

For subsequent releases, the watcher calls the same helper automatically.
`--runtime-root DIR` lets a clean candidate worktree target the existing bind
mount root. `--no-build` still requires matching gates and an image-ID receipt.
`--hold` leaves `logs/release-staged.json`, keeps claims paused, and does **not**
advance the successful-deploy marker. `--resume` verifies the original recovery
seal, exact required artifact paths/hashes, Compose JSON/image-ID consistency,
the snapshot's checksum and a fresh isolated restore, and availability of the
previous Docker image IDs before accepting pause/rollback ownership. It then
re-verifies the exact staged image before activating. Missing/corrupt/unsealed
recovery evidence leaves the candidate paused without attempting rollback;
`--resume` never creates a new seal to hide drift. Cron will not resume it or an
unknown pause for you. Keep the private recovery bundle intact while held.

```bash
bash scripts/ci-watch.sh
CI_WATCH_FORCE=1 bash scripts/ci-watch.sh     # no safety checks are bypassed
CI_WATCH_DRY_RUN=1 bash scripts/ci-watch.sh   # cached origin/main; no fetch/deploy
CI_WATCH_TEST_ALERT=1 bash scripts/ci-watch.sh # existing best-effort Telegram test
```

## Failure, rollback and backup recovery

A build/test failure leaves services untouched. A failure after pausing retains
the pause and evidence. After a candidate switch, the helper attempts bounded
graceful shutdown and **previous images/config only**; normal prior split-worker
images may start paused, while a legacy embedded web is restored **stopped**.
If shutdown/drain cannot be established, no replacement is forced. Previous code
may not support the new schema: keep claims paused and investigate, rather than
faking readiness or running a schema downgrade.

**Never automatically restore an older DB after activation.** External posts can
already exist, and restoring lost receipts/idempotency state could publish them
again. Preserve the current DB and snapshot for delivery reconciliation. A failed
release does not become the recorded successful revision. After a rollback or
failed drain, recover explicitly under the same lock; do not just delete the
pause or start a legacy embedded worker.

During a failed **legacy** transition, keep the deployment lock across every
recovery phase until the tested watcher has replaced the old script. The old
watcher can otherwise rebuild the old revision and replace only the web service,
even while a new worker is paused. If the lock cannot remain held between recovery
calls, privately save and temporarily comment only that application's deployment
cron entry. Preserve unrelated entries and restore its exact schedule only after
the matched release and tested watcher are installed. Do not clear the pause or
restart the legacy publishers merely to silence an unhealthy status.

Scheduled backup destinations/policy stay unchanged:

- Local staging: `/home/will/social-auto-upload/backups`.
- Remote: `Onedrive-Yahooforsub-Tao:Scripts-ssh-ssl-keys/socialupload/`.
- Keep **5** remote archives, deleting the local archive only after upload;
  checksum sidecars travel/rotate with their archives. Existing cron is unchanged.
- Archives contain the same config/cookie/frontend items, but use a verified
  online SQLite snapshot instead of the live DB. Plaintext/encrypted cookie bytes
  are not transformed; restrictive permissions are added, no encryption keys
  are generated or replaced.

```bash
bash scripts/backup.sh --dry-run
bash scripts/restore.sh --file /path/backup.tar.gz --verify-only
# Only after ALL container/host/cron/CLI writers are stopped and recovery approved:
bash scripts/restore.sh --file /path/backup.tar.gz --offline
```

Restore validates archive checksum and safe extraction, snapshot checksum,
`quick_check` and an isolated restore. It refuses running DB-sharing containers,
open host handles, unsafe destinations, or a held release lock. `--yes` skips only
the prompt. Older archives without historical checksums need explicit
`--legacy-archive`; this verifies a new isolated snapshot, not historical
provenance. Offline originals/sidecars are retained under `db/.pre-restore.*`
plus the existing `backups/pre-restore-*.tar.gz` safety archive. Services stay
stopped and claims paused. Archive those quarantine files under the protected
backup directory after recovery review so unknown files correctly stop blocking
the watcher; review schema and delivery state before resuming any publisher.

Release baselines are deliberately retained for operator recovery/review (not
swept by the scheduled archive rotation). Protect and manage their disk usage.
The existing image retention remains 3 newest `commit-*` tags plus running/
previous references (`SAU_CI_WATCH_KEEP_IMAGES`), and the existing BuildKit cache
limit remains 3GB (`SAU_CI_WATCH_BUILD_CACHE`).

Logs: `logs/ci-watch.log`, `logs/ci-watch-build-<sha>.log`,
`logs/releases/deploy-*.log`; gate/image receipts:
`logs/releases/gates/<full-sha>/`. Telegram alert configuration and destinations
are unchanged. Logs must never include rendered configuration or credentials.
