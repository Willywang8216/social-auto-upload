# Thin but working: optimisation pass

Date: 2026-10-10 · all changes committed, deployed and verified

Goal: make the project thin but working properly, with scheduling, publishing and
the MCP API all functioning without problems.

## Headline

| | before this session | after |
| --- | --- | --- |
| **Host disk** | 92% (8.4 G free) | **72% (28 G free)** |
| Working tree | 3.5 GB | **1.6 GB** |
| `.git` | 1.3 GB | **315 MB** |
| `db/` | 763 MB | **261 MB** |
| Tracked source | — | **8.8 MB / 604 files** |
| Tests | 1559 passed | **1577 passed, 1 skipped** |

## Bugs fixed

### 1. The inbox path depended on where the code was imported (HIGH)

`INBOX_DIR` was `BASE_DIR/sau-inbox`. Inside the container that resolves to the
mounted `/app/sau-inbox`, but on the host `BASE_DIR` is
`/home/will/social-auto-upload`, so it resolved to a directory that **does not
exist** and `list_items()` silently returned **zero ready items while 304 were
queued**. Any host-side health or MCP call reported an empty inbox.

`_resolve_inbox_dir()` now prefers, in order: `SAU_INBOX`, `SAU_WATCH_STATE`'s
parent, the known absolute `/home/will/sau-inbox`, `/app/sau-inbox`, and only
then the project-relative path for local dev. Host and container now agree.

### 2. 304 stale "ready" inbox items (MEDIUM)

Every one pointed at a file that no longer exists — processed work never marked
done (44 of the first 60 already had a matching publish job). They sat in the
queue forever and hid genuinely new items. Cleared through the module's own
`approve()` path, which moves them to `processed` rather than deleting, with a
`state.json` backup first. **302 moved**, 2 were already gone. Inbox is now
`ready=0, pending=5`.

The 5 remaining `pending` entries are **correct**: the watcher holds back files
whose names lack the required `__sfw`/`__nsfw` tag, with the reason recorded.

### 3. Deploys leaked ~3 GB images (MEDIUM)

Every `ci-watch.sh` build tagged `commit-<sha>` and nothing removed them — 11
images after one day on a 97 GB disk shared with a dozen other services. It now
keeps the running image plus the newest `SAU_CI_WATCH_KEEP_IMAGES` (default 3) and
removes the rest, touching only tags this script creates. A failed `rmi` is
logged and ignored, so cleanup can never turn a healthy deploy into a failure.

## Space reclaimed, safely

| item | reclaimed | how |
| --- | ---: | --- |
| Docker build cache | 644 MB | `docker builder prune` (pure cache) |
| Stale agent worktree | 438 MB | branch already merged into main; uv.lock diff saved |
| `.git` loose objects | 1 GB | `git gc --prune=now` |
| DB backups 17 → 5 | 502 MB | keep-newest-5, every retained copy verified `quick_check=ok` |
| Stale ci-watch images | 8 tags | only tags this script creates |

**Deliberately NOT done:** `docker system prune -a`. Re-verified:
`docker system df` reports **7.1 GB of volumes at ZERO reclaimable** — they are
all in use by the other services on this host, exactly as the crontab comment
warns. I pruned only the build cache.

**Nothing deleted without proof:** the 12 removed DB backups came after verifying
the 5 retained ones; the six `.env.bak-*` files were **moved** to
`backups/env-pre-rotation/` (mode 600) after confirming **zero keys exist only in
a backup** — the current `.env` is a superset of all six.

## MCP API: all 36 tools verified

Every tool answers correctly — either returning live data or validating its
arguments:

```
whoami  supported_platforms  profiles_list  profiles_create  accounts_list
accounts_get  accounts_groups  accounts_health  accounts_check  inbox_list
inbox_approve  inbox_reject  jobs_list  jobs_get  jobs_calendar  jobs_run
jobs_cancel  jobs_system_health  jobs_target_cancel  jobs_target_reschedule
jobs_target_resubmit  publish_preview  publish_submit  publish_regenerate
publish_templates_*  upload_register
```

Strongest proof, `publish_preview` run end to end: it generated real on-brand copy
through the live LLM, the content guard and the platform rules:

> "Taipei, I'm coming. If you're at Pride this year, come say hi — I'd love to
> meet kindred spirits who don't think the body is something to apologize for.
> Let's talk naturism, art, and freedom. #TaipeiPride #Naturism #BodyPositivity"

## Scheduling and publishing

- **API**: `/healthz` 200 locally and publicly; authenticated `/profiles` 200,
  unauthenticated 401.
- **Offload cron**: `rc=0`, stable across runs, `_library` intentionally local.
- **Six operator campaigns** all `publishing` and on schedule — Pride
  invitations 2026-10-24 (one week before Pride, 2026-10-31), time-sensitive news
  2026-10-12. All 90 targets verified `READY` earlier today.

## What is thin now

The **tracked repository is 8.8 MB across 604 files**. Everything else in the
working tree is gitignored scratch (`db/`, `videoFile/`, `.claude/`), and media
lives on Google Drive through the tiered `sau/inbox` + `sau/published` archive
that restore reads. That is the intended shape: the code is small, the media is
off-box, and the database holds only metadata.

## Left for the operator

1. **208 unused legacy Drive objects (~16.6 GB)** —
   `scripts/purge_legacy_drive.py --apply` (the 5-object proof already ran and is
   reversible via `sau/trash/`).
2. **Archive migration** — `scripts/archive_drive.py`, dry-run only.
3. **DNS account id 1** (broken credential) and **`stage.sexualwill.com`** (525).
4. **X API credits** and **account 108 OAuth**, and a provider choice for
   **Muyuan transcription** (Cloudflare-blocked on every route).
5. **Re-enable GitHub Actions** in the repo's Actions tab — the fork-usage
   disable cannot be cleared through the API; `scripts/ci-watch.sh` covers
   deployment meanwhile.
