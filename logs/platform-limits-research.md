# Platform-limit source record — 2026-11-06

Enforcement is owned by `myUtils/platform_limits.py`, `myUtils/text_limits.py`, and the final media validators. The tracked summaries are `docs/platform-posting-limits.md` and `docs/platform-api-rate-limits.md`. This follow-up supersedes earlier claims that TikTok's ten-minute inbox-upload rule is a universal Direct Post ceiling. SAU retains a conservative 600-second preparation policy; it does not enable longer videos solely because a creator reports a larger allowance. The 2026-11-05 source record remains in the production Git history.

## Method and confidence

The planning and approved execution used **24 fresh Exa searches**, plus source retrieval through the configured official Exa API. Searches preferred official domains and returned source text, not only generated summaries. Numerical ceilings below are tied to a transport, post type, counting unit, and evidence strength. No search result, source text, or recommendation automatically grants an account entitlement. Fresh creator/channel allowances, upload grants, exact profile permissions, pooled storage and reset headers remain authoritative.

Meta official-domain searches returned no results. The earlier Meta reference retrieval also returned 403. Existing Meta compatibility profiles therefore remain qualified, corroborated policies, not newly verified universal maxima. Unsupported/inactive browser and blog transports stay inactive or explicitly unknown; research does not enable publishing or invent a backup.

### Fresh Exa search request IDs

| Route / query scope | Request ID |
|---|---|
| TikTok direct posting, creator allowance, captions, audit and status | `1b5f43aedce28a87c175c00d672030f5` |
| YouTube insert, quota, description and upload | `c1a1d3671710be52dd398217d1eb9837` |
| Bluesky video service and image schema | `4ca5c3fb551bc4a26053d0f31b3d425c` |
| X Post and native video entitlement | `88c40f858cc5d6302265fe4c971d9a73` |
| Facebook Page videos — official search, no results | `a429ce2320fbf7054d89b5921fa908e5` |
| Instagram publishing — official search, no results | `483b131b7f8c59f1854d79659920e4c7` |
| Threads publishing — official search, no results | `a30db58a80935ac1b8bbe5363cfa1009` |
| Telegram cloud Bot API multipart/URL and rate guidance | `e3ba46424e5899d7f11e66b941d259fd` |
| Sociamonials API, profile grants, validation and storage | `716460eb1aaa921b563b547b42dc8c1c` |
| WordPress REST media and host/PHP constraints | `59768d1c5073a6269351bb34389209f7` |
| Douyin upload, duration and transport chunks | `95cd617eec9be03f7c4bfc0a663e19ed` |
| Bilibili submission/upload | `3932555cf42e4920491be708f820fa8a` |
| Kuaishou upload/fragmentation — surface-specific only | `39ddeff57966d47ea4c8f1c85a87db0e` |
| Xiaohongshu SDK sharing / open-account FAQ | `1d96540a5a2faf7caa40f7dbf7f0560f` |
| WeChat Channels API catalog | `3960c9a755b3e226a9809f8a6ace9d2d` |
| Baijiahao OpenAuth / account publishing allowance | `1be765ecf001b122cdeb324c1a7ee64f` |
| Discord attachments / rate buckets | `dfa14a4d9d668272f1567b67b1b6fbbb` |
| LinkedIn Videos API / feed specification distinction | `0f1e155b23a9bef8a6ebff144cd0d593` |
| Pinterest organic publishing / access-tier rates | `a69c5b1c000c39a127d52102feae6fb2` |
| Medium legacy API | `1104a1c8187968cf30f6fa73fb8a9fa6` |
| Substack video recommendations | `f33ad6fb10c71b7f4e23fd5b9d271787` |
| Patreon attachments / native video surfaces | `157bc78928d6832dab8e9b2c9c5c4a3e` |
| Reddit Data API — retired publishing remains excluded | `9c80463a91d55e22d69f57b7e265a74a` |
| TikTok explicit inbox-upload endpoint/schema | `646d2b30a325148783a94889f93897b3` |

Source-retrieval request `a0f00bb66c0a297b00340399124b68fc` fetched TikTok media transfer/status and the Sociamonials public reference. Follow-up retrieval `c90f31765ae20437f5a916cc5d9dd1e5` fetched TikTok media transfer, Direct Post, inbox upload, and YouTube thumbnails. The two requests are retrievals, not additional searches.

## Active-route numerical evidence

### TikTok — Direct Post is not inbox upload

Sources:

- https://developers.tiktok.com/docs/en/content-posting-api-reference-direct-post
- https://developers.tiktok.com/docs/en/content-posting-api-reference-query-creator-info
- https://developers.tiktok.com/docs/en/content-posting-api-get-started-upload-content
- https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide
- https://developers.tiktok.com/docs/en/content-posting-api-reference-get-video-status

Retrieved quotations:

> “The maximum length is 2200 in UTF-16 runes.”

> “The longest video a developer can send via the initialize Upload Video endpoint is 10 minutes.”

> “Maximum of 4GB”

> “Different users have different maximum video-duration privileges. Developers should use this field to stop video posts that are too long.”

Direct video posting uses `/v2/post/publish/video/init/`, `video.publish`, and `post_info` plus `source_info`. Explicit inbox video upload uses `/v2/post/publish/inbox/video/init/`, `video.upload`, and `source_info`; changing a video `post_mode` on the direct endpoint is not the documented switch. Photo `content/init` has a different schema and mode field.

Creator query is limited to 20/min/token, video init to 6/min/token, and status fetch to 30/min/token. Returned reset/wait instructions govern retries. “Typical” daily creator posting allowances are variable, not a guaranteed application quota. Transport chunks are not separate publications.

The status source states that `SEND_TO_USER_INBOX` asks the creator to finish editing/posting, and that `PUBLISH_COMPLETE` in inbox mode means the user subsequently completed that flow. It defines `publicaly_available_post_id` as a **list** returned only after a public post passes moderation. A tracking receipt is not a public post ID. Empty public IDs on a requested public completion mean moderation/pending reconciliation, not permission for another provider to create a second post.

Live read-only creator checks on 2026-11-06 returned public visibility and a 3,600-second creator allowance for both existing configured accounts. These checks do not override SAU's retained 600-second compatibility policy or establish that an unaudited client can publish publicly. The native NW account resolved to `nakedwill2`; Teaching resolved to `weiwei_wang0`. Credentials were not recorded here.

### YouTube

Sources:

- https://developers.google.com/youtube/data/reference/rest/v3/videos/insert
- https://developers.google.com/youtube/data/determine_quota_cost
- https://developers.google.com/youtube/v3/docs/videos
- https://support.google.com/youtube/answer/71673
- https://developers.google.com/youtube/v3/docs/thumbnails/set

Fresh insert text: “Maximum file size: 256GB”. The quota calculator gives separate `search.list` and `videos.insert` default buckets of 100/day and one unit/call. Older generated summaries quoting obsolete insert costs are not authoritative. The retained 5,000-UTF-8-byte description, 100-character title and 500 aggregate tag-character policies follow the video resource fields, not Python character length for descriptions. Long uploads still require exact channel entitlement; default preparation remains 900 seconds.

Fresh `thumbnails.set` text states “Maximum file size: 50MB”. SAU's 2,000,000-byte prepared cover is therefore a retained conservative policy, **not** the current provider API ceiling. Cover intent/order remains unchanged.

### Bluesky

Sources:

- https://docs.bsky.app/docs/tutorials/video
- https://bsky.app/profile/bsky.app/post/3mni6hmaff22u
- https://bsky.app/profile/bsky.app/post/3mtwf7gxkwc2r
- https://unpkg.com/@atproto/api@0.30.0/dist/client/lexicons.js
- https://docs.bsky.app/docs/advanced-guides/rate-limits

Fresh official announcement: “We bumped the video upload limit from 100MB to 300MB”. This concerns `uploadVideo`, not an automatic increase in the resulting PDS blob. The official tutorial requires email verification and account-level upload allowances, and polling until a usable BlobRef. The existing `app.bsky.embed.images` policy retains the published four-image, 2,000,000-byte schema; a newer gallery is a separate protocol, not permission to increase this route's count. Retained text limits are 300 graphemes **and** 3,000 UTF-8 bytes. Video preparation retains 600 seconds/300,000,000 bytes, tightened by the exact account.

### X

Sources:

- https://github.com/xdevplatform/docs/blob/main/x-api/posts/create-post.mdx
- https://github.com/xdevplatform/large-video-upload-python
- https://x-preview.mintlify.app/x-api/fundamentals/rate-limits

The maintained Post source lists “Default (no Premium) | 20 minutes, 8 GB” and “X Premium / verified | 125 minutes, 16 GB”, and at most four photos/one GIF/one video. The historical official upload sample describes 140 seconds/512 MB. SAU deliberately retains the smaller 140-second/512,000,000-byte profile rather than inferring Premium or attachment entitlement from media-upload completion. Provider reset headers and posting-account authorization remain necessary.

### Telegram

Sources:

- https://core.telegram.org/bots/api/methods/sendvideo.md
- https://core.telegram.org/bots/api/
- https://core.telegram.org/bots/faq/

Fresh `sendVideo` text says cloud bots can send video files “up to 50 MB” and captions “0-1024 characters after entities parsing”. The sending-files section distinguishes HTTP URL ingestion, “20 MB max for other types of content”, from multipart's 50 MB. Existing `file_id` and a separately configured local Bot API server/MTProto user are different transports; no silent switch to a larger limit is authorized. FAQ guidance: avoid more than one message/second/chat, groups at most 20/min, bulk about 30/sec unless explicitly using paid broadcasting. Returned `retry_after` is authoritative.

### Sociamonials

Source: https://www.sociamonials.com/developers.php

Retrieved message field:

> “Limits in canonical network order: fb 63206, tw 280, ln 3000, in 2200, pi 500, gmb 1500, thrd 500, tiktok 150, blsk 300.”

Retrieved profile instructions require a fresh listing and literal `agent_can_publish_to=true`; references must not be guessed or substituted by a similar name. `validate_only=true` and `strict=true` use the documented `POST /api/v1/posts` no-create surface. Validation does not replace local final-byte/hash verification or actual per-destination delivery receipts.

Live read-only checks on 2026-11-06 reconfirmed the existing Nakedwill TikTok profile `3917` in workspace `26985`, connected and publish-authorized, and Teaching's isolated `5800` route in `34293`. Both workspace credentials had direct-posting permission with no current approval hold. Both contexts returned `max_upload_bytes.video=1073741824` and `max_upload_bytes.image=15728640`. Storage is pooled across them; the observed available total was 2,955,575,983 bytes. These observations are not cached upload entitlements: recheck before every new upload. No assets were deleted and no plan upgrade was performed.

Retained external-video URL ceiling: 1,024,000,000 bytes. Common video preparation policy: at most 1,000,000,000 bytes, tightened by native, live backup grants and available storage. An approved native TikTok caption may exceed Sociamonials' 150-unit allowance; that is an explicit exception, not permission to shorten it.

### Meta and blog transports

Official Meta searches produced no fresh result; earlier reference fetches were blocked. Retain qualified Facebook Page `/videos` 4,000 MB/14,460 seconds, Instagram Reels 300 MB/3–900 seconds, and Threads 1 GB/300 seconds policies. Do not apply Page Reels' 3–90-second specification to `/videos`, app carousel counts to the implemented API, or secondary publishing quotas as universal account entitlements. Essential official and corroborating references remain in the tracked API guide.

WordPress REST upload checks explicitly use host/site storage and `fileupload_maxk`, while PHP `upload_max_filesize` and `post_max_size` concern single-file and total request budgets. NW/SW Git/URL-embed blogs and Teaching/MSL WordPress therefore have no invented universal video size/duration ceiling.

## Inactive / legacy route evidence

| Route | Fresh retrieved evidence / caveat | Source |
|---|---|---|
| Douyin | Upload text says total within 4 GB, over 300 MB requires transport fragments; duration error says no more than 15 minutes. Fragment sizes are not post splits. | https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/video-management/douyin/create-video/upload-video |
| Bilibili | 4 GB, duration less than five hours, resolution at most 4096×2160, peak bitrate 60 Mbps; over 100 M uses transport chunks. Retrieved non-official-member cap is five submissions/day. | https://openhome.bilibili.com/doc/4/0c532c6a-e6fb-0aff-8021-905ae2409095 ; https://openhome.bilibili.com/doc/4/f7fc57dd-55a1-5cb1-cba4-61fb2994bf0f |
| Kuaishou | Retrieved small-program/highlight surfaces describe 10 MB fragments. They do not establish a universal whole-video/feed publishing ceiling. No general duration/byte ceiling is invented. | https://open.kuaishou.com/docs/operate/specification/integrationGuide/apiGuide.html ; https://open.kuaishou.com/docs/develop/IndustrySolutions/highLightUploadInterface.html |
| Xiaohongshu | JS sharing/client handoff and app-scoped frequency controls, not a verified general server video-note publisher. | https://agora.xiaohongshu.com/doc/js ; https://openaccount.xiaohongshu.com/docs/faq |
| WeChat Channels | API catalog covers capabilities such as live commerce and quota queries; no ordinary-video posting API established. | https://developers.weixin.qq.com/doc/channels/api/ |
| Baijiahao | Unified dynamic daily author allowance; title 8–40 characters, rich text up to 20,000 characters. URL ingestion is not a newly verified universal video byte/duration limit. | https://baijiahao.baidu.com/builder/author/openauth/openauthProtection |
| Discord | Fresh change log increases default upload from 10 MiB to 20 MiB. SAU retains conservative 20,000,000 bytes. Parse bucket/reset and `retry_after`. | https://docs.discord.com/developers/change-log ; https://docs.discord.com/developers/topics/rate-limits |
| LinkedIn | Same Videos API page distinguishes feed specification 3 seconds–30 minutes/75 kB–500 MB from an initialize-upload schema maximum of 5 GB. Do not equate these surfaces or enable the route. | https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/videos-api?view=li-lms-2026-10 |
| Pinterest | Rate categories depend on Trial/Standard and per-app versus per-user scope. No universal organic video byte/duration ceiling was verified. | https://developers.pinterest.com/docs/reference/rate-limits/ ; https://developers.pinterest.com/docs/work-with-organic-content-and-users/create-boards-and-pins/ |
| Medium | Legacy OAuth/integration-token posting API; no new native video publication entitlement. | https://github.com/Medium/medium-api-docs/ |
| Substack | Video guide calls 20 GB a recommended maximum and recommends H.264/AAC; not a general automated API entitlement. | https://support.substack.com/hc/en-us/articles/21093671091220-Guide-to-video-posts-on-Substack |
| Patreon | Retrieved surfaces distinguish 2 GB uploads, 5 GB attachments, and native video without a byte ceiling but at most 12 hours. No general automatic publishing API is established. | https://support.patreon.com/hc/en-us/articles/204606055-What-file-formats-are-supported-for-posts ; https://support.patreon.com/hc/en-us/articles/360017431292-Posting-videos |
| Reddit | Official Data API access/rate/retention policy retrieved. SAU publishing remains retired; no backup or recovery enablement. | https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki |

## Application invariants (not provider maxima)

Prepare exact originals/variants, preserve framing and approved copy, compress against measured byte budgets with headroom, split **all** content into complete ordered parts, then validate final transformed bytes and immutable hashes. Do not crop content away, discard tails, substitute originals, or split again inside an uploader.

Native and managed aliases use one canonical destination for duplicates, leases, scheduling and recovery. The default three posts/day and 30-minute gap are operator policies, counted in operator-local days with UTC queue instants. Requested caps/gaps may only tighten them. Past-due unresolved bookings consume present capacity; a confirmed receipt is counted once, not once per alias.

Publication receipts and uncertain requests are reconciled without blind fallback. Terminal publication outcome and alert event commit together; notification retries are independent and never republish. All production recovery plans, database snapshots, media/customer content and credentials remain private on the server, not in this evidence file or test fixtures.
