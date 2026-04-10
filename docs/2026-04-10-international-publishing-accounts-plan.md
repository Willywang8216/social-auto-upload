# International publishing accounts plan

## Current state

- The persisted account table now supports:
  - `platform_key`
  - `auth_mode`
  - `metadata_json`
  alongside the legacy domestic `type/filePath/userName/status` fields.
- `/getAccounts` and `/getValidAccounts` now return normalized account objects rather than requiring positional-array handling.
- Frontend `accountStore` normalizes both legacy rows and normalized account objects.
- `AccountManagement.vue` now supports:
  - domestic QR/cookie platforms: 小紅書 / 影片號 / 抖音 / 快手
  - international placeholder accounts: X / Twitter, Threads, Facebook, Reddit, TikTok, YouTube
  - metadata editing
  - cookie upload/download for domestic accounts
- Profile-level `contentAccounts` now support per-account copy rules, but they are not login-backed publishing accounts.
- The current publish center routes only domestic publishing targets through numeric `publishType` / `accountType`.

## Goal

Add real publishing-account support for:

- X / Twitter
- Threads
- Facebook
- Reddit
- TikTok
- YouTube

This means each platform account can be:

1. created and stored,
2. authenticated with platform-specific credentials,
3. validated,
4. selected in profiles and publish flows,
5. used by a publishing adapter.

## Recommended architecture

### 1. Split "content persona" from "publishing account"

Keep both concepts:

- `contentAccounts`: per-profile writing persona and copy rules
- `publishing accounts`: real platform credentials and runtime state

The linkage should be:

- Profile selects one or more publishing accounts
- Each publishing account may optionally reference one content persona

This avoids mixing "how to write" with "how to authenticate".

### 2. Replace numeric-only platform typing

Current numeric type values are too brittle for expansion. Move toward:

- a canonical platform key, e.g. `twitter`, `threads`, `facebook`, `reddit`, `tiktok`, `youtube`
- keep a compatibility layer for old domestic numeric types during migration

Recommended backend shape:

- `accounts.platform_key`
- optional `accounts.legacy_type`

### 3. Expand the account schema

Recommended new table or migrated schema:

- `id`
- `platform_key`
- `display_name`
- `auth_mode`
- `credential_path` or encrypted credential blob reference
- `status`
- `last_validated_at`
- `metadata_json`
- `created_at`
- `updated_at`

`metadata_json` should hold platform-specific extras such as:

- channel ID
- page ID
- subreddit
- account handle
- publish defaults

### 4. Introduce platform adapters

Create one adapter per platform behind a common interface:

- `login()`
- `validate()`
- `publish(post_payload)`
- `refresh_auth()` when supported

Suggested modules:

- `uploader/twitter_uploader/`
- `uploader/threads_uploader/`
- `uploader/facebook_uploader/`
- `uploader/reddit_uploader/`
- `uploader/tiktok_uploader/`
- `uploader/youtube_uploader/`

Each adapter should translate a normalized publish payload into platform-native API or browser automation calls.

## Delivery phases

### Phase 1: data model and account management

- Add canonical platform keys in backend and frontend
- Add schema migration for international publishing accounts
- Update account list API to return normalized objects instead of positional arrays
- Update `AccountManagement.vue` to support new platform options and credential forms

Acceptance criteria:

- new international accounts can be created, listed, edited, and deleted
- domestic accounts still render correctly
- cookie-backed accounts receive a default cookie file path when created manually

Status: completed on 2026-04-10.

### Phase 2: account validation

- Add per-platform validation methods
- Surface validation status in account management
- Add explicit error details for expired or invalid credentials
- Store `last_validated_at` and optionally `last_error` so account lists can show why validation failed

Acceptance criteria:

- each supported platform can report a meaningful validation result

### Phase 3: publish center integration

- Extend publish center platform map beyond domestic targets
- Allow selecting international publishing accounts directly
- Bind a content persona to a publishing account when generating final post text
- Keep domestic `postVideo` compatibility by introducing a normalized publish target layer instead of replacing the current numeric flow in one step

Acceptance criteria:

- user can generate copy with a profile persona and route it to real international publishing accounts

### Phase 4: automated publishing

- Implement actual post submission per platform
- Handle media upload, post text, optional first comment, CTA, and thumbnails where supported
- Add per-platform publish result logs

Acceptance criteria:

- at least one happy-path publish flow per platform is verified in runtime testing

## Platform-specific notes

### X / Twitter

- Prefer official API support where possible
- Need tweet length enforcement and media upload flow
- Handle OAuth token lifecycle cleanly

### Threads

- Need to confirm the supported publishing path in the target environment
- May depend on Meta-side APIs or browser automation fallback

### Facebook

- Distinguish page publishing from personal profile flows
- Page ID and page token handling should live in metadata

### Reddit

- Needs subreddit-specific configuration
- Copy generation should avoid promotional tone by default

### TikTok

- Existing repo already has TikTok upload logic in examples/uploader space
- Reuse the existing TikTok automation path instead of inventing a separate mechanism

### YouTube

- Community posts and video upload/description flows are different capabilities
- Model them as separate capabilities on the same account when needed

## Risks

- Different platforms have incompatible auth and publish rules
- Browser automation is more fragile than API-based publishing
- Current frontend account store expects positional rows, which will make mixed old/new account models awkward until normalized
- Migration must preserve existing domestic accounts without breaking publish center logic

## Minimal next implementation slice

If implementation starts immediately, the safest first slice is:

1. add validation adapters for Twitter/X, Reddit, Facebook, YouTube, TikTok, and Threads,
2. persist validation timestamps and error messages,
3. add publish-center-side normalized platform selection for international accounts,
4. keep actual publish execution limited to one or two international platforms first,
5. add runtime-tested publishing adapters platform by platform.
