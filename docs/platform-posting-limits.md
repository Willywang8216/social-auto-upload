# Platform publishing limits and preprocessing

Updated 2026-11-06. Enforcement is owned by `myUtils/platform_limits.py`, `myUtils/text_limits.py`, and the media validators. [API rate limits](platform-api-rate-limits.md) contains essential citations; `logs/platform-limits-research.md` records the 24 fresh Exa search request IDs, source-retrieval evidence, units, and uncertainty. Earlier source records remain in Git history.

These are route-specific ceilings and conservative SAU compatibility policies, not consumer-app limits or automatic plan entitlements. MB/GB below are decimal unless exact bytes are stated. Fresh account allowances and upload grants may tighten the prepared output; request overrides cannot raise them. Unknown constraints do not grant unlimited upload permission.

## Video policy used by SAU

| Platform / transport | Maximum prepared bytes | Duration | Interpretation |
|---|---:|---:|---|
| X Post / native media | 512,000,000 | 140 s | Existing conservative policy. Newer 20 min / 8 GB and Premium 125 min / 16 GB Post entitlements are documented, not enabled by this change. |
| Bluesky video service | 300,000,000 | 600 s | Current official video announcement and published lexicon. Poll until a BlobRef; inspect current account upload allowance. |
| Facebook Page `/videos` | 4,000,000,000 | 14,460 s | Existing conservative Page-video policy. Universal Meta maxima were not independently retrieved. **Not the Reels route.** |
| Instagram Reels API | 300,000,000 | 3–900 s | Qualified compatibility profile corroborated by publishing references; Meta reference retrieval returned 403. |
| Threads API | 1,000,000,000 | >0–300 s | Qualified compatibility profile; Meta reference retrieval returned 403. |
| TikTok Content Posting | 4,000,000,000 | SAU ≤600 s and fresh creator allowance | Offline baseline 180 s. The documented inbox-upload ceiling is 600 s; Direct Post uses creator-specific allowances. SAU retains a conservative 600-second compatibility ceiling rather than automatically enabling longer creator entitlements. |
| YouTube Data API | 256,000,000,000 | ≤43,200 s | Default 900 s unless fresh channel status confirms long uploads are allowed. Shorts remain a separate intended post type. |
| Telegram cloud Bot API multipart | 50,000,000 | No general documented duration cap | HTTP URL ingestion is separately limited to 20,000,000 bytes. Existing `file_id` is a different transport. |
| Telegram explicitly configured MTProto user | 2,000,000,000 | No general documented duration cap | Existing conservative user profile; no inferred Premium entitlement, local Bot API server, or silent transport switch. |
| Discord attachment (inactive) | 20,000,000 | No universal cap asserted | Conservative decimal budget below the documented 20 MiB default. |
| LinkedIn (inactive) | 5,000,000,000 | 900 s | Existing policy only. Upload schema capacity is not feed-spec compliance; no route enablement. |
| Pinterest (inactive) | 2,000,000,000 | 300 s | Existing policy; universal API byte ceiling not newly verified. |
| Reddit (retired) | Legacy only | Legacy only | No enablement, backup, or recovery. |
| NW/SW blogs and Teaching/MSL WordPress | Host/transport dependent | Host/transport dependent | PHP, proxy, storage, and URL embedding differ. No universal provider number is invented. |

Fresh creator/channel allowances must come from the exact selected account, not arbitrary request flags. Selecting multiple accounts uses the tightest applicable constraints. Native-only accounts keep their native profile; a verified viable backup can tighten the common profile before queueing.

### Route differences

- Facebook Page Reels' retrieved 3–90-second, 9:16 API contract does not govern `/videos`. Consumer rollout announcements cannot remove an API validator.
- Instagram's implemented API carousel remains 2–10 children. Threads retains the implemented conservative ten-image cap even where other API/app references describe twenty. Split videos are separate scheduled posts, not silently changed into carousel children.
- TikTok privacy, interactions, disclosures, creator consent, and direct-post versus inbox mode remain part of the original intent.
- Direct video uses `/v2/post/publish/video/init/`; explicit inbox video uses `/v2/post/publish/inbox/video/init/` with `source_info` only. Video `post_mode` is not a substitute for endpoint selection. Public intent is never silently changed to `SELF_ONLY`. A public `PUBLISH_COMPLETE` still awaits moderation when its structured public-post-ID list is empty; inbox handoff requires creator action and review notification.
- X `tweet_video` differs from `dm_video`; upload completion is not proof that the posting account can attach it to a Post.
- Bluesky `app.bsky.embed.images` supports four images. The newer gallery is a different schema and is not implemented by increasing a constant.

## Still images, albums, and covers

The following are **prepared-output policies**. Lower live backup grants also apply. Animated images are not silently flattened. Normalization preserves framing with scale/pad rather than destructive cropping, preserves order and alt text, and records final hashes.

| Implemented route | Images per post | Prepared image bytes | Notes |
|---|---:|---:|---|
| X static media | 4 | 5,000,000 | JPEG/PNG compatibility output; GIF/video is a different attachment contract. |
| Bluesky `embed.images` | 4 | 2,000,000 | Exact published schema; preparation aims below the ceiling. |
| Facebook Page photos | 10 | 4,000,000 | Conservative SAU output policy, below corroborated provider ceilings. |
| Instagram feed/carousel | 10 | 8,000,000 | JPEG; conservative 320–1080 px width and 4:5–1.91:1 framing profile. |
| Threads carousel | 10 | 8,000,000 | JPEG/PNG; qualified compatibility profile and conservative count. |
| TikTok Direct Post photos | 35 native; 10 common backup | 20,000,000 native | JPEG compatibility output; photo and video schemas differ. Current backup hosting may be tighter. |
| Telegram photo/media group | 10 | 10,000,000 | Width + height ≤10,000; ratio ≤20. Albums and text-only messages use different contracts. |
| YouTube thumbnail | Cover only | 2,000,000 | Not a standalone image publication; retain the exact cover intent. |

Support thumbnails/covers stay attached to their intended video and are not counted as ordinary photo posts. Context TXT, transcript WAV/audio, and metadata files cannot be sent to media upload endpoints. Unsupported cover or alt-text intent on a backup stops for review rather than dropping it.

## Field-specific text counting

`text_limits.validate_message()` returns the exact text or raises. The shared grapheme dependency prevents splitting combining sequences and emoji in ordinary draft truncation; approved recovery text is always validated without shortening or regeneration.

| Field / route | Limit | Counting unit |
|---|---:|---|
| X main Post | 280 | twitter-text-style Unicode weights; HTTP(S) URLs count as 23 |
| Bluesky Post | 300 and 3,000 | Graphemes **and** UTF-8 bytes |
| TikTok video caption | 2,200 | UTF-16 units |
| YouTube description | 5,000 | **UTF-8 bytes**, not Python characters |
| YouTube title / tags | 100 / 500 | Title characters; aggregate tags include commas and quotes around tags with spaces |
| Instagram caption | 2,200 | Retained qualified character policy |
| Threads main text | 500 | Retained qualified character policy; emoji-specific provider uncertainty remains documented |
| Facebook main text | 63,206 | Retained character policy |
| Telegram text / media caption | 4,096 / 1,024 | After entity parsing; notification chunks additionally respect UTF-16 budgets |
| Sociamonials TikTok message | 150 | Provider backup restriction; strict validation is authoritative |

Sociamonials may impose additional restrictions. In particular, unsupported X links cannot be removed or moved to an undocumented first-comment path. Incompatible approved text becomes an actionable exception, not a rewritten post.

## Preparation and immutable scheduling contract

1. Resolve the exact original/variant/part through existing storage mappings and verify integrity.
2. Probe actual streams, duration, size, geometry, and encoding; apply the existing watermark/content variant.
3. Normalize only as necessary. Common video output is progressive MP4/H.264/yuv420p, square pixels, fast-start, closed GOP, and AAC mono/stereo up to 48 kHz. These are compatibility choices, not claims that every provider prohibits other formats.
4. Compress against a measured byte budget with headroom and bounded attempts. Split complete content when duration or the quality floor requires it.
5. Validate every final output after all transformations. Immutable manifests record policy version, checksums, ordered source intervals, group and part counts.
6. Return the complete ordered set for scheduling. No first-part-only upload, oversized-original fallback, discarded tail, or in-uploader resplitting.

An explicit recovery may derive a new generation from an intact checksum-verified older prepared part, retaining original interval/provenance identity. It never overwrites paths used by scheduled/published work. Live uploaders validate the scheduled artifacts rather than silently rewriting them. Encoding and probing happen outside queue write transactions.

## Sociamonials backup and final alerts

Sociamonials is a transport backup, not a policy or destination bypass. Resolve the effective per-account mapping, exact workspace, current connected profile, grants, capabilities, approval policy, and storage. Teaching never falls back to the default workspace's credential.

The live hosted-video ceiling observed on 2026-11-05 was **1,073,741,824 bytes (1 GiB)** in both configured workspaces. External `video_url` has a separate **1,024,000,000-byte** ceiling. The common video preparation profile uses at most **1,000,000,000 bytes**, tightened by current native/backup allowances. Pooled storage is rechecked before each upload; no asset deletion or plan upgrade is authorized. Reuse requires exact ready media identity; a stable public URL must be accessible and byte-verified.

Strict no-create provider validation (`POST /api/v1/posts`, `validate_only=true`, `strict=true`) precedes submission. Privacy, interactions, disclosures, YouTube settings, post type, copy, covers, and alt text must be supported unchanged. Literal per-destination confirmed delivery with a usable network post receipt is success. Processing, partial delivery, approval holds, and uncertain requests retain receipts for reconciliation/review.

Native and backup have separate finite persisted budgets. Once backup is selected, retries retain its provider and idempotency key. `Retry-After` and reset deadlines release concurrency without being shortened. Terminal failure/blocked/review transitions atomically enqueue a durable notification event; notification retry never republishes. Sent events remain deduplicated by target and recovery generation.

## Scheduling and recovery

Prepare **all** eligible media first, then allocate future slots across canonical destinations. Preserve production's **three posts per destination per day and 30-minute minimum gap**, including shared aliases, confirmed delivery history, existing bookings, and unresolved/in-flight work. Recovery cannot raise or disable the configured cap.

The [failed-publication recovery guide](failed-publication-recovery.md) describes frozen selection, private prepare/apply manifests, guarded atomic all-part installation, and exception reporting. Successful, cancelled, duplicate, disabled, deleted, retired, receipt-bearing, uncertain, and unrepairable targets are not blindly retried. Queued and accepted outcomes are not delivered outcomes.

## Inactive and manual routes

Douyin, Bilibili, Kuaishou, Xiaohongshu, WeChat Channels, Baijiahao, Discord, LinkedIn, Pinterest, Medium, Substack, and Patreon have route-specific evidence or explicit unknowns in the API guide/source record. Browser transport chunks are not separate posts. Research does not enable a route or invent an automatic backup.

## Existing bounded AI context

Before caption generation, long transcripts are deduplicated only for adjacent identical lines, split at UTF-8-safe boundaries into at most 32 batches, and summarized through at most three reduction passes. The full source stays intact. A source hash reuses summaries across accounts rather than sending the same transcript repeatedly. Empty or oversized summaries fail instead of looping. These byte budgets are application policy, not model TPM entitlements; provider/account limits and returned wait/reset values remain authoritative. This release does not redesign caption generation or regenerate approved recovery copy.

## Shared NW/SW SFW TikTok

Keep separate profile-owned account entries with the exact existing destination, never a copied OAuth refresh credential or a foreign account selection. Native-first remains available where the selected account can publish publicly; an explicitly configured managed alias may use the verified same-account Sociamonials connection. Teaching remains isolated. Shared aliases receive one duplicate guard, one lease exclusion and one mutable booking calendar across synchronous/asynchronous finalization and recovery. Same original media sets are queued once without losing ordered tail parts.

`myUtils/destination_pacing.py` owns the shared booking/allocation contract. Daily budgets use the configured operator timezone; queue timestamps remain UTC. Confirmed identical provider receipts count once, while running/unknown and overdue unresolved work consume capacity. Publish-now requests are paced and request overrides cannot raise or disable a configured positive cap. Missing/corrupt booking history blocks allocation instead of creating an empty calendar.
