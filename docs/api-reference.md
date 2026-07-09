# SocialUpload API Reference

Base URL: `https://socialupload.iamwillywang.com`

All endpoints require a Bearer token in the `Authorization` header (except public routes).

```
Authorization: Bearer YOUR_API_TOKEN
```

---

## Authentication

The API uses Bearer token authentication. Set the token in `.env`:
```
SAU_API_TOKENS=your-strong-token-here
```

If no tokens are configured, the system runs in **open mode** (no auth required).

---

## System / Health

| Method | Path | Description |
|--------|------|-------------|
| GET | `/whoami` | Health check — returns `{authenticated, openMode}` |
| GET | `/` | Serve frontend SPA |

---

## File Upload

| Method | Path | Description | Parameters |
|--------|------|-------------|------------|
| POST | `/upload` | Upload file via multipart form | `file` (form) |
| POST | `/upload/direct` | Get presigned URL for direct upload | `filename`, `content_type` |
| POST | `/upload/file` | Upload file to DigitalOcean Spaces | `file` (form) |
| POST | `/upload/register` | Register externally-uploaded file | `filename`, `storage_key` |
| POST | `/upload/multipart/init` | Initialize multipart upload | `filename`, `content_type` |
| POST | `/upload/multipart/presign` | Get presigned URLs for parts | `upload_id`, `key`, `part_numbers` |
| POST | `/upload/multipart/complete` | Complete multipart upload | `upload_id`, `key`, `parts` |

### Example: Upload a file
```bash
curl -X POST https://socialupload.iamwillywang.com/upload \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "file=@/path/to/video.mp4"
```

---

## File Management

| Method | Path | Description |
|--------|------|-------------|
| GET | `/getFiles` | List all file records |
| GET | `/getFile?filepath=<path>` | Download/retrieve a file |
| GET | `/deleteFile?fileId=<id>` | Delete a file record |
| POST | `/deleteFiles` | Batch delete files |

---

## Account Management

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/accounts` | List all accounts with status |
| GET | `/api/accounts/health` | Account health summary |
| POST | `/api/accounts/<id>/check` | Check single account connection |
| POST | `/accounts/<id>/refresh-token` | Refresh OAuth tokens |
| POST | `/accounts/batch/check-connections` | Batch connection check |
| POST | `/accounts/batch/refresh-tokens` | Batch token refresh |
| POST | `/accounts/maintenance/run` | Trigger auto-refresh maintenance |
| GET | `/accounts/maintenance/status` | Maintenance scheduler status |
| PATCH | `/accounts/<id>` | Update account fields |

### Example: List accounts
```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  https://socialupload.iamwillywang.com/api/accounts
```

---

## Profiles

| Method | Path | Description |
|--------|------|-------------|
| GET | `/profiles` | List all profiles |
| POST | `/profiles` | Create a profile |
| GET | `/profiles/<id>` | Get profile details |
| PATCH | `/profiles/<id>` | Update profile |
| DELETE | `/profiles/<id>` | Delete profile |
| GET | `/profiles/<id>/accounts` | List accounts in profile |
| POST | `/profiles/<id>/accounts` | Create account in profile |

---

## Publishing

| Method | Path | Description |
|--------|------|-------------|
| POST | `/publish-center/submit` | Submit a publish job |
| POST | `/publish-center/preview` | Preview publish draft |
| POST | `/publish-center/regenerate` | Regenerate AI draft |

### Example: Submit a publish job
```bash
curl -X POST https://socialupload.iamwillywang.com/publish-center/submit \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "profileIds": [3],
    "selectedAccountIds": [77, 106],
    "mediaFilePaths": ["uploads/uuid_video.mp4"],
    "brief": "My post content",
    "options": {"tiktokDirectPost": true},
    "tiktokPostSettings": {
      "100": {
        "title": "My TikTok Title",
        "privacyLevel": "PUBLIC_TO_EVERYONE",
        "consentChecked": true
      }
    }
  }'
```

---

## Jobs

| Method | Path | Description |
|--------|------|-------------|
| POST | `/jobs` | Create a new job |
| GET | `/jobs` | List jobs (filter by `status`, `limit`) |
| GET | `/jobs/<id>` | Get job details |
| POST | `/jobs/<id>/cancel` | Cancel a job |
| POST | `/jobs/run` | Trigger job runner manually |

---

## OAuth Flows

### Reddit
| Method | Path | Description |
|--------|------|-------------|
| POST | `/oauth/reddit/start` | Start OAuth (returns authorize URL) |
| GET | `/oauth/reddit/callback` | OAuth callback |

### Meta (Facebook/Instagram)
| Method | Path | Description |
|--------|------|-------------|
| POST | `/oauth/meta/start` | Start Meta OAuth |
| GET | `/oauth/meta/callback` | Meta callback |

### YouTube
| Method | Path | Description |
|--------|------|-------------|
| POST | `/oauth/youtube/start` | Start YouTube OAuth |
| GET | `/oauth/youtube/callback` | YouTube callback |

#### YouTube Upload Fields

YouTube uploads support all API fields via the account config or draft:

**Snippet fields:**
- `title` — Video title (max 100 chars)
- `description` — Video description (max 5000 chars)
- `tags` — List of keyword tags
- `categoryId` — YouTube category ID (default: "22" for People & Blogs)
- `defaultLanguage` — Language code (e.g., "en")
- `defaultAudioLanguage` — Audio language code

**Status fields:**
- `privacyStatus` — "public", "private", or "unlisted"
- `publishAt` — Scheduled publish time (ISO 8601, requires privacyStatus="private")
- `license` — "youtube" or "creativeCommon"
- `embeddable` — Whether video can be embedded (default: true)
- `publicStatsViewable` — Whether stats are publicly visible (default: true)
- `madeForKids` — Whether video is made for kids
- `containsSyntheticMedia` — Flag for AI-generated content

**Recording details:**
- `recordingDate` — When video was recorded (ISO 8601)
- `locationDescription` — Text description of location
- `latitude` / `longitude` — Geographical coordinates

**Thumbnails:**
- Auto-uploaded from image artifacts or same-name .png/.jpg files

**Example config:**
```json
{
  "channelId": "UC_xxx",
  "privacyStatus": "public",
  "categoryId": "22",
  "defaultLanguage": "en",
  "embeddable": true,
  "publicStatsViewable": true,
  "license": "youtube"
}
```

### TikTok
| Method | Path | Description |
|--------|------|-------------|
| POST | `/oauth/tiktok/start` | Start TikTok OAuth |
| GET | `/oauth/tiktok/callback` | TikTok callback |
| GET/POST | `/webhooks/tiktok` | TikTok webhook |

### Twitter/X
| Method | Path | Description |
|--------|------|-------------|
| POST | `/oauth/twitter/start` | Start Twitter OAuth |
| GET | `/oauth/twitter/callback` | Twitter callback |

### Threads
| Method | Path | Description |
|--------|------|-------------|
| POST | `/oauth/threads/start` | Start Threads OAuth |
| GET | `/oauth/threads/callback` | Threads callback |

---

## TikTok Specific

| Method | Path | Description |
|--------|------|-------------|
| GET | `/tiktok/creator-info/<account_id>` | Get creator info (nickname, limits, etc.) |
| GET | `/tiktok/publish-status/<job_id>` | Check publish status |
| POST | `/media/video-info` | Get video metadata (duration, resolution) |

---

## Analytics

| Method | Path | Description |
|--------|------|-------------|
| POST | `/analytics/sync` | Trigger analytics sync |
| GET | `/analytics/overview` | Aggregated stats |
| GET | `/analytics/videos` | List videos with analytics |
| GET | `/analytics/top-videos` | Top performing videos |
| GET | `/analytics/trends` | Trends over time |
| GET | `/analytics/sync/status` | Recent sync log |

---

## Campaigns

| Method | Path | Description |
|--------|------|-------------|
| POST | `/campaigns/prepare` | Prepare a campaign |
| GET | `/campaigns/<id>` | Get campaign details |
| POST | `/campaigns/<id>/publish` | Publish a campaign |
| POST | `/api/campaigns/<id>/generate` | AI-generate content |
| POST | `/api/campaigns/<id>/validate` | Validate before publish |
| POST | `/api/campaigns/<id>/approve` | Approve campaign |

---

## Media Assets

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/media/upload/batch` | Batch upload media |
| GET | `/api/media/assets` | List media assets |
| GET | `/api/media/assets/<id>` | Get single asset |
| DELETE | `/api/media/assets/<id>` | Delete asset |

---

## Rate Limits

See [API Rate Limits Documentation](./api-rate-limits.md) for detailed per-platform limits.

| Platform | Daily Limit | Rate Limit |
|----------|-------------|------------|
| YouTube | ~100 uploads/day | 10,000 units/day |
| TikTok | ~15 posts/day | 6 req/min per token |
| Reddit | Anti-spam throttled | 100 QPM |
| Twitter | ~50/day (Free) | Per-endpoint, 15-min |
| Facebook | ~25/page | 200×DAU calls/hr |
| Instagram | 50/24h (hard) | BUC limits |
| Threads | 250/24h | Per-profile |
| Bilibili | No published limit | No published limit |
| Douyin | 75/day/user | Per-scope quota |

---

## Error Responses

All errors follow this format:
```json
{
  "code": 400,
  "msg": "Error description",
  "data": null
}
```

Common status codes:
- `200` — Success
- `400` — Bad request
- `401` — Unauthorized (invalid/missing token)
- `404` — Not found
- `500` — Internal server error
