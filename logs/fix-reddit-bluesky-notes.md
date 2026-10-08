# Fix notes: Reddit self-post fallback + Bluesky duration guard

Date: 2026-10-08
Files changed:
- `myUtils/prepared_publishers.py`
- `sau_backend.py` (only `_select_videos_for_platform`)
- `tests/test_prepared_publishers.py`

Held files (`myUtils/worker.py`, `myUtils/publish_orchestrator.py`) were not touched.

---

## Bug 1 — Reddit video refused to publish at all (targets 5486, 5357)

### Symptom

```
PreparedPublishError: Reddit video artifacts require a public URL;
refusing to fall back to a self post
```

### Root cause (two layers)

1. **Publisher:** `publish_reddit_sync` raised as soon as `media["videos"]` had
   no `public_url`, *before* the link/self precedence and before
   `_reddit_prefers_self_post` was consulted. The `self` branch already handles
   a missing URL correctly, so the raise defeated the whole self-post path.

2. **Artifact selection (why the URL was missing in the first place):**
   `prepare_campaign_media_artifacts` stores a watermarked video twice — a
   `watermarked_video` row and a `local` row that carries the served
   `…/getFile` URL — both pointing at the same `local_path`.
   `_select_videos_for_platform` grouped them as one "full" video and returned
   `full[0]`, which was the `watermarked_video` row with `public_url: null`.

   Evidence, job 5486 payload for campaign 2517:

   ```
   artifacts = [
     {"id": 2382, "artifact_kind": "watermarked_video",
      "local_path": "…/…-watermarked_video.mp4", "public_url": null, …}
   ]
   ```

   The sibling row the payload never saw:

   ```
   {"id": 2383, "artifact_kind": "local",
    "local_path": "…/…-watermarked_video.mp4",
    "public_url": "https://socialupload.iamwillywang.com/getFile?filename=…"}
   ```

   Note `myUtils/worker.py::_fallback_media_paths` already deliberately skips
   `watermarked_*` rows in favour of the `local` row; the direct publisher path
   was the only place that did not.

### Change

`myUtils/prepared_publishers.py::publish_reddit_sync`
- Removed the video early raise. A video with no `public_url` now falls through
  to the `self` branch instead of aborting.
- The self-post body already appends `public_url` only when present; kept that.
- Kept the existing link behaviour unchanged (`public_url` present and subreddit
  not self-post-preferred → `kind="link"`).
- Removed the analogous over-strict raise in the native-image path. An image
  with no local file is now forced to a **self** post (never a link post to our
  own storage) with the URL in the body.

`sau_backend.py::_select_videos_for_platform`
- The "full video" fallback now prefers the artifact that carries a
  `public_url`, so a link-capable platform can post a link. Ties keep the
  original order.

---

## Bug 2 — Bluesky duration guard: bad message + split investigation (targets 5483, 5484)

### Symptom

```
PreparedPublishError: Bluesky video duration 600s exceeds the 600s limit;
re-encode a shorter cut
```

`{duration:.0f}` rounded, so 600.003 s printed as `600s`, making the message
read "600 exceeds 600".

### Change

`myUtils/prepared_publishers.py::publish_bluesky_sync`
- Message now reports the real value and the overage with one decimal:

  `Bluesky video duration 601.2s exceeds the 600s limit by 1.2s; re-encode or
  split a shorter cut`

- The comparison is intentionally `>` (not `>=`), so a video of **exactly**
  600.0 s is accepted. That matches Bluesky's documented cap: 10 minutes /
  600 s, raised from 3 minutes on 2026-08-26
  (`docs/platform-posting-limits.md`, `logs/platform-limits-research.md`,
  sources [6] and [7]). A clip strictly longer than 10 minutes is the only
  thing rejected.
- The limit was **not** raised.

`sau_backend.py::_select_videos_for_platform` (the real fix)
- The "which split parts may this platform use" gate was too strict. A part was
  usable only when the platform's own name was in its `split_for` tag. For
  campaign 2517 the only persisted plan was `split_for: ["twitter"]` (5 x
  ~120 s parts); Bluesky was not tagged, so `_planned_for` refused those parts
  and the function returned the **full** artifact. That full artifact's metadata
  carries no `max_duration_seconds`, so `_fits()` could not reject it, and the
  publisher's 600 s guard was left to fire.
- Added an untagged fallback: when no platform-tagged part fits, and the split
  the parts came from implies a source that is actually over this platform's
  cap, use the fitting untagged parts. The source duration/size is recovered
  from the metadata the split already stores (`part value x part_count`).
- Gated on the inferred source on purpose: a large-cap platform (YouTube,
  cap 43200 s) still keeps the full video instead of grabbing X's 120 s parts,
  which is exactly what the old `_planned_for` guard protected against. New
  tests pin both directions (Bluesky gets the parts, YouTube keeps the full).

### Why did the 600.003 s clip reach the publisher unsplit?

**The split code already handles Bluesky.** Re-running the exact plan loop from
`prepare_campaign_media_artifacts` on the real file:

```
duration 600.003  size 129.05 MB
plans {(600.0, 300.0): {'bluesky'}, (140.0, 512.0): {'twitter'}}
  plan sec=140.0 mb=512.0 for=['twitter']
  plan sec=600.0 mb=300.0 for=['bluesky']
```

So a Bluesky-only split plan *is* produced whenever the probed duration is
greater than 600.0. The split did not "silently fail" — no `media split …
failed` warning exists in any worker log, and the twitter plan in campaign 2517
proves the loop ran.

What campaign 2517 actually persisted:

```
2382  watermarked_video                     (no metadata)
2383  local                                (served URL, no metadata)
2418…2422 local  max_duration_seconds=120.0006 part_count=5 split_for=["twitter"]
```

There is **no** `split_for: ["bluesky"]` row, so Bluesky was never tagged in
that run. `120.0006 × 5 = 600.003` means the run that produced the twitter
parts probed the source as 600.003 s; for the same run not to also tag Bluesky,
the plan loop must have seen `<= 600.0` for it, or Bluesky was not in
`selected_platforms` when the plan was built. Either way the persisted state is
what it is, and the selector then refused the perfectly-fitting twitter parts.

Job 5483's first attempt is the other part of the story:

```
21:00:54 attempt 1 → MediaRestoreError: Generated artifact is missing …
21:04:23 backend restore failed (rclone copyto)
21:04:41 attempt 2 → PreparedPublishError: Bluesky video duration 600s exceeds …
```

The full watermarked file was declared an "unreadable source" and staged /
offloaded by `offload_to_drive.sh` at `2026-10-08T12:42:41Z` (mtime 20:42
local); the failing job was served a restored copy that probes 3 ms over the
cap.

**Conclusion:** there are two defects here. The publisher message was the
visible one (fixed). The substantive one is the over-strict `_planned_for` gate:
a part that fits a platform is not usable unless that exact platform requested
the split, so any platform missing from `split_for` (added after prep, or the
prep probe read the source at/below its cap) is left holding an oversized
`full` artifact that `_fits()` cannot see through — and the publisher guard is
where that surfaces. The untagged fallback above fixes the selector; the guard
itself is correct and its cap was not raised.

### Regression tests

`tests/test_prepared_publishers.py`
- Reddit: video without a URL → self post (message only), not a raise.
- Reddit: video without a URL → self post for both a self-post sub and a normal
  sub.
- Reddit: image without a local file → forced self post with the URL in the body
  (never a link post to our storage).
- Reddit: `_select_videos_for_platform(_artifact_payloads_for_platform)` prefers
  the URL-carrying full artifact, and the selected payload posts a link.
- Bluesky: over-limit duration emits `601.2s`, `600s limit`, `by 1.2s`,
  `re-encode or split`.
- Bluesky: exactly 600.0 s is accepted and publishes.
- Bluesky: untagged 5 x 120 s parts are used when the inferred source (600.003 s)
  is over the 600 s cap, and the grouping yields 5 posts.
- YouTube: the same untagged parts are **not** used (source fits its 43200 s cap);
  it keeps the full video.
- Instagram: a ~300 s source split for Threads is not used (source fits 900 s).

### Test results

```
.venv/bin/python -m pytest tests/ -q
1395 passed, 1 skipped, 105 subtests passed in 158.14s
```

(An earlier full run hit a single order-dependent flake in
`test_sociamonials_fallback.py::test_publish_uploads_when_library_listing_is_not_entitled`;
it passes in isolation and when its file runs alone, and the rerun above is
fully green. Not related to these changes.)
