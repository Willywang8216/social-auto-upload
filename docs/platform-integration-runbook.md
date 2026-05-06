# Platform integration runbook

This file is the practical setup guide for making the current `social-auto-upload` stack work with real platform credentials.

It covers:

- where secrets and tokens go
- exact callback URLs and webhook URLs used by this codebase
- what each platform app/integration should be configured for
- suggested answers for the usual review / onboarding questions
- which platforms are fully API/OAuth-driven vs still cookie/browser-driven in this repo

Important: this guide is based on the code currently in this repository as of 2026-05-06.

---

## 1. First, understand what is actually implemented

Not every platform in the product vision is in the same technical state.

### Fully structured / API-or-OAuth-driven in the current web UI

These have explicit structured account forms and current integration code:

- TikTok
- Reddit
- YouTube
- Facebook Pages
- Instagram Professional
- Threads
- Telegram
- Discord
- Google Sheets
- OneDrive / rclone
- LLM provider

### Supported, but currently not part of the newer structured API setup

These are still legacy or browser/cookie-driven in the current repo:

- X / Twitter
- Douyin
- Kuaishou
- Xiaohongshu
- Tencent / WeChat Channel uploader flow
- Bilibili / other legacy uploaders where applicable

### Important caveat platforms

- Patreon: current codebase treats this as content-generation / export territory, not a finished direct-publish API integration.
- X / Twitter: current codebase still uses the legacy browser/cookie uploader path for posting, not the new structured OAuth/token account-management flow.

So if your goal is “make the current system work today”, focus first on the structured platforms above.

---

## 2. Where to put credentials

### Repo root `.env`

This project auto-loads the repo root `.env` for backend, CLI, worker, and migration helpers.

Use:

```bash
cp .env.example .env
```

Then put real secrets in that root `.env` file.

### Important pattern: the UI usually stores the **env var name**, not the secret value

In `Account Management`, many fields are things like:

- `clientIdEnv`
- `clientSecretEnv`
- `refreshTokenEnv`
- `botTokenEnv`
- `accessTokenEnv`
- `webhookUrlEnv`

That means:

- in `.env`, store the real secret
- in the web UI, store the **name** of that env var

Example:

```env
TELEGRAM_BOT_TOKEN_BRAND_A=123456:ABC...
```

Then in the Telegram account form:

- `botTokenEnv = TELEGRAM_BOT_TOKEN_BRAND_A`

### Some fields may store values directly

A few structured flows can store direct values in account config, especially after OAuth connect:

- access tokens
- refresh tokens
- IDs returned from provider APIs

But for long-term maintainability, prefer env-backed secrets when the form supports it.

---

## 3. Current production URLs, callback URLs, and webhook URLs

The codebase currently assumes the production web domain is:

- `https://up.iamwillywang.com`

### Public app URLs

Use these when a platform asks for homepage / app domain / policy pages:

- App / homepage: `https://up.iamwillywang.com`
- Privacy policy: `https://up.iamwillywang.com/privacy`
- Terms: `https://up.iamwillywang.com/terms`

### OAuth callback URLs used by the current code

| Platform | Callback URL | Override env in backend helper | Platform-specific redirect env also worth setting |
|---|---|---|---|
| TikTok | `https://up.iamwillywang.com/oauth/tiktok/callback` | `SAU_TIKTOK_CALLBACK_URL` | `TIKTOK_REDIRECT_URI` |
| Reddit | `https://up.iamwillywang.com/oauth/reddit/callback` | `SAU_REDDIT_CALLBACK_URL` | `REDDIT_REDIRECT_URI` |
| YouTube | `https://up.iamwillywang.com/oauth/youtube/callback` | `SAU_YOUTUBE_CALLBACK_URL` | `YOUTUBE_REDIRECT_URI` |
| Meta (Facebook / Instagram) | `https://up.iamwillywang.com/oauth/meta/callback` | `SAU_META_CALLBACK_URL` | `META_REDIRECT_URI` |
| Threads | `https://up.iamwillywang.com/oauth/threads/callback` | `SAU_THREADS_CALLBACK_URL` | `THREADS_REDIRECT_URI` |

### Webhook URLs

| Platform | Webhook URL | Notes |
|---|---|---|
| TikTok | `https://up.iamwillywang.com/webhooks/tiktok` | GET challenge supported, POST signature verification supported |

### Very important if you deploy on a different domain

If you do **not** use `up.iamwillywang.com`, then:

1. set the matching `SAU_*_CALLBACK_URL` env vars
2. also set the matching platform `*_REDIRECT_URI` env vars to the same value
3. register those exact URLs in each platform console
4. if using TikTok webhooks on another domain, your platform-side webhook must point to:
   - `https://YOUR_DOMAIN/webhooks/tiktok`

Note: the route exists generically, but some status UI text in the code is still opinionated toward the production domain. If you fully white-label to another domain, you may want to update those display strings too.

---

## 4. Recommended `.env` skeleton

This is the practical starting point for the current stack:

```env
# Core runtime
TZ=Asia/Taipei
PYTHONUNBUFFERED=1

# LLM
SAU_LLM_API_BASE_URL=https://your-llm-endpoint.example.com
SAU_LLM_API_KEY=sk-...

# Google Sheets
SAU_GOOGLE_SERVICE_ACCOUNT_FILE=./secrets/google-service-account.json
# or
# SAU_GOOGLE_SERVICE_ACCOUNT_JSON={...json...}

# OneDrive / rclone
SAU_DEFAULT_RCLONE_REMOTE=Onedrive-Yahooforsub-Tao
SAU_DEFAULT_RCLONE_PATH=Scripts-ssh-ssl-keys/SocialUpload
SAU_PUBLIC_URL_TEMPLATE=

# TikTok
TIKTOK_CLIENT_KEY=...
TIKTOK_CLIENT_SECRET=...
TIKTOK_REDIRECT_URI=https://up.iamwillywang.com/oauth/tiktok/callback
SAU_TIKTOK_CALLBACK_URL=https://up.iamwillywang.com/oauth/tiktok/callback

# Reddit
REDDIT_CLIENT_ID=...
REDDIT_CLIENT_SECRET=...
REDDIT_REDIRECT_URI=https://up.iamwillywang.com/oauth/reddit/callback
SAU_REDDIT_CALLBACK_URL=https://up.iamwillywang.com/oauth/reddit/callback
REDDIT_USER_AGENT=social-auto-upload/1.0 (contact: you@example.com)

# YouTube / Google
YT_CLIENT_ID=...
YT_CLIENT_SECRET=...
YOUTUBE_REDIRECT_URI=https://up.iamwillywang.com/oauth/youtube/callback
SAU_YOUTUBE_CALLBACK_URL=https://up.iamwillywang.com/oauth/youtube/callback

# Meta / Facebook / Instagram
META_APP_ID=...
META_APP_SECRET=...
META_REDIRECT_URI=https://up.iamwillywang.com/oauth/meta/callback
SAU_META_CALLBACK_URL=https://up.iamwillywang.com/oauth/meta/callback

# Threads
THREADS_APP_ID=...
THREADS_APP_SECRET=...
THREADS_REDIRECT_URI=https://up.iamwillywang.com/oauth/threads/callback
SAU_THREADS_CALLBACK_URL=https://up.iamwillywang.com/oauth/threads/callback

# Telegram
TELEGRAM_BOT_TOKEN_BRAND_A=...

# Discord
DISCORD_WEBHOOK_URL_BRAND_A=https://discord.com/api/webhooks/...
```

Notes:

- For structured account forms, you can name brand-specific vars however you want.
- The account form should hold the env **key name**, not the secret value.
- Your current personal `.env` already includes some Telegram / Reddit / X values; keep using root `.env` as the place for those.

---

## 5. Shared “what is this app?” answers you can reuse

These are the answers I recommend reusing across provider forms, with small edits per platform.

### Short app description

> Social Auto Upload is a web-based content operations tool for authorized brand and creator teams. It helps operators upload their own media, generate platform-specific copy, review drafts, and publish or queue posts to connected social accounts they directly manage.

### Long app description

> Social Auto Upload is an internal publishing and workflow tool for content creators, agencies, and brand operators. The system allows an authenticated operator to upload media, organize assets into campaign groups, connect owned social accounts, generate channel-specific captions and descriptions, export planning sheets, and publish or queue content to supported social platforms. The application is not used for scraping third-party data, bulk engagement automation, follower manipulation, or reposting unauthorized content. It is used only to manage and publish content for accounts that the operator owns or is authorized to manage.

### What the app is used for

> The app is used to publish first-party content to accounts owned or managed by the user’s organization. Typical actions include connecting the organization’s account, validating the connection, uploading creator-owned media, preparing a platform-specific caption, and publishing or scheduling the post.

### Who uses it

> The app is used by internal operators, creators, or agency staff who already manage the connected social accounts.

### What data is accessed

> The app accesses only the minimum data needed to authenticate the connected account, validate account ownership, and publish content. Depending on the platform, this may include basic account identity metadata such as page name, username, channel title, account ID, access token status, and publishing permissions.

### What the app does **not** do

> The app does not scrape public user data, does not resell user data, does not perform spam or engagement manipulation, and does not post to accounts that the operator has not explicitly connected and authorized.

### Data retention answer

> Tokens and account metadata are stored only to support account connection, health checks, and publishing operations. Operators can disconnect the account inside the app and can also revoke access from the provider dashboard at any time.

### Deletion / revocation answer

> Users can revoke access in the provider’s developer/security settings and can remove the connected account from the application. If a platform asks for a public deletion URL, the current repo has public privacy and terms routes, but it does not yet expose a dedicated deletion callback endpoint. If the provider requires one, create a dedicated public deletion-instructions page before submission.

### Privacy policy URL answer

- `https://up.iamwillywang.com/privacy`

### Terms URL answer

- `https://up.iamwillywang.com/terms`

### Support contact answer

Use a real monitored email address, for example:

- `support@yourdomain.com`

Do **not** submit a throwaway address for platform review.

---

## 6. Platform-by-platform setup and suggested answers

## 6.1 TikTok

### What the current code uses

Products/features actively used by this repo:

- Login Kit for Web
- Content Posting API
- Webhooks

Scopes actively used:

- `user.info.basic`
- `video.publish`

Exact URLs:

- Redirect URI: `https://up.iamwillywang.com/oauth/tiktok/callback`
- Webhook URL: `https://up.iamwillywang.com/webhooks/tiktok`

Required envs:

- `TIKTOK_CLIENT_KEY`
- `TIKTOK_CLIENT_SECRET`
- recommended also:
  - `TIKTOK_REDIRECT_URI`
  - `SAU_TIKTOK_CALLBACK_URL`

### What TikTok will usually ask

#### App website / domain
Use:

- `https://up.iamwillywang.com`

#### Redirect URI
Use exactly:

- `https://up.iamwillywang.com/oauth/tiktok/callback`

#### Webhook callback URL
Use exactly:

- `https://up.iamwillywang.com/webhooks/tiktok`

#### How do you intend to use the app?
Suggested answer:

> We use this application to let authenticated operators connect TikTok accounts they directly manage, upload creator-owned media, prepare post metadata, and publish or submit content through TikTok’s official Content Posting API. The application is used only for first-party publishing workflows and account management.

#### Why do you need `user.info.basic`?

> We use `user.info.basic` to confirm the identity of the connected TikTok account, display the connected creator’s basic profile information in the account-management UI, and ensure that publishing actions are tied to the correct authorized account.

#### Why do you need `video.publish`?

> We use `video.publish` to submit creator-authorized content through TikTok’s official Content Posting API after the operator has uploaded media and explicitly initiated the publish action in the app.

#### Review / screencast summary
Use the dedicated file:

- `docs/tiktok-app-review-demo.md`

That file already matches the current implementation and production domain.

### Technical notes

- TikTok webhook signature verification in this code uses the TikTok client secret as the HMAC secret.
- The webhook handler supports:
  - GET challenge response
  - POST signature verification
- TikTok Content Posting API currently prohibits branded/promotional watermark behavior in ways that conflict with their policy. Be careful with your TikTok profile watermark settings.

---

## 6.2 Reddit

### What the current code uses

Reddit app type should be:

- **web app**

Scopes used:

- `identity`
- `submit`
- `read`

Required envs:

- `REDDIT_CLIENT_ID`
- `REDDIT_CLIENT_SECRET`
- recommended also:
  - `REDDIT_REDIRECT_URI`
  - `SAU_REDDIT_CALLBACK_URL`
  - `REDDIT_USER_AGENT`

Redirect URI:

- `https://up.iamwillywang.com/oauth/reddit/callback`

### Structured account form fields

Typical values in Account Management:

- `subreddits`
- `clientIdEnv`
- `clientSecretEnv`
- `refreshTokenEnv`
- optional `userAgent`

### Suggested answers

#### About this app

> This is an internal publishing workflow tool used by authorized operators to submit first-party content to subreddits they are allowed to post in. The app does not scrape Reddit content or automate public engagement. It is used only to authenticate a Reddit account, validate identity, and submit content to selected subreddits.

#### Why do you need `identity`?

> To verify which Reddit account is connected and display the connected username inside the app.

#### Why do you need `submit`?

> To submit posts to explicitly selected subreddits on behalf of the authenticated user after the user has initiated the post in our application.

#### Why do you need `read`?

> To validate basic account state and support minimal subreddit/account checks needed for the publishing workflow.

### Practical setup steps

1. Log in to the posting Reddit account.
2. Open `https://www.reddit.com/prefs/apps`.
3. Create a **web app**.
4. Set redirect URI to:
   - `https://up.iamwillywang.com/oauth/reddit/callback`
5. Copy client ID and client secret.
6. Complete an OAuth flow with `duration=permanent` so you get a refresh token.
7. Put the real secrets in `.env`.
8. In the account form, put the env var names into the `clientIdEnv`, `clientSecretEnv`, and `refreshTokenEnv` fields.

---

## 6.3 YouTube / Google

### What the current code uses

OAuth scopes:

- `https://www.googleapis.com/auth/youtube.upload`
- `https://www.googleapis.com/auth/youtube.readonly`

Required envs:

- `YT_CLIENT_ID`
- `YT_CLIENT_SECRET`
- recommended also:
  - `YOUTUBE_REDIRECT_URI`
  - `SAU_YOUTUBE_CALLBACK_URL`

Redirect URI:

- `https://up.iamwillywang.com/oauth/youtube/callback`

### Suggested answers for Google OAuth consent / verification

#### App description

> This application allows authorized users to connect YouTube channels they manage, validate the connected channel identity, upload creator-owned videos, optionally assign uploaded videos to playlists, and publish those videos through the official YouTube Data API.

#### Why do you need `youtube.upload`?

> We use `youtube.upload` to upload videos that the authenticated user has explicitly chosen to publish from within the application.

#### Why do you need `youtube.readonly`?

> We use `youtube.readonly` to retrieve the authenticated user’s channel identity and metadata so the app can validate the connection and display the correct connected channel in the UI.

#### Test instructions for reviewer

> Sign in with a test Google account that has access to a test YouTube channel. Connect the channel in Account Management, confirm the channel title is shown in the UI, then upload and publish a test video through the application.

### Practical setup steps

1. Create/select a Google Cloud project.
2. Enable:
   - **YouTube Data API v3**
3. Configure OAuth consent screen.
4. Create an OAuth client for a web application.
5. Add authorized redirect URI:
   - `https://up.iamwillywang.com/oauth/youtube/callback`
6. Put client ID/secret in `.env`.
7. Use the in-app connect flow or OAuth flow to obtain refreshable tokens.
8. Store env names in account form if you want env-backed secrets.

### Notes

- If Google asks for app verification because of sensitive scopes, give them a short screencast showing:
  - connect YouTube account
  - channel title appearing in UI
  - upload/publish action

---

## 6.4 Meta / Facebook Pages

### What the current code uses

Default scopes for Facebook Pages flow:

- `pages_show_list`
- `pages_manage_posts`
- `pages_read_engagement`
- `pages_manage_metadata`
- `business_management`

Required envs:

- `META_APP_ID`
- `META_APP_SECRET`
- recommended also:
  - `META_REDIRECT_URI`
  - `SAU_META_CALLBACK_URL`

Redirect URI:

- `https://up.iamwillywang.com/oauth/meta/callback`

### Suggested answers

#### What does the app do?

> The app is an internal publishing workflow tool for authorized page operators. It allows a page admin to connect the Facebook Page they manage, validate the page identity, and publish first-party content to that page through Meta’s official APIs.

#### Why `pages_show_list`?

> To list the Pages the authenticated user manages and allow the user to select the correct Page for connection.

#### Why `pages_manage_posts`?

> To publish posts to the authenticated user’s selected Facebook Page after the user initiates the publish action.

#### Why `pages_read_engagement`?

> To validate the connected Page and retrieve minimal page metadata needed to confirm the correct page was connected.

#### Why `pages_manage_metadata`?

> To support stable Page integration behavior and access required by page-management workflows.

#### Why `business_management`?

> To support the page/business asset relationship required by Meta’s business publishing flows.

### Practical setup steps

1. Create a Meta app.
2. Add the Facebook-related login/publishing products relevant to your dashboard flow.
3. Add redirect URI:
   - `https://up.iamwillywang.com/oauth/meta/callback`
4. Complete Meta OAuth with a user who manages the target Page.
5. The app exchanges for a long-lived Meta user token and then fetches managed Pages.
6. The connected account stores:
   - page ID
   - page name
   - page access token
   - long-lived Meta user token for re-syncing page credentials later

### Important note

The current code does **not** just rely on a raw page token pasted by hand anymore. The stronger path is the in-app Meta connect flow, because that also stores the long-lived Meta user token used for future credential re-sync.

---

## 6.5 Meta / Instagram Professional

### What the current code uses

Default Instagram scopes:

- `pages_show_list`
- `instagram_basic`
- `instagram_content_publish`
- `business_management`

Required envs:

- same Meta app credentials as Facebook
- `META_APP_ID`
- `META_APP_SECRET`
- `META_REDIRECT_URI`
- `SAU_META_CALLBACK_URL`

Redirect URI:

- `https://up.iamwillywang.com/oauth/meta/callback`

### Suggested answers

#### What does the app do?

> The app allows authorized operators to connect an Instagram Professional account that is linked to a managed Facebook Page, validate the connected account identity, and publish first-party media content through Meta’s official Instagram publishing APIs.

#### Why `instagram_basic`?

> To identify and validate the connected Instagram Professional account in the UI.

#### Why `instagram_content_publish`?

> To publish media posts that the user explicitly prepared and approved within the application.

#### Why `pages_show_list` and `business_management`?

> Because the Instagram Professional account is resolved through the authenticated user’s managed Facebook Page / business relationship in the official Meta integration flow.

### Practical setup steps

1. Use an Instagram Professional account.
2. Ensure it is linked correctly in the Meta business/page setup.
3. Run the in-app Meta connect flow.
4. The app resolves the Page + Instagram business account pair and stores:
   - `pageId`
   - `igUserId`
   - `instagramUserName`
   - page-backed access token
   - long-lived Meta user token

### Important note

Current Meta docs changed significantly around Instagram login/product setup in 2024–2025. Use the current Meta dashboard/product wording, but keep the redirect URI and scopes aligned with the code above.

---

## 6.6 Threads

### What the current code uses

Scopes:

- `threads_basic`
- `threads_content_publish`

Required envs:

- `THREADS_APP_ID` or `THREADS_CLIENT_ID`
- `THREADS_APP_SECRET` or `THREADS_CLIENT_SECRET`
- `THREADS_REDIRECT_URI`
- `SAU_THREADS_CALLBACK_URL`

Redirect URI:

- `https://up.iamwillywang.com/oauth/threads/callback`

### Suggested answers

#### What does the app do?

> The app is an internal content-publishing workflow tool that allows authorized users to connect a Threads account they manage, validate account identity, and publish first-party text, image, or video content through the official Threads API.

#### Why `threads_basic`?

> To identify and validate the connected Threads account and show the correct username/account in the UI.

#### Why `threads_content_publish`?

> To publish user-approved first-party content from the application to the authenticated Threads account.

### Practical setup steps

1. Create a Meta app with the **Threads** use case / product.
2. Add redirect URI:
   - `https://up.iamwillywang.com/oauth/threads/callback`
3. Complete OAuth through the in-app connect flow.
4. The app exchanges for a long-lived Threads token and stores identity metadata.

---

## 6.7 Telegram

### What the current code uses

No OAuth app review flow is needed.

Typical structured account fields:

- `chatId`
- `botTokenEnv`
- optional `parseMode`
- `silent`
- `disableWebPreview`

Provider-side secret typically stored in `.env`:

- `TELEGRAM_BOT_TOKEN_BRAND_A=...`

### Practical setup steps

1. Open Telegram.
2. Chat with **@BotFather**.
3. Run `/newbot`.
4. Create:
   - bot display name
   - bot username
5. BotFather returns the bot token.
6. Put the token in root `.env`.
7. In the account form:
   - `botTokenEnv = TELEGRAM_BOT_TOKEN_BRAND_A`
   - `chatId = @channel_name` or a group/channel ID
8. If posting to a channel, add the bot as an admin.

### Suggested answers if BotFather / bot profile text asks for descriptions

#### Bot description

> Internal publishing bot used by our content operations system to send approved campaign posts to our own Telegram channels and groups.

#### About text

> Publishes approved first-party content from our content management workflow to authorized Telegram destinations.

No separate platform app review is normally required here.

---

## 6.8 Discord

### What the current code uses

No OAuth app review flow is needed for the current integration.

Current implementation uses:

- incoming webhook URL

Structured account field:

- `webhookUrlEnv`

Suggested env:

```env
DISCORD_WEBHOOK_URL_BRAND_A=https://discord.com/api/webhooks/...
```

### Practical setup steps

1. Open the target Discord server.
2. Open channel settings.
3. Open **Integrations -> Webhooks**.
4. Create webhook.
5. Copy webhook URL.
6. Put it in root `.env`.
7. In the account form:
   - `webhookUrlEnv = DISCORD_WEBHOOK_URL_BRAND_A`

### Suggested webhook name

> Social Auto Upload

No separate app-review answers are normally needed for this webhook-only path.

---

## 6.9 Google Sheets service account

### What the current code uses

The app exports campaign sheet rows via a Google service account.

Supported envs:

- `SAU_GOOGLE_SERVICE_ACCOUNT_FILE`
- `SAU_GOOGLE_SERVICE_ACCOUNT_JSON`

### Practical setup steps

1. Create/select Google Cloud project.
2. Enable **Google Sheets API**.
3. Create service account.
4. Create JSON key.
5. Save it somewhere safe, for example:
   - `./secrets/google-service-account.json`
6. Set:

```env
SAU_GOOGLE_SERVICE_ACCOUNT_FILE=./secrets/google-service-account.json
```

7. If writing to an existing spreadsheet, share that spreadsheet with the service account email.

### Important note

This is not an OAuth user-consent flow. It is a server-to-server credential.

---

## 6.10 OneDrive / rclone

### What the current code uses

Current envs:

- `SAU_DEFAULT_RCLONE_REMOTE`
- `SAU_DEFAULT_RCLONE_PATH`
- optional `SAU_PUBLIC_URL_TEMPLATE`

### Practical setup steps

1. Install `rclone` on the machine running backend/worker.
2. Configure a OneDrive remote.
3. Test:

```bash
rclone copyto /path/to/test.jpg Onedrive-Yahooforsub-Tao:Scripts-ssh-ssl-keys/SocialUpload/test.jpg
```

4. Test public link creation:

```bash
rclone link Onedrive-Yahooforsub-Tao:Scripts-ssh-ssl-keys/SocialUpload/test.jpg --onedrive-link-scope anonymous --onedrive-link-type view
```

5. If that fails because your tenant blocks anonymous links, either:
   - enable anonymous/public sharing for that drive, or
   - provide `SAU_PUBLIC_URL_TEMPLATE` if you already have another public-serving layer in front of the files

### Current recommended envs

```env
SAU_DEFAULT_RCLONE_REMOTE=Onedrive-Yahooforsub-Tao
SAU_DEFAULT_RCLONE_PATH=Scripts-ssh-ssl-keys/SocialUpload
```

---

## 6.11 LLM provider

### What the current code uses

Required envs:

- `SAU_LLM_API_BASE_URL`
- `SAU_LLM_API_KEY`

Used for:

- transcript generation
- platform-specific copy generation

### What your provider must support

- chat completion
- audio transcription or equivalent transcription endpoint your integration expects

Example:

```env
SAU_LLM_API_BASE_URL=https://llmapi.iamwillywang.com
SAU_LLM_API_KEY=...
```

---

## 6.12 X / Twitter

### Important current-state note

Your current repo still treats X/Twitter as a **legacy browser/cookie-driven publisher**, not as a finished structured OAuth/API integration in Account Management.

That means:

- the structured account-management OAuth/token system does **not** currently consume your `X_*` API credentials
- the posting implementation still uses the browser automation uploader under:
  - `uploader/twitter_uploader/main.py`

### What this means for you

- Your existing `X_API_KEY`, `X_CLIENT_ID`, `X_ACCESS_TOKEN`, etc. can remain in `.env`, but they are not the credentials that make the current X posting path work.
- To make X work **today** in this repo, you still need the legacy/cookie workflow.

### Recommended wording in your internal guide

> X/Twitter is currently supported by browser/cookie automation rather than the newer structured OAuth/token integration. The current web UI and backend do not yet use the `X_*` API keys for direct structured publishing.

This is important so nobody wastes time trying to “fix” X by only filling API keys.

---

## 6.13 Patreon

### Important current-state note

Patreon is not a finished direct-publish platform in this repo.

Treat it as:

- generated-content / export / manual-post workflow

Do **not** assume that filling Patreon API credentials alone will make end-to-end posting work today.

---

## 6.14 Legacy domestic/browser platforms

These are still mainly cookie/browser-login style in the current repo:

- Douyin
- Kuaishou
- Xiaohongshu
- Tencent / WeChat Channel-related uploader flow
- similar legacy uploaders

These generally do **not** need developer-app callback/webhook setup in the same way as TikTok/Meta/Google/Reddit.

Instead, they use:

- QR login
- cookie files
- browser automation

So for those, the setup question is usually not “what OAuth callback URL do I register?” but “how do I complete login and maintain the cookie/session file?”.

---

## 7. Suggested platform-review answer bank

Use these as starting points and edit only where needed.

### “What is your app about?”

> Social Auto Upload is a content operations and publishing tool for authorized brand and creator teams. It helps users connect accounts they manage, upload first-party media, prepare captions and metadata, review drafts, and publish or schedule content to their own social channels.

### “How do you intend to use the data?”

> We use the data only to authenticate the connected account, validate account ownership, display the connected account identity inside the app, and publish user-approved first-party content to that account.

### “Will you share or sell user data?”

> No. The application does not sell, resell, or broker user data.

### “Does the app post automatically?”

> The app publishes only after an authenticated operator explicitly prepares and submits content within the application. It is used for managed publishing workflows, not for unauthorized posting or engagement automation.

### “Who are the end users?”

> Internal operators, creators, or agency staff who already manage the connected accounts.

### “How can reviewers test this?”

> Reviewers can connect a test account inside the web UI, confirm the connected account identity appears in Account Management, upload or prepare test content in the UI, and then trigger a publish or draft action using the officially requested scope(s).

### “What content is being posted?”

> Only first-party content that belongs to or is authorized by the connected account owner or the organization operating that account.

---

## 8. Recommended setup order

If you want the fastest path to a working system, do this order:

1. Root `.env` and backend startup sanity check
2. LLM provider
3. Google Sheets service account
4. OneDrive / rclone public link flow
5. Telegram bot
6. Reddit
7. YouTube
8. Meta app for Facebook / Instagram
9. Threads
10. TikTok
11. Discord webhook
12. Legacy cookie-based platforms
13. X/Twitter legacy cookie flow

---

## 9. Final practical checklist

Before you start live posting, verify all of these:

- root `.env` exists and is not committed
- backend can read `.env`
- Google service account JSON path is valid
- `rclone copyto` works
- `rclone link` works, or `SAU_PUBLIC_URL_TEMPLATE` is configured
- each provider console has the exact redirect URI registered
- TikTok webhook points to the right public URL
- privacy policy and terms URLs are public and reachable
- structured account forms use **env var names**, not raw secrets, where supported
- Meta / Threads / TikTok connections are done through the in-app connect buttons where available
- X/Twitter expectations are aligned with the current legacy cookie implementation

---

## 10. Short answer to “where do I put what?”

- Put real secrets in root `.env`
- Put Google service account JSON in a safe local path and reference it via `SAU_GOOGLE_SERVICE_ACCOUNT_FILE`
- In Account Management, fields ending in `Env` should usually contain the env var **name**
- Register the callback URLs from Section 3 exactly in each provider console
- Use the shared production domain `https://up.iamwillywang.com` unless you are intentionally overriding it everywhere

