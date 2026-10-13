# Media storage pipeline

## Offload invariant (fail-closed)

The offload (`offload_to_drive.sh`) must **never** delete a local file that is
referenced by a live publish target (status `pending`, `running`, or `retrying`)
unless a **verified restore mapping** exists. If the mapping cannot be proven,
the file stays local.

A verified restore mapping is one of:

1. A `file_records` row with `storage_key IS NOT NULL` **and**
   `storage_backend_id IS NOT NULL` — the worker can download the bytes back
   from the recorded remote location.
2. A `campaign_artifacts` row with `public_url IS NOT NULL` **or**
   `remote_path IS NOT NULL` — the worker can fetch the bytes from the
   recorded URL or remote path.

### How it works

`pending_excludes()` builds the keep-local exclude list in three passes:

1. **In-flight targets** (`running`/`retrying`): unconditionally excluded. The
   worker is actively reading the file; it cannot vanish mid-send.
2. **Due-soon pending targets** (within 30 minutes): unconditionally excluded.
   The claim-race window between listing and copying must not lose the file.
3. **All live targets without a mapping**: any file referenced by a live target
   (`pending`/`running`/`retrying`) that lacks a verified restore mapping is
   excluded. Without a mapping the worker cannot pull the bytes back from
   Drive, and the publisher would fail with `media file is missing`.

Files with a verified mapping are safe to offload even for far-future scheduled
targets: the worker restores them at claim time
(`worker._ensure_artifact_paths_local`).

### Why this matters

Without this guard, the offload could purge a local file that a future publish
target still needs, with no way to restore it. The publisher then fails with
`PreparedPublishError: media file is missing` and the target burns its retry
budget on an unrecoverable error.

## URL-fetch publishing pipeline

URL-fetch platforms need a public HTTPS media URL. The media storage dispatcher
tries share hosting, then S3-compatible object storage, then the legacy rclone
backend. If no backend can produce a usable URL, publishing should stop with a
clear error instead of using a localhost URL.

Production canary checklist:

- Public artifact URL returns HTTP 200 from outside the container.
- Video artifacts return an expected content type such as video/mp4.
- The URL is not localhost or a private network address.
- TikTok Direct Post public URLs match the configured verified URL prefixes.
