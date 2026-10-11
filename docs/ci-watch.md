# Local CI watcher (`scripts/ci-watch.sh`)

> **Status note (2026-10-11).** GitHub Actions *was* disabled on this fork
> (`reports/ci-actions-reenable.md`), which is why this watcher exists. As of
> 2026-10-11 the Actions API reports `enabled: true` and runs for
> `origin/main` are succeeding again, so this watcher is currently a redundant
> but harmless fallback. Keep it installed until Actions has been observed
> healthy for a while; remove the cron entry only if Actions is confirmed
> working (see the operator notes in `reports/ci-actions-reenable.md`).

GitHub Actions was disabled on this fork (see
[`reports/ci-actions-reenable.md`](../reports/ci-actions-reenable.md)), so pushes
to `origin/main` no longer built an image; `scripts/ci-watch.sh` is the
GitHub-independent fallback that does.

## What it does

On each run it:

1. `git fetch origin main` and reads that commit (never the local branch).
2. If that commit is already deployed **and** the running container uses its
   image, it exits.
3. Otherwise it checks out that exact commit into a throwaway `git worktree`
   (so a dirty working tree is neither shipped nor a blocker) and runs
   `docker build`.
4. Tags the image `:commit-<sha>` and `:latest`, runs the image's import check,
   restarts the `social-auto-upload` service with `--pull never`, and waits for
   `/healthz` to return `200`.
5. Records the deployed SHA in `logs/ci-watch.state`.

If any step fails it logs to `logs/ci-watch.log`, writes the build output to
`logs/ci-watch-build-<sha>.log`, and sends a best-effort Telegram alert (creds
read from `.env`).

## Scheduling

Installed as a per-user cron entry:

```
*/2 * * * * /bin/bash /home/will/social-auto-upload/scripts/ci-watch.sh >/dev/null 2>&1
```

An flock on `logs/ci-watch.lock` guarantees only one watcher runs at a time, so
overlapping ticks are safe.

## Manual use

```bash
scripts/ci-watch.sh                    # exactly what cron runs
CI_WATCH_FORCE=1 scripts/ci-watch.sh   # redeploy even if the marker matches
CI_WATCH_DRY_RUN=1 scripts/ci-watch.sh # report only; change nothing
CI_WATCH_TEST_ALERT=1 scripts/ci-watch.sh # send one test Telegram alert, then exit
```

## Relationship to `scripts/deploy-local.sh`

`deploy-local.sh` builds and deploys the **current working tree**. `ci-watch.sh`
builds and deploys **`origin/main`**, autonomously. If Actions stays enabled, the
watcher is redundant and the cron entry can eventually be removed; the workflows
are untouched and keep working either way.

_Verified: on 2026-10-10 the watcher built and deployed the immediately-following commit from `origin/main` (see `logs/ci-watch.log`)._
