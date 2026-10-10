# Operational hygiene audit — 2026-10-10

Repo: `/home/will/social-auto-upload` (branch `main`, HEAD `2a39647`)
Host: uid `1000(will)` gid `1000(will)`; arm64/aarch64 Linux, `/` on `/dev/sda1`.
Audit time: 2026-10-10 ~05:0x UTC (13:0x local).
Constraints honoured: **nothing was deleted** (no data, no media, no backups, no
`/tmp` entries). No destructive command was run. The only file created is one
throw-away **copy** of a DB backup under `/tmp` for the integrity check (§2).

Baseline test suite result is at the bottom: **1559 passed, 1 skipped,
139 subtests passed** — matches the stated baseline.

---

## 1. FILE OWNERSHIP (uid 1000 vs root)

### Measured state

There are **zero** root-owned (uid 0) files or directories under `logs/` or
`generated/`. Every entry is `will:will` (uid 1000/gid 1000). The PermissionError
class of failure (`logs/jobs/*.log`) is **not present on disk right now**.

```console
$ find logs generated -uid 0 \( -type f -o -type d -o -type l \) \
    -printf '%u:%g %m %s %TY-%Tm-%Td %TH:%TM %p\n'
(no output — exit 0)

$ find logs generated -printf '%u\n' | sort | uniq -c
   2653 will

$ find logs/jobs -maxdepth 1 -printf '%u:%g %p\n' | sort | uniq -c | head
      1 will:will logs/jobs
      1 will:will logs/jobs/job-108.log
      1 will:will logs/jobs/job-108.log.1
      1 will:will logs/jobs/job-110.log
      1 will:will logs/jobs/job-110.log.1
      ...
```

Counts: `logs/` = 2,144 files (13 MB); `generated/` = 0 files (2.0 MB of empty
`generated/campaigns/campaign-*` directories — all artifacts already offloaded).

### What I ran

The `find … -uid 0` commands above (read-only). `id` to confirm the host user is
1000, matching the container uid.

### What I changed

**Nothing — there was nothing to fix.** The chown recipe was not needed:

```bash
# Only run this if root-owned files reappear under logs/ (regression guard):
docker run --rm --platform linux/arm64 \
  -v /home/will/social-auto-upload/logs:/logs alpine chown -R 1000:1000 /logs
# and the equivalent for generated/:
docker run --rm --platform linux/arm64 \
  -v /home/will/social-auto-upload/generated:/generated alpine chown -R 1000:1000 /generated
```

### What remains

Nothing on disk. Because the reported failure was intermittent (a container
process running as a different uid can recreate root-owned files), the operator
should re-run the `find … -uid 0` check after any deploy or manual
`docker exec` that writes logs. If it reappears, the two `docker run … chown`
lines above are the fix; they use the provided arm64 workaround (the host is
aarch64, so `--platform linux/arm64` avoids build/exec-format issues).

---

## 2. DB BACKUPS

### Measured state

16 backup files under `db/`, **676 MB total**, all `will:will`, mode `644`:

| # | mtime (local) | size (bytes) | file |
|---|---|---|---|
| 1 | 2026-10-10 02:24:17 | 45,473,792 | `db/database.db.bak-pre-register-videofile-20261010-022417` |
| 2 | 2026-10-09 22:36:39 | 45,453,312 | `db/database.db.bak-pre-2603-status-20261009-223638` |
| 3 | 2026-10-09 22:05:59 | 45,449,216 | `db/database.db.bak-pre-subsfix-20261009-220559` |
| 4 | 2026-10-09 18:27:11 | 45,105,152 | `db/database.db.bak-pre-2603-reprep-20261009-182711` |
| 5 | 2026-10-09 18:15:47 | 45,096,960 | `db/database.db.bak-pre-2599-fix-20261009-181547` |
| 6 | 2026-10-09 17:12:10 | 44,335,104 | `db/database.db.bak-pride-news-20261009-171229` |
| 7 | 2026-10-09 13:41:22 | 44,331,008 | `db/database.db.bak-pre-artifact-purge-20261009-054149` |
| 8 | 2026-10-09 13:40:04 | 44,331,008 | `db/database.db.bak-pre-purge-20261009-134004` |
| 9 | 2026-10-09 10:54:38 | 44,302,336 | `db/database.db.bak-drive-reconnect-20261009-105438` |
| 10 | 2026-10-09 00:54:24 | 44,220,416 | `db/database.db.bak-subfix-20261009-005424` |
| 11 | 2026-10-08 22:20:19 | 44,216,320 | `db/database.db.bak-20261008-222019` |
| 12 | 2026-10-08 19:41:59 | 44,060,672 | `db/database.db.before-dup-cancel-20261008194159.bak` |
| 13 | 2026-10-08 19:36:58 | 43,937,792 | `db/database.db.before-dup-recovery-20261008193657.bak` |
| 14 | 2026-10-08 15:59:50 | 43,012,096 | `db/database.db.before-wal-20261008155950.bak` |
| 15 | 2026-10-07 22:09:04 | 42,237,952 | `db/database.db.before-oldjob-cancel-20261007220904.bak` |
| 16 | 2026-10-07 21:49:45 | 42,180,608 | `db/database.db.before-stale-cancel-20261007214945.bak` |

Newest = **row 1**, `db/database.db.bak-pre-register-videofile-20261010-022417`
(45,473,792 bytes, 2026-10-10 02:24:17 local).

### Integrity check on the newest backup (verified on a copy)

Per the instruction, the newest backup was **copied** to `/tmp` and checked —
the original was never opened for writing:

```console
$ cp -p db/database.db.bak-pre-register-videofile-20261010-022417 \
        /tmp/sau-hygiene-backup-check-1791608784.db

$ .venv/bin/python - /tmp/sau-hygiene-backup-check-1791608784.db <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
print("integrity_check:", con.execute("PRAGMA integrity_check").fetchall())
print("quick_check:", con.execute("PRAGMA quick_check").fetchall())
print("user_version:", con.execute("PRAGMA user_version").fetchone())
print("tables:", con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0])
print("journal_mode:", con.execute("PRAGMA journal_mode").fetchone())
con.close()
PY
integrity_check: [('ok',)]
quick_check: [('ok',)]
user_version: (0,)
tables: 42
journal_mode: ('delete',)
```

Live DB, for comparison (read-only URI, no mutation):

```console
$ .venv/bin/python - <<'PY'
import sqlite3
con = sqlite3.connect("file:db/database.db?mode=ro", uri=True)
print("live integrity_check:", con.execute("PRAGMA integrity_check").fetchall())
print("live size (pages*page_size):", con.execute("PRAGMA page_count").fetchone()[0] * con.execute("PRAGMA page_size").fetchone()[0])
PY
live integrity_check: [('ok',)]
live size (pages*page_size): 45592576
```

The newest backup is a valid, complete SQLite DB (`ok`), so it is safe to rely
on for recovery.

### What I changed

Nothing in `db/`. One temporary copy was created at
`/tmp/sau-hygiene-backup-check-1791608784.db` (45 MB). It is disposable; the
operator may remove it with the command in §3.

### Recommendation — retention rule: **keep the newest 5, archive/remove older**

These are ad-hoc pre-change snapshots (not a rotating daily set). There is a
separate offsite daily backup (`scripts/sau-daily-backup.sh` → OneDrive
`備份/` + `share.iamwillywang.com`), which self-expires after 5 runs — verify
it is still scheduled before relying on it.

Sizes for the rule:

```console
$ find db -maxdepth 1 \( -name 'database.db*.bak*' -o -name 'database.db.before-*' \) \
    -printf '%T@ %s %p\n' | sort -rn | head -5 | awk '{s+=$2} END {printf "newest 5 = %d bytes (%.0f MB)\n", s, s/1048576}'
newest 5 = 216 MB
$ ... head -3 ...
newest 3 = 130 MB
```

- **Recommended:** keep the newest **5** (216 MB), removing the oldest 11,
  frees ≈ 460 MB. Each file is ~44 MB and they span < 3 days; a rolling
  newest-5 window is enough to cover any single bad change.
- **Aggressive:** keep the newest **3** (130 MB) and rely on the offsite daily
  backup for older recovery points.
- **Retighten the rule:** delete local snapshots older than **7 days** once the
  offsite daily backup is confirmed current.

Exact list/verify commands (all non-destructive; `rm` deliberately NOT run):

```bash
cd /home/will/social-auto-upload

# List newest-first with size/date (audit)
find db -maxdepth 1 \( -name 'database.db*.bak*' -o -name 'database.db.before-*' \) \
  -printf '%TY-%Tm-%Td %TH:%TM  %12s  %p\n' | sort -r

# Verify a candidate BEFORE deleting it
cp -p "db/<file>" /tmp/sau-verify.db
.venv/bin/python -c "import sqlite3;print(sqlite3.connect('/tmp/sau-verify.db').execute('PRAGMA integrity_check').fetchall())"

# Preview exactly what a newest-5 retention would remove (DRY RUN: prints only)
find db -maxdepth 1 \( -name 'database.db*.bak*' -o -name 'database.db.before-*' \) \
  -printf '%T@ %p\n' | sort -rn | tail -n +6 | cut -d' ' -f2-

# To actually delete, after reviewing the printed list (operator decision):
# find db ... | sort -rn | tail -n +6 | cut -d' ' -f2- | xargs -r rm -v
```

---

## 3. DISK AND STALE FILES

### Measured state

```console
$ df -h /
Filesystem      Size  Used Avail Use% Mounted on
/dev/sda1        97G   72G   26G  75% /
```

```console
$ du -sh logs/
13M     logs/            # 2,144 files

$ du -sh generated/
2.0M    generated/       # 0 files, only empty campaign-* dirs

$ du -sh .claude/worktrees/
438M    .claude/worktrees/

$ find /tmp -maxdepth 1 -name 'sau-*' | wc -l
585
$ du -sch /tmp/sau-* | tail -1
391M    total            # NOTE: includes the 44M DB-check copy made in §2
```

`__pycache__` in the checkout (excluding all `.venv`/`.git`/`node_modules`):

```console
$ find . -type d -name __pycache__ -not -path '*/.venv/*' \
     -not -path '*/.git/*' -not -path '*/node_modules/*' | wc -l
39
$ ... -print0 | du -ch --files0-from=- | tail -1
8.8M    total
```

The stale worktree's own `.venv` additionally holds **112** `__pycache__` dirs /
**24 MB** (already counted inside its 438 MB).

`/tmp/sau-*` breakdown by prefix:

| prefix | count | size | what it is |
|---|---:|---:|---|
| `sau-test-generated-*` | 211 | 293 M | pytest temp dirs (largest single: `…-vshnv3xi` = 267 M) |
| `sau-cleanup-test.db` | 1 | 37 M | test DB from 2026-10-05 |
| `sau-test-jobs-*` | 330 | 3.1 M | pytest temp job dirs |
| `sau-test-db-*` | 32 | 0 | pytest temp DB dirs |
| `sau-iso*` | 3 | 14 M | ISO/installer build leftovers |
| `sau-robust-hotfix-backup` | 1 | 736 K | backup copy of `worker.py` |
| `sau-worker-generated-restore-before` | 1 | 88 K | backup copy of `worker.py` |
| `sau-*-*.log`, `sau-spawn-prompt.txt`, `sau-draft-recon.sh`, `sau-upload-test.mp4` | 4 | ~275 K | ad-hoc session logs/scripts |
| `sau-hygiene-backup-check-1791608784.db` | 1 | 44 M | **created by this audit** (§2) |
| **total** | **585** | **391 M** | |

Age range of `/tmp/sau-*`: 2026-10-03 22:18 → 2026-10-10 10:38 (oldest ≈ 6 days).

Stale worktree:

```console
$ git worktree list
/home/will/social-auto-upload                                           2a39647 [main]
/home/will/social-auto-upload/.claude/worktrees/agent-a3429f7edffc909a3 65329a8 [worktree-agent-a3429f7edffc909a3]

$ git -C .claude/worktrees/agent-a3429f7edffc909a3 log -1 --format='%ci %h %s'
2026-09-29 20:27:30 +0800 65329a8 Polish stable Traditional Chinese schedule views

$ git -C .claude/worktrees/agent-a3429f7edffc909a3 status --short
 M uv.lock
$ git merge-base --is-ancestor worktree-agent-a3429f7edffc909a3 main && echo merged
merged
```

So the worktree is 11 days stale, its branch is **already merged into `main`**,
and its only uncommitted change is `uv.lock` (13 insertions / 2 deletions). The
438 MB is almost entirely the abandoned `.venv` (430 MB) inside it.

### What is safe to remove (listed, NOT executed)

| Target | Size | Safe? | Why |
|---|---:|---|---|
| `__pycache__` dirs in the checkout | 8.8 M | ✅ | Byte-compiled cache; regenerated on next import. |
| `/tmp/sau-test-*`, `/tmp/sau-cleanup-test.db`, `/tmp/sau-upload-test.mp4`, `/tmp/sau-iso*` | ~347 M | ✅ | Pytest/build scratch; no reference from the app. Test data, not production data. |
| `/tmp/sau-hygiene-backup-check-1791608784.db` | 44 M | ✅ | Copy made by this audit; delete freely. |
| `/tmp/sau-robust-hotfix-backup`, `/tmp/sau-worker-generated-restore-before` | 0.8 M | ⚠️ | Historical `worker.py` backups; harmless but may be wanted for forensics. |
| `.claude/worktrees/agent-a3429f7edffc909a3` | 438 M | ⚠️ | Branch merged; only `uv.lock` differs. Preserve that diff first, then `git worktree remove`. |
| `logs/` | 13 M | ❌ | Operational evidence; do not bulk-delete. `worker.log.1` (5.8 M) can be truncated/pruned by the existing log rotation. |

Suggested exact commands (review before running; recursive deletes are **not**
run by this audit):

```bash
cd /home/will/social-auto-upload

# 1. Python caches (safe, regenerated)
find . -type d -name __pycache__ -not -path '*/.venv/*' -not -path '*/.git/*' \
  -prune -print          # preview
find . -type d -name __pycache__ -not -path '*/.venv/*' -not -path '*/.git/*' \
  -prune -exec rm -rf -- {} +   # operator-run

# 2. /tmp test scratch (preview first)
du -sh /tmp/sau-test-* /tmp/sau-cleanup-test.db /tmp/sau-iso* 2>/dev/null | sort -rh
rm -rf /tmp/sau-test-* /tmp/sau-cleanup-test.db /tmp/sau-iso* /tmp/sau-hygiene-backup-check-*.db

# 3. Stale worktree — save the uv.lock diff first, then remove
git -C .claude/worktrees/agent-a3429f7edffc909a3 diff -- uv.lock > /tmp/worktree-uvlock.diff
git worktree remove .claude/worktrees/agent-a3429f7edffc909a3   # refuses if dirty; use --force only after saving the diff
git worktree prune
```

### What I changed

Nothing. `df -h /` is 75% full with 26 GB free; the removables above are ~800 MB
total, so this is tidiness, not capacity pressure.

---

## 4. DEPLOY GATE / FRONTEND `dist/` BUILD STEP

### The named documents do not exist

`AGENTS.md` and `HANDOFF.md` are **not present** anywhere in this repository
(including the worktree), and were never tracked by git. This was verified from
the repo root and from `/home/will`:

```console
$ cd /home/will/social-auto-upload
$ ls -la AGENTS.md HANDOFF.md
ls: cannot access 'AGENTS.md': No such file or directory
ls: cannot access 'HANDOFF.md': No such file or directory

$ find . \( -iname 'AGENTS.md' -o -iname 'HANDOFF.md' \) \
    -not -path '*/.venv/*' -not -path '*/.git/*'
(no output)

$ git log --all --name-only --pretty=format: | grep -iE '^AGENTS\.md$|^HANDOFF\.md$' | sort -u
(no output — never committed)

$ git stash show --name-only 'stash@{0}' | grep -iE 'agents|handoff'   # also 1, 2
(none)
```

**No parked deploy-gate text could be located**, so the summary below is
reconstructed from the closest documents that *do* exist — the agent
instructions `CLAUDE.md`, the rolling handoff log `docs/agent-bootstrap.md`, and
the CI/deploy reports — plus the actual workflow/Dockerfile/scripts. It is
labelled as reconstruction, not quoted from the missing files.

### Our closest handoff/governance docs say

- `docs/agent-bootstrap.md` (last handoff entries): line ~178 —
  *"前端 `sau_frontend/dist` 沒有 bind mount，後端與前端都要等映像重建（push main → Actions → Watchtower）才會生效"*
  (the frontend `dist` has no bind mount; both frontend and backend changes only
  take effect after the image is rebuilt). Line ~229 records the production
  outage caused by a Python f-string that was legal on the 3.12 dev venv but a
  `SyntaxError` on the image's 3.10 interpreter; the fix was to add
  `RUN python3 -m compileall` to the Dockerfile so the image **fails the build**.
- `docs/ci-watch.md` / `reports/ci-actions-reenable.md`: GitHub Actions is
  disabled on this fork, so the real deploy path is the cron watcher
  `scripts/ci-watch.sh` (and manual `scripts/deploy-local.sh`), both of which
  `docker build` directly and then restart the container.
- `CLAUDE.md` documents the build/run steps but contains no deploy gate.

### The factual deploy-gate situation in this repo

1. **CI jobs and the image build are decoupled.** `.github/workflows/ci.yml`
   defines `backend-tests`, `postgres-tests`, `frontend-build`
   (`npm ci && npm run build`) and `dependency-guard`. None of them is linked to
   `.github/workflows/image.yml`, which independently builds and pushes
   `:latest` on any push to `main` (its path filter includes `sau_frontend/**`).
   There is **no `needs:` anywhere** in `.github/workflows/`:

   ```console
   $ grep -rn "needs:" .github/workflows/
   (no output)
   ```

   So a commit that fails tests/frontend build can still be published as
   `:latest`.

2. **The live path is the watcher, and it has no test gate.**
   `scripts/ci-watch.sh` fetches `origin/main`, checks out the exact commit into
   a throwaway worktree, runs `docker build`, tags `:commit-<sha>` + `:latest`,
   restarts the service and waits for `/healthz == 200`. It never runs pytest or
   a standalone frontend build.

3. **The frontend build is nonetheless an implicit build gate.**
   `Dockerfile` stage 1 (`node:22-slim`) runs `npm install --legacy-peer-deps`
   and `npm run build`, then copies `/app/dist` into the runtime image. If that
   build fails, `docker build` fails and nothing is deployed. `sau_frontend/dist`
   is `.gitignore`d and is **not** bind-mounted; `sau_backend.py` serves it from
   the image when present, falling back to source/root only for legacy layouts.
   Consequence: a local `python sau_backend.py` run can serve a **stale host
   `dist/`** built on 2026-10-08, even after the Vue source changed.

### Crisp summary of the (likely) parked item

- **Problem.** There is no deploy gate that ties "tests/frontend build pass" to
  "image published and deployed". GitHub's `image.yml` publishes `:latest` from
  any push; the actual deploy path (`ci-watch.sh`) builds and ships
  `origin/main` with no test run. The frontend `dist/` is rebuilt inside the
  image (so it is not usually stale in production) but is unbundled and stale on
  the host for local runs.
- **Proposed fix (as parked — do NOT implement now).** Make the image/deploy
  step depend on the existing checks: e.g. gate `image.yml` on the CI jobs
  (`needs:`/`workflow_run`), and/or make `ci-watch.sh` run the same
  `pytest …` + `npm run build` before `docker build`, treating a failure as a
  no-deploy + Telegram alert. Optionally add a host-side `dist` freshness check
  for the dev server.
- **What could break.**
  - **Actions are disabled on this fork.** A `needs:`/`workflow_run` gate in
    `image.yml` would never fire here, so it would not gate the real
    (`ci-watch.sh`) deploy path; the local watcher must carry the gate too or
    the change is cosmetic.
  - **Flaky/slow tests become deploy blockers.** Putting the full suite in
    `ci-watch.sh` roughly doubles deploy time and can wedge the watcher
    (single-flight `flock`), so a hotfix that cannot pass an unrelated flaky
    test would stop being deployable automatically.
  - **Hotfix latency.** The compileall build gate already proved its value, but
    a full test gate is a much stronger barrier; keep an explicit operator
    bypass (`CI_WATCH_FORCE=1` / `--no-build`) if added.
  - **`dist` freshness checks are false-positive-prone** because `dist` is
    gitignored and legitimately absent on fresh checkouts; any gate must treat
    "missing" as "will be built by Docker", not as failure.
  - **The worktree is stale.** `ci-watch.sh` builds from a fresh `git worktree`
    of `origin/main`, so a gate reading the working tree could inspect the wrong
    tree.

### Exact commands to inspect (no change made)

```bash
cd /home/will/social-auto-upload

# Where a gate would attach:
sed -n '1,95p' .github/workflows/ci.yml          # CI jobs (frontend-build at ~line 73)
cat .github/workflows/image.yml                  # publish job; has no needs:

# The real deploy path:
sed -n '1,140p' scripts/ci-watch.sh
cat scripts/deploy-local.sh

# Frontend build + Python compile gates already inside the image build:
sed -n '1,60p' Dockerfile

# The stale-host-dist note in the handoff log:
grep -n "dist" docs/agent-bootstrap.md

# Docs that DO exist (there is no AGENTS.md / HANDOFF.md):
ls AGENTS.md HANDOFF.md 2>&1
```

**Decision required by the operator before implementation** — the previous
session deliberately parked this, so it should stay parked until the gate's
scope (GitHub-side vs `ci-watch.sh`-side), the test selection, and the bypass
policy are agreed.

---

## Test suite result (required)

```console
$ cd /home/will/social-auto-upload
$ .venv/bin/python -m pytest tests/ --ignore=tests/test_security_http.py -q
........................................................................ [  4%]
...
........................................................................ [100%]
1559 passed, 1 skipped, 139 subtests passed in 120.68s (0:02:00)
```

**1559 passed, 1 skipped, 139 subtests passed** — exactly the stated baseline.

---

## Change summary

| Item | Changed? |
|---|---|
| Root-owned files under `logs/`+`generated/` | None found; nothing chowned |
| DB backups | None added/removed; one temp copy in `/tmp` for verification |
| Disk / stale files | Nothing deleted; removal candidates listed with commands |
| Deploy gate docs | Not implemented; missing-doc fact reported + reconstructed summary |
| Tracked source files | Unmodified (`git status` shows only untracked reports/backups) |
