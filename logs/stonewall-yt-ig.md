# "SFW Taipei Stonewall.mp4" — why it is not visible on YouTube and Instagram

Investigated 2026-10-06 (read-only GETs only). Job logs are local time (CST,
+0800); DB/API timestamps are UTC. Times below are UTC unless marked.

## TL;DR

* **YouTube did publish, but as `private`.** Video id **`eG-NzyGR934`** on
  channel `UC0APXUHKspMvS91CJ18qWzQ` (`itsnakedwill`, account 110), uploaded
  `2026-10-05T06:45:34Z`, 13m34s, `uploadStatus=processed`,
  **`privacyStatus=private`**, 0 views. Not age-restricted as far as the API
  exposes it (`contentRating={}`, `madeForKids=false`). It is invisible because
  it is private.
* **Instagram NW (account 72) never published.** All three direct Graph API
  containers are permanently **ERROR** (`2207053 Media upload has failed`), and
  the Sociamonials fallback post `10689273` is **still `delivery_state=publishing`,
  `networks.in.delivered=false`, `platform_post_id=null`, ~18 h late**. The
  worker logged it as "delivered" because at that moment the fallback still
  treated acceptance as delivery (fixed later in `dffa2e2`).
* **Instagram SW (account 75) did publish** and is visible:
  <https://www.instagram.com/reel/DeGq_I_EvWT/> (`2026-10-05T07:30:12Z`). It was
  the only target that used the **re-encoded 206 MB H.264** file.
* **Root cause: the raw 602 MB HEVC original was put on the wire.** The
  pre-publish `ffmpeg` re-encode was interrupted/failed and the old
  best-effort `_shrink_for_publish` returned the original unchanged. YouTube
  ingested it (then defaulted to `private`); Instagram could not process it
  (602 MB > 300 MB cap, HEVC); the fallback re-used the same raw file and also
  never delivered.

## (a) YouTube — target 4521 / account 110

Routing: campaign publish goes through `myUtils/worker.default_executor` →
`_run_prepared_campaign_upload` → `PREPARED_PUBLISHER_REGISTRY["youtube"]` →
`prepared_publishers.publish_youtube_sync` — i.e. the **YouTube Data API
resumable upload, not the browser uploader**. `_run_platform_upload`
(the browser path) has no `youtube` branch, and `uploader/youtube_uploader/main.py`
is not imported anywhere in the runtime (only in an old worktree). The premise
that this succeeded "via the browser uploader" is incorrect.

`publish_youtube_sync` builds `status` as:

```python
status = {"privacyStatus": str(config.get("privacyStatus") or "private"), ...}
```

Account 110's `config_json` has **no `privacyStatus` key** (checked: account 108
and 110 both have `instr(config_json,'privacyStatus') == 0`), so the upload was
created as `private`.

Live read-back (`GET /youtube/v3/videos?id=eG-NzyGR934&part=snippet,status,contentDetails,statistics`):

```
snippet.publishedAt  2026-10-05T06:45:34Z
snippet.title        Stonewall in Taipei: LGBTQ+ Community, Body Freedom, and Self-Identity
contentDetails.duration  PT13M34S (814 s)
contentDetails.contentRating {}          <- no API-visible age restriction
status.uploadStatus   processed
status.privacyStatus  private
status.madeForKids    false
status.embeddable     true
statistics.viewCount  0
```

Channel uploads list confirms the pattern: the three most recent uploads
(2026-09-12, 2026-09-17, 2026-10-05) are all `private`, while the older two
(2026-08-21, 2026-09-09) are `public`. So this is not a one-off; every recent
API upload has defaulted to private.

**Not the cause:** processing, deletion, age restriction. `uploadStatus=processed`
and the video is listable through the API; it is simply private. Age restriction
is not exposed for a private video and `contentRating` is empty, so there is no
evidence of an age gate — but note the 602 MB HEVC source is exactly the kind of
content YouTube may age-gate once public, and the repo's own browser uploader
comment warns that videos uploaded through an **unaudited** API project are
force-locked to private. That caveat means simply changing `privacyStatus` may
not be sufficient; see the fix.

Analytics tables are empty for this video (`video_analytics_videos` and
`video_analytics_snapshots` have no rows for account 110; the last
`analytics_sync_log` YouTube run was 2026-07-03), so nothing else surfaced it.

## (b) Instagram — target 4513 / account 72 (direct + fallback) and 4525 / account 75

### Direct Graph API (account 72, NW_IG / nakedwill8, igUserId 17841459849650966)

`GET /v25.0/{container}?fields=status_code,status` for all three containers from
job 4513:

```
18008134388994989 -> ERROR  "Media upload has failed with error code 2207053"
18008134667994989 -> ERROR  "Media upload has failed with error code 2207053"
18008134832994989 -> ERROR  "Media upload has failed with error code 2207053"
```

They are **not still processing — they are permanently failed.** The account's
recent media list contains no Stonewall post, so nothing from job 4513 ever
appeared. (Error 2207053 is Meta's media upload/processing failure, consistent
with an unsupported/oversized source.)

### Sociamonials fallback post 10689273

`GET https://www.sociamonials.com/api/v1/posts/10689273`:

```
delivered: true                      <- top level (queue acceptance only)
delivery_state: "publishing"
networks.in.delivered: false
networks.in.delivery_status: "pending"
networks.in.profiles[0].delivery_state: "publishing"
networks.in.profiles[0].platform_post_id: null
networks.in.profiles[0].late_minutes: 1072   (~17.9 h)
media.video_url: https://i.campaignshare.app/upload/api_media/26985/52afc4d3-....mp4
assets[0].filename: SFW%20Taipei%20Stonewall.mp4
```

The attached asset's `Content-Length` is **602285541** — the same raw original.
So the fallback also handed Instagram the oversized HEVC file; it has sat
"publishing" with no `platform_post_id` for ~18 h. **It did not deliver.**

Why the worker logged success: job 4513 ran 2026-10-05 05:58–06:05 UTC, before
commit `dffa2e2` (2026-10-05 15:00 +0800 = 07:00 UTC) added delivery
verification. Before that, the fallback marked a target succeeded on create
acceptance alone (`571f683:myUtils/worker.py` logged
"delivered via Sociamonials fallback …" unconditionally). The current code has
the poll, but `_wait_for_delivery` still returns `pending` → the worker still
marks success with a "(delivery pending)" note unless the state is `failed`.

### Account 75 (SW_IG / sexualwill8) actually published

Job 4525 (campaign 2496) used `/app/generated/campaigns/campaign-2496/SFW Taipei
Stonewall_pub.mp4` and succeeded on attempt 2 (attempt 1 was only a 180 s
`IN_PROGRESS` timeout). Its media list shows the post:

```
2026-10-05T07:30:12Z  VIDEO REELS  "Taipei carries its own Stonewall spirit: ..."
permalink https://www.instagram.com/reel/DeGq_I_EvWT/
```

So the re-encoded file is the one that works; the NW profile never got a
re-encoded file.

## (c) Exact media used vs `myUtils/platform_limits.py`

| Target | Platform / account | File shipped | Bytes | MB (decimal) | Duration | Codec / dims | Limits check |
|---|---|---|---|---|---|---|---|
| 4521 | YouTube / 110 | `videoFile/SFW Taipei Stonewall.mp4` (raw) | 602,285,541 | 602.29 | 813.256 s | HEVC 1080x1906 | YT cap 262144 MB / 43200 s → **within limits** |
| 4513 | Instagram / 72 (direct + fallback) | same raw file (R2 + Sociamonials asset) | 602,285,541 | 602.29 | 813.256 s | HEVC 1080x1906 | IG cap **300 MB** → **over by 2.0×**; 900 s duration → OK |
| 4525 | Instagram / 75 | `generated/campaigns/campaign-2496/SFW Taipei Stonewall_pub.mp4` | 205,792,049 | 205.79 | 813.256 s | H.264 1080x1920 | IG cap 300 MB / 900 s → **within limits** |

`platform_limits.py` says Instagram `media_max_mb = 300`, `video_max_seconds = 900`;
YouTube `media_max_mb = 262144`, `video_max_seconds = 43200`. Note the docstring
of `sau_backend._shrink_for_publish` says "Instagram 250 MB" while
`platform_limits` says 300 MB — a minor doc drift worth fixing.

The re-encode was produced by `sau_backend._shrink_for_publish` →
`myUtils.media_prep.shrink()` (writes `<stem>_pub.mp4` into the campaign
workspace). Campaign 2492 recorded only `campaign_artifacts` row 2314 (the raw
`/app/videoFile/...` file); campaign 2496 recorded row 2315 (the `_pub.mp4`) plus
row 2316 (the raw upload). That is the whole difference between the working and
broken Instagram targets.

## Root cause

1. **Pre-publish re-encode silently gave up.** `_shrink_for_publish` was
   best-effort: when the 602 MB / 13.5 min HEVC re-encode was interrupted
   (CPU-bound, ~15 min), it returned the *source* path. Campaign 2492 then
   recorded and uploaded the raw original to R2 and published it to both
   YouTube and Instagram.
2. **Instagram cannot process that source** (602 MB > 300 MB cap; HEVC), so the
   Graph API container ended in `ERROR 2207053`. The Sociamonials fallback
   received the same raw public URL and is likewise stuck.
3. **The YouTube API publisher defaults to private.** Account 110 has no
   `privacyStatus`, and `publish_youtube_sync` falls back to `"private"`. The
   upload "succeeded" and is processed, but is not visible.
4. **Acceptance was mistaken for delivery.** At job time the Sociamonials
   fallback marked the target succeeded on create, so the stuck/undelivered IG
   post was recorded as delivered.

These are independent failures that happened to share one raw asset. The
`ffmpeg` fallback was already hardened on 2026-10-05 (`216ad67`, "Refuse to
publish an oversized original when the pre-publish re-encode fails"), and the
fallback delivery check was added in `dffa2e2`; campaign 2492 predates both,
which is why it slipped through.

## Concrete fix

Immediate (operator actions; not done here — read-only investigation):

1. **YouTube:** make the already-uploaded video public, e.g. Data API
   `videos.update` with `status.privacyStatus="public"` for `eG-NzyGR934`, if the
   channel is allowed to. Watch for the unaudited-project private lock: if the
   API refuses/forced-private, use the browser uploader instead.
2. **Instagram NW (account 72):** cancel Sociamonials post `10689273` and
   re-publish the re-encoded 206 MB file, exactly as was done for account 75
   (the working pattern is <https://www.instagram.com/reel/DeGq_I_EvWT/>).

Code, to stop this recurring:

1. **YouTube privacy.** Set an explicit `privacyStatus` on the YouTube accounts
   (110, 108) — or change the default in `publish_youtube_sync` to `public` — and
   add a **post-upload verification**: after the resumable upload, GET
   `videos.list?part=status` for the returned id and fail/alert if
   `privacyStatus` differs from the requested value. "Succeeded" must mean
   "visible at the requested visibility".
2. **Do not let best-effort prep ship an over-cap original.**
   `_shrink_for_publish`/`_assert_within_platform_caps` (commit `216ad67`) now
   raises; keep the re-encode off the critical retry path (pre-compute it, or
   give it a long timeout) so it is not interrupted on the first publish.
3. **Fallback must use the prepared media.** The worker already passes
   `media_paths` into the fallback; ensure the fallback prefers the local
   re-encoded `_pub.mp4` over the raw `public_url` for Instagram (its current
   `collect_media` uses the payload's `public_url` for the video when present),
   so the fallback cannot re-attach the original that just failed.
4. **Treat `pending` as not-delivered.** `DEFAULT_DELIVERY_TIMEOUT` is 60 s and
   the worker records `pending` as success (with a note). For video hand-offs,
   either extend the timeout or keep the target in a `delivering` state and
   re-check instead of closing it succeeded.
5. **Drift:** align the "Instagram 250 MB" docstring in `_shrink_for_publish`
   with `platform_limits.MEDIA_MAX_MB["instagram"] == 300`.

## Evidence / commands used (all read-only GET/HEAD)

* `sqlite3 db/database.db` — `publish_jobs`, `publish_job_targets`,
  `campaign_posts`, `campaign_artifacts`, `file_records`, `accounts`.
* `GET https://www.googleapis.com/youtube/v3/videos?id=eG-NzyGR934&part=...`
  and `.../playlistItems?playlistId=UU0APXUHKspMvS91CJ18qWzQ` (acct 110 token).
* `GET https://graph.facebook.com/v25.0/{container}?fields=status_code,status`
  (acct 72 token) and `.../{igUserId}/media` for accounts 72 and 75.
* `GET https://www.sociamonials.com/api/v1/posts/10689273` (SOCIAMONIALS_API_KEY).
* `curl -I` against the R2 raw/`_pub` objects and the Sociamonials asset;
  `ffprobe` on the R2 objects. Tokens/keys are never echoed in this report.
