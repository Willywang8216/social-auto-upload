# 2026 Hard Posting Limits — Every Platform Supported by `social-auto-upload`

> **Human-readable reference — not the enforcement source.** The repo's enforced single source of truth is [`myUtils/platform_limits.py`](../myUtils/platform_limits.py) (tested by `tests/test_platform_limits.py`); per-number citations live in [`logs/platform-limits-research.md`](../logs/platform-limits-research.md). If this document and the code disagree, **the code and its tests win**. See [Enforced values](#enforced-values-applied-by-myutilsplatform_limitspy) below.

**Researched:** 2026-10-05 · **Method:** live Exa + Tavily searches (no memory), preferring 2026/official sources.
**Platforms covered:** X/Twitter, Bluesky, Facebook, Instagram, Threads, TikTok, YouTube, Reddit, Telegram, LinkedIn, Pinterest.

> **Read this first.** Most platforms enforce **two different sets of limits**: what the native app/website accepts, and what the public publishing **API** accepts. This repo publishes largely through APIs, so where the two differ both are listed. All file sizes below are **decimal** (1 MB = 1,000,000 bytes, 1 GB = 1,000,000,000 bytes) as the platforms themselves use. Grapheme vs. character counting is called out where it matters.
> Tiers that raise a limit (X Premium, Telegram Premium, Reddit Premium) are marked.

### How this maps to this repo

`social-auto-upload` publishes through each platform's public API (and, for Douyin / Xiaohongshu / Bilibili, browser automation). [`myUtils/platform_limits.py`](../myUtils/platform_limits.py) is the **enforced single source of truth**: draft generation, media prep and the individual publishers all read from it, and `tests/test_platform_limits.py` locks the values. It encodes **API-only** limits; the master table below is the broader human-readable research view (app/web maxima, byte conversions, restrictions). Where the two differ, the code is what actually runs.

### Enforced values applied by `myUtils/platform_limits.py`

Exact numbers the repo validates against today (`media_max_mb` is decimal MB; `video` is seconds; `—` = no platform cap):

| Platform | message_max_chars | media_max_mb | video (s) | max_images | max_videos |
|---|---|---|---|---|---|
| twitter | 280 | 512 | 140 | 4 | 1 |
| bluesky | 300 (+ 3,000 bytes) | 300 | 600 | 10 | 1 |
| facebook | 63,206 | 4,096 | 14,460 | 10 | 1 |
| instagram | 2,200 | 300 | 900 | 10 | 1 |
| threads | 500 | 1,024 | 300 | 20 | 1 |
| tiktok | 2,200 (API) | 4,096 | 3,600 | 35 | 1 |
| youtube | 5,000 (desc; title 100) | 262,144 | 43,200 | — | 1 |
| reddit | 40,000 (title 300) | 1,000 | 900 | 20 | 1 |
| telegram | 4,096 (caption 1,024) | 2,000 | — | 10 | 10 |
| linkedin | 3,000 | 5,000 | 900 | 20 | 1 |
| pinterest | 800 | 2,048 | 300 | 1 | 1 |

The module deliberately hard-codes API-era figures (e.g. X 512 MB / 140 s) even where a tier or the web app is more generous, so uploads always clear the API's own validation. Regenerate this table from the module if the dicts change.

---

## Master table

| Platform | Max caption / message | Max media file size (decimal MB → bytes) | Max video duration | Max images / post | Max videos / post | Notable restrictions |
|---|---|---|---|---|---|---|
| **X / Twitter** | 280 chars (free) · 25,000 chars (Premium). URLs always **23 chars**. Media doesn't count. [3][4] | Image **5 MB** (5,000,000 B); GIF **15 MB** (15,000,000 B); video **512 MB** (512,000,000 B) free — API default **8 GB** (8,000,000,000 B), Premium up to **16 GB** (16,000,000,000 B). [1][2][4] | Free web/app **140 s** (2:20); API default **1,200 s** (20 min) / 8 GB; Premium up to **7,500 s** (125 min). [1][2][5] | **4** [2] | **1** (1 video **or** 1 GIF) [2] | Adult/NSFW allowed only if marked; no adult content in profile/banner. Image 16:9 recommended, max 1920×1200 / 1200×1920. Android caps long video at 10 min. [4][5] |
| **Bluesky** | **300 graphemes** AND **3,000 UTF-8 bytes** per post. Emoji = 1 grapheme. [7][11] | Image **2 MB** (2,000,000 B) each; video **300 MB** (300,000,000 B). [6][9] | **600 s** (10 min), raised from 3 min on 2026-08-26. [6][7] | **10** (raised 2026) [10] | **1** — cannot mix images & video [11] | Images and video are mutually exclusive. Links count at full visible length. Daily cap **25 videos or 10 GB**. Email verification required for video. [6][11] |
| **Facebook** | **63,206 chars** post; comments 8,000. [12][14] | Image **30 MB** (30,000,000 B) feed/carousel (link OG image 8 MB); video **4 GB** (4,000,000,000 B) documented, organic up to ~**10 GB** (10,000,000,000 B). [14][15] | **14,460 s** (241 min) Feed documented; **Reels have no length/format cap since June 2025**; Stories ≤120 s ads / 60 s organic. [12][13] | Carousel **2–10**; multi-photo Page post practical ~15; albums up to 1,000 photos. [14][15] | **1** per post (or a photo carousel, never both) [14] | All new videos are Reels. >20% text overlay reduces reach. NSFW/sexual content banned by Community Standards. [12][13][15] |
| **Instagram** | **2,200 chars** caption; first **125** shown; max **30 hashtags**. [17][18] | Image **8 MB** (8,000,000 B) JPEG via API (app accepts ~30 MB); Reel/feed video **300 MB** (300,000,000 B) API; Story video 100 MB. [16][17] | API Reels **3–900 s** (15 min) accepted; **Reels-tab eligibility 5–90 s** (some accounts 3 min). App camera up to 20 min. [16][17] | Carousel **2–10** via API (up to **20** in app); single image = 1. [17][19] | Carousel items can be videos; single video post = 1. Up to 10 API / 20 app items. [17][19] | Image must be **JPEG** via API. Caption links are not clickable. NSFW/sexual content banned. Aspect 4:5–1.91:1 feed, 9:16 Reels. API 100 posts/24h; carousel = 1. Professional accounts only. [16][17][19] |
| **Threads** | **500 chars** post (hard); text attachments up to 10,000 chars; max **5 links**. [20][21] | Image **8 MB** (8,000,000 B); video **1,024 MB** (1,024,000,000 B) API. [20][21] | **300 s** (5 min). [20][21] | Carousel **2–20** (API/app). [21][22] | Up to 20 in a mixed carousel; single video post = 1. [22] | 500-char cap applies to post/reply. 5 links/post. 250 posts/24h. NSFW banned (Instagram guidelines). 9:16 recommended; 320–1440 px wide. [20][21] |
| **TikTok** | **4,000 chars** in-app; **2,200 chars** via Content Posting API. Hashtags/@ count. [24] | Video **4 GB** (4,000,000,000 B) via API (mobile 72 MB Android / ~288 MB iOS); image **20 MB** (20,000,000 B) each. [23][26] | API **600 s** (10 min); in-app up to 10 min (some 3/5/10); web uploads up to **3,600 s** (60 min). [23][25] | Photo Mode **1–35** (help says up to 35; carousel ads 2–35). [25][27] | **1** — no mixing; Photo Mode is photos only. [26] | **Media required** (no text-only posts). No image+video mix. NSFW/sexual content banned. 9:16 recommended. 25 API posts/24h. [23][26] |
| **YouTube** | Title **100 chars**; description **5,000 chars**; tags 500. [30][31] | Video **256 GB** (256,000,000,000 B) **or** 12 h, whichever first; thumbnail **2 MB** (2,000,000 B). [28][30] | **43,200 s** (12 h) verified; **900 s** (15 min) unverified default; **Shorts ≤180 s**. [28][29] | n/a (video platform); 1 thumbnail. Shorts/Community differ. [31] | **1** per upload/call. [30] | Unverified accounts limited to 15 min. Shorts need square/vertical and ≤3 min for the Shorts shelf. NSFW age-restricted/removed. [28][29] |
| **Reddit** | Title **300 chars**; self-post body **40,000 chars** (Reddit Premium **80,000**); comment 10,000. [32][33] | Image **20 MB** (20,000,000 B) each; GIF 100 MB; video **1 GB** (1,000,000,000 B). [35] | **900 s** (15 min) native video. [35] | Gallery **up to 20** images/GIFs (each caption 180 chars). [34][35] | **1** native video per post. [34] | Subreddit rules can be stricter (video disabled, length caps). NSFW subs restrict comment images; NSFW images not allowed in comments. Title required. [34][35] |
| **Telegram** | Message **4,096 chars** (all users); media caption **1,024 chars** free, **2,048 chars** Premium. Text-only limit ≠ caption limit. [36][39] | File **2 GB** (2,000,000,000 B) free; **4 GB** (4,000,000,000 B) Premium. Bot API: upload 50 MB / download 20 MB (self-hosted ~2 GB). [37][38] | No platform duration cap (bounded by file size); round video message **60 s**. [39] | Album **up to 10** photos/videos. [37][39] | Up to **10** in an album; caption only on the first item. [37][39] | Bot API limits far lower than user accounts (50 MB upload). Album caption applies to first item only. Photos ≤10 MB compressed, w+h ≤10,000 px. NSFW restricted on public channels/App Store. [37][38][39] |
| **LinkedIn** | Post **3,000 chars**; comment 1,250; article body 125,000; headline 220. See-more fold ~140–210 chars. [43] | Image ~**5–10 MB** (not officially published; 5 MB commonly enforced); video **5 GB** (5,000,000,000 B). [41][42] | **900 s** (15 min) desktop; **600 s** (10 min) mobile & Company Pages. [41][42] | Multi-image post up to **20** photos (File carousel = up to 300 pages). [42] | **1** native video per post. [41] | Professional-context policy; NSFW not permitted. Aspect 1:2.4–2.4:1 (practical 1:1/4:5/9:16/16:9). Video ads much smaller (200–500 MB). [41][42] |
| **Pinterest** | Title **100 chars**; description **800 chars** organic (500 ads); textbox 250. [44][45] | Image **20 MB** web (20,000,000 B) / 1 GB recommended mobile (32 MB cited); video **2 GB** (2,000,000,000 B). [44][46] | **4–300 s** (5 min) organic; up to **900 s** (15 min) ads. [45][46] | Standard Pin 1 image; Idea Pin up to **20** pages; carousel ads 2–5. [45] | 1 (Idea Pin = up to 20 video/image pages). [45] | A destination link is required. Adult/NSFW content banned. Aspect **2:3 (1000×1500)** recommended; video 9:16/1:1. [44][45][46] |

---

## Per-platform notes

### X / Twitter
- Text: 280 standard, 25,000 for any Premium tier; every URL is rewritten to a flat 23 characters (`t.co`), and media/polls don't consume the budget. [3][4]
- Media: 4 photos, **or** 1 animated GIF, **or** 1 video per post. [2]
- Official X API media table: image/GIF 5 MB/15 MB; `tweet_video` default 20 min / 8 GB, Premium 125 min / 16 GB; `dm_video` default 140 s / 512 MB. [1]
- Consumer web/app free-tier video is 140 s / 512 MB; some 2026 guides note third-party/API apps may still be held to 2:20 regardless of tier. Premium web/iOS reaches ~3–4 h; Android is capped around 10 min. [4][5]

### Bluesky
- Text is limited by **both** 300 graphemes and 3,000 UTF-8 bytes; emoji cost 1 grapheme each, which is unusual. [7][11]
- Video went 60 s → 3 min (Mar 2025) → **10 min / 300 MB (26 Aug 2026)**; the official @bsky.app post is the primary source. [6][7][8]
- Images: up to **10** per post, each exactly **2,000,000 bytes** max, ≤4000×4000, JPG/PNG/WEBP/HEIC; GIFs are not supported. Images and video cannot coexist, and a post holds either an image set or one video. [9][10][11]
- Per-account daily ceiling: 25 videos or 10 GB. Email verification required before first video. [6][11]

### Facebook
- Post text 63,206 chars; comments 8,000; private messages 20,000. [12][14]
- Feed video documented ceiling **241 min**; since June 2025 Meta made all new videos Reels with "no length or format restrictions," so modern uploads may exceed the old Reels 90 s cap. Stories: 60 s organic / up to 120 s ads. [12][13]
- File caps: images ~30 MB (link OG image 8 MB); video 4 GB documented for ads, organic commonly up to ~10 GB. [14][15]
- Carousel holds 2–10 cards; albums can hold up to 1,000 photos (an album is not a single post). NSFW/sexual content is prohibited by Community Standards. [14][15]

### Instagram
- Caption 2,200 chars (uniform across feed/Reels/carousel), 30 hashtags, 125 chars visible before "more." [17][18]
- API media reference: image JPEG ≤8 MB with aspect 4:5–1.91:1; Reels 3 s–15 min / 300 MB; Stories 60 s / 100 MB. The API strictly **requires JPEG** for images and only publishes to professional accounts. [16][17]
- Carousel: **2–10 items via API**, but the app now allows **20**; one orientation applies to all slides. [17][19]
- Reels-tab eligibility is narrower than the upload cap: 5–90 s with 9:16 aspect; longer clips publish as regular video posts. [17]
- 100 API-published posts per rolling 24 h; a carousel counts as one. [17]

### Threads
- 500-char hard cap on posts and replies; text attachments hold 10,000 chars and don't count. Max 5 links per post. [20][21]
- Media: images 8 MB (JPEG/PNG, 320–1440 px wide, aspect up to 10:1); video **1,024 MB** / 5 min (MOV/MP4, H.264/HEVC, 23–60 fps). [20][21]
- Carousel supports **2–20** mixed images/videos and counts as one post. [21][22]
- 250 posts / 1,000 replies / 100 deletions per 24 h. NSFW follows Instagram's guidelines and is not permitted. [21]

### TikTok
- Caption: **4,000 chars in-app** vs **2,200 chars through the Content Posting API** — a caption written in-app can be rejected by an automation. Hashtags and mentions count against the same 2,200 budget via API. [24]
- API video: MP4/WebM/MOV, H.264/HEVC/VP8/VP9, 23–60 fps, 360–4096 px per side, up to 4 GB, longest sendable duration 10 min (creators may have 3/5/10 min). [23][25]
- Photo Mode: up to **35 images**, ≤20 MB each (JPEG/WebP; PNG is converted), 9:16/1:1/4:5. Cannot mix photos and video; text-only posts are impossible. [25][26][27]
- 25 API posts / 24 h. NSFW/sexual content banned. 9:16 native. [26]

### YouTube
- Title 100 chars (first 70 in search), description 5,000 chars (first ~200 in search), tags 500. [30][31]
- Upload ceiling **256 GB or 12 h, whichever comes first**, for verified accounts. Unverified accounts are limited to 15 min by default. [28][30]
- Shorts: square or vertical, **≤3 min (180 s)** to be categorised as a Short; longer uploads still publish but don't enter the Shorts feed. [29]
- Thumbnails ≤2 MB (desktop up to 50 MB as of Mar 2026 per third-party tracking). NSFW content is age-restricted or removed. [31]

### Reddit
- Title 300 chars; self-post body 40,000 chars (Reddit Premium 80,000); comment 10,000. [32][33]
- Media: images ≤20 MB each; GIFs ≤100 MB; native video ≤1 GB / 15 min (MP4/MOV, H.264). Gallery posts support up to **20** images/GIFs, each with a 180-char caption. [34][35]
- Reddit is a federation: individual subreddits can disable video, impose shorter limits, or require certain post types. [35]
- NSFW images are not permitted in comments and comment images are only available in SFW communities. [35]

### Telegram
- Text message 4,096 chars for everyone; **media captions are a separate, smaller limit**: 1,024 chars free, 2,048 chars Premium. The 4,096 figure does **not** apply to captions. [36][39]
- File uploads: 2 GB free, 4 GB Premium. The Bot API is far more restrictive (50 MB upload / 20 MB download); run a local Bot API server to approach ~2 GB. [37][38]
- Albums: up to **10** photos or videos, and only the first media item can carry a caption. No hard video duration limit beyond the file-size ceiling; round video messages are 60 s. [37][39]
- Photos are compressed unless sent as files; a photo must be ≤10 MB and width+height ≤10,000 px. [37]

### LinkedIn
- Post 3,000 chars; comment 1,250; article body 125,000; connection-request note 300; DM 2,000; profile headline 220. The mobile "see more" fold is roughly 140–210 chars. [43]
- Video: 5 GB max, 3 s minimum, 15 min desktop / 10 min mobile & Company Pages; resolution 256×144–4096×2304; aspect 1:2.4–2.4:1; MP4/H.264, 10–60 fps. Ads cap at 200–500 MB. [41][42]
- Multi-image posts support up to **20** photos (5 MB each commonly enforced); document carousels up to 300 pages are a different format. [42]
- NSFW and overtly unprofessional content is not permitted. [41]

### Pinterest
- Title 100 chars (first ~30 visible), organic description 800 chars (ads 500); textbox overlay 250 chars. Description text does not show in the home/search feed but is indexed for relevance. [44][45]
- Image Pins: 20 MB max on web, 1 GB recommended on mobile (32 MB also cited); video Pins 2 GB, MP4/MOV/M4V, H.264/H.265. [44][46]
- Organic video Pins: 4 s–5 min; ads up to 15 min. Standard Pin = 1 image; Idea Pins up to 20 pages; carousel ads 2–5 cards. [45][46]
- A destination link is required for a standard Pin; adult/NSFW content is banned; 2:3 (1000×1500) is the recommended ratio. [44][45]

---

## Sources

1. X API v2 — Media upload endpoints — https://docs.x.com/x-api/media/introduction
2. X API v2 — Best practices (size & duration limits) — https://docs.x.com/x-api/media/quickstart/best-practices
3. Ferryman — X character limits (2026) — https://ferryman.io/character-limits/x
4. FileSize.org — Twitter/X file size limits (2026) — https://filesize.org/limits/twitter/
5. DMpro — Twitter video length limit (2026) — https://www.dmpro.ai/blog/twitter-video-length-limit
6. Bluesky — official @bsky.app post, 2026-08-25 (10 min / 300 MB) — https://bsky.app/profile/bsky.app/post/3mtwf7gxkwc2r
7. TechCrunch — Bluesky now lets you upload 10-minute long videos (2026-08-26) — https://techcrunch.com/2026/08/26/bluesky-now-lets-you-upload-10-minute-long-videos
8. Bluesky docs — Uploading Video — https://docs.bsky.app/docs/tutorials/video
9. AdaptlyPost — Bluesky image size limit (2,000,000 bytes) — https://adaptlypost.com/blog/bluesky-image-size-limit
10. PostFast — Bluesky Post Size (10 images, 2026) — https://postfa.st/sizes/bluesky/posts
11. PublishQ — Bluesky API limits (300 graphemes / 3,000 bytes) — https://publishq.com/blog/bluesky-api-post-limits
12. Meta Business Help — Video length specifications across placements (241 min) — https://www.facebook.com/business/help/817989058548892
13. Meta Business Help — Requirements for Facebook Reels (no length/format limits) — https://www.facebook.com/business/help/1197310377458196
14. AllPlatforms.io — Facebook specs (2026) — https://allplatforms.io/facebook
15. Crowbert — Facebook video format, size & upload limits (2026) — https://www.crowbert.com/blog/facebook-video-format-and-size
16. Meta — Instagram Content Publishing (API media specs) — https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/content-publishing/
17. Upload-Post — Instagram API limits (2026) — https://www.upload-post.com/instagram-api
18. GrowthScribe — Instagram caption character limit (2026) — https://growthscribe.com/instagram-caption-character-limit
19. SocialRails — Instagram carousel posts guide (up to 20 slides, 2026) — https://socialrails.com/blog/instagram-carousel-posts-guide
20. Meta — Threads Posts (API media specifications) — https://developers.facebook.com/docs/threads/posts/
21. Upload-Post — Threads API limits (2026) — https://www.upload-post.com/threads-api
22. Recurpost — Meta Threads image & video specs (20-item carousel) — https://recurpost.com/meta-threads-scheduler/meta-threads-image-and-video-specs
23. TikTok for Developers — Media Transfer Guide (video/image restrictions) — https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide
24. Koda — TikTok caption limit: 4,000 in app, 2,200 via API (2026) — https://kodahq.app/en/reference/tiktok-caption-and-hashtag-limits
25. PostFast — TikTok video size & aspect ratio (2026) — https://postfa.st/sizes/tiktok/video
26. PublishQ — TikTok limits (2026) — https://publishq.com/tiktok-limits
27. TikTok for Business — Specifications for Carousel Ads (2–35 images) — https://ads.tiktok.com/resources/help/article/specifications-for-carousel-ads
28. YouTube Help — Upload videos longer than 15 minutes (256 GB / 12 h) — https://support.google.com/youtube/answer/71673
29. YouTube Help — Understand three-minute YouTube Shorts — https://support.google.com/youtube/answer/15424877
30. Google for Developers — YouTube Data API `videos.insert` (256 GB) — https://developers.google.com/youtube/v3/docs/videos/insert
31. TheLoops — YouTube limits reference (title 100 / description 5,000) — https://theloops.live/tools/youtube-limits
32. Reddit — API reference `POST /api/submit` (title ≤300) — https://www.reddit.com/dev/api/
33. RedReplier — Reddit post character limit (Premium 80,000) — https://redreplier.com/blog/reddit-post-character-limit
34. Reddit — Introducing Reddit Image Galleries (up to 20 images) — https://www.redditinc.com/blog/introducing-reddit-image-galleries
35. AllPlatforms.io — Reddit specs (20 MB image / 1 GB·15 min video) — https://allplatforms.io/reddit
36. Telegram Info — Telegram Limits (message 4,096 / caption 1,024) — https://limits.tginfo.me/en
37. Telegram — Uploading and Downloading Files (2 GB / 4 GB) — https://core.telegram.org/api/files
38. Carly — Telegram file size limit: 2 GB free, 4 GB Premium (2026) — https://www.usecarly.com/blog/telegram-file-size-limit
39. SkyBots — Telegram limits reference (caption, albums, 10 media) — https://skybots.ru/en/telegram-limity
40. LinkedIn Help — Video sharing troubleshooting — https://www.linkedin.com/help/linkedin/answer/a548372
41. PostFast — LinkedIn video size (5 GB / 15 min, 2026) — https://postfa.st/sizes/linkedin/video
42. ConnectSafely — How many photos can you post on LinkedIn (20 images, 2026) — https://connectsafely.ai/articles/how-many-photos-can-you-post-on-linkedin-2026
43. AuthoredUp — LinkedIn character limits (2026) — https://authoredup.com/blog/linkedin-character-limit
44. Pinterest Help — Review Pin specs (title 100 / description 800 / image 20 MB) — https://help.pinterest.com/en/article/review-pin-specs
45. AllPlatforms.io — Pinterest specs (video 4 s–5 min organic, 2 GB) — https://allplatforms.io/pinterest
46. Recurpost — Pinterest pin dimensions (20 MB desktop image, 800-char description) — https://recurpost.com/best-pinterest-scheduler/pinterest-pin-dimensions

---

## Caveats

- Third-party "spec" guides frequently disagree (e.g. Instagram feed video 300 MB API vs 4 GB ads; Threads image 8 MB API vs 100 MB app; Facebook image 15 MB vs 30 MB). Where they conflicted this document prefers the platform's own docs and marks app-vs-API differences explicitly.
- Reddit, Facebook and Telegram apply **community/channel-level** rules on top of the platform caps; an individual subreddit, Page or channel can be stricter.
- Platform limits change; verify against the linked official source before shipping a hard validator. Fields most likely to have moved since this pass: Bluesky (recently raised), Facebook Reels (no cap since June 2025), Instagram Reels duration, and TikTok API caption/file-size caps.
