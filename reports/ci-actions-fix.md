# GitHub Actions stopped creating workflow runs — diagnosis and resolution

Date: 2026-10-09
Repository: `Willywang8216/social-auto-upload` (a fork of `dreammis/social-auto-upload`)
Investigator: pi agent session

## TL;DR

**Root cause (verified): GitHub disabled Actions on this repository because of its
GitHub Actions usage on a fork.** The repository's own Actions page states it verbatim:

> Workflows aren't being run on this fork because of its GitHub Actions usage.
> A repository maintainer can re-enable them.

The API confirms it: any workflow dispatch returns
`HTTP 422: Actions has been disabled for this repository.`

This is **repository-specific, not account-wide**: other repositories owned by the
same account (`Willywang8216`) ran workflows as recently as
`2026-10-09T03:51:39Z`, after this repo went dark. It is therefore **not** a billing
limit, an organisation policy, or an account suspension.

The disable **cannot be cleared through the REST API.** `PUT /actions/permissions`
with `enabled:true` returns `204` but does not clear the flag; toggling it off/on and
`PUT /actions/workflows/{id}/enable` also have no effect, and the banner persists.
Only a repository maintainer can re-enable it from the GitHub UI Actions tab.

Because the cause is GitHub-side and not fixable from here, the working fallback is
the local build + deploy path `scripts/deploy-local.sh` (built, deployed, and verified
healthy as part of this work). Exact commands are in
[Fallback](#fallback-local-build--deploy).

---

## 1. Symptom reconfirmed

```
$ git status --short        # (trimmed; unrelated untracked db backups omitted)
?? AGENT_MANAGER_SESSION_GUIDE.md
?? myUtils/schedule_shift.py
 M sau_backend.py
 M sau_cli.py
```

```
$ gh api "repos/Willywang8216/social-auto-upload/actions/runs?per_page=20" \
    --jq '.workflow_runs[] | [.created_at,.status,(.conclusion//"-"),.event,.name,.head_sha[0:8]] | @tsv'
2026-10-09T01:53:18Z  completed  cancelled  push  CI                      d1319216
2026-10-09T01:45:24Z  completed  success    push  Build & Push App Image  edba1f3a
2026-10-09T01:45:24Z  completed  success    push  CI                      edba1f3a
2026-10-09T01:41:52Z  completed  success    push  CI                      c2aa2eb6
2026-10-09T01:41:52Z  completed  success    push  Build & Push App Image  c2aa2eb6
2026-10-09T01:36:06Z  completed  success    push  Build & Push App Image  2e446171
2026-10-09T01:36:06Z  completed  success    push  CI                      2e446171
2026-10-09T01:28:32Z  completed  success    push  Build & Push App Image  7a7db06d
2026-10-09T01:28:32Z  completed  success    push  CI                      7a7db06d
...
```

The newest run is indeed `d1319216` (CI, cancelled). Every commit after it
(`e25e28a`, `0e9f8f8`, `051987a`, `8cce09b`, `de8a2b9`, `be2db41`, `2481aca`,
`7587c18`, and two commits made later by a parallel session, `d99e3a3` and
`927625e`) produced **zero** runs.

`d1319216`'s own job breakdown shows a workflow that was mid-flight when it was
cancelled — three jobs finished, the long one was cut off:

```
$ gh api repos/Willywang8216/social-auto-upload/actions/runs/37871921496/jobs \
    --jq '.jobs[] | [.name,.status,(.conclusion//"-"),.started_at,.completed_at] | @tsv'
backend-tests      completed  cancelled  01:53:21  01:55:54
frontend-build     completed  success    01:53:21  01:53:38
dependency-guard   completed  success    01:53:21  01:53:26
postgres-tests     completed  success    01:53:21  01:54:06
```

The run was cancelled at `01:55:55Z`, roughly two minutes after it started, and
nothing has run since. This timing is consistent with GitHub aborting in-flight
work when it disabled Actions (see [Root cause](#8-root-cause)).

---

## 2. Are the workflow files on `origin/main` present, valid, and triggered?

The local tree is identical to `origin/main`, and all three workflow files exist,
are byte-identical to local, and have no `paths` filter on `ci.yml`.

```
$ git fetch origin main
$ git rev-parse HEAD origin/main
7587c18...   # at the time of the check; later advanced by a parallel session
7587c18...
$ git ls-tree -r --name-only origin/main -- .github/
.github/workflows/base-image.yml
.github/workflows/ci.yml
.github/workflows/image.yml
$ git diff HEAD origin/main -- .github/     # (empty)
```

`ci.yml` trigger block (no paths filter):

```yaml
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
```

All three workflows enumerate as `active` via the API (the `gh workflow list`
"only Copilot" observation is a red herring — see
[Section 11](#11-red-herrings)):

```
$ gh api "repos/Willywang8216/social-auto-upload/actions/workflows?per_page=100" --paginate \
    --jq '.workflows[] | [.id,.name,.state,.path] | @tsv'
281994318  Build & Push Base Image  active  .github/workflows/base-image.yml
298855786  CI                      active  .github/workflows/ci.yml
340590840  Build & Push App Image  active  .github/workflows/image.yml
```

A malformed YAML file is ruled out: the files parse, GitHub lists them as valid
workflows, and `d1319216`'s `ci.yml` ran successfully in the same workflow before the
disable. It is not a per-file parse failure.

---

## 3. Repo-level Actions policy probes

```
$ gh api repos/Willywang8216/social-auto-upload/actions/permissions
{"enabled":true,"allowed_actions":"all","sha_pinning_required":false}

$ gh api repos/Willywang8216/social-auto-upload/actions/permissions/selected-actions
{"message":"Conflict","errors":"All actions and workflows are allowed on this repository", ...}
409

$ gh api repos/Willywang8216/social-auto-upload/actions/permissions/workflow
{"default_workflow_permissions":"write","can_approve_pull_request_reviews":true}

$ gh api repos/Willywang8216/social-auto-upload/actions/permissions/access
{"message":"Access policy only applies to internal and private repositories.", ...} 422

$ gh api repos/Willywang8216/social-auto-upload/actions/permissions/fork-pr-contributor-approval
{"approval_policy":"first_time_contributors"}

$ gh api repos/Willywang8216/social-auto-upload/actions/permissions/fork-pr-workflows
404 (private-repo-only setting)

$ gh api repos/Willywang8216/social-auto-upload/actions/runners --jq .total_count
0
```

There is no repo ruleset and no branch protection that could suppress runs:

```
$ gh api repos/Willywang8216/social-auto-upload/rulesets          -> []
$ gh api repos/Willywang8216/social-auto-upload/rules/branches/main -> []
$ gh api repos/Willywang8216/social-auto-upload/branches/main/protection
404 Branch not protected
```

Note the central contradiction that made this hard to see: the granular
`actions/permissions` endpoint reports `enabled: true`, while the fork-usage disable
is a **separate flag that the REST API does not expose and `PUT` does not clear**
(see [Section 7](#7-manual-trigger-attempts-and-api-re-enable-attempts)).

---

## 4. Account / billing probes

The billing endpoint could not be read with the current token — this is reported as
such, not guessed:

```
$ gh auth status
Token scopes: 'gist', 'read:org', 'repo'

$ gh api /users/Willywang8216/settings/billing/actions
{"message":"Not Found", ...} 404
gh: This API operation needs the "user" scope...

$ gh api /users/Willywang8216/settings/billing/shared-storage
404
```

The public repo has no billed minutes, and the account-level hypothesis is
**directly disproven** by comparing sibling repositories — they ran Actions after this
repo went dark:

```
$ gh api users/Willywang8216/repos?per_page=100&sort=pushed --jq '.[].full_name' | ...
Willywang8216/ipblocklist                 workflows=3  last_run=2026-10-08T23:10:17Z
Willywang8216/octopus                     workflows=4  last_run=2026-05-26T07:14:37Z
Willywang8216/anyrouter-check-in          workflows=4  last_run=2026-10-08T23:56:58Z
Willywang8216/auto-read-liunxdo-my        workflows=16 last_run=2026-10-09T03:51:39Z
Willywang8216/inbox-zero-will             workflows=7  last_run=none
Willywang8216/obsidian-yoloAI             workflows=2  last_run=none
```

`auto-read-liunxdo-my` ran at `2026-10-09T03:51:39Z`, i.e. ~2 hours *after* this repo's
last run. So Actions is not disabled for the account.

---

## 5. What changed around `d1319216`?

* No commit after `d1319216` touched `.github/`:
  ```
  $ git diff --stat d1319216 7587c18 -- .github/
  (empty)
  ```
* No large file was introduced. The largest blobs added after `d1319216` were source
  files of 236/228/185 lines; the repo `size` is ~55 MB and no object is near the
  100 MB limit.
* The working tree's large `db/database.db.*.bak` files are untracked and excluded by
  `.dockerignore`; they are not in any pushed commit.
* The push events themselves are all recorded, so the pushes definitely reached
  GitHub. The problem is downstream of the push:
  ```
  $ gh api "repos/Willywang8216/social-auto-upload/events?per_page=30" --jq '...push...'
  2026-10-09T03:35:18Z  before=051987a...  head=8cce09b4...  ref=refs/heads/main
  2026-10-09T03:18:01Z  before=0e9f8f8...  head=051987a1...  ref=refs/heads/main
  2026-10-09T01:53:16Z  before=edba1f3...  head=d1319216...  ref=refs/heads/main
  ```

The decisive change is visible in the **check suites** attached to each commit.
Working commits get a `github-actions` check suite; every commit after `d1319216` gets
none — only third-party apps:

```
$ gh api repos/.../commits/2e44617/check-suites --jq '.check_suites[] | [.app.slug,.status] | @tsv'
cloudflare-workers-and-pages  queued
digitalocean                  queued
vercel                        queued
claude                        queued
github-actions                completed      <-- present (working)

$ gh api repos/.../commits/e25e28a/check-suites --jq ...
cloudflare-workers-and-pages  queued
digitalocean                  queued
vercel                        queued
claude                        queued
                               <-- no github-actions row at all (dead)
```

Same result for `0e9f8f8`, `051987a`, `8cce09b`, `de8a2b9`, `be2db41`, `2481aca`,
`7587c18`, `d99e3a3`, `927625e`. GitHub's Actions event handler stopped creating
check suites for this repo at the same point.

---

## 6. Concurrency groups

None of the three workflows declares `concurrency:`. A cancelled run of a concurrency
group therefore cannot be blocking later runs.

```
$ grep -n concurrency .github/workflows/*.yml
(no matches)
```

---

## 7. Manual trigger attempts and API re-enable attempts

`ci.yml` does not declare `workflow_dispatch`, but GitHub checks the disabled state
before the trigger, so every dispatch attempt returns the same 422:

```
$ gh workflow run image.yml     -R Willywang8216/social-auto-upload --ref main
could not create workflow dispatch event: HTTP 422:
  Actions has been disabled for this repository.
  (…/actions/workflows/340590840/dispatches)

$ gh workflow run ci.yml        -R Willywang8216/social-auto-upload --ref main
HTTP 422: Actions has been disabled for this repository.  (…/298855786/dispatches)

$ gh workflow run base-image.yml -R Willywang8216/social-auto-upload --ref main
HTTP 422: Actions has been disabled for this repository.  (…/281994318/dispatches)
```

Attempts to clear it through the REST API all returned success but had no effect:

```
# set enabled=true (proper JSON boolean)
$ echo '{"enabled": true, "allowed_actions": "all"}' | \
    gh api -X PUT repos/Willywang8216/social-auto-upload/actions/permissions --input -
exit=0        # 204
$ gh api repos/.../actions/permissions
{"enabled":true,"allowed_actions":"all","sha_pinning_required":false}   # unchanged

# toggle off then on
$ echo '{"enabled": false}' | gh api -X PUT .../actions/permissions --input -   # 204
$ gh api .../actions/permissions -> {"enabled":false,...}
$ echo '{"enabled": true,"allowed_actions":"all"}' | gh api -X PUT .../actions/permissions --input -
$ gh api .../actions/permissions -> {"enabled":true,...}
$ gh workflow run image.yml ... -> still HTTP 422 "Actions has been disabled for this repository."

# enable each workflow
$ gh api -X PUT repos/.../actions/workflows/298855786/enable   # exit 0
$ gh api -X PUT repos/.../actions/workflows/340590840/enable   # exit 0
$ gh api -X PUT repos/.../actions/workflows/281994318/enable   # exit 0
$ gh workflow run image.yml ... -> still HTTP 422
```

---

## 8. Root cause

The repository's **public Actions page** contains the answer (fetched with the API
token; the banner is part of the public HTML):

```html
<div class="flash flash-warn ...">
  <svg ... class="octicon octicon-alert">…</svg>
  Workflows aren’t being run on this fork because of its GitHub Actions usage.
  A repository maintainer can re-enable them.
</div>
```

So: **GitHub disabled Actions on this fork for usage reasons.** This is a
repository-level fork-usage policy, not:
* a YAML parse error (files parse, workflows list active),
* a paths filter (empty probe commit produced no run),
* a concurrency deadlock (no `concurrency:` anywhere),
* a repo ruleset / branch protection (none exist),
* an account-wide or billing limit (sibling repos ran at 03:51Z the same day),
* the repo being archived/disabled (`archived:false`, `disabled:false`).

The precise threshold that triggered GitHub's usage policy is **not exposed by any
API**, so it is not determinable from here; the repo's usage is nonetheless very high
for a fork (495 workflow runs, 881 active caches totalling ~3.6 GB). The exact trigger
should be taken up with GitHub Support if the UI re-enable is not available.

The `d1319216` cancellation is consistent with this: it is the run that was in flight
when the disable took effect (`created 01:53:18Z`, `cancelled 01:55:55Z`), after which
GitHub stopped creating check suites for the repository entirely.

### Why it cannot be fixed from this API session

The fork-usage disable is a separate flag from the `actions/permissions.enabled`
toggle. `actions/permissions` still reports `true`; the enable/dispatch paths that
write that toggle (and `PUT /workflows/{id}/enable`) return `204`, but do not clear
the fork-usage flag. There is no documented REST/GraphQL endpoint for the fork-usage
re-enable.

### Fix (requires the repository maintainer, in the GitHub UI)

1. Open <https://github.com/Willywang8216/social-auto-upload/actions> while logged in
   as `Willywang8216` (the maintainer).
2. Use the re-enable control the banner references ("A repository maintainer can
   re-enable them") — GitHub shows this control to maintainers only; it is not in the
   logged-out HTML we could fetch.
3. Confirm with:
   ```bash
   gh workflow run ci.yml -R Willywang8216/social-auto-upload --ref main   # or any workflow
   gh api "repos/Willywang8216/social-auto-upload/actions/runs?per_page=1" \
       --jq '.workflow_runs[0] | [.created_at,.name,.status] | @tsv'
   ```
4. If the UI control is absent, open a GitHub Support ticket referencing
   "Actions has been disabled for this repository / because of its GitHub Actions
   usage" and ask them to re-enable Actions on the fork.

Because a workflow file touch does **not** help (a parallel session pushed
`d99e3a3 ci: re-register the CI workflow`, which added a placeholder comment to
`ci.yml`; it produced no run either), the currently-present re-registration marker in
`.github/workflows/ci.yml` can be removed once Actions resumes.

---

## 9. Fallback: local build + deploy

`scripts/deploy-local.sh` (committed on `main`) performs the same job `image.yml`
does, locally and without Actions:

```bash
#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

IMAGE_BASE="ghcr.io/willywang8216/social-auto-upload"
SHA="$(git rev-parse --short HEAD)"
IMAGE_SHA="${IMAGE_BASE}:commit-${SHA}"

docker build -f Dockerfile -t "$IMAGE_SHA" .            # build native (host is aarch64)
docker tag "$IMAGE_SHA" "${IMAGE_BASE}:latest"          # compose pins :latest
docker run --rm --entrypoint python "$IMAGE_SHA" -c \
  "import sys; sys.path.insert(0,'/app'); from myUtils import platform_limits as pl; print(pl.media_max_mb('threads'))"
docker compose up -d --force-recreate --pull never social-auto-upload
curl -fsS http://localhost:5409/healthz
```

**Exact operator commands** (run from `/home/will/social-auto-upload`):

```bash
# Build + deploy + verify, all-in-one:
scripts/deploy-local.sh

# Or step by step:
git fetch origin && git checkout main && git pull --ff-only
docker build -f Dockerfile -t ghcr.io/willywang8216/social-auto-upload:commit-$(git rev-parse --short HEAD) .
docker tag  ghcr.io/willywang8216/social-auto-upload:commit-$(git rev-parse --short HEAD) \
            ghcr.io/willywang8216/social-auto-upload:latest
docker compose up -d --force-recreate --pull never social-auto-upload
curl -fsS -o /dev/null -w '%{http_code}\n' http://localhost:5409/healthz   # expect 200
```

Important operational notes:

* `docker-compose.yml` pins `ghcr.io/willywang8216/social-auto-upload:latest` with
  `pull_policy: always`, so a **plain local `docker build` is not enough** — the image
  must be tagged `:latest` and, when re-deploying, brought up with `--pull never` (as
  the script does). Otherwise `docker compose up` pulls the stale registry image back.
* The host is `aarch64`, `docker buildx ls` exposes only `linux/arm64` (no QEMU
  binfmt), and `docker run --platform linux/amd64` fails with `exec format error`.
  **Multi-arch/amd64 images cannot be built on this host**; the fallback is arm64-only.
  If an amd64 image is required for another host, build it there or use a
  cross-platform builder.
* Optional — publish the local build to GHCR so `docker compose pull` and Watchtower
  can see it (requires the existing `CR_PAT`, referred to by name; never printed):
  ```bash
  echo "$CR_PAT" | docker login ghcr.io -u Willywang8216 --password-stdin
  docker buildx build --platform linux/arm64 \
    -t ghcr.io/willywang8216/social-auto-upload:latest \
    -t ghcr.io/willywang8216/social-auto-upload:commit-$(git rev-parse --short HEAD) \
    --push .
  ```

### Fallback proof

The fallback was exercised for commit `d99e3a3` while this investigation ran, and the
resulting image is the one currently serving production:

```
$ docker images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.CreatedAt}}' | grep social-auto-upload
ghcr.io/willywang8216/social-auto-upload:commit-d99e3a3  8e2fe720bbb8  2026-10-09 17:02:03 +0800
ghcr.io/willywang8216/social-auto-upload:latest          8e2fe720bbb8  2026-10-09 17:02:03 +0800

$ docker ps --filter name=social-auto-upload --format '{{.Names}} {{.Image}} {{.Status}}'
social-auto-upload ghcr.io/willywang8216/social-auto-upload:latest Up 2 minutes (healthy)

$ curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://localhost:5409/healthz
HTTP 200

# the built image demonstrably contains newer code (Threads cap changed 1024 -> 1000
# by commit 0e9f8f8, which never got an Actions-built image):
$ docker run --rm --entrypoint python ghcr.io/willywang8216/social-auto-upload:latest -c \
    "import sys; sys.path.insert(0,'/app'); from myUtils import platform_limits as pl; print(pl.media_max_mb('threads'))"
1000
```

`bash -n scripts/deploy-local.sh` also passes. This confirms the local path works: a
push can be turned into a running, healthy deployment without GitHub Actions.

---

## 10. What is and is not verified

Verified:
* Actions is disabled for this specific repo (dispatch 422 + public fork-usage banner).
* It is not account-wide (sibling repo ran 2 hours later).
* It is not YAML/paths/concurrency/ruleset/branch-protection.
* REST cannot clear it (PUT enable, off/on toggle, workflow enable all no-op).
* The local build+deploy fallback works and is currently serving production.

Not determinable from here:
* The exact usage threshold/date that tripped GitHub's fork policy (no API exposes it);
  the in-flight `d1319216` cancellation at `01:55:55Z` is the best available timestamp.
* Whether re-enabling requires one click or a Support ticket — the re-enable control
  is only rendered for an authenticated maintainer, which this API token is not.

---

## 11. Red herrings

* **`gh workflow list` showed only "Copilot".** This was not a signal about the fork.
  Without an explicit `-R`, `gh` resolved the repository to the **upstream** remote
  `dreammis/social-auto-upload`, which has no CI/image workflows:
  ```
  $ gh repo view --json nameWithOwner --jq .nameWithOwner
  dreammis/social-auto-upload          # <- upstream, not origin
  $ gh workflow list -R Willywang8216/social-auto-upload --all
  Build & Push Base Image  active  281994318
  CI                       active  298855786
  Build & Push App Image   active  340590840
  ```
  All API calls in this report are addressed explicitly to
  `Willywang8216/social-auto-upload`.
* **`actions/permissions` says `enabled: true`.** True but misleading; the fork-usage
  disable is a separate flag that endpoint does not represent.
* **`gh run list` showed only July Copilot runs.** Same upstream-repo-resolution
  cause as above.
* **Touching the workflow file.** A parallel session pushed `d99e3a3` adding a
  re-registration placeholder to `ci.yml`; it produced no run. Re-registering a
  workflow cannot clear a repository-level disable.
