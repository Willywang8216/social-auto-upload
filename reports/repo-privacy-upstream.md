# Repo privacy + upstream tracking — investigation and (blocked) action

Date: 2026-10-11 (UTC)
Repo: `Willywang8216/social-auto-upload`
Fork of: `dreammis/social-auto-upload`
Operator request (verbatim): *“make the repo private and upstream still tracking the original one we use another upstream.”*

**Outcome up front:** the repository is **still PUBLIC**. GitHub
**rejects** making a public fork private
(`422 Public forks can't be made private`). The only supported way is a
separate, **permanent** de-forking step (“Leave fork network”), which discards
PR/issue/wiki/stars/watchers metadata. Per the task constraint (“if a step is
irreversible, STOP and report”), that step was **not** performed. Everything
else (upstream remotes, fetch, divergence, breakage checks, docs) is done.
The operator must choose an option in the last section.

---

## 1. Pre-state snapshot

```
$ gh api repos/Willywang8216/social-auto-upload --jq '{full_name,private,visibility,fork,parent:.parent.full_name,source:.source.full_name,default_branch,disabled,archived}'
{"archived":false,"default_branch":"main","disabled":false,"fork":true,"full_name":"Willywang8216/social-auto-upload","parent":"dreammis/social-auto-upload","private":false,"source":"dreammis/social-auto-upload","visibility":"public"}
```

```
$ git remote -v
origin	git@github.com:Willywang8216/social-auto-upload.git (fetch)
origin	git@github.com:Willywang8216/social-auto-upload.git (push)
upstream	https://github.com/dreammis/social-auto-upload.git (fetch)
upstream	https://github.com/dreammis/social-auto-upload.git (push)
```

Repo metadata relevant to de-forking eligibility:

```
$ gh api repos/Willywang8216/social-auto-upload --jq '{size_kb:.size, forks_count, stargazers_count, subscribers_count, open_issues_count, has_issues, has_wiki, has_pages, network_count, created_at, pushed_at}'
{"size_kb":57669, "forks_count":0, "stargazers_count":0, "subscribers_count":0,
 "open_issues_count":0, "has_issues":false, "has_wiki":true, "has_pages":false,
 "network_count":2613, "created_at":"2026-03-25T23:27:28Z",
 "pushed_at":"2026-10-11T09:30:22Z"}
```

* `size_kb = 57669` ≈ **56 MiB (< 1 GB)** → eligible for the UI “Leave fork
  network” option.
* `forks_count = 0` and `repos/.../forks -> length 0` → **no child forks** →
  eligible.
* `visibility = public` → eligible.
* At risk if de-forked: **72 PRs** (all closed/merged, 0 open), **0 issues**
  (issues are disabled on the fork), **0 releases**, 1 remote branch (`main`),
  7 tags, 0 stars/watchers/subscribers.

```
$ gh api "repos/Willywang8216/social-auto-upload/pulls?state=all&per_page=100" --paginate --jq '[.[]] | length'
72
$ gh api "repos/Willywang8216/social-auto-upload/issues?state=all&per_page=100" --paginate --jq '[.[] | select(.pull_request==null)] | length'
0
```

---

## 2. The exact API call, and its output (BLOCKED)

```
$ gh api -X PATCH repos/Willywang8216/social-auto-upload -F private=true -i
HTTP/2.0 422 Unprocessable Entity
{"message":"Validation Failed","errors":[{"resource":"Repository","code":"custom","message":"Public forks can't be made private"}],"documentation_url":"https://docs.github.com/rest/repos/repos#update-a-repository","status":"422"}
```

The change was **rejected**; nothing was modified. Post-call:

```
$ gh api repos/Willywang8216/social-auto-upload --jq '{private,visibility,fork,parent:.parent.full_name,source:.source.full_name}'
{"private":false,"visibility":"public","fork":true,"parent":"dreammis/social-auto-upload","source":"dreammis/social-auto-upload"}
```

### Why (GitHub docs)

<https://docs.github.com/en/pull-requests/reference/forks> — “Visibility of
forks”:

> A fork’s visibility depends on the upstream repository. Public repository
> forks are public, and private repository forks are private. **You cannot
> change the visibility of a fork by itself.**

<https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility>

> **Making a repository private** … GitHub will detach public forks of the
> public repository and put them into a new network. Public forks are not made
> private.
>
> | Action | Effect on forks |
> | --- | --- |
> | A public repository is made private | Its public forks stay public in a separate network. |

Because `dreammis/social-auto-upload` is public, this fork may not be set
private while it is still in that fork network. You must **leave the fork
network first**, then change visibility.

### The supported detach procedure (NOT performed)

<https://docs.github.com/en/pull-requests/how-tos/work-with-forks/detaching-a-fork>

> **Leaving the fork network** — You can only detach forks with the leave network
> option when: the fork is public, the fork is less than 1 GB, the fork does not
> have any child forks attached.
> […] Settings → General → Danger Zone → **Leave fork network**.
>
> **Warning** — The new repository will not retain any of its issues, pull
> requests, wikis, stars, watchers, comments, child forks, or other metadata […]
> **Leaving the fork network is permanent and the new repository cannot be
> reconnected to the fork network.**

Our repo satisfies all three eligibility conditions (public, 56 MiB, 0 child
forks), so the operator can perform this in the GitHub UI. It is **permanent**
and **irreversible** — hence this report stops here instead of clicking it.

There is **no REST API** to leave a fork network. Attempts return 404:

```
$ for ep in repos/Willywang8216/social-auto-upload/detach \
             repos/Willywang8216/social-auto-upload/forks/detach \
             repos/Willywang8216/social-auto-upload/unfork; do
    gh api -X POST "$ep" -i 2>&1 | head -1; done
HTTP/2.0 404 Not Found
HTTP/2.0 404 Not Found
HTTP/2.0 404 Not Found
```

So the de-fork must be done in the **web UI** (or by asking GitHub Support),
not by this agent.

---

## 3. Remote layout (original upstream preserved)

The original upstream was preserved **before** attempting the visibility change.
`upstream` fetch still works; its push URL was neutered so it is explicitly
read-only.

```
$ git remote set-url --push upstream DISABLED

$ git remote -v
origin	git@github.com:Willywang8216/social-auto-upload.git (fetch)
origin	git@github.com:Willywang8216/social-auto-upload.git (push)
upstream	https://github.com/dreammis/social-auto-upload.git (fetch)
upstream	DISABLED (push)

$ GIT_TERMINAL_PROMPT=0 git ls-remote --heads upstream main
0012d2c355f88f683cc38dde2a2db209e14091bc	refs/heads/main
```

| Remote | URL | Role |
| --- | --- | --- |
| `origin` | `git@github.com:Willywang8216/social-auto-upload.git` | **Our own line of work.** Push target. This is the repo the operator wants private. |
| `upstream` | `https://github.com/dreammis/social-auto-upload.git` (fetch only; push = `DISABLED`) | **The original project.** Fetch/compare/integrate only. Read-only by config. |

`branch.main` tracks `origin/main`. Nothing was force-pushed or rewritten.

The git-level `upstream` remote is independent of GitHub’s fork-network
metadata, so it keeps working **even after** a future de-fork. De-forking only
removes the GitHub UI fork relationship; it does not touch `.git/config`.

### Could not satisfy “a different upstream for our own work” literally

There is no second work repository URL in the request, and current `origin`
already *is* “another remote for our own work.” If the operator actually wants a
**new/private** home for the work line (different from this fork), they must
provide the URL — see Option B below.

---

## 4. Divergence from the original upstream

```
$ git fetch upstream
   0d3f93e..0012d2c  main       -> upstream/main

$ git rev-list --left-right --count upstream/main...origin/main
48	636
```

* left = **48** commits on `upstream/main` we do not have (**behind**).
* right = **636** commits on `origin/main` that upstream lacks (**ahead**).
* merge-base: `0d3f93e8ac6ad9089b4a356bd2ede717c6248aca`
* `upstream/main` head: `0012d2c` *Merge pull request #278 from oscarLeoy/codex/kuaishou-upload-modal-recovery*
* `origin/main` head: `7518616` *Scheduler must wake for a campaign awaiting prep, not only for a due post*

Only a fetch/compare was done — no merge, no rebase, no history rewrite.

---

## 5. What I checked for breakage, and the result

### 5.1 GHCR container images — OK

| Package | Anonymous pull | Authenticated pull (host) | Verdict |
| --- | --- | --- | --- |
| `ghcr.io/willywang8216/social-auto-upload` (app) | **403 (private already)** | `docker manifest inspect` OK | Making the repo private cannot make it *more* private; the host is already authenticated. |
| `ghcr.io/willywang8216/sau-base` (Dockerfile base) | **200 (public)** | OK | Package visibility is independent of repo visibility; it stays public, so `docker build` needs no auth. |

Anonymous probe (no credentials):

```
$ token=$(curl -s "https://ghcr.io/token?scope=repository:willywang8216/social-auto-upload:pull&service=ghcr.io" | jq -r .token)
$ curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $token" \
    https://ghcr.io/v2/willywang8216/social-auto-upload/manifests/latest
403
```

Authenticated probe (host config `~/.docker/config.json` contains a `ghcr.io`
`auth` entry):

```
$ docker manifest inspect ghcr.io/willywang8216/social-auto-upload:latest >/dev/null && echo AUTH_PULL_OK
AUTH_PULL_OK
```

Does the **deployed host pull** from GHCR?

* `docker-compose.yml` declares `image: ghcr.io/willywang8216/social-auto-upload:latest`
  and `pull_policy: always` — a bare `docker compose up -d` *would* pull.
* But every deploy path overrides that:
  * `scripts/ci-watch.sh` → `docker compose up -d --force-recreate --pull never …`
  * `scripts/deploy-local.sh` → `docker compose up -d --force-recreate --pull never social-auto-upload`
* The running container uses the locally-built image; `ci-watch` builds locally
  (`docker build -f Dockerfile`), so it does not rely on GHCR being public.
* Even if it *did* pull, the host is authenticated and the package is already
  private, so it would still work. **Deploys are unaffected either way.**

### 5.2 Workflows / scripts / docs that assume “public” — OK

* `.github/workflows/image.yml`, `base-image.yml`, `ci.yml` — push to GHCR with
  `GITHUB_TOKEN`; private-repo pushes work the same. No public-only assumption.
* `Dockerfile` base `ghcr.io/willywang8216/sau-base:slim` is a **public** package.
* `docs/install.md` clone URL points at **dreammis** (the upstream), not this
  fork.
* No `raw.githubusercontent.com/.../social-auto-upload` or Pages dependency
  (`has_pages=false`) that would break when private.
* GitHub Pages would be auto-unpublished on going private — none configured.

### 5.3 Webhooks / integrations / access — OK

```
$ gh api repos/Willywang8216/social-auto-upload/hooks   --jq '.[] | {id,active,events}'   # (empty)
$ gh api repos/Willywang8216/social-auto-upload/keys    --jq '.[] | {title,read_only}'    # (empty)
$ gh api repos/Willywang8216/social-auto-upload/collaborators --jq '.[] | {login,permissions}'
{"login":"Willywang8216","permissions":{"admin":true,"maintain":true,"pull":true,"push":true,"triage":true}}
```

No webhooks, no deploy keys, no outside collaborators → nothing to break.

### 5.4 GitHub Actions — **the task’s premise is now outdated**

The task states Actions is disabled on this fork. **It is not disabled right
now:**

```
$ gh api repos/Willywang8216/social-auto-upload/actions/permissions
{"enabled":true,"allowed_actions":"all","sha_pinning_required":false}

$ gh api "repos/Willywang8216/social-auto-upload/actions/runs?per_page=3" --jq '.total_count, (.workflow_runs[] | {name,conclusion,created_at,head_sha:(.head_sha[0:7])})'
497
{"name":"Build & Push App Image","conclusion":"success","created_at":"2026-10-11T09:30:24Z","head_sha":"7518616"}
{"name":"CI","conclusion":"success","created_at":"2026-10-11T09:30:24Z","head_sha":"7518616"}
{"name":"cancelled … "}
```

Runs for the current `HEAD` (`7518616`) succeeded at 09:30:24Z on 2026-10-11.
`scripts/ci-watch.sh` also ran at 09:31 and decided “already deployed and
running; nothing to do.” So **both** Actions and the local watcher are live.
The watcher was created while Actions was disabled (see
`reports/ci-actions-reenable.md`) and is harmless if left installed, but it is
redundant while Actions runs.

**Billing note for a private repo:** Actions minutes for **public** repos are
free/unlimited; for **private** repos they consume the account’s included
minutes (and are then billed). Given this fork previously exhausted its
Actions allowance, making it private means future runs are metered. The local
`ci-watch.sh` fallback is therefore worth keeping.

---

## 6. Post-change verification (nothing broke)

```
$ curl -s -o /dev/null -w 'healthz: %{http_code}\n' http://localhost:5409/healthz
healthz: 200

$ docker inspect -f '{{.Config.Image}} {{.State.Status}}' social-auto-upload
ghcr.io/willywang8216/social-auto-upload:latest running

$ GIT_SSH_COMMAND=... git ls-remote --heads origin main
75186160eef1037ddef754113ba0ccfd40ec406b	refs/heads/main
```

`git status` shows **no modification made by this task** to tracked source. There
is one tracked file modified and several new files created by a **different,
concurrent agent session** working on Sociamonials:

```
 M myUtils/sociamonials_fallback.py
?? migrations/versions/0022_sociamonials_delivered_assets.py
?? myUtils/sociamonials_reclaim.py
?? scripts/sociamonials_reclaim.py
```

Those were **not** staged, committed, or touched. Only this report and the two
docs below were committed.

---

## 7. Docs updated

* `reports/repo-privacy-upstream.md` (this file).
* `CLAUDE.md` — added a “Repository visibility & remotes” section (status,
  remote layout, de-fork blocker, Actions/ci-watch reality).
* `docs/ci-watch.md` — corrected the “Actions is disabled” framing to “it was
  disabled; it is currently enabled and running, and the watcher is the
  fallback.”

`AGENTS.md` and `HANDOFF.md` do **not** exist in this repo, so they could not be
updated.

---

## 8. Operator must decide

The requested “make it private” **cannot be done and left reversible**. Pick one:

### Option A — De-fork, then make private (same repo, URL unchanged) — *matches the request*
1. GitHub → repo → **Settings → General → Danger Zone → Leave fork network**,
   confirm the warning, type the repo name. (Eligible: public, 56 MiB, 0 child
   forks.)
2. Then **Settings → Danger Zone → Change repository visibility → Private**
   (or `gh api -X PATCH repos/Willywang8216/social-auto-upload -F private=true`,
   which will succeed once standalone).
* Cost: **permanent**, cannot rejoin the fork network. Documented metadata loss:
  **72 PRs**, wiki, stars/watchers/comments/child forks (currently 0 except the
  PRs). Code, branches, tags, and commits are preserved; the `upstream` remote
  still works.
* After step 1, Actions on the now-standalone private repo consume billed
  minutes.

### Option B — Keep the fork public, create a separate private work repo (non-destructive)
1. Create a new private repo (e.g. `Willywang8216/social-auto-upload-private`).
2. `git remote add private git@github.com:Willywang8216/social-auto-upload-private.git`
   and push `main` + tags; make it `origin`, keep the old fork as `fork-public`
   or archive it.
* Keeps all 72 PRs and the fork relationship intact, and arguably matches
  “we use another upstream for our own work” best — but “the repo private”
  becomes “a private copy,” not `Willywang8216/social-auto-upload`.

### Option C — Ask GitHub Support to de-fork / privatize
Request that Support detach the fork (possibly preserving PR metadata, which the
self-serve UI does not). Slow, but the least destructive route to Option A.

### Option D — Manual delete + recreate under the same name
Bare clone → delete fork → create a private repo with the same name → mirror
push. Same metadata loss as A **plus** destroying the fork object and its
Actions/PR history. Not recommended.

**Recommendation:** if the goal is specifically to make *this* repo private,
**Option A** (or C if the 72 PRs matter). If the real goal is “stop doing our
work in public while still tracking dreammis,” **Option B** is safer and
non-destructive.

No irreversible action will be taken until the operator picks an option.
