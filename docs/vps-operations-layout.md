# One SAU operations root; container-only verification

Production remains `/home/will/social-auto-upload`. Do not flatten import packages or combine source code with public media. All private operational material belongs beneath `ops/`, excluded from Git and Docker contexts:

```text
social-auto-upload/
  myUtils/, sau_app/, sau_frontend/, scripts/, ...  maintained source
  db/, videoFile/, generated/, cookies/, ...        existing protected runtime
  backups/, logs/                                  retained release and receipt history
  ops/
    inbox/                                        canonical phone/Drive inbox
    legacy-work/sau-work/                          preserved authored materials
    evidence/                                     sealed source/receipt/cleanup archives
    verification/<revision>/                      bounded container output
    toolchains/rules/<rules-version>/              private read-only rules bundle
```

The 2026-11-17 cleanup preserved Git history, archived five source-only clones, removed six clean unused Git worktrees, and moved nine historical evidence directories. Production code, database, pending media, rollback and receipt bytes were not reset or pruned. `~/sau-inbox` and `~/sau-work` are compatibility symlinks to their canonical directories, not duplicate storage. Keep them until the frozen mounts, cron and historical operational references no longer need them. New compose mounts use `SAU_INBOX_HOST_DIR`, default `./ops/inbox`.

## Verification

Do not run the backend test suite beside production `.env` or its database, and do not run it on Windows as a substitute for VPS release gates. The host shell may control Docker; application, render and test execution belongs inside containers. SSH, the Docker daemon, kernel swap, host security and minimal supervisors remain host infrastructure.

`Dockerfile.verify` adds fixed Node 22.21.1, Git and OpenSSH to an explicitly selected runtime image. Build it on the VPS with a local image tag bound to an already verified immutable runtime ID. It is a **verification tool image**, not an application deployment. Record its resulting image ID; never infer production readiness from successfully building it.

Run the source-bound containers using:

```text
SAU_VPS_ROOT=/home/will/social-auto-upload bash scripts/vps-gates.sh <full-revision> frontend <immutable-verify-image-id> <private-rules-version-root>
SAU_VPS_ROOT=/home/will/social-auto-upload bash scripts/vps-gates.sh <full-revision> backend-3.12 <immutable-verify-image-id> <private-rules-version-root>
SAU_VPS_ROOT=/home/will/social-auto-upload bash scripts/vps-gates.sh <full-revision> backend-3.13 <immutable-verify-image-id> <private-rules-version-root>
```

Run frontend first, preserving its locked KaTeX assets for the backend's read-only fixture mount. The backend toolchain is Python 3.12.14 / 3.13.15, uv 0.12.7 and the unchanged frozen dependency graph. Each container reconstructs a writable **ephemeral** Git checkout from read-only public Git objects; no host Git configuration/hooks, production database, credentials, media or Docker socket are mounted. Private voice rules stay outside the image and public source. No publisher starts.

The launcher enforces a private output path, memory/disk headroom, 2 CPUs, 6 GiB memory, bounded PIDs, a 4 GiB executable private `/work` tmpfs and a bounded non-executable standard `/tmp`. Both temporary mounts retain `nosuid,nodev`; source/rules remain read-only and no capabilities/socket are granted. It refuses an existing container identity rather than silently repeat an unknown launch. Inspect the original container's exit status and full logs; archive them and the exact component receipts before removing that container. Readiness runs before the frontend receipt, so a failed gate does not yield a passing release receipt.

Missing private rules or offline formula assets **fail the Teaching backend release gate**. Skipping its real renderer/humanizer tests is not release evidence. Readiness, isolated migrations, maintained full backend/frontend checks, immutable image binding, the unchanged HIGH/CRITICAL CVE policy and non-root probes still apply before deployment. The preparation/release flags remain off until an independently approved rollout.

## Fixed operational containers

`Dockerfile.ops` is a small, allowlisted overlay on the **existing immutable production runtime**, not a web/worker feature deployment. Build with a locally bound `SAU_RUNTIME_IMAGE`, its independently checked `SAU_RUNTIME_ID`, and the exact `SAU_OPS_REVISION`. Its context contains only the five inbox tools, two vault scripts, shared-lock archive module and fixed entrypoint; it cannot include `.env`, DB, cookies, media, Git metadata, private voice rules or `ops/`. Image-owned legacy inbox aliases point to the same mounted inbox, not additional storage. Legacy identity matching and archive state updates preserve existing receipts, claims and attempt budgets under the shared state lock.

`scripts/vps-ops.sh` accepts only seven named jobs: watcher, inbox-monitor, pipeline-monitor, inbox-archive, inbox-autopublish, vault-credentials and vault-llm-stt. It checks that the overlay's base image still matches **both** production roles. Host cron remains the one scheduler. Each job is non-root, has a read-only rootfs, bounded CPU/memory/PIDs, no capabilities and no Docker socket. Per-job host locks are nonblocking; an existing container identity requires reconciliation, not automatic replay.

Private configuration lives in `ops/container-jobs/`: `image-id.txt`, release-specific public controllers, lock files, and separately scoped `config/drive/` and `config/onedrive/` directories. Each rclone config contains just its required remote. Only its dedicated directory is writable, so atomic OAuth refresh works without granting the full host config. Never commit or print those files. Watcher/watchdog receive the inbox configuration, not application credentials; vault/application jobs receive their existing `.env` read-only. State, archive ledgers, notification cooldowns, pending/uncertain items and publication retry budgets remain the same mounted files.

`SAU_OPS_CHECK_ONLY=1` makes the controller request `--check`: readable-mount validation only, with no app/provider action, state writes or credential output. After unit/full gates and immutable-image checks, verify each real mount this way before a guarded cron switch. Archive the exact previous cron bytes and wait for or reconcile its in-flight processes; never start a second scheduler or reset failed/uncertain publication state. Keep host environments until the last native user has been verified gone.

The watcher uses `SAU_WATCH_HOME` for its existing state/config/health directory and `SAU_INBOX_RCLONE_MODE=container` for local rclone and bounded temporary cleanup. The legacy Docker bridge remains available for old host installations. A busy scan lock skips the tick without advancing its heartbeat. Container-internal API/STT URLs translate only known loopback endpoints; custom configured routes are retained. The original autopublish kill switch, 24-hour grace, schedule intent and retry counters are not changed.

Privileged host media-offload/unlink coordination, Docker build/deploy controllers, disk safety, cron, SSH and system services are not replaced by a privileged Docker socket inside an application container. Manager's paused migration journal/lock and its active Teaching publisher must not be erased, replayed or replaced as a side effect of an SAU cron switch.

## Cleanup boundaries

Never remove a source clone/worktree without checking current Git status, preserved refs/commits, running processes/open files and container mounts. Keep a verified source archive before removing the redundant copy. Move valid recovery/scan/rollback evidence, do not erase it as old data. No global Docker/volume prune, no removal of active/uncertain media, no reset of physical attempts or provider budgets.

Native cron jobs must retain their exact input/config/lock and publication behavior when converted to containers. Do not delete a host venv or stop a watcher until its last user is accounted for and the replacement has been checked. No synthetic public posting, historical backfill, automatic approval or new accounts are part of this cleanup.

Push only the verified task branch normally. A main push can activate `ci-watch.sh`; never publish main before the same-revision release gates and controlled deployment checks are satisfied. Historical Git commits and all uncertainty/receipt facts stay intact.
