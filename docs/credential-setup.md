# Credential setup guide

This project now auto-loads the repo root `.env` file for the backend, CLI,
worker, and Alembic helpers. A typical local setup is:

```bash
cp .env.example .env
```

Then replace the dummy values with real credentials.

## General rules

- The **variable name can be anything**; the structured account form stores the env key name.
- The examples below use names like `*_BRAND_A` so you can keep multiple brand/account credentials side by side.
- Do **not** commit your real `.env` file.
- TikTok's official Content Posting API does **not** allow branded/promotional watermarks on uploaded content.

## 1. LLM API

Used for transcript generation and platform-specific copy generation.

Required envs:

- `SAU_LLM_API_BASE_URL`
- `SAU_LLM_API_KEY`

How to get it:

- Use any OpenAI-compatible provider or your own gateway.
- Confirm it supports:
  - chat completions
  - audio transcription

Example:

```env
SAU_LLM_API_BASE_URL=https://your-llm-endpoint.example.com
SAU_LLM_API_KEY=sk-...
```

## 2. Google Sheets service account

Used for campaign sheet export.

Required envs:

- `SAU_GOOGLE_SERVICE_ACCOUNT_FILE` or `SAU_GOOGLE_SERVICE_ACCOUNT_JSON`

How to get it:

1. Open Google Cloud Console.
2. Create or select a project.
3. Enable **Google Sheets API**.
4. Go to **IAM & Admin -> Service Accounts**.
5. Create a service account.
6. Create a JSON key and download it.
7. Point `SAU_GOOGLE_SERVICE_ACCOUNT_FILE` at that JSON file.
8. If you are writing into an existing spreadsheet, share the sheet with the service-account email.

Example:

```env
SAU_GOOGLE_SERVICE_ACCOUNT_FILE=./secrets/google-service-account.json
```

## 3. Telegram bot posting

Used for Telegram channel/group posting.

Typical structured account fields:

- `chatId`
- `botTokenEnv`

How to get the bot token:

1. Open Telegram and chat with **@BotFather**.
2. Run `/newbot`.
3. Follow the prompts.
4. Copy the bot token.

How to get the chat ID:

- For a public channel: use `@channel_name`
- For a group/supergroup: add the bot, send a message, then inspect updates or use a bot like `@userinfobot`.
- If posting to a channel, the bot must usually be added as an admin.

Example:

```env
TELEGRAM_BOT_TOKEN_BRAND_A=123456:ABCDEF...
```

## 4. Reddit

Used for subreddit posting.

Typical structured account fields:

- `subreddits`
- `clientIdEnv`
- `clientSecretEnv`
- `refreshTokenEnv`
- optional `userAgent`

How to get them:

1. Log in to Reddit with the posting account.
2. Open `https://www.reddit.com/prefs/apps`.
3. Create an app of type **web app**.
4. Set a redirect URI such as `http://localhost:8080/callback`.
5. Copy:
   - client ID
   - client secret
6. Run an OAuth authorization flow with `duration=permanent` and `scope=submit identity read`.
7. Exchange the code for a refresh token.

You can use your own small OAuth helper script or Postman.

Example:

```env
REDDIT_CLIENT_ID_BRAND_A=...
REDDIT_CLIENT_SECRET_BRAND_A=...
REDDIT_REFRESH_TOKEN_BRAND_A=...
REDDIT_USER_AGENT_BRAND_A=social-auto-upload/0.1 (brand-a)
```

## 5. YouTube

Used for video upload via YouTube Data API.

Typical structured account fields:

- `channelId`
- `clientIdEnv`
- `clientSecretEnv`
- `refreshTokenEnv`
- optional `playlistId`

How to get them:

1. Open Google Cloud Console.
2. Enable **YouTube Data API v3**.
3. Create OAuth client credentials.
4. Add a redirect URI such as `http://localhost:8080/callback`.
5. Use OAuth 2.0 Playground or your own auth helper to request YouTube scopes.
6. Exchange the auth code for:
   - access token
   - refresh token
7. Keep the refresh token in `.env`.
8. Get your channel ID from YouTube Studio or the Data API.

Example:

```env
YT_CLIENT_ID_BRAND_A=...
YT_CLIENT_SECRET_BRAND_A=...
YT_REFRESH_TOKEN_BRAND_A=...
```

## 6. Facebook Pages

Used for page posting.

Typical structured account fields:

- `pageId`
- `accessTokenEnv`

How to get them:

1. Create a Meta developer app.
2. Add the required Graph API / Facebook Login products for your integration.
3. Obtain a user access token for a user who manages the Page.
4. Exchange it for a long-lived token if appropriate.
5. Query the user's managed pages and page access tokens.
6. Store the **page access token** in `.env`.
7. Copy the Page ID.

Example:

```env
FB_PAGE_TOKEN_BRAND_A=...
```

## 7. Instagram Professional

Used for Instagram content publishing.

Typical structured account fields:

- `igUserId`
- `accessTokenEnv`

How to get them:

1. Set up a Meta developer app.
2. Use an Instagram Professional account connected to the relevant business setup.
3. Obtain an access token with the required publishing permissions.
4. Query the connected Instagram account ID (`igUserId`).
5. Store the access token in `.env`.

Example:

```env
IG_ACCESS_TOKEN_BRAND_A=...
```

## 8. Threads

Used for Threads posting.

Typical structured account fields:

- `threadUserId`
- `accessTokenEnv`

How to get them:

1. Create a Meta app with the **Threads** use case/product.
2. Complete the Threads API app setup and approval requirements.
3. Obtain a Threads user access token via the approved OAuth flow.
4. Query the Threads user ID.
5. Store the access token in `.env`.

Current API host is `graph.threads.net`.

Example:

```env
THREADS_ACCESS_TOKEN_BRAND_A=...
```

## 9. TikTok Content Posting API

Used for direct post or draft-mode media posting.

Typical structured account fields:

- `accessTokenEnv`
- `publishMode`
- optional privacy / comment / duet / stitch flags

How to get them:

1. Create a TikTok developer app.
2. Add **Content Posting API**.
3. Enable **Direct Post** if you want direct publishing.
4. Submit the app for review and get approval for `video.publish`.
5. Complete the TikTok OAuth flow for the target creator account.
6. Store the TikTok user access token in `.env`.

Important:

- The app should query creator info before posting.
- TikTok requires publicly accessible media URLs for the direct API flow used here.
- TikTok prohibits unwanted promotional branding/watermarks in posted media.

Example:

```env
TIKTOK_ACCESS_TOKEN_BRAND_A=...
```

## 10. Discord webhook

Used for Discord channel posting.

Typical structured account fields:

- `webhookUrlEnv`

How to get it:

1. Open the target Discord server.
2. Go to the channel settings.
3. Open **Integrations -> Webhooks**.
4. Create a webhook.
5. Copy the webhook URL.
6. Store it in `.env`.

Example:

```env
DISCORD_WEBHOOK_URL_BRAND_A=https://discord.com/api/webhooks/...
```

## 11. OneDrive / rclone

Used for public media URLs before posting.

Required envs:

- `SAU_DEFAULT_RCLONE_REMOTE`
- `SAU_DEFAULT_RCLONE_PATH`
- optional `SAU_PUBLIC_URL_TEMPLATE`

How to get it working:

1. Install and configure `rclone`.
2. Create a OneDrive remote.
3. Confirm `rclone copyto` works.
4. Confirm `rclone link` works for your tenant if you want automatic public URLs.
5. If anonymous links are blocked by your tenant, use `SAU_PUBLIC_URL_TEMPLATE` only if you already have another public-serving layer.

## Suggested first setup order

If you want the fastest route to a working campaign pipeline, do these first:

1. `SAU_LLM_API_BASE_URL` / `SAU_LLM_API_KEY`
2. Google service account JSON
3. OneDrive / rclone
4. Telegram bot token
5. Reddit OAuth
6. YouTube OAuth
7. Meta tokens (Facebook / Instagram / Threads)
8. TikTok Content Posting API
9. Discord webhook
