# Efficiency audit — why bulk publishing saturates, and what to do

Measured 2026-10-08. Every number below is from the running system, not inferred.

## Summary

The app is **not** inefficient at publishing. It is inefficient at **media
preparation**, and the 504s are a **timeout/config** bug, not a capacity wall.
Recommended fixes are configuration and ~3 small code changes. **A rewrite in
Go/Rust/Postgres would not fix the bottleneck and would cost a lot.**

## What the two reported blockers actually are

### Blocker 1 "saturated, ~1 campaign/min" — this is a concurrency bug, not a limit

Measured on the host while the batch ran:

```
container CPU : 271%
host cores    : 4
ffmpeg procs  : 8, each at 35-54% CPU
```

Eight concurrent ffmpeg encodes on four cores. Each gets ~45% of a core, so
every encode runs **~2x slower than it should**. The work is correct; it is just
queueing against itself.

Why eight: `gunicorn wsgi:app --workers 1 --threads 8`, and **nothing throttles
media prep**:

```
grep -rn "Semaphore|Lock|max_workers|ThreadPool" myUtils/media_prep.py sau_backend.py
-> (empty)
```

So up to 8 submit requests each start an ffmpeg at once.

The 504 has a second, independent cause: gunicorn is configured with
`--timeout 120`, while a 602 MB transcode takes **~900 s**. Any large clip
submitted synchronously is **guaranteed** to 504 after 120 s even when the work
later completes. That is exactly "13 show submitted-504 = response lost but
campaigns exist".

Also `_prepare_campaign_media_artifacts` runs **inline** in the request (0
`to_thread`/`async` usage), so the request occupies a gunicorn thread for the
whole transcode.

### Blocker 2 "NW TG 中文 has 656 queued, needs a content decision" — it is a duplication bug

The account has 662 pending targets out to **2027-06-03**, but they are **not 662
pieces of content**:

```
distinct filenames : 356
total targets      : 662
```

The same files are queued **4-5 times each**:

| file | times queued |
|---|---|
| 20260722155038425_pub.mp4 | 5 |
| action_20260807065004238_sys_part1..3_pub.mp4 | 4 each |
| 20260827122134369_part1..3_pub.mp4 | 4 each |

And 477 of the campaigns were created on a **single day** (2026-09-11), which is
the signature of a mass re-submission rather than 477 editorial decisions.

Scope across the whole queue:

```
total queued targets     : 1365
distinct (account,file)  : 1051
redundant                :  314  (23%)
```

So roughly a quarter of the backlog is duplicate sends of the same file to the
same account. Fixing that is a **correctness** fix, not a content cull — and it
shrinks the queue without losing anything.

## Where the time actually goes

```
host CPU during batch : 271% of 400%
8 ffmpeg at ~45% each : ~360% -> the box is doing ffmpeg, not Python
individual publishes : 1-4 min (job 4560: 1 s; 3847: 18 s; 3746: 3 m 58 s)
```

Publishing itself is **fast**. The cost is transcode + watermark, and it is
CPU-bound in ffmpeg — a C program. Python is not the constraint.

## What this means for a stack rewrite

| candidate | would it help? |
|---|---|
| **Rust/Go for the app** | **No.** The CPU is inside ffmpeg, which is already native. Rewriting 83k lines of Python + 91 test files to call the same ffmpeg binary moves no work. |
| **Postgres instead of SQLite** | **Marginal.** The DB is 43 MB, WAL is off (`journal_mode=delete`) and there are a few concurrent writers, so WAL is worth enabling. But the queue is not lock-bound — it is ffmpeg-bound. |
| **Bigger box / more cores** | **Yes, directly.** Encoding scales with cores. This is the cheapest real throughput win. |
| **Throttling + queueing** | **Yes, biggest correctness win.** 8 encodes on 4 cores is *slower* in wall-clock than 4 at a time, and it is what causes the request pile-up. |
| **Hardware encode (NVENC/QSV)** | **Yes, large.** 5-10x on supported hardware. |

**Recommendation: do not rewrite.** The architecture is sound; three bugs and a
config are the problem.

## Recommended fixes, in order

### 1. Throttle media prep to the core count (small, high impact)

Add a process-wide semaphore around the transcode so concurrent ffmpeg ≈ cores,
not gunicorn threads:

```python
_PREP_SLOTS = threading.Semaphore(max(1, (os.cpu_count() or 2) // 2))
```

Wrap `_shrink_for_publish` / `split_to_seconds` / watermark calls. Wall-clock
throughput goes **up** because the encodes stop fighting each other.

### 2. Make the transcode asynchronous (medium, removes the 504 class)

Either raise gunicorn `--timeout` well above the worst transcode, or (better)
return immediately with the campaign in `preparing` and let the worker do the
prep. The DB already has that state, so this is a wiring change, not new design.

### 3. De-duplicate the queue (small, correctness)

A target should be unique per `(account, file)`. There is already a
`UNIQUE(job_id, account_ref, file_ref)` constraint, but nothing prevents the same
**media** arriving under many job_ids. Add a guard at submit time: skip a target
whose `(account_id, media_group_id)` already has a pending/queued target. This
alone removes ~314 needless sends and shortens the horizon by months.

### 4. ~~Enable SQLite WAL~~ — REJECTED after checking the backups

WAL was enabled, tested, then **deliberately reverted**. `myUtils/jobs.py`
already documents why, and the check confirms it:

- `scripts/backup.sh`, `scripts/restore.sh` and `scripts/sau-daily-backup.sh`
  all copy `db/database.db` as a **plain file**.
- Under WAL, recently committed data lives in `database.db-wal` until a
  checkpoint, so those scripts would silently back up a **stale** database and
  the loss would only appear on restore.

The busy-timeout is already raised to 15 s, which covers the read/write overlap
that WAL would have helped with. Not worth trading a backup-integrity bug for a
marginal concurrency gain; if WAL is ever wanted, the three scripts must switch
to `sqlite3 .backup` (or copy the `-wal`/`-shm` side files) in the same change.

### 5. Hardware encoding — available on the host, blocked by the container

Verified on the production VPS (129.150.36.167):

| check | result |
|---|---|
| cores | 4 |
| ffmpeg encoders compiled in | 10 (`h264_nvenc`, `h264_vaapi`, `hevc_vaapi`, ...) |
| NVIDIA device | **none** (`/dev/nvidia*` absent) |
| iGPU device on the HOST | **present** (`/dev/dri/card0`, `renderD128`) |
| `/dev/dri` inside the container | **absent** |
| `h264_nvenc` | `Conversion failed!` (no GPU) |
| `h264_vaapi` | `Device creation failed: -22` (device not passed in) |

So the VPS **has an iGPU capable of VAAPI**, and ffmpeg supports it, but the
container is not given the device. The compose file mounts only `videoFile`,
`uploads`, `generated`, `cookies`, `db`, `logs`, `data` — no `/dev/dri`.

Fix: add the device passthrough, then use `h264_vaapi` for the transcode:

```yaml
# docker-compose.yml, social-auto-upload service
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - video      # the render group, so the container user may open it
```

Expected: **3-5x** on encode with the CPU freed for other work. Confirm with the
same `testsrc` command above before trusting it in the pipeline.

Caveat: VAAPI quality at a given CRF differs from x264, so re-check that output
still meets platform limits (size/duration) rather than assuming parity.

## What NOT to do

- Do not cull content to fix the NW TG 中文 backlog. The backlog is ~50%
  duplicate; dedupe first, then re-measure.
- Do not migrate to Postgres for throughput. 43 MB and no lock contention.
- Do not port to Go/Rust. The bottleneck is not the language.
