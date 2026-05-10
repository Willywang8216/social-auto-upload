# Step-by-step setup checklist

Use this file when you want the shortest path from an empty `.env` to a working system.

For detailed platform explanations, app-review wording, and callback/webhook reference values, also see:

- `docs/platform-integration-runbook.md`

---

## Goal

By the end of this checklist, you should have:

- backend running
- frontend running
- Google Sheets export working
- OneDrive / rclone upload working
- LLM generation working
- at least one connected social platform account
- a successful draft/prepare flow
- a successful publish test on at least one platform

---

## Phase 0 — Prerequisites

Do these before touching `.env`.

### 0.1 Install required software

- [ ] Python environment for the backend
- [ ] Node.js + npm for the frontend
- [ ] `rclone`
- [ ] Playwright browser runtime
- [ ] Chromium browser for Playwright

### 0.2 Confirm production/public domain decision

Choose one:

- [ ] I will use `https://up.iamwillywang.com`
- [ ] I will use another public HTTPS domain

If using another domain, write it down now:

- [ ] My public app URL is: `https://____________________`

You will need this exact value for provider callbacks.

### 0.3 Confirm support email

- [ ] Decide on a real support email address: `____________________`

### 0.4 Confirm privacy + terms pages are publicly reachable

Current expected public URLs:

- [ ] Privacy: `https://up.iamwillywang.com/privacy`
- [ ] Terms: `https://up.iamwillywang.com/terms`

If using another public domain, confirm the equivalent URLs exist.

---

## Phase 1 — Create `.env`

### 1.1 Create the file

- [ ] Run:

```bash
cp .env.example .env
```

If `.env.example` is missing or incomplete, create `.env` manually.

### 1.2 Add core runtime values

Put these in root `.env`:

```env
TZ=Asia/Taipei
PYTHONUNBUFFERED=1
LOCAL_CHROME_HEADLESS=True
DEBUG_MODE=False
```

Checklist:

- [ ] root `.env` exists
- [ ] `.env` is not committed to git

---

## Phase 2 — Install dependencies and boot the app locally

### 2.1 Backend dependencies

- [ ] Run:

```bash
uv sync --extra web
```

Or, if you are not using `uv`:

```bash
pip install -e '.[web]'
```

### 2.2 Playwright browser install

- [ ] Run:

```bash
playwright install chromium
```

### 2.3 Frontend dependencies

- [ ] Run:

```bash
cd sau_frontend
npm install
cd ..
```

### 2.4 Initialize database

- [ ] Run:

```bash
python db/createTable.py
```

### 2.5 Start backend

- [ ] Run:

```bash
python sau_backend.py
```

Expected:

- [ ] backend responds on `http://localhost:5409`

### 2.6 Start frontend

In another terminal:

- [ ] Run:

```bash
cd sau_frontend
npm run dev
```

Expected:

- [ ] frontend opens on `http://localhost:5173`

### 2.7 Basic app sanity check

- [ ] Open frontend
- [ ] Log in if your deployment requires it
- [ ] Confirm Dashboard loads
- [ ] Confirm Account Management loads
- [ ] Confirm Publish Center loads

Do not continue until this works.

---

## Phase 3 — Configure LLM first

The campaign preparation flow depends on this.

### 3.1 Obtain LLM credentials

You need a provider that supports:

- [ ] chat completions
- [ ] audio transcription

### 3.2 Put the values into `.env`

```env
SAU_LLM_API_BASE_URL=https://your-llm-endpoint.example.com
SAU_LLM_API_KEY=sk-...
```

Checklist:

- [ ] `SAU_LLM_API_BASE_URL` set
- [ ] `SAU_LLM_API_KEY` set

### 3.3 Verify mentally what this is used for

- [ ] transcript generation
- [ ] caption/post generation

---

## Phase 4 — Configure Google Sheets

### 4.1 Create Google Cloud project

- [ ] Open Google Cloud Console
- [ ] Create/select a project
- [ ] Enable **Google Sheets API**

### 4.2 Create service account

- [ ] Go to **IAM & Admin -> Service Accounts**
- [ ] Create service account
- [ ] Create JSON key
- [ ] Download JSON key

### 4.3 Store the service account key

Recommended:

- [ ] Create a safe local file such as:
  - `./secrets/google-service-account.json`

### 4.4 Put path into `.env`

```env
SAU_GOOGLE_SERVICE_ACCOUNT_FILE=./secrets/google-service-account.json
```

Or use inline JSON if you must:

```env
SAU_GOOGLE_SERVICE_ACCOUNT_JSON={...}
```

Checklist:

- [ ] exactly one credential method is set
- [ ] JSON file exists if using file mode

---

## Phase 5 — Configure OneDrive / rclone

This is required for publicly accessible media URLs.

### 5.1 Configure rclone remote

- [ ] Install `rclone`
- [ ] Create OneDrive remote
- [ ] Confirm the remote name you want to use

Your current preferred values appear to be:

```env
SAU_DEFAULT_RCLONE_REMOTE=Onedrive-Yahooforsub-Tao
SAU_DEFAULT_RCLONE_PATH=Scripts-ssh-ssl-keys/SocialUpload
```

### 5.2 Put them into `.env`

```env
SAU_DEFAULT_RCLONE_REMOTE=Onedrive-Yahooforsub-Tao
SAU_DEFAULT_RCLONE_PATH=Scripts-ssh-ssl-keys/SocialUpload
SAU_PUBLIC_URL_TEMPLATE=
```

### 5.3 Verify file upload works

- [ ] Run a manual test:

```bash
rclone copyto /path/to/test.jpg Onedrive-Yahooforsub-Tao:Scripts-ssh-ssl-keys/SocialUpload/test.jpg
```

### 5.4 Verify public-link generation works

- [ ] Run:

```bash
rclone link Onedrive-Yahooforsub-Tao:Scripts-ssh-ssl-keys/SocialUpload/test.jpg --onedrive-link-scope anonymous --onedrive-link-type view
```

Expected:

- [ ] you get a public URL

If it fails because anonymous sharing is blocked:

- [ ] either enable public sharing for that drive
- [ ] or set `SAU_PUBLIC_URL_TEMPLATE` to your own public-serving layer

Do not continue until this works, because multiple publishing flows depend on public media URLs.

---

## Phase 6 — Choose your first platform order

For fastest success, do platforms in this order:

1. Telegram
2. Reddit
3. YouTube
4. Facebook / Instagram
5. Threads
6. TikTok
7. Discord
8. legacy/browser platforms
9. X/Twitter legacy path

If you want the quickest proof of life, start with:

- [ ] Telegram
- [ ] Reddit or YouTube

---

## Phase 7 — Configure Telegram first

This is usually the easiest structured platform.

### 7.1 Create bot

- [ ] Open Telegram
- [ ] Chat with `@BotFather`
- [ ] Run `/newbot`
- [ ] Create bot name + username
- [ ] Copy bot token

### 7.2 Add token to `.env`

Example:

```env
TELEGRAM_BOT_TOKEN_BRAND_A=123456:ABCDEF...
```

### 7.3 Decide target destination

- [ ] Public channel username like `@my_channel`
- [ ] Group/supergroup chat ID like `-100...`

### 7.4 Add bot to destination

- [ ] Add bot to target channel/group
- [ ] If channel, make bot admin if needed

### 7.5 Create account in Account Management

- [ ] Open **Account Management**
- [ ] Create or choose a **Profile**
- [ ] Add platform = `telegram`
- [ ] Set `chatId`
- [ ] Set `botTokenEnv = TELEGRAM_BOT_TOKEN_BRAND_A`
- [ ] Save account

### 7.6 Verify connection

- [ ] Click **Check Telegram connection**
- [ ] Confirm connection passes

---

## Phase 8 — Configure Reddit

### 8.1 Create Reddit app

- [ ] Log in to Reddit with the posting account
- [ ] Open `https://www.reddit.com/prefs/apps`
- [ ] Create app type = **web app**
- [ ] Set redirect URI to:
  - `https://up.iamwillywang.com/oauth/reddit/callback`
  - or your overridden public callback URL

### 8.2 Put credentials into `.env`

```env
REDDIT_CLIENT_ID=...
REDDIT_CLIENT_SECRET=...
REDDIT_REDIRECT_URI=https://up.iamwillywang.com/oauth/reddit/callback
SAU_REDDIT_CALLBACK_URL=https://up.iamwillywang.com/oauth/reddit/callback
REDDIT_USER_AGENT=social-auto-upload/1.0 (contact: you@example.com)
```

### 8.3 Create Reddit account entry in UI

- [ ] Add platform = `reddit`
- [ ] Set target subreddit list
- [ ] Set `clientIdEnv = REDDIT_CLIENT_ID`
- [ ] Set `clientSecretEnv = REDDIT_CLIENT_SECRET`
- [ ] Optional: set `userAgent`
- [ ] Save account

### 8.4 Connect Reddit via UI

- [ ] Click **Connect with Reddit**
- [ ] Approve access
- [ ] Confirm callback completes
- [ ] Confirm OAuth status page shows connected state

### 8.5 Refresh / validate

- [ ] Click **Refresh Reddit token**
- [ ] Confirm no errors

---

## Phase 9 — Configure YouTube

### 9.1 Create Google OAuth app

- [ ] Open Google Cloud Console
- [ ] Enable **YouTube Data API v3**
- [ ] Configure OAuth consent screen
- [ ] Create OAuth client for **Web application**
- [ ] Add redirect URI:
  - `https://up.iamwillywang.com/oauth/youtube/callback`

### 9.2 Put credentials into `.env`

```env
YT_CLIENT_ID=...
YT_CLIENT_SECRET=...
YOUTUBE_REDIRECT_URI=https://up.iamwillywang.com/oauth/youtube/callback
SAU_YOUTUBE_CALLBACK_URL=https://up.iamwillywang.com/oauth/youtube/callback
```

### 9.3 Create YouTube account entry in UI

- [ ] Add platform = `youtube`
- [ ] Set `channelId` if known
- [ ] Set `clientIdEnv = YT_CLIENT_ID`
- [ ] Set `clientSecretEnv = YT_CLIENT_SECRET`
- [ ] Save account

### 9.4 Connect YouTube via UI

- [ ] Click **Connect with YouTube**
- [ ] Approve Google OAuth
- [ ] Confirm channel title appears in UI
- [ ] Confirm OAuth status page shows connected state

### 9.5 Refresh / validate

- [ ] Click **Refresh YouTube token**
- [ ] Confirm no errors

---

## Phase 10 — Configure Meta app for Facebook + Instagram

Use one Meta app for both flows.

### 10.1 Create Meta app

- [ ] Create Meta app
- [ ] Add relevant Facebook Login / Graph / Instagram products
- [ ] Add redirect URI:
  - `https://up.iamwillywang.com/oauth/meta/callback`

### 10.2 Put Meta app credentials into `.env`

```env
META_APP_ID=...
META_APP_SECRET=...
META_REDIRECT_URI=https://up.iamwillywang.com/oauth/meta/callback
SAU_META_CALLBACK_URL=https://up.iamwillywang.com/oauth/meta/callback
```

### 10.3 Facebook Page account entry

- [ ] Add platform = `facebook`
- [ ] Save account
- [ ] Click **Connect with Facebook**
- [ ] Approve Meta OAuth with a user who manages the target Page
- [ ] Confirm page name appears
- [ ] Confirm OAuth status page works

### 10.4 Instagram Professional account entry

Prerequisite:

- [ ] Instagram account is Professional
- [ ] It is linked properly to the relevant Meta/Facebook business/page setup

Then:

- [ ] Add platform = `instagram`
- [ ] Save account
- [ ] Click **Connect with Instagram**
- [ ] Approve Meta OAuth
- [ ] Confirm Instagram username appears
- [ ] Confirm OAuth status page works

### 10.5 Refresh / validate Meta accounts

- [ ] Click **Refresh Facebook token**
- [ ] Click **Refresh Instagram token**
- [ ] Confirm no errors

---

## Phase 11 — Configure Threads

### 11.1 Create Threads app/use case

- [ ] Create Meta app / Threads use case as required by current Meta dashboard
- [ ] Add redirect URI:
  - `https://up.iamwillywang.com/oauth/threads/callback`

### 11.2 Put Threads credentials into `.env`

```env
THREADS_APP_ID=...
THREADS_APP_SECRET=...
THREADS_REDIRECT_URI=https://up.iamwillywang.com/oauth/threads/callback
SAU_THREADS_CALLBACK_URL=https://up.iamwillywang.com/oauth/threads/callback
```

### 11.3 Create Threads account entry in UI

- [ ] Add platform = `threads`
- [ ] Save account
- [ ] Click **Connect with Threads**
- [ ] Approve OAuth
- [ ] Confirm username appears
- [ ] Confirm OAuth status page works

### 11.4 Refresh / validate

- [ ] Click **Refresh Threads token**
- [ ] Confirm no errors

---

## Phase 12 — Configure TikTok

Do this after LLM + OneDrive + basic structured accounts are already working.

### 12.1 Create TikTok developer app

- [ ] Create app on TikTok for Developers
- [ ] Add only products actually used by this repo:
  - Login Kit for Web
  - Content Posting API
  - Webhooks
- [ ] Add scopes:
  - `user.info.basic`
  - `video.upload`
  - `video.publish`
- [ ] Add redirect URI:
  - `https://up.iamwillywang.com/oauth/tiktok/callback`
- [ ] Add webhook URL:
  - `https://up.iamwillywang.com/webhooks/tiktok`

### 12.2 Put credentials into `.env`

```env
TIKTOK_CLIENT_KEY=...
TIKTOK_CLIENT_SECRET=...
TIKTOK_REDIRECT_URI=https://up.iamwillywang.com/oauth/tiktok/callback
SAU_TIKTOK_CALLBACK_URL=https://up.iamwillywang.com/oauth/tiktok/callback
```

### 12.3 Create TikTok account entry in UI

- [ ] Add platform = `tiktok`
- [ ] Save account
- [ ] Click **Connect with TikTok**
- [ ] Approve TikTok OAuth
- [ ] Confirm callback status page shows callback and/or webhook receipts

### 12.4 Refresh / validate

- [ ] Click **Refresh TikTok token**
- [ ] Confirm no errors

### 12.5 Policy check

- [ ] Make sure TikTok profile configuration does **not** use prohibited branded/promotional watermark behavior for direct-post API content

---

## Phase 13 — Configure Discord

### 13.1 Create webhook

- [ ] Open Discord server
- [ ] Open channel settings
- [ ] Integrations -> Webhooks
- [ ] Create webhook
- [ ] Copy webhook URL

### 13.2 Put it into `.env`

```env
DISCORD_WEBHOOK_URL_BRAND_A=https://discord.com/api/webhooks/...
```

### 13.3 Create Discord account entry in UI

- [ ] Add platform = `discord`
- [ ] Set `webhookUrlEnv = DISCORD_WEBHOOK_URL_BRAND_A`
- [ ] Save account
- [ ] Click **Check Discord connection**
- [ ] Confirm no errors

---

## Phase 14 — Create your first Profile properly

### 14.1 Create Profile

- [ ] Open **Account Management**
- [ ] Create a new Profile
- [ ] Give it a real name like `Brand A`

### 14.2 Fill Profile settings

At minimum set:

- [ ] `System Prompt`
- [ ] `watermark` only for platforms where it is acceptable
- [ ] `contactDetails`
- [ ] `ctaText`

### 14.3 Add accounts under that Profile

Recommended first working set:

- [ ] Telegram
- [ ] Reddit or YouTube
- [ ] optionally Facebook / Instagram / Threads / TikTok / Discord

---

## Phase 15 — Verify dashboard / account-management health state

### 15.1 Dashboard

- [ ] Open Dashboard
- [ ] Confirm accounts appear
- [ ] Confirm maintenance status card appears
- [ ] Confirm expiry/risk section appears

### 15.2 Account Management

- [ ] Confirm accounts appear under the correct Profile
- [ ] Confirm OAuth status pages open
- [ ] Confirm connection health rows show current identity/timestamps
- [ ] Confirm filters and sort sync into the URL
- [ ] Confirm table sorting works

---

## Phase 16 — Upload real media

### 16.1 Material upload

- [ ] Open **Material Management**
- [ ] Upload at least:
  - one video
  - one or more images

### 16.2 Confirm media is visible

- [ ] Uploaded files appear in the UI

---

## Phase 17 — Prepare your first campaign

Use a platform mix that is easiest to validate first.

Recommended first publish attempt:

- [ ] Telegram
- [ ] Reddit or YouTube

### 17.1 In Publish Center

- [ ] Select your Profile
- [ ] Select connected accounts
- [ ] Upload/select media group or assets as required by current flow
- [ ] Enter title / notes / hashtags / contact details / CTA if needed

### 17.2 Run prepare flow

- [ ] Confirm transcript generation works for video
- [ ] Confirm caption generation works
- [ ] Confirm Google Sheet export works
- [ ] Confirm OneDrive public media URLs exist

If any of these fail, stop and fix them before live publishing.

---

## Phase 18 — Verify Google Sheets export

### 18.1 Expected outcome

- [ ] Spreadsheet/tab is created
- [ ] Sheet title follows expected date/profile naming
- [ ] Rows are populated for supported non-Telegram/non-Patreon/non-Discord outputs

### 18.2 If using an existing spreadsheet

- [ ] Confirm service-account email has access

---

## Phase 19 — Publish your first safe test

### 19.1 Start with safest platforms

Recommended first test set:

- [ ] Telegram
- [ ] Discord
- [ ] Reddit test subreddit you control
- [ ] YouTube test/private upload

### 19.2 Avoid risky first live tests on

- [ ] TikTok production direct posting
- [ ] Meta production pages/accounts

until the earlier checks are stable.

### 19.3 Confirm result

- [ ] Job appears in Jobs / worker flow
- [ ] Platform receives content
- [ ] No auth/permission errors

---

## Phase 20 — Only then move to Meta + TikTok live publishing

### 20.1 Facebook

- [ ] Publish a harmless test post to a test Page
- [ ] Confirm correct page identity

### 20.2 Instagram

- [ ] Publish a harmless test post to a test Professional account
- [ ] Confirm correct account identity

### 20.3 Threads
n
- [ ] Publish a harmless test post to a test Threads account
- [ ] Confirm correct account identity

### 20.4 TikTok

- [ ] Confirm TikTok app review/approval is complete if required for your account/app state
- [ ] Use non-watermarked compliant media
- [ ] Publish a safe test post or draft
- [ ] Confirm callback/webhook status updates

---

## Phase 21 — Worker / maintenance sanity

### 21.1 Worker

- [ ] Start worker if you are using queued jobs:

```bash
python -m myUtils.worker --max-concurrent 3
```

### 21.2 Maintenance UI

- [ ] Open Dashboard
- [ ] Confirm maintenance card is healthy
- [ ] Open Account Management
- [ ] Confirm maintenance status banner is healthy
- [ ] Test a manual maintenance refresh

---

## Phase 22 — What “fully working” means

You can consider the system working when all boxes below are true:

- [ ] backend starts cleanly
- [ ] frontend starts cleanly
- [ ] Dashboard loads
- [ ] Account Management loads
- [ ] Publish Center loads
- [ ] root `.env` is populated with real secrets
- [ ] Google Sheets export works
- [ ] OneDrive / rclone upload + public URLs work
- [ ] LLM transcript/caption generation works
- [ ] at least one platform account connects successfully
- [ ] at least one account refresh/check action succeeds
- [ ] at least one campaign prepare flow succeeds end-to-end
- [ ] at least one real publish test succeeds end-to-end

---

## Phase 23 — Known current-state limitations

These are not setup mistakes; they are how the current repo is built.

- [ ] X/Twitter is still legacy cookie/browser-driven, not part of the newer structured OAuth account flow
- [ ] Patreon is not a finished direct-publish integration in this codebase
- [ ] Domestic legacy browser platforms still rely mainly on cookie/session automation rather than developer-app OAuth setup

---

## Phase 24 — If something fails, debug in this order

1. [ ] `.env` missing or wrong env var names
2. [ ] callback URI mismatch between provider console and app config
3. [ ] public domain not reachable from provider
4. [ ] OneDrive public links not actually public
5. [ ] Google service account cannot access target sheet
6. [ ] account connected but wrong page/channel/subreddit/account selected
7. [ ] provider app review/permissions not approved yet
8. [ ] TikTok/Meta platform policy mismatch
9. [ ] worker not running for queued jobs

---

## Short version: first 10 things to do

If you want the absolute shortest startup list:

- [ ] Create root `.env`
- [ ] Install backend deps
- [ ] Install frontend deps
- [ ] Install Playwright Chromium
- [ ] Start backend + frontend
- [ ] Configure LLM envs
- [ ] Configure Google service account
- [ ] Configure OneDrive / rclone and confirm public links
- [ ] Connect Telegram
- [ ] Connect Reddit or YouTube

That gets you to the first realistic end-to-end success path fastest.
