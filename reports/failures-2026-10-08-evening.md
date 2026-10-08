# The Telegram failure notices (2026-10-08 evening) — four distinct bugs

Investigated after the operator reported Bluesky, Reddit and Sociamonials
fallback failures. They are FOUR unrelated problems, not one.

## 1. Path mismatch: host prefix vs container path (biggest class)

    MediaRestoreError: Artifact restore failed for
      /home/will/social-auto-upload/videoFile/_inbox_cache/demo.mp4:
      [Errno 13] Permission denied: '/home/will'

The artifact's stored `local_path` is a **host absolute path**, but the app runs
in a container where that path does not exist:

    docker exec social-auto-upload sh -c "ls /home/will"
    ls: cannot access '/home/will': No such file or directory

    host: /home/will/social-auto-upload/videoFile  ->  container: /app/videoFile

The worker tries to write `/home/will/...` inside the container. Because the
container now runs as uid 1000 (a recent fix), the failure surfaces as EACCES
rather than ENOENT - which is why it reads as a permissions problem when it is
really a path problem. 13 failures today (11 "Could not restore artifact", 2
"Artifact restore failed").

This is the THIRD variant of the same path-prefix class (bare vs `videoFile/`
prefix was fixed earlier). It needs one canonical normaliser.

## 2. Reddit refuses to publish videos at all

    PreparedPublishError: Reddit video artifacts require a public URL;
    refusing to fall back to a self post

`myUtils/prepared_publishers.py:3393` raises before the self-post logic we added
for link-whitelisted subreddits. But the submit body already handles a missing
URL correctly - the `else` branch posts a **self** post - so the early raise
defeats it. A self post with just the message body is a legitimate Reddit post.

## 3. Bluesky: a marginally-long video leaves the platform with no usable variant

    PreparedPublishError: Bluesky video duration 600s exceeds the 600s limit

The source was **600.x seconds** against Bluesky's **600s** cap. The split DID
run, but only for Twitter:

    campaign 2517 parts -> split_for: ["twitter"]   (5 x 120s parts)
    Bluesky was not tagged, so _select_videos_for_platform handed it the FULL
    600.x s artifact, and the guard fired.

So the guard is correct, and the message is misleading (`{duration:.0f}` rounds
600.x to "600", producing "600s exceeds the 600s limit"). The real defect is in
`_planned_for()`: a part whose `split_for` excludes a platform is refused even
when its duration would fit that platform, so a platform can end up with no
fitting artifact although smaller parts exist.

## 4. Sociamonials workspace is 99.57% full

    fallback error: media upload grant failed (HTTP 422): storage_quota_exceeded

Verified live (workspace 26985):

| | |
|---|---:|
| usage | 3,982,902,697 B (3.98 GB) |
| allowance | 4,000,000,000 B (4.00 GB) |
| available | **17 MB** |
| percent_used | **99.57%** |
| enforcement | true |

Note `storage.status` still reports `ok` while 17 MB remains - the status field
does not reflect "about to fail", so nothing warned before publishes started
failing.

122 assets: 32 videos (3.89 GB) + 90 images (85 MB). The biggest items are
**duplicates of the same file**, uploaded repeatedly because the fallback's
idempotency key is per TARGET (`sau-target-<id>`), not per FILE, so a retried
target re-uploads identical media:

    4x  829.7 MB  1T_pub.mp4
    6x  498.2 MB  20260820101216997_pub.mp4
    3x  514.6 MB  20260822085411068_pub.mp4
    2x  537.5 MB  20260822145825986_pub_pub.mp4
    (+ 5 more)

**1,949.5 MB is reclaimable by keeping one copy of each**, taking usage to ~2.03 GB
with ~2 GB headroom. Deletion is live third-party data, so it is dry-run only
until the operator approves.

## Second-order lesson

Three of the four surfaced as "permission denied", "guard fired" or "fallback
failed" - none of which named the real cause. The path bug reported EACCES for a
missing directory; the Bluesky guard named a limit that the split had already
solved for a different platform. Before trusting an error message here, check
what the code was actually doing.
