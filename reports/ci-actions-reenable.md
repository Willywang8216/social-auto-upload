# GitHub Actions re-enable attempt + local CI fallback — verified report

Date: 2026-10-10
Repository: `Willywang8216/social-auto-upload` (fork of `dreammis/social-auto-upload`)
Predecessor: `reports/ci-actions-fix.md` (2026-10-09 diagnosis)
Outcome: **GitHub Actions cannot be re-enabled from this host/API session. A
local cron watcher now builds and deploys `origin/main` on every push, and it
has been proven end-to-end.**

---

## TL;DR

* **Re-verified first**, in case it had recovered: it had not. The newest
  workflow run is still `d1319216` (CI, `cancelled`, 2026-10-09T01:53:18Z).
  Dispatching anything returns `HTTP 422: Actions has been disabled for this
  repository.` The `/actions/permissions` endpoint still lies (`enabled:true`).
* **The disable is specific to this one fork.** The parent
  (`dreammis/social-auto-upload`) reports no banner, and 8 other forks of the
  same parent report no banner. Sibling repositories on the same account ran
  Actions after this repo went dark. The account is a **User** with **no
  orgs**, so no organisation policy applies.
* **The fork-usage disable is not exposed by any REST endpoint**; every write
  path (`PUT /actions/permissions` enabled true/false/true, `PUT
  /workflows/{id}/enable`) returns success and changes nothing actionable. This
  is GitHub's fork Actions-usage policy, clearable only by a maintainer in the
  GitHub UI (or GitHub Support).
* **A self-hosted runner does not help**: a runner registration token can still
  be minted, but workflow *run creation itself* is what is blocked (422 on
  dispatch), so no runner will ever be handed a job.
* **Implemented and tested** `scripts/ci-watch.sh` + cron: it fetches
  `origin/main`, builds from a clean worktree of that exact commit, deploys the
  service, verifies `/healthz`, and alerts Telegram on failure. Proof below:
  commits `96bd767` and `f5be35a` were each detected, built, and deployed, with
  the running container image ID matching. The alert channel was tested live.

---

## 1. Re-verification at the start of this task

Current HEAD when this task began was `09f852f`; `origin/main` had advanced to
`e791421` (a parallel session) by the time the watcher first ran.

Newest runs (the `gh run list` "only Copilot" result is a red herring — see
§2.6):

```
$ gh api "repos/Willywang8216/social-auto-upload/actions/runs?per_page=3" \
    --jq '.workflow_runs[] | [.created_at,.name,.conclusion,.head_sha[0:8]] | @tsv'
2026-10-09T01:53:18Z  CI                      cancelled  d1319216
2026-10-09T01:45:24Z  Build & Push App Image  success    edba1f3a
2026-10-09T01:45:24Z  CI                      success    edba1f3a
```

Every commit after `d1319216` still has **zero** `github-actions` check suites
(24 commits checked, all `actions_suites=0`), so GitHub's event handler is still
not creating runs.

`d1319216`'s run is the one that was in flight when the disable took effect:

```
$ gh api .../actions/runs/37871921496/jobs \
    --jq '.jobs[] | [.name,.status,(.conclusion//"-")] | @tsv'
backend-tests      completed  cancelled
frontend-build     completed  success
dependency-guard   completed  success
postgres-tests     completed  success
```

### 1.1 Manual dispatch — still refused

```
$ gh api -X POST repos/Willywang8216/social-auto-upload/actions/workflows/298855786/dispatches -f ref=main
{"message":"Actions has been disabled for this repository.",
 "documentation_url":"https://docs.github.com/rest/actions/workflows#create-a-workflow-dispatch-event",
 "status":"422"}
gh: Actions has been disabled for this repository. (HTTP 422)
```

`gh workflow run ci.yml --ref main` separately said `workflow ci.yml not found
on the default branch` — another red herring: the file **is** on `main` (the
branch SHA and the contents API both show it), but GitHub reports "not found"
for a disabled repository.

### 1.2 The banner is still on the public Actions page

```html
<div class="tmp-mb-4 flash flash-warn mt-2 tmp-mt-2">
  <svg ... class="octicon octicon-alert">…</svg>
  Workflows aren’t being run on this fork because of its GitHub Actions usage.
  A repository maintainer can re-enable them.
</div>
```

("495 workflow runs" is rendered right beneath it.)

### 1.3 The repo-level permission endpoint still says enabled

```
$ gh api repos/Willywang8216/social-auto-upload/actions/permissions
{"enabled":true,"allowed_actions":"all","sha_pinning_required":false}
```

This is the central inconsistency: the toggle endpoint says enabled, while the
enforcement gate says disabled.

---

## 2. Fork-specific investigation

### 2.1 The repo is a fork; the parent is healthy

```
$ gh api repos/Willywang8216/social-auto-upload --jq '{fork, parent:.parent.full_name, source:.source.full_name}'
{"fork":true,"parent":"dreammis/social-auto-upload","source":"dreammis/social-auto-upload"}
```

Parent: `archived:false`, `disabled:false`, `fork:false`, and its Actions page
shows **no** "Actions usage" banner. Its only workflow is the GitHub-managed
`Copilot` dynamic workflow. So nothing is propagating down from the parent.

### 2.2 It is not all forks of this parent

Checked the Actions page of the 8 newest forks of `dreammis/social-auto-upload`:

```
todotobe1/social-auto-upload        banner=0
BlockQuery/social-auto-upload       banner=0
jimli0514/social-auto-upload        banner=0
LumioraStudio/social-auto-upload    banner=0
735140144/social-auto-upload        banner=0
todobugs/social-auto-upload         banner=0
JohnsonChen2007/social-auto-upload  banner=0
LIUNANYAN/social-auto-upload        banner=0
```

Only `Willywang8216/social-auto-upload` shows the banner. The disable is
**repository-specific**.

### 2.3 Account type and org policy

```
$ gh api /user --jq '{login, type, id}'
{"login":"Willywang8216","type":"User","id":80213096}

$ gh api /user/orgs
[]          # no organisations, so no org-level Actions policy can apply
```

### 2.4 Repo Actions policy endpoints

```
actions/permissions                       -> {"enabled":true,"allowed_actions":"all","sha_pinning_required":false}
actions/permissions/selected-actions      -> 409 "All actions and workflows are allowed on this repository"
actions/permissions/workflow              -> {"default_workflow_permissions":"write","can_approve_pull_request_reviews":true}
actions/permissions/access                -> 422 "Access policy only applies to internal and private repositories."
actions/runners                           -> total_count 0
```

`actions/permissions/enabled` is not a real endpoint (404). There is **no**
endpoint that represents or toggles the fork-usage disable.

### 2.5 Billing — reported, not guessed

```
$ gh api /users/Willywang8216/settings/billing/actions
{"message":"Not Found","documentation_url":"https://docs.github.com/rest/billing/billing#get-github-actions-billing-for-a-user","status":"404"}
gh: This API operation needs the "user" scope. To request it, run: gh auth refresh -h github.com -s user
```

The token scopes are `gist`, `read:org`, `repo`; the `user` scope is absent, so
the billing API cannot be read. It is not needed: the account-level hypothesis
is already disproven, because sibling repos on the same account ran Actions
*after* this repo went dark (e.g. `auto-read-liunxdo-my` at
`2026-10-09T03:51:39Z`, ~2h later).

### 2.6 Red herring: why `gh run list` / `gh workflow list` looked wrong

Without an explicit `-R`, `gh` resolved the repo to the **upstream** remote
(`dreammis/social-auto-upload`), which has only the July `Copilot` runs. All
commands in this report address `Willywang8216/social-auto-upload` explicitly.
The fork's own list shows the real history:

```
$ gh run list --repo Willywang8216/social-auto-upload --limit 3
completed  cancelled  Record the pipeline-audit execution log   CI  main  push  37871921496  ...
```

### 2.7 Self-hosted runner probe

A runner registration token can still be minted:

```
$ gh api -X POST repos/Willywang8216/social-auto-upload/actions/runners/registration-token
{"token":"ATD7I2…","expires_at":"2026-10-10T10:22:38.742+08:00"}
```

But this does not help: the 422 above is returned when GitHub tries to
**create the run**, before any runner is selected. With run creation blocked,
no self-hosted runner can ever receive a job. Ruled out.

---

## 3. Re-enable attempts (all returned success, none cleared it)

```
# set enabled=true (proper JSON boolean)           -> 204, flag unchanged
$ echo '{"enabled": true, "allowed_actions": "all"}' | \
    gh api -X PUT repos/Willywang8216/social-auto-upload/actions/permissions --input -

# toggle false, then true                          -> 204 each, flag follows for reads,
#                                                     but dispatch still 422
# PUT /actions/workflows/{id}/enable for all three -> 204 each, dispatch still 422

$ gh api -X POST .../actions/workflows/298855786/dispatches -f ref=main
HTTP 422: Actions has been disabled for this repository.
```

There is no documented REST or GraphQL endpoint for the fork-usage flag, and
the re-enable control is only rendered to an authenticated maintainer in the
web UI, which cannot be driven from an API token.

---

## 4. Root cause and why it is not fixable from here

**Root cause (GitHub-side):** GitHub's fork Actions-usage policy disabled
Actions on this one fork. This is a distinct flag from
`actions/permissions.enabled`:

* `actions/permissions.enabled` = `true` (readable and writable via REST),
* the fork-usage gate = *disabled* (not represented in any API; enforced by
  returning 422 for every dispatch and by not creating any check suites).

Because the gate is not exposed, the only paths to clear it are:

1. The maintainer clicking the re-enable control on
   <https://github.com/Willywang8216/social-auto-upload/actions> while logged
   in as `Willywang8216`; or
2. A GitHub Support ticket asking them to re-enable Actions on the fork.

Neither is reachable from this host or this API token. This is the precise
reason it cannot be fixed from here.

The trigger was *not* any of the obvious local causes (all checked in the
predecessor report and re-confirmed): not a YAML error (workflows list
`active`), not a `paths:` filter (`ci.yml` has none; an empty probe commit
produced no run), not `concurrency:` (none declared), not rulesets/branch
protection (none), not the repo being archived/disabled, not an account-wide or
billing limit. The repo's usage is nonetheless very high for a fork — 495 runs
and 827 active caches (~3.6 GB) — and the in-flight `d1319216` cancellation at
`01:55:55Z` is the best available timestamp for when the disable took effect.
The exact threshold is not exposed by any API.

**Constraints honoured:** no secrets printed; no force-push or history rewrite
(plain fast-forward pushes only); the repo was **not** un-forked; the three
existing workflows were left byte-for-byte unchanged for when Actions returns.

---

## 5. Alternatives considered

| Option | Verdict |
| --- | --- |
| **Un-fork / convert to a standalone repo** | Would reset the fork-usage flag, but `gh` cannot un-fork: it requires deleting the repo and re-creating it (losing issues/PRs/stars/ghcr package links), or asking GitHub Support to detach it. High risk, not done. Steps/risks: delete `Willywang8216/social-auto-upload`, create a fresh non-fork, push the same `main`, re-set `CR_PAT` and package visibility. History is preserved by the local clone, but every repo-scoped setting (secrets, webhooks, package linkage) must be rebuilt. |
| **Self-hosted runner** | Does not help — run creation is blocked before runner selection (see §2.7). |
| **GitHub webhook → local listener** | Possible, but needs a public endpoint (or `gh webhook forward`) and a long-running listener; more moving parts and a new inbound attack surface than a local cron job. The repo currently has **no** webhooks. |
| **`git post-receive` hook** | Not applicable: pushes go to GitHub, not a local bare repo. |
| **Local cron watcher** | **Implemented and proven below.** Local, verifiable, and independent of GitHub. |

---

## 6. What was implemented

### 6.1 `scripts/ci-watch.sh`

Runs on a schedule; on each tick it:

1. `git fetch origin main` (read-only) and resolves that commit.
2. Exits if that commit is already deployed **and** the running container uses
   its image (with a drift check so a manual revert is auto-corrected).
3. Checks out `origin/main` into a throwaway `git worktree`, so a **dirty
   working tree is neither shipped nor a blocker**, and runs `docker build` for
   `:commit-<sha>`. Build output goes to
   `logs/ci-watch-build-<sha>.log`.
4. Tags `:commit-<sha>` and `:latest`, runs the image's import check, restarts
   `social-auto-upload` with `--pull never`, and waits for `/healthz == 200`.
5. Only then records the SHA in `logs/ci-watch.state`.

Safety: `flock` on `logs/ci-watch.lock` makes it single-flight (**two ticks
cannot overlap**); it never commits/checks out/resets the main worktree; every
failure logs and sends a best-effort Telegram alert (creds read from `.env`,
never printed). Modes: `CI_WATCH_FORCE=1`, `CI_WATCH_DRY_RUN=1`,
`CI_WATCH_TEST_ALERT=1`.

### 6.2 Documentation

`docs/ci-watch.md` — operator-facing description and manual commands.

### 6.3 Scheduling (installed)

```
$ crontab -l | tail -1
*/2 * * * * /bin/bash /home/will/social-auto-upload/scripts/ci-watch.sh >/dev/null 2>&1
```

A minimal-environment run confirms the script needs nothing beyond cron's PATH
and `$HOME`:

```
$ env -i HOME=/home/will PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    SHELL=/bin/bash /bin/bash /home/will/social-auto-upload/scripts/ci-watch.sh
[2026-10-10T01:26:24Z] origin/main=f5be35a last_deployed=f5be35a
[2026-10-10T01:26:24Z] already deployed and running; nothing to do
```

---

## 7. Proof it works

### 7.1 Deploy 1 — the watcher commit itself (`96bd767`)

```
$ scripts/ci-watch.sh
[2026-10-10T01:24:20Z] origin/main=96bd767 last_deployed=
[2026-10-10T01:24:20Z] building ...:commit-96bd767 from clean worktree ...
[2026-10-10T01:25:22Z] build finished for 96bd767
[2026-10-10T01:25:22Z] verifying the image runs
[2026-10-10T01:25:24Z] waiting for http://localhost:5409/healthz
[2026-10-10T01:25:26Z] deployed 96bd767 (healthz 200; container: Up 2 seconds)
real 1m8.886s
```

### 7.2 Idempotency — a second run does nothing

```
$ scripts/ci-watch.sh
[2026-10-10T01:25:33Z] origin/main=96bd767 last_deployed=96bd767
[2026-10-10T01:25:33Z] already deployed and running; nothing to do
```

### 7.3 Deploy 2 — the trivial smoke-test commit (`f5be35a`)

A one-line doc commit was pushed, then the watcher picked it up on its own:

```
$ git rev-parse --short origin/main
f5be35a

$ scripts/ci-watch.sh
[2026-10-10T01:25:47Z] origin/main=f5be35a last_deployed=96bd767
[2026-10-10T01:25:47Z] building ...:commit-f5be35a from clean worktree ...
[2026-10-10T01:25:55Z] build finished for f5be35a
[2026-10-10T01:25:59Z] deployed f5be35a (healthz 200; container: Up 2 seconds)
real 0m14.637s

# the running container is exactly the image built for the pushed commit:
$ docker inspect -f '{{.Image}}' social-auto-upload
sha256:e48db8ec73e1e4765559a1d31d444fec31f16d214a13a9555791d4566f8eee12
$ docker image inspect -f '{{.Id}}' ghcr.io/willywang8216/social-auto-upload:commit-f5be35a
sha256:e48db8ec73e1e4765559a1d31d444fec31f16d214a13a9555791d4566f8eee12
$ curl -s -o /dev/null -w '%{http_code}\n' http://localhost:5409/healthz
200
$ cat logs/ci-watch.state
f5be35a8e577a6a4a513babefb5f1e77027cf394
```

### 7.4 Failure alert channel — tested live

```
$ CI_WATCH_TEST_ALERT=1 scripts/ci-watch.sh
[2026-10-10T01:26:12Z] sending a test alert (no action needed)
[2026-10-10T01:26:13Z]   failure alert delivered
```

A real build/deploy failure takes the same path: log to
`logs/ci-watch.log` (+ the per-build log) and a Telegram alert.

### 7.5 Summary table

| Commit | Detected as `origin/main` | Image `:commit-<sha>` built | Running container == that image | healthz |
| --- | --- | --- | --- | --- |
| `96bd767` | yes | yes | yes | 200 |
| `f5be35a` | yes | yes | yes | 200 |

---

## 8. Operator notes

* **When Actions is re-enabled** (maintainer clicks the banner, or Support does
  it): verify with
  `gh api -X POST repos/Willywang8216/social-auto-upload/actions/workflows/298855786/dispatches -f ref=main`
  and then remove the cron line for `scripts/ci-watch.sh`. The workflows were
  never modified, so they resume as before. The watcher is harmless if left in
  place — it will simply find the running image already matches and exit — but
  it is redundant once Actions works.
* **To force a local redeploy of `origin/main` now:**
  `CI_WATCH_FORCE=1 scripts/ci-watch.sh`.
* **Logs:** `logs/ci-watch.log` (decisions, deploys, errors),
  `logs/ci-watch-build-<sha>.log` (full `docker build` output),
  `logs/ci-watch.state` (last successfully deployed SHA).
* **Invariants:** the watcher is arm64-only (matching this host); it keeps the
  `:latest` tag in sync so `docker-compose.yml`'s `pull_policy: always` is
  overridden with `--pull never` at deploy time, exactly like
  `scripts/deploy-local.sh`.
