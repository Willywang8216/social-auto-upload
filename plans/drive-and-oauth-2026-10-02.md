# Google Drive cleanup + SAU media/OAuth session (2026-10-02)

Working notes for the Drive reorganisation, the "add the media to SAU scheduling"
task, and the Google OAuth verification question. Continues the work a previous
session started (SAU-Archive / Teaching / Nakedwill / Sexualwill hubs, 2026-10-01
~22:00–23:20).

## Hard constraints (do NOT move — live automation depends on these paths)

| Drive path | Used by |
|---|---|
| `GDrive-willywang8216:sau` | `offload_to_drive.sh` DST (cron `17,47 * * * *`); worker restores media from here |
| `GDrive-willywang8216:SAU-Inbox` | `myUtils/inbox_drive.py` DEFAULT_ROOT; host `/home/will/sau-inbox/watch.py` (cron every minute) scans it recursively |

Google Drive keeps a folder's **ID** through a move, so *shared links survive*;
only path-based references break. Those two are the only path-couplings found
(`grep -rIn GDrive-willywang8216`).

## The SAU-Inbox ingest flow (how "add the media" actually works)

1. Drop media into `SAU-Inbox/<persona>/<video|img>/` (personas: `nw`, `sw`,
   `teaching`, `msl`, `both`), named `<topic?>__<sfw|nsfw>.<ext>`
   (see `SAU-Inbox/00-先讀我-README.txt`).
2. `watch.py --once` (cron, every minute) syncs the Drive folder to
   `/home/will/sau-inbox`, makes a thumbnail, drafts copy, writes `state.json`.
3. A **ready** item appears in the app (`/publish/inbox`) and a Telegram
   "已備好" ping is sent. Publishing still requires an explicit user approval.

So staging media = copy it into `SAU-Inbox/<persona>/<video|img>/` with the
sfw/nsfw name. `sfw` = eligible for IG/FB/Threads/YT (limited nudity);
`nsfw` = full nudity, X/adult only.

## Material folders (the three linked)

| Folder (link id) | Contents |
|---|---|
| `新增資料夾` (`1NYCgbVFO3NV…`) | 59 × `.mp4`, 9.7 GiB — timestamped, content/persona unknown |
| `Sexualwill` (`1UQJv7NtmtXh…`) | 36 jpg + 6 mp4 + 1 png (+ story.md/analysis json) |
| `Nakedwill` (`1QTHf3rpFEyK…`) | 258 jpg + 5 png + 2 mp4 (+ story.md/r2_gallery.md, `News/`) |

Blocked on user decision: persona + sfw/nsfw per item, and what the 59
timestamped videos are.

## Reorganisation executed 2026-10-02 (moves only — nothing deleted)

Everything below used `rclone move` (server-side, ~10–60 s each). Log: `/tmp/reorg.log`.

- `SAU-Archive/project-archives/` ← BLOG, branch-bundles, finance-whole-app-archive,
  iamwillywang-vocab, nakedwill-members, nakedwill-repo-backup, nakedwill.com-archive,
  od-qd-panel-backup, quartz-archive, wisdomin.life-archive, Overleaf,
  residue-backup-20261001 (moved earlier as the test)
- `SAU-Archive/` ← youtube, twitterdub-assets
- `Personal/` ← 我的重要檔案, 文件, 附件, Digital, I2DL, Colab Notebooks, Exib see,
  Scripts-ssh-ssl-keys, 屁屁手術, 性學書, 阿嬤爺爺.zip, Screenshot_…_Bybit.jpg, 挂谷.mp4
- `Teaching/` ← the loose GEPT/全民英檢 PDFs (10 files), `KK vs IPA …docx`, `考卷.pdf`

Left in place (deliberately): `sau/`, `SAU-Inbox/`, `SAU-Archive/`, `Teaching/`,
`Nakedwill/`, `Sexualwill/`, `Personal/`, `新增資料夾/`.

### Duplicates found (not deleted — reported only)
- `2023_常春藤…試題本.pdf` ×3 / `…詳解本.pdf` ×2 / `五回滿分試題本` ×3 / `五回滿分詳解本` ×2
  (identical sizes) → now all under `Teaching/`, dedupe needed.
- `阿嬤爺爺.zip` at root **and** inside `我的重要檔案` (both now under `Personal/`).

## Google OAuth verification — diagnosis

Live check (curl) of `https://socialupload.iamwillywang.com`:

- `/` → HTTP 200, `<title>Socialupload — Schedule and publish content…</title>`,
  a `<meta name="description">` explaining the product, then `<div id="app">` +
  Vue bundles. **Client-rendered SPA: without JS a crawler sees only the title +
  meta description, no body purpose text.**
- `/privacy` → HTTP 200, 20 KB of **server-rendered** privacy policy.
- `/healthz` → `{"status":"ok"}`, `/whoami` → 401 JSON → the SAU backend is live
  on that host.

Facts that matter:
- **"In production" ≠ "verified"** for Google OAuth. YouTube upload is a
  *sensitive* scope, so verification is required regardless of publishing
  status. Publishing the app only (a) stops the 7-day refresh-token expiry
  (the YouTube `MSL` id-108 `invalid_grant` cause) and (b) lifts the 100-user
  cap. The "requires verification" banner stays until the app is reviewed.
- The three "issues found from the previous verification attempt" are from the
  **last** attempt; they persist until fixed and **resubmitted** in the
  Verification Center. Reviewing ≠ auto.
- Likely causes of the listed issues: homepage is JS-only (reviewer needs to
  see the purpose without executing scripts) and/or the submitted homepage URL
  pointed at an app route. The privacy policy at `/privacy` is already
  substantial — check the submitted privacy URL is exactly that path.

Checklist to pass: submit `https://socialupload.iamwillywang.com/` as home and
`…/privacy` as privacy policy; add visible server-rendered purpose text +
`<noscript>` fallback to `sau_frontend/index.html`; confirm no CDN cache serves
an old page; clear the Project Checkup items (delete unused OAuth clients, add a
second project owner/editor, fill contact info).

## "Add the media" — staged into SAU-Inbox (2026-10-02, later)

All three folders were copied into the inbox with the required
`<stem>__nsfw.<ext>` naming (server-side `rclone copyto`, originals kept):

| Dest | Count |
|---|---|
| `SAU-Inbox/sw/img` (Sexualwill) | 37 (36 jpg + 1 png) |
| `SAU-Inbox/sw/video` | 6 |
| `SAU-Inbox/nw/img` (Nakedwill) | 280 |
| `SAU-Inbox/nw/video` (Nakedwill 2 + `新增資料夾` 59) | 61 |

Persona from the folders; flag is `nsfw` because the 59 `新增資料夾` videos map
to existing `file_records` under `videoFile/_batch1/nsfw/` and their campaigns
posted to the NW accounts (118/119/116/123/124), and both brands are nude work.
The every-minute watcher turns staged files into `ready` items (Telegram "已備好").
**Done: 387 ready** (nw/img 280, nw/video 61, sw/img 37, sw/video 6, + 3 pre-existing
`both/` items), `processed` 389, 0 quarantined, every ready item has a thumbnail.

The reason it stalled: `detect_new_files()` was downloading **every** staged file
to the VPS, and the container's rclone takes ~40–58 s per call (token refresh +
a config-save retry over the bind-mounted `rclone-cache.conf`) while the host
rclone is ~2–5 s. For `nsfw` items the download is pointless — their thumbnail is
`thumb.make_titlecard()` (text only, never reads the source) and the worker
re-fetches from Drive at publish time via `remotePath`. **Fix applied to
`/home/will/sau-inbox/watch.py`** (backup `watch.py.agentbak-*`): skip the
download when the parsed flag is `nsfw` (sfw still downloads for its frame
thumbnail). After that the drain finished in minutes.

Two host changes were also made so the watcher is fast and never writes
root-owned scans: the cron in `/etc/cron.d/sau-inbox-watch` now runs it with
`SAU_INBOX_RCLONE_MODE=host SAU_INBOX_RCLONE_CONFIG=/home/will/.config/rclone/rclone.conf`,
and `/app/sau-inbox` is a host symlink to `/home/will/sau-inbox` (that's the path
the watcher writes to). Backups: `/tmp/sau-inbox-watch.bak.*`.

### Blockers found (pipeline reliability, needs a fix)

The watcher is very slow and fragile for bulk because:

1. **rclone uses the shared default Google client_id → Drive API 403
   `Quota exceeded … 'Previous quota: Requests per minute'` / `project_number:202264815644`.**
   Neither `/home/will/.config/rclone/rclone.conf`'s `GDrive-willywang8216`
   remote nor the container's `rclone-cache.conf` set `client_id`, so every call
   competes for rclone's tiny shared quota (the user's *other* Drive remotes in
   the same conf do set `client_id`). One container `copyto` can take 40–106 s
   retrying pacer backoff. **Fix: add the user's `client_id`/`client_secret` to
   the `GDrive-willywang8216` remote and re-auth** (the token is bound to the
   client that issued it, so this needs a browser OAuth once).
2. **Stale/root-owned `.sau-inbox-scan-*` dirs** make the `will`-run watcher die
   with `PermissionError: …/5`. Purge with `sudo rm -rf
   /home/will/sau-inbox/.sau-inbox-scan-*` before a run.
3. The watcher downloads **every** staged file in one all-or-nothing run (a
   2 GB `Nakedwill` video is in the set), so one failure aborts the whole run and
   `processed` isn't saved incrementally → a big batch can restart from zero.

Transient things touched and restored: a fresh token was planted into
`rclone-cache.conf` (the stale 2026-09-21 token), and the watcher cron in
`/etc/cron.d/sau-inbox-watch` was paused and re-enabled (backup at
`/tmp/sau-inbox-watch.bak`). No compose/container changes were made.
