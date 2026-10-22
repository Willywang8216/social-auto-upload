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
| Pipeline monitor | `*/15` | app `/healthz` down · **disk > 85 %** (warn) / **> 92 %** (critical) · last offload `rc != 0` · publish targets **failed in the last 2 h** (excluding "disabled by the operator") · campaigns in `needs_review` (last 24 h) · targets **overdue > 45 min** · watcher heartbeat stale > 30 min | `myUtils/pipeline_monitor.py` |
| Disk reclaim | daily 04:30 | nothing (silent); bounds the Docker build cache and dangling layers | `scripts/disk-reclaim.sh` |
| Auto-publish | `*/10` | never alerts on success; a failed submit retries 3×, then reports | `myUtils/inbox_autopublish.py` |
| Auto-archive | `*/15` | never alerts on success; on a failed Drive move it reports and leaves the file in place | `myUtils/inbox_archive.py` |
| Worker / digest | in-process | account refresh failures, publish exceptions, the 09:00 daily digest | `myUtils/ops_alerts.py` |

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
| scheduler thread stops draining | pipeline monitor (overdue targets) |
| prep dies (artifact missing, ffmpeg OOM, container restart mid-encode) | pipeline monitor (`needs_review`) |
| a platform starts rejecting posts | pipeline monitor (failed targets in 2 h) |
| offload stops moving media out (disk fills) | pipeline monitor (`rc != 0`) |
| app container down / bad image | pipeline monitor (`/healthz`) |
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

## 6. Known gaps (deliberately not silenced)

* A publish that fails **once** on a retryable platform is retried by the
  worker; only permanent failures surface (the monitor reads `status='failed'`).
* `needs_review` campaigns need a human: the alert tells you the id, and the
  recovery is a `preparing` reset (see
  `reports/inbox-new-uploads-2026-10-20.md` §11.2).
* The monitor cannot see *wrong* content — only stuck or failed work.
