# Fix: root-owned `generated/` broke media prep (PermissionError)

Date: 2026-10-08
Author: agent session (perms fix)
Scope: `docker-compose.yml`, `tests/conftest.py`, `tests/test_media_pipeline.py`,
one-off ownership migration; notes only (no commit, no container restart).

## Root cause

The production container ran as **root**:

- `Dockerfile` has **no `USER`** directive.
- `docker-compose.yml` had **no `user:`** key.
- Base image `ghcr.io/willywang8216/sau-base:slim` also sets no user, so
  `docker inspect` reported `Config.User=` (empty) and the live process was
  `uid=0(root) gid=0(root)`.

Every bind-mounted directory the app created therefore came out `root:root`:

```
$ ls -ld generated generated/campaigns uploads videoFile data
drwxr-xr-x   3 root root  4096 Oct  5 18:39 generated
drwxr-xr-x 301 root root 12288 Oct  8 19:37 generated/campaigns
drwxrwxr-x   2 will will  4096 Sep  7 14:48 uploads
drwxr-xr-x  11 root root  4096 Oct  8 19:36 videoFile
drwxr-xr-x   9 root root  4096 Sep 21 01:17 data
```

The host operator is `will` (uid/gid 1000). Media prep
(`myUtils/media_pipeline.build_campaign_workspace` →
`<repo>/generated/campaigns/campaign-<id>`) runs on the host for the test suite
and for host-side tooling, and it could not write into the root-owned tree:

```
PermissionError: [Errno 13] Permission denied:
  '/home/will/social-auto-upload/generated/campaigns/campaign-2579'
ValueError: I/O operation on closed file.   # loguru sink dying on the above
```

It also caused a real publish failure earlier:
`Artifact restore failed ... Permission denied: '/app/generated'`.

## Approach chosen and why

**Chosen: `user:` in `docker-compose.yml`** (default `1000:1000`, overridable):

```yaml
user: "${SAU_UID:-1000}:${SAU_GID:-1000}"
```

Rationale:

- The uid that must match is the **host operator's**, which is a deployment
  property, not an image property. The project already deploys a prebuilt GHCR
  image via Compose + Watchtower and bind-mounts the runtime dirs. The Compose
  layer is exactly where "run as the host user" belongs, and it needs no image
  rebuild.
- A `USER` in the Dockerfile would bake `1000` into the image and break any
  host whose operator is not uid 1000; it would also require a rebuild before
  it took effect.
- **umask alone is not enough.** The container root's primary group is `root`
  (gid 0), not `will`, so even `umask 002` yields `root:root` files the host
  user cannot write. Group-writable output from root still lands in group
  `root`.
- **ACLs would work but are unavailable here:** `setfacl`/`getfacl` are not
  installed on the host (`which setfacl` → nothing), so default ACLs on the
  bind mounts were not an option.

Because a non-root uid has no passwd entry in the image, `HOME` would otherwise
stay at the inaccessible `/root`. Added:

```yaml
environment:
  - HOME=/tmp
  - XDG_CACHE_HOME=/tmp/.cache
```

These point rclone / fontconfig / playwright cache lookups at a world-writable
path. Verified `python -c "import myUtils.media_pipeline, patchright, playwright"`
works as uid 1000, and `gunicorn` tolerates a missing passwd entry (its
`get_username` is only called when `--user` is passed, and it catches
`KeyError` anyway). No code in the repo calls `getpass`/`getpwuid`/`getlogin`.

**Not done:** `USER` in the Dockerfile. Keeping this at the Compose layer means
the host-matching uid is a deploy concern and the image stays host-agnostic.

## Ownership fix for the existing tree

The container is root, so `docker exec` (one-off, no restart) migrated the
existing runtime data. Files owned by the host `will` already (uploads, logs,
db, cookies, cookiesFile) were left alone. `rclone-cache.conf` was included
because Compose keeps it writable so rclone can persist a refreshed OAuth
token; without it a uid-1000 container could not refresh.

```bash
docker exec social-auto-upload \
  chown -R 1000:1000 /app/generated /app/videoFile /app/data /app/rclone-cache.conf
```

Findings before the fix (all root-owned):

| Path | Owner | Notes |
|------|-------|-------|
| `generated/` (+ `generated/campaigns/`, 300 campaign dirs) | `root:root` | 506 non-`will` entries; 1.3 GB |
| `videoFile/` | `root:root` | 430 non-`will` entries; 13 GB |
| `data/` | `root:root` | 253 non-`will` entries; 1.1 MB |
| `rclone-cache.conf` | `root:root` mode 0600 | rclone needs write to persist refreshed token |
| `uploads/` | `will:will` | already correct |
| `logs/`, `db/`, `cookies/`, `cookiesFile/` | `will:will` | already correct |
| `/home/will/sau-inbox` (mounted) | `will:will` | already correct |

Nothing tracked by git was chowned: `git ls-files generated videoFile uploads
data` is empty, and `check-ignore` confirms `videoFile`, `uploads`, `data`,
`cookies`, `cookiesFile` are ignored.

After the fix: `find generated videoFile data ! -user will` returns 0 for each.

## Before/after write test (proof)

Before (host as `will`, uid 1000):

```
$ mkdir -p generated/campaigns/host-write-probe
mkdir: Permission denied            # HOST_WRITE_DENIED
```

Before (container, still root): succeeded and created `root:root`
(`drwxr-xr-x 2 root root ... container-root-probe`).

After the chown:

```
# Host, uid 1000
$ mkdir -p generated/campaigns/host-write-probe
$ ls -ld generated/campaigns/host-write-probe
drwxrwxr-x 2 will will ...          # HOST_WRITE_OK

# Container as uid 1000 (simulates the Compose user: setting)
$ docker exec -u 1000:1000 -e HOME=/tmp social-auto-upload sh -c \
    'mkdir -p /app/generated/campaigns/uid1000-write-probe && echo ok > .../f'
$ ls -ld .../uid1000-write-probe
drwxr-xr-x 2 1000 1000 ...          # CONTAINER_WRITE_OK
```

Both sides can now write the same files. Probe dirs were removed afterwards.

## Test-suite resilience

`GENERATED_MEDIA_ROOT` was already configurable via `SAU_GENERATED_MEDIA_ROOT`
(verified in `myUtils/media_pipeline.py`), so no per-test patching was needed.

`tests/conftest.py` now points it at a throwaway directory **before** any test
imports `media_pipeline` (same pattern as the existing `SAU_JOB_LOG_DIR`
redirect). It is set unconditionally, not `setdefault`, so a stray value in the
developer's shell cannot send tests back at the shared, possibly root-owned
tree.

Added `GeneratedMediaRootIsolationTests` in `tests/test_media_pipeline.py` to
pin the redirect and prove `build_campaign_workspace` writes under the temp
root.

Verification:

```
$ .venv/bin/python -m pytest tests/test_media_pipeline.py \
    tests/test_campaign_media_prep.py tests/test_worker_media_restore.py -q
30 passed
$ ls -d generated/campaigns/campaign-987654321
ls: cannot access '...': No such file or directory   # real tree untouched
```

## Deployment note (important)

The Compose change takes effect only when the service is **recreated**
(`docker compose up -d`). This session deliberately did **not** restart the
running container (live publishing depends on it), so until that recreate:

- the live container is still root and can create **new** `root:root`
  directories under `generated/`;
- the one-off chown fixed all **existing** data, and the test suite is now
  independent of the shared tree regardless.

The next `docker compose up -d` will start the app as `1000:1000`, matching the
host, and root-owned artifacts will stop being created.

## Unrelated failures observed (not caused by this change)

A full-suite run initially showed 10 failures in `tests/test_inbox_both.py` and
`tests/test_publish_center.py`. They reproduce identically with the pre-change
`tests/conftest.py`. The real cause is the deployment's `.env` setting
`SAU_ASYNC_PREP=1` (`.env` line 125): the suite loads `.env`, so sync-contract
tests ("a valid submit returns jobs") took the async branch, which returns
`jobs: []` while the campaign is `preparing`. A concurrent agent session pinned
`SAU_ASYNC_PREP=0` for the whole suite in `tests/conftest.py` (the second,
separate block in that file, alongside this session's
`SAU_GENERATED_MEDIA_ROOT` block). With both pins in place:
`tests/test_inbox_both.py tests/test_publish_center.py` → 66 passed.

The ongoing `myUtils/publish_orchestrator.py` change from that other session is
unrelated to those failures (the new dedupe query returns False on a fresh test
DB).

Separately, the bundled Playwright `chromium` channel fails to launch in this
image because of missing system libs (`libcups2t64`); it fails identically as
root, so it is pre-existing and unrelated to the uid change.
