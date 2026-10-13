# Publish failures — root cause + fix spec

Date: 2026-10-13. Coordinator: `verify-publish-e2e`.

## The problem

54 failed `publish_job_targets`. Buckets:

| bucket | count |
|---|---|
| X / Sociamonials fallback | 20 |
| missing-media | 13 |
| other | 15 |
| reddit content / ratelimit | 6 |

The dominant, systemic failure is **missing media at publish time**. The
publishers raise:
- `PreparedPublishError: Telegram media file is missing or empty: /app/generated/...watermarked_video.mp4`
- `PreparedPublishError: YouTube video artifact is missing or empty: ...`
- `sociamonials fallback failed ... fallback error: local media missing: /app/generated/...`
- Bluesky direct: `HTTPError 404 ... /getFile?filename=...` and the fallback then
  also reports `local media missing`.

Of the 54 failed targets, **47 reference an artifact that HAS a restore mapping
but still failed to restore**, and **7 have no mapping at all**.

### Evidence

`campaign-2514` `...watermarked_video_part1of2_pub.mp4`:
```
local exists: False
file_records: storage_key=campaigns/campaign-2514/...mp4, storage_backend_id=691
campaign_artifacts: public_url=None, remote_path=None
backend 691: provider=rclone endpoint=sau/inbox/generated bucket=GDrive-willywang8216
rclone lsf GDrive-willywang8216:sau/inbox/generated/campaigns/campaign-2514/watermarked_video/
  -> ERROR: directory not found      # stale mapping: the object is NOT there
```

`campaign-2568` `...watermarked_video.mp4`:
```
campaign_artifacts row 2490 exists, but public_url/remote_path are NULL
file_records: none
-> _record_for finds neither branch, so the restore has no source
```

`campaign-2550`, `campaign-2543`: no `file_records` and no usable
`campaign_artifacts` mapping.

So three distinct defects compound:
1. **The restore download is not reliable** — a mapped artifact (2514) still
   surfaced as "missing". Either the download fails silently, or the restore is
   skipped/fails before the publisher, and the target burns its 3 fast attempts
   and is marked permanently failed.
2. **Artifacts are not always registered** — a campaign artifact can exist with
   bytes on disk/Drive but no `file_records.storage_key`+`storage_backend_id`
   and no `public_url`/`remote_path`, so there is no restore route (2568/2550/2543).
3. **The offload can delete a needed local file without a verified mapping** —
   a live target's artifact went away and nothing could pull it back.

## Fix workstreams (no file overlap)

| subagent | owns | deliverable |
|---|---|---|
| `pf-restore` | `myUtils/worker.py`, `tests/test_worker_media_restore.py` | make `_ensure_artifact_paths_local` / `_record_for` actually restore a mapped artifact (correct endpoint+key download, size check), use the `campaign_artifacts` fallback, and **fail fast with a clear error before the publisher** when truly unrecoverable — with the target retryable, not permanently failed after 3 instant attempts. |
| `pf-register` | `myUtils/campaigns.py`, `myUtils/campaign_media_prep.py`, new `scripts/repair_missing_artifacts.py`, tests | guarantee every artifact has a restorable mapping (`add_campaign_artifact` writes a `file_records` row; campaign_artifacts records `public_url`/`remote_path`). Add a repair script that finds live targets whose artifact has no mapping and either registers it (bytes exist locally/Drive) or reports it. |
| `pf-offload` | `offload_to_drive.sh`, `docs/media-storage-pipeline.md`, tests | never purge a local file referenced by a live target unless a verified restore mapping exists (fail closed); add a regression test. |
| `pf-verify` | read-only; `reports/pf-verify.md` | after the three land: reproduce the missing-media failure on a fixture, prove the fix, then list the failed targets that are now restorable/retryable. |

Rules: no live publishes, no DB status changes outside your scope, tests must be
offline (mock the download). The coordinator owns the failed-target requeue
after `pf-verify`.

## What "never happened again" means

- A publisher must never be reached with a missing artifact file: the restore
  runs first, verifies size, and only then the send. If it can't restore, the
  target fails with a precise reason (retryable when the bytes exist but the
  download was transient; permanent when the mapping/media is truly gone).
- Registration is unconditional: bytes on disk/Drive ⇒ a `file_records` mapping.
- Offload is fail-closed: no mapping ⇒ the file stays local.
- A regression test covers each of the three.
