# International publishing accounts plan

## Current state

- The persisted account table is `user_info(id, type, filePath, userName, status)`.
- Frontend account management only recognizes four platform types:
  - 1: 小紅書
  - 2: 影片號
  - 3: 抖音
  - 4: 快手
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

### Phase 2: account validation

- Add per-platform validation methods
- Surface validation status in account management
- Add explicit error details for expired or invalid credentials

Acceptance criteria:

- each supported platform can report a meaningful validation result

### Phase 3: publish center integration

- Extend publish center platform map beyond domestic targets
- Allow selecting international publishing accounts directly
- Bind a content persona to a publishing account when generating final post text

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

1. add canonical platform keys and normalized account API responses,
2. migrate account management/store to object-based accounts,
3. support creating placeholder international publishing accounts,
4. keep actual publish execution limited to existing domestic platforms until adapters are added.
