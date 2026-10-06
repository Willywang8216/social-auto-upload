# Stonewall media: length/size enforcement audit

Scope: `myUtils/media_prep.py`, `myUtils/platform_limits.py`,
`sau_backend._prepare_campaign_media_artifacts` / `_shrink_for_publish` /
`_assert_within_platform_caps`, and the YouTube/Instagram/TikTok/Threads
publishers in `myUtils/prepared_publishers.py`.

Incident under audit (all times +0800, 2026-10-05):

| Job | Platform | Artifact | Bytes / duration | Result |
|-----|----------|----------|------------------|--------|
| 4513 | Instagram (NW_IG) | `/app/videoFile/SFW Taipei Stonewall.mp4` | 602,285,541 B / 813 s | direct API **failed 3x** `container failed to process`, then Sociamonials fallback (10689273) |
| 4521 | YouTube | same original | 602 MB / 813 s | succeeded in 19 s |
| 4525 | Instagram (SW_IG) | `campaign-2496/SFW Taipei Stonewall_pub.mp4` | 206 MB / 813 s | succeeded on attempt 2 |
| — | Threads/TikTok | `SFW Taipei Stonewall_short.mp4` | 48,850,548 B / 295 s | manually created cut, then run back through prep as `_short_pub.mp4` |

Relevant commits:

* `216ad67` **2026-10-05 14:49:43** "Refuse to publish an oversized original when
  the pre-publish re-encode fails" — added `_assert_within_platform_caps`.
* `c68d0a7` 2026-10-05 14:53:22 — TikTok floor-division chunk fix, Meta real error.
* `efdf050` **2026-10-05 19:54:49** "One source of truth for platform limits,
  enforced at publish time" — created `myUtils/platform_limits.py` and added
  `prepared_publishers._enforce_video_limits` (FB/IG/X size+duration guards).

Job 4513 ran at **13:58**, i.e. *before* both size guards existed.

---

## 1. Does the pipeline enforce per-platform DURATION?

**Media prep does not. The publishers do, partially and best-effort.**

### `myUtils/media_prep.py` — dimensions / fps / size only

`should_shrink()` is the only gate that decides whether a file is prepared, and
its predicate is exactly:

```python
return (
    size_mb_decimal(meta) > float(threshold_mb)
    or width > TARGET_W
    or height > TARGET_H
    or fps > MAX_FPS
)
```

`probe()` returns `duration`, but **nothing reads it**. `shrink()` and
`build_filters()` never emit a duration option (`-t` / `-ss`); the only temporal
option is `-r` to cap frame rate. So the publishing profile normalizes
resolution, fps and size, and preserves the source duration. `resize_to_target_if_landscape()`
is still a no-op stub. Consequence: **prep can never shorten a clip, and never
produces a duration-limited cut per platform.** The single output is
`<stem>_pub.mp4`, full length, sized to the strictest size cap among the
selected platforms.

### `myUtils/platform_limits.py` — the numbers exist and are correct

`VIDEO_MAX_SECONDS` is populated (IG 900, Threads 300, TikTok 3600, YouTube
43200, X 140, Bluesky 600, Facebook 14460, Reddit/LinkedIn 900, Pinterest 300,
Telegram None) and exposed via `video_max_seconds()` / `limits_for_platform()`.
The table is fine; prep simply ignores it.

### `myUtils/prepared_publishers.py` — duration guards, but only for some platforms and only when a local file is present

* `_enforce_video_limits(local_path, platform)` (line 100) checks **both size and
  duration**. Called for Instagram (1658), Facebook (1402), Twitter (2834).
* Threads `_validate_threads_video_artifact` (1753) checks size + duration.
* TikTok `_validate_tiktok_video_artifact` (1994) checks size + min/max duration
  (using the account's `creator_info` max when available).
* Bluesky has its own duration check (4277).
* sociamonials_fallback `_assert_video_duration` (393) checks duration per
  mapped network.
* **YouTube `publish_youtube_sync` (3394) has no size and no duration check at
  all.** Its caps (43,200 s / 262,144 MB) are non-binding, so it is mostly moot,
  but the asymmetry is real.

Critically, every one of these guards is written as:

```python
path = Path(str(local_path or ""))
if not path.is_file():
    return   # <-- silently passes
```

For the URL-fetch platforms (IG, Threads, TikTok) the container is created from
`public_url`, not from the local file. So a remote-only artifact — or an artifact
whose `local_path` is not visible inside the worker (Docker `/app/...` path not
mounted) — **bypasses the guard entirely** and the platform gets whatever bytes
the URL serves. The guard also has no way to probe a URL, so it cannot catch an
over-limit remote object.

---

## 2. Why did the 602 MB full (over IG's 300 MB) reach Instagram?

Because at 13:58 the only place that could have shrunk or blocked it failed and
silently handed the original through:

1. **Prep swallowed the failure.** `_shrink_for_publish` re-encodes the file,
   but its `except Exception` logged a warning and `return source_path`. The
   commit message for `216ad67` records exactly this: the ~15-minute
   CPU-bound re-encode was interrupted, the log said
   `Pre-publish media prep failed for ...: ffmpeg failed`, and the campaign
   recorded the **original** as its prepared artifact. The DB agrees:
   `campaign_artifacts` row 2314 (campaign 2492) is
   `artifact_kind=remote_upload`,
   `local_path=/app/videoFile/SFW Taipei Stonewall.mp4`,
   `remote_path=campaigns/2492/videos/SFW Taipei Stonewall.mp4` — the
   un-suffixed original, not `_pub.mp4`.
2. **No cap assertion on the failure path.** `_assert_within_platform_caps` was
   introduced by `216ad67` at **14:49**, ~51 minutes *after* job 4513 started.
   Before that, the fallback returned the source unconditionally.
3. **No publisher-side guard.** `_enforce_video_limits` and the
   `platform_limits` size table were introduced by `efdf050` at **19:54**, the
   same evening. Pre-`efdf050` the IG publisher had no media size check
   (`git show efdf050^:myUtils/prepared_publishers.py` contains no
   `_enforce_video_limits`).
4. **IG's own limit is duration-generous, which is why it looked size-specific.**
   813 s is under IG's 900 s, so duration was never the problem for IG; the
   binding constraint was 602 MB > 300 MB. Meta accepted the container, fetched
   the R2 object, then flipped it to an opaque `container failed to process`.
   Threads (300 s) rejected the same artifact on duration; TikTok (3600 s)
   failed on the unrelated chunk-count bug. The commit notes this precisely:
   "Instagram allows 900 s, which is exactly why the same artifact succeeded
   there" (duration), while IG still failed on the 602 MB size.

The 206 MB `_pub.mp4` in job 4525 succeeded because it was produced *after* the
encoder finished (or by a later pass) and is under 300 MB at 813 s < 900 s.

---

## 3. Does prep ever produce a duration-limited cut per platform?

**No.** Prep emits one `<stem>_pub.mp4` per source, full duration, sized for the
strictest cap. There is no `-t`/`-ss`, no duration-bucket fan-out, no
per-platform artifact variant, and `_artifact_payloads_for_platform` only
distinguishes watermark/raw (and TikTok raw preference) — not duration. The
295 s / 49 MB `SFW Taipei Stonewall_short.mp4` was made by hand; the offload log
shows it copied at 16:18 and then separately re-prepped as
`campaigns/campaign-2498/SFW Taipei Stonewall_short_pub.mp4`. That manual step is
the missing pipeline feature.

---

## 4. Concrete fix

The clean fix mirrors what prep already does for size, but adds duration and
per-platform artifact selection. Prefer **B**; **A** is the minimal stopgap.

### A. Stopgap: trim the shared artifact to the strictest duration cap

In `myUtils/media_prep.py`:

```python
def resolve_duration_limit_seconds(platforms=None) -> float | None:
    if not platforms:
        return None
    caps = [
        platform_limits.video_max_seconds(str(p).strip().lower())
        for p in platforms
    ]
    caps = [c for c in caps if c]
    return min(caps) if caps else None
```

Then fold it into `should_shrink` (true when `duration > limit`) and into
`shrink()` as an ffmpeg output option, e.g. append `-t <limit>` before the output
path. Finally extend `_assert_within_platform_caps` to raise on
`duration > resolve_duration_limit_seconds(selected_platforms)` too, so the
re-encode-failure path cannot publish an over-duration original.

This is safe and matches the existing "strictest cap wins" size design, but it
also shortens the IG/YouTube copy to Threads' 300 s whenever a campaign mixes
them — undesirable for exactly this Stonewall case, where the full 813 s was
wanted on IG/YouTube. Use only if a shared cut is acceptable.

### B. Correct fix: per-platform duration variants + selection

1. **Probe + record at prep time.** In `_prepare_campaign_media_artifacts`,
   after `_shrink_for_publish`, compute for each video the source duration (from
   `media_prep.probe`) and the selected platforms' caps
   (`platform_limits.video_max_seconds`).
2. **Produce one variant per distinct cap** below the source duration. Add to
   `media_prep` a `trim_to_seconds(src, out_dir, max_seconds) -> Path` that runs
   the existing profile plus `-t max_seconds` and writes e.g.
   `<stem>_pub_<int(sec)>s.mp4`. Do not trim when the platform cap is `None`
   (YouTube-only) or already satisfied. Keep the existing full `<stem>_pub.mp4`
   as the ≥ longest cap / no-cap artifact.
3. **Record `max_duration_seconds` in the artifact metadata** for each variant
   (and keep uploading each variant to remote storage so URL-fetch platforms can
   use it).
4. **Select per platform** in
   `sau_backend._artifact_payloads_for_platform(artifacts, platform)`: instead of
   returning every artifact for non-TikTok, return at most one video per
   `source_file_record_id` — the variant whose `metadata.max_duration_seconds`
   is the largest value ≤ `platform_limits.video_max_seconds(platform)`, falling
   back to the full `_pub.mp4`. This also fixes a latent bug: returning multiple
   video artifacts makes IG raise "accepts one video per post".
5. **Close the remote-only bypass.** `_extract_media` currently drops artifact
   metadata, so the publisher guards cannot validate a URL-only artifact. Carry
   `duration_seconds` and `size_bytes` (probed at prep) through `_extract_media`
   and have `_enforce_video_limits` / the Threads/TikTok validators check those
   values when `local_path` is missing. That way a sheet/API payload with only a
   `public_url` is still rejected locally instead of burning three opaque
   container retries.
6. **Add the missing YouTube guard** for symmetry (duration + size via
   `platform_limits`), even though its caps are non-binding, and fix the stale
   `_shrink_for_publish` docstring ("Instagram 250 MB" — the table says 300 MB).

With B, the Stonewall campaign would have generated a full `_pub.mp4` for
IG/YouTube and a ≤300 s `_pub_300s.mp4` for Threads/TikTok automatically, with
the correct artifact chosen per target.

---

## 5. Summary

* **Duration is not enforced by prep.** `should_shrink`/`shrink` look only at
  size, width, height, fps; `probe()`'s duration is unused. No duration-limited
  cut is ever produced.
* **Duration is enforced by publishers**, but only for IG/FB/X, Threads, TikTok,
  Bluesky and the Sociamonials fallback — and only when a *local* file exists.
  YouTube has no guard. URL-only artifacts bypass every guard.
* **Why 602 MB hit IG:** job 4513 ran before both fixes. `_shrink_for_publish`
  swallowed the interrupted ffmpeg encode and returned the original, with no
  `_assert_within_platform_caps` (added 14:49, `216ad67`) and no publisher size
  guard (added 19:54, `efdf050`). IG's 900 s allowance made duration a non-issue;
  602 MB > 300 MB is why the container failed to process.
* **Concrete fix:** make `media_prep` duration-aware and emit per-platform
  duration variants (`-t`), record `max_duration_seconds`/`size_bytes` in
  artifact metadata, select the largest fitting variant per platform in
  `_artifact_payloads_for_platform`, and validate recorded metadata (not just
  `local_path`) in the publisher guards. Minimal stopgap: trim the single shared
  cut to the strictest duration cap and extend `_assert_within_platform_caps` to
  cover duration.
