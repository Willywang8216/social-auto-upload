# Pipeline monitoring & automatic inbox archiving

Everything on this page exists because of one incident: from **2026-10-17 to
2026-10-20** the Drive-inbox watcher crashed on every single cron tick
(`ModuleNotFoundError: No module named 'PIL'`) and the only trace was 781 KB of
traceback in `watch.log`. New uploads sat untouched for four days and nobody
knew. **A log file is not a notification channel.**

All alerts go to Telegram **@Notification_personalprojectbot**
(chat `8633483147`). Credentials live in two files and are never committed:

| file | keys |
| --- | --- |
| `~/social-auto-upload/.env` | `SAU_ALERT_TELEGRAM_BOT_TOKEN`, `SAU_ALERT_TELEGRAM_CHAT_ID` (container: worker / publish / digest) |
| `~/sau-inbox/.env` | `SAU_WATCH_TG_BOT_TOKEN`, `SAU_WATCH_TG_CHAT_ID` (host: watcher + watchdog) |

---

## 1. What alerts, and when

| piece | cron | fires when | module |
| --- | --- | --- | --- |
| Inbox watcher | every minute | a scan **raises** (thumbnails, Drive listing, state write) — the exact 2026-10-17 failure | `sau-inbox/watch.py::report_scan_failure` |
| Watcher watchdog | `*/10` | no **successful** scan in 15 min, or the health file is missing (cron died, run lock stuck, Drive unreachable) | `sau-inbox/inbox_monitor.py` |
| Stuck-item reminder | `*/10` (same job) | a `ready`/`pending` item is older than 24 h — it arrived, was announced once, and nobody approved it | `sau-inbox/inbox_monitor.py` |
| Pipeline monitor | `*/15` | app HTTP health down (`SAU_HEALTH_URL`, default `/healthz`; see §8 for readiness) · **disk > 85 %** (warn) / **> 92 %** (critical) · last offload `rc != 0` · publish targets **failed in the last 2 h** (excluding "disabled by the operator") · campaigns in `needs_review` (last 24 h) · targets **overdue > 45 min** · watcher heartbeat stale > 30 min | `myUtils/pipeline_monitor.py` |
| Disk reclaim | daily 04:30 | nothing (silent); bounds the Docker build cache and dangling layers | `scripts/disk-reclaim.sh` |
| Auto-publish | `*/10` | never alerts on success; a failed submit retries 3×, then reports | `myUtils/inbox_autopublish.py` |
| Auto-archive | `*/15` | never alerts on success; on a failed Drive move it reports and leaves the file in place | `myUtils/inbox_archive.py` |
| Worker / digest | standalone worker service | account refresh failures, publish exceptions, the 09:00 daily digest; web never owns these loops | `myUtils/ops_alerts.py` |

Every alert is deduplicated per signature (watcher 30 min, watchdog 60 min,
pipeline monitor 60 min), so a permanent outage is one message per hour, not
144 a day.

### Manual checks (same code the cron runs)

```bash
cd ~/sau-inbox && /usr/bin/python3 inbox_monitor.py --status        # watcher health
cd ~/social-auto-upload && .venv/bin/python -m myUtils.pipeline_monitor --status
cd ~/social-auto-upload && .venv/bin/python -m myUtils.inbox_archive --dry-run --from-drive --include-scheduled
```

`--status` / `--dry-run` never send anything and never move anything.

### Proving the channel works

```bash
cd ~/sau-inbox && /usr/bin/python3 -c "
import importlib.util, pathlib
spec = importlib.util.spec_from_file_location('w', 'watch.py')
w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)
try: raise RuntimeError('self-test')
except Exception as e: w.report_scan_failure(e)
"
```

or from the container:

```bash
docker exec social-auto-upload python3 -c "
from myUtils import ops_alerts
print(ops_alerts.send_ops_alert(subject='test', body='test'))"
```

---

## 2. Publish unless the operator objects (24-hour countdown)

Operator rule (2026-10-21): *notify a day before; if I did nothing, publish as
it is.*

`myUtils/inbox_autopublish.py` (cron `*/10`) implements exactly that:

1. the watcher stamps every new `ready` item with `autoPublishAt = createdAt +
   SAU_INBOX_AUTOPUBLISH_HOURS` (default **24**) and the "已備好" Telegram message
   states that deadline — that is the "a day before" notice;
2. two hours before the deadline a reminder is sent (once);
3. at the deadline the item is submitted through the same inbox route the UI
   uses (`POST /api/inbox/items/<id>/publish`), so NSFW platform guards,
   per-account language routing and per-account LLM copy all still apply. A
   filename `publishDate` pins the date at 21:00 Taipei; otherwise the allocator
   picks the next free slot;
4. the item leaves `ready`, is marked `processed` (so the watcher never
   re-adds it), and a "✅ 已自動排程發布" message lists the campaign ids.

**Stopping it** — any of these, all of which simply remove the item from
`ready` before the deadline:

* reply in Telegram / reject in the web UI,
* approve it yourself (it publishes immediately and leaves the queue),
* edit the brief and re-submit,
* or flip the global switch: `SAU_INBOX_AUTOPUBLISH=0` in the environment of the
  cron job (`--disable` for a single run).

The submit is claimed in `state.json` (`autoPublishState="publishing"`) under the
watcher's own file lock before the HTTP call, so overlapping ticks cannot
double-publish. Failures are retried, then reported (`autoPublishAttempts`,
default 3).

---

## 3. Automatic archiving (the Drive inbox stays clean)

`docs/gdrive-filename-convention.md` §10 requires every inbox object to end up
in `sau/published/videoFile/` as `<Title>_<SFW|NSFW>_<YYYYMMDD>.<ext>` with a
same-name `.txt` beside it. Until 2026-10-21 that step was **manual**, which is
how `SAU-Inbox/` reached 404 objects — most of them published weeks earlier.

`python -m myUtils.inbox_archive` now does it on a 15-minute cron:

1. reads the watcher queue (`/home/will/sau-inbox/state.json`), **and** with
   `--from-drive` reconciles every object in `SAU-Inbox/` against the database
   (so media whose queue entry was lost or hand-submitted is not stranded),
2. matches the object to a campaign by filename (UUID-prefixed upload names from
   the web UI are normalised first),
3. archives it when the campaign has taken responsibility for it:
   * default — **all** targets terminal and ≥ 1 succeeded (published), or
   * `--include-scheduled` — the campaign exists and has targets (scheduled for
     a future slot). This is what the cron uses: the operator's drop box should
     only show media the pipeline has **not** picked up yet,
4. writes the note (brief + the exact per-platform copy from
   `campaign_posts.draft_json`), then removes the queue entry and marks the
   Drive path `processed`,
5. records the object in `logs/inbox-archive-ledger.json` and, on a later run,
   **rewrites the note once the campaign has actually published** — so the
   archive ends up saying "published" rather than "scheduled" while the inbox
   stays clean from the moment the pipeline takes the file.

Nothing is deleted: a failed move leaves the file in the inbox and alerts.

Run once for the backlog, then let cron handle the rest:

```bash
cd ~/social-auto-upload
.venv/bin/python -m myUtils.inbox_archive --from-drive --include-scheduled --limit 400
```

**2026-10-21 result:** 374 objects archived (282 from the queue + 92 reconciled
from Drive), leaving only the operator's 4 `.docx` transcripts and the inbox
README.

---

## 4. Failure modes this is meant to catch

| silent failure | caught by |
| --- | --- |
| watcher crashes every minute (missing dependency, Drive auth, disk full) | watcher alert (immediate) + watchdog (15 min) |
| cron entry removed / machine rebooted without cron | watchdog (no heartbeat) |
| publisher stops claiming / heartbeat goes stale | `/readyz`, worker Docker probe and pipeline monitor (overdue targets) |
| prep dies (artifact missing, ffmpeg OOM, container restart mid-encode) | pipeline monitor (`needs_review`) |
| a platform starts rejecting posts | pipeline monitor (failed targets in 2 h) |
| offload stops moving media out (disk fills) | pipeline monitor (`rc != 0`) |
| app container down / bad image | pipeline monitor (`SAU_HEALTH_URL`); release probes additionally require schema/worker `/readyz` |
| inbox fills with published media | auto-archive cron |

## 5. Disk: why it filled and what keeps it bounded

On **2026-10-21** the 97 GB disk hit **99 %** (1.9 GB free). Accounting:

| consumer | size | note |
| --- | ---: | --- |
| Docker **BuildKit build cache** | 15.7 GB | `docker image prune` does **not** touch it; every local `ci-watch` build added layers and nothing removed them |
| Docker images | ~25 GB | 3.08 GB per `commit-<sha>` (ci-watch keeps 3 + `latest`), plus other services (browserless 3 GB, exam-scraper 2 GB, teamtime 1.6 GB, …) |
| Docker volumes | 9.4 GB | mysql / engvoc / grok2api / sexualwill |
| `/home` | 19 GB | repo 5.5 GB (generated 2.2 + videoFile 2.2 + db 0.7), obsidiansync 1.9, agent scratch 1.0 |
| `/opt`, `/usr` | 10 GB | other services, OS |

Fixes now in place:

* `scripts/ci-watch.sh` runs `docker builder prune -f --keep-storage 3GB` after
  every successful deploy (`SAU_CI_WATCH_BUILD_CACHE` to tune);
* `scripts/disk-reclaim.sh` runs daily as a safety net for builds made outside
  ci-watch;
* `pipeline_monitor` alerts at 85 % and 92 %, so a full disk can never again be
  discovered by accident.

`SAU_CI_WATCH_KEEP_IMAGES` (default 3) bounds how many `commit-<sha>` images stay
around for rollback.

## 6. Offload: the recurring rc=1 was a Google Drive race, not a real failure

`offload_to_drive.sh` moves media to Drive and only unlinks a source after
`rclone check` agrees the remote holds it. It reported `rc=1` three times
(2026-10-17, 10-21, 10-22) with

```
keeping campaigns/campaign-2563/…-watermarked_video_part1of2_pub.mp4: remote copy is missing or differs
```

and each time the *same* file verified clean on the next run (campaign-2563:
uploaded 21:25:54, "differs" at 21:27:20, `0 differences found` at 21:37).
Cause: Google Drive can serve an object before its MD5 is populated, so a hash
check seconds after the upload sees a difference that does not exist. The purge
correctly refused to delete, then paged the operator for a non-event.

What changed:

* `verify_remote_copy()` retries the hash check three times (5 s apart), then
  accepts a **size match** (`--size-only`) — the check the script header always
  claimed to use. A note is logged when the fallback is what saved it.
* A failed verification is now a **deferral**: the file stays local, the run
  stays green (`rc=0 deferred=N`), and the next run retries it.
* `logs/offload-deferred.txt` counts consecutive deferrals per file. After
  `SAU_OFFLOAD_DEFER_ALERT_AT` (default **3**) consecutive failures the run
  fails and pages — so a real problem (quota, permissions) still reaches you
  while a one-off race does not.
* Offload alerts now go through `myUtils.ops_alerts` (the app's channel,
  @Notification_personalprojectbot) instead of the old mailserver bot token;
  the mailserver token remains as a fallback.
* `pipeline_monitor` treats `rc=0 deferred=N` as healthy unless N ≥ 5.

## 7. Known gaps (deliberately not silenced)

* A publish that fails **once** on a retryable platform is retried by the
  worker; only permanent failures surface (the monitor reads `status='failed'`).
* `needs_review` campaigns need a human: the alert tells you the id, and the
  recovery is a `preparing` reset (see
  `reports/inbox-new-uploads-2026-10-20.md` §11.2).
* The monitor cannot see *wrong* content — only stuck or failed work.

## 8. Release ownership and paused readiness

The every-two-minute `scripts/ci-watch.sh` is the **only production deployer**.
GitHub Actions gates/publishes images; Watchtower stays disabled for both
`social-auto-upload` (web) and `social-auto-upload-worker`. They must have the
same image ID and `SAU_BUILD_REVISION`/OCI revision. Only worker owns publishing
and maintenance; both share `/app/db/database.db` and the existing data mounts.

Read-only operational probes (no synthetic publish is needed):

```bash
docker exec social-auto-upload python scripts/release_probe.py web
docker exec social-auto-upload-worker python scripts/release_probe.py worker
```

`/healthz` returning 200 only means the web process is alive. Normal readiness
requires `/readyz` 200, the expected schema/revision, and a fresh **non-draining**
publisher heartbeat. A rendered SPA homepage is not a release gate.

The host cron monitor still defaults to `/healthz` in
`myUtils/pipeline_monitor.py`. At rollout, set
`SAU_HEALTH_URL=http://127.0.0.1:5409/readyz` in that cron process's environment
to monitor readiness as well. Without that override its HTTP check is only
liveness; the release/Compose probes already require `/readyz`. Do not assume
changing container settings also changes a host cron environment.

During deployment `db/publish.pause` stops new claims; existing targets/preps
must drain before any restart or migration. The normal probes deliberately fail
while paused. The deploy helper uses its separate `--allow-paused` verification
mode to check a fresh draining heartbeat and schema without releasing work,
then requires normal readiness after activation. A `--hold` initial rollout or
failed release retains the pause; cron never silently removes it. Investigate a
persistent pause even if `/healthz` remains green.

Do not recover by copying an old live SQLite file or resetting publish rows:
external posts may already exist. Failed releases retain verified online
snapshots and private configuration/image baselines, and roll back **images
only**, keeping claims paused on uncertainty. Legacy web embedded publishers
ignore the new pause and require a parent-controlled idle/outage transition.
See [the release/backup runbook](ci-watch.md) for hold/resume, safe offline
restore, lock ownership, evidence paths and that initial-rollout limitation.
All existing inbox/offload/archive/backup cron schedules and destinations remain
unchanged.
