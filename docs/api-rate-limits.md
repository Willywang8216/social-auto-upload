# Platform API Rate Limits & Quotas

This document covers the API rate limits, quotas, and upload constraints for every platform supported by SocialUpload. Use this as a reference when configuring accounts and planning content schedules.

## Quick Reference

| Platform | Daily Post Limit | Rate Limit | Max Upload | Token Refresh |
|----------|-----------------|------------|------------|---------------|
| YouTube | ~100/day | 10,000 units/day | 256 GB | Auto (refresh_token) |
| TikTok | ~15/day/creator | 6 req/min per token | 4 GB | Auto (refresh_token) |
| Reddit | Anti-spam throttled | 100 QPM | N/A (links) | Auto (refresh_token) |
| Twitter/X | ~50/day (Free) | Per-endpoint, 15-min | 512 MB | Auto (refresh_token) |
| Facebook | ~25/page | 200×DAU calls/hr | N/A | Auto (60-day extend) |
| Instagram | 50/24h (hard) | BUC limits | 250 MB | Auto (60-day extend) |
| Threads | 250/24h | Per-profile | IMG/VIDEO | Auto (60-day extend) |
| Bilibili | No published limit | No published limit | 4 GB | N/A (cookie) |
| Douyin | 75/day/user | Per-scope quota | 4 GB | N/A (cookie) |
| Xiaohongshu | Not documented | Not documented | Not documented | N/A (browser) |

---

## YouTube Data API v3

### Quota System
- **Default allocation**: 10,000 units/day per Google Cloud project
- **Reset time**: Midnight Pacific Time
- **No paid tier** — quota cannot be purchased, only requested via audit

### Granular Quota Buckets (Dec 2025+)
- `search.list`: 100 calls/day (own bucket)
- `videos.insert` (upload): 100 calls/day (own bucket)
- All other methods: shared 10,000 units/day

### Quota Costs

| Operation | Units |
|-----------|-------|
| `videos.list`, `channels.list`, `playlists.list` | 1 |
| `search.list` | 100 (own bucket) |
| `videos.insert` (upload) | 100 (own bucket) |
| `videos.update` / `delete` | 50 |
| `playlists.insert` / `update` / `delete` | 50 |
| `thumbnails.set` | 50 |
| `captions.insert` | 400 |

### Upload Limits
- **File size**: Up to 256 GB (or 12 hours)
- **Daily uploads**: ~100/day (limited by `videos.insert` bucket)
- **Resumable upload** required for large files

### How We Handle It
- Token auto-refresh via `refresh_token` (never expires)
- Worker refreshes before each publish
- Quota errors surface as `PreparedPublishError`

---

## TikTok Content Posting API

### Rate Limits
- **General API**: 600 requests/minute
- **Upload init**: 6 requests/minute per user token
- **Pending shares**: Max 5 videos in 24h
- **Direct Post**: ~15 posts/day/creator

### Upload Limits
- **Max size**: 4 GB
- **Max duration**: 10 minutes (some accounts up to 60 min)
- **Formats**: MP4 (H.264), WebM
- **Chunked upload**: Required for files >64 MB
  - Min chunk: 5 MB
  - Max chunk: 64 MB
  - Sequential upload required

### How We Handle It
- Token auto-refresh via `refresh_token`
- Chunked upload with retry (3 attempts per chunk)
- Prefers `PULL_FROM_URL` over `FILE_UPLOAD` to avoid chunk issues
- Falls back to `FILE_UPLOAD` when domain not verified
- Video duration validation against `max_video_post_duration_sec`

---

## Reddit API

### Rate Limits
- **OAuth clients**: 100 QPM (averaged over 10-minute window)
- **Without OAuth**: 10 QPM (effectively blocked)
- **Response headers**: `X-Ratelimit-Used`, `X-Ratelimit-Remaining`, `X-Ratelimit-Reset`

### Posting Limits
- Anti-spam cooldowns (10-15 min for rapid posting)
- Cooldown decreases with subreddit karma
- New accounts rate-limited more aggressively

### How We Handle It
- OAuth API with `refresh_token` (auto-refresh)
- Multi-proxy support with failover (residential IPs)
- Cookie-based fallback via browser automation
- Token_v2 extraction from browser session cookies

---

## Twitter/X API v2

### Tier-Based Limits

| Tier | Price | Posts/Month |
|------|-------|-------------|
| Free | $0 | ~1,500 |
| Basic | $200/mo | ~3,000 |
| Pro | $5,000/mo | ~300,000 |

### Rate Limits (per 15-minute window)
- **Tweet creation (Free)**: ~50/day
- **Tweet creation (Basic)**: ~3,000/month
- **Media upload**: Separate rate limits

### Media Limits
- **Images**: 5 MB (PNG, JPEG, GIF)
- **GIF**: 15 MB
- **Video**: 512 MB, max 2:20

### How We Handle It
- Token auto-refresh via `refresh_token`
- Media upload with retry

---

## Facebook Graph API

### Rate Limits
- **Application-level**: `200 × Daily Active Users` calls/hour
- **User-level**: Per-user limit (not disclosed)
- **Headers**: `X-App-Usage` (JSON with `call_count`, `total_cputime`, `total_time`)

### Posting Limits
- **Pages**: ~25 posts/day recommended
- **User profiles**: No explicit limit, but spam detection active

### How We Handle It
- Long-lived token (60 days) with auto-extend via `fb_exchange_token`
- Worker refreshes tokens before expiry
- Token validation before publish

---

## Instagram Graph API

### Publishing Limits
- **50 posts per 24-hour moving period** (hard limit)

### Upload Limits
- **Images**: 8 MB (JPEG, PNG)
- **Video (Feed)**: 250 MB, max 60 minutes
- **Video (Reels)**: 250 MB, max 90 seconds
- **Carousel**: Up to 10 items

### Requirements
- Business or Creator account
- Connected Facebook Page
- `instagram_content_publish` permission

### How We Handle It
- Long-lived token (60 days) with auto-extend
- Two-step publish: create container → publish
- Token validation before publish
- Old tokens without expiry detected as expired

---

## Threads API

### Rate Limits
- **250 posts per 24-hour period** per profile

### Content Types
- TEXT (500 chars), IMAGE, VIDEO, CAROUSEL

### How We Handle It
- Long-lived token (60 days) with auto-extend
- Two-step container model (same as Instagram)
- 30-second wait between container creation and publish

---

## Bilibili

### Upload Limits
- **File size**: 4 GB
- **Duration**: < 5 hours
- **Formats**: MP4, FLV
- **Resolution**: Max 4096×4096
- **Frame rate**: Max 120 fps
- **Audio**: Max 320 Kbps, 48 KHz

### How We Handle It
- Uses `biliup` library for upload
- Cookie-based authentication
- No published rate limits

---

## Douyin (抖音)

### Quota System
- **Reset**: Daily at 8:00 AM Beijing Time
- **Limit**: 75 posts/day/user/application

### Upload Limits
- **Single upload**: 128 MB
- **Chunked upload**: Required >50 MB (recommended), >128 MB (mandatory)
- **Max total**: 4 GB
- **Duration**: Max 15 minutes
- **Formats**: MP4, WebM

### How We Handle It
- Cookie-based authentication via browser automation
- CLI and web UI support
- Title max 1000 chars

---

## Xiaohongshu (小红书)

### API Overview
- Primarily e-commerce focused
- Content publishing requires special approval
- China-focused, documentation in Chinese

### How We Handle It
- Browser automation (no official content API)
- Cookie-based authentication
