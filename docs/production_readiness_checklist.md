# Production Readiness Checklist

## Required Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `SAU_API_TOKENS` | Comma-separated bearer tokens for API auth | Yes (production) |
| `SAU_TIKTOK_VERIFIED_URL_PREFIXES` | Comma-separated verified URL prefixes for TikTok PULL_FROM_URL | Yes (TikTok Direct Post) |
| `SAU_COOKIE_ENCRYPTION_KEY` | Base64 AES key for cookie encryption | Optional |
| `SAU_APP_ORIGIN` | Frontend origin for OAuth postMessage | Yes (OAuth flows) |
| `SAU_ACCOUNT_MAINTENANCE_INTERVAL_SECONDS` | Auto-refresh interval (0 to disable) | Optional |
| `TIKTOK_CLIENT_KEY` | TikTok app client key | Yes (TikTok) |
| `TIKTOK_CLIENT_SECRET` | TikTok app client secret | Yes (TikTok) |
| `YT_CLIENT_ID` | YouTube OAuth client ID | Yes (YouTube) |
| `YT_CLIENT_SECRET` | YouTube OAuth client secret | Yes (YouTube) |
| `X_CLIENT_ID` | Twitter/X OAuth client ID | Yes (Twitter) |
| `X_CLIENT_SECRET` | Twitter/X OAuth client secret | Yes (Twitter) |
| `REDDIT_CLIENT_ID` | Reddit OAuth client ID | Yes (Reddit) |
| `REDDIT_CLIENT_SECRET` | Reddit OAuth client secret | Yes (Reddit) |

## Backup Files/Directories

| Path | Description |
|------|-------------|
| `db/database.db` | SQLite database (accounts, jobs, campaigns, etc.) |
| `cookies/` | Platform cookie files |
| `cookiesFile/` | Additional cookie storage |
| `.env` | Environment configuration |
| `videoFile/` | Uploaded media files |
| `data/` | Application data |

## Backup Command

```bash
# Create backup
tar czf backup-$(date +%Y%m%d-%H%M%S).tar.gz \
  db/ cookies/ cookiesFile/ .env videoFile/ data/

# Copy to safe location
cp backup-*.tar.gz /backup/location/
```

## Restore Drill

```bash
# 1. Stop the container
docker compose down

# 2. Restore files
tar xzf backup-YYYYMMDD-HHMMSS.tar.gz

# 3. Rebuild and start
docker compose build --no-cache social-auto-upload
docker compose up -d social-auto-upload

# 4. Verify health
curl -s -o /dev/null -w "%{http_code}" http://localhost:5409/
# Should return 200
```

## Rollback Command

```bash
# If new version has issues, rollback to previous
git checkout <previous-commit-hash>
docker compose build --no-cache social-auto-upload
docker compose up -d social-auto-upload
```

## Health Check Endpoint

```
GET /
```

Returns 200 if the backend is healthy.

## TikTok Verified URL Prefix Setup

TikTok requires domain ownership verification for PULL_FROM_URL. Configure:

```bash
# In .env or docker-compose.yml
SAU_TIKTOK_VERIFIED_URL_PREFIXES=https://cdn.example.com/,https://storage.example.com/
```

The prefix must match the beginning of your public URLs. For example:
- If your CDN URLs are `https://cdn.example.com/uploads/...`, set prefix to `https://cdn.example.com/`
- If using DO Spaces, set to your CDN URL like `https://pub-xxx.r2.dev/`

## TikTok Demo Video Checklist

See `docs/tiktok_review_demo_script.md` for the complete step-by-step demo script.

Key points to demonstrate:
1. Creator info fetched fresh (show loading state)
2. Creator nickname visible
3. Title entry
4. Privacy dropdown from creator_info (no default)
5. Interaction checkboxes unchecked by default
6. Disabled interactions greyed out
7. Photo posts hide Duet/Stitch
8. Commercial disclosure off by default
9. Your Brand / Branded Content behavior
10. Branded content blocks private visibility
11. English declaration text visible
12. Explicit consent checkbox
13. Video preview
14. No watermark for TikTok
15. Post-processing notice
16. Status polling visible

## Security Notes

- Never commit `.env` or secrets to the repository
- Cookie files are encrypted at rest when `SAU_COOKIE_ENCRYPTION_KEY` is set
- API tokens are validated with constant-time comparison
- OAuth tokens are never exposed to the frontend (removed from postMessage payloads)
- Upload keys are strictly validated (`uploads/<uuid>_<filename>` pattern)
- File paths are resolved safely to prevent path traversal
