# Local CI watcher (`scripts/ci-watch.sh`)

GitHub Actions is disabled on this fork (see
[`reports/ci-actions-reenable.md`](../reports/ci-actions-reenable.md)), so pushes
to `origin/main` no longer build an image. `scripts/ci-watch.sh` is the
GitHub-independent replacement.

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
builds and deploys **`origin/main`**, autonomously. When Actions is re-enabled,
the watcher becomes redundant and the cron entry can be removed; the workflows
are untouched and keep working.

_Verified: on 2026-10-10 the watcher built and deployed the immediately-following commit from `origin/main` (see `logs/ci-watch.log`)._
