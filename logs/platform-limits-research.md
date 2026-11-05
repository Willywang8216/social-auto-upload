# Platform-limit source record — 2026-11-05

Enforcement lives in `myUtils/platform_limits.py`; tracked summaries are `docs/platform-posting-limits.md` and `docs/platform-api-rate-limits.md`. This recheck supersedes the numerical claims in the 2026-10-05 research record (retained in Git history), particularly the app/API equivalences, Bluesky image schema, YouTube text units, and presumed universal daily quotas.

## Method and evidence strength

PI-Desktop performed 25 Exa searches on 2026-11-05 using the configured official `api.exa.ai/search` service, plus Exa content retrieval. The configured Exa MCP endpoint returned Cloudflare 403; it was not retried. Queries preferred official domains. Meta's reference pages returned 403 even through Exa content retrieval, so secondary publishing references corroborate conservative policies but do not establish freshly verified universal Meta maxima. Numerical limits are bound to the named API/transport/post type. Account allowances, returned upload grants, quota/reset headers and plan entitlements remain authoritative. Recommendations and SAU quality choices are not hard provider limits.

### Exa request IDs

| Query | Request ID |
|---|---|
| TikTok transfer/creator/caption | `8d0892278bea4246af7ecd53d3af05ee` |
| Instagram official-domain search (no results) | `e9ed2f7c621d5c1f80642217af07ea9a` |
| Threads official-domain search (no results) | `1353e28966f63283289cf2ba7dbf2e67` |
| Bluesky video/API | `df05dd4e98a07fdb6db54e54d8d0245b` |
| X media/Post entitlement | `8a17b9da1af761668f966e3e11c4c709` |
| YouTube | `c0315a3f2643d30cf294302925f685e2` |
| Facebook Page video | `bdf1d916becd4c750efd161b41388be2` |
| Telegram | `3d62652e97b6c2bb095aeb47803ad83d` |
| Sociamonials | `6c259fbedac099f2d28b74c8750a10fa` |
| Instagram corroboration | `d60866474092148ab1a64a4ec6b1546f` |
| Threads corroboration | `f2563f5a8abe3b84505168512a8cb1e0` |
| Bluesky current gallery/video | `c0ba3569efd8d817a7b63d11f4b6e779` |
| WordPress | `3b4927f73f026fed64569ec0afa7a3e9` |
| Douyin | `bafaf3d185d93b3cd04e1fdea68a3b08` |
| Bilibili | `e0e185c73ed139bea3340efa12e9271e` |
| Kuaishou | `d8d187a6f30de48dbceb852eb60f73f3` |
| Xiaohongshu | `958c8af2c14d75a3f3b71d4e30f63404` |
| WeChat Channels | `5cb63f6053de36474ad6248b1cafa60b` |
| Baijiahao | `7679da7b6d2027aa60b854f74c567e83` |
| Discord | `5f8c8b3a4cabdeb357977a89e5a5c1fd` |
| LinkedIn | `0bbdf0d898e497e7ae39b47aec4bd5b5` |
| Pinterest | `f3c69f184494b76b2f6e1330fdb1292c` |
| Medium | `7285d1e5577f9ca50b67a85d183dba88` |
| Substack | `5f0667bbb8225e51552a2764a79a2095` |
| Patreon | `7d4095789dc5dff1f883fa30cf50d154` |

Exa contents request `d9f51d88772b3da96bc3158a19a7f20a` retrieved Sociamonials and reported the unavailable Meta sources. The full Sociamonials reference was additionally read directly because its embedded schemas exceeded the Exa text window. Bluesky's official published `@atproto/api@0.30.0` lexicons were read directly; the former raw GitHub lexicon URLs returned 404.

## Active network routes

### X — Post video, not DM video

- [Maintained Post documentation](https://github.com/xdevplatform/docs/blob/main/x-api/posts/create-post.mdx): default account “20 minutes, 8 GB”; Premium/verified “125 minutes, 16 GB”. These follow the posting account's entitlement and `media_category`, not its developer API plan. Upload success alone does not establish permission to attach the media to a Post.
- [Media best practices](https://x-preview.mintlify.app/x-api/media/quickstart/best-practices): `tweet_image` 5 MB, `tweet_gif` 15 MB, up to four photos or one GIF/video; `dm_video` retains 140 seconds/512 MB for default accounts. H.264 High, progressive yuv420p, square pixels and AAC-LC mono/stereo are compatible output choices.
- SAU retains 140 seconds/512,000,000 bytes as its conservative existing preparation policy, not a universal current Post-video ceiling. X text counting weights Unicode and t.co URLs (23 units); approved recovery copy is not silently shortened.
- [Rate table](https://x-preview.mintlify.app/x-api/fundamentals/rate-limits): endpoint/user/app buckets differ. Runtime remaining/reset values govern eligibility, not a backlog-wide retry loop.

### Bluesky — images versus gallery

- [Official announcement](https://bsky.app/profile/bsky.app/post/3mtwf7gxkwc2r): “videos up to 10 minutes long” and “maximum file size to 300MB”.
- [Official package lexicons](https://unpkg.com/@atproto/api@0.30.0/dist/client/lexicons.js): `app.bsky.embed.video` accepts `video/mp4`, `maxSize: 300000000`; `app.bsky.embed.images` is `maxLength: 4`, each image `maxSize: 2000000`. Gallery is a **different** schema: future-proof max 20, currently authored with a soft limit of ten. SAU's existing image route remains four; increasing a shared constant cannot implement the gallery protocol.
- `app.bsky.feed.post`: `maxGraphemes: 300` and `maxLength: 3000` (UTF-8 bytes). Combining sequences/emoji must not be sliced as independent code points.
- [Video tutorial](https://docs.bsky.app/docs/tutorials/video) and [rate guide](https://docs.bsky.app/docs/advanced-guides/rate-limits): email verification, video service processing until a BlobRef, current `getUploadLimits`/canUpload and remaining allowances. Historical “25 videos/10 GB per day” is not hard-coded as a current universal allowance.

### Instagram Reels API

- Official sources, unavailable during this recheck: [content publishing](https://developers.facebook.com/docs/instagram-platform/content-publishing/), [IG user media](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media/).
- Corroboration: [AdaptlyPost's quoted API specifications](https://adaptlypost.com/blog/instagram-reels-api-max-length-file-size), [Sprout publishing requirements](https://support.sproutsocial.com/hc/en-us/articles/49500207602445-Video-Requirements-per-Network-with-Sprout-Publishing), [Publora API guide](https://docs.publora.com/platforms/instagram).
- Quoted Reels profile: “15 mins maximum, 3 seconds minimum”, “300MB maximum”; MP4/MOV, H.264/HEVC progressive closed GOP/yuv420p, AAC up to 48 kHz/mono/stereo, 23–60 fps, max 1920 horizontal pixels/25 Mbps. SAU chooses a smaller common encoding profile and validates after watermarking.
- Feed images: conservative JPEG/8 MB and 4:5–1.91:1 framing, using padding instead of destructive cropping. Keep the existing 2–10 API carousel policy; app/other-route announcements do not authorize increasing it.
- Caption 2,200; account-dependent publishing limits must be read from `content_publishing_limit` (`quota_total`), because published sections conflict at 50/100 per rolling 24 hours. Carousel videos, Reels and Stories are different surfaces.

### Threads API

- Official [posts reference](https://developers.facebook.com/docs/threads/posts/) could not be retrieved. [Emplifi requirements](https://docs.emplifi.io/platform/latest/home/threads-publishing.md), [PostZen API guide](https://www.postzen.dev/blog/threads-api), and [PublishQ](https://publishq.com/docs/platforms/threads-api) corroborate 1 GB, >0–300 seconds, H.264/HEVC MP4/MOV, AAC, 23–60 fps, 1920-pixel width/25 Mbps.
- Image compatibility: JPEG/PNG, 8 MB, 320–1440 pixels wide and a bounded 10:1 ratio; choose conservative prepared output. Caption 500; some references give special emoji counting, so uncertainty is documented rather than pretending every field uses Python `len`.
- References describe carousel 2–20; SAU retains its **conservative implemented ten-image group** and one independently scheduled video per video intent. Retrieved publishing budgets 250 posts/1,000 replies per rolling 24 hours; use returned account quotas.

### Facebook Page `/videos`

- [Page video reference](https://developers.facebook.com/docs/graph-api/reference/page/videos/) and [Graph quota guide](https://developers.facebook.com/docs/graph-api/overview/rate-limiting/) were not directly retrieved. Secondary Page-video references disagree, including [PostZen](https://www.postzen.dev/blog/facebook-graph-api-post-to-page), [PostGate](https://postgate.studio/how-to/facebook-graph-api-video-upload) and [Zernio](https://docs.zernio.com/platforms/facebook).
- The existing SAU 4,000,000,000-byte/14,460-second Page-video profile is conservative application policy, **not a newly verified universal maximum**. Reels' retrieved 3–90-second/9:16 API profile does not govern the current Page `/videos` call. A consumer “no restrictions” announcement does not remove an API validator.
- Corroborated Page images JPEG/PNG up to ten MB; SAU deliberately prepares a tighter **4,000,000-byte** compatibility output and retains ten photos per post. Caption 63,206 is retained policy/corroboration. Graph usage/reset headers and per-account capability remain authoritative.

### TikTok Content Posting API

- [Transfer guide](https://developers.tiktok.com/docs/en/content-posting-api-media-transfer-guide): “Maximum of 4GB”; the longest developer-sent upload video is ten minutes. Creator duration privileges differ; [creator info](https://developers.tiktok.com/docs/en/content-posting-api-reference-query-creator-info) returns `max_video_post_duration_sec` and must be queried fresh. Offline default 180 seconds, bounded by the 600-second upload route; do not use the former 3,600-second web-app assumption.
- Video MP4 (recommended), WebM/MOV; H.264 recommended (others supported), 23–60 fps, 360–4096 pixels per side. Images JPEG/WebP, max 20 MB, maximum 1080p, separate schema up to 35 images.
- Transport chunks: 5–64 MB except final up to 128 MB; below 5 MB whole upload, above 64 MB multiple chunks, at most 1,000 chunks, sequential. **Transport chunks are not separate posts.**
- [Direct Post](https://developers.tiktok.com/doc/content-posting-api-reference-direct-post): caption “maximum length is 2200 in UTF-16 runes”. Creator consent, current privacy/interaction settings, branded-content/AI disclosures and app audit restrictions are retained. Do not turn an inbox upload into a public direct post in backup.
- Creator info 20/min/token, video init six/min/token, [status](https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status) 30/min. Typical creator daily quotas are variable and shared; no guaranteed 15/25-post allowance is invented.

### YouTube Data API

- [Insert](https://developers.google.com/youtube/data/reference/rest/v3/videos/insert): “Maximum file size: 256GB”. [Long uploads](https://support.google.com/youtube/answer/71673): 12 hours or 256 GB, whichever smaller; default 15 minutes until long uploads are allowed.
- [Video resource](https://developers.google.com/youtube/v3/docs/videos): title max 100 characters, description max **5000 bytes**, tags max 500 characters including commas and the quotes counted around tags containing spaces. Chinese text can exceed the byte cap long before 5,000 code points. The old “description 5000 characters” claim is superseded.
- [Quota calculator](https://developers.google.com/youtube/data/determine_quota_cost): current body specifies 100 `videos.insert` calls/day in a separate bucket, one unit/call, and 10,000 combined units for most other methods; midnight Pacific reset. Older generated summaries still report obsolete costs. Actual project/channel allowances take precedence.
- Shorts are square/vertical and at most 180 seconds; do not silently change a regular video into a Short.

### Telegram

- [Bot API](https://core.telegram.org/bots/api/) and [sendVideo](https://core.telegram.org/bots/api/methods/sendvideo.md): cloud multipart video up to 50 MB; HTTP URL ingestion 20 MB (photos five MB); existing `file_id` is another route. [sendPhoto](https://core.telegram.org/bots/api/methods/sendphoto.md): ten MB, width+height <=10,000, ratio <=20. [Albums](https://core.telegram.org/bots/api/methods/sendmediagroup.md): 2–10 items.
- Text-only 4,096; media caption 1,024 after entity parsing. No documented general video duration cap. [Bot FAQ](https://core.telegram.org/bots/faq/): chat/group/broadcast throughput differs; respect `parameters.retry_after`.
- Production Telegram accounts explicitly use MTProto. SAU retains their separate conservative 2,000,000,000-byte profile; no inference of Premium and no transport switch/local Bot API server enablement.

## Sociamonials backup contract

Primary source: [public REST/MCP reference](https://www.sociamonials.com/developers.php), fetched through Exa and directly on 2026-11-05.

- Defaults: reads 120/min, writes 30/min per credential; AgencyPro doubles them. Media transfers have a separate 300/min default. Actual `X-RateLimit-Limit/Remaining/Reset` and 429 `Retry-After` govern retries. Identity/workspace/profile discovery endpoints are exempt.
- All-source workspace limits: 1,000 sent/day, queue 1,500 undelivered, horizon 12 months; published provider-side YouTube ceiling ten/channel/24 hours is a Sociamonials contract, not a universal YouTube site limit.
- External `video_url`: **1,024 MB decimal**, 1,024,000,000 bytes. Hosted video: platform default **1024 MiB = 1 GiB**, 1,073,741,824 bytes, possibly lowered by the workspace plan. Read `storage.max_upload_bytes.video`; null means hosted upload is not entitled. Both configured workspaces reported that 1 GiB effective ceiling during read-only verification. This is a time-specific reading, not permission to ignore a later lower allowance.
- Pooled Media/library quota is enforced; Publisher media and external URLs have separate accounting. No library asset deletion is authorized by this change. Prefer exact ready SHA-verified reuse; filename+size alone is not equality. Stable verified external URLs may be used when appropriate, not oversized/stale originals.
- Images default 15 MiB, 100 MP and 20,000-pixel longest side at hosting; network constraints can be smaller. Multipart threshold 32 MiB, API parts 16 MiB; use returned grants and ETags rather than inventing chunk plans.
- Public text table: fb 63,206; tw 280; ln 3,000; in 2,200; pi 500; gmb 1,500; thrd 500; tiktok 150; blsk 300. The shorter TikTok field is a backup restriction, not TikTok's native 2,200-unit limit. Strict post validation is authoritative; incompatible approved copy is flagged, never silently shortened.
- Default X posts cannot contain links; Enterprise allowance is separately limited. `first_comment` is documented for fb/in/ln/blsk/thrd, **not X**. Removing a main-post link or moving it to an unimplemented reply cannot preserve intent.
- Read current `social-profiles`: exact network/profile reference, `agent_can_publish_to=true`, current availability and relevant capabilities. A mobile handoff is not automatic publication. An absent mapping is an exception, never a guessed similarly named destination.
- The no-create validation contract in the linked OpenAPI is **`POST /api/v1/posts` with `validate_only=true`, `strict=true`, `mode=publish_now`**. There is no separate `/api/v1/posts/validate` route in that schema; the earlier planning claim is corrected here. Confirm `valid=true`, `validate_only=true`, no post ID, explicit exact profile resolution and no warnings before any create. Map TikTok privacy/interactions/disclosures and YouTube visibility/tags/kids/synthetic-media/Shorts settings explicitly; reject unsupported changes.
- Asset `processing` is not ready; accepted/queued/approval-held is not delivered. Confirm the exact destination with literal delivery evidence and its network post identifier, retaining generic provider receipts for reconciliation. Ambiguous submission cannot start a second provider.

## Other configured capabilities, inactive routes and unknowns

| Route | Source evidence / limitations |
|---|---|
| Discord | [API reference](https://docs.discord.com/developers/reference) and [rate guide](https://docs.discord.com/developers/topics/rate-limits): current default file limit 20 MiB per file, possibly higher with an explicit entitlement; SAU chooses 20,000,000-byte conservative prep. Text 2,000. Use bucket/reset and `retry_after`; no hard-coded universal per-route rate. No production Discord account was present in the execution inventory. |
| LinkedIn | [Videos](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/videos-api?view=li-lms-2026-10) has feed specs three seconds–30 minutes/75 KB–500 MB, while the initialization schema allows five GB. These are different upload/serving surfaces; do not infer feed compliance from upload capacity. [Posts](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api) and UGC text 3,000. Not enabled/recovered. |
| Pinterest | [Organic API](https://developers.pinterest.com/docs/work-with-organic-content-and-users/create-boards-and-pins/), [Pin specs](https://help.pinterest.com/en/article/review-pin-specs), [rates](https://developers.pinterest.com/docs/reference/rate-limits/): organic video four seconds–five minutes, title 100/description 800 in help; API numerical byte block not independently verified. Sociamonials description is 500. Trial/Standard rate buckets differ; don't substitute ad/catalog specs. Not enabled/recovered. |
| WordPress / blogs | [wp_max_upload_size](https://developer.wordpress.org/reference/functions/wp_max_upload_size/) derives PHP limits, with host/proxy/storage/multisite and URL embedding creating additional route differences. No universal byte/duration limit exists. Use the actual configured transport and known deployment limits; unknown does not establish unlimited uploading. Production has NW/SW blogs plus Teaching/MSL WordPress routes. |
| Douyin | [Upload](https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/video-management/douyin/create-video/upload-video) total four GB, >300 MB must use transport chunks, >50 MB recommended; [create](https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/video-management/douyin/create-video/video-create/) <=15 minutes, 75 works/user/app/day, title <=1,000. Browser uploader is another route. No production account present; research does not enable one. |
| Bilibili | [Submission specifications](https://openhome.bilibili.com/doc/4/0c532c6a-e6fb-0aff-8021-905ae2409095): four GB, **less than** five hours, <=4096×2160/120 fps/60 Mbps, GOP <=ten seconds, audio <=48 kHz/two channels/320 kbps. [Create](https://openhome.bilibili.com/doc/4/f7fc57dd-55a1-5cb1-cba4-61fb2994bf0f): non-official members five submissions/day, title <80. Capability approval/authorization required. No production account present. |
| Kuaishou | [Open API](https://open.kuaishou.com/platform/openApi?menu=20): <10 MB direct uploads and <=10 MB fragments are transport recommendations, not whole-video caps. Novel-drama 20-minute material is a **different** mini-program surface. General video duration/whole-file/publishing quota not verified; unknowns remain explicit. No production account present. |
| Xiaohongshu | Official [JS](https://agora.xiaohongshu.com/doc/js), [Android](https://agora.xiaohongshu.com/doc/android), [iOS](https://agora.xiaohongshu.com/doc/ios) sharing SDKs hand off to the client; Android image notes 1–18, video notes one video/optional cover. Numeric general browser upload size/duration and a general automatic server publishing API were not verified. No production account present; no invented Sociamonials route. |
| WeChat Channels (`tencent`) | [API catalog](https://developers.weixin.qq.com/doc/channels/api/) and [errors](https://developers.weixin.qq.com/doc/channels/dev_before/base/commErrCode.html) concern commerce and capability quotas. No general ordinary-video publishing API or universal browser upload cap verified. No production account present. |
| Baijiahao | [OpenAuth](https://baijiahao.baidu.com/builder/author/openauth/openauthProtection): URL ingest, video title 8–40, <=ten tags of <=ten characters, account's dynamic daily allowance. Numeric universal video size/duration/request cap not verified. Legacy HTTP examples do not authorize insecure credential transport. No production account present. |
| Medium | [Official API archive](https://github.com/Medium/medium-api-docs/blob/master/README.md): title beyond 100 ignored, first three tags (<=25 each), image upload/embed and limited legacy API. [Help](https://help.medium.com/hc/en-us/articles/213480228-API-Importing) and [embeds](https://help.medium.com/hc/en-us/articles/214981378-Using-embeds) do not establish a new video publishing transport. No production account present. |
| Substack | [Video help](https://support.substack.com/hc/en-us/articles/21093671091220-Guide-to-video-posts-on-Substack): recommended <=20 GB, H.264/AAC, <=8 Mbps, 1080p/1440p or smaller; recommendations are not a universal API entitlement. [App announcement](https://on.substack.com/p/publish-video-posts-directly-in-the) says files up to 20 GB. No general API guarantee or production account assumed. |
| Patreon | [Formats](https://support.patreon.com/hc/en-us/articles/204606055-What-file-formats-are-supported-for-posts): ordinary upload two GB/attachment five GB; native Video has no file-size maximum but <=12 hours. [Hosting](https://support.patreon.com/hc/en-us/articles/6598555214477-Patreon-Video-overview) has account/monthly hour entitlements. [API](https://docs.patreon.com/) lists client 100 requests/two seconds and token 100/minute; this is not a publishing API. No production account present. |
| Reddit | Retired by the operator. Historical figures stay in Git history; no account enablement, backup or retry is authorized. The execution inventory includes 62 failed targets referencing retired/deleted Reddit accounts, all excluded. |

## Operational policy

Use immutable original/variant references, final measured media, complete ordered parts and strict Unicode/field checks. Native-only routes retain native policy; viable backup routes can use the conservative 1,000,000,000-byte common preparation ceiling, tightened further by current account/hosting allowances. Preserve captions, brand, language, content policy, alt text, covers and platform options. Separate posts are paced by canonical destination, including aliases, delivered history and existing schedules. Production defaults three posts/day and a 30-minute minimum gap are preserved; recovery does not raise them. After all safe routes stop, a durable, deduplicated notification retries independently without resubmitting the post. Queued/accepted is never reported as delivered.
